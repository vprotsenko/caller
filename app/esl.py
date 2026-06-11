"""Minimal asyncio ESL (FreeSWITCH Event Socket Layer) inbound client.

Decision (Plan.md §13): the default candidate `greenswitch` is gevent-based and
cannot share a process with the uvicorn/asyncio loop that serves the API, so
v2 uses this hand-rolled client instead. The wire protocol is plain text
(RFC822-style headers, an optional Content-Length payload, frames separated by
a blank line), small enough to implement directly and unit-test without a live
FreeSWITCH (Plan.md §16 level 1).

Inbound mode only (connect to FreeSWITCH :8021, auth, send commands, receive
events). The outbound-socket server that drives the IVR per answered call is
stage 2 and will live in app/ivr.py on top of the same frame parser.

SECURITY: the ESL password is sent on the socket but never logged and never
included in raised exceptions.
"""

import asyncio
import collections
import logging
import urllib.parse

logger = logging.getLogger(__name__)


class ESLError(Exception):
    """Protocol violation, auth failure or connection loss."""


async def read_frame(reader):
    """Read one ESL frame: header lines, blank line, optional payload.

    Returns {"headers": {...}, "body": str} or None on clean EOF. Header
    values are URL-decoded (FreeSWITCH percent-encodes them in plain mode).
    """
    headers = {}
    while True:
        line = await reader.readline()
        if not line:
            if headers:
                raise ESLError("connection closed mid-frame")
            return None
        line = line.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if headers:
                break
            continue  # tolerate stray blank lines between frames
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip()] = urllib.parse.unquote(value.strip())
    body = ""
    length = int(headers.get("Content-Length", 0) or 0)
    if length:
        body = (await reader.readexactly(length)).decode("utf-8", "replace")
    return {"headers": headers, "body": body}


def parse_event(body):
    """Parse a text/event-plain payload into a flat dict of event headers.

    The payload is itself a header block (URL-encoded values); if it carries
    its own Content-Length, the remainder is the event body, stored under the
    pseudo-key "_body".
    """
    event = {}
    lines = body.split("\n")
    rest_at = len(lines)
    for i, line in enumerate(lines):
        if not line.strip():
            rest_at = i + 1
            break
        key, sep, value = line.partition(":")
        if sep:
            event[key.strip()] = urllib.parse.unquote(value.strip())
    if "Content-Length" in event:
        event["_body"] = "\n".join(lines[rest_at:])
    return event


