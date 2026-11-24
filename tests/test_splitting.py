# -*- coding: utf-8 -*-
"""拆表核心算法单元测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.splitting import (  # noqa: E402
    CONFLICT, EMPTY, PENDING, UNIQUE, rejoin_counts, split_work,
)


def test_both_sides_structurally_unique():
    # x=(2,3), y=(1,2)：第一行 gcd(2,4)=1，结构上唯一
    rows = [
        ("山水", "故宫特展", 2), ("山水", "南洋巡展", 4),
        ("花鸟", "故宫特展", 3), ("花鸟", "南洋巡展", 6),
    ]
    r = split_work("W1", rows)
    assert r.status == UNIQUE
    assert r.title_counts == {"山水": 2, "花鸟": 3}
    assert r.exhibition_counts == {"故宫特展": 1, "南洋巡展": 2}
    assert r.title_total == 5 and r.exhibition_total == 3
    # 重新左联接逐组合对上
    assert sorted(rejoin_counts(r.title_counts, r.exhibition_counts)) == sorted(rows)


def test_scaled_matrix_needs_ledger_then_unique():
    # x=(2,4)=2*(1,2), y=(3,6)=3*(1,2)，矩阵元素都是 6 的倍数，存在多解
    rows = [
        ("T1", "E1", 6), ("T1", "E2", 12),
        ("T2", "E1", 12), ("T2", "E2", 24),
    ]
    r = split_work("W2", rows)
    assert r.status == PENDING
    assert r.candidate_count >= 2
    # 登记题名底账 6（=2+4）后唯一
    r2 = split_work("W2", rows, title_ledger=6)
    assert r2.status == UNIQUE
    assert r2.title_counts == {"T1": 2, "T2": 4}
    assert r2.exhibition_counts == {"E1": 3, "E2": 6}
    # 只登记参展底账 9 也应唯一
    r3 = split_work("W2", rows, exhibition_ledger=9)
    assert r3.status == UNIQUE and r3.exhibition_total == 9
    # 错误底账 -> 冲突
    r4 = split_work("W2", rows, title_ledger=7)
    assert r4.status == CONFLICT

def test_non_multiplicative_matrix_is_conflict():
    rows = [("T1", "E1", 2), ("T1", "E2", 4),
            ("T2", "E1", 3), ("T2", "E2", 7)]  # 7 不满足秩一
    r = split_work("W3", rows)
    assert r.status == CONFLICT
    assert r.reasons


def test_missing_combination_is_conflict():
    rows = [("T1", "E1", 2), ("T1", "E2", 4), ("T2", "E1", 3)]
    r = split_work("W4", rows)
    assert r.status == CONFLICT and "缺少组合" in r.reasons[0]


def test_title_only_work():
    # 无参展明细：展名全空，次数即题名份数
    rows = [("册页甲", None, 2), ("册页乙", None, 5)]
    r = split_work("W5", rows)
    assert r.status == UNIQUE
    assert r.title_counts == {"册页甲": 2, "册页乙": 5}
    assert r.exhibition_total == 0
    # 登记了非零参展底账应冲突
    r2 = split_work("W5", rows, exhibition_ledger=1)
    assert r2.status == CONFLICT
    # 题名底账与推出值不符
    r3 = split_work("W5", rows, title_ledger=8)
    assert r3.status == CONFLICT


def test_exhibition_only_work():
    rows = [(None, "晋唐宋元展", 3), (None, "百年回顾展", 1)]
    r = split_work("W6", rows)
    assert r.status == UNIQUE
    assert r.exhibition_counts == {"晋唐宋元展": 3, "百年回顾展": 1}
    assert r.title_total == 0


def test_neither_detail_null_null_rows():
    r = split_work("W7", [(None, None, 1)])
    assert r.status == UNIQUE and r.title_total == 0 and r.exhibition_total == 0
    r2 = split_work("W7", [(None, None, 1)], title_ledger=1)
    assert r2.status == CONFLICT


def test_mixed_null_side_is_conflict():
    rows = [("T1", "E1", 2), ("T1", None, 1)]  # 展名时而非空时而空
    assert split_work("W8", rows).status == CONFLICT


def test_duplicate_names_are_mergeable():
    # 原明细允许同名：同一组合两行，归并（解析层职责）后 c=(6,6)。
    # 单题名时 x1 自由（x1=1,2,3,6 均可），结构上不唯一 -> 待补底账；
    # 登记题名底账 1 后锁定 x1=1，y=(6,6)。
    merged = {("T1", "E1"): 2 + 4, ("T1", "E2"): 6}
    rows = [(t, e, c) for (t, e), c in merged.items()]
    r = split_work("W9", rows)
    assert r.status == PENDING
    r2 = split_work("W9", rows, title_ledger=1)
    assert r2.status == UNIQUE
    assert r2.title_counts == {"T1": 1}
    assert r2.exhibition_counts == {"E1": 6, "E2": 6}


def test_empty_work():
    assert split_work("W10", []).status == EMPTY


def test_rejoin_one_sided_and_empty():
    assert rejoin_counts({"T1": 3}, {}) == [("T1", None, 3)]
    assert rejoin_counts({}, {"E1": 4}) == [(None, "E1", 4)]
    assert rejoin_counts({}, {}) == []
