"""Harness² Lab web app: FastAPI + Server-Sent Events + a static single-page front end.

    GET  /                       the SPA
    GET  /api/meta               tasks (public view only), components, backends, prompts
    POST /api/runs               start a stream (simulator or a real model) -> {"id"}
    GET  /api/runs/{id}/events   SSE: every event since the start, then live ones
    POST /api/runs/{id}/cancel   stop after the current task
    GET  /api/runs/{id}          run status + summary
    GET  /api/budget-lab         the precomputed Budget Lab sweep
    POST /api/budget-lab         re-run a small sweep live on the simulator -> {"id"}

API keys arrive in the POST body, are used to build the model client for that run only,
and are never stored, logged or echoed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from h2lab import __version__, prompts, tasks
from h2lab.harness import COMPONENTS, base_harness
from h2lab.llm import LLMError, make_llm
from h2lab.llm.anthropic_llm import CLAUDE_MODELS
from h2lab.llm.openai_compat import PRESETS
from h2lab.pipeline import Pipeline, RunConfig

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
DATA = ROOT / "data"

MAX_ACTIVE = int(os.environ.get("H2_MAX_ACTIVE_RUNS", "4"))
MAX_REAL_TASKS = int(os.environ.get("H2_MAX_REAL_TASKS", "3"))
MAX_REAL_K = int(os.environ.get("H2_MAX_REAL_K", "3"))
ALLOW_SERVER_KEYS = os.environ.get("H2_ALLOW_SERVER_KEYS") == "1"
KEEP_RUNS = 40

app = FastAPI(title="Harness² Lab", version=__version__)


class RunRequest(BaseModel):
    backend: str = "sim"
    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = None
    executor_model: str = ""
    editor_model: str = ""
    effort: str = "medium"
    tasks: list[str] = Field(default_factory=lambda: ["fin-01", "fin-02", "fin-03"])
    mode: str = "parallel"
    k: int = 1
    seed: int = 0
    library: bool = True
    scoping: bool = True
    composition: bool = True
    adaptive_budget: float = Field(default=2.0, ge=1, le=3, allow_inf_nan=False)
    max_cost_usd: float = Field(default=5.0, ge=0.5, allow_inf_nan=False)


class LabRequest(BaseModel):
    seeds: int = 3
    policies: list[str] | None = None


@dataclass
class Run:
    id: str
    kind: str
    config: dict
    events: list[dict] = field(default_factory=list)
    status: str = "running"
    created: float = field(default_factory=time.time)
    cancel: threading.Event = field(default_factory=threading.Event)
    workdir: Path | None = None
    error: str = ""

    def push(self, event: dict) -> None:
        self.events.append(event)


RUNS: dict[str, Run] = {}
LOCK = threading.Lock()


def _active() -> int:
    return sum(r.status == "running" for r in RUNS.values())


def _gc() -> None:
    with LOCK:
        done = sorted((r for r in RUNS.values() if r.status != "running"), key=lambda r: r.created)
        for r in done[: max(0, len(RUNS) - KEEP_RUNS)]:
            if r.workdir:
                shutil.rmtree(r.workdir, ignore_errors=True)
            RUNS.pop(r.id, None)


def _register(kind: str, config: dict) -> Run:
    _gc()
    with LOCK:
        if _active() >= MAX_ACTIVE:
            raise HTTPException(429, "the lab is busy; try again in a minute")
        run = Run(id=uuid.uuid4().hex[:10], kind=kind, config=config)
        RUNS[run.id] = run
    return run


def _server_key(backend: str) -> str | None:
    if not ALLOW_SERVER_KEYS:
        return None
    return {"anthropic": os.environ.get("ANTHROPIC_API_KEY"),
            "openai": os.environ.get("OPENAI_API_KEY") or os.environ.get("HF_TOKEN")}.get(backend)


def _models(req: RunRequest):
    key = req.api_key or _server_key(req.backend)
    if req.backend == "sim":
        llm = make_llm("sim", seed=req.seed)
        return llm, llm
    if req.backend not in ("anthropic", "openai"):
        raise HTTPException(400, f"unknown backend {req.backend!r}")
    if not key and not (req.backend == "openai" and (req.base_url or "").startswith(("http://localhost", "ollama"))):
        raise HTTPException(400, "this backend needs an API key")
    try:
        ex = make_llm(req.backend, req.executor_model, api_key=key, base_url=req.base_url, effort=req.effort)
        ed = make_llm(req.backend, req.editor_model or req.executor_model, api_key=key,
                      base_url=req.base_url, effort="high" if req.effort != "low" else "medium")
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ex, ed


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "version": __version__, "active_runs": _active()}


@app.get("/api/meta")
def meta() -> dict:
    return {
        "version": __version__,
        "tasks": [t.public() for t in tasks.all_tasks()],
        "domains": list(tasks.DOMAINS),
        "components": [c.__dict__ for c in COMPONENTS],
        "base_harness": base_harness().files,
        "backends": {
            "sim": {"label": "Simulator (offline, no key)"},
            "anthropic": {"label": "Claude (Anthropic API)", "models": list(CLAUDE_MODELS),
                          "default_executor": "claude-opus-5-5", "default_editor": "claude-opus-5-5",
                          "server_key": bool(_server_key("anthropic"))},
            "openai": {"label": "OpenAI-compatible endpoint", "presets": PRESETS,
                       "server_key": bool(_server_key("openai"))},
        },
        "limits": {"max_real_tasks": MAX_REAL_TASKS, "max_real_k": MAX_REAL_K},
        "prompts": {
            "Propose · INV": prompts.propose(tasks.get("fin-01"), "INV"),
            "Propose · DOM": prompts.propose(tasks.get("fin-01"), "DOM"),
            "Compose (parallel, k=3)": prompts.aggregate(tasks.get("fin-01"), k=3, index=1),
            "Compose (sequential checkpoint)": prompts.compose_round(tasks.get("fin-01"), t=2, k=3),
            "Select": prompts.select(tasks.get("fin-01"), ["cand-A", "cand-B", "cand-C"]),
            "Reflect": prompts.experience(tasks.get("fin-01"), "agg2", "<selector's reasoning>"),
        },
    }


@app.post("/api/runs")
def start_run(req: RunRequest) -> dict:
    if req.mode not in ("parallel", "sequential", "adaptive"):
        raise HTTPException(400, "mode must be parallel, sequential or adaptive")
    try:
        chosen = [tasks.get(t) for t in dict.fromkeys(req.tasks)]
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not chosen:
        raise HTTPException(400, "pick at least one task")
    k = max(1, min(req.k, 5))
    if req.backend != "sim":
        if len(chosen) > MAX_REAL_TASKS:
            raise HTTPException(400, f"real-model runs are limited to {MAX_REAL_TASKS} tasks here")
        k = min(k, MAX_REAL_K)
    executor, editor = _models(req)
    public = req.model_dump(exclude={"api_key"})
    public.update(k=k, tasks=[t.id for t in chosen], executor=executor.describe(), editor=editor.describe())
    run = _register("stream", public)
    run.workdir = Path(tempfile.mkdtemp(prefix=f"h2run-{run.id}-"))
    cfg = RunConfig(k=k, mode=req.mode, seed=req.seed, library=req.library, scoping=req.scoping,
                    composition=req.composition, width=4 if req.backend == "sim" else 3,
                    adaptive_budget=round(req.adaptive_budget * len(chosen)), adaptive_kmax=3,
                    max_cost_usd=0 if req.backend == "sim" else max(0.5, req.max_cost_usd))

    def work() -> None:
        try:
            Pipeline(executor, editor, cfg, root=run.workdir, sink=run.push,
                     cancelled=run.cancel.is_set).run_stream(chosen)
            run.status = "cancelled" if run.cancel.is_set() else "done"
        except Exception as exc:  # surfaced to the client; never crash the server
            run.error = f"{type(exc).__name__}: {exc}"
            run.push({"type": "run_error", "error": run.error, "ts": time.time()})
            run.status = "error"

    threading.Thread(target=work, daemon=True, name=f"run-{run.id}").start()
    return {"id": run.id, "config": public}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> dict:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    run.cancel.set()
    return {"ok": True}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    end = next((e for e in reversed(run.events) if e["type"] in ("stream_end", "lab_end")), None)
    return {"id": run.id, "kind": run.kind, "status": run.status, "config": run.config,
            "events": len(run.events), "error": run.error, "summary": end}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")

    async def stream():
        try:
            i = max(0, min(int(request.headers.get("last-event-id", "0")), len(run.events)))
        except ValueError:
            i = 0
        idle = 0.0
        while True:
            if await request.is_disconnected():
                return
            batch = run.events[i:]
            if batch:
                idle = 0.0
                for ev in batch:
                    i += 1
                    yield f"id: {i}\ndata: {json.dumps(ev, default=str)}\n\n"
            elif run.status != "running":
                yield f"data: {json.dumps({'type': 'done', 'status': run.status, 'error': run.error})}\n\n"
                return
            else:
                await asyncio.sleep(0.15)
                idle += 0.15
                if idle >= 15:
                    idle = 0.0
                    yield ": keepalive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/budget-lab")
def budget_lab() -> JSONResponse:
    path = DATA / "budget_lab.json"
    if not path.is_file():
        raise HTTPException(404, "no precomputed Budget Lab data; run scripts/budget_lab.py")
    return JSONResponse(json.loads(path.read_text()))


@app.post("/api/budget-lab")
def budget_lab_run(req: LabRequest) -> dict:
    from h2lab.budget_lab import POLICIES, run_lab
    from h2lab.sim import SimLLM

    seeds = list(range(max(1, min(req.seeds, 6))))
    pols = [p for p in POLICIES if not req.policies or p["name"] in req.policies]
    run = _register("lab", {"seeds": seeds, "policies": [p["name"] for p in pols]})

    def work() -> None:
        try:
            run.push({"type": "lab_start", "jobs": len(seeds) * len(pols), "ts": time.time()})
            lab = run_lab(lambda s: SimLLM(seed=s), seeds, policies=pols, workers=2,
                          progress=lambda p: run.push({"type": "lab_progress", **p, "ts": time.time()}))
            run.push({"type": "lab_end", "lab": lab, "ts": time.time()})
            run.status = "done"
        except Exception as exc:
            run.error = f"{type(exc).__name__}: {exc}"
            run.push({"type": "run_error", "error": run.error, "ts": time.time()})
            run.status = "error"

    threading.Thread(target=work, daemon=True, name=f"lab-{run.id}").start()
    return {"id": run.id}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))


if __name__ == "__main__":  # pragma: no cover
    main()

