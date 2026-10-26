"""FINRA：逐日卖空/场外成交（Reg SHO）、周度暗池与做市商（OTC Transparency）、双周空头持仓。
接口匿名可用（必须用 dateRangeFilters 过滤日期）；若设置 FINRA_CLIENT_ID/SECRET 则走 OAuth 提高配额。"""
import base64, csv, datetime as dt, io, json, os, time
import pandas as pd
from .common import DATA, SYMBOL, HISTORY_START, get, read_csv, upsert, UA_WEB

BASE = "https://api.finra.org/data/group/otcMarket/name/"
TOKEN_URL = "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token?grant_type=client_credentials"
_tok = {"t": None, "exp": 0}


def _token():
    cid, sec = os.environ.get("FINRA_CLIENT_ID"), os.environ.get("FINRA_CLIENT_SECRET")
    if not cid or not sec: return None
    if _tok["t"] and time.time() < _tok["exp"]: return _tok["t"]
    import requests
    r = requests.post(TOKEN_URL, headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(), "User-Agent": UA_WEB}, timeout=40)
    r.raise_for_status(); d = r.json(); _tok.update(t=d["access_token"], exp=time.time() + min(int(d.get("expires_in", 1800)), 1800) - 60)
    return _tok["t"]


def query(dataset, compare=None, date_range=None, limit=5000):
    import requests
    tok = _token(); out = []; offset = 0
    while True:
        body = {"limit": limit, "offset": offset}
        if compare: body["compareFilters"] = [{"compareType": "EQUAL", "fieldName": k, "fieldValue": v} for k, v in compare.items()]
        if date_range: body["dateRangeFilters"] = [{"fieldName": date_range[0], "startDate": date_range[1], "endDate": date_range[2]}]
        h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA_WEB}
        if tok: h["Authorization"] = "Bearer " + tok
        r = requests.post(BASE + dataset, data=json.dumps(body), headers=h, timeout=60)
        if r.status_code == 204 or not r.text.strip(): break
        if r.status_code != 200: raise RuntimeError(f"FINRA {dataset} HTTP {r.status_code}: {r.text[:200]}")
        try: rows = r.json()
        except json.JSONDecodeError: rows = list(csv.DictReader(io.StringIO(r.text)))
        out += rows
        if len(rows) < limit: break
        offset += limit
    return out


def _since(path, col, default=HISTORY_START, back_days=21):
    old = read_csv(DATA / path)
    if old.empty or col not in old: return default
    return (pd.to_datetime(old[col]).max() - pd.Timedelta(days=back_days)).strftime("%Y-%m-%d")


