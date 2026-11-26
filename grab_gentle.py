#!/usr/bin/env python3
"""揭阳校区场馆预约（d2.xksyun.com 预约平台）

与同目录的激进版 grab.py 共用同一份 config.json（账号、登录态、表单、偏好完全兼容，无需改配置）。
核心目标只有一个：保证最终预约成功的后台时间戳落在真人区间（避开 10:00:00 / 10:00:01），
同时在放行提交后保持与旧版 grab.py 同等的全速火力，最大限度保住成功率。

工作方式：
  1. 高精度校时 + 实时刷列表：沿用 grab.py 的 3 秒高精度校时（误差约 ±0.03 秒）；
     放场瞬间由后台刷新线程高频拉取实时列表，开枪时直接跳过已被抢走的场。
  2. 成交时间窗口控制 + 多号错峰：到点后暂不提交，等达到随机设定的拟人开枪时刻才放行；
     多账号之间自动错开，让多个账号的成功时间戳自然分散在不同秒。
  3. 库存安全阀（保底防翻车）：等待期间实时监控目标时段余量；如果外部抢得太猛导致目标时段
     被抢走或余量告急，立即提前结束等待开抢，同时守住安全底线（绝不撞进 10:00:00 / 01s）。
  4. 放行即全速：一旦到达开枪时刻或触发安全阀，已提前预热好的全部连接（默认每号 3 连接、
     0.03 秒重发）立刻全速提交，对抗 3~6 秒的真人与脚本高峰。

内置三档预设（--profile，默认 balanced）：
  balanced  均衡模式（默认）：首号 +2.65~3.55s 开枪 -> 成交约 10:00:03~05，安全阀底线 +1.95s
  tight     热门日保底模式：首号 +1.95~2.65s 开枪 -> 成交约 10:00:02~04，安全阀底线 +1.65s（适合周二/四仅 12 场）
  relaxed   更从容模式：首号 +3.50~4.60s 开枪 -> 成交约 10:00:04~07，安全阀底线 +2.20s（适合周三/六/日 24 场）

也可直接用 --delay 临时指定首号开枪延迟（如 --delay 2.8-3.8 或 --delay 3.0），
或在 config.json 里加可选的 "gentle": { ... } 字段固定参数（不影响旧版 grab.py）。

用法：
  python3 grab_gentle.py --check                   只读检查：登录态、表单、时段及时间窗口配置
  python3 grab_gentle.py                           按默认 balanced 档位等待 open_time 开抢
  python3 grab_gentle.py --profile tight           周二/四 12 场保底档位
  python3 grab_gentle.py --profile relaxed         24 场日更从容档位
  python3 grab_gentle.py --delay 2.5-3.5           自定义首号开枪延迟区间（秒）
  python3 grab_gentle.py --now                     忽略 open_time 和拟人等待，立即开抢
  python3 grab_gentle.py --account 名字            只用指定账号
  python3 grab_gentle.py --set-session ID [--name 名字]  新增/更新登录 ID
"""
import argparse
import datetime as dt
import email.utils
import http.client
import json
import math
import random
import re
import sys
import threading
import time
import urllib.parse
from itertools import count
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
APP = "weixin_reserve"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2542035) XWEB/26134"
)
REFERER = "https://servicewechat.com/wxd120e74973a3e848/50/page-frame.html"
CFG_PATH = Path(__file__).with_name("config.json")
KNOWN_SLOTS_PATH = Path(__file__).with_name("reserve_configs.json")
BUSY_CODE = "1404"
WEEKDAY_NAMES = "一二三四五六日"

# 说明：实测后台落库时间约为 发出 POST 后 +0.25~0.50 秒
PROFILES = {
    "balanced": {
        "label": "均衡模式（目标成交 10:00:03~05）",
        "delay_min": 2.65,
        "delay_max": 3.55,
        "account_stagger_min": 0.70,
        "account_stagger_max": 1.35,
        "max_stagger_span": 2.00,
        "valve_floor_seconds": 1.95,
        "valve_min_slots": 2,
        "valve_group_lost": 2,
        "valve_global_lost": 4,
        "wait_refresh_seconds": 0.25,
        "fast_refresh_window": 10.0,
        "refresh_seconds": 1.5,
        "scene_val": 1089,
    },
    "tight": {
        "label": "热门日保底模式（目标成交 10:00:02~04，适合周二/四）",
        "delay_min": 1.95,
        "delay_max": 2.65,
        "account_stagger_min": 0.45,
        "account_stagger_max": 0.85,
        "max_stagger_span": 1.30,
        "valve_floor_seconds": 1.65,
        "valve_min_slots": 3,
        "valve_group_lost": 1,
        "valve_global_lost": 3,
        "wait_refresh_seconds": 0.20,
        "fast_refresh_window": 10.0,
        "refresh_seconds": 1.5,
        "scene_val": 1089,
    },
    "relaxed": {
        "label": "更从容模式（目标成交 10:00:04~07，适合 24 场日）",
        "delay_min": 3.50,
        "delay_max": 4.60,
        "account_stagger_min": 0.90,
        "account_stagger_max": 1.60,
        "max_stagger_span": 2.60,
        "valve_floor_seconds": 2.20,
        "valve_min_slots": 2,
        "valve_group_lost": 2,
        "valve_global_lost": 5,
        "wait_refresh_seconds": 0.25,
        "fast_refresh_window": 12.0,
        "refresh_seconds": 1.5,
        "scene_val": 1089,
    },
}

_ctx = threading.local()
_cfg_lock = threading.Lock()
_log_lock = threading.Lock()


def log(msg: str) -> None:
    now = dt.datetime.now(TZ).strftime("%H:%M:%S.%f")[:-3]
    who = getattr(_ctx, "who", "")
    with _log_lock:
        sys.stdout.write(f"[{now}] {f'[{who}] ' if who else ''}{msg}\n")
        sys.stdout.flush()


def parse_delay_arg(spec: str) -> tuple:
    """解析 --delay 参数：支持 '3.0'（自动 ±0.3s 抖动）或 '2.5-3.8'。"""
    s = spec.strip()
    if "-" in s:
        a, b = s.split("-", 1)
        lo, hi = float(a), float(b)
    else:
        mid = float(s)
        lo, hi = max(0.0, mid - 0.3), mid + 0.3
    if lo < 0 or hi < lo:
        raise SystemExit(f"--delay 参数无效：{spec}（示例：--delay 2.5-3.8 或 --delay 3.0）")
    return lo, hi


