"""A local SQLite copy of the Business Central rows that can never change.

Only immutable entries are kept here: general ledger entries, fixed-asset entries and
bank account entries. Once posted, those rows are fixed, so a period can be read once
and re-used.

A closed period is read from BC exactly once and then answered from this file, which
is what makes the month and year views cheap. The file is capped (see MAX_ROWS) and
the least recently used periods are dropped first, so it cannot grow without bound on
a small server.

What is deliberately NOT kept: supplier and customer ledger entries, and the balances
read from the chart of accounts. A supplier entry carries an Open flag and a remaining
amount that change the day the invoice is paid or applied, and the account balances are
recalculated by BC on every read. Caching those on disk would show yesterday's truth as
today's, which is exactly the kind of quiet error this application is built to avoid.

The file is a cache, not a source of truth: it can be deleted at any time and the
app will simply read everything again from BC.
"""
import json
import os
import sqlite3
import threading
import time

import config

DB_PATH = config.BASE_DIR / "data" / "cache.db"
_lock = threading.Lock()
_conn = None


def _connect():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS entries (
                kind TEXT NOT NULL, company TEXT NOT NULL, entry_no INTEGER NOT NULL,
                posting_date TEXT, payload TEXT NOT NULL,
                PRIMARY KEY (kind, company, entry_no)
            );
            CREATE INDEX IF NOT EXISTS entries_period
                ON entries (kind, company, posting_date);
            CREATE TABLE IF NOT EXISTS periods (
                kind TEXT NOT NULL, company TEXT NOT NULL,
                date_from TEXT NOT NULL, date_to TEXT NOT NULL,
                loaded_at REAL NOT NULL, rows INTEGER NOT NULL,
                PRIMARY KEY (kind, company, date_from, date_to)
            );
        """)
    return _conn


def rows(kind, company, date_from, date_to):
    """Everything stored for this period, oldest entry first."""
    with _lock:
        cur = _connect().execute(
            "SELECT payload FROM entries WHERE kind=? AND company=? "
            "AND posting_date >= ? AND posting_date <= ? ORDER BY entry_no",
            (kind, company, date_from, date_to))
        return [json.loads(r[0]) for r in cur.fetchall()]


def max_entry(kind, company, date_from=None, date_to=None):
    sql = "SELECT MAX(entry_no) FROM entries WHERE kind=? AND company=?"
    args = [kind, company]
    if date_from:
        sql += " AND posting_date >= ? AND posting_date <= ?"
        args += [date_from, date_to]
    with _lock:
        value = _connect().execute(sql, args).fetchone()[0]
    return int(value) if value is not None else None


def store(kind, company, rows_in):
    """Writes rows, ignoring ones already held. Returns how many were new."""
    if not rows_in:
        return 0
    payload = []
    for r in rows_in:
        entry_no = r.get("Entry_No")
        if entry_no is None:
            continue
        payload.append((kind, company, int(entry_no), str(r.get("Posting_Date") or "")[:10],
                        json.dumps(r, separators=(",", ":"))))
    if not payload:
        return 0
    with _lock:
        conn = _connect()
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO entries (kind, company, entry_no, posting_date, payload) "
                         "VALUES (?,?,?,?,?)", payload)
        conn.commit()
        written = conn.total_changes - before
        _evict_if_needed(conn)
        return written


def period_loaded(kind, company, date_from, date_to):
    with _lock:
        row = _connect().execute(
            "SELECT loaded_at, rows FROM periods WHERE kind=? AND company=? AND date_from=? AND date_to=?",
            (kind, company, date_from, date_to)).fetchone()
    return {"loaded_at": row[0], "rows": row[1]} if row else None


def mark_period(kind, company, date_from, date_to, row_count):
    with _lock:
        conn = _connect()
        conn.execute("INSERT OR REPLACE INTO periods (kind, company, date_from, date_to, loaded_at, rows) "
                     "VALUES (?,?,?,?,?,?)",
                     (kind, company, date_from, date_to, time.time(), row_count))
        conn.commit()


# The server this runs on is small, so the cache is bounded. At roughly 400 bytes a
# row this is about 120 MB of payload in the worst case, and far less in practice.
MAX_ROWS = int(os.environ.get("CACHE_MAX_ROWS", "300000"))


def _evict_if_needed(conn):
    """Drops whole periods, least recently used first, until the row cap is met.

    Periods are the unit because a half-loaded period would be read as complete and
    would quietly under-report; dropping it whole forces a clean re-read from BC.
    """
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    if total <= MAX_ROWS:
        return 0
    dropped = 0
    for kind, company, dfrom, dto in conn.execute(
            "SELECT kind, company, date_from, date_to FROM periods ORDER BY loaded_at ASC").fetchall():
        cur = conn.execute(
            "DELETE FROM entries WHERE kind=? AND company=? AND posting_date >= ? AND posting_date <= ?",
            (kind, company, dfrom, dto))
        conn.execute("DELETE FROM periods WHERE kind=? AND company=? AND date_from=? AND date_to=?",
                     (kind, company, dfrom, dto))
        dropped += cur.rowcount
        total -= cur.rowcount
        if total <= MAX_ROWS * 0.9:      # leave headroom so this runs rarely
            break
    conn.commit()
    return dropped


def stats():
    with _lock:
        conn = _connect()
        total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        by_kind = conn.execute(
            "SELECT kind, company, COUNT(*), MIN(posting_date), MAX(posting_date) "
            "FROM entries GROUP BY kind, company").fetchall()
        periods = conn.execute("SELECT COUNT(*) FROM periods").fetchone()[0]
    size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return {"rows": total, "max_rows": MAX_ROWS, "periods": periods,
            "file_mb": round(size / 1048576, 1), "path": str(DB_PATH),
            "detail": [{"kind": k, "company": c, "rows": n, "from": a, "to": b}
                       for k, c, n, a, b in by_kind]}


def forget(kind=None, company=None):
    """Drops what is stored. The app simply reads it again from BC."""
    with _lock:
        conn = _connect()
        if kind and company:
            conn.execute("DELETE FROM entries WHERE kind=? AND company=?", (kind, company))
            conn.execute("DELETE FROM periods WHERE kind=? AND company=?", (kind, company))
        else:
            conn.executescript("DELETE FROM entries; DELETE FROM periods;")
        conn.commit()
        conn.execute("VACUUM")
