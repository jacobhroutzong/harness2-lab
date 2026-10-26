"""Scripted-but-stochastic policies for every agent role, written as generators.

Each policy is a generator that yields an `Act` (a short message + tool calls) and receives
the tool results of that act. `yield from` composes them, so a policy reads top to bottom
like the agent's plan:

    res = yield Act("Let me look at the inputs.", [call("list_files", path="input")])

Everything these policies do goes through the real tools, so the offline demo exercises the
real agent loop, sandbox, plugins, validator, evidence builder, selection and library.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from typing import Generator

from ..harness import line_overlap, parse_frontmatter
from ..tasks.base import Task
from . import catalog as C
from .world import numeric_columns, problems, render

Results = list[dict]


@dataclass
class Act:
    text: str = ""
    calls: list[tuple[str, dict]] = field(default_factory=list)


Policy = Generator[Act, Results, None]


def call(tool: str, /, **args) -> tuple[str, dict]:
    return tool, args


def outs(res: Results) -> list[str]:
    return [r.get("content", "") for r in res if r.get("type") == "tool_result"]


def errors(res: Results) -> list[bool]:
    return [bool(r.get("is_error")) for r in res if r.get("type") == "tool_result"]


def runtime_notes(res: Results) -> str:
    return "\n".join(r.get("text", "") for r in res if r.get("type") == "text")


_MORE = re.compile(r"\n\.\.\. \[[\d,]+ more chars; call read_file with offset=(\d+)\]$")


def read_all(paths: list[str], say: str = "") -> Generator[Act, Results, dict[str, str]]:
    """Read several files in one turn, then page through any that were cut off."""
    paths = list(dict.fromkeys(paths))
    if not paths:
        return {}
    res = yield Act(say, [call("read_file", path=p, limit=12000) for p in paths])
    got: dict[str, str] = {}
    pending: dict[str, int] = {}
    for p, out, err in zip(paths, outs(res), errors(res)):
        if err:
            continue
        m = _MORE.search(out)
        got[p] = out[: m.start()] if m else out
        if m:
            pending[p] = int(m.group(1))
    while pending:
        batch = list(pending)
        res = yield Act("", [call("read_file", path=p, offset=pending[p], limit=12000) for p in batch])
        nxt: dict[str, int] = {}
        for p, out in zip(batch, outs(res)):
            m = _MORE.search(out)
            got[p] += out[: m.start()] if m else out
            if m:
                nxt[p] = int(m.group(1))
        pending = nxt
    return got


def list_files(path: str) -> Generator[Act, Results, list[str]]:
    res = yield Act("", [call("list_files", path=path)])
    out = outs(res)[0] if outs(res) else ""
    return [ln.split("  (")[0].strip() for ln in out.splitlines() if "  (" in ln]


# ===================================================================== executor

class Context:
    """What the simulated executor has 'in mind': always-on text, loaded skills, hints."""

    def __init__(self, system: str):
        self.always_on = system.split("\n# Skills")[0]
        self.system = system
        self.skills = ""
        self.hints = ""

    def dilution(self) -> float:
        extra = max(0, len(self.always_on) - C.ALWAYS_ON_COMFORT)
        return 1.0 / (1.0 + extra / 4000)

    def p(self, req: C.Req, base: float, *, strong: float) -> float:
        """P(handled) given where (if anywhere) the harness addresses this requirement."""
        if req.cue_re.search(self.skills) or req.cue_re.search(self.hints):
            return strong
        if req.cue_re.search(self.always_on):
            return base + (strong - 0.05 - base) * self.dilution()
        return base


def _skills_listed(system: str) -> list[tuple[str, str]]:
    block = system.split("# Skills", 1)[1].split("\n# ", 1)[0] if "# Skills" in system else ""
    return re.findall(r"^- `([a-z0-9_-]+)`: (.+)$", block, re.M)


_STOP = {"the", "and", "or", "a", "an", "of", "to", "in", "from", "with", "for", "on", "by", "is",
         "each", "every", "this", "that", "when", "data"}


def _words(text: str) -> set[str]:
    """Crude stems (5-char prefixes) so 'reconciling' meets 'reconcile' and 'logs' meets 'log'."""
    return {w[:5] for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _STOP}


def _summary_code(path: str) -> str:
    if path.endswith(".csv"):
        return (f"import csv\nrows = list(csv.DictReader(open({path!r})))\nprint(len(rows), 'rows')\n"
                "print(list(rows[0]))\nprint(rows[:2])\n")
    return (f"lines = open({path!r}).read().splitlines()\nprint(len(lines), 'lines')\n"
            "print('\\n'.join(lines[:3]))\n")


def executor(task: Task, system: str, rng: random.Random, seed: int = 0) -> Policy:
    spec = task.sim
    ctx = Context(system)
    reqs = C.reqs_for(spec.reqs)
    hurried = False

    def unaided(r: C.Req) -> float:
        return C.competence(seed, r, task.domain)

    yield Act("Let me look at the inputs.", [call("list_files", path="input")])

    for name, desc in _skills_listed(system)[:4]:
        if _words(desc) & _words(task.prompt + " " + task.domain):
            res = yield Act(f"Loading the `{name}` skill.", [call("skill", name=name)])
            ctx.skills += "\n" + "\n".join(outs(res))

    # attention cost of long always-on text: the agent dithers
    for _ in range(int(max(0, len(ctx.always_on) - C.ALWAYS_ON_COMFORT) / 1200)):
        yield Act("Re-reading the instructions before going on.", [call("list_files", path=".")])

    small = [p for p in sorted(task.files) if len(task.files[p]) <= 4000]
    big = [p for p in sorted(task.files) if len(task.files[p]) > 4000]
    if small:
        yield Act("Reading the inputs.", [call("read_file", path=p) for p in small])
    budget = C.REQS["budget"]
    for p in big:
        if rng.random() < ctx.p(budget, 0.0, strong=C.P_INV_CUED):
            yield Act(budget.pass_say, [call("run_python", code=_summary_code(p))])
            continue
        lost = rng.random() > unaided(budget)
        offset = 0
        while True:
            res = yield Act("", [call("read_file", path=p, offset=offset)])
            out = (outs(res) or [""])[0]
            note = runtime_notes(res)
            if "[plugin:" in out:
                ctx.hints += out
                if "run_python" in out:
                    yield Act("The plugin says this input is large; switching to run_python.",
                              [call("run_python", code=_summary_code(p))])
                    break
            if "turns remain" in note and rng.random() < 0.5:
                hurried = True
                break
            m = re.search(r"offset=(\d+)\]$", out)
            if m:
                offset = int(m.group(1))
                continue
            if lost:
                lost = False
                offset = 0
                yield Act(budget.fail_say, [call("list_files", path="input")])
                continue
            break
        if hurried:
            break

    # scripts the harness points at
    script_ok = False
    if not hurried:
        named = set(re.findall(r"((?:skills/[a-z0-9_-]+/)?scripts/[a-z0-9_-]+\.py)",
                               ctx.always_on + ctx.skills + system))
        csvs = [p for p in sorted(task.files) if p.endswith(".csv")]
        for script in sorted(named):
            if not csvs:
                break
            header = task.files[csvs[0]].splitlines()[0].split(",")
            cols = numeric_columns(header)
            if not cols:
                continue
            res = yield Act(f"Running `{script}` for exact totals.",
                            [call("run_script", path=script, args=[csvs[0], cols[0]])])
            script_ok = script_ok or "exit code: 0" in (outs(res) or [""])[0]

    # decide what this run gets right (the latent outcome) and say so
    mistakes: set[str] = set()
    faults: set[str] = set()
    said: list[str] = []
    for r in reqs:
        if r.id in ("verify", "budget"):
            continue
        if r.kind == "dom":
            p = max(unaided(r), ctx.p(r, unaided(r), strong=C.P_DOM_SKILL))
        elif r.id == "arith" and script_ok:
            p = C.P_SCRIPT
        else:
            p = max(unaided(r), ctx.p(r, unaided(r), strong=C.P_INV_CUED))
        ok = rng.random() < p and not (hurried and r.kind == "dom" and rng.random() < 0.6)
        if not ok:
            (mistakes if r.kind == "dom" else faults).add(r.id)
            if rng.random() < C.P_SAY:
                said.append(r.fail_say)
        elif rng.random() < C.P_SAY:
            said.append(r.pass_say)
    if hurried:
        faults.add("arith")
        said.append("Running out of turns; writing the deliverable from what I have.")
    if rng.random() < C.P_MALFORMED:
        faults.add("malformed")
    if "arith" not in faults and not hurried:
        yield Act("Computing the figures.", [call("run_python", code="print('ok')\n")])

    obj = spec.solve(task.files, mistakes)
    text = render(spec, obj, faults, random.Random(rng.random()))
    res = yield Act(" ".join(said) or "Writing the deliverable.",
                    [call("write_file", path=spec.deliverable, content=text)])
    if "VERIFY FAILED" in (outs(res) or [""])[0] and rng.random() < C.P_FIX_AFTER_SIGNAL:
        faults.discard("malformed")
        text = render(spec, obj, faults, random.Random(rng.random()))
        yield Act("The plugin caught a problem in the file; fixing it.",
                  [call("write_file", path=spec.deliverable, content=text)])

    def reread() -> Generator[Act, Results, None]:
        nonlocal text
        res = yield Act(C.REQS["verify"].pass_say, [call("read_file", path=spec.deliverable, limit=12000)])
        if problems(spec.fmt, (outs(res) or [""])[0]) and rng.random() < C.P_FIX_AFTER_SIGNAL:
            faults.discard("malformed")
            text = render(spec, obj, faults, random.Random(rng.random()))
            yield Act("Found a problem on re-reading; fixing it.",
                      [call("write_file", path=spec.deliverable, content=text)])

    reread_habit = unaided(C.REQS["verify"])
    if not hurried and rng.random() < max(reread_habit, ctx.p(C.REQS["verify"], reread_habit,
                                                              strong=C.P_REREAD_CUED)):
        yield from reread()
    res = yield Act("Done.", [call("finish", summary=f"Wrote {spec.deliverable}.")])
    if any(errors(res)):  # a plugin refused `finish` until the deliverable is reopened
        yield from reread()
        yield Act("Done.", [call("finish", summary=f"Wrote {spec.deliverable} and checked it.")])


# ===================================================================== evidence reading

@dataclass
class Evidence:
    label: str
    trajectory: str
    output: str | None
    delivered: bool
    notes: str = ""
    files: dict[str, str] = field(default_factory=dict)

    def symptom(self, req: C.Req, fmt: str) -> bool:
        t = self.trajectory
        if req.id == "verify":
            return bool(self.output is not None and problems(fmt, self.output)) or "VERIFY FAILED" in t
        if req.id == "budget":
            return (not self.delivered and "stop: max_turns" in t) or req.fail_say in t
        return bool(req.fail_say) and req.fail_say in t

    def shows(self, req: C.Req) -> bool:
        return bool(req.pass_say) and req.pass_say in self.trajectory


def gather(labels: list[str], base_dir: str, task: Task) -> Generator[Act, Results, dict[str, Evidence]]:
    out_name = task.sim.deliverable.split("/")[-1]
    paths = []
    for lbl in labels:
        paths += [f"{base_dir}/{lbl}/trajectory.md", f"{base_dir}/{lbl}/meta.json"]
    got = yield from read_all(paths, "Reading the trajectories.")
    outp = yield from read_all([f"{base_dir}/{lbl}/outputs/{out_name}" for lbl in labels])
    ev = {}
    for lbl in labels:
        meta = got.get(f"{base_dir}/{lbl}/meta.json", "{}")
        try:
            delivered = bool(json.loads(meta).get("delivered"))
        except json.JSONDecodeError:
            delivered = False
        ev[lbl] = Evidence(lbl, got.get(f"{base_dir}/{lbl}/trajectory.md", ""),
                           outp.get(f"{base_dir}/{lbl}/outputs/{out_name}"), delivered)
    return ev


def _labels(layout: str, kind: str) -> list[str]:
    return list(dict.fromkeys(re.findall(rf"`context/{kind}/([^/`]+)/`", layout)))


def read_harness(root: str) -> Generator[Act, Results, dict[str, str]]:
    files = yield from list_files(root)
    got = yield from read_all(files)
    prefix = root.rstrip("/") + "/"
    return {p[len(prefix):]: t for p, t in got.items() if p.startswith(prefix)}


# ===================================================================== proposer

@dataclass
class Issue:
    req: C.Req
    kind: str          # divergence | repeated | risk
    fails: list[str]
    total: int

    def moment(self) -> str:
        quote = f": '{self.req.fail_say}'" if self.req.fail_say else ""
        return f"{self.kind}, failed in {len(self.fails)}/{self.total} runs ({self.fails[0]}{quote})"


def diagnose(task: Task, ev: dict[str, Evidence], kind: str | None) -> list[Issue]:
    issues = []
    for r in C.reqs_for(task.sim.reqs, kind):
        fails = [lbl for lbl, e in ev.items() if e.symptom(r, task.sim.fmt)]
        if not fails:
            continue
        passes = [lbl for lbl, e in ev.items() if e.shows(r) and lbl not in fails]
        issues.append(Issue(r, "divergence" if passes else "repeated", fails, len(ev)))
    issues.sort(key=lambda i: (i.kind != "divergence", -len(i.fails)))
    return issues


def _append_line(files: dict[str, str], path: str, line: str) -> None:
    cur = files.get(path, "").rstrip()
    if line.strip() not in cur:
        files[path] = (cur + "\n" if cur else "") + line.rstrip() + "\n"


def apply_fix(files: dict[str, str], req: C.Req, form: str, domain: str,
              library_skill: str | None = None, vague: bool = False) -> str:
    """Edit `files` in place to address `req` in the given form; returns the path touched."""
    rule = C.vague_rule(req) if vague else req.rule
    if form == "skill":
        name = C.DOMAIN_SKILLS[domain][0]
        path = f"skills/{name}/SKILL.md"
        rules = C.skill_rules(parse_frontmatter(files.get(path, library_skill or ""))[1])
        if rule not in rules:
            rules.append(rule)
        files[path] = C.skill_text(domain, rules)
        _append_line(files, "guardrails.md", C.trigger_line(domain))
        return path
    if form == "plugin":
        path, body = (("plugin/verify-deliverables.json", C.VERIFY_PLUGIN) if req.id == "verify"
                      else ("plugin/trim-large-reads.json", C.TRIM_PLUGIN))
        files[path] = body
        return path
    if form == "script":
        files["scripts/exact_sum.py"] = C.EXACT_SUM_SCRIPT
        _append_line(files, "tool_descriptions/tool_guidance.md",
                     "- Compute every number with `run_python` or `scripts/exact_sum.py <file.csv> "
                     "<amount column> [group column]`; copy the printed values, never do arithmetic by hand.")
        return "scripts/exact_sum.py"
    path = {"memory": "LongTermMEMORY.md", "guardrail": "guardrails.md",
            "guidance": "tool_descriptions/tool_guidance.md", "systemprompt": "systemprompt.md"}[form]
    _append_line(files, path, f"- {rule}" if form != "systemprompt" else f"\n{rule}")
    return path


def _library(layout_index: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"`harnesses/([^/`]+)/`", layout_index)))


def proposer(task: Task, mission: str, rng: random.Random) -> Policy:
    domain = task.domain
    if "everything that would help" in mission:
        kind = None
    else:
        kind = "inv" if "YOUR DIMENSION: INV" in mission else "dom"
    retry = "YOUR PREVIOUS ATTEMPT WAS REJECTED" in mission

    layout = (yield from read_all(["context/LAYOUT.md", "context/task.md"], "Reading the layout.")).get(
        "context/LAYOUT.md", "")
    refs = _labels(layout, "rollouts")
    pool = _labels(layout, "pool")
    checkpoints = sorted([p for p in pool if re.fullmatch(r"c\d+", p)], key=lambda s: int(s[1:]))
    ev = yield from gather(refs, "context/rollouts", task)
    if checkpoints:  # sequential: the latest checkpoint's rollout is on-policy evidence
        ev.update((yield from gather(checkpoints[-1:], "context/pool", task)))
        latest = ev[checkpoints[-1]]
        ev = {k: v for k, v in ev.items() if k == checkpoints[-1] or not latest.delivered}
    harness = yield from read_harness("context/harness")

    if retry:  # repair what the validator rejected, keep the rest
        for path, text in list(harness.items()):
            if path.endswith(".md"):
                fixed = re.sub(r"^.*`(?:skills/[a-z0-9_-]+/)?scripts/check_totals\.py`.*\n?", "", text, flags=re.M)
                if fixed != text:
                    harness[path] = fixed
                    yield Act("Removing the reference to a script I did not ship.",
                              [call("write_file", path=f"context/harness/{path}", content=fixed)])
        yield Act("", [call("finish", notes="<notes>revised -> removed a dangling script reference "
                                            "-> validator rejection</notes>")])
        return

    index = (yield from read_all(["context/library/INDEX.md"])).get("context/library/INDEX.md", "")
    lib_tasks = _library(index)
    lib_files: dict[str, dict[str, str]] = {}
    for t in lib_tasks:
        lib_files[t] = yield from read_harness(f"context/library/harnesses/{t}/harness")

    files = dict(harness)
    notes: list[str] = []
    issues = diagnose(task, ev, kind)
    n_fix = 1 + (rng.random() < 0.6) + (rng.random() < 0.25)
    for issue in issues[:n_fix]:
        r = issue.req
        form = r.fix_form if rng.random() < 0.75 else r.alt_form
        lib_skill = _library_skill(lib_files, domain) if form == "skill" else None
        vague = form not in ("plugin", "script") and rng.random() < C.P_VAGUE
        path = apply_fix(files, r, form, domain, lib_skill, vague=vague)
        src = " (starting from the library's skill)" if lib_skill and f"skills/{C.DOMAIN_SKILLS[domain][0]}/SKILL.md" not in harness else ""
        notes.append(f"{form} edit in `{path}`{src} -> {issue.moment()}")

    # reuse: carry library components for this dimension even without a symptom here
    if lib_files and rng.random() < 0.75:
        if kind in ("inv", None):
            for lf in lib_files.values():
                for path in ("guardrails.md", "tool_descriptions/tool_guidance.md"):
                    for line in lf.get(path, "").splitlines():
                        inv = [r for r in C.req_addressed_by(line) if C.REQS[r].kind == "inv"]
                        if inv and line.strip() not in files.get(path, ""):
                            _append_line(files, path, line)
                            notes.append(f"reused a `{path}` rule ({inv[0]}) from the {domain} library "
                                         "-> lesson in INDEX.md")
        for path, text in _library_components(lib_files, domain, kind).items():
            if files.get(path) != text and (path not in files or path.startswith(("skills/", "plugin/"))):
                if path.endswith("SKILL.md") and path in files:
                    merged = list(dict.fromkeys(C.skill_rules(parse_frontmatter(files[path])[1])
                                                + C.skill_rules(parse_frontmatter(text)[1])))
                    files[path] = C.skill_text(domain, merged)
                else:
                    files[path] = text
                if path.endswith("SKILL.md"):
                    _append_line(files, "guardrails.md", C.trigger_line(domain))
                notes.append(f"reused `{path}` from the {domain} library -> lesson in INDEX.md; "
                             "re-verified against this task's inputs")

    if rng.random() < 0.08:  # misdiagnosis: an edit no trajectory moment supports
        unseen = [r for r in C.reqs_for(task.sim.reqs, kind) if r.id not in {i.req.id for i in issues}]
        if unseen:
            r = rng.choice(unseen)
            apply_fix(files, r, r.alt_form if r.alt_form != "script" else "guidance", domain)
            notes.append(f"precautionary rule for {r.id} -> risk (not observed in these runs)")
    if rng.random() < 0.1:
        _append_line(files, "systemprompt.md", "\n" + C.SPECULATIVE)
        notes.append("general carefulness paragraph in `systemprompt.md` -> risk")
    skill_path = f"skills/{C.DOMAIN_SKILLS[domain][0]}/SKILL.md"
    if skill_path in files and files.get(skill_path) != harness.get(skill_path) and rng.random() < 0.1:
        files[skill_path] += "- Run `scripts/check_totals.py` to verify the result.\n"  # never shipped

    changed = {p: t for p, t in files.items() if harness.get(p) != t}
    if changed:
        yield Act("Writing the edits.", [call("write_file", path=f"context/harness/{p}", content=t)
                                         for p, t in changed.items()])
    body = "\n".join(notes) if changed else "KEEP_BASE -> no trajectory moment argues for a change"
    yield Act("", [call("finish", notes=f"<notes>\n{body}\n</notes>")])


def _library_skill(lib_files: dict[str, dict[str, str]], domain: str) -> str | None:
    path = f"skills/{C.DOMAIN_SKILLS[domain][0]}/SKILL.md"
    best = None
    for files in lib_files.values():
        if path in files and (best is None or len(files[path]) > len(best)):
            best = files[path]
    return best


def _library_components(lib_files: dict[str, dict[str, str]], domain: str, kind: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for files in lib_files.values():
        for path, text in files.items():
            if path.startswith("skills/") and kind in ("dom", None):
                if len(text) > len(out.get(path, "")):
                    out[path] = text
            if path.startswith(("plugin/", "scripts/")) and kind in ("inv", None):
                out[path] = text
    return out


# ===================================================================== composer

def _added_lines(old: str, new: str) -> str:
    olds = set(old.splitlines())
    return "\n".join(ln for ln in new.splitlines() if ln not in olds)


def _merge(path: str, start: str, versions: list[str], domain: str) -> str:
    if path.endswith("SKILL.md"):
        rules: list[str] = []
        for v in [start, *versions]:
            rules += C.skill_rules(parse_frontmatter(v)[1]) if v else []
        return C.skill_text(domain, list(dict.fromkeys(rules)))
    if path.endswith((".json", ".py")) or not start:
        return versions[-1]
    lines = start.rstrip("\n").splitlines()
    for v in versions:
        for ln in v.splitlines():
            if ln not in lines:
                lines.append(ln)
    return "\n".join(lines) + "\n"


def composer(task: Task, mission: str, rng: random.Random) -> Policy:
    domain = task.domain
    layout = (yield from read_all(["context/LAYOUT.md", "context/decisions.md"], "Reading the evidence.")
              ).get("context/LAYOUT.md", "")
    refs = _labels(layout, "rollouts")
    pool = _labels(layout, "pool")
    rounds = [int(m) for p in pool for m in re.findall(r"^(?:inv|dom)(\d+)$", p)]
    current_round = max(rounds) if rounds and "checkpoint" in mission else None
    cands = [p for p in pool if re.fullmatch(r"(inv|dom)\d+", p)
             and (current_round is None or p.endswith(str(current_round)))]
    checkpoints = sorted([p for p in pool if re.fullmatch(r"c\d+", p)], key=lambda s: int(s[1:]))

    ref_ev = yield from gather(refs, "context/rollouts", task)
    if checkpoints:
        ref_ev.update((yield from gather(checkpoints[-1:], "context/pool", task)))
    cand_ev = yield from gather(cands, "context/pool", task)
    start = yield from read_harness("context/harness")
    cand_files = {}
    for c in cands:
        cand_files[c] = yield from read_harness(f"context/pool/{c}/harness")
    notes_txt = yield from read_all([f"context/pool/{c}/notes.md" for c in cands])

    fails = {r.id: [lbl for lbl, e in ref_ev.items() if e.symptom(r, task.sim.fmt)]
             for r in C.reqs_for(task.sim.reqs)}
    keep: dict[str, list[str]] = {}
    decisions: list[str] = []
    for c in cands:
        e = cand_ev[c]
        changed = {p: t for p, t in cand_files[c].items() if start.get(p) != t}
        if not changed:
            decisions.append(f"dropped -> {c} (KEEP_BASE) -> nothing to merge")
            continue
        from_lib = "library" in notes_txt.get(f"context/pool/{c}/notes.md", "")
        for path, text in changed.items():
            targets = [r for r in C.req_addressed_by(_added_lines(start.get(path, ""), text))
                       if r in fails]
            helped = [r for r in targets if fails[r] and not e.symptom(C.REQS[r], task.sim.fmt)]
            if not e.delivered:
                verdict, p_keep = f"{c}'s probe DELIVERED NOTHING; treating as unsafe", 0.03
            elif helped:
                r = helped[0]
                verdict = (f"'{C.REQS[r].fail_say or r}' in {len(fails[r])}/{len(ref_ev)} base runs, "
                           f"gone in {c}'s probe") if C.REQS[r].fail_say else (
                    f"{r} failed in {len(fails[r])}/{len(ref_ev)} base runs, fixed in {c}'s probe")
                p_keep = 0.93
            elif targets and all(not fails[r] for r in targets):
                verdict = "targets nothing the base runs got wrong" + (" (library lesson)" if from_lib else "")
                p_keep = 0.7 if from_lib else 0.3
            elif targets:
                verdict, p_keep = f"{c}'s probe still shows the targeted failure", 0.25
            else:
                verdict, p_keep = "no observable mechanism", 0.15
            if rng.random() < p_keep:
                keep.setdefault(path, []).append(text)
                decisions.append(f"kept -> {path} from {c} -> {verdict}")
            else:
                decisions.append(f"dropped -> {path} from {c} -> {verdict}")

    composed = dict(start)
    for path, versions in keep.items():
        composed[path] = _merge(path, start.get(path, ""), versions, domain)
    # a skill only exists if something names it
    skill = C.DOMAIN_SKILLS[domain][0]
    if f"skills/{skill}/SKILL.md" in composed:
        _append_line(composed, "guardrails.md", C.trigger_line(domain))
    # pay for always-on text: cut the speculative paragraph if it crept in
    if C.SPECULATIVE in composed.get("systemprompt.md", "") and rng.random() < 0.8:
        composed["systemprompt.md"] = composed["systemprompt.md"].replace("\n" + C.SPECULATIVE, "").replace(C.SPECULATIVE, "")
        decisions.append("revised -> systemprompt.md -> cut a general paragraph no trajectory supports")
    # drop broken references the validator would reject
    for path, text in list(composed.items()):
        if "scripts/check_totals.py" in text:
            composed[path] = re.sub(r"^.*scripts/check_totals\.py.*\n?", "", text, flags=re.M)

    changed = {p: t for p, t in composed.items() if start.get(p) != t}
    if changed:
        yield Act("Composing the final harness.", [call("write_file", path=f"context/harness/{p}", content=t)
                                                  for p, t in changed.items()])
    body = "\n".join(decisions) or "KEEP_BASE -> no candidate earned a place"
    yield Act("", [call("finish", notes=f"<notes>\n{body}\n</notes>")])


# ===================================================================== selector

P_RECHECK = 0.3      # chance the selector recomputes a given figure from the source
SELECT_NOISE = 1.0   # judgment noise on top of the checks

def _fidelity(task: Task, text: str, rng: random.Random) -> float:
    spec = task.sim
    truth = spec.solve(task.files, set())
    score = 0.0
    if spec.fmt == "json":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return -5.0
        score += 2.0 if sorted(obj) == sorted(truth) else -2.0
        for k, v in truth.items():  # recomputing a figure from the source is effortful and fallible
            if rng.random() < P_RECHECK:
                score += 0.5 if json.dumps(obj.get(k), sort_keys=True) == json.dumps(v, sort_keys=True) else -1.0
        if spec.consistency:
            try:
                score += 1.0 if spec.consistency(obj) else -1.0
            except Exception:
                score -= 1.0
        return score
    if spec.fmt == "csv":
        expect = render(spec, truth, set(), rng).splitlines()
        lines = text.splitlines()
        score += 2.0 if lines[:1] == expect[:1] else -2.0
        for ln in expect[1:]:
            if rng.random() < P_RECHECK:
                score += 0.5 if ln in lines else -1.0
        return score
    expect = [ln for ln in truth.splitlines() if ln.strip()]
    for ln in expect:
        if ln.startswith(("#", "|")) and rng.random() < 0.8:
            score += 0.4 if ln in text else -1.0
        elif re.search(r"\d", ln) and rng.random() < P_RECHECK:
            nums = re.findall(r"\d+(?:\.\d+)?%?", ln)
            score += 0.4 if all(n in text for n in nums) else -1.0
    return score


def selector(task: Task, mission: str, rng: random.Random) -> Policy:
    ids = sorted(set(re.findall(r"cand-[A-Z]", mission)))
    yield from read_all(["context/task.md"], "Reading the request.")
    srcs = yield from list_files("context/source")
    yield from read_all(srcs[:4])
    name = task.sim.deliverable.split("/")[-1]
    got = yield from read_all([f"context/candidates/{i}/{name}" for i in ids], "Reading every candidate.")
    scored = []
    for i in ids:
        text = got.get(f"context/candidates/{i}/{name}")
        s = -10.0 if text is None else (-5.0 if problems(task.sim.fmt, text) else 0.0)
        if text is not None:
            s += _fidelity(task, text, rng)
        scored.append((s + rng.gauss(0, SELECT_NOISE), i))
    scored.sort(reverse=True)
    best = scored[0][1]
    runner = scored[1][1] if len(scored) > 1 else best
    why = (f"{best} conforms to the requested shape and its figures check out against the source; "
           f"{runner} disagrees with the source on at least one recomputed value.")
    yield Act("", [call("finish", answer=f"<why>\n{why}\n</why>\n```choice\n{best}\n```")])


# ===================================================================== reflector

def reflector(task: Task, mission: str, rng: random.Random) -> Policy:
    layout = (yield from read_all(["context/LAYOUT.md"], "Reading the record.")).get("context/LAYOUT.md", "")
    refs = _labels(layout, "rollouts")
    pool = [p for p in _labels(layout, "pool") if re.fullmatch(r"(inv|dom)\d+", p)]
    ref_ev = yield from gather(refs, "context/rollouts", task)
    cand_ev = yield from gather(pool, "context/pool", task)
    base = yield from read_harness("context/base_harness")
    adopted = yield from read_harness("context/harness")
    rows: list[str] = []
    helped: list[tuple[int, C.Req, str, str]] = []
    for c in pool:
        files = yield from read_harness(f"context/pool/{c}/harness")
        changed = {p: t for p, t in files.items() if base.get(p) != t}
        for path, text in changed.items():
            targets = C.req_addressed_by(_added_lines(base.get(path, ""), text))
            fails = {r: [lbl for lbl, e in ref_ev.items() if e.symptom(C.REQS[r], task.sim.fmt)]
                     for r in targets if r in C.REQS}
            e = cand_ev[c]
            good = [r for r, f in fails.items() if f and not e.symptom(C.REQS[r], task.sim.fmt)]
            if not e.delivered:
                verdict, observed = "hurt", "the probe delivered nothing"
            elif good and len(changed) > 1 and rng.random() < 0.4:
                verdict, observed = "confounded", f"{c} also changed {len(changed) - 1} other component(s)"
            elif good:
                verdict = "helped"
                observed = f"failure in {len(fails[good[0]])}/{len(ref_ev)} base runs is absent from {c}'s probe"
                added = _added_lines(base.get(path, ""), text)
                if adopted.get(path) == text or (adopted.get(path) and line_overlap(added, adopted[path]) >= 0.5):
                    helped.append((len(fails[good[0]]), C.REQS[good[0]], path, c))
            else:
                verdict, observed = "no observed effect", "the probe behaves like the base runs"
            rows.append(f"component: {path}\ncandidates: {c}\nchange: "
                        f"{'adds ' + ', '.join(targets) if targets else 'general edit'}\n"
                        f"observed: {observed}\nverdict: {verdict}")
    edits = "<edits>\n" + "\n\n".join(rows or ["(no candidate changed anything)"]) + "\n</edits>\n\n"
    if not helped:
        yield Act("", [call("finish", answer=edits + "NO_ENTRY")])
        return
    helped.sort(key=lambda h: -h[0])
    n, r, path, c = helped[0]
    form = {"skill": "an on-demand skill", "plugin": "a plugin", "guidance": "tool guidance",
            "guardrail": "a guardrail", "memory": "always-on memory", "script": "a script"}
    kind = next((f for f in form if f in path or (f == "guidance" and "tool_guidance" in path)
                 or (f == "memory" and "MEMORY" in path)), "skill")
    exp = (f"<experience>\ncue: when {r.situation}\nlesson: {r.rule} Encode it as {form.get(kind, 'a component')} "
           f"(`{path}`).\nevidence: {c} row -- failure in {n}/{len(ref_ev)} base runs, absent in the probe\n"
           f"confidence: {'high' if n == len(ref_ev) else 'medium'} -- {n} of {len(ref_ev)} references "
           "showed the failure\n</experience>")
    yield Act("", [call("finish", answer=edits + exp)])


def subagent() -> Policy:
    yield Act("", [call("finish", summary="(the simulator does not model subagent work)")])


__all__ = ["Act", "executor", "proposer", "composer", "selector", "reflector", "subagent", "call"]


def _json_safe(x) -> str:  # pragma: no cover - debugging helper
    return json.dumps(x, default=str)[:400]
