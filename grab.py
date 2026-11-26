#!/usr/bin/env python3
"""揭阳校区场馆预约（d2.xksyun.com 预约平台）：在放场时刻自动选场并提交，支持多个账号各抢一个场。

读取同一文件夹下的 config.json。accounts 里每一项是一个账号，所有账号地位相同、一起抢，每个账号抢一个场。
一项可以直接写登录 ID 字符串（名字自动叫 账号1、账号2……），也可以写成
{"name": "账号名", "wx_session_user_id": "...", "form": {...}, "prefer": [...], "own_only": true}：
form 可以省略，服务器会用该账号以前填过的信息预填；
prefer 可以省略，省略就用顶层的 prefer。写了就先抢自己的时段，自己的时段都抢光了再按顶层 prefer 抢；
再加 "own_only": true 就只抢自己的时段，抢光了也不按顶层 prefer 抢。
默认自己的时段里某个场没了会先换同一时间段的其他场；加 "strict": true 就只抢 prefer 里点名的场，
点名的场都没了（约满，或者自己的其他账号正在提交、已经抢到）再按顶层 prefer 抢（配合 own_only 就直接放弃）。
"avoid": [...] 写的场这个账号一律不碰（写法同 prefer），用来把某些场留给别的账号。

和抢场速度有关的参数都可以不写，config.json 里写了才会覆盖默认值。
默认值就是 11/16 那次 3 个号 1.5 秒内全部抢到时用的参数，没有实测依据不要改：
  connections      每个账号开几个连接同时提交，默认 3，最多 4。连接越多每秒提交次数越多，但请求太密有被限流的风险
  parallel         每个账号同时提交几个不同的场地，默认 2，最多 2。设成 2 时有小概率一个号同时约到两个场
  poll_seconds     开抢后最多抢多少秒，默认 60
  blind_lead_seconds / lead_seconds  比放场时刻提前多少秒发出，默认 0.05 / 0.3（前者用于按已知时段 id 直接提交）
  stagger_seconds  开抢时各连接依次错开的秒数，默认 0.025
  poll_interval_seconds  放场前拉场地列表的间隔，默认 0.2
  timeout_seconds  单次请求的超时，默认 5

用法：
  python3 grab.py --check      只读检查：每个账号的登录态、表单字段，以及目标日期的时段
  python3 grab.py              有 open_time 就等到那一刻再抢；没有就立即开始轮询
  python3 grab.py --now        忽略 open_time，立即开始轮询并提交
  python3 grab.py --account 名字   只用这一个账号（可以和上面的选项一起用）
  python3 grab.py --set-session ID [--name 名字]
      新增一个账号（名字取服务器里该账号填过的姓名）；带 --name 就更新这个账号的登录 ID
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
_ctx = threading.local()
_cfg_lock = threading.Lock()
_log_lock = threading.Lock()


def log(msg: str) -> None:
    now = dt.datetime.now(TZ).strftime("%H:%M:%S.%f")[:-3]
    who = getattr(_ctx, "who", "")
    # print 会把内容和换行分两次写，多个账号线程同时打日志时会串行
    with _log_lock:
        sys.stdout.write(f"[{now}] {f'[{who}] ' if who else ''}{msg}\n")
        sys.stdout.flush()


class SessionExpired(Exception):
    pass


class Account:
    def __init__(self, name: str, sid: str, form: dict, index: int, prefer: list | None = None,
                 own_only: bool = False, strict: bool = False, avoid: list | None = None):
        self.name = name
        self.sid = sid
        self.form = form
        self.index = index  # 在 config.json 的 accounts 里的位置，用来找回只写了登录 ID 字符串的账号
        self.prefer = prefer or []  # 空表示跟顶层 prefer 走
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
    # 重新读文件再改，避免覆盖其他账号线程或抓包脚本刚写进去的登录 ID
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
    def __init__(self, cfg: dict, acct: Account, host: str, https: bool = True):
        self.cfg = cfg
        self.acct = acct
        self.host = host
        self.https = https
        self.conn = None

    def close(self) -> None:
        if self.conn:
            self.conn.close()
        self.conn = None

    def _open(self) -> None:
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        self.conn = cls(self.host, timeout=self.cfg.get("timeout_seconds", 5))

    def connect(self) -> None:
        """提前做好 TCP 和 TLS 握手（约 0.2 秒），开抢时第一次提交不用再等。连不上就等提交时再连。"""
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
                # keep-alive 连接可能被服务器关掉，重连一次
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
        return resp.getheader("Date"), payload

    def load_service(self):
        return self._request("GET", "personal_services/load_service_v2", {
            "wx_session_user_id": self.acct.sid,
            "service_id": self.cfg["service_id"],
            "service_record_id": "null",
        }, None)

    def create_record(self, service_record: dict):
        return self._request("POST", "personal_services/create_service_record_v2", {}, {
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


def matches(item: dict, patterns: list) -> bool:
    return any(all(t in str(item.get("name", "")) for t in p.split()) for p in patterns)


def rank_slots(items: list, prefer: list, fallback_any: bool) -> list:
    """按偏好顺序返回还有余量的时段。prefer 里每一项是空格分隔的关键词，全部出现在时段名里才算命中。"""
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
    # 同一时间段的场地排在一起：某个场抢不到时先换同一时间段的其他场（包括 prefer 里没写的），
    # 这个时间段都没了才轮到下一个时间段
    # 时间段的先后按 prefer 定，已约满的场也算，这样首选场满了仍会先换同一时间段的其他场
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
    """把 keys 里的时间段（其他账号已经抢到的时间段）挪到最前，让几个账号尽量约在同一时间段。"""
    if not keys:
        return ranked
    return [i for i in ranked if time_key(i["name"]) in keys] + \
           [i for i in ranked if time_key(i["name"]) not in keys]


def account_ranking(items: list, acct: Account, prefer: list, fallback_any: bool, won_keys: list) -> list:
    """账号自己的 prefer 排在前面，后面接顶层 prefer 的排序，自己的时段都没了就自然轮到顶层的时段。"""
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
    """item 是不是落在账号自己 prefer 的时间段里（同一时间段里没写进 prefer 的场也算；strict 账号只算点名的场）。"""
    if acct.strict:
        return matches(item, acct.prefer)
    own_keys = {time_key(i["name"]) for i in items
                for p in acct.prefer if all(t in str(i.get("name", "")) for t in p.split())}
    return time_key(item["name"]) in own_keys


def first_time_group(ranked: list, limit: int | None = None) -> list:
    """只取排在最前的那个时间段的场地，最多 limit 个（None 表示不限）。"""
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
    """估计 服务器时间 - 本机时间，返回 (估计值, 误差)。

    Date 头只精确到秒：某次请求在本机时间 [t0, t1] 内发出并收到、Date 为 sec，就有
    sec - t1 <= 偏差 < sec + 1 - t0。连续请求几秒、跨过几次跳秒后把这些区间取交集，宽度接近一次往返时间。
    用很轻的 HEAD /（约 60 毫秒）而不是 load_service（0.3~1 秒），区间才窄得下来。
    """
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
            sec = email.utils.parsedate_to_datetime(resp.getheader("Date") or "").timestamp()
            lo, hi = max(lo, sec - t1), min(hi, sec + 1 - t0)
            # 随机错开一点再发，总有某次请求贴着跳秒的时刻，区间才收得紧
            time.sleep(random.uniform(0, 0.03))
    finally:
        conn.close()
    if lo == -math.inf:
        raise ValueError("没有拿到服务器时间")
    return (lo + hi) / 2, abs(hi - lo) / 2


def reload_sid(acct: Account) -> bool:
    """抓包脚本或 set_session.sh 把新的登录 ID 写进 config.json 后，读进正在运行的脚本。"""
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
    """登录态失效后不退出，等 config.json 里出现能用的新 ID；到 deadline 还没有就返回 False。"""
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
    """检查每个账号的登录态。失效的账号只提示、不阻塞，其他账号照常等待；新 ID 写进 config.json 后自动恢复。"""
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
    """长时间等待时定期检查登录态，并保证在 until 前 5 分钟左右再查一次，失效了还来得及重抓。"""
    expired = set()
    while (left := until - time.time()) > 0:
        step = min(left, 60 if expired else cfg.get("heartbeat_seconds", 600))
        if left - step < 300 < left:
            step = left - 300
        time.sleep(step)
        if until - time.time() <= 0:
            return
        check_sessions(apis, expired)


BUSY_CODE = "1404"  # 当前排队人数较多（服务器限流），或者还没开放：时段本身仍可抢，直接重试
WEEKDAY_NAMES = "一二三四五六日"
KNOWN_SLOTS_PATH = Path(__file__).with_name("reserve_configs.json")


def known_slots(date_value: str) -> list:
    """reserve_configs.json 里按星期列出的时段 id。放场前服务器不返回时段，有了它就能在放场瞬间直接提交。"""
    try:
        configs = json.loads(KNOWN_SLOTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    weekday = "周" + WEEKDAY_NAMES[dt.date.fromisoformat(date_value).weekday()]
    for conf in configs:
        if weekday in (conf.get("rule_value") or []):
            return [{"id": i["id"], "name": i["name"], "su_count": i.get("count"),
                     "b_close": i.get("b_close", False)} for i in conf.get("items") or []]
    return []


def submit(api: Api, form: dict, date_value: str, item: dict) -> tuple:
    """提交一个时段，返回 (服务器的 code, 响应)；网络错误或非 JSON 响应返回 ("neterr", {})。"""
    record = {**form, "wx_reserve_date": date_value, "wx_reserve_name": item["name"], "wx_reserve_id": item["id"]}
    t0 = time.time()
    try:
        _, resp = api.create_record(record)
    except (OSError, http.client.HTTPException, ValueError) as e:
        # 放场高峰时服务器可能返回 500/502 页面或断开连接
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


def time_key(name: str) -> str:
    """时段名里从第一个时间开始的部分，例如 "2号场19:30~20:30" -> "19:30~20:30"，用来判断是不是同一时间段。"""
    m = re.search(r"\d{1,2}[:：]\d{2}.*", str(name))
    return m.group(0) if m else str(name)


class Board:
    """最新的场地列表，所有账号共用，后台刷新和 1433 的响应都会更新它。

    几个响应可能同时在路上，旧列表可能比新列表晚到；见过约满的场就一直当作约满，
    免得旧列表把它又"放"出来，白白浪费一次进门的机会。"""

    def __init__(self):
        self.cond = threading.Condition()
        self.items = None  # None 表示还没拿到开放后的列表
        self.full = set()
        self.version = 0

    def update(self, items: list) -> None:
        if not items:
            return
        with self.cond:
            self.full |= {i["id"] for i in items if i.get("b_close") or slot_left(i) == 0}
            self.items = [dict(i, su_count=0) if i["id"] in self.full else i for i in items]
            self.version += 1
            self.cond.notify_all()

    def mark_full(self, item_id: str) -> None:
        with self.cond:
            self.full.add(item_id)

    def snapshot(self) -> tuple:
        with self.cond:
            return self.items, self.version, set(self.full)

    def wait_change(self, version: int, timeout: float) -> None:
        with self.cond:
            self.cond.wait_for(lambda: self.version != version, timeout)


class Coordinator:
    """多个账号之间的分工：同一片场同一时刻只让一个账号提交，免得自己的账号互相抢；
    记下各账号抢到的场，让还没抢到的账号优先抢同一时间段。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.claims = {}  # 时段 id -> 正在提交它或已经抢到它的账号名
        self.won = {}  # 账号名 -> 抢到的时段 id
        self.shared_won = []  # 按顶层 prefer 抢的账号抢到的时段，用来把其他按顶层 prefer 抢的账号拉到同一时间段

    def try_claim(self, who: str, item: dict) -> bool:
        with self.lock:
            return self.claims.setdefault(item["id"], who) == who

    def release(self, who: str, ids=None) -> None:
        """放开 who 占着的场（ids 为 None 时全部放开），抢到的场一直占着。"""
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

    def taken_by_others(self, who: str) -> set:
        with self.lock:
            return {sid for sid, owner in self.claims.items() if owner != who}


