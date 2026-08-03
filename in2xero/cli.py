"""Command line: preflight, auth, backfill, sync, report."""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from . import __version__, config as cfgmod
from .crosswalk import Crosswalk
from .ninja import Ninja, NinjaError, dec, Z
from .transform import (
    GONE,
    apply_against_remaining,
    PAYABLE,
    Refused,
    SETTLED,
    already_in_xero,
    build_contact,
    build_invoice,
    build_payments,
    index_existing,
    is_credit_application,
    make_tax_resolver,
)
from .xero import (RateLimitExhausted, ValidationRejected, Xero, XeroError,
                    xero_date)


def _window(cfg):
    return cfg.sync.start_date, (cfg.sync.end_date or date.today().isoformat())


def _say(msg):
    print(msg, file=sys.stderr, flush=True)


def adopt_invoices(cw, x, invoices, start, end, quiet=False):
    """Make sure every in-scope invoice has its Xero id in the crosswalk.

    The payments step cannot function without this map, and it used to be built
    only as a side effect of the invoices step - so `--only payments` always ran
    against an empty crosswalk and refused everything with a misleading
    "ordering problem". Adoption belongs to whoever needs the ids, not to one step.
    """
    have = cw.known("invoice")
    live = x.invoices_in_window(start, end)
    numbers, refs = index_existing(live)

    # What each invoice still owes, according to Xero. This is the only
    # trustworthy answer to "does this payment still need to be applied?" - the
    # crosswalk records intent, Xero records fact, and they diverge whenever a
    # post fails.
    due_by_xero_id = {i.get("InvoiceID"): dec(i.get("AmountDue")) for i in live}

    need = [i for i in invoices if str(i.get("id")) not in have]
    pairs, blocked = [], {}
    for inv in need:
        hit = already_in_xero(inv, numbers, refs)
        if not hit or not hit.get("id"):
            continue
        if hit["status"] in PAYABLE or hit["status"] in SETTLED:
            pairs.append((str(inv.get("id")), hit["id"]))
        else:
            blocked[str(inv.get("id"))] = hit["status"]
    if pairs:
        cw.put_many("invoice", pairs)
        if not quiet:
            _say(f"  adopted {len(pairs)} invoice id(s) from Xero into the crosswalk")
    if blocked and not quiet:
        _say(f"  {len(blocked)} invoice(s) in Xero cannot take a payment "
             f"(status {', '.join(sorted(set(blocked.values())))})")
    ids = cw.known("invoice")
    by_ninja = {nid: due_by_xero_id.get(xid, Z) for nid, xid in ids.items()}
    return ids, by_ninja, dict(due_by_xero_id)


# ---- preflight ---------------------------------------------------------

def cmd_preflight(cfg, args):
    """Read-only. Proves both ends work and shows what a run would touch."""
    start, end = _window(cfg)
    _say(f"in2xero {__version__}  window {start} .. {end}\n")

    _say("Invoice Ninja")
    n = Ninja(cfg.ninja)
    invoices = list(n.invoices(start, end))
    payments = list(n.payments(start, end))
    total = sum(dec(i.get("amount")) for i in invoices)
    _say(f"  ledger invoices     {len(invoices):>6}   {total:>14,.2f}")
    _say(f"  ledger payments     {len(payments):>6}")
    credit_apps = sum(1 for p in payments if is_credit_application(p))
    if credit_apps:
        _say(f"  credit applications {credit_apps:>6}   (routed away from cash)")
    client_ids = {str(i.get("client_id")) for i in invoices if i.get("client_id")}
    _say(f"  clients with invoices{len(client_ids):>5}   (contacts to create)")
    _say("  records by lifecycle:")
    n.report_lifecycle()

    _say("\nXero")
    x = Xero(cfg.xero, dry_run=True)
    try:
        tenant = x.authenticate()
    except XeroError as e:
        _say(f"  AUTH FAILED: {e}")
        return 1
    _say(f"  authenticated, tenant {tenant}")
    accounts = x.accounts()
    _say(f"  chart of accounts   {len(accounts)} codes")

    ok = True
    for label, code, required in (
        ("sales", cfg.xero.sales_account_code, True),
        ("rounding", cfg.xero.rounding_account_code, False),
    ):
        if not code:
            if required:
                _say(f"  {label:9} account   NOT SET in config")
                ok = False
            continue
        a = accounts.get(code)
        if not a:
            _say(f"  {label:9} account   code {code} does not exist in Xero")
            ok = False
            continue
        _say(f"  {label:9} account   {code}  {a.get('Name')}  [{a.get('Type')}]")

    # Clearing is checked by GUID, not code - Xero gives bank accounts no code.
    ref = cfgmod.clearing_ref(cfg)
    banks = {a.get("AccountID"): a for a in x.bank_accounts()}
    if not ref:
        _say("  clearing  account   NOT SET. Run `in2xero accounts` for the GUID.")
        ok = False
    elif "AccountID" in ref:
        a = banks.get(ref["AccountID"])
        if not a:
            _say(f"  clearing  account   {ref['AccountID']} is not a BANK account in this org")
            _say("      Run `in2xero accounts` to see the real list.")
            ok = False
        else:
            _say(f"  clearing  account   {a.get('Name')}  [BANK]")
            _say("      payments settle here, NOT into the fed bank account")
    else:
        a = accounts.get(ref["Code"])
        if not a or a.get("Type") != "BANK":
            _say(f"  clearing  account   code {ref['Code']} is missing or not BANK type")
            ok = False
        else:
            _say(f"  clearing  account   {ref['Code']}  {a.get('Name')}  [BANK]")

    rates = x.tax_rates()
    _say(f"  tax rates           {len(rates)}  (tax_mode={cfg.sync.tax_mode})")

    est = (len(client_ids) + len(invoices) + len(payments)) / max(1, cfg.xero.batch_size)
    _say(f"\n  estimated API calls  ~{int(est) + 4}  (batch size {cfg.xero.batch_size})")
    _say("\npreflight " + ("OK" if ok else "FAILED - fix the above before backfilling"))
    return 0 if ok else 1


