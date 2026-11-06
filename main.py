"""Alycia 日记与记录。

三件事，提示词全在配置里（人格线写），这里只管管道：
1. 逐条记录：所有渠道、所有用户的每一轮——对方说了什么、她想了什么（模型思考）、她回了什么。
2. 日记：每天固定时间回顾前一天写一篇只给自己看的日记；之后每轮对话把最近一篇附在系统提示词末尾（不进历史）。
3. 看一眼 ManicTime：主动消息时把宿主机脚本导出的活动摘要附上。
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from quart import jsonify, request

from .core import (
    DiaryStore,
    RecordStore,
    age_minutes,
    build_diary_prompt,
    diary_due,
    load_json,
    now_str,
    parse_date,
    relative_day_cn,
    render_activity_day,
    render_activity_now,
    render_conversation,
    render_template,
    strip_favour_tags,
    weekday_cn,
)

PLUGIN_NAME = "astrbot_plugin_alycia_journal"
LOG = "[AlyciaJournal]"

# 占位默认值。真正的措辞由人格线在 WebUI 配置里写，这里只保证空配置也能跑。
DEFAULT_DIARY_SYSTEM_SUFFIX = "现在没有人在和你说话。你在写只给自己看的日记，想到哪写到哪，不用对任何人交代。"
DEFAULT_DIARY_PROMPT = (
    "今天是 {today}（{today_weekday}），回顾 {date}（{weekday}）这一天。\n\n"
    "{previous_diaries}\n\n"
    "【{date} 的对话】\n{conversation}\n\n"
    "{activity}\n\n"
    "写 {date} 的日记。只写正文，不要标题、不要日期行。"
)
DEFAULT_INJECT_TEMPLATE = "【你{relative}（{date} {weekday}）写的日记，只有你自己看过】\n{diary}"
DEFAULT_MANICTIME_TEMPLATE = "【你顺手看了一眼对方电脑的屏幕时间记录】\n{summary}"


def _safe(fn, default=""):
    try:
        v = fn()
        return default if v is None else v
    except Exception:
        return default


def _is_proactive(event) -> bool:
    """proactive_chat 的伪事件：类名 ProactiveMessageEvent，message_id 以 proactive 开头，message_str 为空。"""
    if type(event).__name__ == "ProactiveMessageEvent":
        return True
    mid = str(_safe(lambda: event.message_obj.message_id))
    return mid.startswith("proactive")


def _extract_reasoning(resp) -> str:
    """AstrBot 自己只认 reasoning_content，ollama 的 /v1 却放在 reasoning 里；从原始 completion 兜底。"""
    r = getattr(resp, "reasoning_content", None)
    if r:
        return str(r)
    raw = getattr(resp, "raw_completion", None)
    if raw is None:
        return ""
    try:
        msg = raw.choices[0].message
    except Exception:
        return ""
    for key in ("reasoning", "reasoning_content"):
        v = getattr(msg, key, None)
        if v:
            return str(v)
    extra = getattr(msg, "model_extra", None) or {}
    for key in ("reasoning", "reasoning_content"):
        if extra.get(key):
            return str(extra[key])
    return ""


def _jsonable(obj):
    if obj is None:
        return None
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    return str(obj)


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.records = RecordStore(self.data_dir / "log")
        self.diaries = DiaryStore(self.data_dir / "diary")
        self.manictime_dir = self.data_dir / "manictime"
        self.manictime_dir.mkdir(parents=True, exist_ok=True)
        self.manictime_file = self.manictime_dir / "latest.json"
        self.state_file = self.data_dir / "state.json"
        self.state: dict = load_json(self.state_file) or {}
        self._diary_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._scheduler: asyncio.Task | None = None
        self._register_web_api()
        self._start_scheduler()
        logger.info(f"{LOG} 已加载，数据目录 {self.data_dir}")

    def _register_web_api(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning(f"{LOG} 当前 AstrBot 版本不支持插件 View API，日记管理页面不可用")
            return

        routes = (
            (f"/{PLUGIN_NAME}/ui/overview", self.webui_overview, ["GET"], "Get journal overview"),
            (f"/{PLUGIN_NAME}/ui/diary/<day>", self.webui_get_diary, ["GET"], "Get a diary"),
            (f"/{PLUGIN_NAME}/ui/diary/<day>", self.webui_save_diary, ["POST"], "Save a diary"),
            (f"/{PLUGIN_NAME}/ui/generate", self.webui_generate_diary, ["POST"], "Generate a diary"),
            (f"/{PLUGIN_NAME}/ui/config", self.webui_get_config, ["GET"], "Get plugin configuration"),
            (f"/{PLUGIN_NAME}/ui/config", self.webui_save_config, ["POST"], "Save plugin configuration"),
        )
        for route, handler, methods, description in routes:
            register(route, handler, methods, description)

    @staticmethod
    def _webui_date(day: str) -> date | None:
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            return None
        return parsed if parsed.isoformat() == day else None

    @staticmethod
    def _webui_error(message: str, status_code: int = 400):
        return jsonify({"status": "error", "message": message}), status_code

    async def webui_overview(self):
        today = date.today()
        diary_dates = self.diaries.dates()
        data = load_json(self.manictime_file)
        generated_at = data.get("generated_at") if data else None
        age = age_minutes(generated_at) if generated_at else None
        return jsonify(
            {
                "today": today.isoformat(),
                "diary_count": len(diary_dates),
                "latest_diary": diary_dates[-1].isoformat() if diary_dates else None,
                "record_count_today": self.records.count_day(today),
                "record_count_yesterday": self.records.count_day(today - timedelta(days=1)),
                "diary_dates": [day.isoformat() for day in reversed(diary_dates[-365:])],
                "last_attempt": self.state.get("last_attempt"),
                "last_result": self.state.get("last_result"),
                "manictime": {
                    "generated_at": generated_at,
                    "age_minutes": age,
                },
            }
        )

    async def webui_get_diary(self, day: str):
        target = self._webui_date(day)
        if target is None:
            return self._webui_error("日期格式无效，应为 YYYY-MM-DD")
        path = self.diaries.path_for(target)
        return jsonify(
            {
                "date": target.isoformat(),
                "exists": path.exists(),
                "content": self.diaries.read(target) or "",
            }
        )

    async def webui_save_diary(self, day: str):
        target = self._webui_date(day)
        if target is None:
            return self._webui_error("日期格式无效，应为 YYYY-MM-DD")
        payload = await request.get_json(silent=True)
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str):
            return self._webui_error("日记正文必须是文本")
        if len(content) > 100_000:
            return self._webui_error("日记正文不能超过 100000 个字符")
        try:
            self.diaries.write(target, content)
        except (OSError, UnicodeError) as e:
            logger.error(f"{LOG} WebUI 保存 {target} 的日记失败：{e!r}")
            return self._webui_error("保存日记失败，请检查数据目录权限", status_code=500)
        return jsonify({"saved": True, "date": target.isoformat()})

    async def webui_generate_diary(self):
        payload = await request.get_json(silent=True)
        day = payload.get("date") if isinstance(payload, dict) else None
        target = self._webui_date(day) if isinstance(day, str) else None
        if target is None:
            return self._webui_error("日期格式无效，应为 YYYY-MM-DD")
        try:
            ok, message = await self.write_diary(target, force=True)
        except (OSError, UnicodeError) as e:
            logger.error(f"{LOG} WebUI 生成 {target} 的日记时写入失败：{e!r}")
            return self._webui_error("写入日记失败，请检查数据目录权限", status_code=500)
        return jsonify({"ok": ok, "message": message, "date": target.isoformat()})

    def _load_webui_config_schema(self) -> dict:
        schema_path = Path(__file__).with_name("_conf_schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if not isinstance(schema, dict):
            raise ValueError("插件配置 Schema 格式无效")
        return schema

    async def webui_get_config(self):
        try:
            schema = self._load_webui_config_schema()
            return jsonify({"schema": schema, "config": copy.deepcopy(dict(self.config))})
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.error(f"{LOG} WebUI 读取配置失败：{e!r}")
            return self._webui_error("读取插件配置失败", status_code=500)

    @staticmethod
    def _merge_webui_config(current: dict, updates: dict, schema: dict, prefix: str = "") -> dict:
        merged = copy.deepcopy(current)
        for key, value in updates.items():
            path = f"{prefix}.{key}" if prefix else key
            definition = schema.get(key)
            if not isinstance(definition, dict):
                raise ValueError(f"未知配置项：{path}")

            expected = definition.get("type")
            if expected == "object":
                if not isinstance(value, dict):
                    raise ValueError(f"{path} 必须是对象")
                old_value = merged.get(key, {})
                if not isinstance(old_value, dict):
                    old_value = {}
                items = definition.get("items")
                if not isinstance(items, dict):
                    raise ValueError(f"{path} 的配置定义无效")
                merged[key] = Main._merge_webui_config(old_value, value, items, path)
            elif expected == "bool":
                if type(value) is not bool:
                    raise ValueError(f"{path} 必须是布尔值")
                merged[key] = value
            elif expected == "int":
                if type(value) is not int:
                    raise ValueError(f"{path} 必须是整数")
                merged[key] = value
            elif expected == "float":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{path} 必须是有效数字")
                merged[key] = float(value)
            elif expected in ("string", "text"):
                if not isinstance(value, str):
                    raise ValueError(f"{path} 必须是文本")
                if len(value) > 100_000:
                    raise ValueError(f"{path} 不能超过 100000 个字符")
                merged[key] = value
            else:
                raise ValueError(f"{path} 使用了不支持的配置类型")
        return merged

    async def webui_save_config(self):
        payload = await request.get_json(silent=True)
        updates = payload.get("config") if isinstance(payload, dict) else None
        if not isinstance(updates, dict):
            return self._webui_error("配置数据格式无效")
        try:
            schema = self._load_webui_config_schema()
            async with self._config_lock:
                previous = copy.deepcopy(dict(self.config))
                updated = self._merge_webui_config(previous, updates, schema)
                save_config = getattr(self.config, "save_config", None)
                if not callable(save_config):
                    return self._webui_error("当前 AstrBot 配置对象不支持保存", status_code=500)
                self.config.clear()
                self.config.update(updated)
                try:
                    save_config()
                except Exception:
                    self.config.clear()
                    self.config.update(previous)
                    raise
        except ValueError as e:
            return self._webui_error(str(e))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as e:
            logger.error(f"{LOG} WebUI 保存配置失败：{e!r}")
            return self._webui_error("保存插件配置失败，请检查数据目录权限", status_code=500)
        return jsonify({"saved": True})

    # ------------------------------------------------------------------ 配置
    def _cfg(self, section: str, key: str, default=None):
        sec = self.config.get(section) if hasattr(self.config, "get") else None
        if isinstance(sec, dict):
            v = sec.get(key)
            if v is not None and v != "":
                return v
        return default

    def _set_state(self, **kw) -> None:
        self.state.update(kw)
        try:
            self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning(f"{LOG} 写 state.json 失败：{e}")

    # ------------------------------------------------------------------ 生命周期
    def _start_scheduler(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._scheduler is None or self._scheduler.done():
            self._scheduler = loop.create_task(self._scheduler_loop())

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        self._start_scheduler()

    async def terminate(self):
        if self._scheduler:
            self._scheduler.cancel()
            self._scheduler = None

    async def _scheduler_loop(self) -> None:
        """每 30 秒看一眼：到点了、昨天还没写、没放弃 → 写。失败隔 10 分钟再试，最多 3 次。"""
        while True:
            try:
                await asyncio.sleep(30)
                if not self._cfg("diary", "enabled", True):
                    continue
                target = diary_due(
                    datetime.now(),
                    str(self._cfg("diary", "write_time", "04:30")),
                    float(self._cfg("diary", "catch_up_hours", 6)),
                )
                if target is None:
                    continue
                key = target.isoformat()
                if self.state.get("done_target") == key:
                    continue
                attempts = int(self.state.get("attempts", 0)) if self.state.get("attempt_target") == key else 0
                if attempts >= 3:
                    continue
                retry_after = self.state.get("retry_after") if self.state.get("attempt_target") == key else None
                if retry_after and now_str() < retry_after:
                    continue
                ok, msg = await self.write_diary(target)
                if ok:
                    self._set_state(done_target=key, last_result=msg, last_attempt=now_str())
                else:
                    self._set_state(
                        attempt_target=key,
                        attempts=attempts + 1,
                        retry_after=(datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),
                        last_result=msg,
                        last_attempt=now_str(),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"{LOG} 定时任务异常：{e!r}")

    # ------------------------------------------------------------------ 人格
    async def _persona_prompt(self, umo: str | None = None) -> str:
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        pid = str(self._cfg("diary", "persona_id", "") or "").strip()
        try:
            if pid:
                p = await pm.get_persona(pid)
                if p:
                    return getattr(p, "system_prompt", None) or (p.get("prompt", "") if isinstance(p, dict) else "")
            dp = await pm.get_default_persona_v3(umo=umo)
            if dp:
                if isinstance(dp, dict):
                    return dp.get("prompt", "") or ""
                return getattr(dp, "prompt", None) or getattr(dp, "system_prompt", "") or ""
        except Exception as e:
            logger.warning(f"{LOG} 取人格失败：{e!r}")
        return ""

    async def _persona_id_of(self, umo: str) -> str:
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return ""
            conv = await cm.get_conversation(umo, cid)
            return getattr(conv, "persona_id", None) or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------ 记录
    def _base_record(self, event: AstrMessageEvent) -> dict:
        return {
            "ts": now_str(),
            "umo": str(_safe(lambda: event.unified_msg_origin)),
            "platform": str(_safe(event.get_platform_name)),
            "platform_id": str(_safe(event.get_platform_id)),
            "user_id": str(_safe(event.get_sender_id)),
            "user_name": str(_safe(event.get_sender_name)),
            "group_id": str(_safe(event.get_group_id)),
            "message_id": str(_safe(lambda: event.message_obj.message_id)),
        }

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        proactive = _is_proactive(event)

        # 1) 记对方这一句（主动消息没有对方的话，记一条"她主动开口"）
        if self._cfg("log", "enabled", True) and not event.get_extra("alycia_journal_logged"):
            event.set_extra("alycia_journal_logged", True)
            rec = self._base_record(event)
            if proactive:
                rec["kind"] = "proactive"
                rec["content"] = ""
                rec["motivation"] = str(getattr(req, "prompt", "") or "")
            else:
                rec["kind"] = "user"
                rec["content"] = str(getattr(event, "message_str", "") or "")
            if self._cfg("log", "log_full_prompt", False):
                # 注意：其他插件（记忆/世界书）可能在本钩子之后才注入，这里看到的未必是最终版
                rec["prompt_sent"] = str(getattr(req, "prompt", "") or "")
                rec["system_prompt_chars"] = len(getattr(req, "system_prompt", "") or "")
            try:
                self.records.append(rec)
            except Exception as e:
                logger.warning(f"{LOG} 写记录失败：{e!r}")

        # 2) 把最近一篇日记附在系统提示词末尾
        if self._cfg("inject", "enabled", True):
            block = self._diary_block()
            if block:
                req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n\n" + block

        # 3) 主动消息时看一眼 ManicTime
        if self._cfg("manictime", "enabled", True):
            want = self._cfg("manictime", "on_proactive", True) if proactive else self._cfg("manictime", "on_reply", False)
            if want:
                block = self._activity_block()
                if block:
                    req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n\n" + block

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        if not self._cfg("log", "enabled", True):
            return
        if getattr(resp, "is_chunk", False):
            return
        text = str(getattr(resp, "completion_text", "") or "")
        reasoning = _extract_reasoning(resp) if self._cfg("log", "log_reasoning", True) else ""
        tool_names = list(getattr(resp, "tools_call_name", None) or [])
        if not text and not reasoning and not tool_names:
            return
        rec = self._base_record(event)
        rec["kind"] = "assistant" if text else "assistant_tool_call"
        cleaned, tags = strip_favour_tags(text)
        rec["content"] = cleaned
        if tags:
            rec["favour_tags"] = tags
        if reasoning:
            rec["reasoning"] = reasoning
        if tool_names:
            rec["tool_calls"] = tool_names
        if _is_proactive(event):
            rec["proactive"] = True
        raw = getattr(resp, "raw_completion", None)
        model = getattr(raw, "model", None)
        if model:
            rec["model"] = str(model)
        usage = _jsonable(getattr(resp, "usage", None))
        if usage:
            rec["usage"] = usage
        rec["persona_id"] = await self._persona_id_of(rec["umo"])
        try:
            self.records.append(rec)
        except Exception as e:
            logger.warning(f"{LOG} 写记录失败：{e!r}")

    # ------------------------------------------------------------------ 注入块
    def _diary_block(self) -> str:
        today = date.today()
        item = self.diaries.latest(today, int(self._cfg("inject", "max_age_days", 3)))
        if not item:
            return ""
        d, text = item
        if not text:
            return ""
        max_chars = int(self._cfg("inject", "max_chars", 2500))
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "……"
        return render_template(
            str(self._cfg("inject", "template", DEFAULT_INJECT_TEMPLATE)),
            date=d.isoformat(),
            weekday=weekday_cn(d),
            relative=relative_day_cn(d, today),
            diary=text,
        )

    def _activity_block(self) -> str:
        data = load_json(self.manictime_file)
        if not data or not data.get("generated_at"):
            return ""
        age = age_minutes(data["generated_at"])
        max_age = float(self._cfg("manictime", "max_age_minutes", 15))
        stale = age is None or age > max_age
        if stale and not self._cfg("manictime", "inject_when_stale", False):
            return ""
        summary = render_activity_now(data)
        if stale:
            summary += f"\n（这份记录是 {int(age or 0)} 分钟前的，之后的情况不清楚）"
        return render_template(
            str(self._cfg("manictime", "template", DEFAULT_MANICTIME_TEMPLATE)),
            summary=summary,
            generated_at=data["generated_at"],
        )

    # ------------------------------------------------------------------ 写日记
    async def write_diary(self, target: date, force: bool = False) -> tuple[bool, str]:
        async with self._diary_lock:
            records = self.records.read_day(target)
            has_convo = any(r.get("kind") in ("user", "assistant", "proactive") for r in records)
            if not has_convo and not force and not self._cfg("diary", "write_when_empty", False):
                return False, "那天没有对话记录"

            provider = self.context.get_using_provider()
            if provider is None:
                return False, "没有可用的模型"

            persona_prompt = await self._persona_prompt()
            suffix = str(self._cfg("diary", "system_prompt_suffix", DEFAULT_DIARY_SYSTEM_SUFFIX))
            system_prompt = (persona_prompt + "\n\n" + suffix).strip()

            conversation = render_conversation(
                records,
                include_reasoning=bool(self._cfg("diary", "include_reasoning", False)),
                max_chars=int(self._cfg("diary", "max_conversation_chars", 24000)),
            )
            previous = self.diaries.latest_before(target, int(self._cfg("diary", "previous_count", 3)))
            activity = ""
            if self._cfg("diary", "include_activity", True):
                data = load_json(self.manictime_file)
                if data:
                    activity = render_activity_day(data, target.isoformat())
            prompt = build_diary_prompt(
                str(self._cfg("diary", "user_prompt", DEFAULT_DIARY_PROMPT)),
                target,
                date.today(),
                conversation,
                previous,
                activity,
            )

            try:
                resp = await provider.text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    session_id=f"alycia_journal_{target.isoformat()}",
                    persist=False,
                )
            except Exception as e:
                logger.error(f"{LOG} 写 {target} 日记时模型调用失败：{e!r}")
                return False, f"模型调用失败：{e}"

            text, _ = strip_favour_tags(str(getattr(resp, "completion_text", "") or ""))
            if not text:
                return False, "模型没有输出正文"
            reasoning = _extract_reasoning(resp) if self._cfg("diary", "save_reasoning", True) else ""
            self.diaries.write(target, text, reasoning or None)
            logger.info(f"{LOG} 已写 {target} 的日记，{len(text)} 字")
            return True, f"{len(text)} 字"

    # ------------------------------------------------------------------ 管理员指令
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("journal_diary")
    async def cmd_journal_diary(self, event: AstrMessageEvent, day: str = ""):
        """手动写某天的日记：/journal_diary [2026-11-01]，默认昨天。会覆盖已有的那篇。"""
        target = parse_date(day) if day else date.today() - timedelta(days=1)
        if target is None:
            yield event.plain_result("日期没看懂，用 2026-11-01 这种格式。")
            return
        yield event.plain_result(f"开始写 {target.isoformat()} 的日记……")
        ok, msg = await self.write_diary(target, force=True)
        yield event.plain_result(f"{target.isoformat()} 的日记：{'已写入' if ok else '没写成'}（{msg}）")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("journal_status")
    async def cmd_journal_status(self, event: AstrMessageEvent):
        """看插件状态：记录条数、日记篇数、定时与 ManicTime 数据新鲜度。不展示日记内容。"""
        today = date.today()
        diary_dates = self.diaries.dates()
        latest = diary_dates[-1].isoformat() if diary_dates else "无"
        data = load_json(self.manictime_file)
        if data and data.get("generated_at"):
            age = age_minutes(data["generated_at"])
            mt = f"{data['generated_at']}（{int(age)} 分钟前）" if age is not None else data["generated_at"]
        else:
            mt = "没有数据（宿主机导出脚本还没跑？）"
        lines = [
            f"记录：今天 {self.records.count_day(today)} 条，昨天 {self.records.count_day(today - timedelta(days=1))} 条",
            f"日记：共 {len(diary_dates)} 篇，最近一篇 {latest}",
            f"定时：每天 {self._cfg('diary', 'write_time', '04:30')} 写前一天；上次 {self.state.get('last_attempt', '无')} → {self.state.get('last_result', '无')}",
            f"ManicTime：{mt}",
            f"数据目录：{self.data_dir}",
        ]
        yield event.plain_result("\n".join(lines))
