"""公共工具：路径、HTTP、CSV 增量合并、交易日历。"""
import datetime as dt, json, os, pathlib, time
import pandas as pd, requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"; DOCS = ROOT / "docs"; DOCS_DATA = DOCS / "data"
for p in (DATA, DOCS_DATA): p.mkdir(parents=True, exist_ok=True)

SYMBOL = os.environ.get("TRACK_SYMBOL", "PRE")
CIK = os.environ.get("TRACK_CIK", "0001876431")
CUSIP = os.environ.get("TRACK_CUSIP", "G72245122")
COMPANY_QUERY = os.environ.get("TRACK_COMPANY_QUERY", '"PRENETICS GLOBAL"')
SHARES_A = int(os.environ.get("TRACK_SHARES", "15356748"))      # A 类流通股（2026-08-05 F-1/A），换手率分母
PEERS = ["HIMS", "IWM", "XBI"]
HISTORY_START = "2025-01-01"

# SEC 要求 UA 里有可联系的身份；用环境变量覆盖（GitHub Secret SEC_UA）
# SEC 要求 UA 为 “名称 邮箱” 格式且域名像公司域（github noreply 会被拒 403）。建议在 GitHub Secrets 里设 SEC_UA 为真实联系方式覆盖默认值。
UA_SEC = os.environ.get("SEC_UA") or "PRE Flow Tracker admin@pre-flow-tracker.example.com"
UA_WEB = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

_session = requests.Session()


def get(url, ua=UA_WEB, headers=None, timeout=40, retries=3, sleep=1.5, ok=(200,), stream_cap=None, **kw):
    """带重试的 GET；返回 Response 或 None（非 ok 状态/异常都返回 None，由调用方决定如何处理）。"""
    h = {"User-Agent": ua, "Accept": "*/*"}
    if headers: h.update(headers)
    last = None
    for i in range(retries):
        try:
            r = _session.get(url, headers=h, timeout=timeout, stream=bool(stream_cap), **kw)
            if r.status_code in ok:
                if stream_cap:
                    buf = bytearray()
                    for chunk in r.iter_content(1 << 16):
                        buf += chunk
                        if len(buf) > stream_cap: break
                    r._content = bytes(buf)
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404): return None       # 不重试
        except Exception as e:
            last = repr(e)
        time.sleep(sleep * (i + 1))
    return None


def read_csv(path, **kw):
    path = pathlib.Path(path)
    if not path.exists() or path.stat().st_size == 0: return pd.DataFrame()
    return pd.read_csv(path, **kw)


def upsert(path, new, keys, sort=None):
    """把 new 合并进 CSV：按 keys 去重（新数据优先），排序后写回；返回合并后的 DataFrame。"""
    path = pathlib.Path(path)
    old = read_csv(path)
    new = pd.DataFrame(new)
    if new.empty and old.empty: return old
    allv = pd.concat([old, new], ignore_index=True) if not old.empty else new.copy()
    for k in keys: allv[k] = allv[k].astype(str)
    allv = allv.drop_duplicates(subset=keys, keep="last")
    allv = allv.sort_values(sort or keys).reset_index(drop=True)
    allv.to_csv(path, index=False)
    return allv


def save_json(path, obj):
    pathlib.Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def load_json(path, default=None):
    p = pathlib.Path(path)
    if not p.exists(): return default
    try: return json.loads(p.read_text())
    except Exception: return default


# ---- 交易日历（NYSE 2025–2027 假日，够用）----
_HOLIDAYS = {
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def trading_days(start, end):
    days = pd.bdate_range(start, end)
    return [d.date() for d in days if d.strftime("%Y-%m-%d") not in _HOLIDAYS]


def et_today():
    """美东今天（UTC-4/-5 粗略按 UTC-4 处理即可，用于判断“最近一个交易日”）。"""
    return (dt.datetime.utcnow() - dt.timedelta(hours=4)).date()


def last_trading_day(ref=None):
    ref = ref or et_today()
    days = trading_days(ref - dt.timedelta(days=10), ref)
    return days[-1]


class Status:
    """每个数据源的运行状态，汇总到 dashboard 的“数据源状态”面板。"""
    def __init__(self): self.rows = []
    def ok(self, source, note=""): self.rows.append({"source": source, "status": "ok", "note": note, "ts": dt.datetime.utcnow().isoformat(timespec="seconds")})
    def fail(self, source, note=""): self.rows.append({"source": source, "status": "fail", "note": str(note)[:300], "ts": dt.datetime.utcnow().isoformat(timespec="seconds")})
    def skip(self, source, note=""): self.rows.append({"source": source, "status": "skip", "note": note, "ts": dt.datetime.utcnow().isoformat(timespec="seconds")})
