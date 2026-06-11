"""Outbound-socket server + IVR flow interpreter (Plan.md §2, §5, §6).

Every answered call is originated with `&socket(127.0.0.1:8084 async full)`,
so FreeSWITCH opens a TCP connection here per call. The session handshake
(`connect` + `myevents`) locks the connection to that channel; the interpreter
then walks the campaign's flow JSON: playback prompts, waits for DTMF, etc.

This module knows nothing about the database or campaigns: the worker
(app/jobs.py) registers a CallContext per origination UUID and consumes the
outcome from it. The interpreter talks to an abstract session (play /
wait_digit / hangup), so unit tests drive it with a scripted fake (§16 lvl 1).
"""

import asyncio
import logging
import uuid as uuid_mod

from . import esl

logger = logging.getLogger(__name__)

MAX_STEPS = 30          # node transitions per call: graph cycles are legal (§5),
                        # infinite loops are not
EXECUTE_TIMEOUT = 180   # any single app (playback of a long message) must fit


class CallEnded(Exception):
    """The callee hung up (or the channel died) mid-flow."""

    def __init__(self, cause="NORMAL_CLEARING"):
        super().__init__(cause)
        self.cause = cause


class CallContext:
    """Per-call link between the worker and the socket server."""

    def __init__(self, number_id, flow, prompt_files):
        self.number_id = number_id
        self.flow = flow
        self.prompt_files = prompt_files  # prompt name -> WAV path
        self.done = asyncio.get_running_loop().create_future()


# origination_uuid -> CallContext, registered by the worker BEFORE originate
REGISTRY = {}


