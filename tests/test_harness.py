import json

from h2lab.harness import Harness, autotrigger, base_harness, component_of, diff, merge, validate
from h2lab.plugins import LoopState, PluginHost, validate_plugin

SKILL = "---\nname: rec\ndescription: reconciling ledgers\n---\nSum signed amounts.\n"


def test_base_harness_is_valid():
    assert validate(base_harness()) == []


def test_component_surface():
    assert component_of("systemprompt.md") == "C1"
    assert component_of("skills/rec/SKILL.md") == "C6"
    assert component_of("skills/rec/scripts/check.py") == "C6"
    assert component_of("scripts/exact_sum.py") == "C7"
    assert component_of("plugin/verify.json") == "C8"
    assert component_of("skills/Rec/SKILL.md") is None  # names are lowercase
    assert component_of("notes.txt") is None


def test_skill_needs_trigger_line_and_autotrigger_repairs_it():
    h = base_harness().copy()
    h.files["skills/rec/SKILL.md"] = SKILL
    assert any("no trigger line" in e for e in validate(h))
    fixed, repaired = autotrigger(h)
    assert repaired == ["rec"]
    assert "`rec`" in fixed.files["guardrails.md"]
    assert validate(fixed) == []


def test_template_braces_dangling_scripts_and_compile_errors_are_rejected():
    h = base_harness().copy()
    h.files["systemprompt.md"] += "Use {{ placeholder }} here.\n"
    h.files["guardrails.md"] += "- Run `scripts/missing.py` first.\n"
    h.files["scripts/broken.py"] = "def f(:\n"
    errors = "\n".join(validate(h))
    assert "template braces" in errors
    assert "scripts/missing.py" in errors
    assert "does not compile" in errors


def test_skill_local_script_reference_resolves():
    h = base_harness().copy()
    h.files["skills/rec/SKILL.md"] = SKILL + "Run `scripts/check.py`.\n"
    h.files["skills/rec/scripts/check.py"] = "print('ok')\n"
    h.files["guardrails.md"] += "- When reconciling, load the `rec` skill.\n"
    assert validate(h) == []


def test_subagent_frontmatter_and_plugin_schema():
    h = base_harness().copy()
    h.files["sub_agents/reader/AGENT.md"] = "---\ndescription: reads\n---\nbody"
    h.files["plugin/bad.json"] = json.dumps({"hooks": [{"action": "explode"}]})
    errors = "\n".join(validate(h))
    assert "mode: subagent" in errors
    assert "unknown action" in errors


def test_diff_and_merge():
    a = base_harness()
    b = a.copy()
    b.files["LongTermMEMORY.md"] = "- fact\n"
    c = a.copy()
    c.files["plugin/v.json"] = json.dumps({"hooks": [{"event": "after_tool", "tool": "write_file",
                                                      "action": "verify_json"}]})
    d = diff(a, b)
    assert d.modified == ["LongTermMEMORY.md"] and d.components() == ["C3"]
    m = merge(a, [b, c])
    assert m.files["LongTermMEMORY.md"] == "- fact\n" and "plugin/v.json" in m.files
    assert diff(a, a).empty


def test_harness_roundtrip_rejects_off_surface_files(tmp_path):
    h = base_harness()
    h.to_dir(tmp_path)
    (tmp_path / "random.txt").write_text("x")
    rejected = []
    again = Harness.from_dir(tmp_path, rejected)
    assert again == h and rejected == ["random.txt"]


# ---- plugins ------------------------------------------------------------------------------

def _host(*hooks):
    return PluginHost({"p": json.dumps({"hooks": list(hooks)})})


def test_verify_json_flags_a_malformed_write():
    host = _host({"event": "after_tool", "tool": "write_file", "match_path": r"\.json$",
                  "action": "verify_json"})
    state = LoopState(read_file=lambda p: '{"a": 1,}')
    out, err = host.after_tool("write_file", {"path": "output/x.json"}, "wrote", False, state)
    assert err and "VERIFY FAILED" in out
    state = LoopState(read_file=lambda p: '{"a": 1}')
    out, err = host.after_tool("write_file", {"path": "output/x.json"}, "wrote", False, state)
    assert not err and "verified" in out


def test_trim_hint_cap_and_require_reread():
    host = _host({"event": "after_tool", "tool": "read_file", "action": "trim_output", "max_chars": 10},
                 {"event": "after_tool", "tool": "*", "match_output": "Traceback", "action": "append_hint",
                  "hint": "check the column name"},
                 {"event": "before_tool", "tool": "read_file", "action": "cap_calls", "max_calls": 2},
                 {"event": "before_finish", "action": "require_reread"})
    st = LoopState()
    out, _ = host.after_tool("read_file", {"path": "input/a"}, "x" * 50, False, st)
    assert "trimmed" in out and len(out) < 200
    out, _ = host.after_tool("run_python", {}, "Traceback (most recent call last)", True, st)
    assert "check the column name" in out
    st.calls["read_file"] = 2
    assert host.before_tool("read_file", {"path": "a"}, st)
    st.writes["output/x.json"] = 3
    assert host.before_finish(st)
    st.reads["output/x.json"] = 4
    assert host.before_finish(st) is None


def test_plugin_validation_messages():
    assert validate_plugin("not json")
    assert validate_plugin(json.dumps({"hooks": [{"action": "append_hint"}]}))
    assert validate_plugin(json.dumps({"hooks": [{"action": "cap_calls", "max_calls": 3}]})) == []
