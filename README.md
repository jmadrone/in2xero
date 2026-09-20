# in2xero

Sync invoices, payments and contacts from **Invoice Ninja** into **Xero** — a one-off
historical backfill, then an incremental daily pass.

It is a small, boring, auditable Python tool. It holds a local crosswalk of what it has
already posted, refuses anything it cannot post safely, and explains every refusal in
plain language. It is designed to be re-run: a missed day is caught up on the next run,
not lost.

---

## Disclaimer — please read

**This code is offered free of charge, to anyone, for any purpose, with no warranty of
any kind. You use it entirely at your own risk.**

It writes to your accounting system. Mistakes in accounting data are tedious and
sometimes expensive to unwind. Specifically:

- **Always run `--dry-run` first**, and read the output.
- **Always take a Xero backup** (or work in the Xero demo company) before your first
  real backfill.
- The authors are not accountants and this is not accounting, tax or financial advice.
  Have someone who is qualified check the result before you rely on it.
- Nobody involved in this project is liable for anything it does to your books. See
  [LICENSE](LICENSE).

If that is not an acceptable trade, do not use this tool.

---

## What it does

- **Contacts** — creates Invoice Ninja clients in Xero as contacts.
- **Invoices** — posts them as ACCREC (sales) invoices, with a duplicate guard that
  checks what Xero already holds, including drafts.
- **Payments** — allocates them against the matching invoices, settling into a
  **clearing account** rather than your real bank account.

It is one-directional: Invoice Ninja → Xero. It never writes to Invoice Ninja, and never
deletes anything in Xero.

## What it deliberately does not do

- **Expenses.** If your bank feed already books expenses, importing them again
  double-counts. Invoice Ninja expenses also tend to carry a blank `payment_date`, which
  would turn every one into a bill and invent accounts payable you do not owe. Some
  categories are not P&L expenses at all — owner's draw, payroll withholdings, loan
  principal. Let the bank feed carry expenses.
- **Two-way sync.** Xero is never the source of truth for Invoice Ninja here.
- **Guessing.** When Invoice Ninja and Xero disagree about money, it refuses and tells
  you, rather than reshaping the numbers so they fit.

---

## The three things that make this non-obvious

**1. Archived is not void.** Invoice Ninja's index endpoints accept a `status=active`
filter that silently drops archived records — and archiving is normal tidying after
payment, so a mature org can have the overwhelming majority of its invoices archived. A
sync that sends that filter imports a tiny slice of the business and looks like it
worked. This tool never sends it; it classifies each record from its own `archived_at`
and `is_deleted` fields.

