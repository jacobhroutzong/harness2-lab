"""Command line: run a task stream and print what happened.

    python -m h2lab.cli --tasks fin-01,fin-02 --k 2                     # simulator
    python -m h2lab.cli --backend anthropic --tasks fin-01 --k 1        # Claude (ANTHROPIC_API_KEY)
    python -m h2lab.cli --backend openai --base-url huggingface \\
        --model <model id> --tasks ops-02                               # any OpenAI-compatible endpoint
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import tasks
from .llm import make_llm
from .pipeline import Pipeline, RunConfig


def _printer(verbose: bool):
    def sink(ev: dict) -> None:
        kind = ev["type"]
        if kind == "task_start":
            print(f"\n== {ev['task']} ({ev['domain']}) — {ev['title']} | library entries: {ev['library_entries']}")
        elif kind == "stage_end":
            st = ev["stage"]
            flag = " FELL BACK" if st["fell_back"] else (" KEEP_BASE" if st["kept_base"] else "")
            print(f"   [{st['dim']:7s}] {st['label']:5s} {st['diff']['summary'][:90]}{flag}")
            if verbose and st["notes"]:
                for line in st["notes"].splitlines()[:6]:
                    print(f"             · {line[:120]}")
        elif kind == "rollout_end":
            r = ev["rollout"]
            print(f"   [rollout] {ev['label']:5s} delivered={r['delivered']} turns={r['turns']} errors={r['tool_errors']}")
        elif kind == "selection" and not ev["selection"]["trivial"]:
            print(f"   [select ] chose {ev['selection']['chosen']}: {ev['selection']['why'][:110]}")
        elif kind == "reflection":
            e = ev["experience"]
            print(f"   [reflect] {'lesson: ' + e['lesson'][:110] if e.get('lesson') else 'NO_ENTRY'}")
        elif kind == "task_end":
            r = ev["result"]
            print(f"   => base {r['base_score']:.1f}  ->  Harness² {r['final_score']:.1f}  ({r['gain']:+.1f})")
        elif kind == "task_error":
            print(f"   !! {ev['error']}")
        elif kind == "stream_end":
            s = ev["summary"]
            if s.get("tasks"):
                print(f"\nmean base {s['base_mean']}  ->  mean Harness² {s['final_mean']}  ({s['gain']:+})"
                      f"  | library entries {s['library_entries']} | cost ${s['cost_usd']}")
    return sink


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="h2lab", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="sim", choices=["sim", "anthropic", "openai"])
    ap.add_argument("--model", default="", help="executor model (frozen)")
    ap.add_argument("--editor-model", default="", help="improver model (defaults to --model)")
    ap.add_argument("--base-url", default=None, help="OpenAI-compatible base URL or preset name")
    ap.add_argument("--tasks", default="fin-01,fin-02,fin-03", help="comma-separated ids, or 'all'")
    ap.add_argument("--mode", default="parallel", choices=["parallel", "sequential", "adaptive"])
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-library", action="store_true")
    ap.add_argument("--no-scoping", action="store_true")
    ap.add_argument("--no-composition", action="store_true")
    ap.add_argument("--max-cost", type=float, default=0.0, help="stop once spend exceeds this (USD)")
    ap.add_argument("--out", default="", help="write the full results JSON here")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    chosen = tasks.all_tasks() if a.tasks == "all" else [tasks.get(t.strip()) for t in a.tasks.split(",")]
    key = (os.environ.get("ANTHROPIC_API_KEY") if a.backend == "anthropic"
           else os.environ.get("OPENAI_API_KEY") or os.environ.get("HF_TOKEN"))
    executor = make_llm(a.backend, a.model, api_key=key, base_url=a.base_url, seed=a.seed)
    editor = make_llm(a.backend, a.editor_model or a.model, api_key=key, base_url=a.base_url,
                      seed=a.seed, effort="high") if a.backend != "sim" else executor
    cfg = RunConfig(k=a.k, mode=a.mode, seed=a.seed, library=not a.no_library,
                    scoping=not a.no_scoping, composition=not a.no_composition,
                    max_cost_usd=a.max_cost, width=4 if a.backend == "sim" else 2)
    results = Pipeline(executor, editor, cfg, sink=_printer(a.verbose)).run_stream(chosen)
    if a.out:
        Path(a.out).write_text(json.dumps([r.to_dict() for r in results], indent=2, default=str))
        print(f"wrote {a.out}")
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
