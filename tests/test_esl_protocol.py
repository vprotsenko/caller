"""ESL wire protocol + InboundClient against an in-process fake FreeSWITCH.

No real FreeSWITCH needed (verification level 1).
"""

import asyncio
import urllib.parse

import pytest

from app import esl


def make_reader(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


# --- frame parsing -------------------------------------------------------------

async def test_read_frame_headers_only():
    reader = make_reader(b"Content-Type: auth/request\n\n")
    frame = await esl.read_frame(reader)
    assert frame["headers"]["Content-Type"] == "auth/request"
    assert frame["body"] == ""


async def test_read_frame_with_body_and_following_frame():
    reader = make_reader(
        b"Content-Type: api/response\nContent-Length: 5\n\nhello"
        b"Content-Type: command/reply\nReply-Text: +OK\n\n"
    )
    first = await esl.read_frame(reader)
    assert first["body"] == "hello"
    second = await esl.read_frame(reader)
    assert second["headers"]["Reply-Text"] == "+OK"
    assert await esl.read_frame(reader) is None  # clean EOF


async def test_read_frame_url_decodes_header_values():
    reader = make_reader(b"Reply-Text: %2BOK%20accepted\n\n")
    frame = await esl.read_frame(reader)
    assert frame["headers"]["Reply-Text"] == "+OK accepted"


async def test_read_frame_eof_mid_frame_raises():
    reader = make_reader(b"Content-Type: command/reply\n")  # no terminating blank line
    with pytest.raises(esl.ESLError):
        await esl.read_frame(reader)


def test_parse_event_headers_and_body():
    body = (
        "Event-Name: BACKGROUND_JOB\n"
        "Job-UUID: abc-123\n"
        "Content-Length: 14\n"
        "\n"
        "+OK call-uuid\n"
    )
    event = esl.parse_event(body)
    assert event["Event-Name"] == "BACKGROUND_JOB"
    assert event["Job-UUID"] == "abc-123"
    assert event["_body"].strip() == "+OK call-uuid"


def test_parse_event_url_decoded():
    event = esl.parse_event("Caller-Caller-ID-Name: Open%20Zap\n")
    assert event["Caller-Caller-ID-Name"] == "Open Zap"


# --- InboundClient against a scripted fake FreeSWITCH ---------------------------

def plain_event(headers: dict, body: str = "") -> bytes:
    """Encode a text/event-plain frame the way mod_event_socket does."""
    lines = [f"{k}: {urllib.parse.quote(v)}" for k, v in headers.items()]
    if body:
        lines.append(f"Content-Length: {len(body.encode())}")
    payload = "\n".join(lines) + "\n\n" + body
    head = f"Content-Type: text/event-plain\nContent-Length: {len(payload.encode())}\n\n"
    return head.encode() + payload.encode()


class FakeFS:
    """Minimal scripted mod_event_socket: auth + canned command replies."""

    def __init__(self):
        self.received = []
        self.server = None
        self.writer = None
        self._password = "testpass"

    async def start(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _serve(self, reader, writer):
        self.writer = writer
        writer.write(b"Content-Type: auth/request\n\n")
        while True:
            # commands arrive as "line\n\n"
            raw = await reader.readline()
            if not raw:
                return  # client disconnected (EOF) — end this handler
            line = raw.decode().strip()
            if not line:
                continue
            await reader.readline()  # the trailing blank line
            self.received.append(line)
            await self.respond(line, writer)

    async def respond(self, line, writer):
        if line.startswith("auth"):
            ok = line == f"auth {self._password}"
            reply = "+OK accepted" if ok else "-ERR invalid"
            writer.write(f"Content-Type: command/reply\nReply-Text: {reply}\n\n".encode())
        elif line.startswith("event plain"):
            writer.write(b"Content-Type: command/reply\nReply-Text: +OK event listener enabled plain\n\n")
        elif line.startswith("api status"):
            body = "UP 0 years, 0 days\n"
            writer.write(
                f"Content-Type: api/response\nContent-Length: {len(body)}\n\n{body}".encode())
        elif line.startswith("bgapi originate"):
            writer.write(
                b"Content-Type: command/reply\nReply-Text: +OK Job-UUID: job-1\nJob-UUID: job-1\n\n")
            # the job result follows as a BACKGROUND_JOB event
            writer.write(plain_event(
                {"Event-Name": "BACKGROUND_JOB", "Job-UUID": "job-1"},
                "+OK call-uuid-1\n"))
        await writer.drain()

    async def emit(self, headers, body=""):
        self.writer.write(plain_event(headers, body))
        await self.writer.drain()

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if self.writer:
            self.writer.close()


@pytest.fixture
async def fake_fs():
    fs = FakeFS()
    port = await fs.start()
    fs.port = port
    yield fs
    await fs.stop()


async def connected_client(fs, password="testpass"):
    client = esl.InboundClient("127.0.0.1", fs.port, password)
    await client.connect()
    return client


async def test_connect_auth_and_subscribe(fake_fs):
    client = await connected_client(fake_fs)
    assert client.connected
    assert fake_fs.received[0] == "auth testpass"
    assert fake_fs.received[1].startswith("event plain BACKGROUND_JOB")
    await client.close()


async def test_bad_password_raises_without_leaking_it(fake_fs):
    client = esl.InboundClient("127.0.0.1", fake_fs.port, "wrong-secret")
    with pytest.raises(esl.ESLError) as exc_info:
        await client.connect()
    assert "wrong-secret" not in str(exc_info.value)


async def test_api_returns_body(fake_fs):
    client = await connected_client(fake_fs)
    body = await client.api("status")
    assert body.startswith("UP")
    await client.close()


async def test_bgapi_resolves_via_background_job(fake_fs):
    client = await connected_client(fake_fs)
    result = await client.bgapi("originate loopback/9999 &playback(/x.wav)", timeout=5)
    assert result == "+OK call-uuid-1"
    await client.close()


async def test_expect_event_resolves_on_matching_uuid(fake_fs):
    client = await connected_client(fake_fs)
    fut = client.expect_event("CHANNEL_HANGUP_COMPLETE", "uuid-A")
    await fake_fs.emit({"Event-Name": "CHANNEL_HANGUP_COMPLETE",
                        "Unique-ID": "uuid-B",          # wrong call: must NOT resolve
                        "Hangup-Cause": "USER_BUSY"})
    await fake_fs.emit({"Event-Name": "CHANNEL_HANGUP_COMPLETE",
                        "Unique-ID": "uuid-A",
                        "Hangup-Cause": "NORMAL_CLEARING"})
    event = await asyncio.wait_for(fut, 5)
    assert event["Hangup-Cause"] == "NORMAL_CLEARING"
    await client.close()


async def test_connection_loss_fails_pending_waiters(fake_fs):
    client = await connected_client(fake_fs)
    fut = client.expect_event("CHANNEL_HANGUP_COMPLETE", "uuid-X")
    await fake_fs.stop()  # drops the TCP connection
    with pytest.raises(Exception):
        await asyncio.wait_for(fut, 5)
    assert not client.connected
    await client.close()