def resolve_policy(cfg: dict, profile: str | None, delay_arg: str | None = None) -> tuple:
    extra = cfg.get("gentle") if isinstance(cfg.get("gentle"), dict) else {}
    chosen = profile or extra.get("profile") or "balanced"
    if chosen not in PROFILES:
        raise SystemExit(f"未知档位：{chosen}（可选：{' / '.join(PROFILES)}）")
    policy = dict(PROFILES[chosen])
    for key, value in extra.items():
        if key in policy and value is not None:
            policy[key] = value
    if delay_arg:
        lo, hi = parse_delay_arg(delay_arg)
        policy["delay_min"] = lo
        policy["delay_max"] = hi
        policy["valve_floor_seconds"] = min(float(policy["valve_floor_seconds"]), lo)
    policy["delay_max"] = max(float(policy["delay_min"]), float(policy["delay_max"]))
    policy["account_stagger_max"] = max(float(policy["account_stagger_min"]), float(policy["account_stagger_max"]))
    return chosen, policy


class SessionExpired(Exception):
    pass


class Account:
    def __init__(self, name: str, sid: str, form: dict, index: int, prefer: list | None = None,
                 own_only: bool = False, strict: bool = False, avoid: list | None = None):
        self.name = name
        self.sid = sid
        self.form = form
        self.index = index
        self.prefer = prefer or []
        self.own_only = own_only and bool(self.prefer)
        self.strict = strict and bool(self.prefer)
        self.avoid = avoid or []

    def refresh_hint(self) -> str:
        return f"运行 ./set_session.sh {self.name} <新ID>"


def entry_name(entry, index: int) -> str:
    return (entry.get("name") if isinstance(entry, dict) else None) or f"账号{index + 1}"


def entry_sid(entry) -> str:
    return (entry.get("wx_session_user_id") if isinstance(entry, dict) else entry) or ""


def config_accounts(cfg: dict) -> list:
    if "extra_accounts" in cfg or "wx_session_user_id" in cfg:
        raise SystemExit("config.json 还是旧格式（顶层 wx_session_user_id / extra_accounts），请把账号都写进 accounts 列表")
    return cfg.get("accounts") or []


def load_accounts(cfg: dict, only: str | None = None) -> list:
    accounts = []
    for index, entry in enumerate(config_accounts(cfg)):
        name = entry_name(entry, index)
        if any(a.name == name for a in accounts):
            raise SystemExit(f"config.json 里有重名的账号：{name}")
        info = entry if isinstance(entry, dict) else {}
        prefer = info.get("prefer") or []
        if isinstance(prefer, str):
            prefer = [prefer]
        avoid = info.get("avoid") or []
        if isinstance(avoid, str):
            avoid = [avoid]
        accounts.append(Account(name, entry_sid(entry), info.get("form") or {}, index, prefer,
                                bool(info.get("own_only")), bool(info.get("strict")), avoid))
    if not accounts:
        raise SystemExit("config.json 的 accounts 里还没有账号，用 ./set_session.sh <登录ID> 添加")
    if only:
        accounts = [a for a in accounts if a.name == only]
        if not accounts:
            raise SystemExit(f"config.json 里没有叫 {only} 的账号")
    for a in [a for a in accounts if not a.sid]:
        print(f"账号 {a.name} 还没有登录 ID，跳过（{a.refresh_hint()}）", flush=True)
    accounts = [a for a in accounts if a.sid]
    if not accounts:
        raise SystemExit("没有可用的账号")
    return accounts


def account_index(data: dict, acct: Account) -> int | None:
    entries = data.get("accounts") or []
    for i, entry in enumerate(entries):
        if isinstance(entry, dict) and entry_name(entry, i) == acct.name:
            return i
    if acct.index < len(entries) and entry_name(entries[acct.index], acct.index) == acct.name:
        return acct.index
    return None


def write_config(data: dict) -> None:
    CFG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def persist_sid(acct: Account) -> None:
    with _cfg_lock:
        data = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        i = account_index(data, acct)
        if i is None:
            return
        entries = data["accounts"]
        if isinstance(entries[i], dict):
            entries[i]["wx_session_user_id"] = acct.sid
        else:
            entries[i] = acct.sid
        write_config(data)


