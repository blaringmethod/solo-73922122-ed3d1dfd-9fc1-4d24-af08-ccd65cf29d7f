# -*- coding: utf-8 -*-
"""入库增量发布的纯逻辑：份号区间运算、增量计算、包 CSV 生成。

份号模型
--------
接收库为同一（作品号, 类别, 名称）下的每一份同名明细单独建档，份号在该键内
从 1 起编：

- 补入：从该键历史最大份号 +1 起连续发放（首次从 1 开始）；
- 撤销：从当前最大有效份号起依次向下撤销，撤掉的号永久停用——即使整项
  删掉再补回，新补的号也从历史最大份号接着发，不复用已撤销的号；
- 编号状态只需记录“历史最大份号 + 已撤销区间”，发包只列连续号段，
  全程无需逐份展开。
"""
from __future__ import annotations

import csv
import io
from typing import Dict, Iterable, List, Tuple

KIND_TITLE = "title"
KIND_EXHIBITION = "exhibition"
KIND_LABELS = {KIND_TITLE: "题名", KIND_EXHIBITION: "参展"}
KIND_ORDER = {KIND_TITLE: 0, KIND_EXHIBITION: 1}

ACTION_ADD = "add"
ACTION_REVOKE = "revoke"
ACTION_LABELS = {ACTION_ADD: "补入", ACTION_REVOKE: "撤销"}

Range = Tuple[int, int]
Key = Tuple[str, str, str]  # (作品号, 类别, 名称)


def normalize_ranges(ranges: Iterable[Range]) -> List[Range]:
    """排序并合并重叠或相邻的区间。"""
    merged: List[List[int]] = []
    for s, e in sorted(ranges):
        if e < s:
            continue
        if merged and s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def active_count(max_issued: int, revoked: Iterable[Range]) -> int:
    """当前有效份数 = 历史最大份号 - 已撤销号数。"""
    return max_issued - sum(e - s + 1 for s, e in revoked)


def top_active_ranges(
    max_issued: int, revoked: Iterable[Range], k: int
) -> List[Range]:
    """当前有效份号中最大的 k 个所占的连续区间（按号段升序返回）。

    有效号 = [1, max_issued] 去掉已撤销区间；从最大的一端依次取 k 个，
    归并为连续号段返回（可能跨多段）。
    """
    if k <= 0:
        return []
    active: List[Range] = []
    prev = 1
    for s, e in normalize_ranges(revoked):
        if prev <= s - 1:
            active.append((prev, s - 1))
        prev = e + 1
    if prev <= max_issued:
        active.append((prev, max_issued))
    out: List[Range] = []
    remaining = k
    for a, b in reversed(active):
        if remaining <= 0:
            break
        size = b - a + 1
        if size <= remaining:
            out.append((a, b))
            remaining -= size
        else:
            out.append((b - remaining + 1, b))
            remaining = 0
    if remaining:
        raise ValueError(
            f"有效份号不足：需撤销 {k} 个，实际只有 {k - remaining} 个")
    out.reverse()
    return out


def compute_changes(
    results: List[dict], copy_state: Dict[Key, dict]
) -> List[dict]:
    """由逐作品唯一拆分结果与当前编号状态，计算本次发布的增减号段。

    results 中每个元素需含 work_no / title_counts / exhibition_counts
    （调用方保证全部作品唯一可拆）。copy_state 形如
    Key -> {"max_issued": int, "revoked": [Range, ...]}；
    首次发布时为空字典，即全部份数从零起算。

    返回按（作品号, 类别, 名称, 号段）排序的变更列表，每项含
    work_no/kind/name/action/r_start/r_end/old_cnt/new_cnt。
    目标拆分中不存在的键（如作品已从新宽表中消失）按 0 份处理，即全部撤销。
    """
    target: Dict[Key, int] = {}
    for r in results:
        for name, cnt in r["title_counts"].items():
            target[(r["work_no"], KIND_TITLE, name)] = cnt
        for name, cnt in r["exhibition_counts"].items():
            target[(r["work_no"], KIND_EXHIBITION, name)] = cnt

    changes: List[dict] = []
    keys = sorted(set(target) | set(copy_state),
                  key=lambda k: (k[0], KIND_ORDER[k[1]], k[2]))
    for work_no, kind, name in keys:
        st = copy_state.get((work_no, kind, name),
                            {"max_issued": 0, "revoked": []})
        old = active_count(st["max_issued"], st["revoked"])
        new = target.get((work_no, kind, name), 0)
        base = {"work_no": work_no, "kind": kind, "name": name,
                "old_cnt": old, "new_cnt": new}
        if new > old:
            start = st["max_issued"] + 1
            changes.append({**base, "action": ACTION_ADD,
                            "r_start": start,
                            "r_end": start + (new - old) - 1})
        elif new < old:
            for s, e in top_active_ranges(
                    st["max_issued"], st["revoked"], old - new):
                changes.append({**base, "action": ACTION_REVOKE,
                                "r_start": s, "r_end": e})
    return changes


def build_package_csv(
    package_id: int,
    prev_id: int,
    data_version: int,
    batch_id: int,
    created_at: str,
    changes: List[dict],
) -> str:
    """生成增量包 CSV 文本。

    头部注明包号与前一包号等元信息；正文按（作品号, 类别, 名称）列出
    补入或撤销的连续号段，一行一个号段，不逐份展开。
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["包号", package_id])
    w.writerow(["前一包号", prev_id])
    w.writerow(["数据版本", data_version])
    w.writerow(["宽表批次", batch_id])
    w.writerow(["生成时间", created_at])
    w.writerow([])
    w.writerow(["作品号", "类别", "名称", "动作", "号段起", "号段止", "份数"])
    for c in changes:
        w.writerow([c["work_no"], KIND_LABELS[c["kind"]], c["name"],
                    ACTION_LABELS[c["action"]],
                    c["r_start"], c["r_end"], c["r_end"] - c["r_start"] + 1])
    return buf.getvalue()
