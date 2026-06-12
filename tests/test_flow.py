"""IVR form -> flow compiler + validator (Plan.md §5, §15; §16 level 1)."""

import pytest

from app import flow as flow_mod


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


def test_compile_full_form_matches_spec_shape():
    flow = flow_mod.compile_form("Привіт", "F3", FULL_FORM)
    menu = flow["nodes"]["menu"]
    assert menu["branches"] == {"1": "to_op", "2": "msg", "0": "optout"}
    assert menu["on_timeout"] == "bye"
    assert menu["timeout_sec"] == 5
    assert flow["nodes"]["to_op"] == {"type": "bridge", "prompt": "connecting"}
    assert flow["nodes"]["optout"]["mark"] == "optout"
    assert flow["prompts"]["connecting"]["text"] == "Зачекайте"
    assert flow["prompts"]["optout_ok"]["text"] == "Видалено"


def test_compile_repeat_only():
    flow = flow_mod.compile_form("Привіт", "F3", {"repeat": {"enabled": True, "max": 3}})
    assert flow["nodes"]["menu"]["branches"] == {"2": "msg"}
    assert flow["nodes"]["menu"]["max_repeats"] == 3
    assert "to_op" not in flow["nodes"]
    assert "optout" not in flow["nodes"]


def test_compile_menu_gets_auto_announcement():
    """Меню без анонсу = мертва тиша після повідомлення; компілятор зобов'язаний
    додати промпт «menu» з автотекстом лише для увімкнених опцій."""
    flow = flow_mod.compile_form("Привіт", "F3", FULL_FORM)
    assert flow["nodes"]["menu"]["prompt"] == "menu"
    assert flow["prompts"]["menu"]["text"] == flow_mod.menu_announcement(
        True, True, True)

    repeat_only = flow_mod.compile_form("Привіт", "F3", {"repeat": {"enabled": True}})
    assert repeat_only["prompts"]["menu"]["text"] == flow_mod.MENU_REPEAT_TEXT


def test_compile_menu_text_override():
    form = dict(FULL_FORM, menu_text="Натисніть один або два.")
    flow = flow_mod.compile_form("Привіт", "F3", form)
    assert flow["prompts"]["menu"]["text"] == "Натисніть один або два."


def test_compile_voice_params_reach_every_prompt():
    """Снапшот flow самодостатній: resume/retry мають синтезувати з тими
    самими параметрами, тож вони лягають у кожен промпт."""
    vp = {"speed": 1.3, "steps": 16, "silence": 0.5}
    flow = flow_mod.compile_form("Привіт", "F3", FULL_FORM, voice_params=vp)
    for prompt in flow["prompts"].values():
        assert prompt["speed"] == 1.3
        assert prompt["steps"] == 16
        assert prompt["silence"] == 0.5


def test_compile_without_voice_params_keeps_plain_prompts():
    flow = flow_mod.compile_form("Привіт", "F3", FULL_FORM)
    assert "speed" not in flow["prompts"]["main"]


def test_compile_default_prompt_texts():
    flow = flow_mod.compile_form("Привіт", "F3", {
        "operator": {"enabled": True, "connect_text": "  "},
        "optout": {"enabled": True},
    })
    assert flow["prompts"]["connecting"]["text"] == flow_mod.DEFAULT_CONNECT_TEXT
    assert flow["prompts"]["optout_ok"]["text"] == flow_mod.DEFAULT_OPTOUT_TEXT


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


def test_cycles_in_graph_are_legal():
    # "2" -> msg -> menu is a cycle by design (§5); the runtime bounds steps
    flow_mod.validate(valid_flow())
