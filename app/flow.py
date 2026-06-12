"""IVR scenario: form -> JSON compiler + validator (Plan.md §5, §15).

The UI is a recursive parameterized form: a tree of menu levels, where every
option can open one more level (action "menu"). This module compiles that tree
into the flat node-graph JSON stored as a snapshot in campaign.ivr_flow. The
runtime interpreter (app/ivr.py) only ever sees that JSON — menus of any depth
are just `menu` nodes whose branches point at other nodes, which the runtime
supported from day one.

The pre-tree form shape (operator/repeat/optout checkboxes, §15 first
version) is still accepted: _legacy_menu() converts it into one root level,
so ansible/call.yml and old clients keep working.

Node types are unchanged: play | menu | bridge | hangup.
Pure functions — fully covered by pytest without FreeSWITCH (§16 level 1).
"""

MAX_REPEATS_LIMIT = 5     # server-side cap (§5)
TIMEOUT_RANGE = (1, 30)   # seconds the menu waits for a digit
NODE_TYPES = ("play", "menu", "bridge", "hangup")
MAX_DEPTH = 4             # menu levels; deeper trees lose callers (IVR UX)
MAX_PROMPTS = 40          # total prompts per campaign — bounds prerender time

DEFAULT_TIMEOUT_SEC = 5
DEFAULT_MAX_REPEATS = 2

DEFAULT_CONNECT_TEXT = "Зачекайте, з'єднуємо з оператором"
DEFAULT_OPTOUT_TEXT = "Вас видалено зі списку"

# Цифри словами — TTS читає їх надійніше, ніж "1"/"0" (ґотча етапу 3).
DIGIT_WORDS = {"0": "нуль", "1": "один", "2": "два", "3": "три",
               "4": "чотири", "5": "п'ять", "6": "шість", "7": "сім",
               "8": "вісім", "9": "дев'ять"}

ACTIONS = ("operator", "replay", "menu", "play", "back", "home", "optout", "hangup")

# Шматки автогенерованого анонсу рівня. Без анонсу меню — мертва тиша:
# play_and_get_digits грає лише silence_stream, і абонент не знає, що можна
# щось натиснути. Опції menu/play описуються підписом (label) — для них
# шаблон LABELLED_TEMPLATE. UI дзеркалить ці тексти у плейсхолдері.
ANNOUNCE_TEMPLATES = {
    "operator": "Щоб з'єднатися з оператором, натисніть {digit}.",
    "replay": "Щоб прослухати ще раз, натисніть {digit}.",
    "optout": "Щоб відписатися від дзвінків, натисніть {digit}.",
    "back": "Щоб повернутися назад, натисніть {digit}.",
    "home": "Щоб повернутися в головне меню, натисніть {digit}.",
    "hangup": "Щоб завершити дзвінок, натисніть {digit}.",
}
LABELLED_TEMPLATE = "{label}: натисніть {digit}."


class FlowError(ValueError):
    """Invalid IVR form input or scenario JSON; .args[0] is user-readable."""


def announce_for_options(options):
    """Default level announcement for its options (the UI mirrors this).

    Options needing a label (menu/play) without one are skipped here: the
    compiler raises a FlowError for them before this text is ever used.
    """
    parts = []
    for opt in options or []:
        digit = DIGIT_WORDS.get(str(opt.get("digit", "")).strip())
        action = opt.get("action")
        if digit is None:
            continue
        if action in ANNOUNCE_TEMPLATES:
            parts.append(ANNOUNCE_TEMPLATES[action].format(digit=digit))
        elif action in ("menu", "play"):
            label = (opt.get("label") or "").strip()
            if label:
                parts.append(LABELLED_TEMPLATE.format(label=label, digit=digit))
    return " ".join(parts)


