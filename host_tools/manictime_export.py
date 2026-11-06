"""把 ManicTime 的 ManicTimeReports.db 聚合成一份小 JSON，给容器里的插件读。

宿主机运行（计划任务每 5 分钟一次，见 install_task.ps1）。只读打开数据库，不写。无第三方依赖。

输出结构（插件 core.render_activity_now / render_activity_day 按这个渲染）：
{
  "generated_at": "2026-11-02 22:20:31", "window_hours": 3,
  "now": {"state": "Active|Away", "since": ts, "app": str, "title": str, "title_since": ts},
  "recent": {"from": ts, "to": ts, "active_min": n, "away_min": n,
             "apps": [{"name", "min", "titles": [[title, min], ...]}], "docs": [{"name", "min"}]},
  "days": {"YYYY-MM-DD": {"active_min", "first_active": "HH:MM", "last_active": "HH:MM", "apps": [...], "docs": [...]}},
  "timeline": [{"from": "HH:MM", "to": "HH:MM", "app", "title"}]
}
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta

TS = "%Y-%m-%d %H:%M:%S"
DEFAULT_DB = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Finkit", "ManicTime", "ManicTimeReports.db")
DEFAULT_OUT = r"C:\Users\uuuiii13\data\plugin_data\astrbot_plugin_alycia_journal\manictime\latest.json"
REPORT_USAGE, REPORT_APPS, REPORT_DOCS = 2, 3, 4  # Ar_Timeline.ReportId：ComputerUsage / Applications / Documents


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(str(s)[:19])


ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


def clean_title(title: str, app: str) -> str:
    """去零宽字符；窗口标题末尾重复的 " - 应用名" 去掉（Edge 的名字里夹着零宽空格，所以先清再比）。"""
    t = (title or "").translate(ZERO_WIDTH).strip()
    a = (app or "").translate(ZERO_WIDTH).strip()
    if a and t.lower().endswith(" - " + a.lower()):
        t = t[: -(len(a) + 3)].rstrip()
    return t


def open_db(path: str) -> tuple[sqlite3.Connection, str | None]:
    """先只读直开；ManicTime 锁着就拷一份到临时目录再读。返回 (连接, 临时目录或 None)。"""
    uri = "file:" + os.path.abspath(path).replace("\\", "/") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=10)
        con.execute("SELECT 1 FROM Ar_Activity LIMIT 1").fetchone()
        return con, None
    except sqlite3.Error:
        tmp = tempfile.mkdtemp(prefix="mt_export_")
        dst = os.path.join(tmp, "copy.db")
        shutil.copy2(path, dst)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(path + suffix):
                shutil.copy2(path + suffix, dst + suffix)
        return sqlite3.connect(dst), tmp


def fetch(con: sqlite3.Connection, report_id: int, since: datetime) -> list[tuple]:
    sql = """SELECT a.StartLocalTime, a.EndLocalTime, COALESCE(g.Name, ''), COALESCE(a.Name, ''), a.IsActive
             FROM Ar_Activity a
             LEFT JOIN Ar_Group g ON g.GroupId = a.GroupId AND g.ReportId = a.ReportId
             WHERE a.ReportId = ? AND a.EndLocalTime >= ?
             ORDER BY a.StartLocalTime"""
    rows = []
    for s, e, g, n, act in con.execute(sql, (report_id, since.strftime(TS))):
        try:
            sd, ed = parse_ts(s), parse_ts(e)
        except (TypeError, ValueError):
            continue
        g = (g or "").translate(ZERO_WIDTH).strip()
        n = clean_title(n, g) if report_id == REPORT_APPS else (n or "").translate(ZERO_WIDTH).strip()
        rows.append((sd, ed, g, n, act))
    return rows


def overlap_min(s: datetime, e: datetime, lo: datetime, hi: datetime) -> float:
    a, b = max(s, lo), min(e, hi)
    return max((b - a).total_seconds(), 0) / 60


def top(d: dict, n: int) -> list[dict]:
    return [{"name": k, "min": round(v, 1)} for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n] if v >= 0.5]


def summarize_range(apps_rows, docs_rows, usage_rows, lo, hi, n_apps=8, n_titles=3, n_docs=8) -> dict:
    app_min: dict[str, float] = defaultdict(float)
    title_min: dict[tuple[str, str], float] = defaultdict(float)
    for s, e, g, n, _ in apps_rows:
        m = overlap_min(s, e, lo, hi)
        if m <= 0:
            continue
        app_min[g or n] += m
        title_min[(g or n, n)] += m
    doc_min: dict[str, float] = defaultdict(float)
    for s, e, g, n, _ in docs_rows:
        m = overlap_min(s, e, lo, hi)
        if m > 0:
            doc_min[g or n] += m
    active = away = 0.0
    first_active = last_active = None
    for s, e, g, n, act in usage_rows:
        m = overlap_min(s, e, lo, hi)
        if m <= 0:
            continue
        if (g or n) == "Active" or act == 1:
            active += m
            first_active = min(first_active, max(s, lo)) if first_active else max(s, lo)
            last_active = max(last_active, min(e, hi)) if last_active else min(e, hi)
        else:
            away += m
    apps = []
    for item in top(app_min, n_apps):
        titles = sorted(
            ((t, m) for (a, t), m in title_min.items() if a == item["name"] and t and t != a),
            key=lambda x: -x[1],
        )[:n_titles]
        item["titles"] = [[t, round(m, 1)] for t, m in titles if m >= 0.5]
        apps.append(item)
    out = {
        "active_min": round(active, 1),
        "away_min": round(away, 1),
        "apps": apps,
        "docs": top(doc_min, n_docs),
    }
    if first_active:
        out["first_active"] = first_active.strftime("%H:%M")
    if last_active:
        # 一直用到跨过午夜被截在 hi 上时，显示 24:00 而不是 00:00
        out["last_active"] = "24:00" if (last_active == hi and hi.time() == datetime.min.time()) else last_active.strftime("%H:%M")
    return out


def build(db_path: str, hours: float, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    today = now.date()
    yesterday = today - timedelta(days=1)
    since = datetime.combine(yesterday, datetime.min.time())
    con, tmp = open_db(db_path)
    try:
        apps_rows = fetch(con, REPORT_APPS, since)
        docs_rows = fetch(con, REPORT_DOCS, since)
        usage_rows = fetch(con, REPORT_USAGE, since)
    finally:
        con.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    # 现在：最后一段使用状态 + 最后一个窗口（3 分钟内才算"当前"）
    now_block: dict = {}
    if usage_rows:
        s, e, g, n, act = max(usage_rows, key=lambda r: r[1])
        now_block["state"] = "Active" if ((g or n) == "Active" or act == 1) else "Away"
        now_block["since"] = s.strftime(TS)
    if apps_rows:
        s, e, g, n, _ = max(apps_rows, key=lambda r: r[1])
        if (now - e) <= timedelta(minutes=3):
            now_block["app"] = g or n
            now_block["title"] = n
            now_block["title_since"] = s.strftime(TS)

    lo = now - timedelta(hours=hours)
    recent = summarize_range(apps_rows, docs_rows, usage_rows, lo, now, n_apps=6, n_titles=3, n_docs=6)
    recent["from"] = lo.strftime(TS)
    recent["to"] = now.strftime(TS)

    days = {}
    for d in (yesterday, today):
        d_lo = datetime.combine(d, datetime.min.time())
        d_hi = min(d_lo + timedelta(days=1), now)
        days[d.isoformat()] = summarize_range(apps_rows, docs_rows, usage_rows, d_lo, d_hi)

    # 时间线：同一窗口被 ManicTime 切成的连续小段合并（间隔 ≤ 1 分钟），再丢掉不到 30 秒的
    merged: list[list] = []
    for s, e, g, n, _ in apps_rows[-200:]:
        app = g or n
        if merged and merged[-1][2] == app and merged[-1][3] == n and (s - merged[-1][1]) <= timedelta(minutes=1):
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, app, n])
    timeline = [
        {"from": s.strftime("%H:%M"), "to": e.strftime("%H:%M"), "app": app, "title": n}
        for s, e, app, n in merged
        if (e - s) >= timedelta(seconds=30)
    ][-10:]

    return {
        "generated_at": now.strftime(TS),
        "window_hours": hours,
        "now": now_block,
        "recent": recent,
        "days": days,
        "timeline": timeline,
    }


def write_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 ManicTime 活动摘要 JSON")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--hours", type=float, default=3.0, help="最近几小时的窗口")
    ap.add_argument("--print", action="store_true", help="同时打印到 stdout")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"找不到数据库：{args.db}", file=sys.stderr)
        return 2
    try:
        data = build(args.db, args.hours)
        write_atomic(args.out, data)
    except Exception as e:  # noqa: BLE001
        print(f"导出失败：{e!r}", file=sys.stderr)
        return 1
    if args.print:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(data, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
