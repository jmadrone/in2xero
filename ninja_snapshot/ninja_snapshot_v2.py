#!/usr/bin/env python3
"""
ninja_snapshot.py - read-only Invoice Ninja extract for reconciling against Xero.

Writes NOTHING, anywhere. It reads your Invoice Ninja instance over the API and
prints a month-by-month picture of invoiced revenue, cash received, and expenses,
plus AR and unpaid-bill positions as of today. It also drops a JSON file you can
send back for comparison against Xero's P&L.

Usage
-----
    export IN_BASE_URL=https://invoices.yourhost.com      # no trailing slash
    export IN_API_TOKEN=xxxxxxxx
    python3 ninja_snapshot.py --start 2025-01-01

Only needs Python 3.8+ and `requests` (pip install requests).

Why this exists: Xero already has ~19 months of bank-fed bookkeeping. Before
pushing anything into it we need to know whether those numbers are complete or
partial, and only Invoice Ninja can answer that.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

# Invoice.status_id
DRAFT, SENT, PARTIAL, PAID, CANCELLED, REVERSED = "1", "2", "3", "4", "5", "6"
LEDGER_INVOICE_STATUSES = {SENT, PARTIAL, PAID}
# Payment.status_id - only these ever reach the ledger
LEDGER_PAYMENT_STATUSES = {"4", "5", "6"}

Z = Decimal("0")


def dec(v) -> Decimal:
    if v in (None, ""):
        return Z
    return Decimal(str(v))


def money(v: Decimal) -> str:
    return f"{v.quantize(Decimal('0.01')):>14,}"


def lifecycle_of(row) -> str:
    """active / archived / deleted, read off the record itself.

    Invoice Ninja's `status=active` index filter silently drops archived rows.
    Archiving in Invoice Ninja is a UI tidying action, not a void - an archived
    invoice is still a real invoice that belongs in the ledger. So we never send
    that filter; we fetch everything and classify here.
    """
    if row.get("is_deleted"):
        return "deleted"
    a = row.get("archived_at")
    if a not in (None, 0, "0", "", False):
        return "archived"
    return "active"


class Ninja:
    def __init__(self, base: str, token: str, page_size: int, insecure: bool,
                 lifecycle: str = "active+archived"):
        self.base = base.rstrip("/")
        self.page_size = page_size
        self.lifecycle = lifecycle
        self.seen = defaultdict(lambda: defaultdict(int))
        self.s = requests.Session()
        self.s.verify = not insecure
        self.s.headers.update(
            {
                "X-API-TOKEN": token,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            }
        )

    def paginate(self, path, params=None):
        p = dict(params or {})
        # Deliberately NO status filter. Invoice Ninja's `status=active` drops
        # archived records, and archived invoices are still real invoices.
        # We classify each row ourselves - see lifecycle_of().
        p.setdefault("sort", "id|asc")
        p["per_page"] = self.page_size
        page = 1
        while True:
            p["page"] = page
            url = f"{self.base}/api/v1/{path}"  # never a trailing slash
            r = self.s.get(url, params=p, timeout=120, allow_redirects=False)
            if r.status_code in (301, 302, 307, 308):
                sys.exit(
                    f"{url} redirected to {r.headers.get('Location')!r}.\n"
                    "Check IN_BASE_URL - http vs https, or a trailing slash. "
                    "Redirects drop the auth header."
                )
            if r.status_code == 403:
                sys.exit(
                    "403 from Invoice Ninja: the API token is not valid for this company.\n"
                    "(Invoice Ninja returns 403, not 401, for a bad token.)"
                )
            if not r.ok:
                sys.exit(f"{r.status_code} {r.url}\n{r.text[:600]}")
            body = r.json()
            rows = body.get("data") or []
            for row in rows:
                state = lifecycle_of(row)
                self.seen[path][state] += 1
                if state == "deleted" and self.lifecycle != "all":
                    continue
                if state == "archived" and self.lifecycle == "active":
                    continue
                yield row
            pg = (body.get("meta") or {}).get("pagination") or {}
            if page >= int(pg.get("total_pages") or 1) or not rows:
                break
            page += 1


def month_of(d: str) -> str:
    return (d or "")[:7] or "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument(
        "--lifecycle",
        choices=["active", "active+archived", "all"],
        default="active+archived",
        help="which records count. Default includes archived: in Invoice Ninja "
             "archiving is a tidying action, not a void, so archived invoices "
             "are still real. 'all' additionally includes soft-deleted rows.",
    )
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification")
    ap.add_argument("--out", default="ninja_snapshot.json")
    a = ap.parse_args()

    base = os.environ.get("IN_BASE_URL", "").strip()
    token = os.environ.get("IN_API_TOKEN", "").strip()
    if not base or not token:
        return int(bool(sys.stderr.write("Set IN_BASE_URL and IN_API_TOKEN.\n"))) or 2

    n = Ninja(base, token, a.page_size, a.insecure, a.lifecycle)
    window = {"date_range": f"date,{a.start},{a.end}", "filter_deleted_clients": "true"}

    inv_by_month = defaultdict(lambda: {"count": 0, "total": Z, "tax": Z})
    ar_open = Z
    ar_count = 0
    status_counts = defaultdict(int)
    draft_total = Z
    currencies = set()
    inclusive_tax_count = 0
    multi_tax_lines = 0
    invoice_total_all = Z

    print("Reading invoices...", file=sys.stderr)
    for i in n.paginate("invoices", window):
        st = str(i.get("status_id") or "")
        status_counts[st] += 1
        amt = dec(i.get("amount"))
        if st == DRAFT:
            draft_total += amt
            continue
        if st in (CANCELLED, REVERSED):
            continue
        m = month_of(i.get("date"))
        inv_by_month[m]["count"] += 1
        inv_by_month[m]["total"] += amt
        inv_by_month[m]["tax"] += dec(i.get("total_taxes"))
        invoice_total_all += amt
        bal = dec(i.get("balance"))
        if bal > Z:
            ar_open += bal
            ar_count += 1
        if i.get("uses_inclusive_taxes"):
            inclusive_tax_count += 1
        for li in i.get("line_items") or []:
            if li.get("tax_name2") or li.get("tax_name3"):
                multi_tax_lines += 1
        if dec(i.get("exchange_rate")) not in (Z, Decimal("1")):
            currencies.add(str(i.get("exchange_rate")))

    pay_by_month = defaultdict(lambda: {"count": 0, "applied": Z, "refunded": Z})
    unapplied_total = Z
    credit_applications = 0
    payment_total_all = Z

    print("Reading payments...", file=sys.stderr)
    for p in n.paginate("payments", window):
        if str(p.get("status_id") or "") not in LEDGER_PAYMENT_STATUSES:
            continue
        rows = p.get("paymentables") or []
        if any(r.get("credit_id") for r in rows):
            # In Invoice Ninja, "apply a credit" is recorded as a payment. That is
            # an allocation, not cash - counting it as cash would invent money.
            credit_applications += 1
            continue
        m = month_of(p.get("date"))
        applied = dec(p.get("applied"))
        refunded = dec(p.get("refunded"))
        pay_by_month[m]["count"] += 1
        pay_by_month[m]["applied"] += applied
        pay_by_month[m]["refunded"] += refunded
        payment_total_all += applied - refunded
        unapplied_total += dec(p.get("amount")) - applied

    exp_by_month = defaultdict(lambda: {"count": 0, "total": Z})
    unpaid_expenses = Z
    unpaid_expense_count = 0
    expense_total_all = Z

    print("Reading expenses...", file=sys.stderr)
    for e in n.paginate("expenses", window):
        m = month_of(e.get("date"))
        amt = dec(e.get("amount"))
        exp_by_month[m]["count"] += 1
        exp_by_month[m]["total"] += amt
        expense_total_all += amt
        if not (e.get("payment_date") or "").strip():
            unpaid_expenses += amt
            unpaid_expense_count += 1

    credit_total = Z
    credit_count = 0
    print("Reading credits...", file=sys.stderr)
    for c in n.paginate("credits", window):
        if str(c.get("status_id") or "") == DRAFT:
            continue
        credit_count += 1
        credit_total += dec(c.get("amount"))

    client_count = sum(1 for _ in n.paginate("clients"))
    vendor_count = sum(1 for _ in n.paginate("vendors"))
    tax_rates = [
        {"name": t.get("name"), "rate": t.get("rate")} for t in n.paginate("tax_rates")
    ]
    categories = sorted({c.get("name", "") for c in n.paginate("expense_categories")})

    months = sorted(set(inv_by_month) | set(pay_by_month) | set(exp_by_month))

    print(f"\n=== Invoice Ninja snapshot  {a.start} .. {a.end} ===\n")
    print(
        f"{'month':9} {'inv#':>5} {'invoiced':>14} {'of which tax':>14} "
        f"{'cash applied':>14} {'exp#':>5} {'expenses':>14}"
    )
    print("-" * 82)
    for m in months:
        iv = inv_by_month.get(m, {"count": 0, "total": Z, "tax": Z})
        pv = pay_by_month.get(m, {"count": 0, "applied": Z, "refunded": Z})
        ev = exp_by_month.get(m, {"count": 0, "total": Z})
        print(
            f"{m:9} {iv['count']:>5} {money(iv['total'])} {money(iv['tax'])} "
            f"{money(pv['applied'] - pv['refunded'])} {ev['count']:>5} {money(ev['total'])}"
        )
    print("-" * 82)
    print(
        f"{'TOTAL':9} {sum(v['count'] for v in inv_by_month.values()):>5} "
        f"{money(invoice_total_all)} "
        f"{money(sum(v['tax'] for v in inv_by_month.values()))} "
        f"{money(payment_total_all)} "
        f"{sum(v['count'] for v in exp_by_month.values()):>5} {money(expense_total_all)}"
    )

    # A payment cannot be "applied" to an invoice that does not exist. If cash
    # applied dwarfs invoiced, records are being dropped somewhere upstream -
    # almost always archived invoices excluded by a status filter.
    if payment_total_all > invoice_total_all * 2 and payment_total_all > Z:
        print("\n" + "!" * 82)
        print("MISMATCH: cash applied far exceeds invoiced in this window.")
        print(f"  invoiced     {money(invoice_total_all).strip()}")
        print(f"  cash applied {money(payment_total_all).strip()}")
        print("Payments are applied TO invoices, so those invoices exist. If they")
        print("are not in the invoiced column, this run is not seeing all of them.")
        print("Re-run with --lifecycle all, and check the record counts below.")
        print("!" * 82)

    print("\nRecords seen, by lifecycle state")
    print(f"  (counting mode: {a.lifecycle})")
    for path in ("invoices", "payments", "expenses", "credits"):
        st = n.seen.get(path)
        if not st:
            continue
        parts = ", ".join(f"{k} {v}" for k, v in sorted(st.items()))
        print(f"  {path:20} {parts}")

    print("\nPositions as of today")
    print(f"  open AR (unpaid invoice balances)   {money(ar_open)}  across {ar_count} invoices")
    print(f"  expenses with no payment_date       {money(unpaid_expenses)}  "
          f"across {unpaid_expense_count}")
    print(f"  credits issued                      {money(credit_total)}  ({credit_count})")
    print(f"  unapplied payments (credit on acct) {money(unapplied_total)}")

    print("\nShape of the data (things that complicate a Xero import)")
    print(f"  clients                             {client_count}")
    print(f"  vendors                             {vendor_count}")
    print(f"  draft invoices (excluded)           {status_counts.get(DRAFT, 0)}"
          f"  worth {money(draft_total).strip()}")
    print(f"  cancelled / reversed (excluded)     "
          f"{status_counts.get(CANCELLED, 0) + status_counts.get(REVERSED, 0)}")
    print(f"  invoices using inclusive tax        {inclusive_tax_count}")
    print(f"  lines with 2nd/3rd tax              {multi_tax_lines}")
    print(f"  credit-application 'payments'       {credit_applications}")
    print(f"  non-1.0 exchange rates seen         {len(currencies)}")
    rate_desc = ", ".join("{}@{}%".format(t["name"], t["rate"]) for t in tax_rates)
    print(f"  tax rates defined                   {rate_desc or 'none'}")
    print(f"  expense categories                  {', '.join(c for c in categories if c) or 'none'}")

    out = {
        "window": {"start": a.start, "end": a.end},
        "months": {
            m: {
                "invoice_count": inv_by_month.get(m, {}).get("count", 0),
                "invoiced": str(inv_by_month.get(m, {}).get("total", Z)),
                "invoiced_tax": str(inv_by_month.get(m, {}).get("tax", Z)),
                "cash_applied": str(
                    pay_by_month.get(m, {}).get("applied", Z)
                    - pay_by_month.get(m, {}).get("refunded", Z)
                ),
                "expense_count": exp_by_month.get(m, {}).get("count", 0),
                "expenses": str(exp_by_month.get(m, {}).get("total", Z)),
            }
            for m in months
        },
        "totals": {
            "invoiced": str(invoice_total_all),
            "cash_applied": str(payment_total_all),
            "expenses": str(expense_total_all),
            "credits": str(credit_total),
        },
        "positions": {
            "open_ar": str(ar_open),
            "open_ar_count": ar_count,
            "unpaid_expenses": str(unpaid_expenses),
            "unpaid_expense_count": unpaid_expense_count,
            "unapplied_payments": str(unapplied_total),
        },
        "lifecycle_mode": a.lifecycle,
        "records_seen": {k: dict(v) for k, v in n.seen.items()},
        "shape": {
            "clients": client_count,
            "vendors": vendor_count,
            "invoice_status_counts": dict(status_counts),
            "draft_total": str(draft_total),
            "inclusive_tax_invoices": inclusive_tax_count,
            "multi_tax_lines": multi_tax_lines,
            "credit_applications": credit_applications,
            "tax_rates": tax_rates,
            "expense_categories": [c for c in categories if c],
        },
    }
    with open(a.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {a.out} - send that back for the Xero comparison.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
