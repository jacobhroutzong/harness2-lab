"""The simulator's 'world model': latent requirements, their symptoms, and their fixes.

Every built-in task exercises a handful of latent requirements. Each one is something a
weak executor sometimes gets wrong, that leaves a *visible symptom* in its trajectory, and
that a specific *harness component* can fix:

    INV (domain-invariant: how the agent works)      shared by every task
        verify  shape  arith  budget
    DOM (domain-specific: what a practitioner knows)  shared within one domain
        finance: fin.partial fin.signed fin.orphans fin.fx
        ops:     ops.utc ops.rate ops.scope ops.sections
        analytics: ana.dedupe ana.nps ana.exclude ana.order

The simulated improver "knows" this catalog the way a real LLM knows accounting or SRE
practice: it can recognise a symptom and write a fix. It never sees a grader. Probabilities
here are calibration knobs of the toy world, not claims about any real model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Req:
    id: str
    kind: str                  # "inv" | "dom"
    domain: str | None
    p_know: float              # P(the frozen model reliably handles it unaided) -- see competence()
    cue: str                   # regex: harness text that addresses this requirement
    fail_say: str              # what the executor says when it gets it wrong
    pass_say: str              # what it says when it handles it
    situation: str             # the cue a reflector would write for a lesson
    rule: str                  # the fix, as one checkable rule
    fix_form: str              # cheapest effective form: skill | guardrail | guidance | plugin
    alt_form: str              # the tempting, worse form a proposer sometimes picks

    @property
    def cue_re(self) -> re.Pattern[str]:
        return re.compile(self.cue, re.I | re.S)


R = Req
REQS: dict[str, Req] = {r.id: r for r in [
    # ---------------------------------------------------------------- INV
    R("verify", "inv", None, 0.35, r"reopen|re-open|read (it|the (file|deliverable)) back|check (that )?it parses",
      fail_say="", pass_say="Reopening the deliverable to check it.",
      situation="an agent writes a structured deliverable and finishes without reopening it",
      rule="After writing a deliverable, reopen it with read_file and check that it parses and is complete before calling finish.",
      fix_form="plugin", alt_form="guardrail"),
    R("shape", "inv", None, 0.85, r"exact(ly)? (the )?(field|key|column|header|heading)s?|never (add|rename)|do not (add|rename)",
      fail_say="I'll make the field names more readable and add a short notes field for context.",
      pass_say="Using exactly the names the task specifies.",
      situation="a deliverable's fields, columns or headings are named by the request",
      rule="Deliver exactly the fields, columns and headings the task names, spelled exactly as given; never add, rename or reorder them.",
      fix_form="guardrail", alt_form="systemprompt"),
    R("arith", "inv", None, 0.7, r"compute every number|never (do )?(arithmetic|math) by hand|copy the printed values",
      fail_say="Adding these up by hand from what I read.",
      pass_say="Computing the numbers with run_python to keep them exact.",
      situation="a deliverable contains totals, rates or other computed numbers",
      rule="Compute every number with `run_python` (or a script) and copy the printed values; never do arithmetic by hand.",
      fix_form="guidance", alt_form="script"),
    R("budget", "inv", None, 0.5, r"(large|big|long) (input )?files?|over ~?4,?000 chars|do not page",
      fail_say="I lost my place in the file; re-reading it from the start.",
      pass_say="The input is large, so I load it in run_python instead of paging through it.",
      situation="an input file is much larger than one read_file window",
      rule="For any input over ~4,000 chars, do not page through it with read_file: load it in `run_python` and print only the counts or aggregates you need.",
      fix_form="guidance", alt_form="plugin"),
    # ---------------------------------------------------------------- finance
    R("fin.partial", "dom", "finance", 0.72, r"several (partial )?payments|partial payments|all payments (applied )?per",
      fail_say="Matching each invoice to its payment by reference (one payment per invoice).",
      pass_say="Some invoices have several payments; summing all payments applied to each invoice.",
      situation="one document can be settled by several partial payments",
      rule="An invoice can be settled by several partial payments: sum all payments applied to each invoice before comparing with its amount.",
      fix_form="skill", alt_form="memory"),
    R("fin.signed", "dom", "finance", 0.7, r"negative|credit (note|memo)s?|keep (the|their) sign",
      fail_say="Treating every amount as a positive charge (absolute values).",
      pass_say="Some amounts are negative (credits/refunds); keeping their sign.",
      situation="a ledger, statement or expense file contains negative amounts (credit notes, refunds)",
      rule="Credit notes, credit memos and refunds are NEGATIVE amounts: keep the sign so they reduce totals and balances; never take abs() or drop negative rows.",
      fix_form="skill", alt_form="memory"),
    R("fin.orphans", "dom", "finance", 0.75, r"no counterpart|one[- ]sided|unmatched|missing from (either|one) side",
      fail_say="Skipping rows that have no counterpart on the other side.",
      pass_say="Listing the items that have no counterpart on the other side.",
      situation="two sources are matched and some items exist on only one side",
      rule="Items with no counterpart on the other side (unapplied payments, one-sided invoices) must be reported, never silently dropped.",
      fix_form="skill", alt_form="memory"),
    R("fin.fx", "dom", "finance", 0.7, r"convert(ing)? (every|all|each)? ?(amount|currenc|eur|line)|exchange rate|fx rate",
      fail_say="Summing the amounts as given (ignoring the currency column).",
      pass_say="Converting non-USD lines with the rate in fx.json before aggregating.",
      situation="amounts come in several currencies",
      rule="Convert every amount to the reporting currency with the provided rate BEFORE aggregating or ranking.",
      fix_form="skill", alt_form="memory"),
    # ---------------------------------------------------------------- ops
    R("ops.utc", "dom", "ops", 0.7, r"utc offsets?|offsets?.*utc|convert(ing)? .*to utc|normali[sz]e .*timestamps",
      fail_say="Ordering events by their timestamps as written.",
      pass_say="Timestamps mix UTC offsets; converting everything to UTC before ordering.",
      situation="timestamps from several sources carry different UTC offsets",
      rule="Sources mix UTC offsets: convert every timestamp to UTC before ordering, windowing or reporting times.",
      fix_form="skill", alt_form="memory"),
    R("ops.rate", "dom", "ops", 0.72, r"4xx (are|is) client|only 5xx|5xx only|exclude 4xx",
      fail_say="Counting every 4xx and 5xx response as an error.",
      pass_say="Counting only 5xx as server errors, over all requests.",
      situation="an error rate or availability is computed from HTTP status counts",
      rule="Error rate / availability counts ONLY server errors (5xx) over ALL requests; 4xx are client errors and do not count against availability.",
      fix_form="skill", alt_form="memory"),
    R("ops.scope", "dom", "ops", 0.75, r"(same|affected|named) service|filter (to|by) (the )?service|scope (the )?(signals|analysis)",
      fail_say="Aggregating across all services in the files.",
      pass_say="Restricting the analysis to the affected service.",
      situation="input files mix data from several services",
      rule="Scope every aggregate to the affected / named service (and window) before computing it.",
      fix_form="skill", alt_form="memory"),
    R("ops.sections", "dom", "ops", 0.78, r"incident (report )?anatomy|summary, timeline, impact|root cause.{0,40}action items",
      fail_say="Writing a short summary; root cause and actions can be added later.",
      pass_say="Following the standard incident-report anatomy.",
      situation="an incident report or postmortem is requested",
      rule="An incident report always has: Summary, Timeline (UTC), Impact (quantified), Root Cause, Action Items (bulleted, concrete).",
      fix_form="skill", alt_form="memory"),
    # ---------------------------------------------------------------- analytics
    R("ana.dedupe", "dom", "analytics", 0.7, r"distinct (users|people|respondents)|de-?dup|people,? not (rows|events)",
      fail_say="Counting each row as one person.",
      pass_say="Some people appear more than once; counting distinct people.",
      situation="survey or event rows can repeat the same person",
      rule="Count distinct people, not rows or events: dedupe resubmissions (latest answer wins) and repeat events before counting.",
      fix_form="skill", alt_form="memory"),
    R("ana.nps", "dom", "analytics", 0.72, r"passives|nps .*all respondents|denominator",
      fail_say="NPS: promoters versus detractors among those two groups.",
      pass_say="NPS uses all respondents as the denominator, passives included.",
      situation="a Net Promoter Score is computed",
      rule="NPS = %promoters (9-10) - %detractors (0-6) with ALL respondents (passives included) in the denominator.",
      fix_form="skill", alt_form="memory"),
    R("ana.exclude", "dom", "analytics", 0.7, r"both (arms|variants)|contaminat|more than one (experiment )?(arm|variant)",
      fail_say="Keeping every assigned user in the analysis.",
      pass_say="Some users were assigned to both variants; excluding them.",
      situation="experiment units may be exposed to more than one arm",
      rule="Exclude units assigned to more than one experiment arm before computing any per-arm rate; report how many were excluded.",
      fix_form="skill", alt_form="memory"),
    R("ana.order", "dom", "analytics", 0.75, r"previous step|ordered funnel|in (time )?order",
      fail_say="Counting each funnel step independently.",
      pass_say="Each step must follow the previous one in time; enforcing order.",
      situation="a multi-step funnel is computed from timestamped events",
      rule="A user counts at a funnel step only if they completed every previous step earlier in time (ordered funnel).",
      fix_form="skill", alt_form="memory"),
]}

# Model knowledge is persistent, not a coin flip per run: for each simulator seed the frozen
# model either knows a requirement (then slips rarely) or has a gap (then fails most runs).
# Gaps in DOM requirements are shared across a domain, INV habits across everything, which
# is what makes base runs agree on their mistakes and what lets the library pay off.
P_WHEN_KNOWN = 0.96
P_WHEN_GAP = 0.12
P_SAY = 0.7              # how often a run narrates the way it handled a requirement
P_MALFORMED = 0.06       # an iid slip: a malformed deliverable
P_REREAD_CUED = 0.92
P_FIX_AFTER_SIGNAL = 0.95
P_DOM_SKILL = 0.95       # rule loaded from a skill
P_DOM_ALWAYS_ON = 0.90   # rule sitting in always-on text (before dilution)
P_INV_CUED = 0.95
P_SCRIPT = 0.98
ALWAYS_ON_COMFORT = 2500  # chars of always-on text before attention starts to dilute

DOMAIN_SKILLS = {
    "finance": ("finance-reconciliation",
                "reconciling, summing or comparing ledgers, invoices, payments, statements or expenses"),
    "ops": ("incident-analysis",
            "analysing logs, alerts, deploys, error rates or SLOs"),
    "analytics": ("metrics-readout",
                  "computing counts, rates, NPS, experiment or funnel metrics from survey or event data"),
}

VERIFY_PLUGIN = """{
  "description": "Check every deliverable the moment it is written, and refuse to finish until each was reopened",
  "hooks": [
    {"event": "after_tool", "tool": "write_file", "match_path": "\\\\.json$", "action": "verify_json"},
    {"event": "after_tool", "tool": "write_file", "match_path": "\\\\.csv$", "action": "verify_csv"},
    {"event": "before_finish", "action": "require_reread"}
  ]
}
"""

TRIM_PLUGIN = """{
  "description": "Stop large input files from being paged through read_file",
  "hooks": [
    {"event": "after_tool", "tool": "read_file", "action": "trim_output", "max_chars": 2500,
     "hint": "This input is large: load it in run_python and print only the aggregates you need."}
  ]
}
"""

EXACT_SUM_SCRIPT = '''"""Exact sums of a CSV column, optionally grouped (Decimal arithmetic, no float drift).

