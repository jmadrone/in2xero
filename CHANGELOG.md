# Changelog

All notable changes to in2xero are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

> **On the "v 4.3.1" commit message.** Commit `248433a` (2026-08-03) is titled
> "Updated to v 4.3.1", but the version it actually sets is **4.2.0** — the diff
> is `3.7.0` → `4.2.0` in both `pyproject.toml` and `in2xero/__init__.py`.
> **There has never been a 4.3.x release:** no tag, no GitHub release, and no
> `4.3.x` string in any tracked file in any commit. The title is a typo; the
> version has never regressed.
>
> 4.2.0 is still current because nothing under `in2xero/` or `tests/` has
> changed since that commit — every later commit is documentation, packaging or
> deployment. (Verified 2026-11-13: `git diff 248433a..HEAD -- in2xero/ tests/`
> is empty.)

## [Unreleased]

Documentation, packaging and deployment only. No change to the package itself,
so the version stays at 4.2.0.

### Added
- MIT `LICENSE`, and `license` declared in `pyproject.toml` (`698d91f`).
- Full user runbook as `README.md`, replacing the previous short version
  (`698d91f`), with two gaps closed afterwards (`e31ca05`).
- systemd units for scheduled syncs — `deploy/in2xero-sync.service`,
  `deploy/in2xero-sync.timer`, `deploy/in2xero.env.example` (`698d91f`).
- `scripts/run_sync.sh`, a wrapper for running the sync on a schedule
  (`6c40808`).
- 1Password integration instructions in the README and config example
  (`c85c343`).

### Changed
- `scripts/run_sync.sh` generalised from a Mac-specific launchd job — which
  hardcoded a path under `/Users` — into a portable cron wrapper whose paths are
  environment-overridable and default to `/opt/in2xero` and `/etc/in2xero/`.
  The README now recommends the systemd units in `deploy/` over cron
  (`698d91f`).
- `config.example.yaml` de-hardcoded: the baked-in Xero `tenant_id` replaced
  with a `${XERO_TENANT_ID}` placeholder (`698d91f`).

### Removed
- `in2xero.sqlite` untracked, and `.gitignore` extended to cover the local
  database and agent hooks (`6c40808`).
- `ninja_snapshot/ninja_snapshot.json` and `ninja_snapshot/ninja_snapshot copy.json`
  untracked (`7666a5d`), after `.gitignore` was extended to exclude the snapshot
  JSON (`4cd6461`). The scripts in `ninja_snapshot/` remain tracked.

## [4.2.0] — 2026-08-03

Committed as `248433a`, whose message misstates the version as "4.3.1".

Payment allocation reworked. Xero refuses a payment that exceeds an invoice's
outstanding balance, and one refusal voids its entire batch. Balances move as a
run applies payments, so a snapshot taken before the run went stale immediately:
two payments against one invoice both looked affordable and the second was
refused.

### Added
- `apply_against_remaining()` in `transform.py` — splits built allocations into
  accepted and over-allocated, decrementing a live `InvoiceID` → amount-owed map
  as it goes. Amounts are `Decimal` throughout; mixing `float` and `Decimal`
  raises `TypeError` rather than rounding silently.
- `--cap-overpayment AMOUNT` — when a payment exceeds the invoice balance by up
  to `AMOUNT`, allocate only what fits and leave the rest. Off by default: the
  tool refuses rather than reshape cash.
- Distinct diagnostics for the overpayment cases a human must tell apart — an
  allocation that duplicates one Xero already shows as paid, a customer who
  overpaid by under a unit, and a genuine Invoice Ninja/Xero disagreement.
- `in2xero forget <payments|invoices|contacts>` (`cmd_forget`, backed by
  `Crosswalk.forget`) — drops local crosswalk mappings and refusal rows for one
  kind so they are re-derived from Xero. Touches nothing in Xero. Needed because
  local state can be wrong: a pre-3.6 run marked rejected payments as posted,
  and nothing inside the crosswalk can identify which.
- Transform test coverage for the allocation paths (`tests/test_transform.py`).

### Changed
- `cli.py` sync and backfill paths moved onto the new allocation logic.
- `crosswalk.py` gained `forget()`.

## [3.7.0] — 2026-07-30

First tracked version (`e73fefd`): Invoice Ninja → Xero backfill and incremental
sync, with the three invariants documented in `in2xero/__init__.py` — never send
Invoice Ninja's `status=active` filter (it silently drops archived records, which
are most of this org's invoices), settle payments to a clearing account rather
than the fed bank account, and leave expenses unimplemented.

[Unreleased]: https://github.com/jmadrone/in2xero/compare/248433a...HEAD
[4.2.0]: https://github.com/jmadrone/in2xero/compare/e73fefd...248433a
[3.7.0]: https://github.com/jmadrone/in2xero/commit/e73fefd