# ---- auth --------------------------------------------------------------

def cmd_existing(cfg, args):
    """What is ALREADY in Xero for the window, including drafts.

    Drafts create no journal, so they show up in no report. This is the only way
    to see them without clicking through the Sales screen.
    """
    start, end = _window(cfg)
    x = Xero(cfg.xero)
    x.authenticate()
    rows = x.invoices_in_window(start, end)
    if not rows:
        _say(f"no ACCREC invoices already in Xero for {start} .. {end}")
        return 0

    for i in rows:
        i["_d"] = xero_date(i.get("Date"))

    by_status = {}
    for i in rows:
        by_status.setdefault(i.get("Status") or "?", []).append(i)

    _say(f"{len(rows)} ACCREC invoices already in Xero, {start} .. {end}\n")
    for st, group in sorted(by_status.items()):
        total = sum(float(i.get("Total") or 0) for i in group)
        span = sorted(g["_d"] for g in group if g["_d"])
        _say(f"  {st:<12} {len(group):>4}   {total:>12,.2f}"
             + (f"   {span[0]} .. {span[-1]}" if span else ""))
        if st in ("DRAFT", "SUBMITTED"):
            _say("               ^ creates no journal - invisible in the P&L, balance")
            _say("                 sheet and aged receivables, but very much present")

    # When did they start appearing? A sharp start date points at an automation
    # being switched on, which is the thing worth finding and stopping.
    drafts = by_status.get("DRAFT", [])
    if drafts:
        months = {}
        for i in drafts:
            if i["_d"]:
                months.setdefault(i["_d"][:7], []).append(i)
        _say("\ndrafts by month:")
        for m, group in sorted(months.items()):
            total = sum(float(i.get("Total") or 0) for i in group)
            _say(f"  {m}   {len(group):>3} invoices  {total:>12,.2f}")
        _say("\nA sharp start month usually means an automation was switched on then.")
        _say("Find and stop it before deleting anything, or the drafts come back.")

    # ACCPAY = supplier bills. Nothing here ever creates one, so any that exist
    # came from somewhere else - and a misconfigured sync that picks ACCPAY
    # instead of ACCREC invents accounts payable out of your own sales.
    bills = x.invoices_in_window(start, end, itype="ACCPAY")
    if bills:
        total = sum(float(b.get("Total") or 0) for b in bills)
        _say(f"\n!! {len(bills)} ACCPAY BILLS also present, {total:,.2f}")
        _say("   in2xero never creates bills. These came from something else.")
        for b in sorted(bills, key=lambda r: xero_date(r.get("Date")))[:20]:
            _say(f"   {xero_date(b.get('Date')):<12} {(b.get('InvoiceNumber') or '-'):<14}"
                 f" {(b.get('Status') or ''):<10} {float(b.get('Total') or 0):>10,.2f}"
                 f"  {(b.get('Contact') or {}).get('Name', '')}")

    if args.list:
        _say("")
        for i in sorted(rows, key=lambda r: (r["_d"], r.get("InvoiceNumber") or "")):
            _say(f"  {i['_d']:<12} {(i.get('InvoiceNumber') or '-'):<14}"
                 f" {(i.get('Status') or ''):<10} {float(i.get('Total') or 0):>10,.2f}"
                 f"  {(i.get('Contact') or {}).get('Name', '')}")
    return 0


def cmd_audit(cfg, args):
    """READ-ONLY. What is actually in Xero, and does it double-count anything?

    Exists because a batch endpoint that partially applies before failing can
    leave the same payment recorded twice, and no amount of reasoning about the
    code substitutes for counting what is really there.
    """
    start, end = _window(cfg)
    x = Xero(cfg.xero)
    x.authenticate()

    _say(f"AUDIT (read-only)  {start} .. {end}\n")

    inv = x.invoices_in_window(start, end)
    by_status = {}
    for i in inv:
        by_status.setdefault(i.get("Status") or "?", []).append(i)
    _say("invoices in Xero:")
    for st, g in sorted(by_status.items()):
        _say(f"  {st:<12} {len(g):>4}   {sum(float(r.get('Total') or 0) for r in g):>12,.2f}")

    # All time, deliberately. A windowed payment query silently hides anything
    # dated outside it and makes the totals look reconciled when they are not.
    pays = x.payments_in_window("2020-01-01", date.today().isoformat())
    live = [p for p in pays if (p.get("Status") or "AUTHORISED") != "DELETED"]
    total = sum(float(p.get("Amount") or 0) for p in live)
    _say(f"\npayments in Xero: {len(live)}   {total:,.2f}")

    # A single Invoice Ninja payment split across two invoices LEGITIMATELY becomes
    # two Xero Payments sharing one Reference. Matching on reference alone therefore
    # flags every split payment as a duplicate - a false positive that, acted on,
    # deletes a real allocation. A true duplicate is the same reference applied to
    # the SAME invoice twice.
    seen = {}
    for p in live:
        ref = str(p.get("Reference") or "").strip()
        if ref.startswith("IN-P-"):
            key = (ref, (p.get("Invoice") or {}).get("InvoiceID"))
            seen.setdefault(key, []).append(p)
    refs = {k[0] for k in seen}
    dupes = {k: g for k, g in seen.items() if len(g) > 1}
    splits = len(seen) - len(refs)
    _say(f"  carrying an in2xero reference: {len(refs)}"
         + (f"  ({splits} split across more than one invoice)" if splits else ""))

    # The identity that actually settles the question: every payment must be
    # accounted for by an invoice balance. If these agree, nothing was applied
    # twice, whatever the reference pattern looks like.
    paid_on_invoices = sum(float(i.get("AmountPaid") or 0) for i in inv)
    _say(f"\nreconciliation:")
    _say(f"  payments recorded in Xero   {total:>14,.2f}")
    _say(f"  AmountPaid across invoices  {paid_on_invoices:>14,.2f}")
    drift = total - paid_on_invoices
    if abs(drift) < 0.01:
        _say("  agrees exactly - no cash is double-counted")
    else:
        _say(f"  DISAGREES by {drift:,.2f} - investigate before posting anything else")

    if dupes:
        extra = sum(sum(float(q.get("Amount") or 0) for q in g[1:]) for g in dupes.values())
        _say(f"\n!! {len(dupes)} payment(s) applied TWICE TO THE SAME INVOICE, {extra:,.2f}")
        for (ref, iid), g in sorted(dupes.items(), key=lambda kv: -len(kv[1]))[:25]:
            _say(f"   {ref} -> invoice {iid}  x{len(g)}")
            for q in g:
                _say(f"        PaymentID {q.get('PaymentID')}  "
                     f"{float(q.get('Amount') or 0):,.2f}  {xero_date(q.get('Date'))}")
    else:
        _say("\nno payment applied twice to the same invoice")

    ov = [i for i in inv if float(i.get("AmountPaid") or 0) > float(i.get("Total") or 0) + 0.005]
    if ov:
        _say(f"\n!! {len(ov)} invoice(s) PAID MORE than their total:")
        for i in ov[:25]:
            _say(f"   {i.get('InvoiceNumber')}  total {float(i.get('Total') or 0):,.2f}"
                 f"  paid {float(i.get('AmountPaid') or 0):,.2f}")

    unpaid = [i for i in inv
              if i.get("Status") == "AUTHORISED" and float(i.get("AmountDue") or 0) > 0.005]
    _say(f"\nstill awaiting payment: {len(unpaid)}   "
         f"{sum(float(i.get('AmountDue') or 0) for i in unpaid):,.2f}")
    return 0


