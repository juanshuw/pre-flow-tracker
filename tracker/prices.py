"""价格与成交：日线（Yahoo 主 + Stooq/Nasdaq 补缺）、1 分钟线日内统计、同行日线。"""
import datetime as dt, io
import numpy as np, pandas as pd
from .common import DATA, SYMBOL, PEERS, HISTORY_START, get, read_csv, upsert, UA_WEB


def _yf_daily(sym, start):
    import yfinance as yf
    h = yf.download(sym, start=start, auto_adjust=False, progress=False, threads=False)
    if h is None or len(h) == 0: return pd.DataFrame()
    if isinstance(h.columns, pd.MultiIndex): h.columns = h.columns.get_level_values(0)
    h = h[["Open", "High", "Low", "Close", "Volume"]].copy(); h.index = pd.to_datetime(h.index).strftime("%Y-%m-%d")
    h = h.reset_index().rename(columns={"index": "date", "Date": "date"}); h.columns = [c.lower() for c in h.columns]
    h["source"] = "yahoo"; return h


def _stooq_daily(sym):
    r = get(f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d")
    if not r or "Date" not in r.text[:50]: return pd.DataFrame()
    h = pd.read_csv(io.StringIO(r.text)); h.columns = [c.lower() for c in h.columns]
    h = h.rename(columns={"date": "date"}); h["source"] = "stooq"; return h[["date", "open", "high", "low", "close", "volume", "source"]]


def _nasdaq_daily(sym, start):
    url = f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass=stocks&fromdate={start}&limit=9999&todate={dt.date.today()}"
    r = get(url, headers={"Accept": "application/json", "Origin": "https://www.nasdaq.com"})
    if not r: return pd.DataFrame()
    try: rows = r.json()["data"]["tradesTable"]["rows"]
    except Exception: return pd.DataFrame()
    out = []
    for x in rows:
        try:
            out.append(dict(date=pd.to_datetime(x["date"]).strftime("%Y-%m-%d"), open=float(x["open"].replace("$", "").replace(",", "")), high=float(x["high"].replace("$", "").replace(",", "")),
                            low=float(x["low"].replace("$", "").replace(",", "")), close=float(x["close"].replace("$", "").replace(",", "")), volume=int(x["volume"].replace(",", "").replace("N/A", "0") or 0), source="nasdaq"))
        except Exception: continue
    return pd.DataFrame(out)


def update_daily(status):
    """主：Yahoo；缺日用 Stooq 再用 Nasdaq 补。写 data/prices_daily.csv。"""
    frames = []
    try:
        y = _yf_daily(SYMBOL, HISTORY_START); frames.append(y); status.ok("prices.yahoo", f"{len(y)} rows")
    except Exception as e: status.fail("prices.yahoo", e)
    try:
        s = _stooq_daily(SYMBOL)
        if len(s): s = s[s.date >= HISTORY_START]; frames.append(s); status.ok("prices.stooq", f"{len(s)} rows")
        else: status.skip("prices.stooq", "empty/blocked")
    except Exception as e: status.fail("prices.stooq", e)
    try:
        n = _nasdaq_daily(SYMBOL, HISTORY_START); frames.append(n); status.ok("prices.nasdaq", f"{len(n)} rows") if len(n) else status.skip("prices.nasdaq", "empty/blocked")
    except Exception as e: status.fail("prices.nasdaq", e)
    frames = [f for f in frames if f is not None and len(f)]
    if not frames: return read_csv(DATA / "prices_daily.csv")
    # 优先级 yahoo > stooq > nasdaq：按日期取第一个来源
    allp = pd.concat(frames, ignore_index=True)
    allp["prio"] = allp.source.map({"yahoo": 0, "stooq": 1, "nasdaq": 2})
    allp = allp.sort_values(["date", "prio"]).drop_duplicates("date", keep="first").drop(columns="prio")
    allp = allp[allp.volume > 0]
    return upsert(DATA / "prices_daily.csv", allp, ["date"])


def update_peers(status):
    try:
        import yfinance as yf
        h = yf.download(PEERS, start=HISTORY_START, auto_adjust=False, progress=False, threads=False)["Close"]
        h.index = pd.to_datetime(h.index).strftime("%Y-%m-%d"); h = h.reset_index().rename(columns={"index": "date", "Date": "date"})
        out = upsert(DATA / "peers_daily.csv", h, ["date"]); status.ok("prices.peers", f"{len(out)} rows"); return out
    except Exception as e:
        status.fail("prices.peers", e); return read_csv(DATA / "peers_daily.csv")


def update_intraday(status, daily):
    """Yahoo 1 分钟线只回溯 7 天：每天跑一次就能把历史攒下来。统计：VWAP、收盘/VWAP、开盘与尾盘 30 分钟占比、最大 1 分钟成交、有成交的分钟数。"""
    try:
        import yfinance as yf
        i = yf.Ticker(SYMBOL).history(period="7d", interval="1m", auto_adjust=False)
        if i is None or len(i) == 0: status.skip("prices.intraday_1m", "empty"); return read_csv(DATA / "intraday_stats.csv")
        i.index = pd.to_datetime(i.index, utc=True).tz_convert("America/New_York"); i = i[i.Volume > 0]
        close_map = dict(zip(daily.date, daily.close)) if len(daily) else {}
        rows = []
        for day, g in i.groupby(i.index.date):
            vol = float(g.Volume.sum()); vwap = float((g.Close * g.Volume).sum()) / vol
            d = day.strftime("%Y-%m-%d"); close = close_map.get(d, float(g.Close.iloc[-1]))
            rows.append(dict(date=d, vwap=round(vwap, 3), close_vs_vwap_pct=round((close / vwap - 1) * 100, 2),
                             first30_pct=round(float(g[g.index.hour == 9].Volume.sum()) / vol * 100, 1),
                             last30_pct=round(float(g[(g.index.hour == 15) & (g.index.minute >= 30)].Volume.sum()) / vol * 100, 1),
                             last60_pct=round(float(g[g.index.hour == 15].Volume.sum()) / vol * 100, 1),
                             max_1m_vol=int(g.Volume.max()), max_1m_time=g.Volume.idxmax().strftime("%H:%M"), minutes_traded=int(len(g)), vol_1m_sum=int(vol)))
        out = upsert(DATA / "intraday_stats.csv", rows, ["date"]); status.ok("prices.intraday_1m", f"{len(rows)} days"); return out
    except Exception as e:
        status.fail("prices.intraday_1m", e); return read_csv(DATA / "intraday_stats.csv")


def enrich(daily, intraday):
    """派生指标：涨跌、换手、20 日均量倍数、收盘位置、均线、RSI、布林 %B、派发/吸筹日。"""
    from .common import SHARES_A
    d = daily.copy().sort_values("date").reset_index(drop=True)
    d["ret_pct"] = (d.close.pct_change() * 100).round(2)
    d["turnover_pct"] = (d.volume / SHARES_A * 100).round(2)
    d["adv20"] = d.volume.rolling(20).mean().shift(1)
    d["vol_x_adv20"] = (d.volume / d.adv20).round(2)
    rng = (d.high - d.low).replace(0, np.nan)
    d["close_pos"] = ((d.close - d.low) / rng).round(2)
    d["gap_pct"] = ((d.open / d.close.shift(1) - 1) * 100).round(2)
    d["intraday_pct"] = ((d.close / d.open - 1) * 100).round(2)
    for n in (20, 50, 200): d[f"ma{n}"] = d.close.rolling(n).mean().round(2)
    delta = d.close.diff(); up = delta.clip(lower=0); dn = -delta.clip(upper=0)
    rs = up.ewm(alpha=1 / 14, adjust=False).mean() / dn.ewm(alpha=1 / 14, adjust=False).mean(); d["rsi14"] = (100 - 100 / (1 + rs)).round(1)
    sd = d.close.rolling(20).std(); d["pct_b"] = ((d.close - (d.ma20 - 2 * sd)) / (4 * sd)).round(2)
    d["dist_ma50_pct"] = ((d.close / d.ma50 - 1) * 100).round(1)
    d["dist_day"] = ((d.close < d.close.shift(1)) & (d.volume > d.volume.shift(1))).astype(int)
    d["acc_day"] = ((d.close > d.close.shift(1)) & (d.volume > d.volume.shift(1))).astype(int)
    if len(intraday): d = d.merge(intraday, on="date", how="left")
    return d
