"""Outbound-socket server + IVR flow interpreter.

Every answered call is originated with `&socket(127.0.0.1:8084 async full)`,
so FreeSWITCH opens a TCP connection here per call. The session handshake
(`connect` + `myevents`) locks the connection to that channel; the interpreter
then walks the campaign's flow JSON: playback prompts, waits for DTMF, etc.

This module knows nothing about the database or campaigns: the worker
(app/jobs.py) registers a CallContext per origination UUID and consumes the
outcome from it. The interpreter talks to an abstract session (play /
wait_digit / hangup), so unit tests drive it with a scripted fake.
"""

import asyncio
import logging
import uuid as uuid_mod

from . import amd as amd_mod, esl

logger = logging.getLogger(__name__)

MAX_STEPS = 30          # node transitions per call: graph cycles are legal,
                        # infinite loops are not. For deep menu trees the real
                        # cap scales with graph size — see run_flow()
# Initial silence before the FIRST prompt (ms): the callee brings the phone to
# their ear and the post-answer media renegotiation (jobs.media_reneg_after_answer)
# completes. Prompts themselves carry no lead-in — see jobs.prerender_prompts.
LEAD_IN_MS = 2000
EXECUTE_TIMEOUT = 180   # any single app (playback of a long message) must fit
AMD_TIMEOUT = 15        # seconds to let mod_amd reach a verdict
BEEP_TIMEOUT = 20       # seconds to wait for the voicemail beep (mod_avmd)


class CallEnded(Exception):
    """The callee hung up (or the channel died) mid-flow."""

    def __init__(self, cause="NORMAL_CLEARING"):
        super().__init__(cause)
        self.cause = cause


