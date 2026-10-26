"""Finance domain: reconciliation work where practitioner knowledge decides the answer.

Recurring domain knowledge the library can carry between these tasks:
  - several partial payments can settle one invoice (aggregate per key before comparing)
  - credit notes / refunds / credit memos are negative and must keep their sign
  - items with no counterpart on the other side are reported, never silently dropped
  - convert currencies before aggregating or ranking
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .base import Check, D, SimSpec, Task, close, field_check, json_checks, load_csv, q, rows

# --------------------------------------------------------------------------- fin-01

FIN01_INVOICES = """\
invoice_id,customer,amount,issued
INV-1001,Acme Corp,1200.00,2026-08-01
INV-1002,Birch LLC,450.50,2026-08-03
INV-1003,Cobalt Inc,980.00,2026-08-05
INV-1004,Acme Corp,310.25,2026-08-09
INV-1005,Dune Partners,2200.00,2026-08-12
INV-1006,Birch LLC,-150.00,2026-08-15
INV-1007,Cobalt Inc,75.00,2026-08-20
"""

FIN01_PAYMENTS = """\
payment_id,invoice_ref,amount,received
PAY-501,INV-1001,1200.00,2026-08-20
PAY-502,INV-1002,200.00,2026-08-21
PAY-503,INV-1003,980.00,2026-08-22
PAY-504,INV-1002,250.50,2026-08-28
PAY-505,INV-1005,1000.00,2026-08-30
PAY-506,INV-9999,500.00,2026-08-31
PAY-507,INV-1004,310.20,2026-09-01
"""


def solve_fin01(files: dict[str, str], mistakes: set[str]) -> dict:
    invoices = rows(files["input/invoices.csv"])
    payments = rows(files["input/payments.csv"])
    ids = {r["invoice_id"] for r in invoices}
    paid: dict[str, Decimal] = defaultdict(Decimal)
    seen: set[str] = set()
    for p in payments:
        ref = p["invoice_ref"]
        if ref not in ids:
            continue
        if "fin.partial" in mistakes and ref in seen:
            continue  # the classic slip: one payment per invoice
        seen.add(ref)
        paid[ref] += D(p["amount"])
    matched, outstanding = [], {}
    for r in invoices:
        amount = D(r["amount"])
        if "fin.signed" in mistakes:
            amount = abs(amount)
        remaining = amount - paid[r["invoice_id"]]
        if remaining == 0:
            matched.append(r["invoice_id"])
        else:
            outstanding[r["invoice_id"]] = q(remaining)
    unapplied = [] if "fin.orphans" in mistakes else sorted(
        p["payment_id"] for p in payments if p["invoice_ref"] not in ids)
    return {"matched": sorted(matched), "outstanding": outstanding,
            "unapplied_payments": unapplied,
            "total_outstanding": q(sum(D(v) for v in outstanding.values()))}


def fin01() -> Task:
    files = {"input/invoices.csv": FIN01_INVOICES, "input/payments.csv": FIN01_PAYMENTS}
    truth = solve_fin01(files, set())
    path = "output/reconciliation.json"
    keys = ["matched", "outstanding", "unapplied_payments", "total_outstanding"]

    def outstanding_ok(got, want):
        return isinstance(got, dict) and set(got) == set(want) and all(
            close(got[k], want[k]) for k in want)

    checks = json_checks(path, keys) + [
        field_check("matched", "fully settled invoices (incl. multi-payment ones)", path,
                    lambda o: sorted(o["matched"]), lambda t: t["matched"]),
        field_check("outstanding", "remaining balance per open invoice", path,
                    lambda o: o["outstanding"], lambda t: t["outstanding"], outstanding_ok),
        field_check("credit_note", "credit note INV-1006 carried as -150.00", path,
                    lambda o: o["outstanding"].get("INV-1006"),
                    lambda t: t["outstanding"]["INV-1006"], close),
        field_check("unapplied", "payment with no matching invoice is reported", path,
                    lambda o: sorted(o["unapplied_payments"]), lambda t: t["unapplied_payments"]),
        field_check("total", "total outstanding is exact to the cent", path,
                    lambda o: o["total_outstanding"], lambda t: t["total_outstanding"], close,
                    weight=1.5),
    ]
    prompt = f"""\
Reconcile August receivables. `input/invoices.csv` lists invoices issued; `input/payments.csv`
lists payments received, each referencing an invoice.

Write `{path}` with exactly these keys:
- "matched": sorted list of invoice ids whose payments fully settle them
- "outstanding": object mapping every invoice id that is not fully settled to its remaining
  balance (amount minus payments applied), as a number with 2 decimals
