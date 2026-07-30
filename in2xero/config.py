"""Config loading and validation.

Fail loudly at startup rather than halfway through a posting run - a run that dies
after 140 invoices leaves a half-built ledger, and while the crosswalk makes that
recoverable it is still an afternoon nobody wanted.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Optional

import yaml


class ConfigError(Exception):
    pass


@dataclass
class NinjaConfig:
    base_url: str
    api_token: str
    verify_tls: bool = True
    page_size: int = 200


@dataclass
class XeroConfig:
    auth_mode: str                      # "custom_connection" | "auth_code"
    client_id: str
    client_secret: str
    tenant_id: str = ""
    refresh_token_path: str = "xero_refresh_token.txt"

    # Must be a subset of what the Custom Connection app actually has ticked.
    # Blank = the tool's default set.
    scopes: str = ""

    # Account codes. Names are known from the org's chart of accounts; the CODES
    # must be read off Accounting -> Advanced -> Chart of accounts and set here.
    sales_account_code: str = ""        # e.g. "4300" - where invoice lines land
    rounding_account_code: str = ""     # e.g. "7050" - sub-cent differences

    # Xero does NOT expose a Code field on bank-type accounts - the UI has no
    # place to put one and most bank accounts genuinely have none. Payments must
    # therefore reference the clearing account by AccountID (a GUID).
    # Run `in2xero accounts` to list bank accounts and their GUIDs.
    clearing_account_id: str = ""
    clearing_account_code: str = ""     # only if the account really does have one

    batch_size: int = 50
    daily_call_floor: int = 50          # stop when X-DayLimit-Remaining drops below


@dataclass
class SyncConfig:
    start_date: str = "2025-01-01"
    end_date: str = ""                  # blank = today
    steps: list = field(default_factory=lambda: ["contacts", "invoices", "payments"])
    tax_mode: str = "none"              # "none" | "resolve"
    crosswalk_path: str = "in2xero.sqlite"
    dry_run: bool = False


@dataclass
class Config:
    ninja: NinjaConfig
    xero: XeroConfig
    sync: SyncConfig


def _req(d: dict, key: str, where: str):
    v = d.get(key)
    if v in (None, ""):
        raise ConfigError(f"{where}.{key} is required")
    return v


def _env_expand(v):
    """Allow ${ENV_VAR} in any string value so secrets stay out of the YAML."""
    if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
        name = v[2:-1]
        got = os.environ.get(name)
        if got is None:
            raise ConfigError(f"config references ${{{name}}} but it is not set")
        return got
    if isinstance(v, dict):
        return {k: _env_expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_env_expand(x) for x in v]
    return v


def _anchor_path(value: str, config_path: str) -> str:
    """Resolve a relative path against the CONFIG file, not the shell's cwd.

    The crosswalk is the only record of what has been posted. Resolving it from
    cwd means running the same command from a different directory silently starts
    from an empty crosswalk - which looks exactly like "nothing has been synced"
    and re-refuses every payment.
    """
    if os.path.isabs(value):
        return value
    return os.path.join(os.path.dirname(os.path.abspath(config_path)) or ".", value)


def load(path: str) -> Config:
    if not os.path.exists(path):
        raise ConfigError(f"no config at {path} - copy config.example.yaml and edit it")
    with open(path) as fh:
        raw = _env_expand(yaml.safe_load(fh) or {})

    n = raw.get("ninja") or {}
    x = raw.get("xero") or {}
    s = raw.get("sync") or {}

    base = str(_req(n, "base_url", "ninja")).rstrip("/")
    if base.endswith("/api/v1"):
        base = base[: -len("/api/v1")]
    if not base.startswith(("http://", "https://")):
        raise ConfigError("ninja.base_url must start with http:// or https://")

    ninja = NinjaConfig(
        base_url=base,
        api_token=str(_req(n, "api_token", "ninja")),
        verify_tls=bool(n.get("verify_tls", True)),
        page_size=int(n.get("page_size", 200)),
    )

    mode = str(x.get("auth_mode", "custom_connection"))
    if mode not in ("custom_connection", "auth_code"):
        raise ConfigError("xero.auth_mode must be custom_connection or auth_code")

    xero = XeroConfig(
        auth_mode=mode,
        client_id=str(_req(x, "client_id", "xero")),
        client_secret=str(_req(x, "client_secret", "xero")),
        tenant_id=str(x.get("tenant_id", "")),
        refresh_token_path=str(x.get("refresh_token_path", "xero_refresh_token.txt")),
        scopes=" ".join(x["scopes"]) if isinstance(x.get("scopes"), list)
               else str(x.get("scopes", "")),
        sales_account_code=str(x.get("sales_account_code", "")),
        clearing_account_id=str(x.get("clearing_account_id", "")),
        clearing_account_code=str(x.get("clearing_account_code", "")),
        rounding_account_code=str(x.get("rounding_account_code", "")),
        batch_size=int(x.get("batch_size", 50)),
        daily_call_floor=int(x.get("daily_call_floor", 50)),
    )

    sync = SyncConfig(
        start_date=str(s.get("start_date", "2025-01-01")),
        end_date=str(s.get("end_date", "") or ""),
        steps=list(s.get("steps", ["contacts", "invoices", "payments"])),
        tax_mode=str(s.get("tax_mode", "none")),
        crosswalk_path=_anchor_path(str(s.get("crosswalk_path", "in2xero.sqlite")), path),
        dry_run=bool(s.get("dry_run", False)),
    )

    bad = [st for st in sync.steps if st not in ("contacts", "invoices", "payments", "credits")]
    if bad:
        raise ConfigError(f"unknown sync.steps: {bad}. Expenses are out of scope by design.")

    return Config(ninja=ninja, xero=xero, sync=sync)


def clearing_ref(cfg: Config) -> dict:
    """How Xero should be told which account a payment settles into.

    AccountID wins: bank accounts usually have no Code at all.
    """
    if cfg.xero.clearing_account_id:
        return {"AccountID": cfg.xero.clearing_account_id}
    if cfg.xero.clearing_account_code:
        return {"Code": cfg.xero.clearing_account_code}
    return {}


def require_posting_accounts(cfg: Config):
    """Only needed when actually posting, not for preflight or dry runs."""
    problems = []
    if not cfg.xero.sales_account_code:
        problems.append(
            "xero.sales_account_code is not set. Accounting -> Advanced -> Chart of "
            "accounts; for this org that is 4300 (Service)."
        )
    if not clearing_ref(cfg):
        problems.append(
            "no clearing account set. Xero does not give bank accounts a Code, so set\n"
            "  xero.clearing_account_id: <GUID>\n"
            "Run `in2xero accounts` to list your bank accounts and their GUIDs."
        )
    if problems:
        raise ConfigError("cannot post yet:\n  - " + "\n  - ".join(problems))
