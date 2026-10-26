"""Analytics domain: survey, experiment and funnel readouts.

Recurring domain knowledge:
  - count distinct people, not rows/events (dedupe resubmissions and repeat events)
  - NPS = %promoters (9-10) - %detractors (0-6) over ALL respondents, passives included
  - exclude units exposed to more than one experiment arm before computing rates
  - a funnel step only counts users who completed the previous step earlier in time
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta

from .base import Check, SimSpec, Task, close, field_check, json_checks, q, rows

# --------------------------------------------------------------------------- ana-01


def _gen_responses(seed: int = 3) -> str:
    rng = random.Random(seed)
    out = ["response_id,respondent_id,submitted_at,score,comment"]
    t = datetime(2026, 9, 1, 9, 0)
    comments = ["", "", "great support", "too slow", "love the new UI", "pricing is high",
                "works fine", "export is broken", ""]
    rid = 0
    people = [f"U{100 + i}" for i in range(64)]
    for person in people:
        rid += 1
        t += timedelta(minutes=rng.randint(5, 90))
        score = rng.choices(range(11), [3, 2, 2, 3, 3, 4, 6, 9, 12, 14, 12])[0]
        out.append(f"R{rid:03d},{person},{t.isoformat()},{score},{rng.choice(comments)}")
    for person in rng.sample(people, 9):  # resubmissions: the later answer supersedes
        rid += 1
        t += timedelta(minutes=rng.randint(5, 90))
        out.append(f"R{rid:03d},{person},{t.isoformat()},{rng.choice([3, 4, 9, 10])},updated answer")
    return "\n".join(out) + "\n"


def solve_ana01(files: dict[str, str], mistakes: set[str]) -> dict:
    data = rows(files["input/responses.csv"])
    if "ana.dedupe" not in mistakes:
        latest: dict[str, dict] = {}
        for r in sorted(data, key=lambda r: r["submitted_at"]):
            latest[r["respondent_id"]] = r
        data = list(latest.values())
    scores = [int(r["score"]) for r in data]
    pro = sum(s >= 9 for s in scores)
    det = sum(s <= 6 for s in scores)
    base = len(scores) if "ana.nps" not in mistakes else (pro + det)
    return {"respondents": len(scores), "promoters": pro, "detractors": det,
            "nps": round(100 * (pro - det) / base)}


def ana01() -> Task:
    files = {"input/responses.csv": _gen_responses()}
    truth = solve_ana01(files, set())
    path = "output/nps.json"
    keys = ["respondents", "promoters", "detractors", "nps"]
    checks = json_checks(path, keys) + [
        field_check("respondents", "distinct respondents (latest answer wins)", path,
                    lambda o: o["respondents"], lambda t: t["respondents"], weight=1.5),
        field_check("promoters", "promoters among deduplicated answers", path,
                    lambda o: o["promoters"], lambda t: t["promoters"]),
        field_check("detractors", "detractors among deduplicated answers", path,
                    lambda o: o["detractors"], lambda t: t["detractors"]),
        field_check("nps", "NPS over all respondents", path, lambda o: o["nps"],
                    lambda t: t["nps"], lambda a, b: close(a, b, 0.5), weight=2.0),
    ]
    prompt = f"""\
