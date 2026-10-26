"""The execution harness H = (c1, ..., c8): a tree of editable component files.

Harness² starts from one identity:  Agent = Model + Harness.
The model M is frozen. Everything the method improves lives in the harness, and the
harness is nothing more exotic than a small directory of files:

    C1  systemprompt.md                       always on    persona + top-level procedure
    C2  guardrails.md                         always on    hard rules for every run
    C3  LongTermMEMORY.md                     always on    durable domain facts
    C4  tool_descriptions/tool_guidance.md    always on    when/how to use each tool
    C5  sub_agents/<name>/AGENT.md            on demand    delegated worker, own context
    C6  skills/<name>/SKILL.md (+scripts/)    on demand    triggered specialist procedure
    C7  scripts/<name>.py                     on demand    deterministic helper
    C8  plugin/<name>.json                    tool loop    middleware that ENFORCES

The layout follows the paper's OpenCode harness (Table 5). The one deliberate change is C8:
the paper mounts TypeScript plugins; here a plugin is a declarative JSON hook spec (see
`plugins.py`) so that a public demo never executes model-written middleware code.

A harness is represented as a plain `dict[str, str]` (relative path -> file text) wrapped in
`Harness`. Keeping it a dict makes diffs, hashing, copying and JSON transport trivial.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

Tree = dict[str, str]

NAME = r"[a-z0-9][a-z0-9_-]*"
ALWAYS_ON = (
    "systemprompt.md",
    "guardrails.md",
    "LongTermMEMORY.md",
    "tool_descriptions/tool_guidance.md",
)
TRIGGER_FILES = ("systemprompt.md", "guardrails.md")
ALWAYS_ON_MAX_CHARS = 9000


@dataclass(frozen=True)
class Component:
    key: str
    name: str
    path: str
    activation: str
    group: str
    role: str


COMPONENTS: tuple[Component, ...] = (
    Component("C1", "System prompt", "systemprompt.md", "always-on", "context",
              "Persona and top-level procedure"),
    Component("C2", "Guardrails", "guardrails.md", "always-on", "context",
              "Hard constraints regardless of task or tool"),
    Component("C3", "Memory", "LongTermMEMORY.md", "always-on", "context",
              "Durable domain facts across tasks"),
    Component("C4", "Tool guidance", "tool_descriptions/tool_guidance.md", "always-on",
              "tool-interface", "When and how to use each tool"),
    Component("C5", "Subagents", "sub_agents/<name>/AGENT.md", "on-demand", "orchestration",
              "Delegated worker in its own context; returns only its result"),
    Component("C6", "Skills", "skills/<name>/SKILL.md", "on-demand", "context",
              "Specialist procedure loaded only when invoked"),
    Component("C7", "Scripts", "scripts/<name>.py", "on-demand", "tool-interface",
              "Deterministic capability run instead of reasoned through"),
    Component("C8", "Plugins", "plugin/<name>.json", "tool-loop", "middleware",
              "Hooks around tool calls; enforces rather than instructs"),
)

_SURFACE: dict[str, re.Pattern[str]] = {
    "C1": re.compile(r"systemprompt\.md"),
    "C2": re.compile(r"guardrails\.md"),
    "C3": re.compile(r"LongTermMEMORY\.md"),
    "C4": re.compile(r"tool_descriptions/tool_guidance\.md"),
    "C5": re.compile(rf"sub_agents/{NAME}/AGENT\.md"),
    "C6": re.compile(rf"skills/{NAME}/(SKILL\.md|scripts/{NAME}\.py)"),
    "C7": re.compile(rf"scripts/{NAME}\.py"),
    "C8": re.compile(rf"plugin/{NAME}\.json"),
}

# A path to a script as it would appear in prose: `scripts/x.py` or `skills/s/scripts/x.py`.
_SCRIPT_REF = re.compile(rf"(?<![\w/])((?:skills/{NAME}/)?scripts/{NAME}\.py)")


def normalize(rel: str) -> str:
    rel = rel.replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.strip("/")


def component_of(rel: str) -> str | None:
    """Which component (C1..C8) a path belongs to, or None if it is off the surface."""
    rel = normalize(rel)
    for key, pattern in _SURFACE.items():
        if pattern.fullmatch(rel):
            return key
    return None


def is_allowed(rel: str) -> bool:
    return component_of(rel) is not None


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """`---\\nkey: value\\n---\\nbody` -> ({key: value}, body). Missing frontmatter -> ({}, text)."""
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*\n?(.*)", text, re.S)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    return meta, m.group(2)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    scripts: dict[str, str] = field(default_factory=dict)


@dataclass
class SubAgent:
    name: str
    description: str
    body: str


class Harness:
    """An execution harness: a validated-on-demand view over a `Tree` of component files."""

    def __init__(self, files: Tree | None = None):
        self.files: Tree = {normalize(k): v for k, v in (files or {}).items()}

    # ---- construction / persistence -------------------------------------------------------
    def copy(self) -> "Harness":
        return Harness(dict(self.files))

    @classmethod
    def from_dir(cls, root: Path | str, rejected: list[str] | None = None) -> "Harness":
        """Read every on-surface file under `root`. Off-surface files go to `rejected`."""
        root = Path(root)
        files: Tree = {}
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if not p.is_file():
                    continue
                rel = normalize(p.relative_to(root).as_posix())
                if is_allowed(rel):
                    files[rel] = p.read_text(encoding="utf-8", errors="replace")
                elif rejected is not None:
                    rejected.append(rel)
        return cls(files)

    def to_dir(self, root: Path | str) -> Path:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        for rel, text in self.files.items():
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        return root

    def fingerprint(self) -> str:
        h = hashlib.sha256(json.dumps(self.files, sort_keys=True).encode())
        return h.hexdigest()[:12]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Harness) and self.files == other.files

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Harness({self.inventory()})"

    # ---- component views ----------------------------------------------------------------
    def always_on_text(self) -> str:
        return "\n".join(self.files.get(rel, "") for rel in ALWAYS_ON)

    def skills(self) -> dict[str, Skill]:
        out: dict[str, Skill] = {}
        for rel, text in self.files.items():
            m = re.fullmatch(rf"skills/({NAME})/SKILL\.md", rel)
            if m:
                meta, body = parse_frontmatter(text)
                out[m.group(1)] = Skill(m.group(1), meta.get("description", ""), body)
        for rel, text in self.files.items():
            m = re.fullmatch(rf"skills/({NAME})/scripts/({NAME}\.py)", rel)
            if m and m.group(1) in out:
                out[m.group(1)].scripts[rel] = text
        return out

    def subagents(self) -> dict[str, SubAgent]:
        out: dict[str, SubAgent] = {}
        for rel, text in self.files.items():
            m = re.fullmatch(rf"sub_agents/({NAME})/AGENT\.md", rel)
            if m:
                meta, body = parse_frontmatter(text)
                out[m.group(1)] = SubAgent(m.group(1), meta.get("description", ""), body)
        return out

    def scripts(self) -> dict[str, str]:
        return {rel: t for rel, t in self.files.items() if rel.endswith(".py")}

    def plugins(self) -> dict[str, str]:
        return {
            rel.split("/")[1][:-5]: t for rel, t in self.files.items() if component_of(rel) == "C8"
        }

    def inventory(self) -> str:
        """One-line census, e.g. '2 skills, 1 scripts; 1,840 always-on chars'."""
        counts = {"skills": len(self.skills()), "sub_agents": len(self.subagents()),
                  "scripts": sum(1 for r in self.files if component_of(r) == "C7"),
                  "plugins": len(self.plugins())}
        bits = [f"{v} {k}" for k, v in counts.items() if v] or ["always-on only"]
        return f"{', '.join(bits)}; {len(self.always_on_text()):,} always-on chars"

    def genome(self) -> list[dict]:
        """Per-component size summary, used by the UI's 'harness genome' strip."""
        out = []
        for comp in COMPONENTS:
            paths = sorted(r for r in self.files if component_of(r) == comp.key)
            out.append({
                "key": comp.key, "name": comp.name, "activation": comp.activation,
                "group": comp.group, "files": paths,
                "chars": sum(len(self.files[p]) for p in paths),
            })
        return out


