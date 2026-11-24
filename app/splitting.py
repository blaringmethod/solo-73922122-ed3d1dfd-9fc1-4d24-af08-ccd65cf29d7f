# -*- coding: utf-8 -*-
"""拆表核心算法。

输入：某一作品在宽表中的全部行（题名、展名、组合次数）。
输出：该作品的唯一拆分结果或冲突/待补原因，以及恢复出的题名份数、
参展份数（有唯一解时）。

模型
----
设作品有题名 T_1..T_p（原明细允许同名，按名称归并），各名称份数为
x_i（正整数，即该题名在题名明细中出现的行数）；参展 E_1..E_q，份数
y_j。宽表由题名明细左联接参展明细后按组合聚合而成，故每个非空组合的
次数为 c_ij = x_i * y_j；某一侧无明细时，该侧以 NULL 参与左联接，
对应行的次数等于另一侧明细的份数本身。

核对时“不展开重复行”即只对聚合后的 (题名, 展名, 次数) 行逐组合比较，
不构造笛卡尔积。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 作品状态
PENDING = "pending"      # 有多组可行拆分，等待馆员补/改底账总条数
CONFLICT = "conflict"    # 数据自相矛盾，或登记的底账与组合次数不相容
UNIQUE = "unique"        # 存在唯一拆分
EMPTY = "empty"          # 宽表中没有该作品的任何行（异常数据）


@dataclass
class WorkRows:
    work_no: str
    # 题名 None 表示无题名明细；展名 None 表示无参展明细
    rows: List[Tuple[Optional[str], Optional[str], int]] = field(default_factory=list)


@dataclass
class SplitResult:
    work_no: str
    status: str
    title_counts: Dict[str, int] = field(default_factory=dict)
    exhibition_counts: Dict[str, int] = field(default_factory=dict)
    title_total: Optional[int] = None
    exhibition_total: Optional[int] = None
    reasons: List[str] = field(default_factory=list)
    # 可行解数量（无登记底账时由矩阵结构决定）
    candidate_count: int = 0


def _divisors(n: int) -> List[int]:
    """返回 n 的全部正约数，升序。"""
    out: List[int] = []
    i = 1
    while i * i <= n:
        if n % i == 0:
            out.append(i)
            if i * i != n:
                out.append(n // i)
        i += 1
    out.sort()
    return out


def _classify(
    work_no: str,
    rows: List[Tuple[Optional[str], Optional[str], int]],
    title_ledger: Optional[int],
    exhibition_ledger: Optional[int],
) -> SplitResult:
    reasons: List[str] = []
    if not rows:
        return SplitResult(work_no, EMPTY, reasons=["宽表中没有该作品的行"])

    title_names = sorted({t for t, _, _ in rows if t is not None})
    exhib_names = sorted({e for _, e, _ in rows if e is not None})
    has_titles = bool(title_names)
    has_exhibs = bool(exhib_names)
    p, q = len(title_names), len(exhib_names)

    # 同名归并后的组合 -> 次数
    matrix: Dict[Tuple[str, str], int] = {}
    title_only: Dict[str, int] = {}
    exhib_only: Dict[str, int] = {}
    null_null_count = 0

    for t, e, c in rows:
        if c <= 0:
            reasons.append(f"组合次数必须为正整数：({t!r},{e!r})={c}")
            continue
        if t is None and e is None:
            null_null_count += c
        elif t is None:
            exhib_only[e] = exhib_only.get(e, 0) + c
        elif e is None:
            title_only[t] = title_only.get(t, 0) + c
        else:
            key = (t, e)
            matrix[key] = matrix.get(key, 0) + c

    if any(r for r in reasons):
        return SplitResult(work_no, CONFLICT, reasons=reasons)

    # 同一作品在左联接语义下，NULL 侧必须整侧为空：
    # 要么有题名明细（全部题名非空），要么没有题名明细（全部题名为空）。
    mixed_titles = has_titles and any(t is None for t, _, _ in rows)
    mixed_exhibs = has_exhibs and any(e is None for _, e, _ in rows)
    if mixed_titles:
        reasons.append("同一作品的题名时而为空时而非空，与左联接语义冲突")
    if mixed_exhibs:
        reasons.append("同一作品的展名时而为空时而非空，与左联接语义冲突")
    if null_null_count and (has_titles or has_exhibs):
        reasons.append("存在题名与展名同时为空的行，但该作品又有非空明细，数据冲突")
    if reasons:
        return SplitResult(work_no, CONFLICT, reasons=reasons)

    # ---- 情形一：两侧均无明细（作品存在但没有任何明细） ----
    if not has_titles and not has_exhibs:
        if null_null_count == 0:
            return SplitResult(work_no, CONFLICT,
                               reasons=["作品行无任何明细且无组合次数"])
        if title_ledger not in (None, 0):
            reasons.append(f"登记题名底账为 {title_ledger}，但该作品无题名明细（应为 0）")
        if exhibition_ledger not in (None, 0):
            reasons.append(f"登记参展底账为 {exhibition_ledger}，但该作品无参展明细（应为 0）")
        if reasons:
            return SplitResult(work_no, CONFLICT, reasons=reasons)
        return SplitResult(work_no, UNIQUE, title_total=0, exhibition_total=0,
                           candidate_count=1)

    # ---- 情形二：只有题名明细，无参展明细 ----
    if has_titles and not has_exhibs:
        # c_i = x_i
        x = {t: title_only.get(t, 0) for t in title_names}
        if any(v <= 0 for v in x.values()):
            return SplitResult(work_no, CONFLICT,
                               reasons=["存在题名缺少 (题名, 空展名) 的组合行"])
        total = sum(x.values())
        if title_ledger is not None and title_ledger != total:
            reasons.append(
                f"登记题名底账 {title_ledger} 条与组合次数推出的 {total} 条不一致")
        if exhibition_ledger not in (None, 0):
            reasons.append(f"登记参展底账为 {exhibition_ledger}，但该作品无参展明细（应为 0）")
        if reasons:
            return SplitResult(work_no, CONFLICT, reasons=reasons)
        return SplitResult(work_no, UNIQUE, title_counts=x,
                           title_total=total, exhibition_total=0,
                           candidate_count=1)

    # ---- 情形三：只有参展明细，无题名明细 ----
    if not has_titles and has_exhibs:
        y = {e: exhib_only.get(e, 0) for e in exhib_names}
        if any(v <= 0 for v in y.values()):
            return SplitResult(work_no, CONFLICT,
                               reasons=["存在展名缺少 (空题名, 展名) 的组合行"])
        total = sum(y.values())
        if exhibition_ledger is not None and exhibition_ledger != total:
            reasons.append(
                f"登记参展底账 {exhibition_ledger} 条与组合次数推出的 {total} 条不一致")
        if title_ledger not in (None, 0):
            reasons.append(f"登记题名底账为 {title_ledger}，但该作品无题名明细（应为 0）")
        if reasons:
            return SplitResult(work_no, CONFLICT, reasons=reasons)
        return SplitResult(work_no, UNIQUE, exhibition_counts=y,
                           title_total=0, exhibition_total=total,
                           candidate_count=1)

    # ---- 情形四：两侧都有明细，矩阵 c_ij = x_i * y_j ----
    # 完整笛卡尔组合检查
    missing = [(t, e) for t in title_names for e in exhib_names
               if (t, e) not in matrix]
    if missing:
        sample = "、".join(f"({t},{e})" for t, e in missing[:5])
        more = "……" if len(missing) > 5 else ""
        return SplitResult(work_no, CONFLICT,
                           reasons=[f"左联接应覆盖全部组合，缺少组合行：{sample}{more}"])

    # 秩一性（可乘性）校验：c_ij * c_11 == c_i1 * c_1j
    t0, e0 = title_names[0], exhib_names[0]
    c00 = matrix[(t0, e0)]
    for t in title_names:
        for e in exhib_names:
            if matrix[(t, e)] * c00 != matrix[(t, e0)] * matrix[(t0, e)]:
                reasons.append(
                    f"组合次数不满足可乘关系 c(题名,展名)=题名份数×参展份数："
                    f"c({t},{e})={matrix[(t,e)]} 与同行/同列次数矛盾")
                return SplitResult(work_no, CONFLICT, reasons=reasons)

    # 结构解：以 x_1（第一个题名的份数）为自由正整数，
    # y_j = c_1j / x_1，x_i = c_i1 / y_j。
    # 可行 x_1 恰为第一行所有次数的公约数。
    first_row = [matrix[(t0, e)] for e in exhib_names]
    g = first_row[0]
    for v in first_row[1:]:
        g = math.gcd(g, v)

    candidates: List[Tuple[Dict[str, int], Dict[str, int]]] = []
    for d in _divisors(g):
        x1 = d
        # 由第一行定 y；整除性由“d 整除第一行全部元素”保证
        y = {}
        ok = True
        for e in exhib_names:
            c = matrix[(t0, e)]
            if c % x1 != 0:
                ok = False
                break
            y[e] = c // x1
        if not ok:
            continue
        x = {t0: x1}
        for t in title_names[1:]:
            c = matrix[(t, e0)]
            if c % y[e0] != 0:
                ok = False
                break
            x[t] = c // y[e0]
        if not ok:
            continue
        # 用整个矩阵复核，保证不展开重复行也能逐组合对上
        if all(matrix[(t, e)] == x[t] * y[e]
               for t in title_names for e in exhib_names):
            candidates.append((x, y))

    if not candidates:
        return SplitResult(work_no, CONFLICT,
                           reasons=["组合次数无法分解为正整数的题名份数与参展份数"])

    # 用馆员登记的底账总条数过滤
    filtered = []
    for x, y in candidates:
        xt, ye = sum(x.values()), sum(y.values())
        if title_ledger is not None and xt != title_ledger:
            continue
        if exhibition_ledger is not None and ye != exhibition_ledger:
            continue
        filtered.append((x, y, xt, ye))

    if not filtered:
        ledger_desc = []
        if title_ledger is not None:
            ledger_desc.append(f"题名底账 {title_ledger} 条")
        if exhibition_ledger is not None:
            ledger_desc.append(f"参展底账 {exhibition_ledger} 条")
        ledger_text = "、".join(ledger_desc) if ledger_desc else "（未登记底账）"
        feasible = [f"题名总{sum(x.values())}/参展总{sum(y.values())}"
                    for x, y in candidates]
        reasons.append(
            f"登记的{ledger_text}与任何可行拆分都不相容；"
            f"由组合次数可推出的总条数候选：{'、'.join(feasible)}")
        return SplitResult(work_no, CONFLICT, reasons=reasons,
                           candidate_count=len(candidates))

    if len(filtered) > 1:
        feasible = [f"题名总{xt}/参展总{ye}" for _, _, xt, ye in filtered]
        return SplitResult(
            work_no, PENDING,
            reasons=[f"存在 {len(filtered)} 组可行拆分（{ '；'.join(feasible) }），"
                     "请登记或更正该作品题名/参展明细的底账总条数"],
            candidate_count=len(filtered))

    x, y, xt, ye = filtered[0]
    return SplitResult(work_no, UNIQUE,
                       title_counts=dict(sorted(x.items())),
                       exhibition_counts=dict(sorted(y.items())),
                       title_total=xt, exhibition_total=ye,
                       candidate_count=1)


def split_work(
    work_no: str,
    rows: List[Tuple[Optional[str], Optional[str], int]],
    title_ledger: Optional[int] = None,
    exhibition_ledger: Optional[int] = None,
) -> SplitResult:
    """拆分单个作品。rows 为 (题名或 None, 展名或 None, 组合次数) 列表。"""
    return _classify(work_no, list(rows), title_ledger, exhibition_ledger)


def rejoin_counts(
    title_counts: Dict[str, int],
    exhibition_counts: Dict[str, int],
) -> List[Tuple[str, str, int]]:
    """由恢复出的份数重新左联接，返回聚合后的组合次数（不展开重复行）。

    两侧都有明细时为完整笛卡尔组合，次数为份数乘积；
    单侧无明细时该侧以 None 占位，次数等于另一侧份数。
    """
    out: List[Tuple[str, str, int]] = []
    if title_counts and exhibition_counts:
        for t, x in sorted(title_counts.items()):
            for e, y in sorted(exhibition_counts.items()):
                out.append((t, e, x * y))
    elif title_counts:
        for t, x in sorted(title_counts.items()):
            out.append((t, None, x))
    elif exhibition_counts:
        for e, y in sorted(exhibition_counts.items()):
            out.append((None, e, y))
    return out
