"""把所有数据汇总成 docs/data/dashboard.json：逐日表+标签、周度暗池、空头/FTD/借券、期权、关注度、持有人、申报、见底打分、参与方矩阵。"""
import datetime as dt, json, math, shutil
import numpy as np, pandas as pd
from .common import DATA, DOCS_DATA, SYMBOL, SHARES_A, read_csv, load_json, save_json
from .prices import enrich
from .finra import weekly_summary, ATS_OPERATOR
from .common import CUSIP

# 价值锚（来自此前研究，可在 anchors.json 覆盖）
ANCHORS_DEFAULT = {"管理层三轮买入均价": 15.90, "CEO 买入均价": 16.38, "2025-10 增发价": 16.08, "2026-05-18 低点": 14.54, "Bear 情景值": 14.0, "C 类权证行权价": 18.0, "权证强制行权线": 21.60, "公司 2026 回购均价": 20.50, "卢煜明换股成本": 13.50}
BLOCK_VENUES = ("BIDS", "PURE", "BLKX", "LQNA", "LQNT")


def _f(x, nd=2):
    try:
        if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))): return None
        return round(float(x), nd)
    except Exception: return None


def _sorted(df, col):
    return df.sort_values(col) if df is not None and len(df) and col in df else (df if df is not None else pd.DataFrame())


def _recs(df, cols=None, n=None):
    if df is None or len(df) == 0: return []
    d = df if cols is None else df[[c for c in cols if c in df]]
    if n: d = d.tail(n)
    out = d.replace({np.nan: None}).to_dict("records")
    for r in out:
        for k, v in r.items():
            if isinstance(v, (np.integer,)): r[k] = int(v)
            elif isinstance(v, (np.floating,)): r[k] = None if np.isnan(v) else float(v)
    return out