class CallContext:
    """Per-call link between the worker and the socket server."""

    def __init__(self, number_id, flow, prompt_files,
                 operators=None, ring_timeout=25, bridge_max=3600,
                 bridge_vars="", campaign_type="info", amd_enabled=False,
                 amd_available=False, main_prompt="main"):
        self.number_id = number_id
        self.flow = flow
        self.prompt_files = prompt_files  # prompt name -> WAV path
        self.operators = operators        # OperatorPool for bridge nodes (stage 3)
        self.ring_timeout = ring_timeout
        self.bridge_max = bridge_max
        self.bridge_vars = bridge_vars    # extra per-leg vars on the operator leg
        self.campaign_type = campaign_type
        self.amd_enabled = amd_enabled    # run AMD before the flow (stage 4)
        self.amd_available = amd_available  # mod_amd actually loaded in FS
        self.main_prompt = main_prompt    # prompt to drop on a voicemail
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
        self.bridged = False     # a CHANNEL_BRIDGE happened on this call
        self._beep = None        # future resolved when mod_avmd reports a beep

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
        # mod_avmd's beep is a CUSTOM event — subscribe explicitly (stage 4)
        await self._command("event plain CUSTOM avmd::beep")

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
            # FS (this build at least) sends the command/reply for a sendmsg
            # execute only AFTER the application finishes — together with
            # EXECUTE_COMPLETE. So the reply timeout has to cover the duration
            # of the playback/PAGD, not just the dispatch: with the default
            # 15 s, any prompt or announce+wait longer than 15 s killed the
            # session with a TimeoutError.
            reply = await self._sendmsg(headers, timeout=timeout)
            if not reply.startswith("+OK"):
                raise esl.ESLError(f"execute {app} refused: {reply}")
            return await asyncio.wait_for(done, timeout)
        finally:
            self._executes.pop(event_uuid, None)

    async def play(self, wav_path):
        await self.execute("playback", wav_path)

    async def detect_amd(self, amd_available=False, timeout=AMD_TIMEOUT):
        """Classify the answered call with mod_amd; return HUMAN/MACHINE/NOTSURE.

        Channel variables are read from the connect-handshake data
        (`variable_<name>`) and from app-completion events, NOT via `api` —
        the outbound socket rejects api commands ("-ERR command not found").

        A test override channel var (amd_test_result), set via originate vars,
        short-circuits the real module so loopback E2E can simulate a verdict
        — the same idea as uuid_recv_dtmf for digits.

        `amd_available` (mod_amd loaded — probed once by the worker over the
        inbound connection) gates the real app: a missing application must not
        be executed, it would tear the channel down. Absent mod_amd -> HUMAN
        (we never drop a call we couldn't classify).
        """
        override = self.channel.get("variable_amd_test_result", "").strip()
        if override:
            return amd_mod.normalize_verdict(override)
        if not amd_available:
            logger.info("mod_amd not loaded — treating answer as HUMAN")
            return amd_mod.HUMAN
        try:
            # mod_amd's `amd` app sets amd_result and fires when it decides
            event = await self.execute("amd", "", timeout=timeout)
        except esl.ESLError:
            return amd_mod.HUMAN
        return amd_mod.normalize_verdict((event or {}).get("variable_amd_result"))

    async def wait_beep(self, timeout=BEEP_TIMEOUT):
        """Best-effort wait for a voicemail beep via mod_avmd before dropping
        the message. Returns True if a beep was detected, False on timeout —
        either way the caller proceeds to play the message."""
        try:
            await self.execute("avmd_start", "", timeout=10)
        except esl.ESLError:
            logger.info("mod_avmd not available — dropping message without beep wait")
            return False
        beep = self._beep = asyncio.get_running_loop().create_future()
        try:
            await asyncio.wait_for(beep, timeout)
            detected = True
        except asyncio.TimeoutError:
            detected = False
        finally:
            self._beep = None
            try:
                await self.execute("avmd_stop", "", timeout=5)
            except (esl.ESLError, CallEnded):
                pass
        return detected

    async def wait_digit(self, timeout, prompt=None):
        """Play `prompt` (if given) and wait for one DTMF digit; None on
        timeout. Raises CallEnded if the call died.

        Implemented with ONE play_and_get_digits, NOT play + wait-for-events:
        - the prompt goes in as the PAGD file, so a digit BARGES IN — the
          announcement stops mid-word and the digit acts immediately (separate
          playback isn't interruptible: «pressed 2 — nothing happened»);
        - loopback channels (the E2E test path) never emit DTMF events —
          uuid_recv_dtmf only queues the digit into the channel input queue,
          which only a digit-reading application consumes; PAGD also picks up
          digits already queued during a previous prompt;
        - the self.dtmf event queue is deliberately NOT consulted: a digit
          arrives BOTH as an event and in the channel input queue, and reading
          both sources made the same press count twice (a stale event digit
          would instantly eat the next round).
        """
        if self.ended.is_set():
            raise CallEnded(self.hangup_cause or "NORMAL_CLEARING")
        self._digit_seq += 1
        var = f"ivr_digit_{self._digit_seq}"
        ms = max(1000, int(timeout * 1000))
        # min max tries timeout terminators file invalid_file var regexp:
        # \d accepts any digit — branch matching is the interpreter's job
        arg = (f"1 1 1 {ms} # {prompt or 'silence_stream://250'} "
               f"silence_stream://250 {var} \\d")
        event = await self.execute("play_and_get_digits", arg,
                                   timeout=timeout + 60)
        digit = (event or {}).get(f"variable_{var}")
        if digit is None:  # event lacked channel vars — ask the engine directly
            body = (await self.api(f"uuid_getvar {self.uuid} {var}")).strip()
            digit = "" if body.startswith("-ERR") or body == "_undef_" else body
        return digit or None

    async def bridge(self, dial_target, ring_timeout, max_seconds, extra_vars=""):
        """Bridge the callee to `dial_target` (e.g. user/1001@<domain>).

        Returns True if a real bridge happened (the operator answered) —
        tracked via CHANNEL_BRIDGE. hangup_after_bridge ends the call when
        either side hangs up; continue_on_fail keeps the channel alive when
        the operator does not answer, so the outcome can be recorded.
        extra_vars are per-leg channel vars on the operator leg (e.g.
        absolute_codec_string to avoid transcoding in the loopback test).
        """
        leg_vars = f"leg_timeout={int(ring_timeout)},hangup_after_bridge=true,continue_on_fail=true"
        if extra_vars:
            leg_vars += "," + extra_vars
        arg = f"{{{leg_vars}}}{dial_target}"
        try:
            event = await self.execute("bridge", arg, timeout=max_seconds)
            logger.info(
                "bridge to %s done: bridged=%s disposition=%s cause=%s",
                dial_target, self.bridged,
                (event or {}).get("variable_originate_disposition"),
                (event or {}).get("variable_last_bridge_hangup_cause")
                or (event or {}).get("variable_bridge_hangup_cause"))
        except CallEnded:
            # the conversation ended with a hangup — normal for a bridge
            logger.info("bridge to %s ended with hangup: bridged=%s",
                        dial_target, self.bridged)
        return self.bridged

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
        elif name == "CHANNEL_BRIDGE":
            self.bridged = True
        elif name == "CUSTOM" and event.get("Event-Subclass") == "avmd::beep":
            if self._beep and not self._beep.done():
                self._beep.set_result(True)
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


