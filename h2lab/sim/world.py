"""Turning latent outcomes into concrete artifacts (and reading artifacts back).

`render()` serialises a task's solution with the INV faults the simulated executor made:
an arithmetic slip, a renamed field, a malformed file. Domain mistakes are applied earlier,
by the task's own solver (`SimSpec.solve(files, mistakes)`), so a wrong deliverable is
wrong in exactly the way the practitioner error would make it.
"""

from __future__ import annotations

import csv
import io
import json
import random
import re
from typing import Any

from ..tasks.base import SimSpec


def render(spec: SimSpec, obj: Any, faults: set[str], rng: random.Random) -> str:
    if spec.fmt == "json":
        return _json(spec, obj, faults, rng)
    if spec.fmt == "csv":
        return _csv(spec, obj, faults, rng)
    return _md(spec, obj, faults, rng)


def _slip(value: float, rng: random.Random) -> float:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value + rng.choice([-3, -2, -1, 1, 2, 3])
    return round(value + rng.choice([-1, 1]) * rng.choice([0.1, 0.3, 1.0, 2.5]), 2)


def _json(spec: SimSpec, obj: dict, faults: set[str], rng: random.Random) -> str:
    obj = json.loads(json.dumps(obj))
    if "arith" in faults:
        for key in spec.numeric:
            if key in obj and isinstance(obj[key], (int, float)):
                obj[key] = _slip(obj[key], rng)
    if "shape" in faults:
        for old, new in list(spec.rename.items())[:1]:
            if old in obj:
                obj = {(new if k == old else k): v for k, v in obj.items()}
        obj["notes"] = "Prepared by the assistant from the input files."
    text = json.dumps(obj, indent=2)
    if "malformed" in faults:
        text = text.rstrip().rstrip("}").rstrip() + ",\n}"
    return text + "\n"


def _csv(spec: SimSpec, obj: tuple, faults: set[str], rng: random.Random) -> str:
    header, rows = list(obj[0]), [list(r) for r in obj[1]]
    if "arith" in faults and rows:
        col = header.index(spec.numeric[0]) if spec.numeric and spec.numeric[0] in header else -1
        row = rng.randrange(len(rows))
        try:
            rows[row][col] = f"{float(rows[row][col]) + rng.choice([-1, 1]) * 0.1:.2f}"
        except ValueError:
            pass
    if "shape" in faults:
        header = [spec.rename.get(h, h) for h in header]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    text = buf.getvalue()
    if "malformed" in faults and rows:
        lines = text.splitlines()
        lines[1] += ",see note"
        text = "\n".join(lines) + "\n"
    return text


def _md(spec: SimSpec, text: str, faults: set[str], rng: random.Random) -> str:
    if "arith" in faults and spec.numeric:
        def bump(m: re.Match) -> str:
            return m.group(0).replace(m.group(1), f"{float(m.group(1)) + 0.4:.1f}")
        text = re.sub(spec.numeric[0], bump, text, count=1)
    if "shape" in faults:
        for old, new in spec.rename.items():
            text = text.replace(old, new)
        text += "\n## Notes\nPrepared by the assistant.\n"
    if "malformed" in faults:
        text = text.rstrip() + "\n- TBD: confirm owner and due date\n"
    return text


def problems(fmt: str, text: str) -> str:
    """What a careful re-read would notice: a parse error or a leftover placeholder."""
    if fmt == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return f"invalid JSON ({exc.msg})"
        return ""
    if fmt == "csv":
        widths = {len(r) for r in csv.reader(io.StringIO(text)) if r}
        return "inconsistent column counts" if len(widths) > 1 else ""
    return "leftover TBD placeholder" if re.search(r"\bTBD\b", text) else ""


def numeric_columns(header: list[str]) -> list[str]:
    return [h for h in header if re.search(r"amount|total|value|score|due", h, re.I)]
