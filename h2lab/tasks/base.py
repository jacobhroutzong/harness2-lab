"""Tasks, deliverables and hidden graders.

The open-world setting of Harness² is defined by what the improver does NOT get: no ground
truth, no rubric, no score. A `Task` therefore carries two very different kinds of data:

    visible   prompt, input files, deliverable paths     -> executor + improver evidence
    hidden    reference solution + `checks`             -> ONLY the post-hoc scoreboard

`grade()` is called by the pipeline after a task has been fully processed, to report how
much the adopted harness actually helped. Nothing it computes is ever written into an
improver's context (the context builder asserts this).

The `sim` field holds the hooks the offline simulator needs (see `h2lab/sim`). A real model
never sees or uses it.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

Outputs = dict[str, str]


@dataclass
class Check:
    id: str
    description: str
    fn: Callable[[Outputs, Any], bool]
    weight: float = 1.0


@dataclass
class Grade:
    score: float
    passed: list[str]
    failed: list[str]

    def to_dict(self) -> dict:
        return {"score": round(self.score, 1), "passed": self.passed, "failed": self.failed}


@dataclass
class SimSpec:
    """Simulator hooks: which latent requirements the task exercises and how to fail them."""

    reqs: list[str]
    solve: Callable[[dict[str, str], set[str]], Any]  # (files, domain mistakes) -> object
    fmt: str                                            # "json" | "csv" | "md"
    deliverable: str
    numeric: list[str] = field(default_factory=list)    # where an arithmetic slip lands
    rename: dict[str, str] = field(default_factory=dict)  # how a shape drift renames
    consistency: Callable[[Any], bool] | None = None    # a label-free internal check


@dataclass
class Task:
    id: str
    domain: str
    title: str
    prompt: str
    files: dict[str, str]
    deliverables: list[str]
    truth: Any
    checks: list[Check]
    sim: SimSpec | None = None
    blurb: str = ""

    def grade(self, outputs: Outputs) -> Grade:
        passed, failed, got, total = [], [], 0.0, 0.0
        for chk in self.checks:
            total += chk.weight
            try:
                ok = bool(chk.fn(outputs, self.truth))
            except Exception:
                ok = False
            (passed if ok else failed).append(chk.id)
            got += chk.weight if ok else 0.0
        return Grade(100.0 * got / total if total else 0.0, passed, failed)

    def public(self) -> dict:
        """Everything a client may see. Note what is absent: truth and checks."""
        return {"id": self.id, "domain": self.domain, "title": self.title,
                "prompt": self.prompt, "deliverables": self.deliverables, "blurb": self.blurb,
                "files": {k: (v if len(v) <= 6000 else v[:6000] + "\n...") for k, v in self.files.items()},
                "file_sizes": {k: len(v) for k, v in self.files.items()}}


# ---- helpers shared by task definitions ---------------------------------------------------

def rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text.strip() + "\n")))


def D(x: Any) -> Decimal:
    return Decimal(str(x))


def q(x: Decimal | float, places: str = "0.01") -> float:
    return float(D(x).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def load_json(outputs: Outputs, path: str) -> Any:
    return json.loads(outputs[path])


def load_csv(outputs: Outputs, path: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(outputs[path])))


def close(a: Any, b: Any, tol: float = 0.005) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def md_sections(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^##\s+(.+?)\s*$", text, re.M)]


def md_section(text: str, heading: str) -> str:
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


def json_checks(path: str, keys: list[str]) -> list[Check]:
    """The two format checks every JSON deliverable gets."""
    def parses(o, t):
        load_json(o, path)
        return True

    def exact(o, t):
        obj = load_json(o, path)
        return isinstance(obj, dict) and sorted(obj) == sorted(keys)

    return [Check("valid_json", f"{path} parses as JSON", parses),
            Check("exact_keys", f"{path} has exactly the keys {keys}", exact)]


def field_check(cid: str, desc: str, path: str, getter: Callable[[Any], Any],
                expect: Callable[[Any], Any], cmp: Callable[[Any, Any], bool] | None = None,
                weight: float = 1.0) -> Check:
    cmp = cmp or (lambda a, b: a == b)

    def fn(o, t):
        return cmp(getter(load_json(o, path)), expect(t))

    return Check(cid, desc, fn, weight)
