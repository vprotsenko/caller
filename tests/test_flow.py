"""IVR form -> flow compiler + validator (verification level 1)."""

import pytest

from app import flow as flow_mod


# --- legacy checkbox form (first version of the API form, still accepted) ---------

FULL_FORM = {
    "operator": {"enabled": True, "connect_text": "Зачекайте"},
    "repeat": {"enabled": True, "max": 2},
    "optout": {"enabled": True, "confirm_text": "Видалено"},
    "timeout_sec": 5,
    "on_timeout": "hangup",
}


def test_compile_no_form_degenerates_to_v1():
    flow = flow_mod.compile_form("Привіт", "F3", None)
    assert flow["start"] == "msg"
    assert flow["nodes"]["msg"] == {"type": "play", "prompt": "main", "next": "bye"}
    assert flow["nodes"]["bye"] == {"type": "hangup"}
    assert flow["prompts"]["main"] == {"text": "Привіт", "voice": "F3"}
    flow_mod.validate(flow)


def test_compile_legacy_form_matches_spec_shape():
    flow = flow_mod.compile_form("Привіт", "F3", FULL_FORM)
    menu = flow["nodes"]["menu"]
    assert menu["branches"] == {"1": "to_op_1", "2": "msg", "0": "optout_0"}
    assert menu["on_timeout"] == "bye"
    assert menu["timeout_sec"] == 5
    assert flow["nodes"]["to_op_1"] == {"type": "bridge", "prompt": "connect_1"}
    assert flow["nodes"]["optout_0"]["mark"] == "optout"
    assert flow["prompts"]["connect_1"]["text"] == "Зачекайте"
    assert flow["prompts"]["optout_ok_0"]["text"] == "Видалено"


def test_compile_legacy_repeat_only():
    flow = flow_mod.compile_form("Привіт", "F3", {"repeat": {"enabled": True, "max": 3}})
    assert flow["nodes"]["menu"]["branches"] == {"2": "msg"}
    assert flow["nodes"]["menu"]["max_repeats"] == 3
    assert "to_op_1" not in flow["nodes"]
    assert "optout_0" not in flow["nodes"]


def test_compile_menu_gets_auto_announcement():
    """A menu without an announcement = dead silence after the message; the
    compiler is obliged to add a "menu" prompt whose auto-text covers only the
    enabled options."""
    flow = flow_mod.compile_form("Привіт", "F3", FULL_FORM)
    assert flow["nodes"]["menu"]["prompt"] == "menu"
    assert flow["prompts"]["menu"]["text"] == (
        "Щоб з'єднатися з оператором, натисніть один. "
        "Щоб прослухати ще раз, натисніть два. "
        "Щоб відписатися від дзвінків, натисніть нуль.")

    repeat_only = flow_mod.compile_form("Привіт", "F3", {"repeat": {"enabled": True}})
    assert repeat_only["prompts"]["menu"]["text"] == \
        "Щоб прослухати ще раз, натисніть два."


def test_compile_menu_text_override():
    form = dict(FULL_FORM, menu_text="Натисніть один або два.")
    flow = flow_mod.compile_form("Привіт", "F3", form)
    assert flow["prompts"]["menu"]["text"] == "Натисніть один або два."


def test_compile_voice_params_reach_every_prompt():
    """The flow snapshot is self-contained: resume/retry must synthesize with
    the same parameters, so they go into every prompt."""
    vp = {"speed": 1.3, "steps": 16, "silence": 0.5, "lang": "en"}
    flow = flow_mod.compile_form("Привіт", "F3", FULL_FORM, voice_params=vp)
    for prompt in flow["prompts"].values():
        assert prompt["speed"] == 1.3
        assert prompt["steps"] == 16
        assert prompt["silence"] == 0.5
        assert prompt["lang"] == "en"


def test_compile_without_voice_params_keeps_plain_prompts():
    flow = flow_mod.compile_form("Привіт", "F3", FULL_FORM)
    assert "speed" not in flow["prompts"]["main"]


def test_compile_default_prompt_texts():
    flow = flow_mod.compile_form("Привіт", "F3", {
        "operator": {"enabled": True, "connect_text": "  "},
        "optout": {"enabled": True},
    })
    assert flow["prompts"]["connect_1"]["text"] == flow_mod.DEFAULT_CONNECT_TEXT
    assert flow["prompts"]["optout_ok_0"]["text"] == flow_mod.DEFAULT_OPTOUT_TEXT