def cmd_clearing(cfg, args):
    """READ-ONLY. Explain the clearing account balance, line by line.

    A BANK account can be moved by Payments, by bank transactions, by manual
    journals and by a conversion balance. Reasoning about which is guesswork;
    this counts them.
    """
    ref = cfgmod.clearing_ref(cfg)
    if not ref:
        _say("no clearing account configured")
        return 1
    x = Xero(cfg.xero)
    x.authenticate()

    target_id, target_code = ref.get("AccountID"), ref.get("Code")
    for a in x.bank_accounts():
        if (target_id and a.get("AccountID") == target_id) or \
           (target_code and a.get("Code") == target_code):
            target_id, target_code = a.get("AccountID"), a.get("Code")
            _say(f"clearing account: {a.get('Name')}  code {a.get('Code') or '-'}")
            break

    wide_start, wide_end = "2020-01-01", date.today().isoformat()

    pays = [p for p in x.payments_in_window(wide_start, wide_end)
            if (p.get("Status") or "AUTHORISED") != "DELETED"]
    mine, other = [], []
    for p in pays:
        acc = p.get("Account") or {}
        if acc.get("AccountID") == target_id or (target_code and acc.get("Code") == target_code):
            (mine if str(p.get("Reference") or "").startswith("IN-P-") else other).append(p)

    def tot(rows, field="Amount"):
        return sum(float(r.get(field) or 0) for r in rows)

    _say("\nPayments hitting this account:")
    _say(f"  posted by in2xero (IN-P-*)   {len(mine):>4}   {tot(mine):>14,.2f}")
    if other:
        _say(f"  from somewhere else          {len(other):>4}   {tot(other):>14,.2f}")
        _say("     ^ not created by this tool. Likely candidates below.")
        for p in sorted(other, key=lambda r: xero_date(r.get("Date")))[:20]:
            _say(f"       {xero_date(p.get('Date'))}  {float(p.get('Amount') or 0):>10,.2f}"
                 f"  ref {p.get('Reference') or '-'}"
                 f"  {(p.get('Invoice') or {}).get('InvoiceNumber') or ''}")

    try:
        bts = [b for b in x.bank_transactions(wide_start, wide_end)
               if (b.get("BankAccount") or {}).get("AccountID") == target_id
               and b.get("Status") != "DELETED"]
    except XeroError as e:
        _say(f"\ncould not read bank transactions: {e}")
        bts = None

    if bts is not None:
        recv = [b for b in bts if b.get("Type", "").startswith("RECEIVE")]
        spend = [b for b in bts if b.get("Type", "").startswith("SPEND")]
        _say("\nBank transactions in this account:")
        _say(f"  money in  (RECEIVE)          {len(recv):>4}   {tot(recv, 'Total'):>14,.2f}")
        _say(f"  money out (SPEND)            {len(spend):>4}   {tot(spend, 'Total'):>14,.2f}")
        for b in sorted(bts, key=lambda r: xero_date(r.get("Date")))[:20]:
            _say(f"       {xero_date(b.get('Date'))}  {b.get('Type'):<14}"
                 f"  {float(b.get('Total') or 0):>10,.2f}"
                 f"  {(b.get('Contact') or {}).get('Name') or ''}")

        movement = tot(mine) + tot(other) + tot(recv, "Total") - tot(spend, "Total")
        _say(f"\naccounted-for movement         {movement:>14,.2f}")

    # Where do these payments actually sit in time? A windowed query and an
    # all-time query disagreeing means either dates outside the sync window or
    # duplicates - and those need opposite responses.
    everything = mine + other
    by_year = {}
    for q in everything:
        by_year.setdefault((xero_date(q.get("Date")) or "????")[:4], []).append(q)
    _say("\npayments by year:")
    for yr, g in sorted(by_year.items()):
        flag = ""
        if yr < cfg.sync.start_date[:4]:
            flag = "  <-- BEFORE the sync window; this tool should not have posted these"
        _say(f"  {yr}   {len(g):>4}   {tot(g):>14,.2f}{flag}")

    # Duplicate test across ALL time, not just the window: same Invoice Ninja
    # payment applied to the same invoice more than once.
    seen = {}
    for q in everything:
        r = str(q.get("Reference") or "").strip()
        if r.startswith("IN-P-"):
            seen.setdefault((r, (q.get("Invoice") or {}).get("InvoiceID")), []).append(q)
    dupes = {k: g for k, g in seen.items() if len(g) > 1}
    if dupes:
        extra = sum(sum(float(z.get("Amount") or 0) for z in g[1:]) for g in dupes.values())
        _say("\n   To clean up: open each invoice in Xero, remove the surplus payment")
        _say("   (keep ONE of each pair), then re-run this audit.")
        _say(f"\n!! {len(dupes)} payment(s) applied TWICE TO THE SAME INVOICE"
             f" - {extra:,.2f} double-counted")
        _say("   Delete the surplus PaymentIDs in Xero before re-coding anything.")
        for (r, iid), g in sorted(dupes.items(), key=lambda kv: -len(kv[1]))[:30]:
            _say(f"   {r}  x{len(g)}  invoice {iid}")
            for z in g:
                _say(f"        PaymentID {z.get('PaymentID')}  "
                     f"{float(z.get('Amount') or 0):>10,.2f}  {xero_date(z.get('Date'))}")
    else:
        _say("\nno payment applied twice to the same invoice (checked across all dates)")
        _say("The balance is real: gross invoice payments with nothing netted against")
        _say("them yet. Re-coding the bank deposits is what brings it down.")
    return 0