usage: python scripts/exact_sum.py <file.csv> <amount column> [group column]
"""
import csv
import json
import sys
from collections import defaultdict
from decimal import Decimal

path, column = sys.argv[1], sys.argv[2]
group = sys.argv[3] if len(sys.argv) > 3 else None
totals = defaultdict(Decimal)
with open(path, newline="") as fh:
    for row in csv.DictReader(fh):
        totals[row[group] if group else "total"] += Decimal(row[column])
print(json.dumps({k: str(v) for k, v in sorted(totals.items())}, indent=2))
'''

P_VAGUE = 0.3           # the editor's practitioner knowledge is imperfect: a vague rule


def vague_rule(req: "Req") -> str:
    """A well-meant rule that names no checkable operation (and so changes no behaviour)."""
    return f"Take extra care with the {req.id.split('.')[-1]} aspect of the work."


SPECULATIVE = ("Be thorough. Double-check every step of your work, consider edge cases carefully, "
               "and make sure the final answer is complete, accurate and well organised.")


@dataclass
class Edit:
    """One proposed change, as the simulated proposer would write it."""

    req: str
    form: str
    files: dict[str, str] = field(default_factory=dict)       # path -> full new content
    appends: dict[str, str] = field(default_factory=dict)     # path -> line(s) to append


def skill_text(domain: str, rules: list[str]) -> str:
    name, desc = DOMAIN_SKILLS[domain]
    body = "\n".join(f"- {r}" for r in rules)
    return (f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n\n"
            "Practitioner rules for this kind of work. Apply each one, then check the deliverable "
            f"against it before finishing.\n\n{body}\n")


def skill_rules(text: str) -> list[str]:
    return [ln[2:].strip() for ln in text.splitlines() if ln.startswith("- ")]


def trigger_line(domain: str) -> str:
    name, desc = DOMAIN_SKILLS[domain]
    return f"- When {desc}, load the `{name}` skill."


def competence(seed: int, req: Req, domain: str) -> float:
    """P(this run handles `req`) for a model with no help -- fixed per (seed, domain, req)."""
    import random

    scope = domain if req.kind == "dom" else "*"
    knows = random.Random(f"competence:{seed}:{scope}:{req.id}").random() < req.p_know
    return P_WHEN_KNOWN if knows else P_WHEN_GAP


def reqs_for(task_reqs: list[str], kind: str | None = None) -> list[Req]:
    return [REQS[r] for r in task_reqs if kind is None or REQS[r].kind == kind]


def req_addressed_by(text: str) -> list[str]:
    """Which requirements a piece of harness text addresses (what a reader of a diff infers)."""
    return [r.id for r in REQS.values() if r.cue_re.search(text)]
