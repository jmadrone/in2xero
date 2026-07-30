"""Xero write client: auth, rate limiting, batching, idempotency.

Auth modes
----------
custom_connection  client_credentials, US$5/month, one org. No refresh token and
                   no 60-day cliff. Correct choice for an unattended cron job.
auth_code          standard flow. The refresh token ROTATES on every use - a crash
                   between receiving a new one and persisting it bricks the
                   connection. Implemented, but persist-before-use is enforced.

Rate limits: 60 calls/minute and 1,000/day on Starter, 5,000/day on Core. The
daily counter is the one that ends runs, so the client watches
X-DayLimit-Remaining and stops cleanly above a floor rather than dying mid-batch.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
import uuid

import requests

IDENTITY = "https://identity.xero.com/connect/token"
API = "https://api.xero.com/api.xro/2.0"
CONNECTIONS = "https://api.xero.com/connections"
# Each round strips the elements Xero named and retries the rest. Distinct
# failure reasons surface one round at a time, so allow several.
MAX_VALIDATION_ROUNDS = 6
# Custom Connections use GRANULAR scopes. There is no accounting.transactions in
# the Custom Connection scope list at all - that is the aggregate scope used by
# standard OAuth apps, and asking for it is rejected as invalid_scope even on an
# app with every box ticked. The granular equivalents:
#
#   accounting.invoices      create and read invoices     (was accounting.transactions)
#   accounting.payments      create payments              (was accounting.transactions)
#   accounting.contacts      create customer contacts
#   accounting.settings.read chart of accounts, tax rates (read is enough)
#
# Least privilege: settings is read-only here, and banktransactions/manualjournals
# are deliberately absent - the Stripe fee journal is posted by hand, not by this.
DEFAULT_SCOPES = (
    "accounting.invoices accounting.payments accounting.contacts "
    "accounting.settings.read accounting.banktransactions.read"
)


def xero_date(v) -> str:
    """Xero returns /Date(1767225600000+0000)/ in JSON, NOT ISO 8601.

    Slicing the first 10 characters of that yields '/Date(1767' - which is how
    this was displayed wrong the first time. Only the *reporting* endpoints and
    the XML API use ISO; the JSON accounting API uses the legacy .NET format.
    """
    if not v:
        return ""
    s = str(v)
    m = re.match(r"/Date\((-?\d+)", s)
    if m:
        try:
            return datetime.fromtimestamp(int(m.group(1)) / 1000, timezone.utc).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return s
    return s[:10]


class XeroError(Exception):
    pass


def elem_ref(item) -> str:
    """Stable label for one element of a batch, for matching Xero's error echo."""
    for k in ("Reference", "InvoiceNumber", "ContactNumber", "Name"):
        v = (item or {}).get(k)
        if v:
            return str(v)
    return ""


def parse_validation(body) -> dict:
    """Pull {ref: [messages]} out of a Xero ValidationException response.

    Xero echoes every element back, flags the bad ones with HasErrors, and hangs
    the reasons off ValidationErrors - both on the element and on individual line
    items. The raw response is enormous and the messages are buried; without this
    the user sees a wall of JSON truncated before the one line that matters.
    """
    rejected = {}
    for el in (body or {}).get("Elements", []) or []:
        msgs = [m.get("Message", "").strip()
                for m in (el.get("ValidationErrors") or []) if m.get("Message")]
        for li in (el.get("LineItems") or []):
            for m in (li.get("ValidationErrors") or []):
                if m.get("Message"):
                    msgs.append("line item: " + m["Message"].strip())
        if not msgs and not el.get("HasErrors"):
            continue
        ref = elem_ref(el) or el.get("InvoiceID") or "?"
        if msgs:
            rejected[ref] = msgs
    return rejected


class ValidationRejected(XeroError):
    """Xero refused elements of a batch. Carries {ref: [messages]}."""

    def __init__(self, rejected, raw=""):
        self.rejected = rejected or {}
        self.raw = raw
        if self.rejected:
            n = len(self.rejected)
            first = next(iter(self.rejected.items()))
            super().__init__(
                f"Xero rejected {n} element(s); the whole batch was refused. "
                f"First: {first[0]} - {'; '.join(first[1])}"
            )
        else:
            super().__init__(f"Xero validation failure: {raw[:400]}")


class RateLimitExhausted(XeroError):
    """Daily quota floor reached. Not a failure - resume tomorrow."""


