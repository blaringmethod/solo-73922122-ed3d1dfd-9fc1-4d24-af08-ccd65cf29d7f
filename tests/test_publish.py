# -*- coding: utf-8 -*-
"""入库增量发布测试：份号发放/撤销、计划-提交、失效重试、CSV 重下、原子性。"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, publish, service  # noqa: E402

# W01: 山水=2 花鸟=3 / 春展=1 秋展=2；W02 册页甲=3；W03 巡展甲=2
BASE_CSV = """作品号,题名,展名,组合次数
W01,山水卷,春展,2
W01,山水卷,秋展,4
W01,花鸟卷,春展,3
W01,花鸟卷,秋展,6
W02,册页甲,,3
W03,,巡展甲,2
"""

# 减量：山水 2->1（撤 #2），册页甲 3->1（撤 #2..#3）
DEC_CSV = """作品号,题名,展名,组合次数
W01,山水卷,春展,1
W01,山水卷,秋展,2
W01,花鸟卷,春展,3
W01,花鸟卷,秋展,6
W02,册页甲,,1
W03,,巡展甲,2
"""

# 补入：山水 1->4（历史最大 2，发 #3..#5），册页甲 1->3（历史最大 3，发 #4..#5）
INC_CSV = """作品号,题名,展名,组合次数
W01,山水卷,春展,4
W01,山水卷,秋展,8
W01,花鸟卷,春展,3
W01,花鸟卷,秋展,6
W02,册页甲,,3
W03,,巡展甲,2
"""


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    app = create_app(db_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, app, db_path


def _upload(client, text):
    data = {"csv_file": (io.BytesIO(text.encode("utf-8")), "wide.csv")}
    return client.post("/upload", data=data, content_type="multipart/form-data",
                       follow_redirects=True)


def _calculate(client):
    rv = client.post("/api/publish/calculate")
    assert rv.status_code == 200, rv.get_json()
    return rv.get_json()


def _submit(client, plan_id):
    return client.post(f"/api/publish/plans/{plan_id}/submit")


def _copy_state(app):
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        state = db.load_copy_state(conn)
        conn.close()
    return state


def _active(state, work_no, kind, name):
    st = state.get((work_no, kind, name), {"max_issued": 0, "revoked": []})
    return publish.active_count(st["max_issued"], st["revoked"])


# ---------------------------------------------------------------- 纯逻辑

def test_top_active_ranges_single_and_multi_interval():
    # 无撤销：最大 3 个就是尾部一段
    assert publish.top_active_ranges(5, [], 3) == [(3, 5)]
    # 有效号 {1,2,3,5,6,7}，最大 5 个 = {2,3,5,6,7} -> 两段
    assert publish.top_active_ranges(7, [(4, 4)], 5) == [(2, 3), (5, 7)]
    # 全部撤光
    assert publish.top_active_ranges(4, [], 4) == [(1, 4)]
    with pytest.raises(ValueError):
        publish.top_active_ranges(2, [], 3)


def test_normalize_ranges_merges_adjacent():
    assert publish.normalize_ranges([(5, 7), (1, 2), (3, 3), (9, 9)]) == \
        [(1, 3), (5, 7), (9, 9)]


# ---------------------------------------------------------------- 首次发布

def test_first_publish_from_zero(client):
    c, app, _ = client
    _upload(c, BASE_CSV)
    plan = _calculate(c)
    assert plan["no_change"] is False
    # 首次从零起算：全部为补入，且各键从 1 开始
    adds = {(ch["work_no"], ch["kind"], ch["name"]):
            (ch["r_start"], ch["r_end"]) for ch in plan["changes"]}
    assert all(ch["action"] == "add" for ch in plan["changes"])
    assert adds[("W01", "title", "山水卷")] == (1, 2)
    assert adds[("W01", "title", "花鸟卷")] == (1, 3)
    assert adds[("W01", "exhibition", "春展")] == (1, 1)
    assert adds[("W01", "exhibition", "秋展")] == (1, 2)
    assert adds[("W02", "title", "册页甲")] == (1, 3)
    assert adds[("W03", "exhibition", "巡展甲")] == (1, 2)

    rv = _submit(c, plan["plan_id"])
    assert rv.status_code == 200
    assert rv.get_json()["package_id"] == 1

    # 包内容与 CSV
    pkg = c.get("/api/packages/1").get_json()
    assert pkg["package"]["prev_id"] == 0
    assert pkg["package"]["add_count"] == 13  # 2+3+1+2+3+2
    assert pkg["package"]["revoke_count"] == 0
    csv_text = c.get("/packages/1/csv").data.decode("utf-8-sig")
    assert "包号,1" in csv_text and "前一包号,0" in csv_text
    assert "W01,题名,山水卷,补入,1,2,2" in csv_text
    assert "W03,参展,巡展甲,补入,1,2,2" in csv_text

    state = _copy_state(app)
    assert _active(state, "W01", "title", "山水卷") == 2
    assert _active(state, "W02", "title", "册页甲") == 3
    # 再算一次：份数无变化 -> 不发包
    plan2 = _calculate(c)
    assert plan2["no_change"] is True and plan2["plan_id"] is None
    assert c.get("/api/packages").get_json()["packages"].__len__() == 1


# ---------------------------------------------------------------- 减量再补入

def test_decrement_then_increment_keeps_retired_numbers(client):
    c, app, _ = client
    _upload(c, BASE_CSV)
    _submit(c, _calculate(c)["plan_id"])

    # ---- 减量：撤掉最大有效份号 ----
    _upload(c, DEC_CSV)
    plan = _calculate(c)
    revokes = {(ch["work_no"], ch["kind"], ch["name"]): (ch["r_start"], ch["r_end"])
               for ch in plan["changes"]}
    assert all(ch["action"] == "revoke" for ch in plan["changes"])
    assert revokes[("W01", "title", "山水卷")] == (2, 2)      # 撤最大的 1 个
    assert revokes[("W02", "title", "册页甲")] == (2, 3)      # 撤最大的 2 个
    pkg2 = _submit(c, plan["plan_id"]).get_json()["package_id"]
    assert pkg2 == 2
    csv2 = c.get("/packages/2/csv").data.decode("utf-8-sig")
    assert "前一包号,1" in csv2
    assert "W01,题名,山水卷,撤销,2,2,1" in csv2
    assert "W02,题名,册页甲,撤销,2,3,2" in csv2

    state = _copy_state(app)
    assert _active(state, "W01", "title", "山水卷") == 1
    assert _active(state, "W02", "title", "册页甲") == 1

    # ---- 补入：从历史最大份号接着发，撤掉的号不复用 ----
    _upload(c, INC_CSV)
    plan = _calculate(c)
    adds = {(ch["work_no"], ch["kind"], ch["name"]): (ch["r_start"], ch["r_end"])
            for ch in plan["changes"]}
    assert all(ch["action"] == "add" for ch in plan["changes"])
    assert adds[("W01", "title", "山水卷")] == (3, 5)   # 历史最大 2 -> 发 3..5
    assert adds[("W02", "title", "册页甲")] == (4, 5)   # 历史最大 3 -> 发 4..5
    pkg3 = _submit(c, plan["plan_id"]).get_json()["package_id"]
    assert pkg3 == 3
    csv3 = c.get("/packages/3/csv").data.decode("utf-8-sig")
    assert "前一包号,2" in csv3
    assert "W01,题名,山水卷,补入,3,5,3" in csv3

    state = _copy_state(app)
    # 有效号：山水 {1,3,4,5}，册页甲 {1,4,5}；2 号（及册页甲 2,3）永久停用
    assert _active(state, "W01", "title", "山水卷") == 4
    assert _active(state, "W02", "title", "册页甲") == 3
    assert state[("W01", "title", "山水卷")]["revoked"] == [(2, 2)]
    assert state[("W02", "title", "册页甲")]["revoked"] == [(2, 3)]
    assert state[("W01", "title", "山水卷")]["max_issued"] == 5
    assert state[("W02", "title", "册页甲")]["max_issued"] == 5


def test_delete_item_then_readd_still_keeps_retired(client):
    c, app, _ = client
    _upload(c, BASE_CSV)
    _submit(c, _calculate(c)["plan_id"])

    # 整项删掉：新宽表没有 W02 -> 册页甲全部撤销
    no_w02 = ("作品号,题名,展名,组合次数\n"
              "W01,山水卷,春展,2\nW01,山水卷,秋展,4\n"
              "W01,花鸟卷,春展,3\nW01,花鸟卷,秋展,6\n"
              "W03,,巡展甲,2\n")
    _upload(c, no_w02)
    plan = _calculate(c)
    assert [(ch["name"], ch["action"], ch["r_start"], ch["r_end"])
            for ch in plan["changes"]] == [("册页甲", "revoke", 1, 3)]
    _submit(c, plan["plan_id"])
    state = _copy_state(app)
    assert _active(state, "W02", "title", "册页甲") == 0
    assert state[("W02", "title", "册页甲")]["revoked"] == [(1, 3)]

    # 再补回：从历史最大 3 接着发，1..3 永不复用
    back = no_w02 + "W02,册页甲,,2\n"
    _upload(c, back)
    plan = _calculate(c)
    assert [(ch["name"], ch["action"], ch["r_start"], ch["r_end"])
            for ch in plan["changes"]] == [("册页甲", "add", 4, 5)]
    _submit(c, plan["plan_id"])
    state = _copy_state(app)
    assert _active(state, "W02", "title", "册页甲") == 2
    assert state[("W02", "title", "册页甲")]["revoked"] == [(1, 3)]
    assert state[("W02", "title", "册页甲")]["max_issued"] == 5


def test_multi_interval_revoke_in_one_package(client):
    c, app, _ = client
    # 册页甲 7 份 -> 撤到 3 -> 补到 6（发 8..10）-> 撤到 1
    _upload(c, "作品号,题名,展名,组合次数\nW02,册页甲,,7\n")
    _submit(c, _calculate(c)["plan_id"])
    _upload(c, "作品号,题名,展名,组合次数\nW02,册页甲,,3\n")
    _submit(c, _calculate(c)["plan_id"])          # 撤 [4,7]
    _upload(c, "作品号,题名,展名,组合次数\nW02,册页甲,,6\n")
    _submit(c, _calculate(c)["plan_id"])          # 补 [8,10]
    _upload(c, "作品号,题名,展名,组合次数\nW02,册页甲,,1\n")
    plan = _calculate(c)
    # 有效 {1,2,3,8,9,10} 撤 5 个 -> 两段 [2,3] 与 [8,10]
    segs = [(ch["r_start"], ch["r_end"]) for ch in plan["changes"]]
    assert segs == [(2, 3), (8, 10)]
    pkg_id = _submit(c, plan["plan_id"]).get_json()["package_id"]
    csv_text = c.get(f"/packages/{pkg_id}/csv").data.decode("utf-8-sig")
    assert "W02,题名,册页甲,撤销,2,3,2" in csv_text
    assert "W02,题名,册页甲,撤销,8,10,3" in csv_text
    state = _copy_state(app)
    assert _active(state, "W02", "title", "册页甲") == 1
    assert state[("W02", "title", "册页甲")]["revoked"] == [(2, 10)]


# ---------------------------------------------------------------- 提交校验

def test_stale_submit_after_upload_or_ledger_change(client):
    c, app, _ = client
    _upload(c, BASE_CSV)
    plan = _calculate(c)
    # 计算后又上传了新宽表 -> 提交提示重试（409）
    _upload(c, DEC_CSV)
    rv = _submit(c, plan["plan_id"])
    assert rv.status_code == 409
    assert "重新计算" in rv.get_json()["error"]
    # 页面提交也会闪回并提示
    rv = c.post(f"/publish/plans/{plan['plan_id']}/submit", follow_redirects=True)
    assert "重新计算".encode() in rv.data
    # 没有产生新包，原发布状态保留
    assert c.get("/api/packages").get_json()["packages"] == []
    # 重新计算后可正常提交
    plan2 = _calculate(c)
    assert _submit(c, plan2["plan_id"]).status_code == 200


def test_stale_submit_after_another_package_published(client):
    c, app, db_path = client
    _upload(c, BASE_CSV)
    plan = _calculate(c)
    # 人为制造一个“最近一包已变动”的场景：另起连接直接改计划基准
    with app.app_context():
        conn = db.connect(db_path)
        db.init_db(conn)
        # 直接插入一条已发布包，使最近包号不再是计划计算时的 0
        conn.execute(
            "INSERT INTO packages(prev_id, data_version, batch_id, add_count, "
            "revoke_count, csv_content) VALUES(0, 0, 1, 0, 0, 'x')")
        conn.commit()
        conn.close()
    rv = _submit(c, plan["plan_id"])
    assert rv.status_code == 409
    assert "重新计算" in rv.get_json()["error"]


def test_calculate_requires_all_unique(client):
    c, app, _ = client
    # W3 矩阵多解且未登记底账 -> 待补，不能计算
    _upload(c, "作品号,题名,展名,组合次数\n"
               "W3,T1,E1,6\nW3,T1,E2,12\nW3,T2,E1,12\nW3,T2,E2,24\n")
    rv = c.post("/api/publish/calculate")
    assert rv.status_code == 409
    assert "全部唯一" in rv.get_json()["error"]
    rv = c.post("/publish/calculate", follow_redirects=True)
    assert "全部唯一".encode() in rv.data
    # 未上传任何数据时同样拒绝
    c2 = create_app(os.path.join(os.path.dirname(app.config["DB_PATH"]),
                                 "empty.db"))
    c2.config["TESTING"] = True
    with c2.test_client() as cc:
        assert cc.post("/api/publish/calculate").status_code == 409


def test_double_submit_rejected(client):
    c, app, _ = client
    _upload(c, BASE_CSV)
    plan = _calculate(c)
    assert _submit(c, plan["plan_id"]).status_code == 200
    rv = _submit(c, plan["plan_id"])
    assert rv.status_code == 409
    assert "已发布" in rv.get_json()["error"]


# ---------------------------------------------------------------- 原子性

def test_failed_submit_rolls_back_everything(client, monkeypatch):
    c, app, db_path = client
    _upload(c, BASE_CSV)
    plan = _calculate(c)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟生成 CSV 失败")

    monkeypatch.setattr(publish, "build_package_csv", boom)
    rv = _submit(c, plan["plan_id"])
    assert rv.status_code == 409
    monkeypatch.undo()
    # 原发布状态保留：无包、无份号状态、计划仍待提交
    with app.app_context():
        conn = db.connect(db_path)
        db.init_db(conn)
        assert db.latest_package_id(conn) == 0
        assert db.load_copy_state(conn) == {}
        assert db.get_plan(conn, plan["plan_id"])["status"] == "pending"
        conn.close()
    # 修复后可重新提交成功
    assert _submit(c, plan["plan_id"]).status_code == 200


# ---------------------------------------------------------------- 页面与重下

def test_pages_and_csv_redownload(client):
    c, app, _ = client
    _upload(c, BASE_CSV)
    # 首页包含增量发布入口
    index = c.get("/")
    assert "入库增量发布".encode() in index.data
    assert "从未发布".encode() in index.data

    rv = c.post("/publish/calculate", follow_redirects=False)
    assert rv.status_code == 302
    plan_page = c.get(rv.headers["Location"])
    assert plan_page.status_code == 200
    assert "提交发布".encode() in plan_page.data
    assert "补入".encode() in plan_page.data

    plan_id = int(rv.headers["Location"].rstrip("/").split("/")[-1])
    rv = c.post(f"/publish/plans/{plan_id}/submit", follow_redirects=False)
    assert rv.status_code == 302
    pkg_page = c.get(rv.headers["Location"])
    assert "增量包 #1".encode() in pkg_page.data
    assert "重下原 CSV".encode() in pkg_page.data

    # 凭包号重下原 CSV：两次下载内容一致
    first = c.get("/packages/1/csv")
    second = c.get("/packages/1/csv")
    assert first.status_code == 200 and first.data == second.data
    assert first.headers["Content-Disposition"].endswith('package_0001.csv"')
    # 首页现在显示最近一包与在册份数
    index = c.get("/")
    assert "#1".encode() in index.data
    assert "重下 CSV".encode() in index.data
    # 不存在的包 404
    assert c.get("/packages/99").status_code == 404
    assert c.get("/packages/99/csv").status_code == 404
