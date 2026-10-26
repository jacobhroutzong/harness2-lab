"""Ops domain: incident analysis over logs, deploys, alerts and SLO data.

Recurring domain knowledge:
  - normalise every timestamp to UTC before ordering or windowing (sources mix offsets)
  - an error rate counts server errors (5xx) over ALL requests; 4xx are client errors
  - scope signals to the affected service / window before aggregating
  - an incident report has a recognised anatomy (summary, timeline, impact, cause, actions)
"""

from __future__ import annotations

import random
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from .base import Check, D, SimSpec, Task, close, field_check, json_checks, md_section, md_sections, q, rows

UTC = timezone.utc

# --------------------------------------------------------------------------- ops-01

OPS01_SECTIONS = ["Summary", "Timeline (UTC)", "Impact", "Root Cause", "Action Items"]


def _gen_access_log(seed: int = 7) -> str:
    rng = random.Random(seed)
    start = datetime(2026, 10, 3, 11, 30, tzinfo=UTC)
    incident = (datetime(2026, 10, 3, 12, 4, 31, tzinfo=UTC), datetime(2026, 10, 3, 12, 41, tzinfo=UTC))
    paths = ["/api/orders", "/api/search", "/api/cart", "/api/checkout", "/api/profile", "/static/app.js"]
    weights = [18, 22, 14, 12, 8, 26]
    hosts = {"web-a": timezone(timedelta(hours=2)), "web-b": UTC}
    lines: list[tuple[datetime, str]] = []
    t = start
    while t < start + timedelta(hours=2):
        t += timedelta(seconds=rng.randint(4, 14))
        host = rng.choice(list(hosts))
        path = rng.choices(paths, weights)[0]
        status = 200
        if rng.random() < 0.05:
            status = rng.choice([404, 404, 401, 429])
        if path == "/api/checkout" and incident[0] <= t <= incident[1] and rng.random() < 0.7:
            status = rng.choice([502, 503, 503])
        lines.append((t, f"{host} GET {path} {status} {rng.randint(18, 900)}ms"))
    # make the very first 5xx unambiguous: web-a (UTC+2) at 12:04:31Z
    lines.append((incident[0], "web-a GET /api/checkout 502 1204ms"))
    lines.append((datetime(2026, 10, 3, 12, 52, 10, tzinfo=UTC), "web-b GET /api/search 500 66ms"))
    lines.sort(key=lambda x: x[0])
    # drop any synthetic 5xx that landed before the pinned first error
    out = []
    for when, rest in lines:
        host = rest.split()[0]
        status = int(rest.split()[3])
        if status >= 500 and when < incident[0]:
            rest = rest.replace(f" {status} ", " 200 ")
        out.append(f"{when.astimezone(hosts[host]).isoformat()} {rest}")
    # web-a and web-b ship logs separately; the merged file is grouped by host, not by time
    out.sort(key=lambda ln: ln.split()[1])
    return "\n".join(out) + "\n"


def _parse_log(text: str) -> list[tuple[datetime, str, str, int]]:
    out = []
    for ln in text.splitlines():
        parts = ln.split()
        if len(parts) >= 5:
            out.append((datetime.fromisoformat(parts[0]), parts[1], parts[3], int(parts[4])))
    return out