class InboundClient:
    """One persistent inbound ESL connection.

    Commands are answered strictly in order on the socket, so replies are
    matched to callers through a FIFO of futures. BACKGROUND_JOB results and
    per-UUID event waiters are matched by their respective UUID headers.
    """

    SUBSCRIBE = ("BACKGROUND_JOB", "CHANNEL_ANSWER", "CHANNEL_HANGUP_COMPLETE")

    def __init__(self, host="127.0.0.1", port=8021, password=""):
        self.host = host
        self.port = port
        self._password = password
        self._reader = None
        self._writer = None
        self._reader_task = None
        self._replies = collections.deque()  # FIFO of futures for command replies
        self._jobs = {}          # Job-UUID -> future (resolved with the job body)
        self._orphan_jobs = {}   # job finished before the caller registered
        self._waiters = {}       # (event_name, unique_id) -> future
        self.connected = False

    async def connect(self, timeout=10):
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout)
        frame = await asyncio.wait_for(read_frame(self._reader), timeout)
        if not frame or frame["headers"].get("Content-Type") != "auth/request":
            raise ESLError("expected auth/request from FreeSWITCH")
        self.connected = True
        self._reader_task = asyncio.create_task(self._read_loop())
        reply = await self._command(f"auth {self._password}")
        if "+OK" not in reply:
            await self.close()
            raise ESLError("ESL authentication failed")  # password never in the message
        reply = await self._command("event plain " + " ".join(self.SUBSCRIBE))
        if not reply.startswith("+OK"):
            await self.close()
            raise ESLError(f"event subscription failed: {reply}")
        logger.info("ESL connected to %s:%s", self.host, self.port)

    async def _command(self, cmd, timeout=15):
        """Send one command line, await its command/reply or api/response frame."""
        if not self.connected:
            raise ESLError("not connected")
        fut = asyncio.get_running_loop().create_future()
        self._replies.append(fut)
        self._writer.write((cmd + "\n\n").encode())
        await self._writer.drain()
        try:
            frame = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            # leave no stale future behind, or later replies would mismatch
            try:
                self._replies.remove(fut)
            except ValueError:
                pass
            raise
        if frame["headers"].get("Content-Type") == "api/response":
            return frame["body"]
        return frame["headers"].get("Reply-Text", "")

    async def api(self, cmd, timeout=15):
        """Blocking api command; returns the response body."""
        return await self._command(f"api {cmd}", timeout)

    async def bgapi(self, cmd, timeout=120):
        """Background api command; awaits the BACKGROUND_JOB result body."""
        reply = await self._command(f"bgapi {cmd}")
        # Reply-Text: "+OK Job-UUID: <uuid>"
        if "Job-UUID:" not in reply:
            raise ESLError(f"bgapi not accepted: {reply}")
        job_uuid = reply.split("Job-UUID:", 1)[1].strip()
        if job_uuid in self._orphan_jobs:           # result raced ahead of us
            return self._orphan_jobs.pop(job_uuid).strip()
        fut = asyncio.get_running_loop().create_future()
        self._jobs[job_uuid] = fut
        try:
            body = await asyncio.wait_for(fut, timeout)
        finally:
            self._jobs.pop(job_uuid, None)
        return body.strip()

    def expect_event(self, event_name, unique_id):
        """Future resolved with the event dict for (event_name, Unique-ID).

        Register BEFORE triggering the action that emits the event (e.g. pass
        origination_uuid to originate), otherwise the event can be missed.
        """
        fut = asyncio.get_running_loop().create_future()
        self._waiters[(event_name, unique_id)] = fut
        return fut

    def cancel_waiter(self, event_name, unique_id):
        fut = self._waiters.pop((event_name, unique_id), None)
        if fut and not fut.done():
            fut.cancel()

    async def _read_loop(self):
        err = None
        try:
            while True:
                frame = await read_frame(self._reader)
                if frame is None:
                    break
                ctype = frame["headers"].get("Content-Type", "")
                if ctype in ("command/reply", "api/response"):
                    if self._replies:
                        fut = self._replies.popleft()
                        if not fut.done():
                            fut.set_result(frame)
                elif ctype == "text/event-plain":
                    self._dispatch(parse_event(frame["body"]))
                elif ctype == "text/disconnect-notice":
                    logger.warning("ESL disconnect notice from FreeSWITCH")
                    break
        except Exception as exc:  # noqa: BLE001 — surface to all pending callers
            err = exc
        finally:
            self.connected = False
            self._fail_pending(err or ESLError("ESL connection closed"))

    def _dispatch(self, event):
        name = event.get("Event-Name", "")
        if name == "BACKGROUND_JOB":
            job_uuid = event.get("Job-UUID", "")
            fut = self._jobs.get(job_uuid)
            if fut and not fut.done():
                fut.set_result(event.get("_body", ""))
            elif fut is None:
                self._orphan_jobs[job_uuid] = event.get("_body", "")
            return
        fut = self._waiters.pop((name, event.get("Unique-ID", "")), None)
        if fut and not fut.done():
            fut.set_result(event)

    def _fail_pending(self, exc):
        for fut in list(self._replies):
            if not fut.done():
                fut.set_exception(exc)
        self._replies.clear()
        for fut in list(self._jobs.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._jobs.clear()
        for fut in list(self._waiters.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._waiters.clear()

    async def close(self):
        self.connected = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader_task = None
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
        self._fail_pending(ESLError("ESL connection closed"))