def cmd_accounts(cfg, args):
    """List accounts, with bank accounts shown by GUID.

    Exists because Xero's chart of accounts gives bank-type accounts no Code, so
    there is nothing to copy out of the UI for the clearing account.
    """
    x = Xero(cfg.xero)
    x.authenticate()
    banks = x.bank_accounts()
    _say("BANK accounts - payments settle into one of these.")
    _say("Xero gives them no Code, so use the GUID as xero.clearing_account_id:\n")
    if not banks:
        _say("  (none found - create one: Accounting -> Chart of accounts -> Add Bank Account)")
    for a in banks:
        _say(f"  {a.get('Name')}")
        _say(f"      clearing_account_id: {a.get('AccountID')}")
        _say(f"      status {a.get('Status')}"
             + (f", code {a['Code']}" if a.get("Code") else ", no code (normal)"))
    if args.all:
        _say("\nAll other accounts:\n")
        for a in sorted(x.all_accounts(), key=lambda r: (r.get("Type") or "", r.get("Code") or "")):
            if a.get("Type") == "BANK":
                continue
            _say(f"  {(a.get('Code') or '----'):>6}  {(a.get('Type') or ''):<12} {a.get('Name')}")
    return 0


def cmd_auth(cfg, args):
    x = Xero(cfg.xero)

    if args.probe_scopes:
        _say("Asking Xero for each scope on its own, to find which ones this app\n"
             "actually holds. Read/write and .read are different strings.\n")
        accepted, rejected, other = [], [], []
        for sc in Xero.PROBE_SCOPES:
            ok, detail = x.probe_scope(sc)
            if ok:
                accepted.append(sc)
                _say(f"  OK       {sc}")
            elif detail == "invalid_scope":
                rejected.append(sc)
                _say(f"  no       {sc}")
            else:
                other.append((sc, detail))
                _say(f"  ERROR    {sc}  ({detail})")

        _say("")
        if not accepted and not rejected:
            _say("Every request errored before scope validation. That is not a scope")
            _say("problem - check the client id and secret first.")
            return 1
        if not accepted:
            _say("Xero accepted NO scopes at all.")
            _say("A Custom Connection that holds no scopes usually means the connection")
            _say("was created but never completed, or its subscription is not active.")
            _say("Open the app at developer.xero.com/myapps and check for a 'Connect'")
            _say("step still pending, and that billing is live.")
            return 1

        have = set(accepted)
        missing = {s: why for s, why in Xero.REQUIRED_SCOPES.items() if s not in have}
        settings_ok = any(s in have for s in Xero.SETTINGS_SCOPES)

        if missing or not settings_ok:
            _say("MISSING - the app needs these ticked before anything can post:")
            for s, why in sorted(missing.items()):
                _say(f"  {s:<32} to {why}")
            if not settings_ok:
                _say(f"  {'accounting.settings.read':<32} to read the chart of accounts")
            _say("")
            _say("developer.xero.com/myapps -> this app -> tick them -> RECONNECT.")
            _say("A scope change does not take effect until the connection is")
            _say("re-authorised; the client id and secret stay the same.")
            return 1

        _say("All required scopes held. Nothing to change - leave scopes blank in")
        _say("config and the default set will match.")
        extra = have - set(Xero.REQUIRED_SCOPES) - set(Xero.SETTINGS_SCOPES)
        if extra:
            _say("")
            _say("Held but unused by this tool: " + " ".join(sorted(extra)))
            _say("Worth unticking - this credential can write to the books.")
        return 0

    tenant = x.authenticate()
    _say(f"authenticated. tenant_id = {tenant}")
    _say("Pin this in config as xero.tenant_id to skip discovery on every run.")
    return 0


# ---- backfill ----------------------------------------------------------