def sleep_till(t: float) -> None:
    if (left := t - time.time()) > 0:
        time.sleep(left)


class AccountGrab:
    """一个账号的抢场。

    - 开 len(apis) 个连接，每个连接各自循环提交。1404（排队人数多，或者还没开放）和网络错误
      不重新拉列表，立刻在这个连接上重发，不等其他连接的结果；
    - 同时在提交的不同场地最多 parallel 个，多出来的连接重复提交这几个场。一个场只能约一次，
      重复提交不会多约，只是多了几次进门的机会；
    - 只提交排在最前的那个时间段的场地，这个时间段的场都没了才换下一个时间段；
    - 1433（已满）或其他失败：这个场不再提交，换同一时间段的下一个场；1433 的响应里带着最新列表，直接拿来更新；
    - 放场前服务器不返回时段，blind（reserve_configs.json 里的已知 id）非空时先按它提交，
      拿到开放后的列表就改按列表提交；
    - 多个账号时通过 coord 分场：其他账号正在提交或已经抢到的场不碰，
      按顶层 prefer 抢的账号之间，有人抢到后其他人优先抢同一时间段；
    - 账号有自己的 prefer 时先抢自己的时段，都没了再按顶层 prefer 抢。
    """

    def __init__(self, acct: Account, apis: list, cfg: dict, date_value: str, form: dict, blind: list,
                 coord: Coordinator, board: Board, parallel: int, deadline: float, tags: list):
        self.acct = acct
        self.apis = apis
        self.date_value = date_value
        self.form = form
        self.blind = blind
        self.coord = coord
        self.board = board
        self.deadline = deadline
        self.tags = tags
        self.lanes_max = max(1, min(parallel, len(apis)))
        self.prefer = cfg.get("prefer", [])
        self.fallback = cfg.get("fallback_any", False)
        self.retry_interval = cfg.get("retry_interval_seconds", 0.03)
        self.slow_reply = cfg.get("slow_reply_seconds", 0.3)
        self.lock = threading.Lock()
        self.relogin_lock = threading.Lock()
        self.lanes = []  # 正在提交的不同场地
        self.pending = {}  # 请求编号 -> (时段 id, 发出时间)，还没回来的请求
        self.tokens = count()
        self.failed = set()  # 这个账号提交失败过（非 1404）的时段 id，不再提交
        self.wins = []
        self.fatal = ""
        self.done = threading.Event()
        self.waiting = ""  # 没有场可提交的原因："others" 其他账号占着，"slow" 等迟迟不回的请求
        self.noted_at = 0.0

    def items(self) -> list:
        items, _, full = self.board.snapshot()
        dead = full | self.failed
        return [dict(i, su_count=0) if i["id"] in dead else i for i in items or self.blind]

    def ranking(self, items: list) -> list:
        ranked = account_ranking(items, self.acct, self.prefer, self.fallback, self.coord.won_times())
        if self.acct.strict:
            # 其他账号正在提交或已经抢到的点名场算有人管了，不等它们的结果：
            # 先换还没人管的点名场，都有人管了就按顶层 prefer 的顺序抢别的，不碰点名场所在时间段里顺带排上的场
            taken = self.coord.taken_by_others(self.acct.name)
            own = [i for i in ranked if matches(i, self.acct.prefer)]
            free = [i for i in own if i["id"] not in taken]
            if free:
                return first_time_group(free)
            if own:
                return first_time_group([i for i in ranked if not matches(i, self.acct.prefer)])
        return first_time_group(ranked)

    def take_lane(self, k: int) -> tuple:
        """给第 k 个连接挑一个场并记为在途，返回 (场, 请求编号)；没有能提交的场返回 (None, None)。"""
        with self.lock:
            ranked = self.ranking(self.items())
            ids = {i["id"] for i in ranked}
            stale = {lane["id"] for lane in self.lanes if lane["id"] not in ids}
            if stale:
                self.lanes = [lane for lane in self.lanes if lane["id"] not in stale]
                self.coord.release(self.acct.name, stale)
            # 换场时旧场的请求可能还在路上，也算一个名额，保证同一时刻在提交的不同场不超过 parallel 个
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
                # 排队被拒一般 0.2~0.24 秒就回来，进了门的请求要处理 0.8~1.2 秒。有请求迟迟不回，多半已经进门：
                # 这时只重复提交它那个场（一个场只能约一次），先不碰别的场，免得两个场都约上。
                # 猜错了也只是多重复提交几次同一个场，所以门槛宁低勿高
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

    def handle(self, item: dict, code: str, resp: dict) -> bool:
        """记下一次提交的结果；返回 True 表示这个连接不用再提交了。"""
        name = self.acct.name
        if code == "1200":
            with self.lock:
                self.wins.append(item)
                n_wins = len(self.wins)
                self.done.set()
                shared = not in_own_times(self.acct, item, self.items())
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
            self.done.set()
            return True
        # 1433（已满）或其他失败（多半是时段已被抢走）：这个场不再提交
        if code == "1433":
            self.board.mark_full(item["id"])
        self.board.update((target_day(resp.get("data") or {}, self.date_value) or {}).get("items") or [])
        with self.lock:
            self.failed.add(item["id"])
            self.lanes = [lane for lane in self.lanes if lane["id"] != item["id"]]
        self.coord.release(name, {item["id"]})
        return False

    def relogin(self, api: Api, bad_sid: str) -> None:
        """登录态失效：只让一个连接等新 ID，其他连接等它的结果。"""
        with self.relogin_lock:
            if self.done.is_set() or self.acct.sid != bad_sid:
                return  # 别的连接已经等到新 ID 了
            with self.lock:
                self.lanes = []
            self.coord.release(self.acct.name)
            if not wait_for_new_session(api, self.deadline):
                self.done.set()

    def idle(self, k: int) -> None:
        """没有能提交的场：等列表更新，或者等其他账号放出场地。"""
        items, version, _ = self.board.snapshot()
        if k == 0 and items and self.waiting != "slow" and time.time() - self.noted_at >= 5:
            self.noted_at = time.time()
            if self.waiting == "others":
                log("剩下的场其他账号正在提交，等它们的结果")
            else:
                log(f"{self.date_value} 暂无符合偏好且有余量的时段，继续轮询：\n" + describe(self.items()))
        wait = 0.02 if self.waiting == "slow" else 0.1
        self.board.wait_change(version, min(wait, max(0.0, self.deadline - time.time())))

    def worker(self, k: int, warm_at: float, fire_at: float) -> None:
        api = self.apis[k]
        sleep_till(warm_at)
        api.connect()
        sleep_till(fire_at)
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
                time.sleep(self.retry_interval)

    def run_conn(self, k: int, warm_at: float, fire_at: float) -> None:
        _ctx.who = self.tags[k]
        try:
            self.worker(k, warm_at, fire_at)
        except Exception as e:  # noqa: BLE001 一个连接出错不能拖垮其他连接
            log(f"异常退出：{e!r}")


