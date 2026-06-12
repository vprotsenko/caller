"""IVR scenario: form -> JSON compiler + validator (Plan.md §5, §15).

The UI is a parameterized form (checkboxes + texts); this module compiles it
into the node-graph JSON stored as a snapshot in campaign.ivr_flow. The
runtime interpreter (app/ivr.py) only ever sees that JSON, so a richer editor
later does not touch the runtime.

Node types of the first version: play | menu | bridge | hangup.
Pure functions — fully covered by pytest without FreeSWITCH (§16 level 1).
"""

MAX_REPEATS_LIMIT = 5     # server-side cap (§5)
TIMEOUT_RANGE = (1, 30)   # seconds the menu waits for a digit
NODE_TYPES = ("play", "menu", "bridge", "hangup")

DEFAULT_CONNECT_TEXT = "Зачекайте, з'єднуємо з оператором"
DEFAULT_OPTOUT_TEXT = "Вас видалено зі списку"
# Шматки автогенерованого анонсу меню (цифри словами — TTS читає їх надійніше,
# ніж "1"/"0"). Без анонсу меню — мертва тиша: play_and_get_digits грає лише
# silence_stream, і абонент не знає, що можна щось натиснути.
MENU_OPERATOR_TEXT = "Щоб з'єднатися з оператором, натисніть один."
MENU_REPEAT_TEXT = "Щоб прослухати повідомлення ще раз, натисніть два."
MENU_OPTOUT_TEXT = "Щоб відписатися від дзвінків, натисніть нуль."


def menu_announcement(operator=False, repeat=False, optout=False):
    """Default menu announcement for the enabled options (UI mirrors this)."""
    parts = []
    if operator:
        parts.append(MENU_OPERATOR_TEXT)
    if repeat:
        parts.append(MENU_REPEAT_TEXT)
    if optout:
        parts.append(MENU_OPTOUT_TEXT)
    return " ".join(parts)


class FlowError(ValueError):
    """Invalid IVR form input or scenario JSON; .args[0] is user-readable."""


def compile_form(message_text, voice, ivr=None, voice_params=None):
    """Compile the §15 `ivr` form object into the §5 flow JSON.

    With no form (or everything disabled) the flow degenerates to
    play-message -> hangup — the v1 behaviour. `voice_params` (вже clamped
    upstream: speed/steps/silence) лягають у КОЖЕН промпт — снапшот flow
    самодостатній, resume/retry синтезують так само.
    """
    ivr = ivr or {}
    operator = ivr.get("operator") or {}
    repeat = ivr.get("repeat") or {}
    optout = ivr.get("optout") or {}
    has_menu = bool(operator.get("enabled") or repeat.get("enabled")
                    or optout.get("enabled"))

    def prompt(text):
        p = {"text": text, "voice": voice}
        if voice_params:
            p.update(voice_params)
        return p

    prompts = {"main": prompt(message_text)}
    nodes = {"bye": {"type": "hangup"}}

    if not has_menu:
        nodes["msg"] = {"type": "play", "prompt": "main", "next": "bye"}
        return {"start": "msg", "nodes": nodes, "prompts": prompts}

    raw_timeout = ivr.get("timeout_sec")
    timeout_sec = 5 if raw_timeout is None else int(raw_timeout)
    on_timeout = ivr.get("on_timeout") or "hangup"
    if on_timeout not in ("hangup",):
        raise FlowError(f"Невідома дія за таймаутом: {on_timeout}")
    raw_max = repeat.get("max") if repeat.get("enabled") else None
    max_repeats = 2 if raw_max is None else int(raw_max)

    branches = {}
    if operator.get("enabled"):
        text = (operator.get("connect_text") or "").strip() or DEFAULT_CONNECT_TEXT
        prompts["connecting"] = prompt(text)
        nodes["to_op"] = {"type": "bridge", "prompt": "connecting"}
        branches["1"] = "to_op"
    if repeat.get("enabled"):
        branches["2"] = "msg"
    if optout.get("enabled"):
        text = (optout.get("confirm_text") or "").strip() or DEFAULT_OPTOUT_TEXT
        prompts["optout_ok"] = prompt(text)
        nodes["optout"] = {"type": "play", "prompt": "optout_ok",
                           "mark": "optout", "next": "bye"}
        branches["0"] = "optout"

    menu_text = (ivr.get("menu_text") or "").strip() or menu_announcement(
        operator.get("enabled"), repeat.get("enabled"), optout.get("enabled"))
    prompts["menu"] = prompt(menu_text)
    nodes["msg"] = {"type": "play", "prompt": "main", "next": "menu"}
    nodes["menu"] = {
        "type": "menu",
        "prompt": "menu",
        "timeout_sec": timeout_sec,
        "max_repeats": max_repeats,
        "branches": branches,
        "on_timeout": "bye",
    }
    flow = {"start": "msg", "nodes": nodes, "prompts": prompts}
    validate(flow)  # the compiler must never emit an invalid flow
    return flow


def validate(flow):
    """Server-side §5 checks; raises FlowError with a readable message.

    Cycles in the graph are allowed (e.g. "2" -> replay message): the runtime
    bounds the number of node transitions instead (ivr.MAX_STEPS).
    """
    if not isinstance(flow, dict):
        raise FlowError("Сценарій має бути обʼєктом")
    nodes = flow.get("nodes")
    prompts = flow.get("prompts", {})
    if not isinstance(nodes, dict) or not nodes:
        raise FlowError("Сценарій без вузлів")
    start = flow.get("start")
    if start not in nodes:
        raise FlowError(f"Стартовий вузол «{start}» не існує")

    def check_ref(node_name, target):
        if target not in nodes:
            raise FlowError(f"Вузол «{node_name}» веде на неіснуючий «{target}»")

    for name, node in nodes.items():
        ntype = node.get("type")
        if ntype not in NODE_TYPES:
            raise FlowError(f"Вузол «{name}»: невідомий тип «{ntype}»")
        if ntype in ("play", "bridge", "menu"):
            prompt = node.get("prompt")
            if prompt and prompt not in prompts:
                raise FlowError(f"Вузол «{name}»: немає промпта «{prompt}»")
        if ntype == "play":
            check_ref(name, node.get("next"))
        if ntype == "menu":
            if not node.get("branches"):
                raise FlowError(f"Меню «{name}» без жодної гілки")
            for digit, target in node["branches"].items():
                if not (isinstance(digit, str) and len(digit) == 1 and digit.isdigit()):
                    raise FlowError(f"Меню «{name}»: некоректна клавіша «{digit}»")
                check_ref(name, target)
            if "on_timeout" not in node:
                raise FlowError(f"Меню «{name}» без on_timeout")
            check_ref(name, node["on_timeout"])
            repeats = int(node.get("max_repeats", 0))
            if not 0 <= repeats <= MAX_REPEATS_LIMIT:
                raise FlowError(
                    f"Меню «{name}»: max_repeats {repeats} поза межами 0..{MAX_REPEATS_LIMIT}")
            timeout = int(node.get("timeout_sec", 0))
            if not TIMEOUT_RANGE[0] <= timeout <= TIMEOUT_RANGE[1]:
                raise FlowError(
                    f"Меню «{name}»: timeout_sec {timeout} поза межами "
                    f"{TIMEOUT_RANGE[0]}..{TIMEOUT_RANGE[1]}")

    for pname, prompt in prompts.items():
        if not (prompt.get("text") or "").strip():
            raise FlowError(f"Промпт «{pname}» з порожнім текстом")
    return flow