def test_compile_rejects_unknown_on_timeout():
    with pytest.raises(flow_mod.FlowError):
        flow_mod.compile_form("Привіт", "F3", {**FULL_FORM, "on_timeout": "explode"})


def test_compile_rejects_too_many_repeats():
    with pytest.raises(flow_mod.FlowError):
        flow_mod.compile_form("Привіт", "F3",
                              {"repeat": {"enabled": True, "max": 99}})


def test_compile_rejects_bad_timeout():
    with pytest.raises(flow_mod.FlowError):
        flow_mod.compile_form("Привіт", "F3", {**FULL_FORM, "timeout_sec": 0})


# --- recursive tree form (current version of the API form) --------------------------

def tree_form():
    """A two-level tree: operator/replay/optout + a "Графік роботи" (working
    hours) submenu."""
    return {
        "timeout_sec": 5,
        "max_repeats": 2,
        "menu": {
            "announce_text": "",
            "options": [
                {"digit": "1", "action": "operator", "connect_text": "Зачекайте"},
                {"digit": "2", "action": "replay"},
                {"digit": "3", "action": "menu", "label": "Графік роботи",
                 "menu": {
                     "text": "Працюємо з девʼятої до вісімнадцятої.",
                     "options": [
                         {"digit": "1", "action": "play", "label": "Субота",
                          "text": "У суботу з десятої до пʼятнадцятої.",
                          "then": "stay"},
                         {"digit": "9", "action": "back"},
                     ],
                 }},
                {"digit": "0", "action": "optout", "confirm_text": "Видалено"},
            ],
        },
    }


def test_compile_tree_two_levels():
    flow = flow_mod.compile_form("Привіт", "F3", tree_form())
    root = flow["nodes"]["menu"]
    assert root["branches"]["3"] == "msg_3"          # submenu with text → play entry
    assert flow["nodes"]["msg_3"] == {"type": "play", "prompt": "text_3",
                                      "next": "menu_3"}
    sub = flow["nodes"]["menu_3"]
    assert sub["branches"]["1"] == "info_3_1"
    assert sub["branches"]["9"] == "menu"            # "back" → the parent's menu
    assert flow["nodes"]["info_3_1"]["next"] == "menu_3"   # then=stay
    assert flow["prompts"]["text_3"]["text"].startswith("Працюємо")
    flow_mod.validate(flow)


def test_compile_tree_root_keeps_main_prompt():
    # voicemail-drop plays the prompt "main" (ivr.CallContext.main_prompt) —
    # the root is obliged to keep this name
    flow = flow_mod.compile_form("Привіт", "F3", tree_form())
    assert flow["start"] == "msg"
    assert flow["nodes"]["msg"]["prompt"] == "main"
    assert flow["prompts"]["main"]["text"] == "Привіт"


def test_compile_tree_submenu_without_text_enters_menu_directly():
    form = tree_form()
    form["menu"]["options"][2]["menu"]["text"] = ""
    flow = flow_mod.compile_form("Привіт", "F3", form)
    assert flow["nodes"]["menu"]["branches"]["3"] == "menu_3"
    assert "msg_3" not in flow["nodes"]


def test_compile_tree_auto_announce_uses_labels_and_digit_words():
    flow = flow_mod.compile_form("Привіт", "F3", tree_form())
    assert "Графік роботи: натисніть три." in flow["prompts"]["menu"]["text"]
    assert flow["prompts"]["menu_3"]["text"] == (
        "Субота: натисніть один. Щоб повернутися назад, натисніть дев'ять.")


def test_compile_tree_announce_override_per_level():
    form = tree_form()
    form["menu"]["options"][2]["menu"]["announce_text"] = "Один — субота, дев'ять — назад."
    flow = flow_mod.compile_form("Привіт", "F3", form)
    assert flow["prompts"]["menu_3"]["text"] == "Один — субота, дев'ять — назад."


