import json
import time
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from app.server import app
from h2lab.llm.anthropic_llm import AnthropicLLM
from h2lab.llm.base import ToolCall, ToolSpec
from h2lab.llm.openai_compat import OpenAICompatLLM

SPEC = [ToolSpec("write_file", "w", {"type": "object", "properties": {"path": {"type": "string"},
                                                                        "content": {"type": "string"}},
                                       "required": ["path", "content"]})]


def _msg(content, stop="tool_use"):
    return NS(content=content, stop_reason=stop, model="claude-opus-5-5",
              usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=50,
                       cache_creation_input_tokens=0))


def test_anthropic_parse_validates_inputs_and_honours_fallback_boundary():
    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm.model = "claude-opus-5-5"
    declined = NS(type="tool_use", id="t0", name="write_file", input={"path": "x", "content": "y"})
    marker = NS(type="fallback")
    good = NS(type="tool_use", id="t1", name="write_file", input={"path": "a"})
    text = NS(type="text", text="writing")
    r = llm._parse(_msg([declined, marker, text, good]), SPEC)
    assert [c.id for c in r.tool_calls] == ["t1"]                 # declined attempt never runs
    assert "missing required field `content`" in r.tool_calls[0].invalid
    assert all(b.type != "tool_use" or b.id == "t1" for b in r.raw)  # and is not echoed back
    assert r.usage.cost_usd > 0
    cut = llm._parse(_msg([NS(type="tool_use", id="t2", name="write_file",
                              input={"path": "a", "content": "b"})], stop="max_tokens"), SPEC)
    assert "truncated" in cut.tool_calls[0].invalid


def test_anthropic_echoes_its_own_turns_verbatim():
    llm = AnthropicLLM.__new__(AnthropicLLM)
    raw = [NS(type="thinking"), NS(type="text", text="hi")]
    msgs = llm._messages([{"role": "user", "content": "go"},
                          {"role": "assistant", "text": "hi", "tool_calls": [], "raw": raw,
                           "backend": "anthropic"},
                          {"role": "user", "content": [{"type": "tool_result", "id": "t", "content": "ok"}]}])
    assert msgs[1]["content"] is raw and msgs[2]["content"][0]["tool_use_id"] == "t"


def test_openai_message_conversion():
    llm = OpenAICompatLLM.__new__(OpenAICompatLLM)
    out = llm._messages("sys", [
        {"role": "user", "content": "go"},
        {"role": "assistant", "text": "", "tool_calls": [ToolCall("c1", "write_file", {"path": "a"})]},
        {"role": "user", "content": [{"type": "tool_result", "id": "c1", "content": "boom", "is_error": True},
                                     {"type": "text", "text": "[runtime] 3 turns remain."}]},
    ])
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[2]["tool_calls"][0]["function"]["arguments"] == json.dumps({"path": "a"})
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "ERROR: boom"}
    assert out[4]["role"] == "user"


def test_server_meta_hides_graders_and_a_sim_run_streams_to_completion():
    client = TestClient(app)
    meta = client.get("/api/meta").json()
    assert len(meta["tasks"]) == 9 and all("truth" not in t for t in meta["tasks"])
    res = client.post("/api/runs", json={"backend": "sim", "tasks": ["ops-02"], "k": 1,
                                         "api_key": "sk-should-not-echo"})
    assert res.status_code == 200
    body = res.json()
    assert "sk-should-not-echo" not in json.dumps(body)
    run_id = body["id"]
    for _ in range(200):
        if client.get(f"/api/runs/{run_id}").json()["status"] != "running":
            break
        time.sleep(0.1)
    info = client.get(f"/api/runs/{run_id}").json()
    assert info["status"] == "done" and info["summary"]["summary"]["tasks"] == 1
    with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
        lines = [ln for ln in stream.iter_lines() if ln.startswith("data: ")]
    events = [json.loads(ln[6:]) for ln in lines]
    assert events[0]["type"] == "stream_start" and events[-1]["type"] == "done"
    assert "sk-should-not-echo" not in "".join(lines)


def test_server_rejects_bad_requests():
    client = TestClient(app)
    assert client.post("/api/runs", json={"backend": "sim", "tasks": ["nope"]}).status_code == 400
    assert client.post("/api/runs", json={"backend": "anthropic", "tasks": ["fin-01"]}).status_code == 400
    assert client.post("/api/runs", json={"backend": "sim", "tasks": ["fin-01"], "mode": "x"}).status_code == 400