def solve_ops01(files: dict[str, str], mistakes: set[str]) -> str:
    entries = _parse_log(files["input/access.log"])
    if "ops.utc" in mistakes:  # read the wall clock as if it were UTC
        entries = [(e[0].replace(tzinfo=UTC), *e[1:]) for e in entries]
    total = len(entries)
    errs = [e for e in entries if e[3] >= 500]
    bad = [e for e in entries if e[3] >= 400] if "ops.rate" in mistakes else errs
    first = min(errs, key=lambda e: e[0].astimezone(UTC))
    last = max(errs, key=lambda e: e[0].astimezone(UTC))
    top, top_n = Counter(e[2] for e in errs).most_common(1)[0]
    rate = 100 * len(bad) / total
    fmt = lambda d: d.astimezone(UTC).strftime("%H:%M:%S")  # noqa: E731
    secs = {
        "Summary": f"Checkout requests failed with 5xx errors between {fmt(first[0])} and "
                   f"{fmt(last[0])} UTC on 2026-10-03.",
        "Timeline (UTC)": f"- {fmt(first[0])} first 5xx ({first[2]} on {first[1]})\n"
                          f"- {fmt(last[0])} last 5xx observed",
        "Impact": f"5xx error rate: {rate:.1f}% ({len(bad)} of {total} requests). "
                  f"{top_n} failures on {top}.",
        "Root Cause": f"Failures concentrate on `{top}` (502/503 from the upstream), "
                      "consistent with a checkout backend outage.",
        "Action Items": "- Add an alert on 5xx rate for `/api/checkout`\n"
                        "- Review the checkout upstream's health checks",
    }
    order = list(OPS01_SECTIONS)
    if "ops.sections" in mistakes:
        order = ["Summary", "Timeline (UTC)", "Impact"]
    return "# Incident report: checkout errors\n\n" + "\n\n".join(
        f"## {h}\n{secs[h]}" for h in order) + "\n"


def ops01() -> Task:
    files = {"input/access.log": _gen_access_log()}
    entries = _parse_log(files["input/access.log"])
    errs = [e for e in entries if e[3] >= 500]
    first = min(errs, key=lambda e: e[0].astimezone(UTC))
    top = Counter(e[2] for e in errs).most_common(1)[0][0]
    truth = {"first": first[0].astimezone(UTC).strftime("%H:%M:%S"),
             "rate": f"{100 * len(errs) / len(entries):.1f}%", "top": top}
    path = "output/incident.md"
    text = lambda o: o[path]  # noqa: E731
    checks = [
        Check("sections", "exactly the five required ## sections, in order",
              lambda o, t: md_sections(text(o)) == OPS01_SECTIONS, 1.5),
        Check("first_error_utc", "timeline gives the first 5xx in UTC",
              lambda o, t: t["first"] in md_section(text(o), "Timeline (UTC)")),
        Check("error_rate", "impact states the 5xx rate over all requests",
              lambda o, t: re.search(rf"5xx error rate:\s*{re.escape(t['rate'])}", text(o)) is not None,
              1.5),
        Check("root_cause", "root cause names the failing endpoint",
              lambda o, t: t["top"] in md_section(text(o), "Root Cause")),
        Check("action_items", "at least two concrete action items",
              lambda o, t: len(re.findall(r"^\s*[-*]\s+\S", md_section(text(o), "Action Items"), re.M)) >= 2),
        Check("no_placeholders", "no TODO/TBD placeholders left",
              lambda o, t: not re.search(r"\bTBD\b|\bTODO\b|<fill", text(o))),
    ]
    prompt = f"""\
Checkout was failing for a while on 2026-10-03. `input/access.log` is the merged access log of
our two web hosts (format: `timestamp host method path status latency`).

Write the incident report `{path}` in Markdown with exactly these sections, in this order:
`## Summary`, `## Timeline (UTC)`, `## Impact`, `## Root Cause`, `## Action Items`.
- The timeline must give the time of the first 5xx response (HH:MM:SS, UTC).
- Impact must contain a line of the form `5xx error rate: X.X% (N of M requests)`.
- Action items are a bulleted list.
"""
    return Task(
        id="ops-01", domain="ops", title="Incident report from an access log",
        blurb="A ~60 KB log from two hosts in different time zones, grouped by host.",
        prompt=prompt, files=files, deliverables=[path], truth=truth, checks=checks,
        sim=SimSpec(reqs=["verify", "shape", "arith", "budget", "ops.utc", "ops.rate", "ops.sections"],
                    solve=solve_ops01, fmt="md", deliverable=path,
                    numeric=[r"5xx error rate:\s*([0-9.]+)%"],
                    rename={"## Timeline (UTC)": "## Timeline"}),
    )


# --------------------------------------------------------------------------- ops-02

