"""The improvement harness R: a per-domain library of adopted harnesses + lessons.

Harnessing-level recursion (paper Eq. 4):  R_{t+1} = Update(R_t, H*_t, D_t)

    <root>/<domain>/INDEX.md
    <root>/<domain>/harnesses/<task>/harness/<files>   the FULL adopted tree, not a summary
    <root>/<domain>/harnesses/<task>/EXPERIENCE.md     why it was picked + the lesson

An entry is written only when reflection produced a lesson the execution contrasts support;
"insufficient evidence -> no entry" is part of the method. Retrieval is by construction: a
domain's directory only ever holds same-domain entries.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .harness import Harness

FIELDS = ("cue", "lesson", "evidence", "confidence")


def has_experience(exp: dict | None) -> bool:
    return isinstance(exp, dict) and all(str(exp.get(k, "")).strip() for k in ("cue", "lesson", "evidence"))


@dataclass
class Entry:
    task_id: str
    adopted: str
    harness: Harness
    experience: dict
    why: str

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "adopted": self.adopted, "why": self.why,
                "experience": self.experience, "inventory": self.harness.inventory(),
                "files": self.harness.files, "genome": self.harness.genome()}


class Library:
    def __init__(self, root: Path | str, domain: str):
        self.root = Path(root)
        self.domain = domain
        self._entries: dict[str, Entry] = {}

    @property
    def dir(self) -> Path:
        return self.root / self.domain

    def context_dir(self) -> Path | None:
        """The directory handed to improver sessions, or None while the library is empty."""
        return self.dir if self._entries else None

    def entries(self) -> list[Entry]:
        return list(self._entries.values())

    def add(self, task_id: str, harness: Harness, *, adopted: str, why: str,
            experience: dict | None) -> Entry | None:
        if not has_experience(experience):
            return None
        entry = Entry(task_id, adopted, harness.copy(), dict(experience), why)
        self._entries[task_id] = entry
        d = self.dir / "harnesses" / task_id
        shutil.rmtree(d, ignore_errors=True)
        harness.to_dir(d / "harness")
        lines = [f"# {task_id}", "", f"- adopted: `{adopted}`", f"- components: {harness.inventory()}", ""]
        if why.strip():
            lines += ["## Why this was chosen", why.strip(), ""]
        if experience.get("edits"):
            lines += ["## What each edit did", str(experience["edits"]).strip(), ""]
        lines += ["## Transferable lesson"]
        lines += [f"- **{f}**: {str(experience[f]).strip()}" for f in FIELDS if experience.get(f)]
        (d / "EXPERIENCE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.rebuild_index()
        return entry

    def rebuild_index(self) -> Path:
        rows = [f"# Library — {self.domain}", "",
                "Each entry is an earlier task in THIS domain whose adopted harness is stored with "
                "its FULL component tree and the lesson its evidence supported. Browse this index, "
                "then open the entries relevant to the task in front of you.", ""]
        if not self._entries:
            rows.append("_(empty — this is the first task in this domain)_")
        for tid, e in self._entries.items():
            rows += [f"## `harnesses/{tid}/`",
                     f"- harness: `harnesses/{tid}/harness/` — {e.harness.inventory()}",
                     f"- why: `harnesses/{tid}/EXPERIENCE.md`",
                     f"- cue: {e.experience.get('cue', '')}",
                     f"- lesson: {e.experience.get('lesson', '')}", ""]
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "INDEX.md").write_text("\n".join(rows), encoding="utf-8")
        return self.dir / "INDEX.md"

    def to_dict(self) -> dict:
        return {"domain": self.domain, "entries": [e.to_dict() for e in self._entries.values()]}