def cmd_backfill(cfg, args):
    dry = cfg.sync.dry_run or args.dry_run
    if not dry:
        cfgmod.require_posting_accounts(cfg)

    start, end = _window(cfg)
    run_start = datetime.utcnow().isoformat(timespec="seconds")
    _say(f"in2xero {__version__}  backfill {start} .. {end}"
         + ("  [DRY RUN - nothing will be posted]" if dry else ""))
    _say(f"crosswalk: {cfg.sync.crosswalk_path}")

    n = Ninja(cfg.ninja)
    x = Xero(cfg.xero, dry_run=dry)
    # A dry run must not require Xero credentials. Building and reconciling every
    # document offline is exactly what you want to do BEFORE Xero is set up.
    if not dry:
        x.authenticate()
    cw = Crosswalk(cfg.sync.crosswalk_path)

    steps = cfg.sync.steps
    if getattr(args, "only", None):
        steps = [s.strip() for s in args.only.split(",") if s.strip()]
        _say(f"running only: {', '.join(steps)}")
    tax_resolve = make_tax_resolver(cfg.sync.tax_mode, x.tax_rates() if not dry else {})

    try:
        invoices = list(n.invoices(start, end))
        wanted_clients = {str(i.get("client_id")) for i in invoices if i.get("client_id")}

        # -- contacts ---------------------------------------------------
        if "contacts" in steps:
            cw.mark_run_start("contacts", run_start)
            clients = n.clients_by_id()
            # Only clients that actually carry an invoice. This org has 1,080
            # clients against ~60 with billing; creating all of them would clutter
            # Xero permanently for no gain.
            todo = [
                clients[cid] for cid in sorted(wanted_clients)
                if cid in clients and not cw.get("contact", cid)
            ]
            _say(f"contacts: {len(todo)} to create ({len(wanted_clients)} in scope)")
            if todo:
                payload = [build_contact(c) for c in todo]

                def _con_rejected(ref, msgs, _cw=cw):
                    _cw.refuse("contact", ref or "?", "Xero rejected: " + "; ".join(msgs))
                    _say(f"  REJECTED contact {ref}: {'; '.join(msgs)}")

                created = x.post_batch("Contacts", "Contacts", payload, "contact",
                                       on_rejected=_con_rejected)
                if not dry:
                    by_number = {c.get("ContactNumber"): c.get("ContactID") for c in created}
                    cw.put_many("contact", [
                        (c.get("id"), by_number.get(f"IN-C-{c.get('id')}"))
                        for c in todo if by_number.get(f"IN-C-{c.get('id')}")
                    ])

        contact_ids = cw.known("contact")
        if not dry and "invoices" in steps:
            # Adopt contacts already in Xero. Without this, running from a fresh
            # crosswalk refuses every invoice with "client has no Xero contact
            # yet" while the contact sits in Xero the whole time.
            missing = {str(i.get("client_id")) for i in invoices
                       if str(i.get("client_id")) not in contact_ids}
            if missing:
                xc = x.contacts_by_number()
                pairs = []
                for cid in missing:
                    got = xc.get(f"IN-C-{cid}")
                    if got:
                        pairs.append((cid, got))
                if pairs:
                    cw.put_many("contact", pairs)
                    contact_ids = cw.known("contact")
                    _say(f"  adopted {len(pairs)} existing Xero contact(s)")

        # -- invoices ---------------------------------------------------
        if "invoices" in steps:
            cw.mark_run_start("invoices", run_start)

            # Duplicate guard. The crosswalk only knows what THIS tool posted; it
            # cannot know about invoices put there by hand, by another sync, or by
            # a previous attempt. Drafts are the dangerous case - they show up in
            # no report, so an org can look empty and hold hundreds.
            numbers, refs = {}, {}
            if not dry:
                pre = x.invoices_in_window(start, end)
                numbers, refs = index_existing(pre)
                if pre:
                    drafts = sum(1 for i in pre if i.get("Status") in ("DRAFT", "SUBMITTED"))
                    _say(f"  {len(pre)} invoices already in Xero for this window"
                         + (f" ({drafts} of them drafts)" if drafts else ""))

            todo, payload = [], []
            adopted, blocked_by_draft = 0, 0
            for inv in invoices:
                nid = str(inv.get("id"))
                if cw.get("invoice", nid):
                    continue
                clash = already_in_xero(inv, numbers, refs)
                if clash and clash["status"] in GONE:
                    clash = None          # deleted/voided is not a duplicate
                if clash:
                    # An existing invoice is not just a duplicate to skip - its
                    # Xero id is the only thing that lets payments settle against
                    # it. Adopt it into the crosswalk. Without this, an invoice
                    # posted by an earlier interrupted run is stranded: present in
                    # Xero, unknown to the tool, and its payment refused forever.
                    if clash["status"] in PAYABLE and clash["id"]:
                        cw.put("invoice", nid, clash["id"],
                               note=f"adopted: {clash['matched_on']}")
                        adopted += 1
                    elif clash["status"] in SETTLED and clash["id"]:
                        cw.put("invoice", nid, clash["id"],
                               note=f"adopted, already PAID: {clash['matched_on']}")
                        adopted += 1
                    elif clash["status"] in ("DRAFT", "SUBMITTED"):
                        blocked_by_draft += 1
                        cw.refuse("invoice", nid,
                                  clash["reason"] + " - a draft cannot take a payment; "
                                  "delete the draft in Xero, then re-run")
                    else:
                        cw.refuse("invoice", nid, clash["reason"])
                    continue
                cid = contact_ids.get(str(inv.get("client_id")))
                if not cid and not dry:
                    cw.refuse("invoice", nid, "client has no Xero contact yet")
                    continue
                try:
                    payload.append(build_invoice(
                        inv, cid or "DRY-RUN", cfg.xero.sales_account_code,
                        tax_resolve, cfg.xero.rounding_account_code,
                    ))
                    todo.append(inv)
                except Refused as e:
                    cw.refuse("invoice", nid, str(e))
                    _say(f"  REFUSED invoice {inv.get('number')}: {e}")
            _say(f"invoices: {len(todo)} to post, {len(invoices) - len(todo)} skipped/known")
            if adopted:
                _say(f"  adopted {adopted} invoice(s) already in Xero into the crosswalk")
                _say("  (their payments can now settle against them)")
            if blocked_by_draft:
                _say(f"  {blocked_by_draft} BLOCKED by an existing DRAFT in Xero.")
                _say("  Xero cannot apply a payment to a draft. Delete those drafts,")
                _say("  then re-run - the invoice and its payment will both go in.")
            if payload:
                def _inv_rejected(ref, msgs, _cw=cw):
                    nid = ref[3:] if ref.startswith("IN-") else ref
                    _cw.refuse("invoice", nid, "Xero rejected: " + "; ".join(msgs))
                    _say(f"  REJECTED invoice {ref}: {'; '.join(msgs)}")

                # Record each batch AS IT LANDS. Writing the crosswalk only after
                # every batch means an exception on batch 5 loses all record of
                # batches 1-4 - which is exactly how 229 invoices ended up in Xero
                # with nothing in the crosswalk pointing at them.
                def _inv_created(objs, _cw=cw, _dry=dry):
                    if _dry:
                        return
                    pairs = []
                    for c in objs:
                        ref = str(c.get("Reference") or "")
                        if ref.startswith("IN-") and c.get("InvoiceID"):
                            pairs.append((ref[3:], c.get("InvoiceID")))
                    if pairs:
                        _cw.put_many("invoice", pairs)

                created = x.post_batch("Invoices", "Invoices", payload, "invoice",
                                       on_rejected=_inv_rejected, on_created=_inv_created)

        invoice_ids = cw.known("invoice")
        outstanding, due_by_xero_id = {}, {}
        if "payments" in steps and not dry:
            invoice_ids, outstanding, due_by_xero_id = adopt_invoices(
                cw, x, invoices, start, end)
        # Invoices settled in a previous run must not be paid again.
        paid_already = cw.notes_matching("invoice", "already PAID")

        # -- payments ---------------------------------------------------
        if "payments" in steps:
            cw.mark_run_start("payments", run_start)
            payload, done = [], []
            skipped_known, skipped_settled, skipped_present = 0, 0, 0
            remaining = dict(due_by_xero_id)

            # Which (payment, invoice) pairs does Xero ALREADY hold? Invoices have
            # had a duplicate guard since v2.2; payments never did, and that is how
            # the same payment got applied twice - a run posted it, died before
            # recording it, and the next run had no way to know. The invoice still
            # showed a balance because it was only part-paid, so the balance check
            # could not catch it either. Only Xero's own payment list can.
            present, paid_on = set(), {}
            if not dry:
                for q in x.payments_in_window("2020-01-01", date.today().isoformat()):
                    if (q.get("Status") or "AUTHORISED") == "DELETED":
                        continue
                    iid = (q.get("Invoice") or {}).get("InvoiceID")
                    paid_on.setdefault(iid, []).append(q)
                    r = str(q.get("Reference") or "")
                    if r.startswith("IN-P-"):
                        present.add((r, iid))
            if present:
                _say(f"  {len(present)} payment allocation(s) already in Xero")
            for p in n.payments(start, end):
                nid = str(p.get("id"))
                allocs = p.get("paymentables") or []

                # Xero first. If every invoice this payment targets already shows a
                # zero balance, the money is in - regardless of what the crosswalk
                # thinks. This is what makes a re-run safe: Xero, not local state,
                # decides whether there is anything left to settle.
                if not dry and allocs and all(
                    str(a.get("invoice_id")) in outstanding
                    and outstanding[str(a.get("invoice_id"))] <= dec("0.005")
                    for a in allocs
                ):
                    skipped_settled += 1
                    cw.put("payment", nid, "settled", note="already settled in Xero")
                    continue

                if cw.get("payment", nid):
                    # Recorded as done locally, but Xero still shows a balance.
                    # Pre-3.6 runs marked rejected payments as posted, so this is
                    # not trustworthy on its own - say so instead of skipping mutely.
                    skipped_known += 1
                    continue

                if any(str(a.get("invoice_id")) in paid_already for a in allocs):
                    cw.refuse("payment", nid,
                              "its invoice was already PAID in Xero before this run")
                    continue
                try:
                    built = build_payments(
                        p,
                        invoice_ids if not dry else {
                            str(k): "DRY" for k in [i.get("id") for i in invoices]
                        },
                        cfgmod.clearing_ref(cfg) or {"AccountID": "DRY-RUN-CLEARING"},
                    )
                    # Xero's AmountDue was read ONCE, before this run posted
                    # anything. Two payments against the same invoice both looked
                    # affordable against that stale snapshot, and the second was
                    # rejected for exceeding the balance - taking its whole batch
                    # down with it. Decrement as we go.
                    # Drop anything Xero already holds for this exact
                    # (payment, invoice) pair before touching the balance maths.
                    fresh = [b for b in built
                             if (b.get("Reference"),
                                 (b.get("Invoice") or {}).get("InvoiceID")) not in present]
                    if not fresh:
                        skipped_present += 1
                        cw.put("payment", nid, "present",
                               note="already applied in Xero")
                        continue
                    keep, over = apply_against_remaining(fresh, remaining)
                    if over:
                        o = over[0]
                        excess = o["amount"] - o["outstanding"]
                        iid = (o["allocation"].get("Invoice") or {}).get("InvoiceID")
                        prior = paid_on.get(iid) or []
                        # Name the actual situation. A cent of overpayment and an
                        # invoice that is already settled are different problems
                        # and want different answers from a human.
                        if prior and o["outstanding"] <= dec("1.00"):
                            why = (f"Xero already shows "
                                   f"{sum(dec(z.get('Amount')) for z in prior)} paid on "
                                   f"this invoice across {len(prior)} payment(s) "
                                   f"({', '.join(str(z.get('Reference') or '?') for z in prior[:3])})"
                                   " - this allocation looks like a duplicate of one of them")
                        elif excess <= dec(str(args.cap_overpayment or "0")):
                            why = None      # handled below by capping
                        elif excess <= dec("1.00"):
                            why = (f"overpaid by {excess} - the customer paid slightly more "
                                   "than the invoice. Re-run with --cap-overpayment 1.00 to "
                                   "allocate what fits and leave the rest")
                        else:
                            why = (f"exceeds the outstanding balance by {excess}. Invoice "
                                   "Ninja and Xero genuinely disagree here - look at this "
                                   "invoice by hand")
                        if why is not None:
                            cw.refuse("payment", nid,
                                      f"allocation {o['amount']} vs {o['outstanding']} "
                                      f"outstanding: {why}")
                            _say(f"  REFUSED payment {nid}: {o['amount']} > "
                                 f"{o['outstanding']} outstanding ({excess} over)")
                            continue
                        # Cap: allocate exactly what is left, never more.
                        capped = dict(o["allocation"])
                        capped["Amount"] = str(o["outstanding"])
                        remaining[iid] = dec(0)
                        keep.append(capped)
                        _say(f"  CAPPED payment {nid}: {o['amount']} -> "
                             f"{o['outstanding']} (overpaid by {excess})")
                    payload.extend(keep)
                    done.append(nid)
                except Refused as e:
                    reason = str(e)
                    # "not in Xero yet" is ambiguous and the obvious reading
                    # (ordering) is usually wrong. Ask Ninja what really happened.
                    miss = getattr(e, "missing_invoice", None)
                    if miss:
                        reason = f"{reason}\n      -> {n.diagnose_invoice(miss, start, end)}"
                    cw.refuse("payment", nid, reason)
                    _say(f"  REFUSED payment {nid}: {reason}")
            if skipped_present:
                _say(f"  {skipped_present} payment(s) skipped - already applied in Xero")
            if skipped_settled:
                _say(f"  {skipped_settled} payment(s) skipped - their invoices already "
                     "show a zero balance in Xero")
            if skipped_known:
                _say(f"  {skipped_known} payment(s) SKIPPED by the crosswalk while their "
                     "invoice still owes money.")
                _say("  These were probably marked posted by a pre-3.6 run that Xero then "
                     "rejected.")
                _say("  Clear them and retry:  in2xero forget payments")
            _say(f"payments: {len(payload)} allocations from {len(done)} payments")
            if payload:
                rejected_ids = set()

                def _pay_rejected(ref, msgs, _cw=cw, _bad=rejected_ids):
                    nid = ref[5:] if str(ref).startswith("IN-P-") else str(ref)
                    _bad.add(nid)
                    _cw.refuse("payment", nid, "Xero rejected: " + "; ".join(msgs))
                    _say(f"  REJECTED payment {ref}: {'; '.join(msgs)}")

                # NO auto-retry here. Xero's Payments endpoint may apply some
                # allocations before failing the batch; re-sending would settle
                # the same cash twice. Report and stop instead.
                def _pay_created(objs, _cw=cw, _dry=dry):
                    # Record each batch AS IT LANDS. Recording only after every
                    # batch means a failure on the last one loses all record of the
                    # earlier ones - and an unrecorded payment gets posted again on
                    # the next run. That is exactly how cash got applied twice.
                    if _dry:
                        return
                    pairs = []
                    for c in objs:
                        r = str(c.get("Reference") or "")
                        if r.startswith("IN-P-"):
                            pairs.append((r[5:], c.get("PaymentID") or "posted"))
                    if pairs:
                        _cw.put_many("payment", pairs)

                x.post_batch("Payments", "Payments", payload, "payment",
                             on_rejected=_pay_rejected, retry_on_validation=False,
                             on_created=_pay_created)
                if not dry:
                    ok = [d for d in done if str(d) not in rejected_ids]
                    if len(ok) != len(done):
                        _say(f"  {len(done) - len(ok)} payment(s) rejected - left open "
                             "for a later run, not marked posted")
                    cw.put_many("payment", [(d, "posted") for d in ok
                                            if not cw.get("payment", d)])

    except RateLimitExhausted as e:
        _say(f"\nSTOPPED CLEANLY: {e}")
        cw.close()
        return 2
    except ValidationRejected as e:
        _say("\nXERO REJECTED ONE BATCH - that batch was not saved.")
        _say("Batches are all-or-nothing individually, but EARLIER BATCHES IN THIS RUN")
        _say("WERE SAVED. Run `in2xero audit` to see the real state before assuming")
        _say("nothing happened.\n")
        by_msg = {}
        for ref, msgs in e.rejected.items():
            by_msg.setdefault(" ; ".join(msgs), []).append(ref)
        for msg, refs in sorted(by_msg.items(), key=lambda kv: -len(kv[1])):
            _say(f"  {len(refs):>4}x  {msg}")
            _say(f"        e.g. {', '.join(refs[:5])}")
        _say("\nRe-run: the failing elements are now recorded as refusals and will be")
        _say("skipped, so the rest of the batch can go through.")
        cw.close()
        return 1
    except (NinjaError, XeroError) as e:
        _say(f"\nERROR: {e}")
        cw.close()
        return 1

    _say(f"\nXero API calls used: {x.calls}"
         + (f"  (day remaining {x.day_remaining})" if x.day_remaining is not None else ""))
    counts = ", ".join(f"{k}={v}" for k, v in sorted(cw.counts().items()))
    _say("crosswalk: " + (counts or "nothing recorded"))
    refs = cw.refusals()
    if refs:
        _say(f"\n{len(refs)} refusals - nothing was posted for these:")
        for kind, nid, reason, _ in refs[:25]:
            _say(f"  {kind} {nid}: {reason}")
        if len(refs) > 25:
            _say(f"  ... and {len(refs) - 25} more (in2xero report)")
    cw.close()
    return 0


