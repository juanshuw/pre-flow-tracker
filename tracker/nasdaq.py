"""Nasdaq Trader 公开文件（卖空熔断名单、Reg SHO 门槛名单）与 api.nasdaq.com（机构持仓、内部人、空头）。api.nasdaq.com 在云端可能被挡，全部 best-effort。"""
import datetime as dt, json, re, time
import pandas as pd
from .common import DATA, SYMBOL, get, read_csv, upsert, trading_days, last_trading_day, load_json, save_json

NQ_H = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


def update_circuit_breaker(status):
    """每日卖空熔断（Rule 201，盘中跌 ≥10% 触发，当日余下 + 次日生效）。"""
    try:
        st = load_json(DATA / "nasdaq_cb_checked.json", {"checked": []}); checked = set(st["checked"])
        start = dt.date(2026, 1, 2)
        rows = []; newly = []; misses = 0
        for d in reversed(trading_days(start, last_trading_day())):     # 从最新往回：历史文件可能已被 Nasdaq 清理，连续缺 8 天即停
            ds = d.strftime("%Y-%m-%d")
            if ds in checked: continue
            r = get(f"https://www.nasdaqtrader.com/dynamic/symdir/shorthalts/shorthalts{d.strftime('%Y%m%d')}.txt", ua="Mozilla/5.0", headers={"Accept": "text/plain"}, retries=1); time.sleep(0.6)   # nasdaqtrader 会限速；浏览器 UA 会拿到 HTML 跳转页
            if not r or not r.text.lstrip().startswith("Symbol"):
                misses += 1
                if misses >= 8: break        # 连续拿不到就停，下次再补
                continue
            misses = 0; trig = ""
            for line in r.text.splitlines():
                p = line.split(",")
                if p and p[0] == SYMBOL and len(p) >= 4: trig = p[-1].strip()
            tdate = ""
            m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", trig)
            if m: tdate = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
            rows.append(dict(date=ds, in_effect=int(bool(trig)), triggered_today=int(tdate == ds), trigger_time=trig, trigger_date=tdate, list_size=max(0, len(r.text.splitlines()) - 2))); newly.append(ds)
        if rows:
            upsert(DATA / "nasdaq_circuit_breaker.csv", rows, ["date"]); checked |= set(newly); save_json(DATA / "nasdaq_cb_checked.json", {"checked": sorted(checked)})
            status.ok("nasdaq.circuit_breaker", f"+{len(rows)} days; in effect {[r['date'] for r in rows if r['in_effect']]}")
        else: status.ok("nasdaq.circuit_breaker", "up to date")
        return read_csv(DATA / "nasdaq_circuit_breaker.csv")
    except Exception as e:
        status.fail("nasdaq.circuit_breaker", e); return read_csv(DATA / "nasdaq_circuit_breaker.csv")


def update_threshold(status):
    """Reg SHO 门槛证券名单（连续 5 日 FTD ≥ 0.5% 流通股）。"""
    try:
        st = load_json(DATA / "nasdaq_th_checked.json", {"checked": []}); checked = set(st["checked"])
        rows = []; newly = []; misses = 0
        for d in reversed(trading_days(dt.date(2026, 1, 2), last_trading_day())):
            ds = d.strftime("%Y-%m-%d")
            if ds in checked: continue
            r = get(f"https://www.nasdaqtrader.com/dynamic/symdir/regsho/nasdaqth{d.strftime('%Y%m%d')}.txt", ua="Mozilla/5.0", headers={"Accept": "text/plain"}, retries=1); time.sleep(0.6)
            if not r or not r.text.lstrip().startswith("Symbol"):
                misses += 1
                if misses >= 8: break
                continue
            misses = 0
            on = any(line.split("|")[0] == SYMBOL for line in r.text.splitlines())
            rows.append(dict(date=ds, on_threshold_list=int(on))); newly.append(ds)
        if rows:
            upsert(DATA / "nasdaq_threshold.csv", rows, ["date"]); checked |= set(newly); save_json(DATA / "nasdaq_th_checked.json", {"checked": sorted(checked)})
            status.ok("nasdaq.threshold", f"+{len(rows)} days; on list {[r['date'] for r in rows if r['on_threshold_list']]}")
        else: status.ok("nasdaq.threshold", "up to date")
        return read_csv(DATA / "nasdaq_threshold.csv")
    except Exception as e:
        status.fail("nasdaq.threshold", e); return read_csv(DATA / "nasdaq_threshold.csv")


