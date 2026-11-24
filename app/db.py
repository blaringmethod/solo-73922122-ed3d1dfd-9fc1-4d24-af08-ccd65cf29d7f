# -*- coding: utf-8 -*-
"""SQLite 持久化层。

保存三类数据：
1. 上传宽表的原始输入（wide_rows，按作品/题名/展名归并后的组合次数）；
2. 馆员按作品登记/更正的两类底账总条数（ledgers）；
3. 每次核对/下载所基于的已提交版本快照（snapshots、snapshot_works），
   核对期间允许继续补登，而核对结论与下载内容始终对应同一版本。
"""
from __future__ import annotations

import csv
import io
import sqlite3
from contextlib import contextmanager
from typing import Dict, Iterable, List, Optional, Tuple

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wide_rows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   INTEGER NOT NULL,
    work_no    TEXT NOT NULL,
    title_name TEXT,
    exhib_name TEXT,
    combo_cnt  INTEGER NOT NULL CHECK (combo_cnt > 0)
);
CREATE INDEX IF NOT EXISTS idx_wide_work ON wide_rows(batch_id, work_no);

CREATE TABLE IF NOT EXISTS ledgers (
    work_no      TEXT PRIMARY KEY,
    title_total  INTEGER CHECK (title_total IS NULL OR title_total >= 0),
    exhib_total  INTEGER CHECK (exhib_total IS NULL OR exhib_total >= 0),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    kind        TEXT NOT NULL,            -- check | download
    batch_id    INTEGER NOT NULL,
    all_unique INTEGER NOT NULL,          -- 0/1
    work_count  INTEGER NOT NULL,
    unique_count INTEGER NOT NULL,
    pending_count INTEGER NOT NULL,
    conflict_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_works (
    snapshot_id      INTEGER NOT NULL REFERENCES snapshots(id),
    work_no          TEXT NOT NULL,
    status           TEXT NOT NULL,
    title_total      INTEGER,
    exhib_total      INTEGER,
    title_ledger     INTEGER,
    exhib_ledger     INTEGER,
    reasons          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (snapshot_id, work_no)
);
"""

CSV_HEADERS = ["作品号", "题名", "展名", "组合次数"]


def get_db_path() -> str:
    import os
    return os.environ.get("SPLIT_DB_PATH", "/data/museum.db")


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def current_batch_id(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "current_batch_id", "0") or 0)


def current_version(conn: sqlite3.Connection) -> int:
    """数据版本号：上传或任何一次底账登记/更正都会 +1。"""
    return int(get_meta(conn, "data_version", "0") or 0)


def bump_version(conn: sqlite3.Connection) -> int:
    v = current_version(conn) + 1
    set_meta(conn, "data_version", str(v))
    return v


# ---------------------------------------------------------------- 上传解析

def parse_wide_csv(text: str) -> Dict[str, List[Tuple[Optional[str], Optional[str], int]]]:
    """解析宽表 CSV，按作品分组，同名组合归并求和。

    空题名/空展名 -> None（表示该作品无此类明细）。
    原明细允许同名，因此同一 (题名,展名) 组合出现多行时次数相加。
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("CSV 为空")
    header = [h.strip().lstrip("﻿") for h in header]
    missing = [h for h in CSV_HEADERS if h not in header]
    if missing:
        raise ValueError(f"CSV 缺少列：{'、'.join(missing)}；需要列：{'、'.join(CSV_HEADERS)}")
    idx = {h: header.index(h) for h in CSV_HEADERS}

    grouped: Dict[str, Dict[Tuple[Optional[str], Optional[str]], int]] = {}
    order: List[str] = []
    line_no = 1
    for raw in reader:
        line_no += 1
        if not raw or all(not cell.strip() for cell in raw):
            continue
        if len(raw) < len(header):
            raw = raw + [""] * (len(header) - len(raw))
        work_no = raw[idx["作品号"]].strip()
        if not work_no:
            raise ValueError(f"第 {line_no} 行作品号为空")
        title = raw[idx["题名"]].strip() or None
        exhib = raw[idx["展名"]].strip() or None
        cnt_raw = raw[idx["组合次数"]].strip()
        try:
            cnt = int(cnt_raw)
        except ValueError:
            raise ValueError(f"第 {line_no} 行组合次数不是整数：{cnt_raw!r}")
        if cnt <= 0:
            raise ValueError(f"第 {line_no} 行组合次数必须为正整数：{cnt}")
        if work_no not in grouped:
            grouped[work_no] = {}
            order.append(work_no)
        key = (title, exhib)
        grouped[work_no][key] = grouped[work_no].get(key, 0) + cnt

    return {
        w: [(t, e, c) for (t, e), c in grouped[w].items()]
        for w in order
    }


def replace_wide_rows(
    conn: sqlite3.Connection,
    parsed: Dict[str, List[Tuple[Optional[str], Optional[str], int]]],
) -> int:
    """以新上传的宽表整体替换旧输入，返回新的批次号。"""
    batch_id = int(get_meta(conn, "next_batch_id", "1") or 1)
    with transaction(conn):
        conn.execute("DELETE FROM wide_rows")
        for work_no, rows in parsed.items():
            conn.executemany(
                "INSERT INTO wide_rows(batch_id, work_no, title_name, exhib_name, combo_cnt) "
                "VALUES(?,?,?,?,?)",
                [(batch_id, work_no, t, e, c) for t, e, c in rows],
            )
        set_meta(conn, "current_batch_id", str(batch_id))
        set_meta(conn, "next_batch_id", str(batch_id + 1))
        bump_version(conn)
        conn.execute(
            "INSERT INTO events(kind, detail) VALUES('upload', ?)",
            (f"批次 {batch_id}：{len(parsed)} 个作品",),
        )
    return batch_id


def load_all_rows(
    conn: sqlite3.Connection,
) -> Dict[str, List[Tuple[Optional[str], Optional[str], int]]]:
    batch_id = current_batch_id(conn)
    out: Dict[str, List[Tuple[Optional[str], Optional[str], int]]] = {}
    for r in conn.execute(
        "SELECT work_no, title_name, exhib_name, combo_cnt "
        "FROM wide_rows WHERE batch_id=? ORDER BY id",
        (batch_id,),
    ):
        out.setdefault(r["work_no"], []).append(
            (r["title_name"], r["exhib_name"], r["combo_cnt"]))
    return out


# ---------------------------------------------------------------- 底账

def upsert_ledger(
    conn: sqlite3.Connection,
    work_no: str,
    kind: str,
    total: Optional[int],
) -> None:
    """kind: 'title' | 'exhibition'；total 为 None 表示清除该侧登记。"""
    if kind not in ("title", "exhibition"):
        raise ValueError("kind 必须为 title 或 exhibition")
    if total is not None and total < 0:
        raise ValueError("底账总条数不能为负")
    with transaction(conn):
        # 先确保作品有一行，再只更新对应一侧，保留另一侧的既有登记
        conn.execute(
            "INSERT INTO ledgers(work_no, title_total, exhib_total) "
            "VALUES(?, NULL, NULL) ON CONFLICT(work_no) DO NOTHING",
            (work_no,),
        )
        if kind == "title":
            conn.execute(
                "UPDATE ledgers SET title_total=?, updated_at=datetime('now') "
                "WHERE work_no=?", (total, work_no))
        else:
            conn.execute(
                "UPDATE ledgers SET exhib_total=?, updated_at=datetime('now') "
                "WHERE work_no=?", (total, work_no))
        label = "题名" if kind == "title" else "参展"
        conn.execute(
            "INSERT INTO events(kind, detail) VALUES('ledger', ?)",
            (f"作品 {work_no} {label}底账 -> {total if total is not None else '清除'}",),
        )
        bump_version(conn)


def get_ledgers(conn: sqlite3.Connection) -> Dict[str, Dict[str, Optional[int]]]:
    out: Dict[str, Dict[str, Optional[int]]] = {}
    for r in conn.execute("SELECT work_no, title_total, exhib_total FROM ledgers"):
        out[r["work_no"]] = {
            "title": r["title_total"],
            "exhibition": r["exhib_total"],
        }
    return out


def list_events(conn: sqlite3.Connection, limit: int = 30) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT created_at, kind, detail FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    ))


