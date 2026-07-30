"""Invoice Ninja read client.

The whole reason this file exists as its own module is the archived-record trap.
Invoice Ninja index routes accept a `status` filter whose values are lifecycle
states - active / archived / deleted. Sending `status=active` is the obvious
"give me the real records" move and it is wrong: archiving in Invoice Ninja is a
UI tidying action, not a void. In this org 318 of 327 invoices are archived. A
sync that filters them out imports 3% of the business and looks like it worked.

So: never send the filter. Fetch everything, classify each row from its own
archived_at / is_deleted fields, and drop only true soft-deletes.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from decimal import Decimal

import requests

Z = Decimal("0")

# Invoice.status_id
DRAFT, SENT, PARTIAL, PAID, CANCELLED, REVERSED = "1", "2", "3", "4", "5", "6"
# Only these carry a journal in Xero. Drafts create nothing; cancelled/reversed
# should not be restated into a rebuilt ledger.
LEDGER_INVOICE_STATUSES = {SENT, PARTIAL, PAID}
# Payment.status_id - pending/failed payments are not cash
LEDGER_PAYMENT_STATUSES = {"4", "5", "6"}


def dec(v) -> Decimal:
    if v in (None, "", False):
        return Z
    return Decimal(str(v))


def lifecycle_of(row) -> str:
    if row.get("is_deleted"):
        return "deleted"
    a = row.get("archived_at")
    if a not in (None, 0, "0", "", False):
        return "archived"
    return "active"


class NinjaError(Exception):
    pass


class Ninja:
    def __init__(self, cfg):
        self.base = cfg.base_url
        self.page_size = cfg.page_size
        self.seen = defaultdict(lambda: defaultdict(int))
        self.s = requests.Session()
        self.s.verify = cfg.verify_tls
        self.s.headers.update(
            {
                "X-API-TOKEN": cfg.api_token,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            }
        )

    def paginate(self, path, params=None, include_archived=True):
        p = dict(params or {})
        # No `status` filter. See module docstring.
        p.setdefault("sort", "id|asc")
        p["per_page"] = self.page_size
        page = 1
        while True:
            p["page"] = page
            url = f"{self.base}/api/v1/{path}"   # never a trailing slash
            r = self.s.get(url, params=p, timeout=120, allow_redirects=False)

            if r.status_code in (301, 302, 307, 308):
                raise NinjaError(
                    f"{url} redirected to {r.headers.get('Location')!r}. Check base_url "
                    "- http vs https, or a trailing slash. Redirects drop the auth header."
                )
            if r.status_code == 403:
                raise NinjaError(
                    "403 from Invoice Ninja: the API token is not valid for this company. "
                    "(Invoice Ninja returns 403, not 401, for a bad token.)"
                )
            if not r.ok:
                raise NinjaError(f"{r.status_code} {r.url}\n{r.text[:600]}")

            body = r.json()
            rows = body.get("data") or []
            for row in rows:
                state = lifecycle_of(row)
                self.seen[path][state] += 1
                if state == "deleted":
                    continue
                if state == "archived" and not include_archived:
                    continue
                yield row

            pg = (body.get("meta") or {}).get("pagination") or {}
            if page >= int(pg.get("total_pages") or 1) or not rows:
                break
            page += 1

    # ---- typed helpers -------------------------------------------------

    def invoices(self, start, end):
        """Ledger-bearing invoices in the window, oldest first."""
        for i in self.paginate("invoices", {"date_range": f"{start},{end}"}):
            if str(i.get("status_id") or "") not in LEDGER_INVOICE_STATUSES:
                continue
            d = (i.get("date") or "")[:10]
            if not d or d < start or d > end:
                continue
            yield i

    def payments(self, start, end):
        for p in self.paginate("payments", {"date_range": f"{start},{end}"}):
            if str(p.get("status_id") or "") not in LEDGER_PAYMENT_STATUSES:
                continue
            d = (p.get("date") or "")[:10]
            if not d or d < start or d > end:
                continue
            yield p

    def diagnose_invoice(self, invoice_id, start, end):
        """Why is this invoice not in the batch? Called only for refusals.

        'not in Xero yet' reads like an ordering problem, and usually is not. An
        invoice can be missing because it predates the window, because it was
        deleted in Invoice Ninja, or because it is a draft that carries no
        journal. Each needs a different response from a human, so name which.
        """
        try:
            r = self.s.get(f"{self.base}/api/v1/invoices/{invoice_id}",
                           timeout=60, allow_redirects=False)
        except requests.RequestException:
            return "could not reach Invoice Ninja to check why"
        if r.status_code == 404:
            return "no such invoice in Invoice Ninja - the allocation is dangling"
        if not r.ok:
            return f"lookup returned {r.status_code}"

        inv = (r.json() or {}).get("data") or {}
        state = lifecycle_of(inv)
        num = inv.get("number") or invoice_id
        d = (inv.get("date") or "")[:10]

        if state == "deleted":
            return f"invoice {num} is DELETED in Invoice Ninja; the payment is orphaned"
        if str(inv.get("status_id") or "") not in LEDGER_INVOICE_STATUSES:
            return f"invoice {num} is a draft/cancelled in Invoice Ninja - carries no ledger"
        if d and d < start:
            return (f"invoice {num} is dated {d}, BEFORE the {start} window - the payment "
                    "collects old AR that was never imported")
        if d and d > end:
            return f"invoice {num} is dated {d}, after the {end} window"
        return (f"invoice {num} ({d}) is in scope and valid in Invoice Ninja. If it is "
                "also in Xero, the crosswalk simply does not know its id - check that "
                "sync.crosswalk_path points where you expect, then re-run")

    def clients_by_id(self):
        return {str(c.get("id")): c for c in self.paginate("clients")}

    def company_settings(self):
        for c in self.paginate("companies"):
            return c.get("settings") or {}
        return {}

    def report_lifecycle(self, out=sys.stderr):
        for path, states in self.seen.items():
            parts = ", ".join(f"{k} {v}" for k, v in sorted(states.items()))
            print(f"  {path:12} {parts}", file=out)