def build(status_rows):
    daily = read_csv(DATA / "prices_daily.csv"); intraday = read_csv(DATA / "intraday_stats.csv")
    if daily.empty: raise RuntimeError("no prices")
    d = enrich(daily, intraday)
    rs = read_csv(DATA / "finra_regsho_daily.csv")
    if len(rs):
        d = d.merge(rs[["date", "short", "short_exempt", "offexch_total", "short_pct"]], on="date", how="left")
        d["offexch_pct"] = (d.offexch_total / d.volume * 100).round(1)
    else:
        for c in ["short", "short_exempt", "offexch_total", "short_pct", "offexch_pct"]: d[c] = np.nan
    cb = read_csv(DATA / "nasdaq_circuit_breaker.csv"); th = read_csv(DATA / "nasdaq_threshold.csv")
    if len(cb) and "in_effect" in cb: d = d.merge(cb[["date", "in_effect", "triggered_today", "trigger_time"]], on="date", how="left")
    else: d = d.assign(in_effect=np.nan, triggered_today=np.nan, trigger_time="")
    d = d.merge(th, on="date", how="left") if len(th) else d.assign(on_threshold_list=np.nan)

    def flags(r):
        f = []
        if _f(r.vol_x_adv20) and r.vol_x_adv20 >= 1.5: f.append("放量")
        if _f(r.offexch_pct) and r.offexch_pct > 55: f.append("场外占比高→疑似大宗")
        if _f(r.short_pct) is not None and r.short_pct < 35 and _f(r.ret_pct) is not None and r.ret_pct < -3: f.append("低卖空占比下跌→多头卖出")
        if _f(r.short_pct) and r.short_pct > 60: f.append("卖空占比高→做市商供货")
        cv = r.get("close_vs_vwap_pct")
        if _f(cv) is not None and cv < -2 and _f(r.ret_pct) is not None and r.ret_pct < 0: f.append("弱收盘")
        l30 = r.get("last30_pct")
        if _f(l30) and l30 > 30: f.append("尾盘集中")
        if _f(r.vol_x_adv20) and r.vol_x_adv20 >= 1.5 and _f(r.close_pos) is not None and r.close_pos >= 0.5: f.append("⭐见底候选")
        if _f(r.triggered_today) == 1: f.append("卖空熔断触发")
        elif _f(r.in_effect) == 1: f.append("熔断生效中")
        if _f(r.get("on_threshold_list")) == 1: f.append("门槛名单")
        return f
    d["flags"] = d.apply(flags, axis=1)      # 注意：pandas 的 .flags 是属性，取列必须用 d["flags"]
    last = d.iloc[-1]; last_flags = list(last["flags"])

    # ---- 周度暗池 ----
    weekly = read_csv(DATA / "finra_otc_weekly.csv"); summ, venues = weekly_summary(weekly, daily)
    venue_pivot = []
    if len(venues):
        recent_weeks = sorted(venues.weekStartDate.unique())[-8:]
        v = venues[venues.weekStartDate.isin(recent_weeks)]
        pv = v.pivot_table(index="venue", columns="weekStartDate", values="totalWeeklyShareQuantity", aggfunc="sum").fillna(0)
        pv["total"] = pv.sum(axis=1); pv = pv.sort_values("total", ascending=False).head(14)
        agg = v.groupby("venue")[["totalWeeklyShareQuantity", "totalWeeklyTradeCount"]].sum(); sz = (agg.totalWeeklyShareQuantity / agg.totalWeeklyTradeCount.clip(lower=1)).round(0)
        for ven, row in pv.iterrows(): venue_pivot.append({"venue": ven, "avg_size": int(sz.get(ven, 0)), "weeks": {w: int(row[w]) for w in recent_weeks}, "total": int(row.total)})
        venue_weeks = list(recent_weeks)
    else: venue_weeks = []
    # 基线 = 最近 30 周里剔除最新 4 周的中位数（滚动半年，避免被当前派发期和早期低样本拉偏）
    _b = summ.ats_pct_of_consol.dropna() if len(summ) else pd.Series(dtype=float)
    baseline_ats = float(_b.iloc[-30:-4].median()) if len(_b) > 8 else (float(_b.median()) if len(_b) else None)

    # ---- 空头/FTD/借券/期权/关注度 ----
    si = read_csv(DATA / "finra_short_interest.csv"); ftd = read_csv(DATA / "sec_ftd.csv"); borrow = read_csv(DATA / "borrow_daily.csv")
    opt = read_csv(DATA / "options_daily.csv"); opt_tot = opt[opt.expiry == "TOTAL"] if len(opt) else pd.DataFrame()
    stm = read_csv(DATA / "stocktwits_messages.csv"); att = read_csv(DATA / "attention_daily.csv"); rd = read_csv(DATA / "reddit_posts.csv")
    st_daily = stm.assign(day=pd.to_datetime(stm.created_at).dt.strftime("%Y-%m-%d")).groupby("day").size() if len(stm) else pd.Series(dtype=int)
    rd_daily = rd.assign(day=rd.created.str[:10]).groupby("day").size() if len(rd) else pd.Series(dtype=int)
    att_rows = []
    for day in sorted(set(st_daily.index) | set(rd_daily.index) | (set(att.date.astype(str)) if len(att) else set())):
        w = att[att.date.astype(str) == day].st_watchlist.iloc[-1] if len(att) and (att.date.astype(str) == day).any() else None
        att_rows.append({"date": day, "st_msgs": int(st_daily.get(day, 0)), "reddit_posts": int(rd_daily.get(day, 0)), "st_watchlist": _f(w, 0)})

    # ---- 持有人 ----
    h13 = read_csv(DATA / "sec_13f_holders.csv")
    holders = {"periods": [], "latest_period": None, "rows": [], "totals": {}}
    if len(h13):
        h = h13[h13.cusip.astype(str) == CUSIP].copy(); h["shares"] = pd.to_numeric(h.shares, errors="coerce"); h["value_usd"] = pd.to_numeric(h.value_usd, errors="coerce")
        h = h.sort_values("filed").drop_duplicates(["filer_cik", "period", "put_call"], keep="last")
        h = h[h.put_call.fillna("") == ""]
        periods = sorted(h.period.dropna().unique()); holders["periods"] = periods
        # 13F 截止日 = 季末后 45 天；未到截止的季度只是“已报部分”，不能当最新期
        today = dt.date.today(); complete = [p for p in periods if (pd.Timestamp(p) + pd.Timedelta(days=47)).date() <= today]
        partial = [p for p in periods if p not in complete]
        holders["partial"] = [{"period": p, "filers_so_far": int(h[h.period == p].filer_cik.nunique()), "shares_so_far": _f(h[h.period == p].shares.sum(), 0), "deadline": (pd.Timestamp(p) + pd.Timedelta(days=45)).strftime("%Y-%m-%d")} for p in partial]
        periods = complete
        if periods:
            P = periods[-1]; P0 = periods[-2] if len(periods) > 1 else None
            cur = h[h.period == P].groupby(["filer_cik", "filer"], as_index=False).shares.sum(); prev = h[h.period == P0].groupby("filer_cik", as_index=False).shares.sum().rename(columns={"shares": "prev_shares"}) if P0 else pd.DataFrame(columns=["filer_cik", "prev_shares"])
            m = cur.merge(prev, on="filer_cik", how="outer"); m["filer"] = m.filer.fillna("(已清仓) ")
            if P0:
                names = h[h.period == P0].drop_duplicates("filer_cik").set_index("filer_cik").filer
                m["filer"] = [f if f != "(已清仓) " else "(已清仓) " + str(names.get(c, "")) for f, c in zip(m.filer, m.filer_cik)]
            m["change"] = m.shares.fillna(0) - m.prev_shares.fillna(0)
            m = m.sort_values("shares", ascending=False, na_position="last")
            holders["latest_period"] = P; holders["prev_period"] = P0
            holders["rows"] = [{"filer": r.filer, "shares": _f(r.shares, 0), "prev_shares": _f(r.prev_shares, 0), "change": _f(r.change, 0)} for r in m.itertuples()]
            holders["totals"] = {"holders": int(cur.shape[0]), "shares": _f(cur.shares.sum(), 0), "pct_of_A": _f(cur.shares.sum() / SHARES_A * 100, 1), "prev_shares": _f(prev.prev_shares.sum(), 0) if P0 else None}
            holders["by_period"] = [{"period": p, "holders": int(h[h.period == p].filer_cik.nunique()), "shares": _f(h[h.period == p].shares.sum(), 0)} for p in periods]
    nq_inst = load_json(DATA / "nasdaq_institutional_latest.json", {})
    f4 = read_csv(DATA / "edgar_form4.csv"); ins = read_csv(DATA / "nasdaq_insiders.csv")
    filings = read_csv(DATA / "edgar_filings.csv"); events = read_csv(DATA / "edgar_events.csv")
    if len(filings) and len(events): filings = filings.merge(events[["accession", "tags", "pct_of_class", "snippet"]], on="accession", how="left")
    aa = read_csv(DATA / "analyst_actions.csv"); at = read_csv(DATA / "analyst_targets_daily.csv"); news = read_csv(DATA / "news.csv")

    # ---- 见底打分 ----
    anchors = {**ANCHORS_DEFAULT, **(load_json(DATA / "anchors.json", {}) or {})}
    post_er = d[d.date >= "2026-08-18"]
    if len(post_er): anchors["8/18 以来成交 VWAP"] = _f((post_er.close * post_er.volume).sum() / post_er.volume.sum())
    recent = d.tail(10); last5 = d.tail(5); down5 = d[d.ret_pct < 0].tail(5)
    items = []
    def item(id, name, value, target, ok, note=""): items.append({"id": id, "name": name, "value": value, "target": target, "pass": ok, "note": note})
    anchor_hi = max(anchors.get("CEO 买入均价", 16.38), anchors.get("2025-10 增发价", 16.08)) * 1.05
    item(1, "价格 vs 价值锚", f"${last.close:.2f}", f"≤ ${anchor_hi:.2f}（管理层/增发价带 +5%）", bool(last.close <= anchor_hi), f"EV/收入 ≈ {((last.close * SHARES_A / 1e6 - 109) / 220):.2f}x FY26E（净现金按 6/30 $109M，FY26E 收入 $220M）")
    rsi, pb, dm = _f(last.rsi14, 1), _f(last.pct_b), _f(last.dist_ma50_pct, 1)
    item(2, "超卖程度", f"RSI {rsi} / %B {pb} / 距50日线 {dm}%", "RSI<25 或 %B<−0.2", (rsi is not None and rsi < 25) or (pb is not None and pb < -0.2), "接近" if rsi is not None and rsi < 32 else "")
    panic = recent[(recent.vol_x_adv20 >= 2) & (recent.close_pos >= 0.5)]
    item(3, "恐慌日（近 10 日）", f"{len(panic)} 天" + (f"（{', '.join(panic.date)}）" if len(panic) else ""), "≥2 倍均量且收在上半区 ≥1 天", bool(len(panic)))
    low20 = d.tail(20).close.min(); exhaust = bool(len(last5) >= 3 and (last5.tail(3).vol_x_adv20 < 0.6).all() and last.close > low20 * 1.0 and last5.tail(3).close.min() > low20)
    item(4, "缩量枯竭（近 3 日）", f"倍数 {', '.join(str(_f(x)) for x in last5.tail(3).vol_x_adv20)}", "连续 3 日 <0.6 倍均量且不创新低", exhaust)
    cvv = down5.close_vs_vwap_pct.dropna() if "close_vs_vwap_pct" in down5 else pd.Series(dtype=float)
    item(5, "下跌日收盘 vs VWAP（近 5 个下跌日）", f"均值 {_f(cvv.mean(), 2)}%", "≥ 0%（卖方收盘前卖完）", bool(len(cvv) and cvv.mean() >= 0))
    acc, dist = int(recent.acc_day.sum()), int(recent.dist_day.sum())
    item(6, "吸筹日 vs 派发日（近 10 日）", f"{acc} vs {dist}", "吸筹 ≥ 派发", acc >= dist)
    sp = down5.short_pct.dropna()
    item(7, "下跌日卖空占场外（近 5 个下跌日）", f"均值 {_f(sp.mean(), 1)}%", "≥ 50%（空头/做市商主导，可回补）", bool(len(sp) and sp.mean() >= 50), "低=多头在卖")
    si_ok = None; si_val = "无数据"
    if len(si):
        s0 = si.iloc[-1]; si_val = f"{int(s0.currentShortPositionQuantity):,}（{s0.settlementDate}，{_f(s0.changePercent, 1)}%，DTC {_f(s0.daysToCoverQuantity, 1)}）"; si_ok = bool(float(s0.changePercent) > 0)
    item(8, "空头持仓", si_val, "回升（为后续回补提供燃料）", si_ok)
    ats_ok = None; ats_val = "无数据"
    if len(summ):
        w0 = summ.iloc[-1]; ats_val = f"{w0.ats_pct_of_consol}%（{w0.week} 当周；基线中位 {_f(baseline_ats, 1)}%）"; ats_ok = bool(w0.ats_pct_of_consol is not None and baseline_ats is not None and w0.ats_pct_of_consol <= baseline_ats + 3)
    item(9, "暗池占全部成交（最新周）", ats_val, "回到基线（派发结束）", ats_ok, "滞后约 5 周")
    cutoff = (pd.Timestamp(last.date) - pd.Timedelta(days=30)).strftime("%Y-%m-%d"); buyers = []
    if len(f4) and "code" in f4:
        fb = f4[(f4.filing_date >= cutoff) & (f4.code.astype(str) == "P") & (f4.acq_disp.astype(str) == "A")]
        if len(fb): buyers.append(f"内部人买入 {len(fb)} 笔")
    if len(filings) and "form" in filings:
        fe = filings[filings.filing_date >= cutoff]
        if len(fe) and fe.get("tags", pd.Series(dtype=str)).fillna("").str.contains("回购").any(): buyers.append("6-K 提及回购")
        if len(fe) and fe.form.str.startswith("SC 13").any(): buyers.append("13D/G 申报")
    item(10, "新买方（近 30 日）", "；".join(buyers) or "无", "内部人买入 / 新回购授权 / 新 5% 股东", bool(buyers))
    peers = read_csv(DATA / "peers_daily.csv"); sec_ok = None; sec_val = "无数据"
    if len(peers) and len(peers) > 11:
        p = peers.tail(11); r10 = {c: _f((p[c].iloc[-1] / p[c].iloc[0] - 1) * 100, 1) for c in p.columns if c != "date"}; sec_val = " / ".join(f"{k} {v}%" for k, v in r10.items()); sec_ok = all(v is not None and v > -5 for v in r10.values())
    item(11, "板块（近 10 日）", sec_val, "同行未显著下跌", sec_ok)
    st7 = int(sum(v for k, v in st_daily.items() if k >= (pd.Timestamp(last.date) - pd.Timedelta(days=7)).strftime("%Y-%m-%d"))) if len(st_daily) else 0
    item(12, "散户关注（Stocktwits 近 7 日）", f"{st7} 条", "沉默（无追涨盘待清）或投降式放量讨论", st7 <= 5 or st7 >= 40)
    passed = sum(1 for i in items if i["pass"]); near = sum(1 for i in items if i["note"] == "接近")
    if items[2]["pass"] or items[3]["pass"]: stage = "C 恐慌/枯竭"
    elif passed >= 8 and items[9]["pass"]: stage = "D 筑底确认"
    elif _f(last.ma50) and last.close < last.ma50: stage = "B 主跌"
    else: stage = "A 派发/震荡"
    scorecard = {"stage": stage, "passed": passed, "near": near, "total": len(items), "items": items, "anchors": anchors}

    # ---- 参与方矩阵 ----
    parts = []
    def P(group, signal, value, reading, source, asof): parts.append({"group": group, "signal": signal, "value": value, "reading": reading, "source": source, "asof": asof})
    w0 = summ.iloc[-1] if len(summ) else None
    P("散户 / 活跃交易者", "Stocktwits 近 7 日消息 / 关注数", f"{st7} 条 / {int(att.st_watchlist.dropna().iloc[-1]) if len(att) and att.st_watchlist.notna().any() else '—'}", "沉默=无追涨盘也无接盘；放量讨论+下跌=投降", "Stocktwits API", str(last.date))
    if w0 is not None: P("散户 / 活跃交易者", "零售批发商周成交（Citadel/批发商/小额合并）", f"{int(w0.citadel_shares):,} / {int(w0.wholesaler_shares):,} / {int(w0.deminimis_shares):,} 股", "Citadel 被单独列出=日均 >200 笔=零售流激增", "FINRA OTC 周度", w0.week)
    P("做市商 / 高频", "最新日卖空占场外成交", f"{_f(last.short_pct, 1)}%", ">55% 做市商在给买单供货；<35% 多头主动卖", "FINRA Reg SHO 日度", str(last.date))
    if w0 is not None: P("做市商 / 高频", "暗池笔均股数（最新周）", f"{w0.ats_avg_size} 股", "<40 股=算法碎单；>200 股=大宗", "FINRA OTC 周度", w0.week)
    if w0 is not None:
        blk = venues[(venues.weekStartDate == w0.week) & (venues.MPID.isin(BLOCK_VENUES))].totalWeeklyShareQuantity.sum() if len(venues) else 0
        P("机构（暗池 / 大宗）", "暗池占全部成交 / 大宗平台成交", f"{w0.ats_pct_of_consol}% / {int(blk):,} 股", f"基线 {_f(baseline_ats, 1)}%；>28% 机构换手（方向看价格+卖空占比）", "FINRA OTC 周度", w0.week)
        P("机构（暗池 / 大宗）", "最活跃暗池", w0.top_ats, "高盛/大摩/UBS=投行客户算法；BIDS/PureStream/BlockCross=大宗", "FINRA OTC 周度", w0.week)
    P("机构（暗池 / 大宗）", "最新日场外占比", f"{_f(last.offexch_pct, 1)}%", ">55% 疑似协议大宗", "FINRA Reg SHO 日度", str(last.date))
    if holders["latest_period"]: P("13F 机构", f"持有人 / 合计股数（{holders['latest_period']}）", f"{holders['totals']['holders']} 家 / {int(holders['totals']['shares']):,} 股（A 类 {holders['totals']['pct_of_A']}%）", f"上期 {int(holders['totals']['prev_shares'] or 0):,} 股", "SEC 13F 全文检索", holders["latest_period"])
    if nq_inst: P("13F 机构", "Nasdaq 机构持股比例", f"{(nq_inst.get('summary') or {}).get('SharesOutstandingPCT', '—')}", "低=卖方在不申报层", "api.nasdaq.com", nq_inst.get("date", ""))
    if len(si): s0 = si.iloc[-1]; P("空头", "空头持仓 / 变动 / DTC", f"{int(s0.currentShortPositionQuantity):,} / {_f(s0.changePercent, 1)}% / {_f(s0.daysToCoverQuantity, 1)}", "在降=空头不是砸盘主力；回升=回补燃料", "FINRA 双周", s0.settlementDate)
    if len(ftd): f0 = ftd.iloc[-1]; P("空头", "失败交收 FTD（最新）", f"{int(f0.qty):,} 股 @ {f0.price}", "持续放大+门槛名单=裸空压力", "SEC FTD 半月", f0.date)
    if len(borrow): b0 = borrow.iloc[-1]; P("空头", "借券费率 / 可借量", f"{b0.fee}% / {b0.available}", "费率飙升=空头拥挤", "iBorrowDesk(IBKR)", b0.date)
    if len(cb) and "triggered_today" in cb: c0 = cb[cb.triggered_today == 1]; P("空头", "卖空熔断最近触发", str(c0.trigger_time.iloc[-1]) if len(c0) else "无", "盘中跌 ≥10% 触发，当日余下+次日卖空受限；受限仍跌=多头卖", "Nasdaq Trader", str(cb.date.iloc[-1]))
    if len(f4) and "code" in f4:
        fp = f4[(f4.code.astype(str) == "P")]; fs = f4[(f4.code.astype(str) == "S")]
        P("内部人 / 大股东", "最近 Form 4 买入 / 卖出", f"买 {fp.tx_date.iloc[-1] if len(fp) else '无'} / 卖 {fs.tx_date.iloc[-1] if len(fs) else '无'}", "历史规律：财报后开窗即买", "SEC Form 4", str(f4.filing_date.max()))
    if len(filings) and "form" in filings:
        f13 = filings[filings.form.str.startswith("SC 13")]
        P("内部人 / 大股东", "最近 13D/G", f"{f13.form.iloc[-1]} {f13.filing_date.iloc[-1]}" if len(f13) else "无", "≥5% 股东变动 ≥1% 须 2 日内申报", "SEC EDGAR", str(filings.filing_date.max()))
        k6 = filings[filings.form == "6-K"]
        P("公司", "最近 6-K 与标签", f"{k6.filing_date.iloc[-1]} {k6.get('tags', pd.Series(dtype=str)).iloc[-1] if len(k6) and 'tags' in k6 else ''}" if len(k6) else "无", "回购授权/增发/权证生效=供需变化", "SEC EDGAR", str(filings.filing_date.max()))
        eff = filings[filings.form == "EFFECT"]; P("公司", "F-1 生效（权证可行权）", f"最近 EFFECT {eff.filing_date.iloc[-1]}" if len(eff) else "未生效", "生效后 $18 以上有权证供给", "SEC EDGAR", str(filings.filing_date.max()))
    if len(at): a0 = at.iloc[-1]; P("卖方 / 分析师", "目标价 均值/区间 · 评级分布", f"${_f(a0.pt_mean, 1)}（{_f(a0.pt_low, 0)}–{_f(a0.pt_high, 0)}）· 强买{_f(a0.strong_buy, 0)}/买{_f(a0.buy, 0)}/持{_f(a0.hold, 0)}/卖{_f(a0.sell, 0)}", "初评日往往放量一日即耗尽", "Yahoo", a0.date)
    if len(aa): a1 = aa.iloc[-1]; P("卖方 / 分析师", "最近评级动作", f"{a1.gradedate} {a1.firm} {a1.action} → {a1.tograde}", "", "Yahoo", a1.gradedate)
    if len(opt_tot): o0 = opt_tot.iloc[-1]; P("期权", "未平仓 看涨/看跌", f"{int(o0.calls_oi)} / {int(o0.puts_oi)} 张", "对冲股数上限 =(C+P)×100；小=无衍生品驱动", "Yahoo 期权链", o0.date)

    # ---- KPI / 最新 ----
    latest = {k: (_f(v) if isinstance(v, (float, np.floating)) else (int(v) if isinstance(v, (np.integer,)) else v)) for k, v in last.drop(labels=["flags"]).items()}
    latest["flags"] = "|".join(last_flags)
    kpis = {"close": _f(last.close), "ret_pct": _f(last.ret_pct), "volume": int(last.volume), "vol_x_adv20": _f(last.vol_x_adv20), "turnover_pct": _f(last.turnover_pct), "close_vs_vwap_pct": _f(last.get("close_vs_vwap_pct")), "last30_pct": _f(last.get("last30_pct"), 1),
            "offexch_pct": _f(last.offexch_pct, 1), "short_pct": _f(last.short_pct, 1), "si": int(si.iloc[-1].currentShortPositionQuantity) if len(si) else None, "si_date": si.iloc[-1].settlementDate if len(si) else None, "dtc": _f(si.iloc[-1].daysToCoverQuantity, 1) if len(si) else None,
            "inst_pct": holders["totals"].get("pct_of_A") if holders.get("totals") else None, "nq_inst_pct": (nq_inst.get("summary") or {}).get("SharesOutstandingPCT") if nq_inst else None, "rsi": rsi, "dist_ma50": dm, "ma50": _f(last.ma50), "ma200": _f(last.ma200), "stage": stage, "score": f"{passed}/{len(items)}"}

    out = {"meta": {"symbol": SYMBOL, "built_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), "last_trade_date": str(last.date), "shares_a": SHARES_A, "sources": status_rows, "baseline_ats_pct": _f(baseline_ats, 1)},
           "kpis": kpis, "latest": latest, "scorecard": scorecard, "participants": parts,
           "daily": _recs(d.assign(flags=d["flags"].apply(lambda x: "|".join(x))), ["date", "open", "high", "low", "close", "volume", "ret_pct", "turnover_pct", "vol_x_adv20", "close_pos", "gap_pct", "intraday_pct", "ma20", "ma50", "ma200", "rsi14", "pct_b", "vwap", "close_vs_vwap_pct", "first30_pct", "last30_pct", "max_1m_vol", "max_1m_time", "short", "short_exempt", "offexch_total", "short_pct", "offexch_pct", "in_effect", "triggered_today", "trigger_time", "on_threshold_list", "acc_day", "dist_day", "flags"], n=260),
           "weekly": _recs(summ, n=40), "venue_weeks": venue_weeks, "venue_pivot": venue_pivot,
           "short_interest": _recs(si, n=40), "ftd": _recs(ftd, n=80), "borrow": _recs(borrow, n=120), "options": _recs(opt_tot, n=120), "options_top": load_json(DATA / "options_top_latest.json", {}),
           "attention": att_rows[-120:], "holders_13f": holders, "nasdaq_inst": nq_inst,
           "form4": _recs(_sorted(f4, "filing_date"), n=40), "insiders_nasdaq": _recs(ins, n=30),
           "filings": _recs(_sorted(filings, "filing_date"), ["filing_date", "form", "description", "tags", "pct_of_class", "snippet", "url"], n=80),
           "analysts": {"actions": _recs(aa, n=30), "targets": _recs(at, n=1)}, "news": _recs(_sorted(news, "date"), n=40)}
    save_json(DOCS_DATA / "dashboard.json", out)
    # CSV 副本供下载
    csvdir = DOCS_DATA / "csv"; csvdir.mkdir(exist_ok=True)
    for p in DATA.glob("*.csv"): shutil.copy(p, csvdir / p.name)
    return out