# ---- validation ---------------------------------------------------------------------------

def validate(h: Harness) -> list[str]:
    """Hard rules from the paper's Appendix H. Any error discards the proposal before rollout.

    1. every skill has frontmatter `name:` (matching its directory) and `description:`
    2. every skill is NAMED in always-on trigger text (an unnamed skill is never opened)
    3. no template braces in always-on text (the paper's runtime renders it through Jinja)
    4. every script a component names exists, and every script compiles
    5. subagents declare `mode: subagent` and `description:`; plugins parse and type-check
    """
    from .plugins import validate_plugin  # local import: plugins imports this module

    errors: list[str] = []
    for rel in h.files:
        if not is_allowed(rel):
            errors.append(f"`{rel}` is off the editable surface")
    if not h.files.get("systemprompt.md", "").strip():
        errors.append("`systemprompt.md` must exist and be non-empty")

    always_on = h.always_on_text()
    if "{{" in always_on or "{%" in always_on:
        errors.append("template braces (`{{` or `{%`) are not allowed in always-on text")
    if len(always_on) > ALWAYS_ON_MAX_CHARS:
        errors.append(
            f"always-on text is {len(always_on):,} chars; the cap is {ALWAYS_ON_MAX_CHARS:,}. "
            "Move procedures into on-demand skills"
        )

    trigger_text = "\n".join(h.files.get(rel, "") for rel in TRIGGER_FILES)
    for rel, text in h.files.items():
        m = re.fullmatch(rf"skills/({NAME})/SKILL\.md", rel)
        if not m:
            continue
        name = m.group(1)
        meta, _ = parse_frontmatter(text)
        if meta.get("name") != name:
            errors.append(f"skill `{name}`: frontmatter `name:` must equal `{name}`")
        if not meta.get("description"):
            errors.append(f"skill `{name}`: frontmatter needs a `description:`")
        if name not in trigger_text:
            errors.append(
                f"skill `{name}` has no trigger line: name it in systemprompt.md or guardrails.md, "
                f"e.g. 'When <situation>, load the `{name}` skill.'"
            )
    for rel in h.files:
        m = re.fullmatch(rf"skills/({NAME})/scripts/{NAME}\.py", rel)
        if m and f"skills/{m.group(1)}/SKILL.md" not in h.files:
            errors.append(f"`{rel}` belongs to a skill that does not exist")

    for rel, text in h.files.items():
        if re.fullmatch(rf"sub_agents/{NAME}/AGENT\.md", rel):
            meta, _ = parse_frontmatter(text)
            if meta.get("mode") != "subagent" or not meta.get("description"):
                errors.append(f"`{rel}`: frontmatter needs `mode: subagent` and `description:`")

    for rel, src in h.scripts().items():
        try:
            compile(src, rel, "exec")
        except SyntaxError as exc:
            errors.append(f"`{rel}` does not compile: line {exc.lineno}: {exc.msg}")

    for rel, text in h.files.items():
        refs = set(_SCRIPT_REF.findall(text)) if rel.endswith(".md") else set()
        skill = re.fullmatch(rf"skills/({NAME})/SKILL\.md", rel)
        for ref in refs:
            local = f"skills/{skill.group(1)}/{ref}" if skill and ref.startswith("scripts/") else None
            if ref not in h.files and (local is None or local not in h.files):
                errors.append(f"`{rel}` names `{ref}`, which is not in the harness")

    for name, text in h.plugins().items():
        errors.extend(f"plugin `{name}`: {e}" for e in validate_plugin(text))
    return errors


