# -*- coding: utf-8 -*-
"""端到端测试：上传 -> 核对 -> 补底账 -> 再核对 -> 打包下载 -> 更正后停下载。"""
import io
import os
import sys
import tempfile
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, service  # noqa: E402


GOOD_CSV = """作品号,题名,展名,组合次数
W1,山水,故宫特展,2
W1,山水,南洋巡展,4
W1,花鸟,故宫特展,3
W1,花鸟,南洋巡展,6
W2,册页甲,,2
W2,册页乙,,5
W3,T1,E1,6
W3,T1,E2,12
W3,T2,E1,12
W3,T2,E2,24
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


def test_full_workflow(client):
    c, app, db_path = client
    rv = _upload(c, GOOD_CSV)
    assert rv.status_code == 200

    # 初始状态：W1/W2 唯一，W3 待补
    state = c.get("/api/state").get_json()
    assert state["summary"]["work_count"] == 3
    assert state["summary"]["all_unique"] is False
    assert state["summary"]["pending"] == ["W3"]
    works = {w["work_no"]: w for w in state["works"]}
    assert works["W1"]["status"] == "unique"
    assert works["W1"]["title_counts"] == {"山水": 2, "花鸟": 3}
    assert works["W2"]["status"] == "unique"

    # 未全唯一时下载被拒（页面与 API 都应拒绝）
    rv = c.post("/download", follow_redirects=False)
    assert rv.status_code == 302  # 闪回首页
    rv_api = c.post("/api/download")
    assert rv_api.status_code == 409

    # 核对生成快照，快照中 W3 为 pending
    rv = c.post("/api/check")
    snap = rv.get_json()
    assert snap["summary"]["all_unique"] is False
    snap_id = snap["snapshot_id"]
    snap_page = c.get(f"/snapshots/{snap_id}")
    assert snap_page.status_code == 200
    assert "W3".encode() in snap_page.data

    # 补登 W3 题名底账 6
    rv = c.post("/api/works/W3/ledger", json={"kind": "title", "total": 6})
    assert rv.get_json()["ok"] is True
    state2 = c.get("/api/state").get_json()
    assert state2["summary"]["all_unique"] is True
    w3 = {w["work_no"]: w for w in state2["works"]}["W3"]
    assert w3["title_counts"] == {"T1": 2, "T2": 4}
    assert w3["exhibition_counts"] == {"E1": 3, "E2": 6}

    # 旧快照不受补登影响（只读，仍记录当时的 pending）
    old = c.get(f"/snapshots/{snap_id}")
    assert "待补底账".encode() in old.data

    # 打包下载，校验 zip 中两份 CSV 内容
    rv = c.post("/api/download")
    assert rv.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(rv.data))
    assert set(zf.namelist()) == {
        "title_details.csv", "exhibition_details.csv", "README.txt"}
    title_csv = zf.read("title_details.csv").decode("utf-8-sig")
    exhib_csv = zf.read("exhibition_details.csv").decode("utf-8-sig")
    assert "W3,T1,2" in title_csv and "W3,T2,4" in title_csv
    assert "W1,山水,2" in title_csv and "W2,册页乙,5" in title_csv
    assert "W3,E1,3" in exhib_csv and "W3,E2,6" in exhib_csv

    # 更正为错误底账 -> 冲突，新下载被停止
    c.post("/api/works/W3/ledger", json={"kind": "title", "total": 99})
    state3 = c.get("/api/state").get_json()
    assert state3["summary"]["all_unique"] is False
    assert state3["summary"]["conflicts"] == ["W3"]
    assert c.post("/api/download").status_code == 409

    # 清除错误登记 -> 回到待补；再登记正确值恢复唯一
    c.post("/api/works/W3/ledger", json={"kind": "title", "total": None})
    assert c.get("/api/state").get_json()["summary"]["pending"] == ["W3"]
    c.post("/api/works/W3/ledger", json={"kind": "exhibition", "total": 9})
    assert c.get("/api/state").get_json()["summary"]["all_unique"] is True


def test_conflicting_csv_blocks_download(client):
    c, app, _ = client
    bad = (
        "作品号,题名,展名,组合次数\n"
        "W1,T1,E1,2\nW1,T1,E2,4\nW1,T2,E1,3\nW1,T2,E2,7\n"  # 秩一冲突
    )
    _upload(c, bad)
    state = c.get("/api/state").get_json()
    assert state["summary"]["conflicts"] == ["W1"]
    assert c.post("/api/download").status_code == 409


def test_duplicate_combo_rows_are_summed(client):
    c, app, _ = client
    csv_text = (
        "作品号,题名,展名,组合次数\n"
        "W1,T1,E1,2\nW1,T1,E1,4\nW1,T1,E2,6\n"
    )
    _upload(c, csv_text)
    state = c.get("/api/state").get_json()
    w = state["works"][0]
    # 单题名矩阵结构上不唯一，补登题名底账 1 后锁定 x=1, y=(6,6)
    assert w["status"] == "pending"
    c.post("/api/works/W1/ledger", json={"kind": "title", "total": 1})
    state = c.get("/api/state").get_json()
    w = state["works"][0]
    assert w["status"] == "unique"
    assert w["exhibition_counts"] == {"E1": 6, "E2": 6}


def test_upload_replaces_old_input_and_bad_csv_rejected(client):
    c, app, _ = client
    _upload(c, GOOD_CSV)
    assert c.get("/api/state").get_json()["summary"]["work_count"] == 3
    # 坏 CSV（缺列）不应破坏已有数据
    rv = c.post(
        "/upload",
        data={"csv_file": (io.BytesIO("a,b\n1,2\n".encode()), "x.csv")},
        content_type="multipart/form-data", follow_redirects=True)
    assert rv.status_code == 200
    assert c.get("/api/state").get_json()["summary"]["work_count"] == 3
    # 新上传整体替换
    _upload(c, "作品号,题名,展名,组合次数\nW9,册页,,4\n")
    state = c.get("/api/state").get_json()
    assert state["summary"]["work_count"] == 1
    assert state["works"][0]["work_no"] == "W9"


def test_snapshot_is_frozen_while_edits_continue(client):
    c, app, _ = client
    _upload(c, GOOD_CSV)
    snap = c.post("/api/check").get_json()
    snap_id = snap["snapshot_id"]
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        rows = db.get_snapshot_works(conn, snap_id)
        statuses = {r["work_no"]: r["status"] for r in rows}
        assert statuses["W3"] == "pending"
        conn.close()
    # 补登后再核对得到新快照，新旧快照互不影响
    c.post("/api/works/W3/ledger", json={"kind": "title", "total": 6})
    snap2 = c.post("/api/check").get_json()
    assert snap2["snapshot_id"] != snap_id
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        old = {r["work_no"]: r["status"]
               for r in db.get_snapshot_works(conn, snap_id)}
        new = {r["work_no"]: r["status"]
               for r in db.get_snapshot_works(conn, snap2["snapshot_id"])}
        assert old["W3"] == "pending" and new["W3"] == "unique"
        conn.close()


def test_rejoin_self_check_matches_input(client):
    c, app, _ = client
    _upload(c, GOOD_CSV)
    c.post("/api/works/W3/ledger", json={"kind": "title", "total": 6})
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        results = service.evaluate_all(conn)
        errors = service.verify_rejoin(conn, results)
        assert errors == []
        conn.close()


def test_gbk_encoded_csv(client):
    c, app, _ = client
    text = "作品号,题名,展名,组合次数\nW1,山水,故宫展,2\nW1,花鸟,故宫展,3\n"
    rv = c.post(
        "/upload",
        data={"csv_file": (io.BytesIO(text.encode("gbk")), "gbk.csv")},
        content_type="multipart/form-data", follow_redirects=True)
    assert rv.status_code == 200
    state = c.get("/api/state").get_json()
    w = state["works"][0]
    assert w["status"] == "unique"
    assert w["title_counts"] == {"山水": 2, "花鸟": 3}


def test_invalid_count_and_missing_header_rejected(client):
    c, app, _ = client
    _upload(c, GOOD_CSV)
    # 非正整数次数
    bad = "作品号,题名,展名,组合次数\nW1,T1,E1,0\n"
    rv = _upload(c, bad)
    assert "正整数".encode() in rv.data
    # 缺列
    rv2 = c.post(
        "/upload",
        data={"csv_file": (io.BytesIO("作品号,题名\nW1,T1\n".encode()), "x.csv")},
        content_type="multipart/form-data", follow_redirects=True)
    assert "缺少列".encode() in rv2.data
    # 旧数据未被破坏
    assert c.get("/api/state").get_json()["summary"]["work_count"] == 3


def test_unknown_work_returns_404(client):
    c, app, _ = client
    _upload(c, GOOD_CSV)
    assert c.get("/works/NOPE").status_code == 404
    assert c.post("/api/works/NOPE/ledger",
                  json={"kind": "title", "total": 1}).status_code == 404