def test_compile_tree_play_then_back_and_hangup():
    form = tree_form()
    sub = form["menu"]["options"][2]["menu"]
    sub["options"][0]["then"] = "back"
    sub["options"].append({"digit": "2", "action": "play", "label": "Адреса",
                           "text": "Вулиця Зелена, один.", "then": "hangup"})
    flow = flow_mod.compile_form("Привіт", "F3", form)
    assert flow["nodes"]["info_3_1"]["next"] == "menu"   # back → the parent's menu
    assert flow["nodes"]["info_3_2"]["next"] == "bye"


def test_compile_tree_per_level_timeout_and_repeats_override():
    form = tree_form()
    form["menu"]["options"][2]["menu"]["timeout_sec"] = 10
    form["menu"]["options"][2]["menu"]["max_repeats"] = 0
    flow = flow_mod.compile_form("Привіт", "F3", form)
    assert flow["nodes"]["menu"]["timeout_sec"] == 5      # the global default
    assert flow["nodes"]["menu_3"]["timeout_sec"] == 10
    assert flow["nodes"]["menu_3"]["max_repeats"] == 0


def test_compile_tree_legacy_and_tree_forms_give_same_graph():
    legacy = flow_mod.compile_form("Привіт", "F3", FULL_FORM)
    tree = flow_mod.compile_form("Привіт", "F3", {
        "timeout_sec": 5,
        "menu": {"options": [
            {"digit": "1", "action": "operator", "connect_text": "Зачекайте"},
            {"digit": "2", "action": "replay"},
            {"digit": "0", "action": "optout", "confirm_text": "Видалено"},
        ]},
    })
    assert legacy["nodes"] == tree["nodes"]
    assert legacy["prompts"] == tree["prompts"]


def test_compile_tree_rejects_too_deep():
    level = {"options": [{"digit": "1", "action": "hangup"}]}
    for _ in range(flow_mod.MAX_DEPTH):  # 1 level deeper than allowed
        level = {"options": [{"digit": "1", "action": "menu", "label": "Глибше",
                              "menu": level}]}
    with pytest.raises(flow_mod.FlowError, match="глибше"):
        flow_mod.compile_form("Привіт", "F3", {"menu": level})


def test_compile_tree_rejects_duplicate_digit():
    form = {"menu": {"options": [
        {"digit": "1", "action": "operator"},
        {"digit": "1", "action": "replay"},
    ]}}
    with pytest.raises(flow_mod.FlowError, match="двічі"):
        flow_mod.compile_form("Привіт", "F3", form)


def test_compile_tree_rejects_back_on_root():
    form = {"menu": {"options": [{"digit": "9", "action": "back"}]}}
    with pytest.raises(flow_mod.FlowError, match="назад"):
        flow_mod.compile_form("Привіт", "F3", form)


def test_compile_tree_home_jumps_to_root_menu():
    # "main menu" from the third level — to where back cannot reach
    form = tree_form()
    sub = form["menu"]["options"][2]["menu"]
    sub["options"].append({"digit": "0", "action": "home"})
    flow = flow_mod.compile_form("Привіт", "F3", form)
    assert flow["nodes"]["menu_3"]["branches"]["0"] == "menu"
    assert "Щоб повернутися в головне меню, натисніть нуль." \
        in flow["prompts"]["menu_3"]["text"]


def test_compile_tree_rejects_home_on_root():
    form = {"menu": {"options": [{"digit": "8", "action": "home"}]}}
    with pytest.raises(flow_mod.FlowError, match="головне меню"):
        flow_mod.compile_form("Привіт", "F3", form)


def test_compile_tree_rejects_empty_submenu():
    form = {"menu": {"options": [
        {"digit": "3", "action": "menu", "label": "Порожнє", "menu": {}},
    ]}}
    with pytest.raises(flow_mod.FlowError, match="жодної опції"):
        flow_mod.compile_form("Привіт", "F3", form)


def test_compile_tree_rejects_play_without_text():
    form = {"menu": {"options": [
        {"digit": "4", "action": "play", "label": "Адреса", "text": "  "},
    ]}}
    with pytest.raises(flow_mod.FlowError, match="без тексту"):
        flow_mod.compile_form("Привіт", "F3", form)


def test_compile_tree_rejects_missing_label_for_auto_announce():
    form = {"menu": {"options": [
        {"digit": "4", "action": "play", "text": "Вулиця Зелена."},
    ]}}
    with pytest.raises(flow_mod.FlowError, match="підпису"):
        flow_mod.compile_form("Привіт", "F3", form)
    # with the level's own announcement a label is not needed
    form["menu"]["announce_text"] = "Натисніть чотири, щоб почути адресу."
    flow_mod.compile_form("Привіт", "F3", form)


