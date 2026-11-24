# -*- coding: utf-8 -*-
"""SQLite 持久化层。

保存三类数据：
1. 上传宽表的原始输入（wide_rows，按作品/题名/展名归并后的组合次数）；
2. 馆员按作品登记/更正的两类底账总条数（ledgers）；
3. 每次核对/下载所基于的已提交版本快照（snapshots、snapshot_works），
   核对期间允许继续补登，而核对结论与下载内容始终对应同一版本。

入库增量发布相关：
4. 份号状态（copy_state 记录每个 (作品号,类别,名称) 的历史最大份号，
   revoked_ranges 记录已永久停用的号段——均按区间存储，不逐份展开）；
5. 已发布的增量包（packages + package_ranges，CSV 原文随包保存，
   凭包号可重下原 CSV）；
6. 待提交的发布计划（publish_plans + publish_plan_ranges），
   计划记录计算时的数据版本与最近包号，提交时校验未变动才生效。
"""
from __future__ import annotations

import csv
import io
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from . import publish

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

-- ---------------- 入库增量发布 ----------------

-- 每个 (作品号, 类别, 名称) 的历史最大份号；有效份号 = [1, max_issued] 去掉
-- revoked_ranges 中的号段。只存最大号与区间，不逐份展开。
CREATE TABLE IF NOT EXISTS copy_state (
    work_no    TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('title','exhibition')),
    name       TEXT NOT NULL,
    max_issued INTEGER NOT NULL DEFAULT 0 CHECK (max_issued >= 0),
    PRIMARY KEY (work_no, kind, name)
);

-- 已撤销（永久停用）的份号区间
CREATE TABLE IF NOT EXISTS revoked_ranges (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    work_no TEXT NOT NULL,
    kind    TEXT NOT NULL,
    name    TEXT NOT NULL,
    r_start INTEGER NOT NULL CHECK (r_start >= 1),
    r_end   INTEGER NOT NULL CHECK (r_end >= r_start)
);
CREATE INDEX IF NOT EXISTS idx_revoked_key ON revoked_ranges(work_no, kind, name);

-- 已发布的增量包；csv_content 保存发包时的 CSV 原文，凭包号可重下
CREATE TABLE IF NOT EXISTS packages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    prev_id      INTEGER NOT NULL DEFAULT 0,   -- 前一包号，0 表示首包
    data_version INTEGER NOT NULL,
    batch_id     INTEGER NOT NULL,
    add_count    INTEGER NOT NULL,
    revoke_count INTEGER NOT NULL,
    csv_content  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS package_ranges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES packages(id),
    work_no    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('add','revoke')),
    r_start    INTEGER NOT NULL,
    r_end      INTEGER NOT NULL CHECK (r_end >= r_start)
);
CREATE INDEX IF NOT EXISTS idx_pkg_ranges ON package_ranges(package_id);

-- 待提交的发布计划：记录计算时的数据版本与最近包号，提交时校验
CREATE TABLE IF NOT EXISTS publish_plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    data_version  INTEGER NOT NULL,
    batch_id      INTEGER NOT NULL,
    base_package  INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','published','superseded')),
    add_count     INTEGER NOT NULL,
    revoke_count  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_plan_ranges (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES publish_plans(id),
    work_no TEXT NOT NULL,
    kind    TEXT NOT NULL,
    name    TEXT NOT NULL,
    action  TEXT NOT NULL CHECK (action IN ('add','revoke')),
    r_start INTEGER NOT NULL,
    r_end   INTEGER NOT NULL CHECK (r_end >= r_start),
    old_cnt INTEGER NOT NULL,
    new_cnt INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_ranges ON publish_plan_ranges(plan_id);
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


@contextmanager
def immediate_transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 事务：进入即取写锁，用于发布提交等
    “先校验再写入”的场景，保证校验与写入之间没有他人提交。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


class StaleStateError(RuntimeError):
    """提交时宽表、底账或最近一包已变动，需重新计算后再提交。"""


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


# ---------------------------------------------------------------- 增量发布

def latest_package_id(conn: sqlite3.Connection) -> int:
    """最近一包号；从未发布返回 0。"""
    row = conn.execute("SELECT MAX(id) AS mid FROM packages").fetchone()
    return int(row["mid"] or 0)


def load_copy_state(conn: sqlite3.Connection) -> Dict[publish.Key, dict]:
    """读取全部 (作品号,类别,名称) 的份号状态：历史最大号 + 已撤销区间。"""
    state: Dict[publish.Key, dict] = {}
    for r in conn.execute(
            "SELECT work_no, kind, name, max_issued FROM copy_state"):
        state[(r["work_no"], r["kind"], r["name"])] = {
            "max_issued": r["max_issued"], "revoked": []}
    for r in conn.execute(
            "SELECT work_no, kind, name, r_start, r_end FROM revoked_ranges "
            "ORDER BY work_no, kind, name, r_start"):
        key = (r["work_no"], r["kind"], r["name"])
        state.setdefault(key, {"max_issued": 0, "revoked": []})
        state[key]["revoked"].append((r["r_start"], r["r_end"]))
    return state


def save_plan(
    conn: sqlite3.Connection,
    changes: List[dict],
    data_version: int,
    batch_id: int,
    base_package: int,
) -> int:
    """保存一份待提交的发布计划；此前的待提交计划一并作废。"""
    add_total = sum(c["r_end"] - c["r_start"] + 1
                    for c in changes if c["action"] == publish.ACTION_ADD)
    revoke_total = sum(c["r_end"] - c["r_start"] + 1
                       for c in changes if c["action"] == publish.ACTION_REVOKE)
    with transaction(conn):
        conn.execute(
            "UPDATE publish_plans SET status='superseded' WHERE status='pending'")
        cur = conn.execute(
            "INSERT INTO publish_plans(data_version, batch_id, base_package, "
            "status, add_count, revoke_count) VALUES(?,?,?,'pending',?,?)",
            (data_version, batch_id, base_package, add_total, revoke_total),
        )
        plan_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO publish_plan_ranges(plan_id, work_no, kind, name, "
            "action, r_start, r_end, old_cnt, new_cnt) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(plan_id, c["work_no"], c["kind"], c["name"], c["action"],
              c["r_start"], c["r_end"], c["old_cnt"], c["new_cnt"])
             for c in changes],
        )
        conn.execute(
            "INSERT INTO events(kind, detail) VALUES('publish_calculate', ?)",
            (f"计划 {plan_id}：补入 {add_total} 份、撤销 {revoke_total} 份"
             f"（基于数据版本 {data_version}，前一包 {base_package}）",),
        )
    return plan_id


