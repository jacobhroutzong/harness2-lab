"""Regenerate data/budget_lab.json (simulator; no API key needed).

    python scripts/budget_lab.py --seeds 8
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from h2lab.budget_lab import run_lab  # noqa: E402
from h2lab.sim import SimLLM  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "data" / "budget_lab.json"))
    args = ap.parse_args()
    t0 = time.time()
    lab = run_lab(lambda s: SimLLM(seed=s), list(range(args.seeds)),
                  progress=lambda p: print(f"  {p['policy']:32s} seed={p['seed']}  score={p['score']:5.1f}"
                                           f"  sessions/task={p['sessions']:5.1f}", flush=True))
    lab["generated_seconds"] = round(time.time() - t0, 1)
    Path(args.out).write_text(json.dumps(lab, indent=2))
    for p in sorted(lab["points"], key=lambda p: p["sessions"]):
        star = "*" if p["name"] in lab["frontier"] else " "
        print(f"{star} {p['name']:32s} score={p['score']:5.1f}±{p['score_se']:.1f}  "
              f"sessions/task={p['sessions']:5.1f}  mean_k={p['mean_k']}")
    print(f"wrote {args.out} in {lab['generated_seconds']}s")


if __name__ == "__main__":
    main()
