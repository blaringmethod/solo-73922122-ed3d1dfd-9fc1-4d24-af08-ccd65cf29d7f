# -*- coding: utf-8 -*-
"""gunicorn / flask 运行入口。"""
import os

from app import create_app

app = create_app(os.environ.get("SPLIT_DB_PATH", "/data/museum.db"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