**2. Payments must settle to a clearing account, never the real bank.** If your Xero
bank feed already carries the Stripe/Venmo/card deposits, posting imported payments to
that same account counts the cash twice. Payments land in a manually-managed BANK
account instead, and you re-code the existing deposits to point at it. See
[Setting up the clearing account](#setting-up-the-clearing-account).

**3. The crosswalk records intent; Xero records fact.** The local database says "I
posted this." Xero says what is actually there. These diverge whenever a post fails — so
whenever the two disagree, Xero wins. That principle is why `forget` exists (below), and
why several counters in the output mean what they mean.

---

## Requirements

- **Python 3.9+**, and two packages (`requests`, `PyYAML`) pulled in automatically.
- **A Xero Custom Connection** (`developer.xero.com` → My Apps → New app → Custom
  Connection). This is a paid add-on at time of writing, and it is the right choice for
  anything unattended: it uses `client_credentials`, so there is **no refresh token and
  no 60-day expiry cliff**.

  The standard `auth_code` flow is also supported, but its refresh token rotates on
  every use — if the process dies between receiving a new token and persisting it, the
  connection is bricked and needs re-authorising by hand. Do not use it for a scheduled
  job.
- **An Invoice Ninja API token** — Settings → Account Management → API Tokens.
- Network reach to your Invoice Ninja instance and to `api.xero.com`. Running it on the
  Invoice Ninja server itself is a good default.

---

## Install

```bash
git clone https://github.com/jmadrone/in2xero.git
cd in2xero
python3 -m venv venv
./venv/bin/pip install .
```

On Debian/Ubuntu, `python3 -m venv` can fail with *"ensurepip is not available"* — the
stdlib `venv` module imports fine but the package is split out. Install it:

```bash
sudo apt install python3-venv     # or e.g. python3.12-venv for a specific version
```

## Configure

```bash
cp config.example.yaml config.yaml
```

`config.yaml` holds no secrets. Any value written as `${NAME}` is replaced from the
environment at startup, and **if the variable is missing the tool exits with an error
rather than running with a blank value.**

The settings you must review:

| Key | What it is |
|---|---|
| `ninja.base_url` | Your Invoice Ninja URL. No trailing slash, no `/api/v1`. |
| `xero.auth_mode` | `custom_connection` (recommended) or `auth_code`. |
| `xero.tenant_id` | Your Xero organisation id. `in2xero auth` prints it. |
| `xero.sales_account_code` | Account **code** for invoice lines, e.g. `4300`. |
| `xero.clearing_account_id` | GUID of the clearing account. `in2xero accounts` prints it. |
| `sync.start_date` / `end_date` | The window. `end_date` blank means "today". |
| `sync.steps` | Which steps run. Default `[contacts, invoices, payments]`. |
| `sync.crosswalk_path` | Where the local state lives. **Use an absolute path** for scheduled runs. |
| `sync.dry_run` | Global safety switch. |

Account **codes** are not names — read them from Xero under Accounting → Advanced →
Chart of accounts.

## Secrets

Put the real values in an environment file, not in `config.yaml`:

```bash
sudo install -d /etc/in2xero
sudo cp deploy/in2xero.env.example /etc/in2xero/in2xero.env
sudo chown root:root /etc/in2xero/in2xero.env
sudo chmod 600      /etc/in2xero/in2xero.env
sudo "${EDITOR:-nano}" /etc/in2xero/in2xero.env
```

```ini
IN_API_TOKEN=...
XERO_CLIENT_ID=...
XERO_CLIENT_SECRET=...
XERO_TENANT_ID=...
```

With the systemd units in [`deploy/`](deploy/), systemd reads this file **as root before
dropping to the unprivileged service user**, so it never needs to be readable by that
account. Keep it `0600 root:root`.

> **Avoid secret managers that require a desktop session.** An unattended job cannot
> depend on a GUI app being open and unlocked. If you want a manager, use something with
> a headless mode — a service account, an instance role, or a parameter store.

---

## Setting up the clearing account

Do this in Xero **before** your first real run.

1. **Create a BANK-type account**, e.g. `Stripe Clearing`. Accounting → Chart of
   accounts → Add Bank Account → choose the manually-managed option, *not* a live feed.

   Xero does not give bank accounts a Code, so there is nothing to copy out of the UI.
   Get its GUID instead:

   ```bash
   in2xero accounts
   ```

   Put that GUID in `xero.clearing_account_id`.

2. **Run the backfill.** Invoices create the revenue and the receivable. Payments settle
   into the clearing account.

3. **Re-code your existing bank lines.** The deposits currently coded to your income
   account get re-coded to the clearing account. They stay reconciled against the bank —
   you are changing which account they point at, not deleting them.

4. **The clearing account should trend to zero.** A balance means imported payments and
   real deposits are not lining up. `in2xero clearing` explains the balance.

> **It must not be your real bank account.** The tool refuses to post there, but set it
> correctly anyway.

---

## First run

In this order. Do not skip the dry run.

```bash
in2xero preflight                 # read-only: both ends reachable, accounts resolve
in2xero accounts                  # bank accounts + GUIDs
in2xero existing                  # what Xero already holds for the window (drafts too)
in2xero backfill --dry-run        # build and reconcile everything, post nothing
in2xero backfill                  # the real thing
in2xero unpaid                    # what is still owing, cross-checked against Ninja
```

Then schedule `in2xero sync` daily. `sync` is the same machinery as `backfill`; the
crosswalk skips whatever is already in.

### Commands

| Command | Writes? | What it is for |
|---|---|---|
| `preflight` | no | Both ends reachable, accounts resolve, estimated API calls. |
| `existing` | no | What Xero already holds for the window. `--list` for every invoice. |
| `accounts` | no | Bank accounts and their GUIDs. `--all` for every account. |
| `auth` | no | Authenticate and print the tenant id. `--probe-scopes` to see granted scopes. |
| `audit` | no | Counts, duplicated payments, overpaid invoices. |
| `unpaid` | no | Open invoices, cross-checked against Invoice Ninja. |
| `clearing` | no | Explain the clearing account balance. |
| `report` | no | Local view: what is posted, and every refusal with a timestamp. |
| `backfill` | **yes** | Post the historical window. |
| `sync` | **yes** | Incremental pass. Schedule this one. |
| `forget <kind>` | local only | Drop local crosswalk state for `payments`/`invoices`/`contacts`. |

Useful flags on `backfill`/`sync`:

- `--dry-run` — build and reconcile everything, post nothing.
- `--only STEPS` — e.g. `--only payments`.
- `--cap-overpayment AMOUNT` — see [Refusals](#refusals).

---

## Scheduling

**systemd (recommended).** Copy the units from [`deploy/`](deploy/):

```bash
sudo cp deploy/in2xero-sync.service deploy/in2xero-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start in2xero-sync        # one manual run first
sudo journalctl -u in2xero-sync -n 80 --no-pager
sudo systemctl enable --now in2xero-sync.timer
systemctl list-timers in2xero-sync.timer
```

`Persistent=true` means a run missed while the machine was off is caught up on next
boot. `OnCalendar` uses the **server's** timezone — check with `timedatectl`.

**cron.** Use `scripts/run_sync.sh`, which sources the env file:

```cron
0 7 * * * /opt/in2xero/scripts/run_sync.sh >> /var/log/in2xero.log 2>&1
```

> **Run it in exactly one place.** Two schedulers posting the same payments will fight
> over the same state. If you migrate the job to another host, disable the old one.

---

## Reading the output

Payment counters, which are easy to misread:

| Line | Meaning |
|---|---|
| `N payment(s) skipped - already applied in Xero` | Xero already holds that allocation. Good. |
| `N payment(s) skipped - their invoices already show a zero balance in Xero` | Settled. Nothing to do. Good. |
| `N payment(s) SKIPPED by the crosswalk while their invoice still owes money` | **Local state says posted, Xero disagrees.** See [forget](#when-local-state-is-wrong). |
| `payments: N allocations from M payments` | What actually got posted. |

> **In a dry run, that third counter is meaningless.** Dry runs skip the step that fetches
> outstanding balances from Xero, so *every* payment already in the crosswalk falls into
> that bucket. A dry run showing a huge number there is an artifact, not a backlog. Check
> `Xero API calls used: 0` — no balances were fetched, so no such claim can be a fact.
> Use `unpaid` to see real Xero state.

### Refusals

A refusal means **nothing was posted** for that record. They are listed at the end of
every run and stored, so `report` can show them with timestamps.

- *"invoice … is dated BEFORE the window"* — the payment settles old receivables that
  were never imported. Usually benign; widen `sync.start_date` if you want them.
- *"invoice … is a draft/cancelled in Invoice Ninja"* — carries no ledger. Benign.
- *"is in scope and valid in Invoice Ninja … the crosswalk simply does not know its id"*
  — the invoice has not been posted yet. Usually resolves once the invoices step has
  run. Check `sync.crosswalk_path` points where you think it does.
- *"allocation X exceeds the Y still outstanding"* — Invoice Ninja and Xero disagree
  about how much is owed. If the customer genuinely overpaid by a trivial amount, pass
  `--cap-overpayment 1.00` to allocate only what fits. **The flag compares the
  *excess*, not the remaining balance** — so a large payment against a tiny residual
  balance will not be capped by a small value, and should not be. Those need a human.
- *Xero rejected: "contact number … already assigned"* / *"Invoice not of valid status
  for modification"* — conditions on the Xero side; fix them there.

> `report` timestamps each refusal. A refusal older than your last successful run may
> simply be a stale row that no longer reflects reality.

### When local state is wrong

If the crosswalk claims payments are posted but Xero still shows the invoices owing,
local state is untrustworthy — an older version could mark a rejected payment as posted,
and there is no way to tell from inside the crosswalk which those were.

```bash
in2xero forget payments     # drops LOCAL payment state only. Xero is untouched.
in2xero backfill --only payments
```

This is safe by construction: the next run re-checks every payment against Xero's
outstanding balances, and anything already settled is skipped, **so cash cannot be
applied twice.** It also clears stored payment refusals, which regenerate if still valid.

Back up the crosswalk first — `forget` is irreversible locally:

```bash
cp in2xero.sqlite in2xero.sqlite.bak
```

---

## Troubleshooting

**`config references ${X} but it is not set`** — the env file was not loaded. Under
systemd, check `EnvironmentFile=`; under cron, check `scripts/run_sync.sh` can read it.
This error is deliberate: the tool refuses to run with a blank credential.

**`multiple orgs connected - set xero.tenant_id`** — your app is authorised against more
than one organisation. Run `in2xero auth` and pin the right id.

**Payments refused with "not in Xero yet" for invoices you can see in Xero** — the
crosswalk does not know their ids. Confirm `sync.crosswalk_path`, then re-run; the
payments step re-derives ids from Xero before it allocates.

**Nothing is being imported and the counts look far too low** — see *Archived is not
void* above.

**Xero API limits** — every run prints `Xero API calls used: N (day remaining M)`. Xero
enforces a daily cap per tenant; large backfills are batched, and `preflight` estimates
the cost before you spend it.

---

## Development

```bash
./venv/bin/pip install -e .
python3 -m pytest tests/
```

The design notes in this repo (`Invoice Ninja → Xero sync — design.md`,
`in2xero-runbook.md`) are historical working documents from the original build. They
describe one specific organisation and are kept for provenance; this README is the
current reference.

---

## Licence

[MIT](LICENSE) — free for anyone to use, modify and redistribute, including
commercially, provided the copyright notice and licence text are retained.

Offered **as is, with no warranty and no liability**. See the
[disclaimer](#disclaimer--please-read) above, and please test against a Xero demo
company before pointing it at books that matter.