Compute the September Net Promoter Score from `input/responses.csv` (0-10 "how likely are you
to recommend us" scores). Some customers answered more than once.

Write `{path}` with exactly these keys: "respondents" (number of customers counted),
"promoters", "detractors" (counts), and "nps" (the Net Promoter Score, rounded to an integer).
"""
    return Task(
        id="ana-01", domain="analytics", title="Net Promoter Score readout",
        blurb="73 survey rows from 64 people; resubmissions supersede earlier answers.",
        prompt=prompt, files=files, deliverables=[path], truth=truth, checks=checks,
        sim=SimSpec(reqs=["verify", "shape", "arith", "ana.dedupe", "ana.nps"], solve=solve_ana01,
                    fmt="json", deliverable=path, numeric=["nps"],
                    rename={"respondents": "n_respondents"},
                    consistency=lambda o: o["promoters"] + o["detractors"] <= o["respondents"]),
    )


# --------------------------------------------------------------------------- ana-02


def _gen_ab(seed: int = 5) -> tuple[str, str]:
    rng = random.Random(seed)
    assign = ["user_id,variant,assigned_at"]
    conv = ["user_id,converted_at,order_value"]
    users = [f"u{1000 + i}" for i in range(420)]
    both = set(rng.sample(users, 14))
    t0 = datetime(2026, 9, 15)
    for u in users:
        v = rng.choice(["control", "treatment"])
        assign.append(f"{u},{v},{(t0 + timedelta(minutes=rng.randint(0, 9000))).isoformat()}")
        if u in both:
            other = "treatment" if v == "control" else "control"
            assign.append(f"{u},{other},{(t0 + timedelta(minutes=rng.randint(9000, 12000))).isoformat()}")
        p = 0.16 if v == "control" else 0.21
        if u in both:
            p = 0.6
        if rng.random() < p:
            for _ in range(rng.choice([1, 1, 1, 2, 3, 5])):
                when = t0 + timedelta(minutes=rng.randint(9000, 30000))
                conv.append(f"{u},{when.isoformat()},{rng.randint(12, 240)}.{rng.randint(0, 99):02d}")
    head, body = conv[0], conv[1:]
    rng.shuffle(body)
    return "\n".join(assign) + "\n", "\n".join([head, *body]) + "\n"


def solve_ana02(files: dict[str, str], mistakes: set[str]) -> dict:
    arms: dict[str, set[str]] = {}
    for r in rows(files["input/assignments.csv"]):
        arms.setdefault(r["user_id"], set()).add(r["variant"])
    excluded = {u for u, a in arms.items() if len(a) > 1}
    conversions = rows(files["input/conversions.csv"])
    out: dict = {}
    for arm in ("control", "treatment"):
        if "ana.exclude" in mistakes:
            users = {u for u, a in arms.items() if arm in a}
        else:
            users = {u for u, a in arms.items() if a == {arm}}
        hits = [c["user_id"] for c in conversions if c["user_id"] in users]
        conv = len(hits) if "ana.dedupe" in mistakes else len(set(hits))
        out[arm] = {"users": len(users), "converters": conv,
                    "conversion_rate": q(conv / len(users), "0.0001")}
    lift = 100 * (out["treatment"]["conversion_rate"] / out["control"]["conversion_rate"] - 1)
    out["relative_lift_pct"] = q(lift, "0.1")
    out["excluded_users"] = 0 if "ana.exclude" in mistakes else len(excluded)
    return out


def ana02() -> Task:
    a, c = _gen_ab()
    files = {"input/assignments.csv": a, "input/conversions.csv": c}
    truth = solve_ana02(files, set())
    path = "output/ab_readout.json"
    keys = ["control", "treatment", "relative_lift_pct", "excluded_users"]
    arm = lambda name, k: (lambda o: o[name][k])  # noqa: E731
    checks = json_checks(path, keys) + [
        field_check("excluded", "users exposed to both arms are excluded", path,
                    lambda o: o["excluded_users"], lambda t: t["excluded_users"]),
        field_check("control_users", "control arm size after exclusion", path,
                    arm("control", "users"), lambda t: t["control"]["users"]),
        field_check("treatment_converters", "distinct converting users, not purchases", path,
                    arm("treatment", "converters"), lambda t: t["treatment"]["converters"],
                    weight=1.5),
        field_check("rates", "conversion rates per arm", path,
                    lambda o: (o["control"]["conversion_rate"], o["treatment"]["conversion_rate"]),
                    lambda t: (t["control"]["conversion_rate"], t["treatment"]["conversion_rate"]),
                    lambda x, y: close(x[0], y[0], 0.00015) and close(x[1], y[1], 0.00015)),
        field_check("lift", "relative lift of treatment over control", path,
                    lambda o: o["relative_lift_pct"], lambda t: t["relative_lift_pct"],
                    lambda x, y: close(x, y, 0.15), weight=1.5),
    ]
    prompt = f"""\
Read out the checkout-button experiment. `input/assignments.csv` records which variant each
user was assigned; `input/conversions.csv` records purchases (a user can buy more than once).

Write `{path}` with exactly these keys:
- "control" and "treatment": objects with "users", "converters" (users who purchased at
  least once) and "conversion_rate" (converters / users, 4 decimals)
- "relative_lift_pct": treatment rate relative to control, in percent (1 decimal)
- "excluded_users": how many users you excluded from the analysis
"""
    return Task(
        id="ana-02", domain="analytics", title="A/B experiment readout",
        blurb="420 users, a contaminated assignment log and repeat purchases (~45 KB).",
        prompt=prompt, files=files, deliverables=[path], truth=truth, checks=checks,
        sim=SimSpec(reqs=["verify", "shape", "arith", "budget", "ana.exclude", "ana.dedupe"],
                    solve=solve_ana02, fmt="json", deliverable=path,
                    numeric=["relative_lift_pct"], rename={"excluded_users": "excluded"}),
    )


# --------------------------------------------------------------------------- ana-03

ANA03_STEPS = ["visit", "signup", "activate", "purchase"]
ANA03_HEADER = "| step | users | conversion_from_previous |"


def _gen_events(seed: int = 9) -> str:
    rng = random.Random(seed)
    out = ["user_id,event,ts"]
    t0 = datetime(2026, 9, 20)
    for i in range(520):
        u = f"p{2000 + i}"
        t = t0 + timedelta(minutes=rng.randint(0, 20000))
        reached = 1
        for p in (0.55, 0.62, 0.5):
            if rng.random() >= p:
                break
            reached += 1
        for step in ANA03_STEPS[:reached]:
            t += timedelta(minutes=rng.randint(1, 400))
            for _ in range(rng.choice([1, 1, 2, 3])):  # duplicate events (retries, reloads)
                out.append(f"{u},{step},{(t + timedelta(seconds=rng.randint(0, 50))).isoformat()}")
        if rng.random() < 0.08:  # out-of-order: an 'activate' logged without a prior signup
            out.append(f"{u},activate,{(t0 - timedelta(hours=rng.randint(1, 48))).isoformat()}")
    head, body = out[0], out[1:]
    rng.shuffle(body)
    return "\n".join([head, *body]) + "\n"


def solve_ana03(files: dict[str, str], mistakes: set[str]) -> str:
    by_user: dict[str, dict[str, list[str]]] = {}
    raw = {s: 0 for s in ANA03_STEPS}
    for e in rows(files["input/events.csv"]):
        raw[e["event"]] += 1
        by_user.setdefault(e["user_id"], {}).setdefault(e["event"], []).append(e["ts"])

    def reaches(events: dict[str, list[str]], upto: int) -> bool:
        t = ""  # each step must happen at or after the previous step
        for step in ANA03_STEPS[: upto + 1]:
            later = [ts for ts in events.get(step, []) if ts >= t]
            if not later:
                return False
            t = min(later)
        return True

    reached: dict[str, int] = {}
    for i, step in enumerate(ANA03_STEPS):
        if "ana.dedupe" in mistakes:
            reached[step] = raw[step]
        elif "ana.order" in mistakes:
            reached[step] = sum(step in ev for ev in by_user.values())
        else:
            reached[step] = sum(reaches(ev, i) for ev in by_user.values())
    lines = ["# Funnel: 2026-09-20 cohort", "", ANA03_HEADER, "|---|---|---|"]
    prev = None
    for step in ANA03_STEPS:
        conv = "—" if prev is None else f"{100 * reached[step] / prev:.1f}%"
        lines.append(f"| {step} | {reached[step]} | {conv} |")
        prev = reached[step]
    overall = 100 * reached["purchase"] / reached["visit"]
    lines += ["", f"Overall conversion: {overall:.1f}%", ""]
    return "\n".join(lines)


def ana03() -> Task:
    files = {"input/events.csv": _gen_events()}
    truth_md = solve_ana03(files, set())
    truth = {m.group(1): (int(m.group(2)), m.group(3)) for m in
             re.finditer(r"^\| (\w+) \| (\d+) \| (.+?) \|$", truth_md, re.M)}
    truth["overall"] = re.search(r"Overall conversion: ([0-9.]+)%", truth_md).group(1)
    path = "output/funnel.md"

    def table(o):
        return {m.group(1): (int(m.group(2)), m.group(3).strip()) for m in
                re.finditer(r"^\|\s*(\w+)\s*\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*$", o[path], re.M)}

    def users(step):
        return lambda o, t: table(o)[step][0] == t[step][0]

    checks = [
        Check("header", "table header exactly as requested", lambda o, t: ANA03_HEADER in o[path]),
        Check("visit", "distinct visitors", users("visit")),
        Check("signup", "distinct users who signed up after visiting", users("signup")),
        Check("activate", "activation respects step order", users("activate"), 1.5),
        Check("purchase", "purchasers who completed every earlier step", users("purchase"), 1.5),
        Check("conversions", "step-to-step conversion percentages",
              lambda o, t: all(table(o)[s][1] == t[s][1] for s in ANA03_STEPS[1:])),
        Check("overall", "overall conversion line",
              lambda o, t: re.search(rf"Overall conversion:\s*{re.escape(t['overall'])}%", o[path]) is not None),
        Check("no_placeholders", "no TODO/TBD placeholders left",
              lambda o, t: not re.search(r"\bTBD\b|\bTODO\b", o[path]), 0.5),
    ]
    prompt = f"""\
Build the signup funnel for the 2026-09-20 cohort from `input/events.csv` (one row per
tracked event; steps in order: visit -> signup -> activate -> purchase).

Write `{path}` containing a Markdown table with exactly this header:
    {ANA03_HEADER}
one row per step in funnel order (use `—` for the first step's conversion, otherwise a
percentage with 1 decimal), followed by a line `Overall conversion: X.X%` (purchase / visit).
"""
    return Task(
        id="ana-03", domain="analytics", title="Ordered signup funnel",
        blurb="~2,000 shuffled events with duplicates and out-of-order steps (~55 KB).",
        prompt=prompt, files=files, deliverables=[path], truth=truth, checks=checks,
        sim=SimSpec(reqs=["verify", "shape", "arith", "budget", "ana.dedupe", "ana.order"],
                    solve=solve_ana03, fmt="md", deliverable=path,
                    numeric=[r"Overall conversion:\s*([0-9.]+)%"],
                    rename={ANA03_HEADER: "| Step | Users | Conversion |"}),
    )


TASKS = [ana01, ana02, ana03]
