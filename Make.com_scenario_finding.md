# The 78 draft invoices: source identified

Written 2026-07-29. Companion to `invoice-ninja-to-xero-runbook`, step 3.

## What was in Xero

80 ACCREC invoices for 2025-01-01 .. 2026-07-29:

- **78 DRAFT**, $56,980.46, dated **2026-01-01 → 2026-06-04**
- 2 PAID, $200.99 — `INV-0001` $1.00 (Josh Madrone, 2025-02-10) and `R-1566` $199.99
  (Elk Country RV Resort, 2025-09-21)

Drafts create no journal, so all 78 were invisible in the P&L, balance sheet and aged
receivables. Every report read $0.00 for 2026 and every report was correct.

## Source: Make (make.com), org "Emerald Security", team 383207

Six scenarios move Invoice Ninja data into Xero. **All are currently `isActive: false`.**

| ID | Name | Created | State |
| --- | --- | --- | --- |
| 3667003 | Invoice Ninja -> Xero | 2025-02-13 | inactive, **invalid** |
| 4030961 | Invoice Ninja -> Xero | 2025-06-05 | inactive |
| 4195263 | Invoice Ninja -> Xero | 2025-08-06 | inactive |
| 4191984 | New scenario | 2025-08-05 | inactive |
| 3667190 | Sync Invoice Ninja Clients to Xero | 2025-02-14 | inactive |
| 4030978 | Sync Invoice Ninja Invoices to Xero | 2025-06-05 | inactive, **invalid**, webhook |

Make's execution history is 30-day retention, so the Jan–Jun 2026 runs are purged and the
per-scenario execution counters read 0. Which specific scenario produced the 78 cannot be
proven from the API — but the mechanism is confirmed below, and every candidate is off.

## Mechanism: Status left empty defaults to DRAFT

Blueprint of 4195263, module `xero:CreateInvoice`:

```
"Status": { "mode": "chose", "label": "Empty" }
```

The Status field is **not mapped**. Xero's CreateInvoice defaults to DRAFT when Status is
omitted. Nothing was failing — the scenario was doing exactly what it was configured to
do, and what it was configured to do produced no accounting.

This is the same trap the in2xero design doc calls out: an importer that posts DRAFT "for
safety" produces an empty balance sheet.

## Two further defects in the same blueprint

**1. The no-contact route creates BILLS, not sales invoices.** Route 2 ("Customer Doesn't
Exist") calls CreateContact and then CreateInvoice with:

```
"Type": "ACCPAY",  "AccountCode": "4300"
```

`ACCPAY` is a supplier bill. Coded to 4300 Service — an *income* account. Any invoice for
a customer not already in Xero would post as accounts payable against revenue. Run
`in2xero existing` (v2.8+) to check whether any ACCPAY bills exist; the earlier sweeps
filtered to ACCREC and would not have shown them.

**2. Line items are mapped as a concatenated string, not an array.**

```
"LineItems": "{{1.items[].name}}{{1.items[].note}}{{1.items[].cost}}{{1.items[].quantity}}"
```

Xero expects an array of line objects. This is why draft `1630` carries $0.00.

**3. No InvoiceNumber mapping in route 1** — Xero auto-numbers instead. `INV-0001`, the
$1.00 PAID invoice, is a Xero auto-number and looks like a test run. The 78 drafts *do*
carry Invoice Ninja numbers, so they came from a different scenario in the set.

## What to do

1. **Delete or archive all six scenarios** in Make. They are inactive, so nothing fires
   today, but any of them could be switched back on by accident.
2. **Delete the 78 drafts** in Xero. Drafts can be deleted; AUTHORISED invoices can only
   be voided. Safe once step 1 is done.
3. **Void `INV-0001`** ($1.00, Josh Madrone) — a test invoice sitting in revenue.
4. **Leave `R-1566` alone.** It is real, PAID, and in2xero will refuse to duplicate it
   (matched by invoice number), which is correct.
5. **Check for ACCPAY bills** before backfilling.

Then the backfill in step 5 of the runbook is unblocked.

## Note on the January start

The drafts begin 2026-01-01 and the Xero bank feed also went quiet in January 2026. The
scenarios all predate that (Feb–Aug 2025), so the January start most likely reflects one
being switched on, or its trigger epoch being set, at the turn of the year. Worth keeping
in mind while investigating the feed (runbook step 10) — they may share a cause.