def update_regsho(status):
    """逐日：按 60 天窗口拉 regShoDaily（每个设施一行），汇总为每日 short/exempt/total。另用 CNMS 日文件补最近一天。"""
    try:
        start = _since("finra_regsho_daily.csv", "date"); end = dt.date.today().strftime("%Y-%m-%d")
        rows = []
        s = pd.Timestamp(start)
        while s <= pd.Timestamp(end):
            e = min(s + pd.Timedelta(days=60), pd.Timestamp(end))
            rows += query("regShoDaily", compare={"securitiesInformationProcessorSymbolIdentifier": SYMBOL}, date_range=("tradeReportDate", s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")))
            s = e + pd.Timedelta(days=1)
        df = pd.DataFrame(rows)
        out = pd.DataFrame()
        if len(df):
            for c in ["shortParQuantity", "shortExemptParQuantity", "totalParQuantity"]: df[c] = pd.to_numeric(df[c], errors="coerce")
            g = df.groupby("tradeReportDate").agg(short=("shortParQuantity", "sum"), short_exempt=("shortExemptParQuantity", "sum"), offexch_total=("totalParQuantity", "sum"),
                                                  facilities=("reportingFacilityCode", lambda x: ",".join(sorted(set(x))))).reset_index().rename(columns={"tradeReportDate": "date"})
            out = g
        # CNMS 日文件补最新交易日（API 可能晚一天）
        from .common import last_trading_day
        ltd = last_trading_day()
        for dday in [ltd, ltd - dt.timedelta(days=1)]:
            if dday.weekday() >= 5: continue
            r = get(f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{dday.strftime('%Y%m%d')}.txt", retries=1)
            if not r: continue
            for line in r.text.splitlines():
                p = line.split("|")
                if len(p) >= 5 and p[1] == SYMBOL:
                    rec = dict(date=dday.strftime("%Y-%m-%d"), short=float(p[2]), short_exempt=float(p[3]), offexch_total=float(p[4]), facilities=p[5] if len(p) > 5 else "")
                    out = pd.concat([out[out.date != rec["date"]] if len(out) else out, pd.DataFrame([rec])], ignore_index=True)
        if len(out):
            out["short_pct"] = (out.short / out.offexch_total * 100).round(1)
            res = upsert(DATA / "finra_regsho_daily.csv", out, ["date"]); status.ok("finra.regsho_daily", f"+{len(out)} days, total {len(res)}"); return res
        status.skip("finra.regsho_daily", "no rows"); return read_csv(DATA / "finra_regsho_daily.csv")
    except Exception as e:
        status.fail("finra.regsho_daily", e); return read_csv(DATA / "finra_regsho_daily.csv")


def update_weekly(status):
    """周度 OTC Transparency：ATS 逐家 + 非 ATS 逐家（含 De Minimis）+ 合计。Tier 2 每周一发布 5 周前那一周。"""
    try:
        start = _since("finra_otc_weekly.csv", "weekStartDate", back_days=60); end = dt.date.today().strftime("%Y-%m-%d")
        rows = query("weeklySummary", compare={"issueSymbolIdentifier": SYMBOL}, date_range=("weekStartDate", start, end))
        if not rows: status.skip("finra.otc_weekly", "no new rows"); return read_csv(DATA / "finra_otc_weekly.csv")
        df = pd.DataFrame(rows)
        keep = ["weekStartDate", "summaryTypeCode", "MPID", "marketParticipantName", "totalWeeklyShareQuantity", "totalWeeklyTradeCount", "totalNotionalSum", "tierIdentifier", "initialPublishedDate", "lastUpdateDate"]
        df = df[[c for c in keep if c in df]].copy(); df["MPID"] = df.get("MPID", "").fillna(""); df["marketParticipantName"] = df["marketParticipantName"].fillna("TOTAL")
        res = upsert(DATA / "finra_otc_weekly.csv", df, ["weekStartDate", "summaryTypeCode", "MPID", "marketParticipantName"])
        status.ok("finra.otc_weekly", f"{len(df)} rows since {start}; latest week {df.weekStartDate.max()}"); return res
    except Exception as e:
        status.fail("finra.otc_weekly", e); return read_csv(DATA / "finra_otc_weekly.csv")


def update_monthly(status):
    """月度 OTC（按证券的 ATS/非 ATS 月合计；有则存）。"""
    try:
        start = _since("finra_otc_monthly.csv", "monthStartDate", back_days=100)
        rows = query("monthlySummary", compare={"issueSymbolIdentifier": SYMBOL}, date_range=("monthStartDate", start, dt.date.today().strftime("%Y-%m-%d")))
        if not rows: status.skip("finra.otc_monthly", "no rows"); return read_csv(DATA / "finra_otc_monthly.csv")
        df = pd.DataFrame(rows); df["marketParticipantName"] = df.get("marketParticipantName", pd.Series(["TOTAL"] * len(df))).fillna("TOTAL"); df["MPID"] = df.get("MPID", pd.Series([""] * len(df))).fillna("")
        res = upsert(DATA / "finra_otc_monthly.csv", df, ["monthStartDate", "summaryTypeCode", "MPID", "marketParticipantName"]); status.ok("finra.otc_monthly", f"{len(df)} rows"); return res
    except Exception as e:
        status.fail("finra.otc_monthly", e); return read_csv(DATA / "finra_otc_monthly.csv")


def update_short_interest(status):
    try:
        start = _since("finra_short_interest.csv", "settlementDate", back_days=45)
        rows = query("consolidatedShortInterest", compare={"symbolCode": SYMBOL}, date_range=("settlementDate", start, dt.date.today().strftime("%Y-%m-%d")))
        if not rows: status.skip("finra.short_interest", "no rows"); return read_csv(DATA / "finra_short_interest.csv")
        df = pd.DataFrame(rows)[["settlementDate", "currentShortPositionQuantity", "previousShortPositionQuantity", "changePercent", "averageDailyVolumeQuantity", "daysToCoverQuantity"]]
        res = upsert(DATA / "finra_short_interest.csv", df, ["settlementDate"]); status.ok("finra.short_interest", f"latest {df.settlementDate.max()}"); return res
    except Exception as e:
        status.fail("finra.short_interest", e); return read_csv(DATA / "finra_short_interest.csv")


ATS_OPERATOR = {"SGMT": "高盛 Sigma X2", "UBSA": "UBS ATS", "MSPL": "大摩 MS Pool", "MSTX": "大摩 Trajectory Cross", "MSRP": "大摩 RPool", "MLIX": "美银 Instinct X", "JPMX": "摩根大通 JPM-X", "JPBX": "摩根大通 JPB-X",
                "INCR": "Intelligent Cross", "CGXS": "OneChronos", "IATS": "盈透 IBKR ATS", "IEOS": "盈透 EOS", "EBXL": "Level ATS", "BIDS": "BIDS（大宗）", "PURE": "PureStream（大宗）", "ITGP": "Posit（Virtu）", "LATS": "巴克莱 LX",
                "KCGM": "Virtu MatchIt", "BNPX": "法巴 Cortex", "ONEC": "花旗 Citi-ONE", "XSTM": "CrossStream", "LQNA": "Liquidnet H2O", "LQNT": "Liquidnet", "BLUE": "BOATS", "ALPX": "AlphaX", "STFX": "Stifel X", "CODX": "CODA", "ICBX": "CBX（Instinet）", "BLKX": "Instinet BlockCross（大宗）"}
WHOLESALERS = ["CITADEL", "VIRTU AMERICAS", "G1 EXECUTION", "JANE STREET", "TWO SIGMA SECURITIES", "HRT FINANCIAL", "HUDSON RIVER", "SUSQUEHANNA", "GTS SECURITIES"]


def weekly_summary(weekly, daily):
    """周度汇总：ATS/非 ATS/Citadel/批发商/De Minimis、笔均、占合并成交比例。"""
    if weekly.empty: return pd.DataFrame(), pd.DataFrame()
    w = weekly.copy()
    for c in ["totalWeeklyShareQuantity", "totalWeeklyTradeCount", "totalNotionalSum"]: w[c] = pd.to_numeric(w[c], errors="coerce")
    d = daily.copy(); d["date"] = pd.to_datetime(d.date); d["week"] = (d.date - pd.to_timedelta(d.date.dt.weekday, unit="D")).dt.strftime("%Y-%m-%d")
    wk_vol = d.groupby("week").agg(consol_vol=("volume", "sum"), week_close=("close", "last"))
    rows = []
    for wk, g in w.groupby("weekStartDate"):
        ats = g[g.summaryTypeCode == "ATS_W_SMBL"]; otc = g[g.summaryTypeCode == "OTC_W_SMBL"]
        atsf = g[g.summaryTypeCode == "ATS_W_SMBL_FIRM"]; otcf = g[g.summaryTypeCode == "OTC_W_SMBL_FIRM"]
        ats_sh = float(ats.totalWeeklyShareQuantity.sum()); ats_tr = float(ats.totalWeeklyTradeCount.sum()); otc_sh = float(otc.totalWeeklyShareQuantity.sum())
        names = otcf.marketParticipantName.fillna("").str.upper()
        whol = float(otcf[names.apply(lambda n: any(k in n for k in WHOLESALERS))].totalWeeklyShareQuantity.sum())
        demin = float(otcf[names.str.contains("DE MINIMIS")].totalWeeklyShareQuantity.sum())
        cit = float(otcf[names.str.contains("CITADEL")].totalWeeklyShareQuantity.sum())
        top = atsf.sort_values("totalWeeklyShareQuantity", ascending=False).head(3)
        cv = float(wk_vol.consol_vol.get(wk, float("nan"))); wc = wk_vol.week_close.get(wk, float("nan"))
        rows.append(dict(week=wk, consol_vol=cv, week_close=wc, ats_shares=ats_sh, ats_trades=ats_tr, ats_avg_size=round(ats_sh / ats_tr) if ats_tr else None, nonats_shares=otc_sh, wholesaler_shares=whol, citadel_shares=cit, deminimis_shares=demin,
                         offexch_shares=ats_sh + otc_sh, ats_pct_of_consol=round(ats_sh / cv * 100, 1) if cv else None, offexch_pct_of_consol=round((ats_sh + otc_sh) / cv * 100, 1) if cv else None,
                         top_ats=" / ".join(f"{ATS_OPERATOR.get(m, m)} {int(s/1e3)}k" for m, s in zip(top.MPID, top.totalWeeklyShareQuantity)), published=g.initialPublishedDate.max() if "initialPublishedDate" in g else ""))
    summ = pd.DataFrame(rows).sort_values("week")
    venues = w[w.summaryTypeCode == "ATS_W_SMBL_FIRM"].copy(); venues["venue"] = venues.MPID.map(lambda m: ATS_OPERATOR.get(m, m)); venues["avg_size"] = (venues.totalWeeklyShareQuantity / venues.totalWeeklyTradeCount).round(0)
    return summ, venues
