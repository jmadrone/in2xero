"""Offline tests for the transform layer. No network, no credentials.

    python3 -m pytest tests/ -q       (or: python3 tests/test_transform.py)
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from in2xero.transform import (            # noqa: E402
    GONE, PAYABLE, Refused, SETTLED, already_in_xero, apply_against_remaining,
    build_contact, build_invoice,
    build_payments, contact_name, index_existing, is_credit_application,
    make_tax_resolver,
)

NOTAX = make_tax_resolver("none", {})
FAILS = 0


def check(label, cond):
    global FAILS
    if cond:
        print(f"  ok   {label}")
    else:
        FAILS += 1
        print(f"  FAIL {label}")


def raises(label, fn, exc=Refused):
    try:
        fn()
    except exc:
        print(f"  ok   {label}")
        return
    global FAILS
    FAILS += 1
    print(f"  FAIL {label} (did not raise)")


def inv(**kw):
    base = {
        "id": "1", "number": "INV-1", "date": "2025-06-15", "due_date": "2025-07-15",
        "amount": "100.00", "total_taxes": "0", "uses_inclusive_taxes": False,
        "line_items": [{"quantity": "1", "cost": "100.00", "notes": "Work"}],
    }
    base.update(kw)
    return base


def total_of(built):
    return sum(Decimal(l["Quantity"]) * Decimal(l["UnitAmount"]) for l in built["LineItems"])


print("\ncontacts")
check("display_name preferred",
      contact_name({"id": 1, "display_name": "Acme", "name": "x"}) == "Acme")
check("falls back to contact person",
      contact_name({"id": 1, "contacts": [{"first_name": "Dan", "last_name": "Wilson"}]})
      == "Dan Wilson")
check("never empty", contact_name({"id": 77}).startswith("Invoice Ninja client"))
check("ContactNumber is the foreign key",
      build_contact({"id": 42, "name": "X"})["ContactNumber"] == "IN-C-42")
check("address only when present", "Addresses" not in build_contact({"id": 1, "name": "X"}))
check("address built when present",
      build_contact({"id": 1, "name": "X", "city": "Arcata"})["Addresses"][0]["City"] == "Arcata")

print("\ninvoices - basics")
b = build_invoice(inv(), "C1", "400", NOTAX)
check("posts AUTHORISED, never DRAFT", b["Status"] == "AUTHORISED")
check("type ACCREC", b["Type"] == "ACCREC")
check("reference carries the Ninja id", b["Reference"] == "IN-1")
check("date preserved exactly", b["Date"] == "2025-06-15")
check("total reconciles", total_of(b) == Decimal("100.00"))

print("\ninvoices - discounts and surcharges")
b = build_invoice(inv(amount="90.00", discount="10", is_amount_discount=False), "C1", "400", NOTAX)
check("percentage discount", total_of(b) == Decimal("90.00"))
b = build_invoice(inv(amount="75.00", discount="25", is_amount_discount=True), "C1", "400", NOTAX)
check("amount discount", total_of(b) == Decimal("75.00"))
b = build_invoice(inv(amount="115.00", custom_surcharge1="15",
                      custom_surcharge_label1="Rush"), "C1", "400", NOTAX)
check("surcharge added", total_of(b) == Decimal("115.00"))
check("surcharge label kept", any(l["Description"] == "Rush" for l in b["LineItems"]))
b = build_invoice(inv(
    amount="180.00",
    line_items=[{"quantity": "2", "cost": "100.00", "notes": "A",
                 "discount": "10", "is_amount_discount": False}],
), "C1", "400", NOTAX)
check("line-level percentage discount", total_of(b) == Decimal("180.00"))

print("\ninvoices - tax")
b = build_invoice(inv(amount="108.75", total_taxes="8.75"), "C1", "400", NOTAX)
check("exclusive is the default", b["LineAmountTypes"] == "Exclusive")
b = build_invoice(inv(amount="100.00", total_taxes="8.04", uses_inclusive_taxes=True),
                  "C1", "400", NOTAX)
check("inclusive flag honoured", b["LineAmountTypes"] == "Inclusive")
check("inclusive total excludes tax again", total_of(b) == Decimal("100.00"))
resolver = make_tax_resolver("resolve", {"CA Sales Tax + Local": "TAX999"})
b = build_invoice(inv(
    amount="108.75", total_taxes="8.75",
    line_items=[{"quantity": "1", "cost": "100.00", "notes": "W",
                 "tax_name1": "CA Sales Tax + Local", "tax_rate1": "8.75"}],
), "C1", "400", resolver)
check("tax type resolved at runtime", b["LineItems"][0]["TaxType"] == "TAX999")
raises("unknown tax rate refuses rather than guessing",
       lambda: build_invoice(inv(
           amount="108.75", total_taxes="8.75",
           line_items=[{"quantity": "1", "cost": "100.00", "notes": "W",
                        "tax_name1": "Mystery Tax", "tax_rate1": "8.75"}],
       ), "C1", "400", resolver))

print("\ninvoices - the reconciliation guard")
raises("large drift is refused, not reshaped",
       lambda: build_invoice(inv(amount="500.00"), "C1", "400", NOTAX))
b = build_invoice(inv(amount="100.01"), "C1", "400", NOTAX)
check("sub-cent drift absorbed by an explicit line",
      any(l["Description"] == "Rounding" for l in b["LineItems"]))
check("and the total then matches Ninja", total_of(b) == Decimal("100.01"))
raises("no line items is refused", lambda: build_invoice(inv(line_items=[]), "C1", "400", NOTAX))

print("\npayments")
pay = {"id": "9", "date": "2025-06-20",
       "paymentables": [{"invoice_id": "1", "amount": "100.00"}]}
out = build_payments(pay, {"1": "XID-1"}, {"AccountID": "BANK-GUID"})
check("one payment per allocation", len(out) == 1)
check("settles to clearing by AccountID", out[0]["Account"]["AccountID"] == "BANK-GUID")
check("payment date preserved", out[0]["Date"] == "2025-06-20")
check("amount correct", out[0]["Amount"] == "100.00")
check("links the right invoice", out[0]["Invoice"]["InvoiceID"] == "XID-1")

split = {"id": "10", "date": "2025-06-21", "paymentables": [
    {"invoice_id": "1", "amount": "60.00"}, {"invoice_id": "2", "amount": "40.00"}]}
out = build_payments(split, {"1": "X1", "2": "X2"}, {"AccountID": "B"})
check("split payment fans out", len(out) == 2)
check("split amounts kept", {o["Amount"] for o in out} == {"60.00", "40.00"})

refunded = {"id": "11", "date": "2025-06-22",
            "paymentables": [{"invoice_id": "1", "amount": "100.00", "refunded": "30.00"}]}
check("refund netted off", build_payments(refunded, {"1": "X1"}, {"AccountID": "B"})[0]["Amount"] == "70.00")

credit_app = {"id": "12", "date": "2025-06-23",
              "paymentables": [{"credit_id": "5", "invoice_id": "1", "amount": "50.00"}]}
check("credit application detected", is_credit_application(credit_app))
raises("credit application never becomes cash",
       lambda: build_payments(credit_app, {"1": "X1"}, {"AccountID": "B"}))
check("bare string still accepted as a Code",
      build_payments(pay, {"1": "XID-1"}, "4300")[0]["Account"]["Code"] == "4300")
raises("no clearing account configured is refused",
       lambda: build_payments(pay, {"1": "XID-1"}, {}))
raises("payment before its invoice is refused",
       lambda: build_payments(pay, {}, {"AccountID": "B"}))
raises("fully refunded payment is refused",
       lambda: build_payments(
           {"id": "13", "date": "2025-06-24",
            "paymentables": [{"invoice_id": "1", "amount": "50.00", "refunded": "50.00"}]},
           {"1": "X1"}, {"AccountID": "B"}))

print("\nnegative quantities (Xero rejects Quantity < 0)")
neg = build_invoice(inv(
    amount="-69.61",
    line_items=[{"quantity": "-1", "cost": "69.61", "notes": "Credit adjustment"}],
), "C1", "400", NOTAX)
check("quantity is never negative",
      all(Decimal(l["Quantity"]) >= 0 for l in neg["LineItems"]))
check("sign moves onto UnitAmount", Decimal(neg["LineItems"][0]["UnitAmount"]) < 0)
check("total still reconciles", total_of(neg) == Decimal("-69.61"))
mixed = build_invoice(inv(
    amount="130.39",
    line_items=[{"quantity": "2", "cost": "100.00", "notes": "Work"},
                {"quantity": "-1", "cost": "69.61", "notes": "Credit"}],
), "C1", "400", NOTAX)
check("mixed signs reconcile", total_of(mixed) == Decimal("130.39"))
check("no negative quantity survives",
      all(Decimal(l["Quantity"]) >= 0 for l in mixed["LineItems"]))

print("\noutstanding-balance tracking (float/Decimal mixing crashed a live run)")

def alloc(xid, amt):
    return {"Invoice": {"InvoiceID": xid}, "Amount": amt}

rem = {"X": Decimal("160.00")}
keep, over = apply_against_remaining([alloc("X", "140.00")], rem)
check("partial payment accepted", len(keep) == 1 and not over)
check("balance decremented", rem["X"] == Decimal("20.00"))
keep, over = apply_against_remaining([alloc("X", "20.00")], rem)
check("second payment fits the remainder", len(keep) == 1 and not over)
check("balance now zero", rem["X"] == Decimal("0.00"))
keep, over = apply_against_remaining([alloc("X", "50.00")], rem)
check("third payment refused, not silently trimmed", len(over) == 1 and not keep)
check("refusal reports both numbers",
      over[0]["amount"] == Decimal("50.00") and over[0]["outstanding"] == Decimal("0.00"))

# Xero hands back floats; the tool works in Decimal. Mixing them raises
# TypeError, which is exactly how this crashed mid-run.
rem_f = {"Y": 100.0}
keep, over = apply_against_remaining([alloc("Y", "40.00")], rem_f)
check("float balances from Xero do not explode", len(keep) == 1 and not over)
check("float coerced to Decimal", rem_f["Y"] == Decimal("60.00"))

rem_multi = {"A": Decimal("100.00"), "B": Decimal("10.00")}
keep, over = apply_against_remaining(
    [alloc("A", "60.00"), alloc("B", "40.00")], rem_multi)
check("split payment: good leg kept, bad leg flagged",
      len(keep) == 1 and len(over) == 1)
check("unknown invoice passes through untouched",
      len(apply_against_remaining([alloc("ZZZ", "5.00")], {})[0]) == 1)

print("\nduplicate guard / adoption")
XERO = [
    {"InvoiceNumber": "R-1794", "Reference": "", "Status": "DRAFT", "InvoiceID": "G-DRAFT"},
    {"InvoiceNumber": "1790", "Reference": "IN-55", "Status": "AUTHORISED", "InvoiceID": "G-AUTH"},
    {"InvoiceNumber": "R-1566", "Reference": "", "Status": "PAID", "InvoiceID": "G-PAID"},
]
nums, refs = index_existing(XERO)
check("indexes numbers and refs", len(nums) == 3 and len(refs) == 1)

# Xero keeps deleted invoices, and a deleted draft can share a number with the
# live invoice that replaced it. If the dead one wins, the live invoice is never
# adopted and every payment against it is refused as "not in Xero".
SHADOW = [
    {"InvoiceNumber": "1612", "Reference": "", "Status": "DELETED", "InvoiceID": "DEAD"},
    {"InvoiceNumber": "1612", "Reference": "IN-abc", "Status": "AUTHORISED",
     "InvoiceID": "LIVE"},
]
n2, r2 = index_existing(SHADOW)
check("live invoice wins over a deleted twin",
      already_in_xero({"id": "abc", "number": "1612"}, n2, r2)["id"] == "LIVE")
n3, r3 = index_existing(list(reversed(SHADOW)))
check("and wins regardless of document order",
      already_in_xero({"id": "zz", "number": "1612"}, n3, r3)["id"] == "LIVE")
check("the live match is payable",
      already_in_xero({"id": "zz", "number": "1612"}, n3, r3)["status"] in PAYABLE)
n4, r4 = index_existing([
    {"InvoiceNumber": "9", "Status": "DELETED", "InvoiceID": "D1"},
    {"InvoiceNumber": "9", "Status": "VOIDED", "InvoiceID": "D2"},
])
check("all-dead still resolves (so it can be re-posted, not adopted)",
      already_in_xero({"id": "q", "number": "9"}, n4, r4)["status"] in GONE)

hit = already_in_xero({"id": "1", "number": "R-1794"}, nums, refs)
check("draft is detected", hit is not None)
check("draft status reported", hit["status"] == "DRAFT")
check("draft is NOT payable", hit["status"] not in PAYABLE)
check("draft carries its Xero id", hit["id"] == "G-DRAFT")

hit = already_in_xero({"id": "55", "number": "zzz"}, nums, refs)
check("reference match wins over number", "IN-55" in hit["matched_on"])
check("authorised is adoptable", hit["status"] in PAYABLE and hit["id"] == "G-AUTH")

hit = already_in_xero({"id": "9", "number": "r-1566"}, nums, refs)
check("number match is case-insensitive", hit is not None)
check("paid is treated as settled", hit["status"] in SETTLED)

check("unknown invoice passes through",
      already_in_xero({"id": "999", "number": "R-9999"}, nums, refs) is None)
check("blank number does not false-match",
      already_in_xero({"id": "998", "number": ""}, nums, refs) is None)
check("empty Xero means nothing collides",
      already_in_xero({"id": "1", "number": "X"}, *index_existing([])) is None)

print(f"\n{'FAILED: ' + str(FAILS) if FAILS else 'all assertions passed'}")
sys.exit(1 if FAILS else 0)
