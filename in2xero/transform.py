"""Invoice Ninja -> Xero document building, with a reconciliation guard.

The guard is the important part. Every document is rebuilt line by line and the
recomputed total is compared against Invoice Ninja's own `amount`. If they differ
by more than a cent the document is REFUSED, not reshaped. Silently massaging a
total to make it fit is how AR quietly stops meaning anything.

Sub-cent drift (rounding between Invoice Ninja's per-line rounding and Xero's) is
absorbed by an explicit rounding line so the difference is visible in the ledger
rather than hidden.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .ninja import Z, dec

CENT = Decimal("0.01")
TOLERANCE = Decimal("0.01")


class Refused(Exception):
    """Document does not reconcile and must not be posted."""


def q(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


# ---- contacts ----------------------------------------------------------

def contact_name(client: dict) -> str:
    """Xero requires a non-empty, org-unique contact Name."""
    for key in ("display_name", "name"):
        v = (client.get(key) or "").strip()
        if v:
            return v[:500]
    contacts = client.get("contacts") or []
    if contacts:
        first = (contacts[0].get("first_name") or "").strip()
        last = (contacts[0].get("last_name") or "").strip()
        full = f"{first} {last}".strip()
        if full:
            return full[:500]
        email = (contacts[0].get("email") or "").strip()
        if email:
            return email[:500]
    return f"Invoice Ninja client {client.get('id')}"


def build_contact(client: dict) -> dict:
    c = {
        "Name": contact_name(client),
        # The stable foreign key. Survives renames on either side.
        "ContactNumber": f"IN-C-{client.get('id')}",
    }
    people = client.get("contacts") or []
    if people:
        p = people[0]
        if (p.get("email") or "").strip():
            c["EmailAddress"] = p["email"].strip()
        first = (p.get("first_name") or "").strip()
        last = (p.get("last_name") or "").strip()
        if first:
            c["FirstName"] = first[:255]
        if last:
            c["LastName"] = last[:255]
    if (client.get("phone") or "").strip():
        c["Phones"] = [{"PhoneType": "DEFAULT", "PhoneNumber": client["phone"].strip()[:50]}]

    addr = {
        k: (client.get(src) or "").strip()
        for k, src in (
            ("AddressLine1", "address1"),
            ("AddressLine2", "address2"),
            ("City", "city"),
            ("Region", "state"),
            ("PostalCode", "postal_code"),
        )
    }
    if any(addr.values()):
        addr["AddressType"] = "STREET"
        c["Addresses"] = [{k: v for k, v in addr.items() if v}]
    return c


# ---- invoices ----------------------------------------------------------

def _line_total(li: dict) -> Decimal:
    """Invoice Ninja line net, before tax, after the line-level discount."""
    qty = dec(li.get("quantity"))
    cost = dec(li.get("cost"))
    gross = qty * cost
    disc = dec(li.get("discount"))
    if disc != Z:
        if li.get("is_amount_discount"):
            gross -= disc
        else:
            gross -= gross * disc / Decimal("100")
    return gross


def build_invoice(inv: dict, xero_contact_id: str, sales_code: str,
                  tax_type_for, rounding_code: str = "") -> dict:
    """Build one ACCREC invoice. Raises Refused if it will not reconcile.

    Posted AUTHORISED, always. An importer that posts DRAFT "for safety" produces
    an empty balance sheet - drafts create no journals in Xero.
    """
    inclusive = bool(inv.get("uses_inclusive_taxes"))
    lines = []
    running = Z

    for li in inv.get("line_items") or []:
        net = _line_total(li)
        if net == Z and not (li.get("notes") or "").strip():
            continue
        # Xero REFUSES a negative Quantity but accepts a negative UnitAmount.
        # Invoice Ninja uses quantity -1 for credit/adjustment lines, so carry the
        # sign on the amount instead. qty * unit is unchanged, so the invoice still
        # reconciles to the same total - this is a representation change, not a
        # value change.
        qty = dec(li.get("quantity")) or Decimal("1")
        signed_qty = abs(qty) or Decimal("1")
        line = {
            "Description": (li.get("notes") or "Services").strip()[:4000] or "Services",
            "Quantity": str(signed_qty),
            "UnitAmount": str(q(net / signed_qty)),
            "AccountCode": sales_code,
        }
        tname = (li.get("tax_name1") or "").strip()
        trate = dec(li.get("tax_rate1"))
        if tname and trate != Z:
            line["TaxType"] = tax_type_for(tname, trate)
        else:
            line["TaxType"] = tax_type_for(None, Z)
        lines.append(line)
        running += net

    if not lines:
        raise Refused("no usable line items")

    # Invoice Ninja surcharges are invoice-level, not line-level.
    for n in ("1", "2", "3", "4"):
        s = dec(inv.get(f"custom_surcharge{n}"))
        if s != Z:
            lines.append({
                "Description": (inv.get(f"custom_surcharge_label{n}") or f"Surcharge {n}")[:4000],
                "Quantity": "1",
                "UnitAmount": str(q(s)),
                "AccountCode": sales_code,
                "TaxType": tax_type_for(None, Z),
            })
            running += s

    # Invoice-level discount.
    d = dec(inv.get("discount"))
    if d != Z:
        amount = d if inv.get("is_amount_discount") else running * d / Decimal("100")
        if amount != Z:
            lines.append({
                "Description": "Discount",
                "Quantity": "1",
                "UnitAmount": str(q(-amount)),
                "AccountCode": sales_code,
                "TaxType": tax_type_for(None, Z),
            })
            running -= amount

    expected = dec(inv.get("amount"))
    tax_total = dec(inv.get("total_taxes"))
    # `amount` is tax-inclusive in Xero terms; compare like with like.
    computed = running if inclusive else running + tax_total
    drift = q(expected) - q(computed)

    if abs(drift) > TOLERANCE:
        raise Refused(
            f"does not reconcile: Invoice Ninja says {q(expected)}, lines rebuild to "
            f"{q(computed)} (drift {q(drift)})"
        )
    if drift != Z:
        lines.append({
            "Description": "Rounding",
            "Quantity": "1",
            "UnitAmount": str(q(drift)),
            "AccountCode": rounding_code or sales_code,
            "TaxType": tax_type_for(None, Z),
        })

    return {
        "Type": "ACCREC",
        "Contact": {"ContactID": xero_contact_id},
        "Date": (inv.get("date") or "")[:10],
        "DueDate": (inv.get("due_date") or inv.get("date") or "")[:10],
        "InvoiceNumber": (inv.get("number") or f"IN-{inv.get('id')}")[:255],
        "Reference": f"IN-{inv.get('id')}",
        "LineAmountTypes": "Inclusive" if inclusive else "Exclusive",
        "Status": "AUTHORISED",
        "LineItems": lines,
    }


# ---- duplicate guard ---------------------------------------------------

# Statuses that can accept a Payment allocation in Xero.
PAYABLE = ("AUTHORISED",)
# Already settled: adopt the id, but NEVER try to pay it again.
SETTLED = ("PAID",)
# Gone. Must not block a fresh post - a deleted draft is not a duplicate, and
# treating it as one silently drops the invoice from the rebuild entirely.
GONE = ("DELETED", "VOIDED")


# How much a match is worth when two Xero invoices share a number. Higher wins.
_STATUS_RANK = {"PAID": 4, "AUTHORISED": 4, "SUBMITTED": 2, "DRAFT": 2,
                "VOIDED": 0, "DELETED": 0}


def index_existing(xero_invoices: list) -> tuple:
    """(numbers, refs) -> {id, status}, for matching what is already in Xero.

    Xero KEEPS deleted invoices, and a deleted draft can carry the same invoice
    number as the live one that replaced it. Last-write-wins therefore lets a
    dead record shadow a live one: the number resolves to a DELETED invoice, the
    live invoice is never adopted, and every payment against it is refused with
    "not in Xero yet" while it sits there plainly visible in Xero.

    So rank by status and never let a deleted or voided record displace a live
    match. A number seen twice with equal rank keeps the first.
    """
    numbers, refs = {}, {}

    def offer(bucket, key, info):
        if not key:
            return
        cur = bucket.get(key)
        if cur is None or _STATUS_RANK.get(info["status"], 1) > _STATUS_RANK.get(
                cur["status"], 1):
            bucket[key] = info

    for i in xero_invoices:
        info = {"id": i.get("InvoiceID"), "status": i.get("Status") or "?"}
        offer(numbers, str(i.get("InvoiceNumber") or "").strip().lower(), info)
        offer(refs, str(i.get("Reference") or "").strip().lower(), info)
    return numbers, refs


def already_in_xero(ninja_inv: dict, numbers: dict, refs: dict):
    """Return the matching Xero record, or None.

    {"id", "status", "matched_on", "reason"}. A match is NOT simply a refusal:
    an AUTHORISED invoice that already exists should be ADOPTED into the
    crosswalk so its payments can still be settled. Only a draft is a genuine
    blocker, because Xero cannot apply a payment to a draft.
    """
    ref = f"in-{ninja_inv.get('id')}"
    hit = refs.get(ref)
    matched = f"reference IN-{ninja_inv.get('id')}"
    if not hit:
        num = str(ninja_inv.get("number") or "").strip()
        if num:
            hit = numbers.get(num.lower())
            matched = f"invoice number {num}"
    if not hit:
        return None
    return {
        "id": hit.get("id"),
        "status": hit.get("status"),
        "matched_on": matched,
        "reason": f"already in Xero by {matched} (status {hit.get('status')})",
    }


def apply_against_remaining(built: list, remaining: dict) -> tuple:
    """Split built allocations into (accepted, over-allocated), decrementing.

    Xero rejects a payment that exceeds an invoice's outstanding balance, and one
    rejection voids its entire batch. The balance changes as a run applies
    payments, so a snapshot taken before the run goes stale immediately - two
    payments against one invoice both look affordable and the second is refused.

    `remaining` maps Xero InvoiceID -> Decimal still owed, and is mutated here.
    Everything is Decimal: mixing float and Decimal raises TypeError, which is
    how this blew up mid-run the first time.
    """
    keep, over = [], []
    for b in built:
        xid = (b.get("Invoice") or {}).get("InvoiceID")
        amt = dec(b.get("Amount"))
        rem = remaining.get(xid)
        if rem is None:
            keep.append(b)
            continue
        rem = dec(rem)
        if amt > rem + TOLERANCE / 2:
            over.append({"allocation": b, "outstanding": rem, "amount": amt})
        else:
            remaining[xid] = rem - amt
            keep.append(b)
    return keep, over


# ---- payments ----------------------------------------------------------

def is_credit_application(payment: dict) -> bool:
    """Invoice Ninja records 'apply a credit to an invoice' AS A PAYMENT.

    Posting that to Xero as a payment invents cash that never touched a bank
    account. These belong in CreditNotes/{id}/Allocations instead.
    """
    return any(r.get("credit_id") for r in (payment.get("paymentables") or []))


def build_payments(payment: dict, invoice_xero_ids: dict, clearing_ref) -> list:
    """One Xero Payment per allocated invoice.

    Settles to the CLEARING account, never the fed bank account. The bank feed
    already carries the Stripe/Venmo deposits for the same money; posting here too
    would double cash. What remains in clearing is processor fees.

    `clearing_ref` is what Xero gets as the payment's Account: normally
    {"AccountID": "<guid>"}, because Xero does not give bank accounts a Code.
    A bare string is accepted and treated as a Code for convenience.
    """
    if isinstance(clearing_ref, str):
        clearing_ref = {"Code": clearing_ref}
    if not clearing_ref:
        raise Refused("no clearing account configured - payments have nowhere to settle")
    if is_credit_application(payment):
        raise Refused("credit application, not cash - route to credit note allocations")

    date = (payment.get("date") or "")[:10]
    out = []
    for r in payment.get("paymentables") or []:
        inv_id = r.get("invoice_id")
        if not inv_id:
            continue
        xid = invoice_xero_ids.get(str(inv_id))
        if not xid:
            r = Refused(f"payment allocated to invoice {inv_id}, which is not in Xero yet")
            r.missing_invoice = inv_id      # so the caller can ask Ninja why
            raise r
        amt = dec(r.get("amount")) - dec(r.get("refunded"))
        if amt <= Z:
            continue
        out.append({
            "Invoice": {"InvoiceID": xid},
            "Account": dict(clearing_ref),
            # The payment date must be historically accurate. A correctly-dated
            # invoice settled "today" inflates AR at every prior reporting date.
            "Date": date,
            "Amount": str(q(amt)),
            "Reference": f"IN-P-{payment.get('id')}",
        })
    if not out:
        raise Refused("no positive invoice allocations")
    return out


# ---- tax ---------------------------------------------------------------

def make_tax_resolver(mode: str, xero_rates: dict, none_code: str = "NONE"):
    """Map an Invoice Ninja tax name to a Xero TaxType at runtime.

    Xero TaxType codes are per-organisation; hardcoding them is a portability bug
    waiting to happen. In this org the whole 19-month window carries $41.48 of tax
    across 5 invoices, so `none` is the default and correct mode.
    """
    def resolve(name, rate):
        if mode == "none" or not name or rate == Z:
            return none_code
        if name in xero_rates:
            return xero_rates[name]
        for rn, rt in xero_rates.items():
            if rn.strip().lower() == str(name).strip().lower():
                return rt
        raise Refused(
            f"Invoice Ninja tax rate {name!r} has no match in Xero. Create it in Xero "
            "(Accounting -> Advanced -> Tax rates) with exactly this name, or set "
            "sync.tax_mode: none."
        )
    return resolve
