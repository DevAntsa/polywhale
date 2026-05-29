"""SQLite connection and migration runner."""

import sqlite3
import time
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(
    db_path: Path,
    *,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open a connection with WAL mode, foreign keys on, Row factory.

    Set check_same_thread=False for daemons (whale-ws) that need to share
    one connection across an asyncio event loop + worker threads. WAL mode
    makes this safe for our concurrent read + occasional serialized write
    pattern.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    conn.commit()
    return conn


def run_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply any unapplied SQL files from the migrations directory.

    Returns the list of newly applied migration versions, in order.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    newly_applied: list[int] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, int(time.time())),
        )
        conn.commit()
        newly_applied.append(version)
    return newly_applied
