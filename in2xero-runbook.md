# 2025/2026 Xero rebuild — runbook

Companion to `invoice-ninja-to-xero-design`. That doc says why; this one says what to do,
in order. Written 2026-07-29.

**Confirmed with Josh:** no 2025 return has been filed, so this is a correction rather
than a restatement — no amended-filing problem. Existing bank lines get **re-coded**, not
deleted.

---

## Step 1 — Chart of accounts codes

Xero: Accounting → Advanced → Chart of accounts. Account *names* are known; the *codes*
are not, and the tool needs codes.

Write down the code for:

- the income account invoices should land in (`Service` is where 2025 revenue sits today)
- `Stripe Fees`
- somewhere for rounding differences (optional — `Miscellaneous` is fine)

## Step 2 — Create the clearing account

Accounting → Chart of accounts → **Add Bank Account** → name it `Stripe Clearing`, set up
manually, no feed. Note its code.

This is the piece that makes the whole thing work. The Xero bank feed already carries the
Stripe and Venmo deposits for money Invoice Ninja also has payments for. Both posting to
the real bank account would double the cash.

## Step 3 — Rehearse on the Demo Company

Xero settings → Demo Company. Point `tenant_id` at it and run the full backfill.

An AUTHORISED invoice can **never be deleted** in Xero, only voided. The Demo Company can
be reset; the real org cannot. Do not skip this.

## Step 4 — Dry run against the real data

```bash
in2xero preflight
in2xero backfill --dry-run
```

`--dry-run` needs no Xero credentials. It builds and reconciles all 319 invoices and
reports anything that will not post. Expect a handful of refusals; read them before going
further.

## Step 5 — Backfill

```bash
in2xero backfill
```

~16 API calls batched at 50. Starter's 1,000/day is ample. If it stops on the rate limit
it stops cleanly — re-run and the crosswalk resumes.

After this Xero holds:

- $117,374 of 2025 revenue across 211 invoices, real customer names
- $73,140 of 2026 revenue across 108 invoices
- payments settled into Stripe Clearing
- AR of $395 across 5 open invoices

And, temporarily, **double-counted income** — the old bank lines are still coded to
`Service`. Step 6 fixes that.

## Step 6 — Re-code the 2025 bank lines

Accounting → Bank accounts → the fed account → Account transactions. For 2025, the rows
currently coded to `Service` income (contacts read *Stripe*, *Venmo*, *Dan Wilson*,
*Elk Country RV Resort*, *Amazon Shopping*) get re-coded to **Stripe Clearing**.

They stay reconciled against the bank. Only the account they point at changes. Cash keeps
tying to the bank statement; the income double-count disappears.

$25,997 of the $27,720 is the single "Stripe" lump, so this is a much smaller job than the
row count suggests.

While in there: *Amazon Shopping* $177.98 sitting in revenue is almost certainly a refund
miscoded to income. Worth a look.

## Step 7 — Clear the clearing account

Whatever remains in Stripe Clearing after step 6 is **Stripe's processing fees**. Journal
it to `Stripe Fees`.

Sanity check: Xero currently shows **$0.33** of Stripe fees for all of 2025 against ~$26K
of Stripe volume. The real figure is likely a few hundred to low thousands. If the
clearing residual comes out near zero, something in steps 5–6 went wrong.

## Step 8 — Verify

- P&L 2025 income ≈ **$117,374**; 2026 YTD ≈ **$73,140**
- Balance sheet AR ≈ **$395**
- Stripe Clearing ≈ **$0**
- Bank account still reconciles to the statement
- `Stripe Fees` is now a plausible number

## Step 9 — The 2026 bank feed

Separate from everything above and **more urgent for D&B than any of it**: Xero recorded
zero transactions in 2026. The feed stopped in January. Cash currently shows $9,984.83,
which is a stale end-of-2025 carry-forward.

Imported payments move cash, but that is not the same as the feed being current and
reconciled. A balance sheet whose cash doesn't tie to the bank statement is exactly what a
credit reviewer checks first.

Find out what broke, restart it, catch up seven months.

## Step 10 — Ongoing

```bash
in2xero sync        # cron it, daily
```

---

## Loose ends worth a look

- **2 draft invoices worth $10,637** sitting unsent in Invoice Ninja. Excluded from the
  import (drafts create no journals) but that is real money not billed.
- **1,080 Invoice Ninja clients**, ~60 with any billing. Only those 60 become Xero
  contacts. The other 1,020 are worth an archive pass in Invoice Ninja someday.
