# 角色小屏状态服务 · astrbot_plugin_char_screen

把你的角色「现在在干什么」做成一个只读 HTTP 接口，给副屏 / USB 小屏 / ESP32 / 桌面宠物之类的程序定时拉取。

> 名字默认是 `Assistant`。你想显示成自己角色的名字，有两种方式：在配置里直接填 `display_name`，或者让角色人格提示词的第一句写成「你叫 XXX」——插件会自动提取。

**它不产出聊天内容，只是把状态暴露出去。** 状态来自 AstrBot 自己的数据库：最近一次对话时间、人格提示词里的名字、今天聊了多少句、认识第几天、最近几句原话。

---

## 装

AstrBot 4.14+ 。两种装法：

**插件市场**：在 AstrBot WebUI → 插件 → 从仓库安装，填 `Yann-Fang/astrbot_plugin_char_screen`。

**手动**：把本目录整个丢到 `<AstrBot数据目录>/plugins/astrbot_plugin_char_screen/`，重启 AstrBot。

启动成功会在日志里看到：

```
[char_screen] 状态服务已启动 http://0.0.0.0:18900（db=data_v4.db）
```

> **容器部署注意**：插件监听的是容器内部端口。要让宿主机/其他设备访问，容器启动时要带 `-p 18900:18900`，或者用 `--network host`。端口被占用时插件会记一条 error 并继续运行，不影响 AstrBot 本体。

---

## 接口

全部 GET、全部只读。

| 路径 | 说明 |
|---|---|
| `/api/state` | 全部状态，JSON，几 KB |
| `/api/cover?b=BV1xx…` | B 站封面图，已带 Referer、默认缩到 320 宽，可直接当图片用 |
| `/api/schema` | 字段说明 |
| `/health` | `{"ok": true}` |

配了 `token` 的话，请求头要带 `Authorization: Bearer <token>` 或 `X-Token: <token>`。

### /api/state 返回示例

```json
{
  "ts": 1761870000,
  "activity": {
    "state": "idle",
    "label": "空闲中",
    "detail": "上次互动 42 分钟前",
    "since_min": 42,
    "idle_seconds": 2520,
    "busy": false,
    "sleeping": false,
    "bili_today_min": 35.5,
    "bili_today_limit": 120,
    "bili_sessions": 2,
    "next_bili_in_min": 36,
    "last_video": {"bvid": "BV1xx411c7mD", "title": "标题"},
    "next_proactive_in_min": 12,
    "last_proactive_text": "在干嘛呢"
  },
  "character": {
    "name": "Assistant",
    "astrbot": "up",
    "qq": "online",
    "model": "deepseek-flash-latest",
    "today_msgs": 128,
    "memory_count": 340,
    "last_active": "2026-10-31 09:00:00",
    "last_reply": "哼，又来啦",
    "days_together": 4,
    "born_at": "2026-10-28",
    "recent_lines": ["哼，又来啦", "那也没辙，总不能去敲门吧"],
    "uptime": {"astrbot": "in-process", "snowluma": "", "ollama": ""}
  }
}
```

**`state` 只有三种**：`chatting`（最近 3 分钟内有对话）、`browsing_bili`（在刷东西，需外部状态文件）、`idle`（其余）。

`sleeping` 只是个标记，不占状态位：落在 `sleep_start`~`sleep_end` 窗口内、且最近 `wake_on_chat_sec` 秒内没互动才算真的在睡。

---

## 配置

在 WebUI 插件配置里改，都有中文说明。关键几项：

| 项 | 默认 | 说明 |
|---|---|---|
| `port` | 18900 | 监听端口 |
| `bind` | 0.0.0.0 | 只本机调试可改 127.0.0.1 |
| `token` | 空 | 留空不校验；公网必须填 |
| `persona_id` | 空 | 留空取排序最靠前的人格 |
| `display_name` | 空 | 直接指定显示名；留空则从人格提示词「你叫 XXX」提取 |
| `default_name` | Assistant | 上面两条都取不到时的兜底名字 |
| `sleep_start` / `sleep_end` | 0.5 / 7.5 | 睡觉窗口，支持跨零点（如 23.5 → 7.5） |
| `owner_umo` | 空 | 形如 `平台id:FriendMessage:QQ号`，填了才显示主动搭话倒计时 |
| `bili_state_path` | 空 | 外部活动状态 JSON；没有就只显示聊天中/空闲 |
| `enable_docker_status` | false | 只有容器内有 docker CLI 才开 |

### 可选数据源（没有也能跑）

- **活动状态文件**（`bili_state_path`）：若存在且含 `sessions[{start,min}]`、`browse_min`、`history[{bvid,title}]`、`last_end`、`next_gap_min`，就能显示「正在刷 / 今日刷了多久 / 下次还有几分钟」。这是给「角色自己有后台活动」的插件用的，不带也行。
- **主动消息状态**（`proactive_state_path` + `owner_umo`）：显示下次主动搭话倒计时。
- **记忆库**（`memory_db_path`）：统计记忆条数。

---

## 副屏怎么接

任何能发 HTTP 的东西都行。最小例子：

```bash
curl -s http://nas:18900/api/state
```

```python
import requests
s = requests.get("http://nas:18900/api/state", timeout=3).json()
print(s["activity"]["label"], s["character"]["name"])
```

建议：**每 5 秒拉一次**，请求超时设 3 秒，失败就退避 15 秒再试。服务端有 `cache_ttl_sec`（默认 2 秒）缓存，多块屏同时拉也不会打爆数据库。

封面单独拉，别塞进状态里：

```python
img = requests.get(f"http://nas:18900/api/cover?b={bvid}", timeout=5).content
```

---

## 排错

| 现象 | 原因 |
|---|---|
| 端口没监听 | 端口被占用 / `enable=false`；看日志里那条 error |
| `name` 是默认值 | 人格提示词里没有「你叫 XXX」，用 `display_name` 直接指定 |
| 永远是 `idle` | 没配 `bili_state_path`，且最近没聊天（超过 `chatting_window_sec`） |
| `memory_count: 0` | 没装记忆库插件，或 `memory_db_path` 不对 |
| `qq: unknown` | 平台自检拿不到连接状态，用 `qq_state_override` 手动指定 |

---

## 许可

MIT
