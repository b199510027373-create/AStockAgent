from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BASE_DIR

DB_PATH = Path(os.getenv("ASTOCK_DB_PATH") or (BASE_DIR / "data" / "astock.db"))
REC_DIR = BASE_DIR / "data" / "recommendations"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    schema_version INTEGER,
    kind TEXT,
    data_as_of TEXT,
    tradable_from TEXT,
    generated_at TEXT,
    source TEXT,
    status TEXT,
    error TEXT,
    params_json TEXT,
    diagnostics_json TEXT,
    json_path TEXT,
    csv_path TEXT
);
CREATE TABLE IF NOT EXISTS items (
    run_id TEXT NOT NULL,
    rank INTEGER,
    code TEXT,
    name TEXT,
    industry TEXT,
    composite REAL,
    technical REAL,
    event REAL,
    action TEXT,
    reason TEXT,
    PRIMARY KEY (run_id, rank)
);
CREATE TABLE IF NOT EXISTS evidence (
    run_id TEXT NOT NULL,
    code TEXT,
    kind TEXT,
    pub_time TEXT,
    title TEXT,
    url TEXT,
    source TEXT,
    content TEXT,
    PRIMARY KEY (run_id, code, kind, pub_time, title)
);
CREATE TABLE IF NOT EXISTS jobs (
    job_key TEXT PRIMARY KEY,
    trade_date TEXT,
    slot TEXT,
    status TEXT,
    attempts INTEGER DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    late_by_min REAL,
    run_id TEXT
);
CREATE TABLE IF NOT EXISTS tracking (
    run_id TEXT NOT NULL,
    code TEXT,
    horizon INTEGER,
    entry_date TEXT,
    exit_date TEXT,
    ret REAL,
    excess REAL,
    computed_at TEXT,
    PRIMARY KEY (run_id, code, horizon)
);
CREATE INDEX IF NOT EXISTS idx_runs_asof ON runs(data_as_of);
CREATE INDEX IF NOT EXISTS idx_items_code ON items(code);
CREATE INDEX IF NOT EXISTS idx_evidence_code ON evidence(code);
"""


def _db_path() -> Path:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DB_PATH


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


@contextmanager
def _read():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def save_run(payload: dict, json_path: str | None = None, csv_path: str | None = None) -> str | None:
    """Persist one recommendation payload. Idempotent by run_id."""
    run_id = str(payload.get("run_id") or "").strip()
    if not run_id:
        return None
    table = payload.get("table") or []
    evidence = payload.get("evidence") or {}
    with transaction() as conn:
        conn.execute("DELETE FROM items WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM evidence WHERE run_id=?", (run_id,))
        conn.execute(
            """INSERT OR REPLACE INTO runs
               (run_id, schema_version, kind, data_as_of, tradable_from, generated_at,
                source, status, error, params_json, diagnostics_json, json_path, csv_path)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                payload.get("schema_version"),
                payload.get("kind", "recommendation"),
                payload.get("data_as_of") or payload.get("as_of"),
                payload.get("tradable_from"),
                payload.get("generated_at"),
                payload.get("source", "live"),
                payload.get("status", "success"),
                payload.get("error"),
                _json(payload.get("params")),
                _json(payload.get("diagnostics")),
                json_path,
                csv_path,
            ),
        )
        for row in table:
            conn.execute(
                """INSERT OR REPLACE INTO items
                   (run_id, rank, code, name, industry, composite, technical, event, action, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    row.get("排名"),
                    row.get("代码"),
                    row.get("名称"),
                    row.get("行业"),
                    row.get("综合分"),
                    row.get("技术分"),
                    row.get("事件分"),
                    row.get("建议"),
                    row.get("理由"),
                ),
            )
        for code, items in evidence.items():
            for ev in items or []:
                conn.execute(
                    """INSERT OR REPLACE INTO evidence
                       (run_id, code, kind, pub_time, title, url, source, content)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        code,
                        ev.get("kind"),
                        ev.get("pub_time"),
                        ev.get("title"),
                        ev.get("url"),
                        ev.get("source"),
                        ev.get("content"),
                    ),
                )
    return run_id


def save_job(
    job_key: str,
    *,
    trade_date: str | None = None,
    slot: str | None = None,
    status: str = "pending",
    attempts: int = 0,
    started_at: str | None = None,
    finished_at: str | None = None,
    error: str | None = None,
    late_by_min: float | None = None,
    run_id: str | None = None,
) -> None:
    with transaction() as conn:
        conn.execute(
            """INSERT INTO jobs
               (job_key, trade_date, slot, status, attempts, started_at, finished_at,
                error, late_by_min, run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_key) DO UPDATE SET
                 trade_date=excluded.trade_date, slot=excluded.slot,
                 status=excluded.status, attempts=excluded.attempts,
                 started_at=excluded.started_at, finished_at=excluded.finished_at,
                 error=excluded.error, late_by_min=excluded.late_by_min,
                 run_id=excluded.run_id""",
            (
                job_key,
                trade_date,
                slot,
                status,
                int(attempts or 0),
                started_at,
                finished_at,
                error,
                late_by_min,
                run_id,
            ),
        )