# ---------------------------------------------------------------- 快照

def save_snapshot(
    conn: sqlite3.Connection,
    kind: str,
    results: List[dict],
) -> int:
    """把一次核对/下载时的逐作品结论固化为快照。"""
    batch_id = current_batch_id(conn)
    n = len(results)
    n_unique = sum(1 for r in results if r["status"] == "unique")
    n_pending = sum(1 for r in results if r["status"] == "pending")
    n_conflict = n - n_unique - n_pending
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO snapshots(kind, batch_id, all_unique, work_count, "
            "unique_count, pending_count, conflict_count) VALUES(?,?,?,?,?,?,?)",
            (kind, batch_id, 1 if n and n_unique == n else 0,
             n, n_unique, n_pending, n_conflict),
        )
        snap_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO snapshot_works(snapshot_id, work_no, status, "
            "title_total, exhib_total, title_ledger, exhib_ledger, reasons) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(snap_id, r["work_no"], r["status"],
              r.get("title_total"), r.get("exhibition_total"),
              r.get("title_ledger"), r.get("exhib_ledger"),
              "；".join(r.get("reasons", []))) for r in results],
        )
        conn.execute(
            "INSERT INTO events(kind, detail) VALUES(?, ?)",
            (kind, f"快照 {snap_id}：唯一 {n_unique}/{n}，"
                   f"待补 {n_pending}，冲突 {n_conflict}"),
        )
    return snap_id


def get_snapshot(conn: sqlite3.Connection, snap_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone()


def get_snapshot_works(conn: sqlite3.Connection, snap_id: int) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM snapshot_works WHERE snapshot_id=? ORDER BY work_no",
        (snap_id,),
    ))