def autotrigger(h: Harness) -> tuple[Harness, list[str]]:
    """Repair the most common invalid proposal: a skill nobody names.

    Mirrors the official release, which appends a trigger line rather than discarding an
    otherwise-good proposal. Returns the repaired harness and the skills that were repaired.
    """
    trigger_text = "\n".join(h.files.get(rel, "") for rel in TRIGGER_FILES)
    fixed: list[str] = []
    out = h.copy()
    for name, skill in sorted(h.skills().items()):
        if name in trigger_text or not skill.description:
            continue
        line = f"- When {skill.description.rstrip('.').lower()}, load the `{name}` skill."
        g = out.files.get("guardrails.md", "").rstrip()
        out.files["guardrails.md"] = (g + "\n" if g else "") + line + "\n"
        fixed.append(name)
    return out, fixed


# ---- diffs --------------------------------------------------------------------------------

@dataclass
class HarnessDiff:
    added: list[str]
    removed: list[str]
    modified: list[str]
    patches: dict[str, str]

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    def components(self) -> list[str]:
        keys = {component_of(p) for p in self.added + self.removed + self.modified}
        return sorted(k for k in keys if k)

    def summary(self) -> str:
        if self.empty:
            return "no change (KEEP_BASE)"
        bits = [f"+{p}" for p in self.added] + [f"-{p}" for p in self.removed]
        bits += [f"~{p}" for p in self.modified]
        return ", ".join(bits)

    def to_dict(self) -> dict:
        return {"added": self.added, "removed": self.removed, "modified": self.modified,
                "patches": self.patches, "components": self.components(),
                "summary": self.summary()}


