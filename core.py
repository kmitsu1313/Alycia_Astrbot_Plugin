"""与 AstrBot 无关的纯逻辑：逐条记录的存取、日记素材拼装、ManicTime 摘要渲染。

不 import astrbot，宿主机上直接 `python -c "import core"` 就能测。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

TS_FMT = "%Y-%m-%d %H:%M:%S"
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 与 Favour_Ultra 的识别规则同宽：[好感度 上升：1] / [好感度 持平] / 全角括号 / 字间夹杂符号。
FAVOUR_TAG_RE = re.compile(
    r"[\[［]\s*好[^一-鿿]{0,2}感[^一-鿿]{0,2}度[^\]］]{0,24}[\]］]"
)


def now_str() -> str:
    return datetime.now().strftime(TS_FMT)


def weekday_cn(d: date) -> str:
    return WEEKDAYS[d.weekday()]


def parse_date(text: str) -> date | None:
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m-%d", "%m%d"):
        try:
            d = datetime.strptime(text, fmt).date()
            if fmt in ("%m-%d", "%m%d"):
                d = d.replace(year=date.today().year)
            return d
        except ValueError:
            continue
    return None


def strip_favour_tags(text: str) -> tuple[str, list[str]]:
    """剥掉好感度标签，返回 (干净文本, 标签列表)。"""
    if not text:
        return "", []
    tags = [m.group(0) for m in FAVOUR_TAG_RE.finditer(text)]
    cleaned = FAVOUR_TAG_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, tags


class SafeDict(dict):
    """format_map 用：模板里写了未知占位符就原样保留，不抛 KeyError。"""

    def __missing__(self, key):
        return "{" + key + "}"


def render_template(template: str, **kwargs) -> str:
    try:
        return (template or "").format_map(SafeDict(**kwargs))
    except (ValueError, IndexError):
        # 模板里有孤立的大括号之类，退化成直接拼接
        return (template or "") + "\n" + "\n".join(str(v) for v in kwargs.values())


# ---------------------------------------------------------------------------
# 逐条记录：data/plugin_data/<plugin>/log/YYYY-MM-DD.jsonl，一行一条
# ---------------------------------------------------------------------------
class RecordStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path_for(self, d: date) -> Path:
        return self.root / f"{d.isoformat()}.jsonl"

    def append(self, record: dict) -> None:
        ts = record.get("ts") or now_str()
        record["ts"] = ts
        d = datetime.strptime(ts[:10], "%Y-%m-%d").date()
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with open(self.path_for(d), "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def read_day(self, d: date) -> list[dict]:
        p = self.path_for(d)
        if not p.exists():
            return []
        out: list[dict] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        out.sort(key=lambda r: r.get("ts", ""))
        return out

    def count_day(self, d: date) -> int:
        return len(self.read_day(d))

    def dates(self) -> list[date]:
        out = []
        for p in self.root.glob("????-??-??.jsonl"):
            try:
                out.append(date.fromisoformat(p.stem))
            except ValueError:
                continue
        return sorted(out)


CONVERSATION_KINDS = ("user", "assistant", "proactive")


def channel_label(r: dict) -> str:
    if r.get("group_id"):
        return f"群{r.get('group_name') or r['group_id']}"
    return f"{r.get('platform_id') or r.get('platform') or ''}私聊"


def render_conversation(
    records: list[dict],
    self_label: str = "我",
    include_reasoning: bool = False,
    max_chars: int = 24000,
) -> str:
    """把一天的记录排成给模型看的对话稿。多个会话混在一天时才标渠道。"""
    convo = [r for r in records if r.get("kind") in CONVERSATION_KINDS]
    umos = {r.get("umo") for r in convo if r.get("umo")}
    multi = len(umos) > 1
    lines: list[str] = []
    for r in convo:
        hm = (r.get("ts") or "")[11:16]
        where = f"（{channel_label(r)}）" if multi else ""
        kind = r.get("kind")
        if kind == "user":
            name = r.get("user_name") or r.get("user_id") or "对方"
            lines.append(f"[{hm}] {name}{where}: {r.get('content', '')}")
        elif kind == "proactive":
            lines.append(f"[{hm}] （{self_label}主动开口{where}）")
        elif kind == "assistant":
            if include_reasoning and r.get("reasoning"):
                lines.append(f"[{hm}] （{self_label}心里想：{r['reasoning']}）")
            lines.append(f"[{hm}] {self_label}: {r.get('content', '')}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = "……（更早的部分略去）\n" + text[-max_chars:]
    return text


# ---------------------------------------------------------------------------
# 日记：data/plugin_data/<plugin>/diary/YYYY-MM-DD.md，首行是 "# 日期 周几"
# ---------------------------------------------------------------------------
class DiaryStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, d: date) -> Path:
        return self.root / f"{d.isoformat()}.md"

    def write(self, d: date, body: str, reasoning: str | None = None) -> Path:
        p = self.path_for(d)
        p.write_text(f"# {d.isoformat()} {weekday_cn(d)}\n\n{body.strip()}\n", encoding="utf-8")
        if reasoning:
            (self.root / f"{d.isoformat()}.reasoning.txt").write_text(reasoning, encoding="utf-8")
        return p

    def read(self, d: date) -> str | None:
        p = self.path_for(d)
        if not p.exists():
            return None
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        if lines and lines[0].startswith("# "):
            lines = lines[1:]
        return "\n".join(lines).strip()

    def dates(self) -> list[date]:
        out = []
        for p in self.root.glob("????-??-??.md"):
            try:
                out.append(date.fromisoformat(p.stem))
            except ValueError:
                continue
        return sorted(out)

    def latest_before(self, d: date, n: int) -> list[tuple[date, str]]:
        """早于 d 的最近 n 篇，按时间正序返回。"""
        picked = [x for x in self.dates() if x < d][-max(n, 0):] if n > 0 else []
        return [(x, self.read(x) or "") for x in picked]

    def latest(self, today: date, max_age_days: int) -> tuple[date, str] | None:
        """最近一篇（日期 ≤ 今天），太旧就不要。"""
        cands = [x for x in self.dates() if x <= today]
        if not cands:
            return None
        d = cands[-1]
        if max_age_days > 0 and (today - d).days > max_age_days:
            return None
        return d, self.read(d) or ""


def relative_day_cn(d: date, today: date) -> str:
    delta = (today - d).days
    if delta <= 0:
        return "今天"
    if delta == 1:
        return "昨天"
    if delta == 2:
        return "前天"
    return f"{delta} 天前"


def build_diary_prompt(
    template: str,
    target: date,
    today: date,
    conversation: str,
    previous: list[tuple[date, str]],
    activity: str = "",
) -> str:
    if previous:
        prev_block = "\n\n".join(
            f"【{d.isoformat()} {weekday_cn(d)} 的日记】\n{t}" for d, t in previous
        )
    else:
        prev_block = "（之前还没有写过日记）"
    activity_block = (
        f"【{target.isoformat()} 对方电脑上的活动（来自屏幕时间记录）】\n{activity}"
        if activity
        else ""
    )
    return render_template(
        template,
        date=target.isoformat(),
        weekday=weekday_cn(target),
        today=today.isoformat(),
        today_weekday=weekday_cn(today),
        conversation=conversation or "（这一天没有任何对话）",
        previous_diaries=prev_block,
        activity=activity_block,
    )


# ---------------------------------------------------------------------------
# 日记定时：每天 HH:MM 写前一天；错过了在 catch_up_hours 内补
# ---------------------------------------------------------------------------
def parse_hhmm(text: str, default=(4, 30)) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*", text or "")
    if not m:
        return default
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return default
    return h, mi


def diary_due(now: datetime, write_time: str, catch_up_hours: float) -> date | None:
    """现在该不该写日记？该写就返回要回顾的那一天（昨天），否则 None。"""
    h, mi = parse_hhmm(write_time)
    due = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if now < due:
        # 还没到今天的点：看看昨天那个点是不是刚错过
        due -= timedelta(days=1)
    if now - due > timedelta(hours=max(catch_up_hours, 0)):
        return None
    return due.date() - timedelta(days=1)


# ---------------------------------------------------------------------------
# ManicTime 摘要（宿主机脚本导出的 latest.json）渲染成给模型看的文字
# ---------------------------------------------------------------------------
def load_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def age_minutes(generated_at: str, now: datetime | None = None) -> float | None:
    try:
        t = datetime.strptime(generated_at, TS_FMT)
    except (TypeError, ValueError):
        return None
    return ((now or datetime.now()) - t).total_seconds() / 60


def fmt_minutes(m: float) -> str:
    m = int(round(m))
    if m < 60:
        return f"{m} 分钟"
    h, r = divmod(m, 60)
    return f"{h} 小时 {r} 分" if r else f"{h} 小时"


def _hm(ts: str) -> str:
    return (ts or "")[11:16]


def _shorten(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def render_activity_now(data: dict, max_apps: int = 6, max_titles: int = 2, max_timeline: int = 5) -> str:
    """主动消息时注入的"现在在干嘛"。"""
    lines: list[str] = []
    gen = data.get("generated_at") or ""
    now = data.get("now") or {}
    state = now.get("state")
    state_cn = {"Active": "在用电脑", "Away": "离开电脑"}.get(state, "状态未知")
    head = f"现在 {_hm(gen)}，{state_cn}"
    if now.get("since"):
        head += f"（从 {_hm(now['since'])} 起）"
    lines.append(head + "。")
    if now.get("app"):
        cur = f"当前窗口：{now['app']}"
        if now.get("title"):
            cur += f" — {_shorten(now['title'], 80)}"
        if now.get("title_since"):
            am = age_minutes(now["title_since"], datetime.strptime(gen, TS_FMT)) if gen else None
            if am is not None and am >= 1:
                cur += f"（已停留 {fmt_minutes(am)}）"
        lines.append(cur)

    recent = data.get("recent") or {}
    if recent:
        hours = float(data.get("window_hours") or 3)
        lines.append(
            f"最近 {hours:g} 小时：使用 {fmt_minutes(recent.get('active_min', 0))}，"
            f"离开 {fmt_minutes(recent.get('away_min', 0))}。"
        )
        for app in (recent.get("apps") or [])[:max_apps]:
            titles = app.get("titles") or []
            tdesc = "；".join(
                f"{_shorten(t[0], 50)} {fmt_minutes(t[1])}" for t in titles[:max_titles] if t and t[0]
            )
            line = f"· {app.get('name')} {fmt_minutes(app.get('min', 0))}"
            if tdesc:
                line += f"（{tdesc}）"
            lines.append(line)
        docs = recent.get("docs") or []
        if docs:
            lines.append(
                "网站/文件：" + "、".join(f"{_shorten(d.get('name'), 40)} {fmt_minutes(d.get('min', 0))}" for d in docs[:6])
            )

    today_key = gen[:10]
    day = (data.get("days") or {}).get(today_key)
    if day:
        span = ""
        if day.get("first_active"):
            span = f"（{day['first_active']} 起）"
        lines.append(f"今天累计使用电脑 {fmt_minutes(day.get('active_min', 0))}{span}。")

    tl = data.get("timeline") or []
    if tl:
        parts = [f"{t.get('from')}–{t.get('to')} {t.get('app')}「{_shorten(t.get('title'), 36)}」" for t in tl[-max_timeline:]]
        lines.append("最近的切换：" + " → ".join(parts))
    return "\n".join(lines)


def render_activity_day(data: dict, day_iso: str, max_apps: int = 8) -> str:
    """写日记时附的"那一天在电脑上干了什么"。"""
    day = (data.get("days") or {}).get(day_iso)
    if not day:
        return ""
    span = ""
    if day.get("first_active") and day.get("last_active"):
        span = f"（{day['first_active']}–{day['last_active']}）"
    lines = [f"使用电脑 {fmt_minutes(day.get('active_min', 0))}{span}。"]
    apps = day.get("apps") or []
    if apps:
        lines.append("主要在：" + "；".join(f"{a.get('name')} {fmt_minutes(a.get('min', 0))}" for a in apps[:max_apps]))
    docs = day.get("docs") or []
    if docs:
        lines.append("网站/文件：" + "、".join(_shorten(d.get("name"), 40) for d in docs[:8]))
    return "\n".join(lines)