- "unapplied_payments": sorted list of payment ids that could not be applied to any invoice
- "total_outstanding": the sum of all remaining balances, 2 decimals
"""
    return Task(
        id="fin-01", domain="finance", title="Invoice ↔ payment reconciliation",
        blurb="Partial payments, a credit note and an orphan payment hide in two small CSVs.",
        prompt=prompt, files=files, deliverables=[path], truth=truth, checks=checks,
        sim=SimSpec(reqs=["verify", "shape", "arith", "fin.partial", "fin.signed", "fin.orphans"],
                    solve=solve_fin01, fmt="json", deliverable=path,
                    numeric=["total_outstanding"],
                    rename={"unapplied_payments": "unappliedPayments"},
                    consistency=lambda o: close(o["total_outstanding"],
                                                sum(o["outstanding"].values()))),
    )


# --------------------------------------------------------------------------- fin-02

FIN02_EXPENSES = """\
expense_id,date,category,description,amount,currency
E-01,2026-09-02,travel,Flight SFO-JFK,612.40,USD
E-02,2026-09-03,meals,Client dinner,184.20,USD
E-03,2026-09-04,lodging,Hotel 2 nights,540.00,USD
E-04,2026-09-06,travel,Train Paris-Lyon,96.00,EUR
E-05,2026-09-07,meals,Team lunch,73.50,EUR
E-06,2026-09-08,travel,Flight refund (cancelled leg),-210.00,USD
E-07,2026-09-10,software,Annual license,299.00,USD
E-08,2026-09-11,lodging,Hotel Lyon (3 nights),612.00,EUR
E-09,2026-09-12,meals,Refund for overcharge,-12.30,USD
"""

FIN02_FX = """{
  "EUR_USD": 1.0850,
  "as_of": "2026-09-30",
  "note": "multiply an EUR amount by EUR_USD to get USD"
}
"""


def solve_fin02(files: dict[str, str], mistakes: set[str]) -> dict:
    import json

    rate = D(json.loads(files["input/fx.json"])["EUR_USD"])
    by_cat: dict[str, Decimal] = defaultdict(Decimal)
    largest, largest_usd = None, Decimal("-1e18")
    for r in rows(files["input/expenses.csv"]):
        amount = D(r["amount"])
        if "fin.signed" in mistakes:
            amount = abs(amount)
        usd = amount * rate if (r["currency"] == "EUR" and "fin.fx" not in mistakes) else amount
        by_cat[r["category"]] += usd
        if usd > largest_usd:
            largest, largest_usd = r["expense_id"], usd
    return {"by_category": {k: q(v) for k, v in sorted(by_cat.items())},
            "net_total_usd": q(sum(by_cat.values())),
            "largest_expense_id": largest}


def fin02() -> Task:
    files = {"input/expenses.csv": FIN02_EXPENSES, "input/fx.json": FIN02_FX}
    truth = solve_fin02(files, set())
    path = "output/summary.json"
    keys = ["by_category", "net_total_usd", "largest_expense_id"]
    cat = lambda name: (lambda o: o["by_category"][name])  # noqa: E731
    checks = json_checks(path, keys) + [
        field_check("travel", "travel total nets the refund", path, cat("travel"),
                    lambda t: t["by_category"]["travel"], close),
        field_check("lodging", "lodging total converts EUR to USD", path, cat("lodging"),
                    lambda t: t["by_category"]["lodging"], close),
        field_check("meals", "meals total (FX + refund)", path, cat("meals"),
                    lambda t: t["by_category"]["meals"], close),
        field_check("software", "software total", path, cat("software"),
                    lambda t: t["by_category"]["software"], close, weight=0.5),
        field_check("net_total", "net total in USD exact to the cent", path,
                    lambda o: o["net_total_usd"], lambda t: t["net_total_usd"], close, weight=1.5),
        field_check("largest", "largest single expense compared in USD", path,
                    lambda o: o["largest_expense_id"], lambda t: t["largest_expense_id"]),
    ]
    prompt = f"""\
Prepare the September expense summary from `input/expenses.csv`. Amounts are in the currency
given on each line; `input/fx.json` has the conversion rate. Report everything in USD.

