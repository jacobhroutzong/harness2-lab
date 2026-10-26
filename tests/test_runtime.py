import json

import pytest

from h2lab.agent import Agent, Tool, schema
from h2lab.harness import Harness
from h2lab.llm.base import LLM, LLMResponse, ToolCall, Usage
from h2lab.runtime import execute, render_system
from h2lab.sandbox import Workspace, WorkspaceError
from h2lab.tasks import get


class Scripted(LLM):
    """Replays a fixed list of tool calls, one per turn."""

    backend = "scripted"

    def __init__(self, turns):
        super().__init__("scripted")
        self.turns = list(turns)

    def chat(self, system, messages, tools, *, max_tokens=16000):
        calls = self.turns.pop(0) if self.turns else []
        return LLMResponse("", [ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(calls)],
                           "tool_use" if calls else "end", Usage(calls=1))


def test_workspace_confines_paths_and_scrubs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    ws = Workspace.create({"input/a.csv": "x\n1\n"}, parent=tmp_path)
    with pytest.raises(WorkspaceError):
        ws.read("../../etc/passwd")
    with pytest.raises(WorkspaceError):
        ws.write("input/a.csv", "tampered")
    res = ws.run_python("import os; print(sorted(os.environ))")
    assert res.exit_code == 0 and "ANTHROPIC_API_KEY" not in res.stdout
    assert ws.run_python("import time; time.sleep(5)", timeout=1).timed_out
    ws.cleanup()


def test_agent_loop_finish_errors_and_invalid_input():
    tools = [Tool("echo", "echo", schema({"text": {"type": "string"}}), lambda a: a["text"])]
    llm = Scripted([[("echo", {"text": "hi"})], [("nope", {})], [("finish", {"summary": "done"})]])
    traj = Agent(llm, "sys", tools + [Tool("finish", "", schema({"summary": {"type": "string"}}),
                                           lambda a: "")]).run("go")
    assert traj.finished and traj.final == "done"
    assert traj.tool_calls == 3 and traj.tool_errors == 1


def test_agent_stops_at_max_turns():
    llm = Scripted([[("echo", {"text": "x"})]] * 10)
    tools = [Tool("echo", "echo", schema({"text": {"type": "string"}}), lambda a: a["text"])]
    traj = Agent(llm, "sys", tools, max_turns=3).run("go")
    assert traj.stop_reason == "max_turns" and traj.turns == 3


def test_execute_mounts_scripts_skills_and_plugins():
    task = get("fin-01")
    h = Harness({
        "systemprompt.md": "Work.",
        "guardrails.md": "- When reconciling invoices, load the `rec` skill.",
        "skills/rec/SKILL.md": "---\nname: rec\ndescription: reconciling invoices\n---\nKeep signs.",
        "scripts/count.py": "import sys; print(len(open(sys.argv[1]).read().splitlines()))",
        "plugin/v.json": json.dumps({"hooks": [{"event": "after_tool", "tool": "write_file",
                                                "match_path": r"\.json$", "action": "verify_json"}]}),
    })
    sysmsg = render_system(h)
    assert "`rec`: reconciling invoices" in sysmsg and "scripts/count.py" in sysmsg
    llm = Scripted([
        [("skill", {"name": "rec"}), ("run_script", {"path": "scripts/count.py", "args": ["input/invoices.csv"]})],
        [("write_file", {"path": "output/reconciliation.json", "content": "{bad"})],
        [("write_file", {"path": "output/reconciliation.json", "content": "{}"})],
        [("finish", {"summary": "ok"})],
    ])
    r = execute(llm, h, task, "t1")
    results = [s for s in r.trajectory.steps if s["kind"] == "result"]
    assert "Keep signs." in results[0]["output"]
    assert "stdout:\n8" in results[1]["output"]
    assert results[2]["is_error"] and "VERIFY FAILED" in results[2]["output"]
    assert r.delivered and r.outputs["output/reconciliation.json"] == "{}"