def get_job(job_key: str) -> dict | None:
    with _read() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_key=?", (job_key,)).fetchone()
        return dict(row) if row else None


def get_run(run_id: str) -> dict | None:
    with _read() as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not run:
            return None
        out = dict(run)
        out["params"] = json.loads(out.pop("params_json") or "{}")
        out["diagnostics"] = json.loads(out.pop("diagnostics_json") or "{}")
        out["items"] = [dict(x) for x in conn.execute(
            "SELECT * FROM items WHERE run_id=? ORDER BY rank", (run_id,)
        )]
        out["evidence"] = [dict(x) for x in conn.execute(
            "SELECT * FROM evidence WHERE run_id=?", (run_id,)
        )]
        return out


def latest_run() -> dict | None:
    with _read() as conn:
        row = conn.execute(
            "SELECT run_id FROM runs ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
    return get_run(row["run_id"]) if row else None


def list_runs(limit: int = 50) -> list[dict]:
    with _read() as conn:
        rows = conn.execute(
            "SELECT run_id, data_as_of, tradable_from, generated_at, source, status "
            "FROM runs ORDER BY generated_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(x) for x in rows]


def recommendation_batches(limit: int = 500) -> list[dict]:
    """Load recommendation runs + items in the shape tracker expects."""
    with _read() as conn:
        runs = conn.execute(
            "SELECT run_id, data_as_of, tradable_from, generated_at, source "
            "FROM runs ORDER BY generated_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        out = []
        for run in runs:
            items = conn.execute(
                "SELECT rank, code, name, industry, composite, technical, event, action, reason "
                "FROM items WHERE run_id=? ORDER BY rank",
                (run["run_id"],),
            ).fetchall()
            if not items:
                continue
            out.append(
                {
                    "path": None,
                    "run_id": run["run_id"],
                    "as_of": run["data_as_of"],
                    "tradable_from": run["tradable_from"],
                    "generated_at": run["generated_at"] or "",
                    "source": run["source"] or "live",
                    "rows": [
                        {
                            "排名": r["rank"],
                            "代码": r["code"],
                            "名称": r["name"],
                            "行业": r["industry"],
                            "综合分": r["composite"],
                            "技术分": r["technical"],
                            "事件分": r["event"],
                            "建议": r["action"],
                            "理由": r["reason"],
                        }
                        for r in items
                    ],
                }
            )
        return out


def tracking_rows(horizon: int = 5) -> list[dict]:
    """Read realised forward returns of one horizon for live runs only.

    The alpha banner aggregates these rows with :mod:`astock.stats`; it never
    reloads prices, so the banner stays cheap and matches the tracking tab.
    ``rank`` comes from the run items so Top2 vs rest can be split without
    re-reading the recommendation payload.
    """
    with _read() as conn:
        rows = conn.execute(
            """
            SELECT t.run_id, r.data_as_of, t.code, t.entry_date, t.exit_date,
                   t.ret, t.excess, i.rank
            FROM tracking t
            JOIN runs r ON r.run_id = t.run_id
            LEFT JOIN items i ON i.run_id = t.run_id AND i.code = t.code
            WHERE t.horizon = ? AND COALESCE(r.source, 'live') = 'live'
            ORDER BY r.data_as_of, t.run_id, i.rank
            """,
            (int(horizon),),
        ).fetchall()
        return [dict(row) for row in rows]


def save_tracking(run_id: str, rows: list[dict]) -> None:
    """rows: code, horizon, entry_date, exit_date, ret, excess."""
    now = datetime.now().isoformat(timespec="seconds")
    with transaction() as conn:
        for row in rows:
            conn.execute(
                """INSERT OR REPLACE INTO tracking
                   (run_id, code, horizon, entry_date, exit_date, ret, excess, computed_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    row.get("code"),
                    row.get("horizon"),
                    row.get("entry_date"),
                    row.get("exit_date"),
                    row.get("ret"),
                    row.get("excess"),
                    now,
                ),
            )


def sync_json_dir(path: Path | str | None = None) -> int:
    """One-shot import of existing recommendation JSON files."""
    folder = Path(path or REC_DIR)
    if not folder.exists():
        return 0
    count = 0
    for file in sorted(folder.glob("recommend_*.json")):
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
            if not payload.get("run_id"):
                # 老文件没有 run_id，补一个稳定 ID，避免覆盖
                payload["run_id"] = f"legacy_{file.stem}"
            if save_run(payload, json_path=str(file)):
                count += 1
        except Exception:
            continue
    return count


def stats() -> dict:
    with _read() as conn:
        return {
            "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            "items": conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            "evidence": conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "tracking": conn.execute("SELECT COUNT(*) FROM tracking").fetchone()[0],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="AStockAgent SQLite ledger")
    parser.add_argument("action", choices=["init", "sync", "stats", "list"])
    parser.add_argument("--path", default=None)
    args = parser.parse_args()
    if args.action == "init":
        with _read():
            pass
        print(f"initialized {_db_path()}")
    elif args.action == "sync":
        print(f"imported {sync_json_dir(args.path)} runs")
    elif args.action == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    elif args.action == "list":
        for row in list_runs(20):
            print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