def update_institutional(status):
    try:
        r = get(f"https://api.nasdaq.com/api/company/{SYMBOL}/institutional-holdings?limit=60&type=TOTAL&sortColumn=marketValue", headers=NQ_H, retries=2)
        if not r: status.skip("nasdaq.institutional", "blocked/empty"); return load_json(DATA / "nasdaq_institutional_latest.json", {})
        j = r.json().get("data") or {}
        snap = {"date": dt.date.today().isoformat(), "summary": {k: v.get("value") for k, v in (j.get("ownershipSummary") or {}).items()},
                "active_positions": [{"position": x.get("positions"), "holders": x.get("holders"), "shares": x.get("shares")} for x in (j.get("activePositions") or {}).get("rows") or []],
                "new_sold_out": [{"position": x.get("positions"), "holders": x.get("holders"), "shares": x.get("shares")} for x in (j.get("newSoldOutPositions") or {}).get("rows") or []],
                "holders": [{k: x.get(k) for k in ("ownerName", "date", "sharesHeld", "sharesChange", "sharesChangePCT", "marketValue")} for x in (j.get("holdingsTransactions") or {}).get("table", {}).get("rows") or []]}
        save_json(DATA / "nasdaq_institutional_latest.json", snap)
        hist = load_json(DATA / "nasdaq_institutional_history.json", []); hist = [h for h in hist if h.get("date") != snap["date"]] + [{"date": snap["date"], "summary": snap["summary"], "active_positions": snap["active_positions"]}]
        save_json(DATA / "nasdaq_institutional_history.json", hist[-400:])
        status.ok("nasdaq.institutional", f"inst own {snap['summary'].get('SharesOutstandingPCT')}; holders {len(snap['holders'])}"); return snap
    except Exception as e:
        status.fail("nasdaq.institutional", e); return load_json(DATA / "nasdaq_institutional_latest.json", {})


def update_insiders(status):
    try:
        r = get(f"https://api.nasdaq.com/api/company/{SYMBOL}/insider-trades?limit=100&type=ALL&sortColumn=lastDate&sortOrder=DESC", headers=NQ_H, retries=2)
        if not r: status.skip("nasdaq.insiders", "blocked/empty"); return read_csv(DATA / "nasdaq_insiders.csv")
        j = r.json().get("data") or {}
        rows = [{k: x.get(k) for k in ("insider", "relation", "lastDate", "transactionType", "ownType", "sharesTraded", "lastPrice", "sharesHeld", "url")} for x in (j.get("transactionTable") or {}).get("table", {}).get("rows") or []]
        if rows:
            out = upsert(DATA / "nasdaq_insiders.csv", rows, ["insider", "lastDate", "transactionType", "sharesTraded"], sort=["lastDate"]); status.ok("nasdaq.insiders", f"{len(rows)} rows"); return out
        status.skip("nasdaq.insiders", "no rows"); return read_csv(DATA / "nasdaq_insiders.csv")
    except Exception as e:
        status.fail("nasdaq.insiders", e); return read_csv(DATA / "nasdaq_insiders.csv")


def update_short_interest(status):
    try:
        r = get(f"https://api.nasdaq.com/api/quote/{SYMBOL}/short-interest?assetclass=stocks", headers=NQ_H, retries=2)
        if not r: status.skip("nasdaq.short_interest", "blocked/empty"); return read_csv(DATA / "nasdaq_short_interest.csv")
        j = r.json().get("data") or {}
        rows = [{k: x.get(k) for k in ("settlementDate", "interest", "avgDailyShareVolume", "daysToCover")} for x in (j.get("shortInterestTable") or {}).get("rows") or []]
        if rows:
            out = upsert(DATA / "nasdaq_short_interest.csv", rows, ["settlementDate"]); status.ok("nasdaq.short_interest", f"{len(rows)} rows"); return out
        status.skip("nasdaq.short_interest", "no rows"); return read_csv(DATA / "nasdaq_short_interest.csv")
    except Exception as e:
        status.fail("nasdaq.short_interest", e); return read_csv(DATA / "nasdaq_short_interest.csv")
