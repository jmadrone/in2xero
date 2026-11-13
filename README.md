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

## How it works

### A run, step by step

Three steps, in this order, each depending on the one before:

1. **Contacts.** Only the clients referenced by in-scope invoices are created — not
   your whole client list. Creating every client pollutes Xero permanently for no gain.
   Clients already in the crosswalk are skipped.
2. **Invoices.** Posted as ACCREC (sales) invoices. Before posting, the step reads what
   Xero already holds for the window and reconciles against it. Clients that exist in
   Xero but are missing from the crosswalk are adopted rather than duplicated.
3. **Payments.** Allocated against those invoices, settling into the clearing account.

`--only` overrides the configured `sync.steps` for a single run.

`sync` and `backfill` are **the same code path** — `sync` simply calls `backfill`. The
window always comes from `sync.start_date`/`end_date`; there is no hidden "only look at
recent changes" mode. Incrementality comes entirely from the crosswalk skipping what it
already posted, and from re-checking Xero. That is why a missed day costs nothing and
why re-running is safe.

### The reconciliation guard

This is the part that matters. Every document is **rebuilt line by line** and the
recomputed total compared against Invoice Ninja's own `amount`. If they differ by more
than a cent the document is **refused, not reshaped** — silently massaging a total to
make it fit is how AR quietly stops meaning anything.

Sub-cent drift (Invoice Ninja rounds per line, Xero rounds differently) is absorbed by
an **explicit rounding line**, so the difference is visible in the ledger rather than
hidden. That line is coded to `xero.rounding_account_code`, or to the sales account if
you leave it blank.

Invoices are always posted **AUTHORISED**, never draft. A draft creates no journal in
Xero, so an importer that posts drafts "for safety" produces an empty balance sheet.

### What gets imported

| | Imported | Ignored |
|---|---|---|
| Invoice status | sent, partial, paid | draft, cancelled, reversed — no ledger to restate |
| Payment status | the settled states (`status_id` 4, 5 and 6) | pending and failed — not cash |
| Lifecycle | active **and archived** | deleted only |