class OutboundSession:
    """One FreeSWITCH outbound-socket connection == one live call."""

    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer
        self.channel = {}        # channel data from the connect handshake
        self.uuid = None
        self.ended = asyncio.Event()
        self.hangup_cause = None
        self.dtmf = asyncio.Queue()
        self._replies = None     # FIFO of futures, created in handshake
        self._executes = {}      # Application-UUID -> future
        self._reader_task = None
        self._digit_seq = 0      # unique channel-var name per wait_digit call

    async def handshake(self):
        self._writer.write(b"connect\n\n")
        await self._writer.drain()
        frame = await asyncio.wait_for(esl.read_frame(self._reader), 10)
        if frame is None:
            raise esl.ESLError("FreeSWITCH closed the outbound socket at connect")
        self.channel = frame["headers"]
        self.uuid = (self.channel.get("Unique-ID")
                     or self.channel.get("Channel-Unique-ID")
                     or self.channel.get("Caller-Unique-ID"))
        if not self.uuid:
            raise esl.ESLError("no channel UUID in outbound connect data")
        self._replies = []
        self._reader_task = asyncio.create_task(self._read_loop())
        # deliver this channel's events here; keep the socket up through hangup
        await self._command("linger 5")
        await self._command("myevents plain")

    async def _command(self, cmd, timeout=15):
        fut = asyncio.get_running_loop().create_future()
        self._replies.append(fut)
        self._writer.write((cmd + "\n\n").encode())
        await self._writer.drain()
        try:
            frame = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            if fut in self._replies:
                self._replies.remove(fut)
            raise
        return frame["headers"].get("Reply-Text", "")

    async def _sendmsg(self, headers, timeout=15):
        lines = ["sendmsg"] + [f"{k}: {v}" for k, v in headers.items()]
        frame = await self._roundtrip("\n".join(lines), timeout)
        return frame["headers"].get("Reply-Text", "")

    async def api(self, cmd, timeout=15):
        """Blocking api command on this socket (full mode); returns the body."""
        frame = await self._roundtrip(f"api {cmd}", timeout)
        if frame["headers"].get("Content-Type") == "api/response":
            return frame["body"]
        return frame["headers"].get("Reply-Text", "")

    async def _roundtrip(self, payload, timeout):
        fut = asyncio.get_running_loop().create_future()
        self._replies.append(fut)
        self._writer.write((payload + "\n\n").encode())
        await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            if fut in self._replies:
                self._replies.remove(fut)
            raise

    async def execute(self, app, arg="", timeout=EXECUTE_TIMEOUT):
        """Run a dialplan application; returns its CHANNEL_EXECUTE_COMPLETE event."""
        if self.ended.is_set():
            raise CallEnded(self.hangup_cause or "NORMAL_CLEARING")
        event_uuid = str(uuid_mod.uuid4())
        done = asyncio.get_running_loop().create_future()
        self._executes[event_uuid] = done
        headers = {
            "call-command": "execute",
            "execute-app-name": app,
            "Event-UUID": event_uuid,
            "event-lock": "true",
        }
        if arg:
            headers["execute-app-arg"] = arg
        try:
            reply = await self._sendmsg(headers)
            if not reply.startswith("+OK"):
                raise esl.ESLError(f"execute {app} refused: {reply}")
            return await asyncio.wait_for(done, timeout)
        finally:
            self._executes.pop(event_uuid, None)

    async def play(self, wav_path):
        await self.execute("playback", wav_path)

    async def wait_digit(self, timeout):
        """One DTMF digit, or None on timeout. Raises CallEnded if the call died.

        Implemented with play_and_get_digits, NOT by waiting for DTMF events:
        loopback channels (the §16 test path) never emit DTMF events —
        uuid_recv_dtmf only queues the digit into the channel input queue,
        which only a digit-reading application consumes. play_and_get_digits
        also picks up digits already queued during the message (barge-in).
        """
        if self.ended.is_set():
            raise CallEnded(self.hangup_cause or "NORMAL_CLEARING")
        if not self.dtmf.empty():  # real SIP calls also emit DTMF events
            return self.dtmf.get_nowait()
        self._digit_seq += 1
        var = f"ivr_digit_{self._digit_seq}"
        ms = max(1000, int(timeout * 1000))
        # min max tries timeout terminators file invalid_file var regexp:
        # \d accepts any digit — branch matching is the interpreter's job
        arg = (f"1 1 1 {ms} # silence_stream://250 silence_stream://250 "
               f"{var} \\d")
        event = await self.execute("play_and_get_digits", arg,
                                   timeout=timeout + 30)
        digit = (event or {}).get(f"variable_{var}")
        if digit is None:  # event lacked channel vars — ask the engine directly
            body = (await self.api(f"uuid_getvar {self.uuid} {var}")).strip()
            digit = "" if body.startswith("-ERR") or body == "_undef_" else body
        return digit or None

    async def hangup(self, cause="NORMAL_CLEARING"):
        if self.ended.is_set():
            return
        try:
            await self._sendmsg({"call-command": "hangup", "hangup-cause": cause})
        except (esl.ESLError, asyncio.TimeoutError, ConnectionError):
            pass  # the channel may already be gone — that's the goal anyway

    async def _read_loop(self):
        try:
            while True:
                frame = await esl.read_frame(self._reader)
                if frame is None:
                    break
                ctype = frame["headers"].get("Content-Type", "")
                if ctype in ("command/reply", "api/response"):
                    if self._replies:
                        fut = self._replies.pop(0)
                        if not fut.done():
                            fut.set_result(frame)
                elif ctype == "text/event-plain":
                    self._on_event(esl.parse_event(frame["body"]))
                elif ctype == "text/disconnect-notice":
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug("outbound session reader ended: %s", exc)
        finally:
            if not self.hangup_cause:
                self.hangup_cause = "NORMAL_CLEARING"
            self.ended.set()
            self._fail_pending()

    def _on_event(self, event):
        name = event.get("Event-Name", "")
        if name == "DTMF":
            digit = event.get("DTMF-Digit", "")
            if digit:
                self.dtmf.put_nowait(digit)
        elif name == "CHANNEL_EXECUTE_COMPLETE":
            fut = self._executes.get(event.get("Application-UUID", ""))
            if fut and not fut.done():
                fut.set_result(event)
        elif name in ("CHANNEL_HANGUP", "CHANNEL_HANGUP_COMPLETE"):
            self.hangup_cause = event.get("Hangup-Cause", "NORMAL_CLEARING")
            self.ended.set()
            self._fail_pending()

    def _fail_pending(self):
        exc = CallEnded(self.hangup_cause or "NORMAL_CLEARING")
        for fut in list(self._executes.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._executes.clear()
        for fut in list(self._replies or []):
            if not fut.done():
                fut.set_exception(exc)
        if self._replies:
            self._replies.clear()

    async def close(self):
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def run_flow(session, flow, prompt_files, on_digit=None):
    """Walk the §5 flow graph on a live session.

    Returns {"mark": None|"optout", "transferred": bool, "dtmf": str,
    "bridge_target": str|None}. CallEnded mid-flow is normal (the callee hung
    up): the partial outcome is returned, the cause is on session.hangup_cause.
    """
    outcome = {"mark": None, "transferred": False, "dtmf": "", "bridge_target": None}
    nodes = flow["nodes"]
    current = flow["start"]
    try:
        for _ in range(MAX_STEPS):
            node = nodes[current]
            ntype = node["type"]

            if ntype == "play":
                await session.play(prompt_files[node["prompt"]])
                if node.get("mark"):
                    outcome["mark"] = node["mark"]
                current = node["next"]

            elif ntype == "menu":
                if node.get("prompt"):
                    await session.play(prompt_files[node["prompt"]])
                branch = None
                # max_repeats = how many extra waiting rounds after the first
                for _attempt in range(int(node.get("max_repeats", 0)) + 1):
                    digit = await session.wait_digit(int(node["timeout_sec"]))
                    if digit is None:
                        continue
                    outcome["dtmf"] += digit
                    if on_digit:
                        on_digit(digit)
                    if digit in node["branches"]:
                        branch = node["branches"][digit]
                        break
                current = branch or node["on_timeout"]

            elif ntype == "bridge":
                if node.get("prompt"):
                    await session.play(prompt_files[node["prompt"]])
                # Stage 3 connects a live operator here; until then the call
                # ends politely after the prompt.
                outcome["bridge_target"] = "pending-stage-3"
                logger.info("bridge node reached — operator bridging is stage 3")
                await session.hangup()
                return outcome

            elif ntype == "hangup":
                await session.hangup()
                return outcome

        logger.warning("flow exceeded %d steps — hanging up (cycle guard)", MAX_STEPS)
        await session.hangup()
    except CallEnded:
        logger.info("callee hung up mid-flow (cause=%s)", session.hangup_cause)
    return outcome


async def handle_connection(reader, writer):
    """One answered call: handshake, look up its CallContext, run the flow."""
    session = OutboundSession(reader, writer)
    ctx = None
    try:
        await session.handshake()
        ctx = REGISTRY.pop(session.uuid, None)
        if ctx is None:
            logger.error("outbound socket for unknown call %s — hanging up", session.uuid)
            await session.hangup()
            return
        outcome = await run_flow(session, ctx.flow, ctx.prompt_files)
        outcome["hangup_cause"] = session.hangup_cause
        if not ctx.done.done():
            ctx.done.set_result(outcome)
    except Exception as exc:  # noqa: BLE001 — a broken call must not kill the server
        logger.exception("outbound session failed")
        if ctx and not ctx.done.done():
            ctx.done.set_exception(exc)
    finally:
        await session.close()


async def start_server(host="127.0.0.1", port=8084):
    server = await asyncio.start_server(handle_connection, host, port)
    logger.info("IVR outbound socket listening on %s:%s", host, port)
    return server
