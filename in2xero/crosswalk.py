"""SQLite crosswalk: Invoice Ninja id -> Xero id.

This is the only thing standing between a re-run and a duplicated ledger. Xero
cannot dedupe for us: ACCPAY numbers are not unique, payments have no natural
key, and an AUTHORISED invoice can never be deleted once posted - only voided.

It is also the resume checkpoint. A run that stops on the daily rate limit picks
up exactly where it left off.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS crosswalk (
    kind        TEXT NOT NULL,          -- contact | invoice | payment | credit
    ninja_id    TEXT NOT NULL,
    xero_id     TEXT NOT NULL,
    posted_at   TEXT NOT NULL DEFAULT (datetime('now')),
    note        TEXT,
    PRIMARY KEY (kind, ninja_id)
);
CREATE INDEX IF NOT EXISTS ix_crosswalk_xero ON crosswalk(kind, xero_id);

CREATE TABLE IF NOT EXISTS watermark (
    step        TEXT PRIMARY KEY,
    -- run START, never run end: a record modified mid-run must be caught next
    -- pass rather than skipped forever.
    started_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refused (
    kind        TEXT NOT NULL,
    ninja_id    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (kind, ninja_id)
);
"""


class Crosswalk:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, kind, ninja_id):
        with closing(self.db.execute(
            "SELECT xero_id FROM crosswalk WHERE kind=? AND ninja_id=?", (kind, str(ninja_id))
        )) as cur:
            row = cur.fetchone()
        return row[0] if row else None

    def put(self, kind, ninja_id, xero_id, note=None):
        self.db.execute(
            "INSERT OR REPLACE INTO crosswalk(kind, ninja_id, xero_id, note) VALUES (?,?,?,?)",
            (kind, str(ninja_id), str(xero_id), note),
        )
        self.db.commit()

    def put_many(self, kind, pairs):
        self.db.executemany(
            "INSERT OR REPLACE INTO crosswalk(kind, ninja_id, xero_id) VALUES (?,?,?)",
            [(kind, str(a), str(b)) for a, b in pairs],
        )
        self.db.commit()

    def known(self, kind):
        with closing(self.db.execute(
            "SELECT ninja_id, xero_id FROM crosswalk WHERE kind=?", (kind,)
        )) as cur:
            return dict(cur.fetchall())

    def notes_matching(self, kind, needle):
        """ninja_ids whose note contains needle. Used to skip already-settled work."""
        with closing(self.db.execute(
            "SELECT ninja_id FROM crosswalk WHERE kind=? AND note LIKE ?",
            (kind, f"%{needle}%"),
        )) as cur:
            return {r[0] for r in cur.fetchall()}

    def refuse(self, kind, ninja_id, reason):
        self.db.execute(
            "INSERT OR REPLACE INTO refused(kind, ninja_id, reason) VALUES (?,?,?)",
            (kind, str(ninja_id), reason),
        )
        self.db.commit()

    def refusals(self):
        with closing(self.db.execute(
            "SELECT kind, ninja_id, reason, seen_at FROM refused ORDER BY seen_at"
        )) as cur:
            return cur.fetchall()

    def counts(self):
        with closing(self.db.execute(
            "SELECT kind, COUNT(*) FROM crosswalk GROUP BY kind"
        )) as cur:
            return dict(cur.fetchall())

    def mark_run_start(self, step, started_at):
        self.db.execute(
            "INSERT OR REPLACE INTO watermark(step, started_at) VALUES (?,?)", (step, started_at)
        )
        self.db.commit()

    def watermark(self, step):
        with closing(self.db.execute(
            "SELECT started_at FROM watermark WHERE step=?", (step,)
        )) as cur:
            row = cur.fetchone()
        return row[0] if row else None