Archived records are imported deliberately — see [Archived is not
void](#the-three-things-that-make-this-non-obvious).

### How records are recognised

Each posted record carries a stable derived identifier, so a re-run recognises its own
work:

| | Xero field | Value |
|---|---|---|
| Contact | `ContactNumber` | `IN-C-<ninja client id>` |
| Invoice | `Reference` | `IN-<ninja invoice id>` |
| Payment | `Reference` | `IN-P-<ninja payment id>` |

An invoice is matched on that reference first, then by **invoice number**
(case-insensitive). A match is not automatically a refusal — what happens depends on the
status Xero reports:

| Xero status | Outcome |
|---|---|
| `AUTHORISED` | **Adopted** into the crosswalk, so its payments can still settle. |
| `PAID` | Adopted, but never paid again — it is already settled. |
| `DRAFT` / `SUBMITTED` | A real blocker: Xero cannot apply a payment to a draft. |
| `DELETED` / `VOIDED` | Ignored, and does **not** count as a duplicate. |

That last row matters more than it looks. Xero keeps deleted invoices, and a deleted
draft can carry the same number as the live invoice that replaced it. Matches are
therefore **ranked by status** so a dead record can never shadow a live one — otherwise
the number resolves to the deleted copy, the live invoice is never adopted, and every
payment against it is refused as "not in Xero yet" while it sits there plainly visible.

Contacts are adopted the same way, by looking up `ContactNumber` `IN-C-<id>` in Xero —
not by matching names. Without that, a run from a fresh crosswalk would refuse every
invoice for a client whose contact already exists.

### The crosswalk

A local SQLite database at `sync.crosswalk_path`, with three tables:

| Table | Holds |
|---|---|
| `crosswalk` | the Invoice Ninja id → Xero id map, per kind, with a timestamp |
| `refused` | each refusal and when it was last seen, so `report` can list them |
| `watermark` | when each step last *started* — shown by `report`, never used to narrow the window |

The watermark stores run **start**, not end, on purpose: a record modified mid-run must
be caught on the next pass rather than skipped forever.

A **relative** `crosswalk_path` resolves against the directory holding the config file,
not the shell's working directory. Running the same command from elsewhere would
otherwise start from an empty crosswalk — which looks exactly like "nothing has been
synced" and re-refuses every payment. Use an absolute path for scheduled runs anyway.

### Talking to both ends safely

- **Batching.** Documents post in batches of `xero.batch_size`. If Xero rejects some
  elements, those are dropped and the rest retried, so one bad document does not void
  the batch.
- **Idempotency.** Each batch carries an `Idempotency-Key` derived from a hash of its
  contents, so a retried identical batch cannot double-post.
- **Rate limits.** Xero caps calls per minute and per day. The client watches
  `X-DayLimit-Remaining` and stops cleanly at `xero.daily_call_floor` rather than dying
  mid-batch. Every run prints the calls used.
- **No redirects.** A redirect drops the auth header, so a 3xx from Invoice Ninja is
  reported as a `base_url` problem instead of being followed.
- **Pagination without the status filter.** Index pages are walked oldest-first and each
  row is classified from its own `archived_at`/`is_deleted` fields.

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
  Tick these five scopes on the Custom Connection — they are what the tool asks for by
  default:

  | Scope | Needed for |
  |---|---|
  | `accounting.invoices` | creating invoices |
  | `accounting.payments` | settling payments against them |
  | `accounting.contacts` | creating customer contacts |
  | `accounting.settings.read` | reading the chart of accounts (`accounting.settings` also works) |
  | `accounting.banktransactions.read` | explaining the clearing balance |

  Custom Connections use **granular** scopes. The aggregate `accounting.transactions`
  that standard OAuth apps use is not on their list at all, and asking for it fails with
  `invalid_scope`. `in2xero auth --probe-scopes` reports which ones your app actually
  holds.

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

For a server install, the units in [`deploy/`](deploy/) expect the code and its
virtualenv under `/opt/in2xero`:

```bash
sudo git clone https://github.com/jmadrone/in2xero.git /opt/in2xero/src
sudo python3 -m venv /opt/in2xero/venv
sudo /opt/in2xero/venv/bin/pip install /opt/in2xero/src
```

On Debian/Ubuntu, `python3 -m venv` can fail with *"ensurepip is not available"* — the
stdlib `venv` module imports fine but the package is split out. Install it:

```bash
sudo apt install python3-venv     # or e.g. python3.12-venv for a specific version
```

`pip install .` puts the `in2xero` binary inside the virtualenv, not on your `PATH`.
Either activate the venv (`source venv/bin/activate`) or call it by full path — which is
what the systemd units and `scripts/run_sync.sh` do:

```bash
./venv/bin/in2xero preflight
/opt/in2xero/venv/bin/in2xero -c /etc/in2xero/config.yaml sync
```

## Updating

```bash
sudo git -C /opt/in2xero/src pull
sudo /opt/in2xero/venv/bin/pip install /opt/in2xero/src
```

For a local checkout:

```bash
git pull
./venv/bin/pip install .
```

Reinstalling from the directory picks up changed code **even when the version number has
not changed**, so you do not need `--force-reinstall`. Nothing in the crosswalk or your
config is touched. If the tree is root-owned, both commands need `sudo`.

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

<details>
<summary><strong>Full reference</strong> — every key, with its default</summary>

| Key | Default | Notes |
|---|---|---|
| `ninja.base_url` | *required* | A trailing `/api/v1` is stripped for you. Must start with `http://` or `https://`. |
| `ninja.api_token` | *required* | Invoice Ninja returns **403**, not 401, for a bad token. |
| `ninja.verify_tls` | `true` | `false` only for a self-signed certificate. |
| `ninja.page_size` | `200` | Records per index page. |
| `xero.auth_mode` | `custom_connection` | Or `auth_code`. |
| `xero.client_id` | *required* | |
| `xero.client_secret` | *required* | |
| `xero.tenant_id` | *(empty)* | Required once the app is authorised against more than one org. |
| `xero.scopes` | *(empty)* | Blank uses the tool's default set. A list or a space-separated string. Custom Connections need **granular** scopes — the aggregate `accounting.transactions` is rejected as `invalid_scope`. |
| `xero.refresh_token_path` | `xero_refresh_token.txt` | `auth_code` mode only. **Relative paths are *not* anchored to the config file** — set an absolute path. |
| `xero.sales_account_code` | *(empty)* | Required to post. Where invoice lines land. |
| `xero.rounding_account_code` | *(empty)* | Absorbs sub-cent drift as a visible line. |
| `xero.clearing_account_id` | *(empty)* | GUID. Required to post, unless the account has a code. |
| `xero.clearing_account_code` | *(empty)* | Only if your bank account really has a code; most do not. |
| `xero.batch_size` | `50` | Documents per API call. |
| `xero.daily_call_floor` | `50` | Stop once `X-DayLimit-Remaining` reaches this. |
| `sync.start_date` | `2025-01-01` | |
| `sync.end_date` | *(empty)* | Blank means today. |
| `sync.steps` | `[contacts, invoices, payments]` | See the note below. |
| `sync.tax_mode` | `none` | `none` posts every line untaxed. `resolve` maps each Invoice Ninja tax **name** to a Xero `TaxType` by name, case-insensitively, and **refuses** the document if no Xero rate matches — Xero's tax codes are per-organisation, so they cannot be hardcoded. |
| `sync.crosswalk_path` | `in2xero.sqlite` | Relative paths resolve against the config file's directory. |
| `sync.dry_run` | `false` | Global safety switch. |

Missing-but-required keys, an unknown `auth_mode`, an unknown step, or a `${VAR}` that
is not set all fail **at startup**, before anything is posted.

The **account codes** are checked only when actually posting, so `preflight` and
`--dry-run` both run before your chart of accounts is wired up. Credentials are a
different matter: `client_id` and `client_secret` must be present for any command to
load its config, and `preflight` does authenticate against Xero (it reports
`AUTH FAILED` rather than exiting). Only `--dry-run` makes no Xero calls at all.

One consequence worth knowing: a dry run has no Xero tax rates to resolve against, so
`tax_mode: resolve` will refuse every taxed invoice in a dry run. That is an artifact of
the dry run, not a problem with your data.

</details>

> **`sync.steps` accepts `credits`, but nothing implements it.** Only `contacts`,
> `invoices` and `payments` ever run, so listing `credits` passes validation and then
> silently does nothing. Invoice Ninja records "apply a credit to an invoice" *as a
> payment*; those are detected and **refused** rather than posted as cash, and
> `preflight` counts them separately.

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

> **First run, before you know your tenant id.** The example config reads
> `tenant_id: ${XERO_TENANT_ID}`, and a `${VAR}` that is *unset* aborts at startup — so
> you cannot run `in2xero auth` to discover the id while the line is unresolvable. Set
> it empty (`XERO_TENANT_ID=`) for that first call: an empty value loads fine and is
> treated as "not pinned". Then put the real id in and keep it, which is what makes the
> tool refuse to guess when an app is authorised against several organisations.

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

Global flags, valid before any subcommand:

- `-c PATH` / `--config PATH` — config file to use. Defaults to `config.yaml` in the
  working directory, and **the crosswalk's relative path is resolved against this
  file's directory**.
- `--version` — print the version and exit.

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

The units assume the binary is at `/opt/in2xero/venv/bin/in2xero`, the config at
`/etc/in2xero/config.yaml` and state under `/var/lib/in2xero`. If you installed
elsewhere, adjust `ExecStart`, `WorkingDirectory` and `ReadWritePaths` to match.

> **If you use `auth_code` mode**, set `xero.refresh_token_path` to an absolute path
> inside the service's writable state directory (e.g.
> `/var/lib/in2xero/xero_refresh_token.txt`). It defaults to a *relative* path, and the
> unit runs with `ProtectSystem=strict`, so a rotated token written anywhere outside
> `ReadWritePaths` will fail — and a refresh token that cannot be persisted bricks the
> connection. This is one more reason to prefer `custom_connection` for scheduled runs.

**cron.** Use `scripts/run_sync.sh`, which sources the env file:

```cron
0 7 * * * /opt/in2xero/src/scripts/run_sync.sh >> /var/log/in2xero.log 2>&1
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
- *"credit application, not cash"* — Invoice Ninja stores an applied credit as a
  payment. It is not cash received, so it is not posted to the clearing account; it
  needs a credit note in Xero instead.
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
./venv/bin/python tests/test_transform.py
```

The transform tests are a plain script — no pytest, no network and no credentials. They
print one line per assertion, keep going after a failure so you see every problem in one
pass, and exit non-zero if any assertion failed.

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