def _legacy_menu(ivr):
    """§15 first-version checkboxes -> one root level of the recursive form."""
    operator = ivr.get("operator") or {}
    repeat = ivr.get("repeat") or {}
    optout = ivr.get("optout") or {}
    options = []
    if operator.get("enabled"):
        options.append({"digit": "1", "action": "operator",
                        "connect_text": operator.get("connect_text", "")})
    if repeat.get("enabled"):
        options.append({"digit": "2", "action": "replay"})
    if optout.get("enabled"):
        options.append({"digit": "0", "action": "optout",
                        "confirm_text": optout.get("confirm_text", "")})
    menu = {"announce_text": ivr.get("menu_text", ""), "options": options}
    if repeat.get("enabled") and repeat.get("max") is not None:
        menu["max_repeats"] = repeat["max"]
    return menu


def _where(path):
    return "Головне меню" if not path else "Підменю " + "→".join(path)


def _int_field(value, default, what, where):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise FlowError(f"{where}: {what} має бути числом") from None


def compile_form(message_text, voice, ivr=None, voice_params=None):
    """Compile the §15 `ivr` form object into the §5 flow JSON.

    With no form (or no options) the flow degenerates to play-message ->
    hangup — the v1 behaviour. `voice_params` (вже clamped upstream:
    speed/steps/silence) лягають у КОЖЕН промпт — снапшот flow
    самодостатній, resume/retry синтезують так само.

    Node/prompt names are path-based (`menu_3_1`, `info_3_1`), so nested
    levels never collide; the root content prompt keeps the name «main» —
    voicemail drop (ivr.CallContext.main_prompt) depends on it.
    """
    ivr = ivr or {}
    on_timeout = ivr.get("on_timeout")
    if on_timeout not in (None, "", "hangup"):
        raise FlowError(f"Невідома дія за таймаутом: {on_timeout}")
    menu = ivr.get("menu") if "menu" in ivr else _legacy_menu(ivr)
    menu = menu or {}

    def prompt(text):
        p = {"text": text, "voice": voice}
        if voice_params:
            p.update(voice_params)
        return p

    prompts = {"main": prompt(message_text)}
    nodes = {"bye": {"type": "hangup"}}

    if not (menu.get("options") or []):
        nodes["msg"] = {"type": "play", "prompt": "main", "next": "bye"}
        return {"start": "msg", "nodes": nodes, "prompts": prompts}

    g_timeout = _int_field(ivr.get("timeout_sec"), DEFAULT_TIMEOUT_SEC,
                           "таймаут", "IVR")
    g_repeats = _int_field(ivr.get("max_repeats"), DEFAULT_MAX_REPEATS,
                           "кількість повторів", "IVR")

    def build_level(level, path, parent_menu_node):
        where = _where(path)
        if len(path) >= MAX_DEPTH:
            raise FlowError(f"{where}: меню глибше за {MAX_DEPTH} рівні(в)")
        options = level.get("options") or []
        if not options:
            raise FlowError(f"{where}: жодної опції")

        suffix = "_" + "_".join(path) if path else ""
        menu_node = f"menu{suffix}"

        timeout_sec = _int_field(level.get("timeout_sec"), g_timeout,
                                 "таймаут", where)
        if not TIMEOUT_RANGE[0] <= timeout_sec <= TIMEOUT_RANGE[1]:
            raise FlowError(f"{where}: таймаут {timeout_sec} поза межами "
                            f"{TIMEOUT_RANGE[0]}..{TIMEOUT_RANGE[1]}")
        max_repeats = _int_field(level.get("max_repeats"), g_repeats,
                                 "кількість повторів", where)
        if not 0 <= max_repeats <= MAX_REPEATS_LIMIT:
            raise FlowError(f"{where}: повторів {max_repeats} поза межами "
                            f"0..{MAX_REPEATS_LIMIT}")

        # контент рівня: на корені — повідомлення кампанії (промпт «main»),
        # глибше — необов'язковий level.text (рівень може бути чистим меню)
        if not path:
            text, content_prompt = message_text, "main"
        else:
            text, content_prompt = (level.get("text") or "").strip(), f"text{suffix}"
        if text:
            if path:
                prompts[content_prompt] = prompt(text)
            nodes[f"msg{suffix}"] = {"type": "play", "prompt": content_prompt,
                                     "next": menu_node}
            entry = f"msg{suffix}"
        else:
            entry = menu_node

        announce = (level.get("announce_text") or "").strip()
        autogen, branches, seen = [], {}, set()
        for opt in options:
            digit = str(opt.get("digit", "")).strip()
            if not (len(digit) == 1 and digit.isdigit()):
                raise FlowError(f"{where}: некоректна клавіша «{digit}»")
            if digit in seen:
                raise FlowError(f"{where}: клавіша {digit} використана двічі")
            seen.add(digit)
            action = opt.get("action")
            opath = "_".join(path + [digit])
            word = DIGIT_WORDS[digit]
            label = (opt.get("label") or "").strip()

            def need_label():
                # автоанонс описує menu/play лише через підпис; без нього
                # абонент не дізнається, що означає клавіша
                if not announce and not label:
                    raise FlowError(
                        f"{where}: опція {digit} потребує підпису для "
                        f"автоанонсу (або заповніть анонс рівня)")

            if action == "operator":
                ctext = (opt.get("connect_text") or "").strip() or DEFAULT_CONNECT_TEXT
                prompts[f"connect_{opath}"] = prompt(ctext)
                nodes[f"to_op_{opath}"] = {"type": "bridge",
                                           "prompt": f"connect_{opath}"}
                branches[digit] = f"to_op_{opath}"
            elif action == "optout":
                ctext = (opt.get("confirm_text") or "").strip() or DEFAULT_OPTOUT_TEXT
                prompts[f"optout_ok_{opath}"] = prompt(ctext)
                nodes[f"optout_{opath}"] = {"type": "play",
                                            "prompt": f"optout_ok_{opath}",
                                            "mark": "optout", "next": "bye"}
                branches[digit] = f"optout_{opath}"
            elif action == "replay":
                branches[digit] = entry
            elif action == "back":
                if parent_menu_node is None:
                    raise FlowError(f"{where}: «назад» неможливий на верхньому рівні")
                branches[digit] = parent_menu_node
            elif action == "home":
                if parent_menu_node is None:
                    raise FlowError(
                        f"{where}: «головне меню» неможливе на верхньому рівні")
                branches[digit] = "menu"  # кореневий menu-вузол (суфікс порожній)
            elif action == "hangup":
                branches[digit] = "bye"
            elif action == "play":
                need_label()
                ptext = (opt.get("text") or "").strip()
                if not ptext:
                    raise FlowError(f"{where}: опція {digit} (фраза) без тексту")
                then = opt.get("then") or "stay"
                if then == "stay":
                    nxt = menu_node
                elif then == "back":
                    if parent_menu_node is None:
                        raise FlowError(f"{where}: опція {digit} — «потім назад» "
                                        f"неможливе на верхньому рівні")
                    nxt = parent_menu_node
                elif then == "hangup":
                    nxt = "bye"
                else:
                    raise FlowError(f"{where}: опція {digit} — невідоме «потім» «{then}»")
                prompts[f"info_{opath}"] = prompt(ptext)
                nodes[f"info_{opath}"] = {"type": "play", "prompt": f"info_{opath}",
                                          "next": nxt}
                branches[digit] = f"info_{opath}"
            elif action == "menu":
                need_label()
                branches[digit] = build_level(opt.get("menu") or {},
                                              path + [digit], menu_node)
            else:
                raise FlowError(f"{where}: невідома дія «{action}»")

        prompts[menu_node] = prompt(announce or announce_for_options(options))
        nodes[menu_node] = {
            "type": "menu",
            "prompt": menu_node,
            "timeout_sec": timeout_sec,
            "max_repeats": max_repeats,
            "branches": branches,
            "on_timeout": "bye",
        }
        return entry

    start = build_level(menu, [], None)
    if len(prompts) > MAX_PROMPTS:
        raise FlowError(f"Забагато фраз для синтезу: {len(prompts)} > {MAX_PROMPTS}")
    flow = {"start": start, "nodes": nodes, "prompts": prompts}
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