def refresher(board: Board, apis: list, cfg: dict, date_value: str, warm_at: float, start_at: float,
              deadline: float, stop: threading.Event) -> None:
    """单独用一个连接拉场地列表，不占提交连接：未开放时快速轮询，开放后每 refresh_seconds 拉一次。
    apis 是每个账号各一个，当前账号登录态失效就换下一个账号来拉。"""
    _ctx.who = "列表"
    interval = cfg.get("poll_interval_seconds", 0.2)
    fast_window = cfg.get("fast_poll_window_seconds", 10)
    refresh_seconds = cfg.get("refresh_seconds", 2)
    if stop.wait(max(0.0, warm_at - time.time())):
        return
    apis[0].connect()
    if stop.wait(max(0.0, start_at - time.time())):
        return
    polls = turn = 0
    while not stop.is_set() and time.time() < deadline:
        api = apis[turn % len(apis)]
        # 放场前后几秒内不空等，开放后第一时间看到
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
            if polls % 10 == 1:
                log("仍未开放，继续轮询")
            stop.wait(0.02 if fast else interval)
            continue
        board.update((target_day(data, date_value) or {}).get("items") or [])
        stop.wait(refresh_seconds)


def prepare(main_apis: dict, forms: dict, cfg: dict, host: str, https: bool) -> float:
    """开抢前约 30 秒：校时，同时刷新每个账号的表单、确认登录态，几件事并行做。返回校时结果。"""
    offset = [0.0]

    def sync():
        try:
            value, err = server_offset(host, https, cfg.get("timeout_seconds", 5))
        except (OSError, http.client.HTTPException, ValueError) as e:
            log(f"校时出错，按本机时间开抢：{e!r}")
            return
        offset[0] = value
        log(f"服务器时间比本机{'快' if value >= 0 else '慢'} {abs(value):.3f} 秒（误差约 ±{err:.3f} 秒）")

    def refresh(acct, api):
        reload_sid(acct)
        try:
            _, payload = api.load_service()
            forms[acct] = build_form(acct, payload["data"])
        except SessionExpired:
            session_expired_banner(acct)  # 开抢后提交时会继续等新 ID
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