Write `{path}` with exactly these keys:
- "by_category": object mapping each category to its total in USD (2 decimals)
- "net_total_usd": the total of all expenses in USD (2 decimals)
- "largest_expense_id": the id of the single largest expense
"""
    return Task(
        id="fin-02", domain="finance", title="Expense summary with FX and refunds",
        blurb="Mixed-currency expenses with refunds; the biggest line item depends on FX.",
        prompt=prompt, files=files, deliverables=[path], truth=truth, checks=checks,
        sim=SimSpec(reqs=["verify", "shape", "arith", "fin.signed", "fin.fx"],
                    solve=solve_fin02, fmt="json", deliverable=path, numeric=["net_total_usd"],
                    rename={"by_category": "byCategory"},
                    consistency=lambda o: close(o["net_total_usd"],
                                                sum(o["by_category"].values()), 0.02)),
    )


# --------------------------------------------------------------------------- fin-03

FIN03_LEDGER = """\
invoice_id,vendor,amount,posted
V-301,Northwind Supply,1450.00,2026-09-03
V-302,Northwind Supply,380.75,2026-09-07
V-303,Northwind Supply,92.10,2026-09-12
V-305,Northwind Supply,2210.00,2026-09-19
V-306,Northwind Supply,-120.00,2026-09-22
V-307,Northwind Supply,415.00,2026-09-26
"""

FIN03_STATEMENT = """\
invoice,amount_due
V-301,1450.00
V-302,398.75
V-303,92.01
V-304,640.00
V-305,2210.00
V-306,-120.00
"""

FIN03_HEADER = ["invoice_id", "ledger_amount", "statement_amount", "difference"]


def solve_fin03(files: dict[str, str], mistakes: set[str]) -> tuple[list[str], list[list[str]]]:
    ledger = {r["invoice_id"]: D(r["amount"]) for r in rows(files["input/ap_ledger.csv"])}
    stmt = {r["invoice"]: D(r["amount_due"]) for r in rows(files["input/vendor_statement.csv"])}
    ids = sorted(set(ledger) | set(stmt)) if "fin.orphans" not in mistakes else sorted(
        set(ledger) & set(stmt))
    out = []
    for i in ids:
        a, b = ledger.get(i), stmt.get(i)
        if a is not None and b is not None and a == b:
            continue
        diff = (b or Decimal(0)) - (a or Decimal(0))
        if "fin.signed" in mistakes:
            diff = abs(diff)
        out.append([i, "" if a is None else f"{a:.2f}", "" if b is None else f"{b:.2f}",
                    f"{diff:.2f}"])
    return FIN03_HEADER, out


def fin03() -> Task:
    files = {"input/ap_ledger.csv": FIN03_LEDGER, "input/vendor_statement.csv": FIN03_STATEMENT}
    header, truth_rows = solve_fin03(files, set())
    path = "output/discrepancies.csv"

    def table(o):
        return load_csv(o, path)

    def body(o):
        return {r[0]: r for r in table(o)[1:] if r}

    checks = [
        Check("valid_csv", "rectangular CSV with a header row",
              lambda o, t: len({len(r) for r in table(o) if r}) == 1 and len(table(o)) > 1),
        Check("exact_header", f"header is exactly {','.join(FIN03_HEADER)}",
              lambda o, t: table(o)[0] == FIN03_HEADER),
        Check("rows", "every mismatched or one-sided invoice appears exactly once",
              lambda o, t: sorted(body(o)) == sorted(r[0] for r in t["rows"]), 1.5),
        Check("one_sided", "invoices missing from either side are included",
              lambda o, t: "V-304" in body(o) and "V-307" in body(o)),
        Check("signed_difference", "difference is statement minus ledger, signed",
              lambda o, t: close(body(o)["V-303"][3], -0.09) and close(body(o)["V-307"][3], -415)),
        Check("amounts", "differences exact to the cent",
              lambda o, t: all(close(body(o)[r[0]][3], r[3]) for r in t["rows"])),
        Check("sorted", "rows sorted by invoice_id",
              lambda o, t: [r[0] for r in table(o)[1:] if r] == sorted(r[0] for r in table(o)[1:] if r),
              0.5),
    ]
    prompt = f"""\
Northwind Supply sent their statement (`input/vendor_statement.csv`). Compare it with our
accounts-payable ledger (`input/ap_ledger.csv`) and list every discrepancy.

Write `{path}` with exactly this header:
    {",".join(FIN03_HEADER)}
One row per invoice whose amounts disagree or that appears on only one side (leave the
missing side's amount empty). difference = statement_amount - ledger_amount, treating a
missing amount as 0. Amounts with 2 decimals. Rows sorted by invoice_id.
"""
    return Task(
        id="fin-03", domain="finance", title="Vendor statement vs AP ledger",
        blurb="Two sources with different column names, one-sided invoices and a credit memo.",
        prompt=prompt, files=files, deliverables=[path],
        truth={"header": header, "rows": truth_rows}, checks=checks,
        sim=SimSpec(reqs=["verify", "shape", "arith", "fin.orphans", "fin.signed"],
                    solve=solve_fin03, fmt="csv", deliverable=path, numeric=["difference"],
                    rename={"statement_amount": "Statement Amount"}),
    )


TASKS = [fin01, fin02, fin03]
