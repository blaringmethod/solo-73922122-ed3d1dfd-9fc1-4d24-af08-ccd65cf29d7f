# -*- coding: utf-8 -*-
"""馆藏迁库拆表页 Flask 应用。"""
from __future__ import annotations

import io
import os

from flask import (
    Flask, Response, abort, flash, g, redirect, render_template,
    request, send_file, url_for,
)

from . import db, publish, service


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["DB_PATH"] = db_path or db.get_db_path()

    def get_conn() -> db.sqlite3.Connection:
        if "conn" not in g:
            g.conn = db.connect(app.config["DB_PATH"])
            db.init_db(g.conn)
        return g.conn

    @app.teardown_appcontext
    def close_conn(exc=None):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.route("/")
    def index():
        conn = get_conn()
        batch_id = db.current_batch_id(conn)
        version = db.current_version(conn)
        ledgers = db.get_ledgers(conn)
        results = service.evaluate_all(conn) if batch_id else []
        summary = service.summarize(results) if batch_id else None
        events = db.list_events(conn)
        return render_template(
            "index.html",
            batch_id=batch_id, version=version,
            results=results, summary=summary,
            ledgers=ledgers, events=events,
            status_labels=service.STATUS_LABELS,
            packages=db.list_packages(conn),
            latest_package_id=db.latest_package_id(conn),
            pending_plan=db.get_pending_plan(conn),
            publish_stats=service.publish_stats(conn),
        )

    @app.route("/upload", methods=["POST"])
    def upload():
        conn = get_conn()
        f = request.files.get("csv_file")
        if f is None or not f.filename:
            flash("请选择要上传的 CSV 文件", "error")
            return redirect(url_for("index"))
        raw = f.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gbk")
            except UnicodeDecodeError:
                flash("CSV 编码无法识别，请使用 UTF-8 或 GBK", "error")
                return redirect(url_for("index"))
        try:
            parsed = db.parse_wide_csv(text)
        except ValueError as exc:
            flash(f"CSV 解析失败：{exc}", "error")
            return redirect(url_for("index"))
        if not parsed:
            flash("CSV 中没有有效数据行", "error")
            return redirect(url_for("index"))
        batch_id = db.replace_wide_rows(conn, parsed)
        flash(f"已上传批次 {batch_id}，共 {len(parsed)} 个作品的宽表输入", "ok")
        return redirect(url_for("index"))

    @app.route("/works/<work_no>")
    def work_detail(work_no: str):
        conn = get_conn()
        all_rows = db.load_all_rows(conn)
        if work_no not in all_rows:
            abort(404)
        ledgers = db.get_ledgers(conn).get(
            work_no, {"title": None, "exhibition": None})
        from .splitting import split_work
        res = split_work(work_no, all_rows[work_no],
                         ledgers["title"], ledgers["exhibition"])
        return render_template(
            "work.html",
            work_no=work_no, rows=all_rows[work_no],
            ledgers=ledgers, result=res,
            status_labels=service.STATUS_LABELS,
        )

    @app.route("/works/<work_no>/ledger", methods=["POST"])
    def update_ledger(work_no: str):
        conn = get_conn()
        all_rows = db.load_all_rows(conn)
        if work_no not in all_rows:
            abort(404)
        kind = request.form.get("kind", "")
        raw_value = request.form.get("total", "").strip()
        if raw_value == "":
            value = None  # 清除登记
        else:
            try:
                value = int(raw_value)
            except ValueError:
                flash("底账总条数必须是整数（留空表示清除登记）", "error")
                return redirect(url_for("work_detail", work_no=work_no))
        try:
            db.upsert_ledger(conn, work_no, kind, value)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("work_detail", work_no=work_no))
        label = "题名" if kind == "title" else "参展"
        flash(f"作品 {work_no} 的{label}底账已"
              f"{'更正' if raw_value == '' else '登记'}为 "
              f"{value if value is not None else '（清除）'}", "ok")
        return redirect(url_for("work_detail", work_no=work_no))

    @app.route("/check", methods=["POST"])
    def check():
        conn = get_conn()
        if not db.current_batch_id(conn):
            flash("请先上传宽表 CSV", "error")
            return redirect(url_for("index"))
        snap_id, summary, results = service.run_check(conn)
        return redirect(url_for("snapshot_view", snap_id=snap_id))

    @app.route("/snapshots/<int:snap_id>")
    def snapshot_view(snap_id: int):
        conn = get_conn()
        snap = db.get_snapshot(conn, snap_id)
        if snap is None:
            abort(404)
        works = db.get_snapshot_works(conn, snap_id)
        current_version = db.current_version(conn)
        return render_template(
            "snapshot.html", snap=snap, works=works,
            current_version=current_version,
            status_labels=service.STATUS_LABELS,
        )

    @app.route("/download", methods=["POST"])
    def download():
        conn = get_conn()
        if not db.current_batch_id(conn):
            flash("请先上传宽表 CSV", "error")
            return redirect(url_for("index"))
        try:
            snap_id, payload = service.build_download(conn)
        except RuntimeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        return send_file(
            io.BytesIO(payload),
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"museum_details_snapshot{snap_id}.zip",
        )

    # ---------------- 入库增量发布 ----------------
    @app.route("/publish/calculate", methods=["POST"])
    def publish_calculate():
        conn = get_conn()
        try:
            plan_id, changes = service.calculate_plan(conn)
        except RuntimeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        if plan_id is None:
            flash("与最近发布相比份数无变化，不生成新包", "ok")
            return redirect(url_for("index"))
        flash(f"已生成增量发布计划 {plan_id}（补入/撤销共 {len(changes)} 个号段），"
              "请核对后提交", "ok")
        return redirect(url_for("publish_plan_view", plan_id=plan_id))

    @app.route("/publish/plans/<int:plan_id>")
    def publish_plan_view(plan_id: int):
        conn = get_conn()
        plan = db.get_plan(conn, plan_id)
        if plan is None:
            abort(404)
        ranges = db.get_plan_ranges(conn, plan_id)
        current_version = db.current_version(conn)
        latest_pkg = db.latest_package_id(conn)
        stale = (plan["status"] == "pending"
                 and (plan["data_version"] != current_version
                      or plan["base_package"] != latest_pkg))
        return render_template(
            "publish_plan.html", plan=plan, ranges=ranges, stale=stale,
            current_version=current_version, latest_package=latest_pkg,
            kind_labels=publish.KIND_LABELS, action_labels=publish.ACTION_LABELS,
        )

    @app.route("/publish/plans/<int:plan_id>/submit", methods=["POST"])
    def publish_submit(plan_id: int):
        conn = get_conn()
        try:
            package_id = service.submit_plan(conn, plan_id)
        except db.StaleStateError as exc:
            flash(f"{exc}", "error")
            return redirect(url_for("publish_plan_view", plan_id=plan_id))
        except RuntimeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        flash(f"包 {package_id} 已发布，份号状态已同步生效", "ok")
        return redirect(url_for("package_view", pkg_id=package_id))

    @app.route("/packages/<int:pkg_id>")
    def package_view(pkg_id: int):
        conn = get_conn()
        pkg = db.get_package(conn, pkg_id)
        if pkg is None:
            abort(404)
        ranges = db.get_package_ranges(conn, pkg_id)
        return render_template(
            "package.html", pkg=pkg, ranges=ranges,
            kind_labels=publish.KIND_LABELS, action_labels=publish.ACTION_LABELS,
        )

    @app.route("/packages/<int:pkg_id>/csv")
    def package_csv(pkg_id: int):
        conn = get_conn()
        pkg = db.get_package(conn, pkg_id)
        if pkg is None:
            abort(404)
        return Response(
            pkg["csv_content"].encode("utf-8-sig"),
            mimetype="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="package_{pkg_id:04d}.csv"'},
        )

    # ---------------- 机器可读接口（便于核验/自动化） ----------------
    @app.route("/api/state")
    def api_state():
        conn = get_conn()
        results = service.evaluate_all(conn) if db.current_batch_id(conn) else []
        summary = service.summarize(results) if results else {
            "work_count": 0, "unique_count": 0,
            "pending": [], "conflicts": [], "all_unique": False}
        return {
            "data_version": db.current_version(conn),
            "batch_id": db.current_batch_id(conn),
            "summary": summary,
            "works": results,
        }

    @app.route("/api/works/<work_no>/ledger", methods=["POST"])
    def api_ledger(work_no: str):
        conn = get_conn()
        if work_no not in db.load_all_rows(conn):
            abort(404)
        data = request.get_json(silent=True) or {}
        kind = data.get("kind", "")
        if kind not in ("title", "exhibition"):
            abort(400, "kind 必须为 title 或 exhibition")
        value = data.get("total")
        if value is not None:
            try:
                value = int(value)
            except (TypeError, ValueError):
                abort(400, "total 必须为整数或 null")
        try:
            db.upsert_ledger(conn, work_no, kind, value)
        except ValueError as exc:
            abort(400, str(exc))
        return {"ok": True, "data_version": db.current_version(conn)}

    @app.route("/api/check", methods=["POST"])
    def api_check():
        conn = get_conn()
        if not db.current_batch_id(conn):
            abort(400, "请先上传宽表 CSV")
        snap_id, summary, results = service.run_check(conn)
        return {"snapshot_id": snap_id,
                "data_version": db.current_version(conn),
                "summary": summary, "works": results}

    @app.route("/api/download", methods=["POST"])
    def api_download():
        conn = get_conn()
        try:
            snap_id, payload = service.build_download(conn)
        except RuntimeError as exc:
            abort(409, str(exc))
        return Response(
            payload,
            mimetype="application/zip",
            headers={
                "Content-Disposition":
                    f'attachment; filename="museum_details_snapshot{snap_id}.zip"',
                "X-Snapshot-Id": str(snap_id),
            },
        )

    @app.route("/api/publish/calculate", methods=["POST"])
    def api_publish_calculate():
        conn = get_conn()
        try:
            plan_id, changes = service.calculate_plan(conn)
        except RuntimeError as exc:
            abort(409, str(exc))
        if plan_id is None:
            return {"ok": True, "no_change": True, "plan_id": None,
                    "changes": []}
        return {"ok": True, "no_change": False, "plan_id": plan_id,
                "changes": changes}

    @app.route("/api/publish/plans/<int:plan_id>")
    def api_publish_plan(plan_id: int):
        conn = get_conn()
        plan = db.get_plan(conn, plan_id)
        if plan is None:
            abort(404)
        ranges = [dict(r) for r in db.get_plan_ranges(conn, plan_id)]
        return {"plan": dict(plan), "ranges": ranges,
                "current_version": db.current_version(conn),
                "latest_package": db.latest_package_id(conn)}

    @app.route("/api/publish/plans/<int:plan_id>/submit", methods=["POST"])
    def api_publish_submit(plan_id: int):
        conn = get_conn()
        try:
            package_id = service.submit_plan(conn, plan_id)
        except (db.StaleStateError, RuntimeError) as exc:
            abort(409, str(exc))
        return {"ok": True, "package_id": package_id}

    @app.route("/api/packages")
    def api_packages():
        conn = get_conn()
        return {"packages": [
            {"id": p["id"], "created_at": p["created_at"],
             "prev_id": p["prev_id"], "data_version": p["data_version"],
             "batch_id": p["batch_id"], "add_count": p["add_count"],
             "revoke_count": p["revoke_count"]}
            for p in db.list_packages(conn)]}

    @app.route("/api/packages/<int:pkg_id>")
    def api_package(pkg_id: int):
        conn = get_conn()
        pkg = db.get_package(conn, pkg_id)
        if pkg is None:
            abort(404)
        return {"package": {k: pkg[k] for k in pkg.keys() if k != "csv_content"},
                "ranges": [dict(r) for r in db.get_package_ranges(conn, pkg_id)]}

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(409)
    def handle_error(err):
        if request.path.startswith("/api/"):
            return {"error": getattr(err, "description", str(err))}, err.code
        return render_template("error.html", err=err), err.code

    return app