def cmd_sync(cfg, args):
    """Incremental pass. Same machinery; the crosswalk skips what is already in."""
    return cmd_backfill(cfg, args)


def cmd_unpaid(cfg, args):
    """READ-ONLY. AUTHORISED invoices still owing, and whether Ninja thinks they are paid.

    An invoice sitting in AR because its payment was refused looks identical to a
    genuinely unpaid one from inside Xero. Only Invoice Ninja can tell them apart.
    """
    start, end = _window(cfg)
    x = Xero(cfg.xero)
    x.authenticate()
    n = Ninja(cfg.ninja)

    open_inv = [i for i in x.invoices_in_window(start, end)
                if i.get("Status") == "AUTHORISED" and float(i.get("AmountDue") or 0) > 0.005]
    _say(f"{len(open_inv)} invoice(s) still owing in Xero, "
         f"{sum(float(i.get('AmountDue') or 0) for i in open_inv):,.2f}\n")

    paid_in_ninja = {}
    for inv in n.invoices(start, end):
        num = str(inv.get("number") or "").strip()
        if num:
            paid_in_ninja[num.lower()] = inv

    genuinely_open, should_be_paid = [], []
    for i in open_inv:
        num = str(i.get("InvoiceNumber") or "").strip().lower()
        ninja = paid_in_ninja.get(num)
        if ninja and dec(ninja.get("balance")) <= Z:
            should_be_paid.append((i, ninja))
        else:
            genuinely_open.append((i, ninja))

    if should_be_paid:
        _say(f"{len(should_be_paid)} are PAID in Invoice Ninja but owing in Xero - their "
             "payment never landed:")
        for i, _ in should_be_paid[:40]:
            _say(f"  {i.get('InvoiceNumber'):<14} {float(i.get('AmountDue') or 0):>10,.2f}"
                 f"  {(i.get('Contact') or {}).get('Name', '')}")
        _say("\n  Re-run `in2xero backfill --only payments`. If they stay open, run")
        _say("  `in2xero report` for the refusal reason on each.")
    if genuinely_open:
        _say(f"\n{len(genuinely_open)} genuinely owe money in Invoice Ninja too "
             "(real accounts receivable):")
        for i, ninja in genuinely_open[:40]:
            bal = f"{dec(ninja.get('balance')):,.2f}" if ninja else "not in Ninja"
            _say(f"  {i.get('InvoiceNumber'):<14} Xero {float(i.get('AmountDue') or 0):>10,.2f}"
                 f"   Ninja balance {bal}")
    return 0