def get_plan(conn: sqlite3.Connection, plan_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM publish_plans WHERE id=?", (plan_id,)).fetchone()


def get_pending_plan(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM publish_plans WHERE status='pending' "
        "ORDER BY id DESC LIMIT 1").fetchone()


def get_plan_ranges(conn: sqlite3.Connection, plan_id: int) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM publish_plan_ranges WHERE plan_id=? ORDER BY id",
        (plan_id,),
    ))


def apply_plan(conn: sqlite3.Connection, plan_id: int) -> int:
    """提交发布计划：新包与份号状态在同一事务内生效。

    提交时若宽表/底账（数据版本）或最近一包已变动，抛出 StaleStateError；
    任何失败都会回滚，保留原发布状态。返回新包号。
    """
    with immediate_transaction(conn):
        plan = get_plan(conn, plan_id)
        if plan is None:
            raise RuntimeError("发布计划不存在")
        if plan["status"] == "published":
            raise RuntimeError("该计划已发布过，不能重复提交")
        if plan["status"] != "pending":
            raise RuntimeError("该计划已作废，请重新计算")
        if (current_version(conn) != plan["data_version"]
                or latest_package_id(conn) != plan["base_package"]):
            raise StaleStateError(
                "宽表、底账或最近一包已变动，请重新计算后再提交")
        changes = [dict(r) for r in conn.execute(
            "SELECT work_no, kind, name, action, r_start, r_end "
            "FROM publish_plan_ranges WHERE plan_id=? ORDER BY id",
            (plan_id,))]
        if not changes:
            raise RuntimeError("计划内没有任何变更，无需发布")

        prev_id = latest_package_id(conn)
        batch_id = current_batch_id(conn)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO packages(created_at, prev_id, data_version, batch_id, "
            "add_count, revoke_count, csv_content) VALUES(?,?,?,?,?,?, '')",
            (created_at, prev_id, plan["data_version"], batch_id,
             plan["add_count"], plan["revoke_count"]),
        )
        package_id = cur.lastrowid

        # 应用份号状态：补入推进历史最大号；撤销写入永久停用的号段（合并相邻）
        for c in changes:
            key = (c["work_no"], c["kind"], c["name"])
            if c["action"] == publish.ACTION_ADD:
                conn.execute(
                    "INSERT INTO copy_state(work_no, kind, name, max_issued) "
                    "VALUES(?,?,?,?) ON CONFLICT(work_no, kind, name) "
                    "DO UPDATE SET max_issued=excluded.max_issued",
                    (key[0], key[1], key[2], c["r_end"]),
                )
            else:
                existing = [(r["r_start"], r["r_end"]) for r in conn.execute(
                    "SELECT r_start, r_end FROM revoked_ranges "
                    "WHERE work_no=? AND kind=? AND name=?", key)]
                merged = publish.normalize_ranges(
                    existing + [(c["r_start"], c["r_end"])])
                conn.execute(
                    "DELETE FROM revoked_ranges "
                    "WHERE work_no=? AND kind=? AND name=?", key)
                conn.executemany(
                    "INSERT INTO revoked_ranges(work_no, kind, name, "
                    "r_start, r_end) VALUES(?,?,?,?,?)",
                    [(key[0], key[1], key[2], s, e) for s, e in merged],
                )
        conn.executemany(
            "INSERT INTO package_ranges(package_id, work_no, kind, name, "
            "action, r_start, r_end) VALUES(?,?,?,?,?,?,?)",
            [(package_id, c["work_no"], c["kind"], c["name"], c["action"],
              c["r_start"], c["r_end"]) for c in changes],
        )
        csv_text = publish.build_package_csv(
            package_id, prev_id, plan["data_version"], batch_id,
            created_at, changes)
        conn.execute("UPDATE packages SET csv_content=? WHERE id=?",
                     (csv_text, package_id))
        conn.execute(
            "UPDATE publish_plans SET status='published' WHERE id=?",
            (plan_id,))
        conn.execute(
            "INSERT INTO events(kind, detail) VALUES('publish', ?)",
            (f"包 {package_id} 发布：补入 {plan['add_count']} 份、"
             f"撤销 {plan['revoke_count']} 份（前一包 {prev_id}）",),
        )
    return package_id


def list_packages(conn: sqlite3.Connection, limit: int = 50) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM packages ORDER BY id DESC LIMIT ?", (limit,)))


def get_package(conn: sqlite3.Connection, pkg_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM packages WHERE id=?", (pkg_id,)).fetchone()


def get_package_ranges(
    conn: sqlite3.Connection, pkg_id: int
) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM package_ranges WHERE package_id=? ORDER BY id",
        (pkg_id,),
    ))
