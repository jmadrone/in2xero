# in2xero

Invoice Ninja → Xero backfill and incremental sync, built for **Emerald Security LLC**.

Rebuilds 2025 and 2026 in Xero from Invoice Ninja: 319 invoices, 319 payments, ~60
customer contacts. Goal is a balance sheet that survives scrutiny in a Dun & Bradstreet
credit file.

---

## The three things that make this non-obvious

**1. Archived is not void.** Invoice Ninja's `status=active` index filter silently drops
archived records. In this org **318 of 327 invoices are archived** — normal tidying after
payment. A sync that sends that filter imports 3% of the business and looks like it
worked. This tool never sends it; it classifies each record from its own
`archived_at`/`is_deleted` fields.

**2. Payments settle to a clearing account, never the real bank.** Xero's bank feed
already carries the Stripe and Venmo deposits. Posting imported payments to the same
account counts the cash twice. See *Setting up the clearing account* below.

**3. Expenses are deliberately not implemented.** Xero's bank feed already books more
expense ($6,769 in 2025) than Invoice Ninja holds ($2,357 across 19 months). Every Invoice
Ninja expense has a blank `payment_date`, so an import would turn all 43 into bills and
invent accounts payable. Several categories are not P&L expenses at all — *Owners Draw*,
*Payroll - Withholdings*, *Auto - Loan Payments*. Let the bank feed carry expenses.

---

## Install

```bash
pip3 install -e .          # or: pip3 install requests PyYAML && python3 -m in2xero.cli
cp config.example.yaml config.yaml
```

Runs on the Invoice Ninja server, or anywhere that can reach both Invoice Ninja and
`api.xero.com`.

## Setting up the clearing account

Do this in Xero before the first real run.

1. **Create a BANK-type account** called `Stripe Clearing`. Accounting → Chart of accounts
   → Add Bank Account → pick the option for an account you manage manually, not a feed.

   **Xero gives bank accounts no Code** — the chart of accounts has no field for one, so
   there is nothing to copy out of the UI. Get its GUID instead:

   ```bash
   in2xero accounts
   ```

   Paste the GUID into `xero.clearing_account_id`.
2. **Run the backfill.** Invoices create the real revenue and AR. Payments settle into
   Stripe Clearing.
3. **Re-code the existing 2025 bank lines.** The Stripe/Venmo deposits currently coded to
   `4300 Service` get re-coded to `Stripe Clearing`. They stay reconciled against the
   bank — you are changing which account they point at, not deleting them. Cash keeps
   tying to the bank statement; the income double-count disappears.
4. **Whatever is left in Stripe Clearing is processor fees.** Journal it to
   `6050 Merchant Account Fees`. Xero currently shows $0.33 across its fee accounts for
   all of 2025, which cannot be right against ~$26K of Stripe volume.

Deleting the bank lines instead gives the same P&L and a balance sheet where cash no
longer matches the bank. Don't.

## Secrets

`config.yaml` never holds real secrets — `${IN_API_TOKEN}`, `${XERO_CLIENT_ID}`, and
`${XERO_CLIENT_SECRET}` are expanded from the environment at load time (see
`in2xero/config.py`). Rather than exporting those by hand, resolve them from 1Password at
run time with the CLI's `op run`, using the `op://` references in `.env`
(gitignored, contains no secrets — just pointers to vault items):

```bash
op run --env-file=.env -- in2xero preflight
op run --env-file=.env -- in2xero backfill --dry-run
op run --env-file=.env -- in2xero backfill
op run --env-file=.env -- in2xero sync
```

Requires the 1Password CLI (`brew install 1password-cli`) signed in to the account that
holds the vault, and "Integrate with 1Password CLI" enabled in the desktop app's
Settings → Developer, so `op` unlocks via biometrics instead of a password prompt per run.
Secrets are resolved into the child process's environment only — never written to disk.

## Run it

```bash
in2xero accounts                  # bank accounts + their GUIDs (see below)
in2xero preflight                 # read-only. Proves both ends, shows what would happen
in2xero backfill --dry-run        # builds and reconciles every document, posts nothing
in2xero backfill                  # posts
in2xero report                    # what went in, and what was refused
in2xero sync                      # incremental; the crosswalk skips what is already there
```

Prefix any of these with `op run --env-file=.env --` to source credentials from
1Password (see *Secrets* above).

`--dry-run` needs **no Xero credentials** — it is the right first move, before Xero is
even configured.

**Rehearse on the Xero Demo Company first.** An AUTHORISED invoice can never be deleted in
Xero, only voided. The Demo Company can be reset from Settings; your real org cannot.

## Safety design

- **Nothing posts unless it reconciles.** Every document is rebuilt line by line and
  compared against Invoice Ninja's own `amount`. Drift over a cent is **refused**, never
  reshaped — silently massaging a total to fit is how AR stops meaning anything. Sub-cent
  drift gets an explicit `Rounding` line so it is visible rather than hidden.
- **Deterministic `Idempotency-Key`** per batch, derived from its contents. A retry after
  a network timeout cannot duplicate.
- **Resumable.** Xero allows 60 calls/min and 1,000/day on Starter (5,000 on Core). The
  client watches `X-DayLimit-Remaining` and stops cleanly above a floor rather than dying
  mid-batch. Re-run and the crosswalk picks up where it left off.
- **Watermarks record run start, not run end**, so a record modified mid-run is caught on
  the next pass rather than skipped forever.
- **Only clients with invoices become contacts.** Invoice Ninja holds 1,080 clients
  against ~60 that carry billing.

## Refusals

A refused document is one the tool would not post. `in2xero report` lists them with
reasons. Common ones:

| Reason | What it means |
| --- | --- |
| `does not reconcile` | Rebuilt lines disagree with Invoice Ninja's total by more than a cent. Look at the invoice by hand. |
| `credit application, not cash` | Invoice Ninja records "apply a credit" as a *payment*. Posting it as cash would invent money that never hit a bank. |
| `payment allocated to invoice N, which is not in Xero yet` | The tool looks up why and appends it. Usually the invoice predates the sync window — the payment collects AR from before the import, and there is nothing to settle it against. Only re-run if the diagnosis says the invoice IS in scope. |
| `tax rate X has no match in Xero` | Create the rate in Xero with exactly that name, or set `tax_mode: none`. |
| `no clearing account configured` | `xero.clearing_account_id` is unset. Run `in2xero accounts`. |

## Tests

```bash
python3 tests/test_transform.py
```

39 assertions over the transform layer — discounts (line and invoice, amount and
percentage), surcharges, inclusive vs exclusive tax, runtime tax resolution, the
reconciliation guard, split payments, refunds, credit applications, contact naming and
fallbacks. No network, no credentials.

## Layout

```
in2xero/config.py      config load + validation, ${ENV_VAR} expansion
in2xero/ninja.py       Invoice Ninja reads; the archived-record handling lives here
in2xero/xero.py        auth, rate limiting, batching, idempotency
in2xero/transform.py   document building + the reconciliation guard
in2xero/crosswalk.py   SQLite ninja_id -> xero_id; the only guard against duplicates
in2xero/cli.py         preflight / accounts / auth / backfill / sync / report
```