def cmd_forget(cfg, args):
    """Drop local crosswalk state for one kind so it is re-derived from Xero.

    Touches nothing in Xero. Invoice ids are re-adopted automatically on the next
    run; payments are re-attempted and Xero rejects any that are already settled,
    so this cannot double-apply cash.
    """
    kind = args.kind
    if kind not in ("payment", "payments", "invoice", "invoices", "contact", "contacts"):
        _say(f"unknown kind {kind!r} - use payments, invoices or contacts")
        return 1
    kind = kind.rstrip("s")
    cw = Crosswalk(cfg.sync.crosswalk_path)
    n, m = cw.forget(kind)
    cw.close()
    _say(f"crosswalk: {cfg.sync.crosswalk_path}")
    _say(f"forgot {n} {kind} mapping(s) and {m} refusal(s). Nothing in Xero changed.")
    if kind == "payment":
        _say("Next run re-checks every payment against Xero's outstanding balances;")
        _say("anything already settled is skipped, so no cash can be applied twice.")
    return 0


def cmd_report(cfg, args):
    cw = Crosswalk(cfg.sync.crosswalk_path)
    counts = cw.counts()
    _say("posted to Xero:")
    for k, v in sorted(counts.items()):
        _say(f"  {k:10} {v}")
    refs = cw.refusals()
    _say(f"\nrefusals: {len(refs)}")
    for kind, nid, reason, seen in refs:
        _say(f"  [{seen}] {kind} {nid}: {reason}")
    for step in ("contacts", "invoices", "payments"):
        wm = cw.watermark(step)
        if wm:
            _say(f"watermark {step}: {wm}")
    cw.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="in2xero", description="Invoice Ninja -> Xero")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn, helptext in (
        ("preflight", cmd_preflight, "read-only check of both ends"),
        ("existing", cmd_existing, "what is already in Xero for the window, drafts included"),
        ("audit", cmd_audit, "READ-ONLY: counts, duplicated payments, overpaid invoices"),
        ("clearing", cmd_clearing, "READ-ONLY: explain the clearing account balance"),
        ("unpaid", cmd_unpaid, "READ-ONLY: open invoices, cross-checked against Ninja"),
        ("accounts", cmd_accounts, "list bank accounts and their GUIDs"),
        ("auth", cmd_auth, "authenticate and print the tenant id"),
        ("backfill", cmd_backfill, "post the historical window"),
        ("sync", cmd_sync, "incremental pass"),
        ("report", cmd_report, "what has been posted, and what was refused"),
        ("forget", cmd_forget, "drop local crosswalk state for one kind (Xero untouched)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(fn=fn)
        if name in ("backfill", "sync"):
            p.add_argument("--dry-run", action="store_true",
                           help="build and reconcile every document, post nothing")
            p.add_argument("--only", metavar="STEPS",
                           help="comma-separated subset, e.g. --only payments")
            p.add_argument("--cap-overpayment", metavar="AMOUNT", default=None,
                           help="when a payment exceeds the invoice balance by up to "
                                "AMOUNT, allocate only what fits (e.g. 1.00). Off by "
                                "default: the tool refuses rather than reshape cash.")
        if name == "accounts":
            p.add_argument("--all", action="store_true",
                           help="also list non-bank accounts with their codes")
        if name == "forget":
            p.add_argument("kind", help="payments | invoices | contacts")
        if name == "existing":
            p.add_argument("--list", action="store_true",
                           help="print every invoice, not just the counts")
        if name == "auth":
            p.add_argument("--probe-scopes", action="store_true",
                           help="find which scopes this app actually holds")

    args = ap.parse_args(argv)
    try:
        cfg = cfgmod.load(args.config)
    except cfgmod.ConfigError as e:
        _say(f"config error: {e}")
        return 1
    return args.fn(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