OPS02_DEPLOYS = """[
  {"id": "D-71", "service": "payments", "version": "4.12.0", "started_at": "2026-10-07T09:10:00+05:30"},
  {"id": "D-72", "service": "checkout", "version": "2.8.1", "started_at": "2026-10-07T04:02:00Z"},
  {"id": "D-73", "service": "payments", "version": "4.12.1", "started_at": "2026-10-07T00:15:00-04:00"},
  {"id": "D-74", "service": "search", "version": "1.3.0", "started_at": "2026-10-07T06:30:00+02:00"}
]
"""

OPS02_ALERTS = """\
alert_id,service,severity,fired_at,resolved_at
A-1,payments,high,2026-10-07T03:55:00Z,2026-10-07T04:20:00Z
A-2,payments,critical,2026-10-07T04:21:00Z,2026-10-07T05:03:00Z
A-3,payments,high,2026-10-07T04:33:00Z,2026-10-07T04:58:00Z
A-4,checkout,low,2026-10-07T04:40:00Z,2026-10-07T04:45:00Z
A-5,payments,high,2026-10-07T04:41:00Z,2026-10-07T05:20:00Z
A-6,search,medium,2026-10-07T04:50:00Z,2026-10-07T05:05:00Z
A-7,checkout,high,2026-10-07T04:12:00Z,2026-10-07T04:30:00Z
A-8,checkout,high,2026-10-07T04:19:00Z,2026-10-07T04:31:00Z
"""


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def solve_ops02(files: dict[str, str], mistakes: set[str]) -> dict:
    import json

    deploys = json.loads(files["input/deploys.json"])
    alerts = rows(files["input/alerts.csv"])
    best = None
    for d in deploys:
        start = _ts(d["started_at"])
        if "ops.utc" in mistakes:
            start = start.replace(tzinfo=UTC)
        hit = [a for a in alerts
               if ("ops.scope" in mistakes or a["service"] == d["service"])
               and timedelta(0) < _ts(a["fired_at"]) - start <= timedelta(minutes=30)]
        if best is None or len(hit) > len(best[1]):
            best = (d["id"], hit)
    ids = sorted(a["alert_id"] for a in best[1])
    mins = [(_ts(a["resolved_at"]) - _ts(a["fired_at"])).total_seconds() / 60 for a in best[1]]
    return {"suspect_deploy": best[0], "alerts_within_30m": ids,
            "mttr_minutes": q(sum(mins) / len(mins), "0.1") if mins else 0.0}


def ops02() -> Task:
    files = {"input/deploys.json": OPS02_DEPLOYS, "input/alerts.csv": OPS02_ALERTS}
    truth = solve_ops02(files, set())
    path = "output/correlation.json"
    keys = ["suspect_deploy", "alerts_within_30m", "mttr_minutes"]
    checks = json_checks(path, keys) + [
        field_check("suspect", "the deploy followed by the most same-service alerts", path,
                    lambda o: o["suspect_deploy"], lambda t: t["suspect_deploy"], weight=2.0),
        field_check("alerts", "alerts on that service within 30 minutes after it", path,
                    lambda o: sorted(o["alerts_within_30m"]), lambda t: t["alerts_within_30m"],
                    weight=1.5),
        field_check("mttr", "mean time to resolve those alerts", path,
                    lambda o: o["mttr_minutes"], lambda t: t["mttr_minutes"],
                    lambda a, b: close(a, b, 0.05)),
    ]
    prompt = f"""\
Several services paged on the morning of 2026-10-07. `input/deploys.json` lists the deploys
that went out; `input/alerts.csv` lists the alerts that fired.

Find the deploy most likely responsible: the one followed by the most alerts on the SAME
service within 30 minutes after it started. Write `{path}` with exactly these keys:
- "suspect_deploy": that deploy's id
- "alerts_within_30m": sorted list of the ids of those alerts
- "mttr_minutes": the mean minutes from fired_at to resolved_at over those alerts (1 decimal)
"""
    return Task(
        id="ops-02", domain="ops", title="Deploy ↔ alert correlation",
        blurb="Deploy times in four different UTC offsets; alerts in UTC.",
        prompt=prompt, files=files, deliverables=[path], truth=truth, checks=checks,
        sim=SimSpec(reqs=["verify", "shape", "arith", "ops.utc", "ops.scope"], solve=solve_ops02,
                    fmt="json", deliverable=path, numeric=["mttr_minutes"],
                    rename={"alerts_within_30m": "alerts"}),
    )