class Api:
    def __init__(self, cfg: dict, acct: Account, host: str, https: bool = True, scene_val: int = 1089):
        self.cfg = cfg
        self.acct = acct
        self.host = host
        self.https = https
        self.scene_val = scene_val
        self.conn = None

    def close(self) -> None:
        if self.conn:
            self.conn.close()
        self.conn = None

    def _open(self) -> None:
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        self.conn = cls(self.host, timeout=self.cfg.get("timeout_seconds", 5))

    def connect(self) -> None:
        self.close()
        self._open()
        try:
            self.conn.connect()
        except (OSError, http.client.HTTPException):
            self.close()

    def _request(self, method: str, path: str, params: dict, body: dict | None):
        query = urllib.parse.urlencode({"app": APP, **params})
        url = f"/api3/{path}?{query}"
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        headers = {
            "User-Agent": UA,
            "xweb_xhr": "1",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Referer": REFERER,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        for attempt in range(2):
            try:
                if self.conn is None:
                    self._open()
                self.conn.request(method, url, body=data, headers=headers)
                resp = self.conn.getresponse()
                raw = resp.read()
                break
            except (OSError, http.client.HTTPException):
                self.close()
                if attempt:
                    raise
        payload = json.loads(raw)
        new_sid = resp.getheader("X-Wx-Session-User-Id") or payload.get("wx_session_user_id")
        if new_sid and new_sid != self.acct.sid:
            self.acct.sid = new_sid
            persist_sid(self.acct)
            log(f"服务器给账号 {self.acct.name} 下发了新的 wx_session_user_id，已写回 config.json")
        if str(payload.get("code")) == "1401":
            raise SessionExpired(payload.get("msg") or "登录态失效")
        return resp.headers.get("Date"), payload

    def load_service(self):
        return self._request("GET", "personal_services/load_service_v2", {
            "wx_session_user_id": self.acct.sid,
            "service_id": self.cfg["service_id"],
            "service_record_id": "null",
        }, None)

    def create_record(self, service_record: dict):
        return self._request("POST", "personal_services/create_service_record_v2", {}, {
            "scene_val": self.scene_val,
            "wx_session_user_id": self.acct.sid,
            "service_id": self.cfg["service_id"],
            "service_record_id": None,
            "service_record": service_record,
            "child_service_records": [],
        })


def norm_title(title) -> str:
    return re.sub(r"[\s:：]", "", str(title or ""))


def resolve_date(spec: str | None, today: dt.date | None = None) -> str:
    today = today or dt.datetime.now(TZ).date()
    if not spec or spec == "today":
        return today.isoformat()
    if spec == "tomorrow":
        return (today + dt.timedelta(days=1)).isoformat()
    return dt.date.fromisoformat(spec).isoformat()


def target_day(data: dict, date_value: str) -> dict | None:
    for week in data.get("reserve_data") or []:
        for day in week:
            if day.get("date_value") == date_value:
                return day
    cur = data.get("current_reserve_data")
    if cur and cur.get("date_value") == date_value:
        return cur
    return None


def slot_left(item: dict) -> int | None:
    try:
        return int(item.get("su_count"))
    except (TypeError, ValueError):
        return None


def slot_capacity(item: dict) -> int | None:
    try:
        return int(item.get("count"))
    except (TypeError, ValueError):
        return slot_left(item)


def time_key(name: str) -> str:
    m = re.search(r"\d{1,2}[:：]\d{2}.*", str(name))
    return m.group(0) if m else str(name)


def matches(item: dict, patterns: list) -> bool:
    return any(all(t in str(item.get("name", "")) for t in p.split()) for p in patterns)


def rank_slots(items: list, prefer: list, fallback_any: bool) -> list:
    open_items = [i for i in items if not i.get("b_close") and (slot_left(i) is None or slot_left(i) > 0)]
    ranked, seen = [], set()
    for pattern in prefer:
        tokens = pattern.split()
        for item in open_items:
            if item["id"] not in seen and all(t in str(item.get("name", "")) for t in tokens):
                ranked.append(item)
                seen.add(item["id"])
    if fallback_any:
        ranked += [i for i in open_items if i["id"] not in seen]
    preferred_times = [time_key(i["name"]) for p in prefer for i in items
                       if all(t in str(i.get("name", "")) for t in p.split())]
    ordered, placed = [], set()
    for key in dict.fromkeys(preferred_times + [time_key(i["name"]) for i in ranked]):
        for item in [i for i in ranked if time_key(i["name"]) == key] + \
                    [i for i in open_items if time_key(i["name"]) == key]:
            if item["id"] not in placed:
                ordered.append(item)
                placed.add(item["id"])
    return ordered


def favor_times(ranked: list, keys: list) -> list:
    if not keys:
        return ranked
    return [i for i in ranked if time_key(i["name"]) in keys] + \
           [i for i in ranked if time_key(i["name"]) not in keys]


def account_ranking(items: list, acct: Account, prefer: list, fallback_any: bool, won_keys: list) -> list:
    if acct.avoid:
        items = [i for i in items if not matches(i, acct.avoid)]
    shared = favor_times(rank_slots(items, prefer, fallback_any), won_keys)
    if not acct.prefer:
        return shared
    own = rank_slots(items, acct.prefer, False)
    if acct.strict:
        own = [i for i in own if matches(i, acct.prefer)]
    if acct.own_only:
        return own
    own_ids = {i["id"] for i in own}
    return own + [i for i in shared if i["id"] not in own_ids]


def in_own_times(acct: Account, item: dict, items: list) -> bool:
    if acct.strict:
        return matches(item, acct.prefer)
    own_keys = {time_key(i["name"]) for i in items
                for p in acct.prefer if all(t in str(i.get("name", "")) for t in p.split())}
    return time_key(item["name"]) in own_keys


def first_time_group(ranked: list, limit: int | None = None) -> list:
    if not ranked:
        return []
    key = time_key(ranked[0]["name"])
    return [i for i in ranked if time_key(i["name"]) == key][:limit]


def build_form(acct: Account, data: dict) -> dict:
    form = dict(data.get("init_service_record") or {})
    user_form = {norm_title(k): v for k, v in (acct.form or {}).items()}
    missing = []
    for field in data.get("fields") or []:
        title = norm_title(field.get("title"))
        value = user_form.get(title)
        if value not in (None, ""):
            form[field["col_name"]] = value
        if field.get("valid_presence") and form.get(field["col_name"]) in (None, ""):
            missing.append(title)
    if missing:
        raise SystemExit(f"账号 {acct.name} 的 form 里缺少必填项：{'、'.join(missing)}")
    return form


def describe(items: list) -> str:
    rows = []
    for i in items:
        state = "关闭" if i.get("b_close") else f"余 {i.get('su_count')}"
        rows.append(f"  - {i.get('name')}  [{state}]  时间:{i.get('b_time')}  id:{i.get('id')}")
    return "\n".join(rows) or "  （没有时段数据，预约未开放时服务器不返回时段）"


def server_offset(host: str, https: bool, timeout: float, seconds: float = 3.0) -> tuple:
    """高精度校时（同 grab.py）：连续请求 3 秒并取跳秒区间交集，确保时间底线精确到 ~±0.03 秒。"""
    cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
    conn = cls(host, timeout=timeout)
    lo, hi, errors = -math.inf, math.inf, 0
    end = time.time() + seconds
    try:
        while time.time() < end:
            try:
                t0 = time.time()
                conn.request("HEAD", "/", headers={"User-Agent": UA})
                resp = conn.getresponse()
                resp.read()
                t1 = time.time()
            except (OSError, http.client.HTTPException):
                conn.close()
                errors += 1
                if errors >= 3:
                    raise
                continue
            date_hdr = resp.headers.get("Date") or ""
            if date_hdr:
                parsed = email.utils.parsedate_to_datetime(date_hdr)
                if parsed.tzinfo is not None:
                    sec = parsed.timestamp()
                    lo, hi = max(lo, sec - t1), min(hi, sec + 1 - t0)
            time.sleep(random.uniform(0, 0.03))
    finally:
        conn.close()
    if lo == -math.inf:
        raise ValueError("没有拿到服务器时间")
    return (lo + hi) / 2, abs(hi - lo) / 2


def reload_sid(acct: Account) -> bool:
    try:
        data = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    i = account_index(data, acct)
    sid = entry_sid(data["accounts"][i]) if i is not None else ""
    if sid and sid != acct.sid:
        acct.sid = sid
        return True
    return False


def session_expired_banner(acct: Account) -> None:
    log("=" * 56)
    log(f"账号 {acct.name} 登录态失效！请{acct.refresh_hint()}")
    log(f"新 ID 写进 {CFG_PATH} 后脚本会自动继续，不用重启")
    log("=" * 56)


def wait_for_new_session(api: Api, deadline: float = float("inf")) -> bool:
    acct = api.acct
    bad = acct.sid
    session_expired_banner(acct)
    last_note = time.time()
    while time.time() < deadline:
        reload_sid(acct)
        if acct.sid != bad:
            try:
                api.load_service()
                log("读到新的登录 ID，登录态有效，继续")
                return True
            except SessionExpired:
                bad = acct.sid
                log("新的登录 ID 也无效，请再抓一次")
            except (OSError, http.client.HTTPException, ValueError) as e:
                api.close()
                log(f"检查新登录态时出错，稍后重试：{e!r}")
        if time.time() - last_note >= 60:
            log("登录态仍然失效，还在等新的登录 ID……")
            last_note = time.time()
        time.sleep(1)
    return False


def check_sessions(apis: dict, expired: set) -> None:
    ok = 0
    for acct, api in apis.items():
        reload_sid(acct)
        try:
            api.load_service()
            ok += 1
            if acct in expired:
                expired.discard(acct)
                log(f"账号 {acct.name} 读到新的登录 ID，登录态有效")
        except SessionExpired:
            if acct not in expired:
                expired.add(acct)
                session_expired_banner(acct)
        except (OSError, http.client.HTTPException, ValueError) as e:
            api.close()
            log(f"定时检查账号 {acct.name} 的登录态时出错，稍后再查：{e!r}")
    if ok == len(apis):
        log(f"定时检查：{'登录态正常' if ok == 1 else f'{ok} 个账号登录态都正常'}")
    elif expired:
        log(f"仍然失效的账号：{'、'.join(a.name for a in expired)}，每分钟再查一次")


def sleep_until(apis: dict, cfg: dict, until: float) -> None:
    expired = set()
    while (left := until - time.time()) > 0:
        step = min(left, 60 if expired else cfg.get("heartbeat_seconds", 600))
        if left - step < 300 < left:
            step = left - 300
        time.sleep(step)
        if until - time.time() <= 0:
            return
        check_sessions(apis, expired)


def known_slots(date_value: str) -> list:
    try:
        configs = json.loads(KNOWN_SLOTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    weekday = "周" + WEEKDAY_NAMES[dt.date.fromisoformat(date_value).weekday()]
    for conf in configs:
        if weekday in (conf.get("rule_value") or []):
            return [{"id": i["id"], "name": i["name"], "count": i.get("count"), "su_count": i.get("count"),
                     "b_close": i.get("b_close", False)} for i in conf.get("items") or []]
    return []


def submit(api: Api, form: dict, date_value: str, item: dict) -> tuple:
    record = {**form, "wx_reserve_date": date_value, "wx_reserve_name": item["name"], "wx_reserve_id": item["id"]}
    t0 = time.time()
    try:
        _, resp = api.create_record(record)
    except (OSError, http.client.HTTPException, ValueError) as e:
        api.close()
        log(f"提交 {item['name']} 出错（{time.time() - t0:.2f} 秒）：{e!r}")
        return "neterr", {}
    took = time.time() - t0
    code = str(resp.get("code"))
    if code == "1200":
        rec = (resp.get("data") or {}).get("service_record") or {}
        log(f"预约成功！记录 id={rec.get('id')}，时段={item['name']}（{took:.2f} 秒）")
    else:
        errors = f" errors={resp.get('errors')}" if resp.get("errors") else ""
        log(f"提交 {item['name']} 失败 code={code} msg={resp.get('msg')}{errors}（{took:.2f} 秒）")
    return code, resp


class Board:
    """共享实时场地状态板，同时记录今日原开放集合供安全阀判断是否被外部抢走。"""

    def __init__(self):
        self.cond = threading.Condition()
        self.items = None
        self.full = set()
        self.opened_ids = set()
        self.version = 0

    def update(self, items: list) -> None:
        if not items:
            return
        with self.cond:
            for i in items:
                if i.get("b_close"):
                    self.full.add(i["id"])
                    continue
                cap = slot_capacity(i)
                left = slot_left(i)
                if cap is None or cap > 0 or (left is not None and left > 0):
                    self.opened_ids.add(i["id"])
                if left == 0:
                    self.full.add(i["id"])
            self.items = [dict(i, su_count=0) if i["id"] in self.full else dict(i) for i in items]
            self.version += 1
            self.cond.notify_all()

    def mark_full(self, item_id: str) -> None:
        with self.cond:
            self.full.add(item_id)
            if self.items:
                self.items = [dict(i, su_count=0) if i["id"] == item_id else i for i in self.items]
            self.version += 1
            self.cond.notify_all()

    def snapshot(self) -> tuple:
        with self.cond:
            return self.items, self.version, set(self.full)

    def snapshot_ext(self) -> tuple:
        with self.cond:
            return self.items, self.version, set(self.full), set(self.opened_ids)

    def wait_change(self, version: int, timeout: float) -> None:
        if timeout <= 0:
            return
        with self.cond:
            self.cond.wait_for(lambda: self.version != version, timeout)


class Coordinator:
    """多个账号之间的分场与同时间段协同。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.claims = {}
        self.won = {}
        self.shared_won = []

    def try_claim(self, who: str, item: dict) -> bool:
        with self.lock:
            return self.claims.setdefault(item["id"], who) == who

    def release(self, who: str, ids=None) -> None:
        with self.lock:
            won = self.won.get(who, set())
            for sid, owner in list(self.claims.items()):
                if owner == who and sid not in won and (ids is None or sid in ids):
                    del self.claims[sid]

    def record_win(self, who: str, item: dict, shared: bool) -> None:
        with self.lock:
            first = who not in self.won
            self.won.setdefault(who, set()).add(item["id"])
            self.claims[item["id"]] = who
            if first and shared:
                self.shared_won.append(item)

    def won_times(self) -> list:
        with self.lock:
            return [time_key(i["name"]) for i in self.shared_won]

    def all_won_ids(self) -> set:
        with self.lock:
            out = set()
            for s in self.won.values():
                out |= s
            return out

    def taken_by_others(self, who: str) -> set:
        with self.lock:
            return {sid for sid, owner in self.claims.items() if owner != who}


def sleep_till(t: float, stop: threading.Event | None = None) -> None:
    left = t - time.time()
    if left <= 0:
        return
    if stop is not None:
        stop.wait(left)
    else:
        time.sleep(left)


class AccountGrab:
    """单个账号的抢场执行器：等待拟人开枪窗口（带库存安全阀），放行后全速并发提交。"""

    def __init__(self, acct: Account, apis: list, cfg: dict, policy: dict, date_value: str, form: dict,
                 blind: list, coord: Coordinator, board: Board, parallel: int,
                 fire_at: float, valve_floor_at: float, stagger: float, deadline: float,
                 start_now: bool, tags: list):
        self.acct = acct
        self.apis = apis
        self.policy = policy
        self.date_value = date_value
        self.form = form
        self.blind = blind
        self.coord = coord
        self.board = board
        self.fire_at = fire_at
        self.valve_floor_at = valve_floor_at
        self.stagger = stagger
        self.deadline = deadline
        self.start_now = start_now
        self.tags = tags
        self.lanes_max = max(1, min(parallel, len(apis)))
        self.prefer = cfg.get("prefer", [])
        self.fallback = cfg.get("fallback_any", False)
        self.retry_interval = cfg.get("retry_interval_seconds", 0.03)
        self.slow_reply = cfg.get("slow_reply_seconds", 0.3)

        self.lock = threading.Lock()
        self.relogin_lock = threading.Lock()
        self.gate_cond = threading.Condition()
        self.released = start_now
        self.release_at = 0.0
        self.valve_triggered = False

        self.lanes = []
        self.pending = {}
        self.tokens = count()
        self.failed = set()
        self.wins = []
        self.win_time = ""
        self.fatal = ""
        self.done = threading.Event()
        self.waiting = ""
        self.noted_at = 0.0
        self.post_count = 0

    def items(self) -> list:
        items, _, full = self.board.snapshot()
        dead = full | self.failed
        return [dict(i, su_count=0) if i["id"] in dead else i for i in items or self.blind]

    def ranking(self, items: list) -> list:
        ranked = account_ranking(items, self.acct, self.prefer, self.fallback, self.coord.won_times())
        taken = self.coord.taken_by_others(self.acct.name)
        if self.acct.strict:
            own = [i for i in ranked if matches(i, self.acct.prefer)]
            free = [i for i in own if i["id"] not in taken]
            if free:
                return first_time_group(free)
            rest = [i for i in ranked if not matches(i, self.acct.prefer)]
            free_rest = [i for i in rest if i["id"] not in taken]
            return first_time_group(free_rest or rest)
        free_all = [i for i in ranked if i["id"] not in taken]
        return first_time_group(free_all or ranked)

    def _baseline_group(self, items: list, opened_ids: set) -> list:
        unbooked = [
            dict(i, su_count=max(1, slot_capacity(i) or 1))
            for i in items
            if not i.get("b_close") and i["id"] in opened_ids
        ]
        if not unbooked:
            return []
        ranked = account_ranking(unbooked, self.acct, self.prefer, self.fallback, self.coord.won_times())
        if self.acct.strict:
            own = [i for i in ranked if matches(i, self.acct.prefer)]
            if own:
                return first_time_group(own)
        return first_time_group(ranked)

    def check_safety_valve(self) -> str:
        items, _, full, opened_ids = self.board.snapshot_ext()
        if not items or not opened_ids:
            return ""
        our_won = self.coord.all_won_ids()
        ext_global = (opened_ids & full) - our_won

        group = self._baseline_group(items, opened_ids)
        group_ids = {i["id"] for i in group}
        ext_group = (group_ids & full) - our_won
        taken_others = self.coord.taken_by_others(self.acct.name)
        remain_group = [i for i in group if i["id"] not in full and i["id"] not in taken_others]

        p = self.policy
        if group_ids and not remain_group:
            return "首选时段已全部不可选，提前转入下一优先时段"
        if len(ext_group) >= p["valve_group_lost"]:
            return f"首选时段已被外部抢走 {len(ext_group)} 场（仅剩 {len(remain_group)} 场）"
        if len(ext_group) >= 1 and len(remain_group) <= p["valve_min_slots"]:
            return f"首选时段已有被抢且仅剩 {len(remain_group)} 场"
        if len(ext_global) >= p["valve_global_lost"]:
            return f"全场已被外部抢走 {len(ext_global)} 场（竞争激烈）"
        return ""

    def wait_gate_leader(self) -> None:
        """0 号连接负责守门：等到 fire_at，期间一旦实时列表触发安全阀就越过安全底线后立刻放行全部连接。"""
        if self.start_now:
            self._open_gate()
            return
        while not self.done.is_set() and time.time() < min(self.fire_at, self.deadline):
            reason = self.check_safety_valve()
            if reason:
                self.valve_triggered = True
                if time.time() < self.valve_floor_at:
                    wait = self.valve_floor_at - time.time()
                    log(f"[安全阀预警] {reason}，等待 {wait:.2f} 秒越过成交时间底线后全速开抢")
                    self.done.wait(wait)
                else:
                    log(f"[安全阀触发] {reason}，提前全速开抢")
                break
            _, version, _ = self.board.snapshot()
            wait = min(self.fire_at - time.time(), max(0.0, self.deadline - time.time()), 0.15)
            if wait <= 0:
                break
            self.board.wait_change(version, wait)
        self._open_gate()

    def _open_gate(self) -> None:
        with self.gate_cond:
            if not self.released:
                self.released = True
                self.release_at = time.time()
                self.gate_cond.notify_all()

    def wait_gate_follower(self, k: int) -> bool:
        with self.gate_cond:
            while not self.released and not self.done.is_set() and time.time() < self.deadline:
                rem = min(0.5, max(0.0, self.deadline - time.time()))
                if rem <= 0:
                    return False
                self.gate_cond.wait(rem)
            rel_at = self.release_at
        if self.done.is_set() or time.time() >= self.deadline:
            return False
        if self.stagger > 0:
            sleep_till(rel_at + k * self.stagger, self.done)
        return not self.done.is_set() and time.time() < self.deadline

    def take_lane(self, k: int) -> tuple:
        with self.lock:
            ranked = self.ranking(self.items())
            ids = {i["id"] for i in ranked}
            stale = {lane["id"] for lane in self.lanes if lane["id"] not in ids}
            if stale:
                self.lanes = [lane for lane in self.lanes if lane["id"] not in stale]
                self.coord.release(self.acct.name, stale)
            occupied = {lane["id"] for lane in self.lanes} | {i for i, _ in self.pending.values()}
            for item in ranked:
                if len(occupied) >= self.lanes_max:
                    break
                if item["id"] not in occupied and self.coord.try_claim(self.acct.name, item):
                    self.lanes.append(item)
                    occupied.add(item["id"])
            self.waiting = "others" if ranked else ""
            if not self.lanes:
                return None, None
            item = self.lanes[k % len(self.lanes)]
            if self.lanes_max > 1:
                now = time.time()
                slow = {i for i, t0 in self.pending.values() if now - t0 >= self.slow_reply}
                if slow and item["id"] not in slow:
                    item = next((lane for lane in self.lanes if lane["id"] in slow), None)
                    if item is None:
                        self.waiting = "slow"
                        return None, None
            token = next(self.tokens)
            self.pending[token] = (item["id"], time.time())
            return item, token

    def finish(self, token: int) -> None:
        with self.lock:
            self.pending.pop(token, None)

    def _mark_done(self) -> None:
        self.done.set()
        with self.gate_cond:
            self.gate_cond.notify_all()

    def handle(self, item: dict, code: str, resp: dict) -> bool:
        name = self.acct.name
        if code == "1200":
            with self.lock:
                self.wins.append(item)
                n_wins = len(self.wins)
                if n_wins == 1:
                    self.win_time = dt.datetime.now(TZ).strftime("%H:%M:%S.%f")[:-3]
                shared = not in_own_times(self.acct, item, self.items())
            self._mark_done()
            self.coord.record_win(name, item, shared)
            if n_wins == 1:
                self.coord.release(name)
            else:
                log("!" * 56)
                log(f"注意：同时约到了 {n_wins} 个场：{'、'.join(i['name'] for i in self.wins)}，请到小程序里取消多余的")
                log("!" * 56)
            return True
        if code in (BUSY_CODE, "neterr"):
            return False
        if code == "1333":
            self.fatal = f"账号 {name} 表单校验失败，检查 config.json 里的 form"
            self._mark_done()
            return True
        if code == "1433":
            self.board.mark_full(item["id"])
        self.board.update((target_day(resp.get("data") or {}, self.date_value) or {}).get("items") or [])
        with self.lock:
            self.failed.add(item["id"])
            self.lanes = [lane for lane in self.lanes if lane["id"] != item["id"]]
        self.coord.release(name, {item["id"]})
        return False

    def relogin(self, api: Api, bad_sid: str) -> None:
        with self.relogin_lock:
            if self.done.is_set() or self.acct.sid != bad_sid:
                return
            with self.lock:
                self.lanes = []
            self.coord.release(self.acct.name)
            if not wait_for_new_session(api, self.deadline):
                self._mark_done()

    def idle(self, k: int) -> None:
        items, version, _ = self.board.snapshot()
        if k == 0 and items and self.waiting != "slow" and time.time() - self.noted_at >= 5:
            self.noted_at = time.time()
            if self.waiting == "others":
                log("剩下的场其他账号正在提交，等它们的结果")
            else:
                log(f"{self.date_value} 暂无符合偏好且有余量的时段，继续轮询：\n" + describe(self.items()))
        wait = 0.02 if self.waiting == "slow" else 0.1
        self.board.wait_change(version, min(wait, max(0.0, self.deadline - time.time())))

    def worker(self, k: int, warm_at: float) -> None:
        api = self.apis[k]
        sleep_till(warm_at, self.done)
        if self.done.is_set():
            return
        api.connect()
        if k == 0:
            self.wait_gate_leader()
        else:
            if not self.wait_gate_follower(k):
                return

        first = True
        while not self.done.is_set() and time.time() < self.deadline:
            item, token = self.take_lane(k)
            if item is None:
                self.idle(k)
                continue
            if first:
                log(f"首次提交：{item['name']}")
                first = False
            sid = self.acct.sid
            with self.lock:
                self.post_count += 1
            try:
                code, resp = submit(api, self.form, self.date_value, item)
            except SessionExpired:
                self.finish(token)
                self.relogin(api, sid)
                continue
            stop = self.handle(item, code, resp)
            self.finish(token)
            if stop:
                return
            if code in (BUSY_CODE, "neterr"):
                self.done.wait(self.retry_interval)

    def run_conn(self, k: int, warm_at: float) -> None:
        _ctx.who = self.tags[k]
        try:
            self.worker(k, warm_at)
        except Exception as e:  # noqa: BLE001
            log(f"异常退出：{e!r}")
        finally:
            if k == 0:
                self._open_gate()


def refresher(board: Board, apis: list, cfg: dict, policy: dict, date_value: str, warm_at: float,
              start_at: float, deadline: float, stop: threading.Event) -> None:
    """放场瞬间全速拉取开放列表，开放后前 fast_refresh_window 秒高频刷新（供安全阀和选场使用）。"""
    _ctx.who = "列表"
    interval = cfg.get("poll_interval_seconds", 0.2)
    wait_interval = policy["wait_refresh_seconds"]
    fast_window = policy["fast_refresh_window"]
    slow_interval = policy["refresh_seconds"]
    if stop.wait(max(0.0, warm_at - time.time())):
        return
    apis[0].connect()
    if stop.wait(max(0.0, start_at - time.time())):
        return
    polls = turn = 0
    logged_open = False
    while not stop.is_set() and time.time() < deadline:
        api = apis[turn % len(apis)]
        fast = time.time() - start_at <= fast_window
        try:
            _, payload = api.load_service()
        except SessionExpired:
            reload_sid(api.acct)
            turn += 1
            if turn % len(apis) == 0:
                stop.wait(1)
            continue
        except (OSError, http.client.HTTPException, ValueError) as e:
            api.close()
            log(f"拉场地列表出错，稍后重试：{e!r}")
            stop.wait(0.05 if fast else max(interval, 1))
            continue
        polls += 1
        data = payload.get("data") or {}
        forbid = ((data.get("service") or {}).get("valid_reserve") or {}).get("is_create_forbid", True)
        if forbid:
            if polls % 15 == 1:
                log("仍未开放，继续轮询")
            stop.wait(0.02 if fast else interval)
            continue
        day_items = (target_day(data, date_value) or {}).get("items") or []
        board.update(day_items)
        if day_items and not logged_open:
            logged_open = True
            open_cnt = sum(1 for i in day_items if not i.get("b_close") and (slot_left(i) or 0) > 0)
            log(f"已开放，实时列表就绪（{date_value} 当前可约 {open_cnt} 场）")
        stop.wait(wait_interval if fast else slow_interval)


def prepare(main_apis: dict, forms: dict, cfg: dict, host: str, https: bool) -> float:
    offset = [0.0]

    def sync():
        try:
            value, err = server_offset(host, https, cfg.get("timeout_seconds", 5))
        except (OSError, http.client.HTTPException, ValueError) as e:
            log(f"校时出错，按本机时间开抢：{e!r}")
            return
        if abs(value) > 60:
            log(f"校时偏差异常（{value:+.1f} 秒），忽略并改按本机时间开抢")
            return
        offset[0] = value
        log(f"服务器时间比本机{'快' if value >= 0 else '慢'} {abs(value):.3f} 秒（误差约 ±{err:.3f} 秒）")

    def refresh(acct, api):
        reload_sid(acct)
        try:
            _, payload = api.load_service()
            forms[acct] = build_form(acct, payload["data"])
        except SessionExpired:
            session_expired_banner(acct)
        except SystemExit as e:
            log(f"{e}，先用之前的表单")
        except (OSError, http.client.HTTPException, ValueError, KeyError):
            api.close()

    threads = [threading.Thread(target=sync)] + \
              [threading.Thread(target=refresh, args=(acct, api)) for acct, api in main_apis.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return offset[0]


def compute_schedule(n_accounts: int, open_ts: float, policy: dict, start_now: bool) -> list:
    """为每个账号计算 (fire_at, valve_floor_at)：首号随机落入拟人区间，多号自然错开且总跨度封顶。"""
    if start_now:
        now = time.time()
        return [(now, now) for _ in range(n_accounts)]

    base_delay = random.uniform(policy["delay_min"], policy["delay_max"])
    if n_accounts > 1:
        raw_step = random.uniform(policy["account_stagger_min"], policy["account_stagger_max"])
        step = min(raw_step, policy["max_stagger_span"] / (n_accounts - 1))
    else:
        step = 0.0

    schedule = []
    for i in range(n_accounts):
        acct_jitter = random.uniform(-0.10, 0.15) if i > 0 else 0.0
        submit_delay = max(policy["valve_floor_seconds"], base_delay + i * step + acct_jitter)
        valve_floor = min(submit_delay, policy["valve_floor_seconds"] + min(i * random.uniform(0.15, 0.35), 0.80))
        schedule.append((open_ts + submit_delay, open_ts + valve_floor))
    return schedule


def run(accounts: list, cfg: dict, policy_name: str, policy: dict, host: str, https: bool, start_now: bool) -> int:
    now = dt.datetime.now(TZ)
    open_time = cfg.get("open_time") or ""
    open_day = now.date()
    open_at = 0.0
    if open_time and not start_now:
        hh, mm, ss = (int(x) for x in open_time.split(":"))
        open_dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if now - open_dt > dt.timedelta(minutes=5):
            open_dt += dt.timedelta(days=1)
        weekdays = set(cfg.get("open_weekdays") or range(1, 8))
        while open_dt.isoweekday() not in weekdays:
            open_dt += dt.timedelta(days=1)
        open_day = open_dt.date()
        open_at = open_dt.timestamp()
    else:
        start_now = True
    date_value = resolve_date(cfg.get("reserve_date"), open_day)

    extra = cfg.get("gentle") if isinstance(cfg.get("gentle"), dict) else {}
    parallel = max(1, min(int(extra.get("parallel", cfg.get("parallel", 1))), 2))
    connections = max(1, min(int(extra.get("connections", cfg.get("connections", 3))), 4))
    stagger = float(cfg.get("stagger_seconds", 0.025))
    scene_val = int(policy["scene_val"])

    main_apis, forms, expired, title = {}, {}, [], ""
    for acct in accounts:
        api = Api(cfg, acct, host, https, scene_val=scene_val)
        try:
            _, first = api.load_service()
        except SessionExpired:
            expired.append(acct)
            continue
        forms[acct] = build_form(acct, first["data"])
        title = title or (first["data"].get("service") or {}).get("title")
        main_apis[acct] = api
    if expired:
        for acct in expired:
            log(f"账号 {acct.name} 登录态失效，请{acct.refresh_hint()}")
        raise SessionExpired("、".join(a.name for a in expired))

    blind = known_slots(date_value) if cfg.get("blind_submit", True) else []
    when = dt.datetime.fromtimestamp(open_at, TZ).strftime("%m-%d %H:%M:%S") if not start_now else "立即"
    if not start_now:
        when += " " + WEEKDAY_NAMES[dt.datetime.fromtimestamp(open_at, TZ).weekday()].join(["周", ""])
    log(f"登录态有效。{title}，目标日期 {date_value}，放场时间 {when}")
    log(f"时间拟人档位：{policy_name}（{policy['label']}）")
    log(f"开枪火力：每号 {connections} 连接，单号并发 {parallel} 场，重试间隔 {cfg.get('retry_interval_seconds', 0.03):.2f}s；"
        f"安全阀(剩余<={policy['valve_min_slots']}或群内被抢>={policy['valve_group_lost']}或全场>={policy['valve_global_lost']})")
    if len(accounts) > 1:
        log(f"{len(accounts)} 个账号各抢一个场：{'、'.join(a.name for a in accounts)}")
        for acct in accounts:
            if acct.prefer:
                log(f"  {acct.name} 先抢{'这几个场' if acct.strict else '自己的时段'}：{'、'.join(acct.prefer)}，"
                    f"{'只抢这些' if acct.own_only else '抢光了再按顶层 prefer'}")
            if acct.avoid:
                log(f"  {acct.name} 不碰：{'、'.join(acct.avoid)}")

    if not start_now and open_at - time.time() > 5:
        wait = open_at - time.time() - 30
        if wait > 0:
            log(f"等待 {wait:.0f} 秒后校时，期间每 {cfg.get('heartbeat_seconds', 600) / 60:.0f} 分钟检查一次登录态……")
            sleep_until(main_apis, cfg, open_at - 30)
        offset = prepare(main_apis, forms, cfg, host, https)
        open_ts = open_at - offset
    elif not start_now and open_at - time.time() > 0:
        open_ts = open_at
        log("距离放场不足 5 秒，跳过校时直接按本机放场时刻进入等待")
    else:
        start_now = True
        blind = []
        open_ts = time.time()

    for api in main_apis.values():
        api.close()

    poll_seconds = cfg.get("poll_seconds", 60)
    deadline = open_ts + poll_seconds if poll_seconds > 0 else float("inf")
    acct_list = list(main_apis)
    schedule = compute_schedule(len(acct_list), open_ts, policy, start_now)

    # 列表连接在放场前预热；各账号提交连接在各自最早可能开枪（安全阀底线）前预热，既 0 延迟又防空闲断连
    warm_seconds = cfg.get("warm_seconds", 2.0)
    refresher_start = open_ts if start_now else (open_ts - 0.10)
    refresher_warm = refresher_start - warm_seconds

    many = len(acct_list) > 1
    board, coord = Board(), Coordinator()
    grabs = []
    for idx, acct in enumerate(acct_list):
        fire_at, valve_floor_at = schedule[idx]
        tags = [(acct.name if many else "") + (f"#{k + 1}" if connections > 1 else "") for k in range(connections)]
        apis = [Api(cfg, acct, host, https, scene_val=scene_val) for _ in range(connections)]
        grabs.append(AccountGrab(
            acct, apis, cfg, policy, date_value, forms[acct], blind, coord, board, parallel,
            fire_at, valve_floor_at, stagger, deadline, start_now, tags,
        ))
        if not start_now:
            log(f"  计划 [{acct.name}]：放场后 +{fire_at - open_ts:.2f}s 全速开抢（安全阀底线 +{valve_floor_at - open_ts:.2f}s）")

    stop = threading.Event()
    list_apis = [Api(cfg, acct, host, https, scene_val=scene_val) for acct in acct_list]
    list_thread = threading.Thread(
        target=refresher, daemon=True,
        args=(board, list_apis, cfg, policy, date_value, refresher_warm, refresher_start, deadline, stop),
    )
    threads = [
        threading.Thread(
            target=g.run_conn,
            args=(k, min(g.valve_floor_at, g.fire_at) - warm_seconds),
            name=f"{g.acct.name}#{k + 1}",
        )
        for g in grabs for k in range(connections)
    ]

    list_thread.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    list_thread.join(timeout=1.5)
    for api in list_apis:
        api.close()
    for g in grabs:
        for api in g.apis:
            api.close()

    for g in grabs:
        prefix = f"[{g.acct.name}] " if many else ""
        if g.fatal:
            log(prefix + g.fatal)
        elif not g.wins:
            log(prefix + "轮询时间用完，没有抢到")
    log("抢场结果：")
    for g in grabs:
        note = "（触发安全阀）" if g.valve_triggered else ""
        win_str = f"{'、'.join(i['name'] for i in g.wins)} @ {g.win_time}" if g.wins else "没抢到"
        log(f"  {g.acct.name}：{win_str}{note}")
    return 0 if any(g.wins for g in grabs) else 1


def check(accounts: list, cfg: dict, policy_name: str, policy: dict, host: str, https: bool) -> int:
    rc, shown_slots = 0, False
    extra = cfg.get("gentle") if isinstance(cfg.get("gentle"), dict) else {}
    conns = max(1, min(int(extra.get("connections", cfg.get("connections", 3))), 4))
    parallel = max(1, min(int(extra.get("parallel", cfg.get("parallel", 1))), 2))
    print(f"时间拟人档位：{policy_name}（{policy['label']}）")
    print(f"  首号开枪延迟：+{policy['delay_min']:.2f}~{policy['delay_max']:.2f}s | "
          f"多号错峰：+{policy['account_stagger_min']:.2f}~{policy['account_stagger_max']:.2f}s | "
          f"安全阀底线：+{policy['valve_floor_seconds']:.2f}s")
    print(f"  放行后火力：每号 {conns} 连接，单号并发 {parallel} 场，重试间隔 {cfg.get('retry_interval_seconds', 0.03):.2f}s")
    for acct in accounts:
        print(f"===== 账号：{acct.name} =====")
        api = Api(cfg, acct, host, https, scene_val=int(policy["scene_val"]))
        try:
            _, payload = api.load_service()
        except SessionExpired as e:
            print(f"登录态：失效（{e}），请{acct.refresh_hint()}")
            rc = max(rc, 2)
            continue
        finally:
            api.close()
        data = payload["data"]
        service = data["service"]
        vr = service.get("valid_reserve") or {}
        print(f"服务：{service.get('title')}")
        print(f"登录态：有效（code={payload.get('code')}）")
        if acct.prefer:
            print(f"{'只抢这几个场' if acct.strict else '自己的时段'}：{'、'.join(acct.prefer)}"
                  f"（{'只抢这些' if acct.own_only else '抢光了再按顶层 prefer'}）")
        if acct.avoid:
            print(f"不碰：{'、'.join(acct.avoid)}")
        print(f"当前是否禁止预约：{vr.get('is_create_forbid')}  {(vr.get('forbit_create_info') or {}).get('message', '')}")
        if service.get("limit_count_info_str"):
            print(f"次数限制：{service['limit_count_info_str']}")
        print("表单字段：")
        user_form = {norm_title(k): v for k, v in (acct.form or {}).items()}
        init = data.get("init_service_record") or {}
        for f in data.get("fields") or []:
            title = norm_title(f.get("title"))
            have = user_form.get(title) or init.get(f["col_name"])
            print(f"  - {title}  必填={f.get('valid_presence')}  已配置={'是' if have else '否'}"
                  f"{f'  将提交：{have}' if have else ''}")
        if not shown_slots:
            shown_slots = True
            date_value = resolve_date(cfg.get("reserve_date"))
            day = target_day(data, date_value) or {}
            print(f"{date_value} 的时段：\n{describe(day.get('items') or [])}")
        try:
            build_form(acct, data)
            print("表单检查：通过")
        except SystemExit as e:
            print(f"表单检查：{e}")
            rc = max(rc, 1)
    return rc


def server_name(data: dict) -> str:
    init = data.get("init_service_record") or {}
    for field in data.get("fields") or []:
        if norm_title(field.get("title")) == "姓名":
            return str(init.get(field["col_name"]) or "").strip()
    return ""


def set_session(sid: str, name: str | None, host: str, https: bool) -> str:
    if not sid or sid in ("null", "undefined"):
        raise SystemExit("登录 ID 是空的")
    with _cfg_lock:
        data = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    entries = config_accounts(data)
    names = [entry_name(e, i) for i, e in enumerate(entries)]
    for i, e in enumerate(entries):
        if entry_sid(e) == sid and (not name or names[i] == name):
            print(f"账号 {names[i]} 已经是这个登录 ID")
            return names[i]
    if not name:
        probe = Account("", sid, {}, len(entries))
        try:
            _, payload = Api(data, probe, host, https).load_service()
        except SessionExpired:
            raise SystemExit("这个登录 ID 无效或已过期，没有写入") from None
        sid = probe.sid
        base = server_name(payload.get("data") or {}) or f"账号{len(entries) + 1}"
        name, n = base, 2
        while name in names:
            name, n = f"{base}{n}", n + 1
    with _cfg_lock:
        data = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        entries = data.setdefault("accounts", [])
        names = [entry_name(e, i) for i, e in enumerate(entries)]
        if name in names:
            i = names.index(name)
            if isinstance(entries[i], dict):
                entries[i]["wx_session_user_id"] = sid
            else:
                entries[i] = sid
            print(f"已更新账号 {name} 的登录 ID")
        else:
            entries.append({"name": name, "wx_session_user_id": sid})
            print(f"新增账号 {name}")
        write_config(data)
    return name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只读检查，不提交")
    ap.add_argument("--now", action="store_true", help="不等放场时间和拟人延迟，立即全速抢")
    ap.add_argument("--profile", choices=list(PROFILES), help="档位：balanced（默认）/ tight / relaxed")
    ap.add_argument("--delay", help="自定义首号开枪延迟秒数，例如 2.6-3.6 或 3.0")
    ap.add_argument("--account", help="只用这个账号")
    ap.add_argument("--set-session", metavar="ID", help="新增账号，或配合 --name 更新这个账号的登录 ID，然后检查它")
    ap.add_argument("--name", help="和 --set-session 一起用：要新增或更新的账号名")
    ap.add_argument("--host", default="d2.xksyun.com", help=argparse.SUPPRESS)
    ap.add_argument("--plain-http", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    https = not args.plain_http
    if args.set_session:
        name = set_session(args.set_session.strip(), args.name, args.host, https)
        cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        policy_name, policy = resolve_policy(cfg, args.profile, args.delay)
        return check(load_accounts(cfg, name), cfg, policy_name, policy, args.host, https)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    policy_name, policy = resolve_policy(cfg, args.profile, args.delay)
    accounts = load_accounts(cfg, args.account)
    try:
        if args.check:
            return check(accounts, cfg, policy_name, policy, args.host, https)
        return run(accounts, cfg, policy_name, policy, args.host, https, args.now)
    except SessionExpired as e:
        log(f"登录态失效（{e}）。请在微信里重新打开小程序，再重新抓一次 wx_session_user_id")
        return 2


if __name__ == "__main__":
    sys.exit(main())
