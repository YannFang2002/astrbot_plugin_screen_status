"""角色小屏状态服务 · AstrBot 插件版

把原先独立跑的 18900 数据服务搬进 AstrBot 插件里：装好插件、配好端口，
副屏程序就能直接拉 `GET /api/state`，不需要再额外部署一个 Python 进程。

提供的接口（全部只读）：
  GET /api/state          一次性拿全部数据（JSON，几 KB）
  GET /api/cover?b=<bvid> 代理 B 站封面（带 Referer，可直接当图片用）
  GET /api/schema         字段说明
  GET /health             存活检查

设计要点：
  * 不依赖 docker CLI、不依赖任何外部目录写死路径，AstrBot 数据目录自动探测
  * 所有可选数据源（B站状态 / 主动搭话状态 / 记忆库）缺失时自动降级，不报错
  * 外部请求全部带 TTL 缓存，副屏每 5 秒拉一次也不会给 DB 压力
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import date as _date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger
from astrbot.api.star import Context, Star, register

try:  # AstrBot 4.x 的路径工具；老版本没有就自己推
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover
    def get_astrbot_data_path() -> str:
        return os.environ.get("ASTRBOT_DATA_PATH", "/AstrBot/data")


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DEFAULTS = {
    "enable": True,
    "bind": "0.0.0.0",
    "port": 18900,
    "token": "",                 # 留空 = 不校验（仅建议内网用）
    "persona_id": "",            # 留空 = 取 AstrBot 当前默认人格
    "display_name": "",          # 留空 = 从人格提示词「你叫 XXX」里提取
    "default_name": "Assistant",   # 兜底名字：既没配 display_name、提示词里也提取不到时用
    "db_path": "",               # 留空 = 自动在数据目录里找 data_v*.db
    "bili_state_path": "",       # 留空 = <data>/bili_state.json（存在才用）
    "bili_cookie_path": "",      # 留空 = <data>/secrets/bili_cookie.txt
    "proactive_state_path": "",  # 留空 = <data>/plugin_data/astrbot_plugin_proactive_chat/state.json
    "owner_umo": "",             # 主人会话 umo，用于查下次主动搭话时间
    "memory_db_path": "",        # 留空 = plugin_data/astrbot_plugin_mnemosyne/chroma_data/chroma.sqlite3
    "bili_daily_limit_min": 120,
    "sleep_start": 0.5,          # 00:30
    "sleep_end": 7.5,            # 07:30
    "wake_on_chat_sec": 1200,
    "chatting_window_sec": 180,
    "recent_lines_count": 5,
    "enable_cover_proxy": True,
    "cover_width": 320,          # 封面缩略宽度（0 = 原图）
    "model_name": "",            # 副屏显示用的模型名（留空 = 尝试从库里读）
    "qq_state_override": "",     # online/offline/留空=自动判断
    "enable_docker_status": False,
    "cache_ttl_sec": 2,
    "born_at": "",               # 留空 = 用库里最早的对话时间
}


class _Cache:
    def __init__(self):
        self._d = {}
        self._lock = threading.Lock()

    def get(self, key, ttl, fn):
        now = time.time()
        with self._lock:
            v = self._d.get(key)
            if v and now - v[0] < ttl:
                return v[1]
        try:
            val = fn()
        except Exception:
            return None
        with self._lock:
            self._d[key] = (now, val)
        return val


@register("astrbot_plugin_screen_status", "YannFang2002",
          "把角色状态（聊天中/忙碌/空闲/睡觉）暴露成只读 HTTP 接口，供小屏、副屏、硬件拉取", "1.0.0")
class CharacterScreenPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = {**DEFAULTS, **(config or {})}
        self.data_dir = get_astrbot_data_path()
        self.cache = _Cache()
        self._httpd = None
        self._thread = None
        self._db_path = self._resolve_db()

    # ---------------- 路径探测 ----------------
    def _resolve_db(self) -> str:
        if self.cfg.get("db_path"):
            return self.cfg["db_path"]
        try:
            cands = [os.path.join(self.data_dir, f) for f in os.listdir(self.data_dir)
                     if re.fullmatch(r"data_v\d+\.db", f)]
        except Exception:
            cands = []
        if not cands:
            return os.path.join(self.data_dir, "data_v4.db")
        return max(cands, key=os.path.getmtime)

    def _p(self, key: str, rel: str) -> str:
        return self.cfg.get(key) or os.path.join(self.data_dir, rel)

    def _db(self):
        return sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True, timeout=5)

    # ---------------- 数据：人格与对话 ----------------
    def _persona_name(self) -> str:
        if self.cfg.get("display_name"):
            return self.cfg["display_name"]

        def probe():
            pid = self.cfg.get("persona_id") or ""
            try:
                c = self._db()
                if pid:
                    row = c.execute("select system_prompt from personas where persona_id = ?",
                                    (pid,)).fetchone()
                else:
                    row = c.execute("select system_prompt from personas "
                                    "order by sort_order limit 1").fetchone()
                c.close()
                if row and row[0]:
                    m = re.search(r"你叫\s*([^\s，,。.！!？?、]{1,12})", row[0])
                    if m:
                        return m.group(1).strip()
                    m = re.search(r"(?:你的名字是|名字叫)\s*([^\s，,。.！!？?、]{1,12})", row[0])
                    if m:
                        return m.group(1).strip()
            except Exception:
                pass
            return self.cfg.get("default_name") or "Assistant"

        return self.cache.get("name", 300, probe) or "Assistant"

    @staticmethod
    def _assistant_text(raw) -> str:
        try:
            msgs = json.loads(raw)
        except Exception:
            return ""
        for m in reversed(msgs or []):
            if m.get("role") != "assistant":
                continue
            cts = m.get("content")
            if isinstance(cts, list):
                txt = " ".join(x.get("text", "") for x in cts
                               if isinstance(x, dict) and x.get("type") == "text")
            else:
                txt = str(cts)
            txt = re.sub(r"<[^>]+>", "", txt)
            return re.sub(r"\s+", " ", txt).strip()
        return ""

    def _stats(self) -> dict:
        d = {"today_msgs": 0, "last_active": None, "memory_count": 0,
             "model": "", "last_reply": "", "days_together": 1,
             "born_at": "", "recent_lines": []}
        try:
            c = self._db()
            today = time.strftime("%Y-%m-%d")
            try:
                n = c.execute("select coalesce(sum(count),0) from platform_stats where timestamp >= ?",
                              (today + " 00:00:00",)).fetchone()[0]
                last = c.execute("select max(timestamp) from platform_stats").fetchone()[0]
                d["today_msgs"], d["last_active"] = int(n or 0), last
            except Exception:
                pass

            limit = max(1, int(self.cfg.get("recent_lines_count") or 5))
            rows = c.execute("select content from conversations "
                             "order by updated_at desc limit 8").fetchall()
            seen, lines = set(), []
            for (raw,) in rows:
                txt = self._assistant_text(raw) if raw else ""
                if not txt or txt in seen:
                    continue
                seen.add(txt)
                lines.append(txt[:140])
                if len(lines) >= limit:
                    break
            d["recent_lines"] = lines
            if lines:
                d["last_reply"] = lines[0]

            born = c.execute("select min(created_at) from conversations").fetchone()[0]
            born_date = None
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(born or ""))
            if m:
                born_date = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif self.cfg.get("born_at"):
                y, mo, dd = [int(x) for x in str(self.cfg["born_at"]).split("-")[:3]]
                born_date = _date(y, mo, dd)
            if born_date is None:
                born_date = _date.today()
            d["born_at"] = born_date.isoformat()
            d["days_together"] = max(1, (_date.today() - born_date).days + 1)
            d["model"] = str(self.cfg.get("model_name") or "")
            if not d["model"]:
                try:
                    row = c.execute("select value from preferences where key = 'model'").fetchone()
                    d["model"] = json.loads(row[0]).get("val", "") if row else ""
                except Exception:
                    pass
            c.close()
        except Exception as e:
            d["db_error"] = str(e)[:80]

        try:
            mc = self._p("memory_db_path", "plugin_data/astrbot_plugin_mnemosyne/"
                                           "chroma_data/chroma.sqlite3")
            c = sqlite3.connect(f"file:{mc}?mode=ro", uri=True, timeout=5)
            d["memory_count"] = c.execute("select count(*) from embeddings").fetchone()[0]
            c.close()
        except Exception:
            pass
        return d

    # ---------------- 数据：B 站 ----------------
    def _cookie(self) -> str:
        try:
            path = self._p("bili_cookie_path", "secrets/bili_cookie.txt")
            return open(path, encoding="utf-8").read().strip()
        except Exception:
            return ""

    @staticmethod
    def _json(url, headers=None, timeout=12):
        req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
        return json.load(urllib.request.urlopen(req, timeout=timeout))

    def _popular(self, limit=8):
        def f():
            try:
                j = self._json(f"https://api.bilibili.com/x/web-interface/popular?ps={limit}&pn=1",
                               {"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
                out = []
                for v in (j.get("data") or {}).get("list") or []:
                    out.append({
                        "bvid": v.get("bvid"),
                        "title": (v.get("title") or "")[:60],
                        "up": (v.get("owner") or {}).get("name", ""),
                        "play": (v.get("stat") or {}).get("view", 0),
                        "danmaku": (v.get("stat") or {}).get("danmaku", 0),
                        "duration": v.get("duration", 0),
                        "cover": f"/api/cover?b={v.get('bvid')}",
                        "url": f"https://www.bilibili.com/video/{v.get('bvid')}",
                    })
                return {"items": out}
            except Exception as e:
                return {"items": [], "error": str(e)[:80]}

        return self.cache.get("popular", 300, f) or {"items": []}

    def _cover(self, bvid):
        def f():
            try:
                j = self._json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                               {"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
                pic = (j.get("data") or {}).get("pic") or ""
                if pic.startswith("//"):
                    pic = "https:" + pic
                if not pic:
                    return None
                w = int(self.cfg.get("cover_width") or 0)
                if w and "@" not in pic:
                    h = int(w * 9 / 16)
                    pic = f"{pic}@{w}w_{h}h_1c.jpg"
                req = urllib.request.Request(pic, headers={"User-Agent": UA,
                                                           "Referer": "https://www.bilibili.com/"})
                return urllib.request.urlopen(req, timeout=15).read()
            except Exception:
                return None

        return self.cache.get("cover:" + str(bvid), 1800, f)

    # ---------------- 数据：活动状态 ----------------
    @staticmethod
    def _read_json(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _last_chat_epoch(self) -> float:
        try:
            c = self._db()
            row = c.execute("select updated_at from conversations "
                            "order by updated_at desc limit 1").fetchone()
            c.close()
            if row and row[0]:
                return time.mktime(time.strptime(str(row[0])[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
        return 0.0

    def _docker_states(self) -> dict:
        if not self.cfg.get("enable_docker_status"):
            return {}
        try:
            import subprocess
            out = subprocess.run(["docker", "ps", "--format", "{{.Names}}|{{.Status}}"],
                                 capture_output=True, text=True, timeout=8).stdout
            return {l.split("|")[0]: l.split("|")[1]
                    for l in out.strip().splitlines() if "|" in l}
        except Exception:
            return {}

    def _activity(self) -> dict:
        now = time.time()
        bi = self._read_json(self._p("bili_state_path", "bili_state.json"))
        pro = self._read_json(self._p("proactive_state_path",
                                      "plugin_data/astrbot_plugin_proactive_chat/state.json"))
        last_msg = self._last_chat_epoch()

        sessions = bi.get("sessions") or []
        in_bili, bili_since = False, None
        if sessions:
            sv = sessions[-1]
            st, mins = float(sv.get("start") or 0), float(sv.get("min") or 0)
            if st and now < st + mins * 60 + 180:
                in_bili, bili_since = True, st
        last_end = float(bi.get("last_end") or 0)
        gap_min = float(bi.get("next_gap_min") or 0)
        next_bili = (last_end + gap_min * 60) if last_end else None
        hist = bi.get("history") or []
        last_video = hist[-1] if hist else None

        umo = self.cfg.get("owner_umo") or ""
        next_pro = float(((pro.get("next_run") or {}).get(umo)) or 0) if umo else 0.0
        sent = ((pro.get("sent") or {}).get(umo) or []) if umo else []
        last_pro_text = sent[-1] if sent else ""

        hh = time.localtime(now).tm_hour + time.localtime(now).tm_min / 60
        s0, s1 = float(self.cfg["sleep_start"]), float(self.cfg["sleep_end"])
        in_window = (s0 <= hh < s1) if s0 <= s1 else (hh >= s0 or hh < s1)
        woke = bool(last_msg and now - last_msg < float(self.cfg["wake_on_chat_sec"]))
        sleeping = in_window and not woke

        state, label = "idle", "空闲中"
        if last_msg and now - last_msg < float(self.cfg["chatting_window_sec"]):
            state, label = "chatting", "聊天中"
        elif in_bili:
            state, label = "browsing_bili", "刷B站"

        marks = [t for t in (last_msg, last_end, bili_since or 0) if t]
        last_active = max(marks) if marks else 0
        idle_sec = int(now - last_active) if last_active else -1
        if state == "chatting":
            since_min = int((now - last_msg) / 60)
        elif state == "browsing_bili":
            since_min = int((now - bili_since) / 60) if bili_since else 0
        else:
            since_min = int(idle_sec / 60) if idle_sec >= 0 else 0

        if state == "idle":
            detail = f"上次互动 {since_min} 分钟前" if since_min else "刚闲下来"
            if sleeping:
                detail = "夜间休息中 · " + detail
        elif state == "browsing_bili":
            detail = f"已刷 {since_min} 分钟"
        else:
            detail = f"聊了 {since_min} 分钟"

        return {
            "state": state, "label": label, "detail": detail,
            "since_min": since_min, "idle_seconds": idle_sec,
            "busy": state in ("chatting", "browsing_bili"), "sleeping": sleeping,
            "bili_today_min": round(float(bi.get("browse_min") or 0), 1),
            "bili_today_limit": int(self.cfg["bili_daily_limit_min"]),
            "bili_sessions": len(sessions),
            "next_bili_in_min": (max(0, int((next_bili - now) / 60)) if next_bili else None),
            "last_video": ({"bvid": last_video.get("bvid"),
                            "title": (last_video.get("标题") or last_video.get("title") or "")[:50]}
                           if last_video else None),
            "next_proactive_in_min": (max(0, int((next_pro - now) / 60)) if next_pro else None),
            "last_proactive_text": last_pro_text[:60],
        }

    def _qq_state(self) -> str:
        if self.cfg.get("qq_state_override"):
            return str(self.cfg["qq_state_override"])
        try:
            pm = getattr(self.context, "platform_manager", None)
            insts = (getattr(pm, "platform_insts", None)
                     or (pm.get_insts() if pm and hasattr(pm, "get_insts") else []))
            for inst in insts:
                bot = getattr(inst, "bot", None)      # aiocqhttp 的 CQHttp 实例
                if bot is None:
                    continue
                # aiocqhttp 的 CQHttp 用这两个容器记录反向 WS 客户端
                for attr in ("_wsr_api_clients", "_wsr_event_clients", "_wsr_conns"):
                    conns = getattr(bot, attr, None)
                    if conns is None:
                        continue
                    try:
                        return "online" if len(conns) > 0 else "offline"
                    except Exception:
                        continue
                return "unknown"
        except Exception:
            pass
        return "unknown"

    def _character(self) -> dict:
        st = self._docker_states()
        a = self._stats()
        return {
            "name": self._persona_name(),
            "astrbot": "up",
            "qq": self._qq_state(),
            "model": a.get("model") or "",
            "today_msgs": a["today_msgs"],
            "memory_count": a["memory_count"],
            "last_active": a["last_active"],
            "last_reply": a["last_reply"],
            "days_together": a["days_together"],
            "born_at": a["born_at"],
            "recent_lines": a.get("recent_lines", []),
            "uptime": {"astrbot": st.get("astrbot", "in-process"),
                       "snowluma": st.get("snowluma", ""),
                       "ollama": st.get("ollama", "")},
        }

    def snapshot(self) -> dict:
        ttl = float(self.cfg.get("cache_ttl_sec") or 2)
        return self.cache.get("state", ttl, lambda: {
            "ts": int(time.time()),
            "activity": self._activity(),
            "character": self._character(),
            "xiaowai": self._character(),   # 兼容旧字段名（早期版本用过），下一版移除
        }) or {}

    # ---------------- HTTP ----------------
    @staticmethod
    def _make_handler(plugin: "CharacterScreenPlugin"):
        token = (plugin.cfg.get("token") or "").strip()

        class Handler(BaseHTTPRequestHandler):
            server_version = "CharacterScreen/1.0"

            def log_message(self, fmt, *args):  # 静音，别刷屏 AstrBot 日志
                pass

            def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

            def _allowed(self) -> bool:
                if not token:
                    return True
                auth = self.headers.get("Authorization", "")
                return auth == f"Bearer {token}" or self.headers.get("X-Token") == token

            def do_GET(self):
                u = urlparse(self.path)
                path = u.path.rstrip("/") or "/"
                if not self._allowed():
                    return self._json({"error": "unauthorized"}, 401)
                if path == "/health":
                    return self._json({"ok": True, "service": "char-screen",
                                       "port": plugin.cfg.get("port")})
                if path == "/api/state":
                    try:
                        return self._json(plugin.snapshot())
                    except Exception as e:
                        return self._json({"error": str(e)[:120]}, 500)
                if path == "/api/cover":
                    if not plugin.cfg.get("enable_cover_proxy"):
                        return self._json({"error": "cover proxy disabled"}, 403)
                    bvid = (parse_qs(u.query).get("b") or [""])[0]
                    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid or ""):
                        return self._json({"error": "bad bvid"}, 400)
                    data = plugin._cover(bvid)
                    if not data:
                        return self._json({"error": "fetch failed"}, 502)
                    return self._send(200, data, "image/jpeg")
                if path == "/api/schema":
                    return self._json(SCHEMA)
                return self._json({"error": "not found",
                                   "endpoints": ["/api/state", "/api/cover?b=BVID",
                                                 "/api/schema", "/health"]}, 404)

            def do_HEAD(self):
                self._send(200, b"")

        return Handler

    async def initialize(self):
        if not self.cfg.get("enable"):
            logger.info("[char_screen] 配置里 enable=false，跳过启动")
            return
        host = str(self.cfg.get("bind") or "0.0.0.0")
        port = int(self.cfg.get("port") or 18900)
        try:
            self._httpd = ThreadingHTTPServer((host, port), self._make_handler(self))
            self._httpd.daemon_threads = True
            self._thread = threading.Thread(target=self._httpd.serve_forever,
                                            name="char-screen-http", daemon=True)
            self._thread.start()
            logger.info(f"[char_screen] 状态服务已启动 http://{host}:{port}"
                        f"（db={os.path.basename(self._db_path)}）")
        except Exception as e:
            self._httpd = None
            logger.error(f"[char_screen] 端口 {port} 启动失败：{e}（可能被占用）")

    async def terminate(self):
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
            logger.info("[char_screen] 状态服务已停止")


SCHEMA = {
    "version": 1,
    "endpoints": {
        "/api/state": "全部状态（JSON）",
        "/api/cover?b=BVID": "B站封面 JPEG",
        "/api/schema": "本说明",
        "/health": "存活检查",
    },
    "state": {
        "ts": "服务器时间戳（秒）",
        "activity": {
            "state": "chatting | browsing_bili | idle",
            "label": "中文标签",
            "detail": "一行补充说明",
            "since_min": "当前状态持续分钟数",
            "idle_seconds": "距上次活动秒数（-1 表示未知）",
            "busy": "是否忙碌",
            "sleeping": "是否在睡觉（睡眠窗口内且近期无互动；只是标记，不占状态位）",
            "bili_today_min": "今日已刷B站分钟",
            "bili_today_limit": "每日上限分钟",
            "bili_sessions": "今日刷B站轮数",
            "next_bili_in_min": "距下次刷B站分钟（null 省略）",
            "last_video": "{bvid, title} 或 null",
            "next_proactive_in_min": "距下次主动搭话分钟（null 省略）",
            "last_proactive_text": "上次主动搭话内容",
        },
        "character": {
            "name": "角色名字（人格同步；旧字段名 xiaowai 仍保留一段兼容期）",
            "astrbot": "up/down",
            "qq": "online/offline/unknown",
            "model": "当前模型",
            "today_msgs": "今日消息条数",
            "memory_count": "记忆库条数",
            "last_active": "最后活动时间",
            "last_reply": "最近一句回复",
            "days_together": "认识第几天",
            "born_at": "起始日期",
            "recent_lines": "最近几句她说过的原话",
        },
    },
}