# --------------------------------------------------------------------------- ops-03

def _gen_requests(seed: int = 11) -> str:
    rng = random.Random(seed)
    tz = timezone(timedelta(hours=-7))
    start = datetime(2026, 10, 9, 0, 0, tzinfo=tz)
    lines = ["hour,service,total,errors_5xx,errors_4xx"]
    for h in range(24):
        hour = (start + timedelta(hours=h)).isoformat()
        for svc, base in (("checkout", 4200), ("search", 9100), ("payments", 2600)):
            total = base + rng.randint(-400, 400)
            e5 = rng.randint(0, 9) if svc == "checkout" else rng.randint(20, 90)
            if svc == "checkout" and h in (13, 14):
                e5 += rng.randint(120, 140)
            e4 = int(total * rng.uniform(0.004, 0.009))
            lines.append(f"{hour},{svc},{total},{e5},{e4}")
    return "\n".join(lines) + "\n"


OPS03_SLO = """{
  "service": "checkout",
  "objective": "availability",
  "target": 0.995,
  "window_hours": 24,
  "definition": "fraction of requests that did not fail"
}
"""


def solve_ops03(files: dict[str, str], mistakes: set[str]) -> dict:
    import json

    slo = json.loads(files["input/slo.json"])
    total = failed = 0
    for r in rows(files["input/requests.csv"]):
        if r["service"] != slo["service"] and "ops.scope" not in mistakes:
            continue
        total += int(r["total"])
        failed += int(r["errors_5xx"]) + (int(r["errors_4xx"]) if "ops.rate" in mistakes else 0)
    allowed = (1 - D(slo["target"])) * total
    return {"service": slo["service"],
            "availability": q(D(1) - D(failed) / D(total), "0.0001"),
            "error_budget_remaining_pct": q(100 * (1 - D(failed) / allowed), "0.1"),
            "breached": failed > allowed}


def ops03() -> Task:
    files = {"input/requests.csv": _gen_requests(), "input/slo.json": OPS03_SLO}
    truth = solve_ops03(files, set())
    path = "output/slo_report.json"
    keys = ["service", "availability", "error_budget_remaining_pct", "breached"]
    checks = json_checks(path, keys) + [
        field_check("service", "reports the SLO's service", path, lambda o: o["service"],
                    lambda t: t["service"], weight=0.5),
        field_check("availability", "availability counts only server errors as failures", path,
                    lambda o: o["availability"], lambda t: t["availability"],
                    lambda a, b: close(a, b, 0.00005), weight=1.5),
        field_check("budget", "error budget remaining, in percent", path,
                    lambda o: o["error_budget_remaining_pct"],
                    lambda t: t["error_budget_remaining_pct"], lambda a, b: close(a, b, 0.05),
                    weight=1.5),
        field_check("breached", "breach flag agrees with the budget", path,
                    lambda o: o["breached"], lambda t: t["breached"]),
    ]
    prompt = f"""\
Produce the daily SLO report for the service named in `input/slo.json` from the hourly
request counts in `input/requests.csv` (the file covers several services).

Write `{path}` with exactly these keys:
- "service": the service name
- "availability": the availability over the window (4 decimals)
- "error_budget_remaining_pct": percent of the window's error budget still unused (1 decimal;
  negative if overspent). The error budget is (1 - target) x total requests.
- "breached": true if the SLO target was missed
"""
    return Task(
        id="ops-03", domain="ops", title="Daily SLO burn report",
        blurb="Hourly counts for three services; 4xx and 5xx columns side by side.",
        prompt=prompt, files=files, deliverables=[path], truth=truth, checks=checks,
        sim=SimSpec(reqs=["verify", "shape", "arith", "ops.rate", "ops.scope"], solve=solve_ops03,
                    fmt="json", deliverable=path, numeric=["error_budget_remaining_pct"],
                    rename={"error_budget_remaining_pct": "budgetRemaining"},
                    consistency=lambda o: (o["error_budget_remaining_pct"] < 0) == bool(o["breached"])),
    )


TASKS = [ops01, ops02, ops03]
