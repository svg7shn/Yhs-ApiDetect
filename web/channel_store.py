"""Independent SQLite channel record store; never accepts credentials."""
from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .channel_security import redact

FIELDS = ("id", "base_url", "model", "family", "claimed_source", "source_label", "verdict", "score", "capability_accuracy", "created_at", "duration_ms", "evidence", "summary")
MAX_RECORDS = 5000


def connect():
    directory = Path(os.environ.get("Yhs-ApiDetect_CHANNEL_DIR", str(Path(__file__).resolve().parent.parent / "web_data" / "channel")))
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / "records.sqlite3", timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
    return conn


def save(record):
    # get(k)：旧记录可能缺少后加的字段，缺省为 None，读取端负责显示占位符。
    safe = redact({k: record.get(k) for k in FIELDS})
    conn = connect()
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?)", (safe["id"], safe["created_at"], json.dumps(safe, ensure_ascii=False)))
            conn.execute("DELETE FROM records WHERE id NOT IN (SELECT id FROM records ORDER BY created_at DESC LIMIT ?)", (MAX_RECORDS,))
    finally:
        conn.close()


def get(record_id):
    if not re.fullmatch(r"[0-9a-f]{32}", record_id):
        return None
    conn = connect()
    try:
        row = conn.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
        return redact(json.loads(row[0])) if row else None
    finally:
        conn.close()


def query(days=7, q=""):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        rows = conn.execute("SELECT payload FROM records WHERE created_at>=? ORDER BY created_at DESC LIMIT ?", (cutoff, MAX_RECORDS)).fetchall()
        records = [redact(json.loads(row[0])) for row in rows]
        return [r for r in records if q.casefold() in r["base_url"].casefold()]
    finally:
        conn.close()


def csv_safe(value):
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def export(records, fmt):
    records = redact(records)
    if fmt == "json":
        return json.dumps({"records": records}, ensure_ascii=False, indent=2), "application/json"
    if fmt == "csv":
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(FIELDS)
        writer.writerows([csv_safe(r.get(k)) for k in FIELDS] for r in records)
        return "\ufeff" + out.getvalue(), "text/csv"
    def esc(value):
        return html.escape(str(value)).replace("\n", " ").replace("\r", " ").replace("[", "&#91;").replace("]", "&#93;").replace("*", "&#42;").replace("_", "&#95;").replace("\x00", "")
    lines = ["# 渠道检测记录", "", "评分为加权通过率（信息性信号不计分），不是真实性概率；结论为概率判定，来源与订阅身份未获密码学证明或账单核验。", ""]
    for r in records:
        lines += ["## " + esc(r["model"]), "", "地址：" + esc(r["base_url"]), "来源声明：" + esc(r["source_label"]), "结论：" + esc(r["verdict"]), "通过率：" + esc(r["score"]), esc(r["summary"])]
        lines += [esc(e["name"]) + " / " + esc(e["status"]) + "：" + esc(e["detail"]) for e in r["evidence"]]
        lines.append("")
    return "\n".join(lines), "text/markdown"