class Xero:
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.s = requests.Session()
        self.token = None
        self.tenant_id = cfg.tenant_id
        self.calls = 0
        self.day_remaining = None
        self.scopes = (getattr(cfg, "scopes", "") or DEFAULT_SCOPES).strip()

    # ---- auth ----------------------------------------------------------

    def authenticate(self):
        if self.cfg.auth_mode == "custom_connection":
            try:
                r = self.s.post(
                    IDENTITY,
                    data={"grant_type": "client_credentials", "scope": self.scopes},
                    auth=(self.cfg.client_id, self.cfg.client_secret),
                    timeout=60,
                )
            except requests.RequestException as e:
                raise XeroError(f"cannot reach identity.xero.com: {e}") from None
            if not r.ok:
                raise XeroError(self._explain_auth_failure(r))
            self.token = r.json()["access_token"]
        else:
            path = self.cfg.refresh_token_path
            if not os.path.exists(path):
                raise XeroError(
                    f"no refresh token at {path}. Run the auth-code flow once to seed it, "
                    "or switch to auth_mode: custom_connection."
                )
            with open(path) as fh:
                refresh = fh.read().strip()
            r = self.s.post(
                IDENTITY,
                data={"grant_type": "refresh_token", "refresh_token": refresh},
                auth=(self.cfg.client_id, self.cfg.client_secret),
                timeout=60,
            )
            if not r.ok:
                raise XeroError(
                    f"refresh failed: {r.status_code} {r.text[:400]}\n"
                    "If this says invalid_grant the token has expired or already rotated; "
                    "re-run the auth-code flow."
                )
            body = r.json()
            # Persist the rotated token BEFORE using the access token for anything.
            # Losing this write is what bricks auth-code setups.
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(body["refresh_token"])
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            self.token = body["access_token"]

        if not self.tenant_id:
            self.tenant_id = self._discover_tenant()
        return self.tenant_id

    # The complete Custom Connection scope list as Xero offers it, accounting
    # section only. Payroll/projects/assets are omitted - nothing here touches them.
    PROBE_SCOPES = [
        "accounting.invoices",
        "accounting.invoices.read",
        "accounting.payments",
        "accounting.payments.read",
        "accounting.contacts",
        "accounting.contacts.read",
        "accounting.settings",
        "accounting.settings.read",
        "accounting.banktransactions",
        "accounting.banktransactions.read",
        "accounting.manualjournals",
        "accounting.manualjournals.read",
        "accounting.attachments",
        "accounting.attachments.read",
        "accounting.budgets.read",
        "accounting.reports.balancesheet.read",
        "accounting.reports.profitandloss.read",
        "accounting.reports.trialbalance.read",
        "accounting.reports.aged.read",
        # Not in the Custom Connection list at all - probed to prove the point.
        "accounting.transactions",
    ]

    # What this tool cannot work without, and why.
    REQUIRED_SCOPES = {
        "accounting.invoices": "create invoices",
        "accounting.payments": "settle payments against them",
        "accounting.contacts": "create customer contacts",
    }
    # Either variant satisfies the chart-of-accounts read.
    SETTINGS_SCOPES = ("accounting.settings.read", "accounting.settings")

    def probe_scope(self, scope):
        """Ask for exactly one scope. Returns (accepted, detail)."""
        try:
            r = self.s.post(
                IDENTITY,
                data={"grant_type": "client_credentials", "scope": scope},
                auth=(self.cfg.client_id, self.cfg.client_secret),
                timeout=60,
            )
        except requests.RequestException as e:
            return False, f"network: {e}"
        if r.ok:
            return True, ""
        try:
            j = r.json()
            return False, j.get("error") or r.text[:120]
        except ValueError:
            return False, f"{r.status_code} {r.text[:120]}"

    def _explain_auth_failure(self, r):
        """Xero's auth errors are terse. Translate the ones that actually happen."""
        body = r.text[:400]
        try:
            err = r.json().get("error", "")
        except ValueError:
            err = ""

        if err == "invalid_scope":
            return (
                "Xero rejected the requested scopes.\n"
                f"  requested: {self.scopes}\n\n"
                "Two things cause this, in order of likelihood:\n\n"
                "1. The app is not a Custom Connection. client_credentials ONLY works\n"
                "   with that app type. At developer.xero.com/myapps, a Custom Connection\n"
                "   is labelled as such - a 'Web app' or 'Mobile or desktop app' cannot\n"
                "   use this grant no matter how the scopes are set. If yours is a web\n"
                "   app, either recreate it as a Custom Connection (US$5/month) or set\n"
                "   auth_mode: auth_code in config.\n\n"
                "2. The Custom Connection exists but does not have these scopes ticked.\n"
                "   Open the app, check its scope list, and set xero.scopes in config to\n"
                "   match exactly what is enabled there.\n\n"
                f"  raw: {body}"
            )
        if err == "invalid_client":
            return (
                "Xero rejected the client id or secret.\n"
                "  Check XERO_CLIENT_ID and XERO_CLIENT_SECRET are set in this shell and\n"
                "  belong to the same app. A regenerated secret invalidates the old one\n"
                "  immediately.\n"
                f"  raw: {body}"
            )
        return f"custom connection auth failed: {r.status_code} {body}"

    def _discover_tenant(self):
        r = self.s.get(
            CONNECTIONS,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            timeout=60,
        )
        if not r.ok:
            raise XeroError(f"could not list connections: {r.status_code} {r.text[:300]}")
        conns = r.json()
        if not conns:
            raise XeroError("this Xero app is not connected to any organisation")
        if len(conns) > 1:
            names = ", ".join(f"{c.get('tenantName')} ({c.get('tenantId')})" for c in conns)
            raise XeroError(f"multiple orgs connected - set xero.tenant_id. Found: {names}")
        return conns[0]["tenantId"]

    # ---- transport -----------------------------------------------------

    def _headers(self, idempotency_key=None):
        h = {
            "Authorization": f"Bearer {self.token}",
            "Xero-tenant-id": self.tenant_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    def _track(self, resp):
        self.calls += 1
        rem = resp.headers.get("X-DayLimit-Remaining")
        if rem is not None:
            try:
                self.day_remaining = int(rem)
            except ValueError:
                pass
        if self.day_remaining is not None and self.day_remaining <= self.cfg.daily_call_floor:
            raise RateLimitExhausted(
                f"daily Xero quota nearly spent ({self.day_remaining} calls left). "
                "Stopping cleanly; re-run tomorrow and the crosswalk resumes where it left off."
            )

    def _request(self, method, path, payload=None, params=None, idempotency_key=None):
        url = f"{API}/{path}"
        for attempt in range(6):
            try:
                r = self.s.request(
                    method,
                    url,
                    headers=self._headers(idempotency_key),
                    data=json.dumps(payload) if payload is not None else None,
                    params=params,
                    timeout=120,
                )
            except requests.RequestException as e:
                if attempt == 5:
                    raise XeroError(f"network error talking to Xero: {e}") from None
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After") or 60)
                time.sleep(min(wait, 120))
                continue
            if r.status_code == 401:
                self.authenticate()
                continue
            self._track(r)
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if not r.ok:
                # A 400 ValidationException echoes every element back with the
                # reasons buried inside. Dumping raw text truncates before the
                # one line that matters, so parse it into refs -> messages.
                if r.status_code == 400:
                    try:
                        body = r.json()
                    except ValueError:
                        body = None
                    if body and body.get("Type") == "ValidationException":
                        raise ValidationRejected(parse_validation(body), r.text)
                raise XeroError(f"{r.status_code} {method} {path}\n{r.text[:900]}")
            return r.json()
        raise XeroError(f"{method} {path} still failing after retries")

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, payload, idempotency_key=None):
        if self.dry_run:
            return {"_dry_run": True, "payload": payload}
        return self._request("POST", path, payload=payload, idempotency_key=idempotency_key)

    # ---- reference data ------------------------------------------------

    def accounts(self):
        """Code -> account. Bank accounts are usually absent: Xero gives them no Code."""
        return {a["Code"]: a for a in self.get("Accounts").get("Accounts", []) if a.get("Code")}

    def all_accounts(self):
        return self.get("Accounts").get("Accounts", [])

    def invoices_in_window(self, start, end, itype="ACCREC"):
        """Every ACCREC invoice already in Xero for the window, any status.

        This is the duplicate guard's input, and DRAFTS ARE THE POINT. A draft
        invoice creates no journal, so it appears in no report - not the P&L, not
        the balance sheet, not aged receivables. An org can hold hundreds of them
        and look empty from every angle except this endpoint.
        """
        def dt(s):
            y, m, d = s.split("-")
            return f"DateTime({int(y)},{int(m)},{int(d)})"

        where = f'Type=="{itype}"&&Date>={dt(start)}&&Date<={dt(end)}'
        out, page = [], 1
        while True:
            body = self.get("Invoices", params={"where": where, "page": page, "order": "Date"})
            rows = body.get("Invoices", []) or []
            out.extend(rows)
            if len(rows) < 100:
                break
            page += 1
        return out

    def bank_transactions(self, start, end):
        """Bank transactions in the window. Needed to explain a clearing balance:
        a Payment is not the only thing that can hit a BANK account."""
        def dt(v):
            y, m, d = v.split("-")
            return f"DateTime({int(y)},{int(m)},{int(d)})"

        where = f"Date>={dt(start)}&&Date<={dt(end)}"
        out, page = [], 1
        while True:
            body = self.get("BankTransactions", params={"where": where, "page": page})
            rows = body.get("BankTransactions", []) or []
            out.extend(rows)
            if len(rows) < 100:
                break
            page += 1
        return out

    def contacts_by_number(self):
        """ContactNumber -> ContactID, for adopting contacts this tool already made.

        The crosswalk lives in a local sqlite file. Run the tool from a different
        directory and it starts empty - but Xero still holds the contacts, and
        re-creating them would fail on the unique-name rule. Adopt instead.
        """
        out, page = {}, 1
        while True:
            body = self.get("Contacts", params={"page": page})
            rows = body.get("Contacts", []) or []
            for c in rows:
                num = str(c.get("ContactNumber") or "").strip()
                if num:
                    out[num] = c.get("ContactID")
            if len(rows) < 100:
                break
            page += 1
        return out

    def payments_in_window(self, start, end):
        """Every Payment in Xero for the window. Read-only audit input."""
        def dt(v):
            y, m, d = v.split("-")
            return f"DateTime({int(y)},{int(m)},{int(d)})"

        where = f"Date>={dt(start)}&&Date<={dt(end)}"
        out, page = [], 1
        while True:
            body = self.get("Payments", params={"where": where, "page": page})
            rows = body.get("Payments", []) or []
            out.extend(rows)
            if len(rows) < 100:
                break
            page += 1
        return out

    def bank_accounts(self):
        """Bank-type accounts, which is where payments can settle.

        Identified by AccountID because Xero does not surface a Code for them.
        """
        return [a for a in self.all_accounts() if a.get("Type") == "BANK"]

    def tax_rates(self):
        # TaxType codes (TAX001, ...) are per-organisation and carry no portable
        # meaning. Always resolved at runtime, never hardcoded.
        return {t["Name"]: t["TaxType"] for t in self.get("TaxRates").get("TaxRates", [])}

    # ---- batched writes ------------------------------------------------

    def post_batch(self, path, wrapper, items, key_prefix, on_rejected=None,
                   on_created=None, retry_on_validation=True):
        """POST items in batches. Returns the created objects, flattened.

        Idempotency-Key is derived deterministically from the batch contents, so a
        retry after a network timeout cannot create the same records twice.

        Xero rejects an ENTIRE batch when any one element fails validation - the
        other 49 are not saved either. So on a ValidationException the failing
        elements are identified, reported via on_rejected(ref, messages), and the
        remainder is retried once. One bad invoice must not block the other 240.
        """
        out = []
        size = max(1, self.cfg.batch_size)
        for i in range(0, len(items), size):
            chunk = items[i : i + size]
            attempt = 0
            while chunk:
                attempt += 1
                key = f"{key_prefix}-{i // size:04d}-{attempt}-" + uuid.uuid5(
                    uuid.NAMESPACE_URL, json.dumps(chunk, sort_keys=True, default=str)
                ).hex[:16]
                try:
                    body = self.post(path, {wrapper: chunk}, idempotency_key=key)
                except ValidationRejected as e:
                    if (not retry_on_validation or attempt >= MAX_VALIDATION_ROUNDS
                            or not e.rejected):
                        raise
                    bad = set()
                    for ref, msgs in e.rejected.items():
                        bad.add(ref)
                        if on_rejected:
                            on_rejected(ref, msgs)
                    keep = [c for c in chunk if elem_ref(c) not in bad]
                    if len(keep) == len(chunk):
                        raise          # could not match any - do not spin
                    chunk = keep
                    continue
                made = chunk if self.dry_run else body.get(wrapper, [])
                if on_created:
                    on_created(made)      # persist NOW, before the next batch
                out.extend(made)
                break
        return out
