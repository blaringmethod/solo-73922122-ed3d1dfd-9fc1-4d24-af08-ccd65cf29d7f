# -*- coding: utf-8 -*-
"""业务编排：读取同一已提交状态 -> 逐作品拆分 -> 核对/导出。"""
from __future__ import annotations

import csv
import io
import zipfile
from typing import Dict, List, Optional, Tuple

from . import db
from .splitting import (
    CONFLICT, EMPTY, PENDING, UNIQUE,
    SplitResult, rejoin_counts, split_work,
)

STATUS_LABELS = {
    UNIQUE: "唯一可拆",
    PENDING: "待补底账",
    CONFLICT: "冲突",
    EMPTY: "无数据",
}


def evaluate_all(conn) -> List[dict]:
    """基于当前已提交数据（同一批次宽表 + 已登记底账）评估全部作品。"""
    rows_by_work = db.load_all_rows(conn)
    ledgers = db.get_ledgers(conn)
    results: List[dict] = []
    for work_no, rows in rows_by_work.items():
        lg = ledgers.get(work_no, {"title": None, "exhibition": None})
        res: SplitResult = split_work(
            work_no, rows, lg["title"], lg["exhibition"])
        results.append({
            "work_no": work_no,
            "status": res.status,
            "title_counts": res.title_counts,
            "exhibition_counts": res.exhibition_counts,
            "title_total": res.title_total,
            "exhibition_total": res.exhibition_total,
            "title_ledger": lg["title"],
            "exhib_ledger": lg["exhibition"],
            "reasons": res.reasons,
            "candidate_count": res.candidate_count,
        })
    results.sort(key=lambda r: (r["status"] != UNIQUE, r["work_no"]))
    return results


def summarize(results: List[dict]) -> dict:
    n = len(results)
    by_status = {UNIQUE: 0, PENDING: 0, CONFLICT: 0, EMPTY: 0}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {
        "work_count": n,
        "unique_count": by_status[UNIQUE],
        "pending": [r["work_no"] for r in results if r["status"] == PENDING],
        "conflicts": [r["work_no"] for r in results
                      if r["status"] in (CONFLICT, EMPTY)],
        "all_unique": n > 0 and by_status[UNIQUE] == n,
    }


def run_check(conn) -> Tuple[int, dict, List[dict]]:
    """核对：固化快照并返回 (snapshot_id, 摘要, 逐作品结果)。"""
    results = evaluate_all(conn)
    summary = summarize(results)
    snap_id = db.save_snapshot(conn, "check", results)
    return snap_id, summary, results


def _detail_csv(results: List[dict], side: str) -> str:
    """生成带份数的明细 CSV。side: 'title' | 'exhibition'。"""
    buf = io.StringIO()
    if side == "title":
        writer = csv.writer(buf)
        writer.writerow(["作品号", "题名", "份数"])
        for r in results:
            for name, cnt in r["title_counts"].items():
                writer.writerow([r["work_no"], name, cnt])
    else:
        writer = csv.writer(buf)
        writer.writerow(["作品号", "展名", "份数"])
        for r in results:
            for name, cnt in r["exhibition_counts"].items():
                writer.writerow([r["work_no"], name, cnt])
    return buf.getvalue()


def verify_rejoin(conn, results: List[dict]) -> List[str]:
    """导出前自检：用恢复份数重新左联接，逐组合与输入宽表核对（不展开重复行）。"""
    rows_by_work = db.load_all_rows(conn)
    errors: List[str] = []
    for r in results:
        if r["status"] != UNIQUE:
            continue
        rebuilt = rejoin_counts(r["title_counts"], r["exhibition_counts"])
        rebuilt_map: Dict[Tuple[Optional[str], Optional[str]], int] = {}
        for t, e, c in rebuilt:
            rebuilt_map[(t, e)] = rebuilt_map.get((t, e), 0) + c
        original_map: Dict[Tuple[Optional[str], Optional[str]], int] = {}
        for t, e, c in rows_by_work.get(r["work_no"], []):
            original_map[(t, e)] = original_map.get((t, e), 0) + c
        if rebuilt_map != original_map:
            errors.append(
                f"作品 {r['work_no']} 重新左联接结果与输入不一致："
                f"输入 {sorted(original_map.items())} vs 还原 {sorted(rebuilt_map.items())}")
    return errors


def build_download(conn) -> Tuple[int, bytes]:
    """全部作品唯一可拆时打包两份带份数明细 CSV，返回 (snapshot_id, zip 字节)。

    若不满足条件抛出 RuntimeError；下载同样固化快照，保证与核对结论
    基于同一已提交状态。
    """
    results = evaluate_all(conn)
    summary = summarize(results)
    if not results:
        raise RuntimeError("还没有上传宽表数据，无法下载")
    if not summary["all_unique"]:
        raise RuntimeError("存在冲突或待补底账的作品，暂不能打包下载")
    errors = verify_rejoin(conn, results)
    if errors:
        raise RuntimeError("导出前重新左联接自检失败：" + "；".join(errors))
    snap_id = db.save_snapshot(conn, "download", results)

    title_csv = _detail_csv(results, "title")
    exhib_csv = _detail_csv(results, "exhibition")
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("title_details.csv", title_csv.encode("utf-8-sig"))
        zf.writestr("exhibition_details.csv", exhib_csv.encode("utf-8-sig"))
        # 附一份核对说明，标明对应的已提交快照
        note = io.StringIO()
        note.write(f"数据快照ID,{snap_id}\n")
        note.write(f"作品数,{summary['work_count']}\n")
        note.write("说明,两份明细的份数由宽表组合次数唯一拆分得到，"
                   "并已通过重新左联接逐组合核对\n")
        zf.writestr("README.txt", note.getvalue().encode("utf-8"))
    return snap_id, zip_buf.getvalue()