def run(accounts: list, cfg: dict, host: str, https: bool, start_now: bool) -> int:
    now = dt.datetime.now(TZ)
    open_time = cfg.get("open_time") or ""
    open_day = now.date()
    if open_time and not start_now:
        hh, mm, ss = (int(x) for x in open_time.split(":"))
        open_dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        # 今天的放场时间已过去 5 分钟以上，就等明天这个时间（比如前一天晚上启动）
        if now - open_dt > dt.timedelta(minutes=5):
            open_dt += dt.timedelta(days=1)
        # open_weekdays 用 1=周一 … 7=周日；为空表示每天都放场
        weekdays = set(cfg.get("open_weekdays") or range(1, 8))
        while open_dt.isoweekday() not in weekdays:
            open_dt += dt.timedelta(days=1)
        open_day = open_dt.date()
        open_at = open_dt.timestamp()
    else:
        start_now = True
    date_value = resolve_date(cfg.get("reserve_date"), open_day)

    # 每个账号同时提交的不同场地数，最多 2 个，防止一个号约到太多场
    parallel = max(1, min(int(cfg.get("parallel", 2)), 2))
    # 每个账号开几个连接；最多 4 个，请求太密容易被服务器限流或封号
    connections = max(1, min(int(cfg.get("connections", 3)), 4))
    main_apis, forms, expired, title = {}, {}, [], ""
    for acct in accounts:
        api = Api(cfg, acct, host, https)
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
    log(f"登录态有效。{title}，目标日期 {date_value}，开始时间 {when}")
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
        lead = cfg.get("blind_lead_seconds", 0.05) if blind else cfg.get("lead_seconds", 0.3)
        fire_at = open_at - offset - lead
    else:
        blind = []
        fire_at = time.time()
    # 服务器约 15 秒关掉空闲连接，这些连接撑不到开抢；提交和拉列表都用开抢前几秒新建的连接
    for api in main_apis.values():
        api.close()
    warm_at = fire_at - cfg.get("warm_seconds", 2)
    # 各连接依次错开一点发出，铺满放场时刻前后，抵消校时误差；早到的会收到 1404 并立刻重发
    stagger = cfg.get("stagger_seconds", 0.025) if blind else 0
    poll_seconds = cfg.get("poll_seconds", 60)
    deadline = fire_at + poll_seconds if poll_seconds > 0 else float("inf")

    many = len(main_apis) > 1
    board, coord = Board(), Coordinator()
    grabs = []
    for acct in main_apis:
        tags = [(acct.name if many else "") + (f"#{k + 1}" if connections > 1 else "") for k in range(connections)]
        grabs.append(AccountGrab(acct, [Api(cfg, acct, host, https) for _ in range(connections)], cfg, date_value,
                                 forms[acct], blind, coord, board, parallel, deadline, tags))
    log(f"每个账号 {connections} 个连接，同时提交的不同场地最多 {parallel} 个"
        + (f"，开抢时各连接依次错开 {stagger * 1000:.0f} 毫秒" if stagger and connections > 1 else ""))
    for g in grabs:
        group = g.ranking(g.items()) if blind else []
        if group:
            log(f"{f'[{g.acct.name}] ' if many else ''}已知时段 id，放场瞬间直接提交：" + "、".join(i["name"] for i in group))

    stop = threading.Event()
    list_thread = threading.Thread(target=refresher, daemon=True, args=(
        board, [Api(cfg, acct, host, https) for acct in main_apis], cfg, date_value, warm_at, fire_at, deadline, stop))
    threads = [threading.Thread(target=g.run_conn, args=(k, warm_at, fire_at + k * stagger), name=f"{g.acct.name}#{k + 1}")
               for g in grabs for k in range(connections)]
    list_thread.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    list_thread.join(timeout=1)

    for g in grabs:
        prefix = f"[{g.acct.name}] " if many else ""
        if g.fatal:
            log(prefix + g.fatal)
        elif not g.wins:
            log(prefix + "轮询时间用完，没有抢到")
    if many:
        log("抢场结果：")
        for g in grabs:
            log(f"  {g.acct.name}：{'、'.join(i['name'] for i in g.wins) or '没抢到'}")
    return 0 if any(g.wins for g in grabs) else 1