def diff(before: Harness, after: Harness) -> HarnessDiff:
    a, b = before.files, after.files
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    modified = sorted(p for p in set(a) & set(b) if a[p] != b[p])
    patches = {}
    for p in added + removed + modified:
        patches[p] = "".join(difflib.unified_diff(
            a.get(p, "").splitlines(keepends=True), b.get(p, "").splitlines(keepends=True),
            fromfile=f"a/{p}", tofile=f"b/{p}", n=2,
        ))
    return HarnessDiff(added, removed, modified, patches)


def line_overlap(a: str, b: str) -> float:
    """Fraction of a's non-trivial lines that also appear in b (used for provenance)."""
    def lines(t: str) -> set[str]:
        return {ln.strip() for ln in t.splitlines() if len(ln.strip()) > 12}
    la, lb = lines(a), lines(b)
    return len(la & lb) / len(la) if la else 0.0


def merge(base: Harness, edits: Iterable[Harness]) -> Harness:
    """Naive file-level union of several candidates' edits against `base` (later wins).

    Composition in Harness² is a model's judgment, not this function; this is only the
    mechanical fallback used by ablations ("w/o composition") and by the simulator.
    """
    out = base.copy()
    for cand in edits:
        d = diff(base, cand)
        for p in d.added + d.modified:
            out.files[p] = cand.files[p]
        for p in d.removed:
            out.files.pop(p, None)
    return out


# ---- the common initial harness H0 --------------------------------------------------------

BASE_SYSTEM_PROMPT = """\
You are an autonomous professional assistant working inside a sandboxed workspace.

The task's source material is in `input/`. Write every deliverable to `output/`, at exactly
the path the task names, in exactly the format it asks for.

Work with the tools: inspect the inputs, compute what is needed, write the deliverable, then
call `finish` with a short summary of what you delivered.
"""

BASE_GUARDRAILS = """\
- Never modify anything in `input/`.
- Do not invent data that is not supported by the inputs.
"""

BASE_TOOL_GUIDANCE = """\
- `list_files` shows a directory; `read_file` returns a window of a text file.
- `run_python` executes a short Python snippet in the workspace (cwd = workspace root).
- `write_file` creates or overwrites a file; `finish` ends the run.
"""


def base_harness() -> Harness:
    """H0: every task in a stream starts here (the library is what carries forward)."""
    return Harness({
        "systemprompt.md": BASE_SYSTEM_PROMPT,
        "guardrails.md": BASE_GUARDRAILS,
        "LongTermMEMORY.md": "",
        "tool_descriptions/tool_guidance.md": BASE_TOOL_GUIDANCE,
    })