def test_compile_tree_rejects_unknown_action_and_then():
    with pytest.raises(flow_mod.FlowError, match="невідома дія"):
        flow_mod.compile_form("Привіт", "F3", {"menu": {"options": [
            {"digit": "1", "action": "teleport"}]}})
    with pytest.raises(flow_mod.FlowError, match="потім"):
        flow_mod.compile_form("Привіт", "F3", {"menu": {"options": [
            {"digit": "1", "action": "play", "label": "X", "text": "x",
             "then": "explode"}]}})


def test_compile_tree_rejects_too_many_prompts():
    # 4 levels of ~10 operator options each → 42 prompts > MAX_PROMPTS
    def level(depth):
        opts = [{"digit": str(d), "action": "operator"} for d in range(10)]
        if depth < flow_mod.MAX_DEPTH:
            opts = opts[:9] + [{"digit": "9", "action": "menu", "label": "Глибше",
                                "menu": level(depth + 1)}]
        return {"options": opts}
    with pytest.raises(flow_mod.FlowError, match="Забагато фраз"):
        flow_mod.compile_form("Привіт", "F3", {"menu": level(1)})


# --- validator on raw flows -------------------------------------------------------

def valid_flow():
    return flow_mod.compile_form("Привіт", "F3", FULL_FORM)


def test_validate_broken_branch_target():
    flow = valid_flow()
    flow["nodes"]["menu"]["branches"]["1"] = "ghost"
    with pytest.raises(flow_mod.FlowError):
        flow_mod.validate(flow)


def test_validate_missing_on_timeout():
    flow = valid_flow()
    del flow["nodes"]["menu"]["on_timeout"]
    with pytest.raises(flow_mod.FlowError):
        flow_mod.validate(flow)


def test_validate_missing_start():
    flow = valid_flow()
    flow["start"] = "nope"
    with pytest.raises(flow_mod.FlowError):
        flow_mod.validate(flow)


def test_validate_unknown_node_type():
    flow = valid_flow()
    flow["nodes"]["weird"] = {"type": "teleport"}
    with pytest.raises(flow_mod.FlowError):
        flow_mod.validate(flow)


def test_validate_missing_prompt():
    flow = valid_flow()
    flow["nodes"]["msg"]["prompt"] = "ghost"
    with pytest.raises(flow_mod.FlowError):
        flow_mod.validate(flow)


def test_validate_empty_prompt_text():
    flow = valid_flow()
    flow["prompts"]["main"]["text"] = "   "
    with pytest.raises(flow_mod.FlowError):
        flow_mod.validate(flow)


def test_validate_bad_menu_key():
    flow = valid_flow()
    flow["nodes"]["menu"]["branches"]["**"] = "bye"
    with pytest.raises(flow_mod.FlowError):
        flow_mod.validate(flow)


def test_validate_accepts_old_snapshots():
    """Campaign snapshots created before the tree editor (the flow JSON of
    early versions, node names to_op/optout without a path) must remain valid —
    resume/retry of old campaigns works without a migration."""
    flow_mod.validate({
        "start": "msg",
        "nodes": {
            "msg": {"type": "play", "prompt": "main", "next": "menu"},
            "menu": {"type": "menu", "prompt": "menu", "timeout_sec": 5,
                     "max_repeats": 2,
                     "branches": {"1": "to_op", "2": "msg", "0": "optout"},
                     "on_timeout": "bye"},
            "to_op": {"type": "bridge", "prompt": "connecting"},
            "optout": {"type": "play", "prompt": "optout_ok",
                       "mark": "optout", "next": "bye"},
            "bye": {"type": "hangup"},
        },
        "prompts": {
            "main": {"text": "Привіт", "voice": "F3"},
            "menu": {"text": "Натисніть один.", "voice": "F3"},
            "connecting": {"text": "Зачекайте", "voice": "F3"},
            "optout_ok": {"text": "Видалено", "voice": "F3"},
        },
    })


def test_cycles_in_graph_are_legal():
    # "2" -> msg -> menu is a cycle by design; the runtime bounds steps
    flow_mod.validate(valid_flow())