def check(accounts: list, cfg: dict, host: str, https: bool) -> int:
    rc, shown_slots = 0, False
    for acct in accounts:
        print(f"===== 账号：{acct.name} =====")
        api = Api(cfg, acct, host, https)
        try:
            _, payload = api.load_service()
        except SessionExpired as e:
            print(f"登录态：失效（{e}），请{acct.refresh_hint()}")
            rc = max(rc, 2)
            continue
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
            known = {i["name"]: i["id"] for i in known_slots(date_value)}
            live = {i.get("name"): i.get("id") for i in day.get("items") or []}
            if known and live:
                diff = [name for name, sid in live.items() if known.get(name) != sid]
                if diff:
                    print(f"reserve_configs.json 里的时段 id 与服务器不一致：{'、'.join(diff)}（直接提交会失败后回退，建议把 blind_submit 设为 false）")
                else:
                    print("reserve_configs.json 里的时段 id 与服务器一致，放场瞬间可以直接提交")
        try:
            build_form(acct, data)
            print("表单检查：通过")
        except SystemExit as e:
            print(f"表单检查：{e}")
            rc = max(rc, 1)
    return rc


def server_name(data: dict) -> str:
    """服务器预填表单里的姓名，也就是这个微信账号上次预约时填的姓名。"""
    init = data.get("init_service_record") or {}
    for field in data.get("fields") or []:
        if norm_title(field.get("title")) == "姓名":
            return str(init.get(field["col_name"]) or "").strip()
    return ""