async def run_flow(session, flow, prompt_files, on_digit=None,
                   operators=None, ring_timeout=25, bridge_max=3600,
                   bridge_vars=""):
    """Walk the flow graph on a live session.

    Returns {"mark": None|"optout", "transferred": bool, "dtmf": str,
    "bridge_attempted": bool, "bridge_target": str|None}. CallEnded mid-flow
    is normal (the callee hung up): the partial outcome is returned, the
    cause is on session.hangup_cause.
    """
    outcome = {"mark": None, "transferred": False, "dtmf": "",
               "bridge_attempted": False, "bridge_target": None}
    nodes = flow["nodes"]
    current = flow["start"]
    # a human wandering a nested tree with «back» legitimately makes many
    # transitions, so the runaway guard grows with the graph
    step_limit = max(MAX_STEPS, 5 * len(nodes))
    try:
        for _ in range(step_limit):
            node = nodes[current]
            ntype = node["type"]

            if ntype == "play":
                await session.play(prompt_files[node["prompt"]])
                if node.get("mark"):
                    outcome["mark"] = node["mark"]
                current = node["next"]

            elif ntype == "menu":
                branch = None
                announce = (prompt_files[node["prompt"]]
                            if node.get("prompt") else None)
                # max_repeats = how many extra waiting rounds after the first.
                # The announcement replays on EVERY round (otherwise repeat
                # rounds are dead air) and goes through wait_digit so a digit
                # interrupts it mid-word (barge-in).
                for _attempt in range(int(node.get("max_repeats", 0)) + 1):
                    digit = await session.wait_digit(int(node["timeout_sec"]),
                                                     prompt=announce)
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
                outcome["bridge_attempted"] = True
                if node.get("prompt"):
                    await session.play(prompt_files[node["prompt"]])
                ext = await operators.acquire() if operators is not None else None
                if ext is None:
                    # nobody free/registered -> polite end; the worker maps
                    # bridge_attempted without transfer to missed-operator
                    logger.info("bridge: no free operator")
                    await session.hangup()
                    return outcome
                outcome["bridge_target"] = ext
                try:
                    bridged = await session.bridge(
                        operators.dial_target(ext), ring_timeout, bridge_max,
                        extra_vars=bridge_vars)
                finally:
                    operators.release(ext)
                outcome["transferred"] = bool(bridged)
                await session.hangup()
                return outcome

            elif ntype == "hangup":
                await session.hangup()
                return outcome

        logger.warning("flow exceeded %d steps — hanging up (cycle guard)", step_limit)
        await session.hangup()
    except CallEnded:
        logger.info("callee hung up mid-flow (cause=%s)", session.hangup_cause)
        # a hangup that ends the bridged conversation is still a transfer
        outcome["transferred"] = outcome["transferred"] or bool(
            getattr(session, "bridged", False))
    return outcome


async def run_call(session, ctx):
    """Full answered-call lifecycle: AMD → branch → flow.

    Returns the flow outcome dict augmented with "amd_result". On a MACHINE
    verdict the flow is skipped: info campaigns drop the message after the
    beep (voicemail-left), operator campaigns hang up (machine-hangup).
    """
    verdict = None
    if ctx.amd_enabled:
        try:
            verdict = await session.detect_amd(amd_available=ctx.amd_available)
        except CallEnded:
            # callee hung up during analysis — nothing reached, record no verdict
            return _flow_outcome(amd_result=None)
        action = amd_mod.decide(verdict, ctx.campaign_type)
        if action == amd_mod.MACHINE_HANGUP:
            await session.hangup()
            return _flow_outcome(amd_result=verdict, amd_action=action)
        if action == amd_mod.VOICEMAIL:
            try:
                await session.wait_beep()
                await session.play(ctx.prompt_files[ctx.main_prompt])
            except CallEnded:
                pass
            await session.hangup()
            return _flow_outcome(amd_result=verdict, amd_action=action)
        # CONTINUE (HUMAN / NOTSURE) falls through to the normal flow

    try:
        await session.play(f"silence_stream://{LEAD_IN_MS}")
    except CallEnded:
        return _flow_outcome(amd_result=verdict)
    outcome = await run_flow(session, ctx.flow, ctx.prompt_files,
                             operators=ctx.operators,
                             ring_timeout=ctx.ring_timeout,
                             bridge_max=ctx.bridge_max,
                             bridge_vars=ctx.bridge_vars)
    outcome["amd_result"] = verdict
    return outcome


def _flow_outcome(amd_result=None, amd_action=None):
    return {"mark": None, "transferred": False, "dtmf": "",
            "bridge_attempted": False, "bridge_target": None,
            "amd_result": amd_result, "amd_action": amd_action}


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
        outcome = await run_call(session, ctx)
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
