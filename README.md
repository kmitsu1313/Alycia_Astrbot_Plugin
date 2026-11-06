# astrbot_plugin_alycia_journal — Alycia 日记与记录

自写 AstrBot 插件，替代 auto_diary。三件事，提示词全在 WebUI 配置里（人格线写），插件只管管道：

1. **逐条记录**：所有渠道、所有用户的每一轮。`log/YYYY-MM-DD.jsonl`，一行一条：时间戳、渠道、用户、对方说的话 / 她的思考（模型 reasoning）/ 她的回复（好感度标签剥掉另存）。主动消息也记（`kind=proactive` + 她的回复）。
2. **日记**：每天 `diary.write_time`（默认 04:30）回顾**前一天**，用「人格提示词 + 追加说明」做系统提示词、`diary.user_prompt` 做指令，附上那天的对话稿、最近几篇旧日记、那天的电脑活动摘要，写一篇存到 `diary/YYYY-MM-DD.md`（写作时的思考另存 `.reasoning.txt`）。之后每轮对话把最近一篇附在系统提示词末尾（不进历史），她可以提起，但没有「读日记」指令给对方用。
3. **看一眼 ManicTime**：宿主机脚本每 5 分钟把 `ManicTimeReports.db` 聚合成 `manictime/latest.json`；她主动开口时（proactive_chat 的请求）插件把摘要附在系统提示词末尾。全部可见，不设黑名单。

## 目录

```
astrbot_plugin_alycia_journal/   ← 源码唯一真本（E:\agent project\）
  main.py            AstrBot 胶水：钩子、定时、指令
  core.py            纯逻辑，不依赖 astrbot，宿主机可直接测
  _conf_schema.json  配置项（WebUI 里改）
  metadata.yaml
  views/journal/     日记查看、编辑与配置管理页面
  .astrbot-plugin/
    i18n/zh-CN.json  View 的中文标题和说明
  host_tools/
    manictime_export.py   宿主机运行，导出 latest.json
    install_task.ps1      注册每 5 分钟一次的计划任务
```

运行副本：`C:\Users\uuuiii13\data\plugins\astrbot_plugin_alycia_journal\`（改完源码重新复制一次，再在 WebUI 重载插件）。

数据：`C:\Users\uuuiii13\data\plugin_data\astrbot_plugin_alycia_journal\`（容器内 `/AstrBot/data/plugin_data/...`）
```
  log/YYYY-MM-DD.jsonl        逐条记录
  diary/YYYY-MM-DD.md         日记；YYYY-MM-DD.reasoning.txt 写作时的思考
  manictime/latest.json       宿主机脚本写入
  state.json                  定时状态
```

## 安装

1. 复制插件目录到 `data/plugins`，WebUI → 插件 → 重载（或重启容器）。
2. 宿主机 PowerShell：`cd "E:\agent project\astrbot_plugin_alycia_journal\host_tools"; .\install_task.ps1`。先手动跑一次看输出：`python manictime_export.py --print`。
3. 让用户的账号成为 AstrBot 管理员（WebUI 配置 → 管理员 ID），否则下面的指令不响应。
4. 人格线在 WebUI 插件配置里填 `diary.system_prompt_suffix`、`diary.user_prompt`、`inject.template`、`manictime.template`。默认值只是占位。
5. 跑稳后卸载 auto_diary。

## 指令（仅管理员）

- `/journal_diary [2026-11-01]` 手动写某天的日记（默认昨天，覆盖已有）。回的是「已写入（N 字）」，不回正文。
- `/journal_status` 记录条数、日记篇数、定时状态、ManicTime 数据新鲜度。

## AstrBot 可视化页面

插件详情页的 **日记管理** View 提供：

- 按日期查看、编辑和保存日记；也可以手动调用模型重新生成指定日期的日记。
- 查看记录数、日记数量、最近一次定时写作结果和 ManicTime 数据新鲜度。
- 在 **插件配置** 页直接编辑全部 `_conf_schema.json` 配置，保存后立即应用并写入 AstrBot 插件配置文件。

将源码复制到 `data/plugins` 并重载插件后，在 AstrBot WebUI 打开该插件详情页即可进入。插件 View 需要 AstrBot 支持插件页面和 `context.register_web_api()`；旧版本若没有该能力，插件会保留原有消息处理和指令功能，并在日志中提示管理页面不可用。配置也仍可通过 AstrBot 原生插件配置面板编辑。

## 记录格式（jsonl 一行）

```json
{"ts":"2026-11-02 21:49:10","umo":"Alycia_core_Personal_Wechat:FriendMessage:846910484","platform":"aiocqhttp",
 "platform_id":"Alycia_core_Personal_Wechat","user_id":"846910484","user_name":"uuuiii13","group_id":"",
 "message_id":"...","kind":"user","content":"忙去了……"}
{"ts":"2026-11-02 21:49:31", ..., "kind":"assistant","content":"……","favour_tags":["[好感度 持平]"],
 "reasoning":"……","model":"qwen3.8:27b","usage":{...},"persona_id":"Alycia_Test"}
```
`kind`：`user` / `assistant` / `proactive`（她主动开口，`motivation` 是 proactive_chat 的动机提示）/ `assistant_tool_call`（只调了工具没说话）。

## 已知边界

- **思考内容取法**：AstrBot `openai_source.py` 把读取键写死成 `reasoning_content`，ollama `/v1` 放在 `reasoning`，所以 `resp.reasoning_content` 恒空；插件从 `resp.raw_completion.choices[0].message.reasoning` 取。**非流式才完整**（当前 `streaming_response=False`）。改流式后需重验。
- `log_full_prompt` 看到的是本插件钩子运行时的 prompt，其他插件（livingmemory、lorebook）之后注入的看不到。
- 工具循环里一个事件可能触发多次 `on_llm_response`，会有 `assistant_tool_call` 行；用户侧只记一次。
- 日记调用 `provider.text_chat` 不经过 AstrBot 管线，所以不会触发本插件或其他插件的钩子（不会递归、不会被 livingmemory 记进记忆）。
- 定时错过（AstrBot 当时没开）在 `catch_up_hours` 内补；失败隔 10 分钟重试，最多 3 次；再不行用 `/journal_diary`。
- ManicTime 数据库只读打开；锁住时拷一份再读。中文正常（之前看到的乱码是控制台编码）。