def set_session(sid: str, name: str | None, host: str, https: bool) -> str:
    """把登录 ID 写进 accounts，返回账号名。有 name 就更新（没有这个账号就新增），没有就按服务器里的姓名新增。"""
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
        probe = Account("", sid, {}, len(entries))  # 空名字不会匹配到任何已有账号，换发的新 ID 不会写错地方
        try:
            _, payload = Api(data, probe, host, https).load_service()
        except SessionExpired:
            raise SystemExit("这个登录 ID 无效或已过期，没有写入") from None
        sid = probe.sid  # 服务器可能当场换发新 ID
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
    ap.add_argument("--now", action="store_true", help="不等放场时间，立即轮询并提交")
    ap.add_argument("--account", help="只用这个账号（只写了登录 ID 的账号名字是 账号1、账号2……）")
    ap.add_argument("--set-session", metavar="ID", help="新增账号，或配合 --name 更新这个账号的登录 ID，然后检查它")
    ap.add_argument("--name", help="和 --set-session 一起用：要新增或更新的账号名")
    ap.add_argument("--host", default="d2.xksyun.com", help=argparse.SUPPRESS)
    ap.add_argument("--plain-http", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    https = not args.plain_http
    if args.set_session:
        name = set_session(args.set_session.strip(), args.name, args.host, https)
        cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        return check(load_accounts(cfg, name), cfg, args.host, https)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    accounts = load_accounts(cfg, args.account)
    try:
        return check(accounts, cfg, args.host, https) if args.check else run(accounts, cfg, args.host, https, args.now)
    except SessionExpired as e:
        log(f"登录态失效（{e}）。请在微信里重新打开小程序，再重新抓一次 wx_session_user_id")
        return 2


if __name__ == "__main__":
    sys.exit(main())
