"""期权、社交关注度（Stocktwits/Reddit）、借券（iBorrowDesk）、分析师动作、新闻（Google News RSS）。全部 best-effort。"""
import datetime as dt, json, re, time, html as _html
import pandas as pd
from .common import DATA, SYMBOL, get, read_csv, upsert, load_json, save_json


def update_options(status):
    try:
        import yfinance as yf
        t = yf.Ticker(SYMBOL); today = dt.date.today().isoformat(); rows = []; tot_c = tot_p = volc = volp = 0; top = []
        for e in t.options[:8]:
            ch = t.option_chain(e); c, p = ch.calls, ch.puts
            oc, op = int(c.openInterest.fillna(0).sum()), int(p.openInterest.fillna(0).sum()); vc, vp = int(c.volume.fillna(0).sum()), int(p.volume.fillna(0).sum())
            tot_c += oc; tot_p += op; volc += vc; volp += vp
            rows.append(dict(date=today, expiry=e, calls_oi=oc, puts_oi=op, calls_vol=vc, puts_vol=vp))
            for df, kind in ((c, "C"), (p, "P")):
                for _, x in df[df.openInterest.fillna(0) >= 20].iterrows(): top.append(dict(expiry=e, type=kind, strike=float(x.strike), oi=int(x.openInterest), vol=int(x.volume) if pd.notna(x.volume) else 0, last=float(x.lastPrice)))
        rows.append(dict(date=today, expiry="TOTAL", calls_oi=tot_c, puts_oi=tot_p, calls_vol=volc, puts_vol=volp))
        out = upsert(DATA / "options_daily.csv", rows, ["date", "expiry"]); save_json(DATA / "options_top_latest.json", {"date": today, "top": sorted(top, key=lambda x: -x["oi"])[:15]})
        status.ok("options.yahoo", f"OI C{tot_c}/P{tot_p}"); return out
    except Exception as e:
        status.fail("options.yahoo", e); return read_csv(DATA / "options_daily.csv")


def update_stocktwits(status):
    try:
        old = read_csv(DATA / "stocktwits_messages.csv"); last_id = int(old.id.max()) if len(old) else 0
        rows = []; mx = None; watch = None
        for _ in range(12):
            url = f"https://api.stocktwits.com/api/2/streams/symbol/{SYMBOL}.json?limit=30" + (f"&max={mx}" if mx else "")
            r = get(url, retries=2)
            if not r: break
            j = r.json(); watch = watch or (j.get("symbol") or {}).get("watchlist_count")
            msgs = j.get("messages") or []
            if not msgs: break
            stop = False
            for m in msgs:
                if int(m["id"]) <= last_id: stop = True; continue
                s = ((m.get("entities") or {}).get("sentiment") or {}).get("basic") or ""
                rows.append(dict(id=int(m["id"]), created_at=m["created_at"], user=(m.get("user") or {}).get("username", ""), followers=(m.get("user") or {}).get("followers", ""), sentiment=s, body=_html.unescape(re.sub(r"\s+", " ", m.get("body", "")))[:400]))
            if stop or not (j.get("cursor") or {}).get("more"): break
            mx = (j.get("cursor") or {}).get("max"); time.sleep(0.4)
        if rows: upsert(DATA / "stocktwits_messages.csv", rows, ["id"], sort=["created_at"])
        att = dict(date=dt.date.today().isoformat(), st_watchlist=watch, st_new_msgs=len(rows))
        upsert(DATA / "attention_daily.csv", [att], ["date"]); status.ok("social.stocktwits", f"watch {watch}; +{len(rows)} msgs"); return read_csv(DATA / "stocktwits_messages.csv")
    except Exception as e:
        status.fail("social.stocktwits", e); return read_csv(DATA / "stocktwits_messages.csv")


def update_reddit(status):
    try:
        q = "%22Prenetics%22+OR+%22IM8%22+supplement+OR+%22%24PRE%22"
        r = get(f"https://www.reddit.com/search.json?q={q}&sort=new&limit=50&raw_json=1&t=month", ua="python:pre-flow-tracker:1.0 (research script)", headers={"Accept": "application/json"}, retries=2)
        if not r or not r.text.lstrip().startswith("{"): status.skip("social.reddit", "blocked"); return read_csv(DATA / "reddit_posts.csv")
        ch = (r.json().get("data") or {}).get("children") or []
        rows = [dict(id=c["data"]["id"], created=dt.datetime.utcfromtimestamp(c["data"]["created_utc"]).strftime("%Y-%m-%d %H:%M"), subreddit=c["data"].get("subreddit"), title=c["data"].get("title", "")[:160], score=c["data"].get("score"), comments=c["data"].get("num_comments"), url="https://www.reddit.com" + c["data"].get("permalink", "")) for c in ch]
        if rows: out = upsert(DATA / "reddit_posts.csv", rows, ["id"], sort=["created"]); status.ok("social.reddit", f"{len(rows)} posts"); return out
        status.ok("social.reddit", "0 posts"); return read_csv(DATA / "reddit_posts.csv")
    except Exception as e:
        status.fail("social.reddit", e); return read_csv(DATA / "reddit_posts.csv")


def update_borrow(status):
    """iBorrowDesk（IBKR 借券费率/可借量）。"""
    try:
        r = get(f"https://iborrowdesk.com/api/ticker/{SYMBOL}", headers={"Accept": "application/json"}, retries=2)
        if not r or not r.text.lstrip().startswith("{"): status.skip("borrow.iborrowdesk", "unreachable"); return read_csv(DATA / "borrow_daily.csv")
        j = r.json(); rows = []
        for d in j.get("daily") or []:
            rows.append(dict(date=str(d.get("date", ""))[:10], fee=d.get("fee"), available=d.get("available")))
        rt = j.get("real_time") or []
        if rt:
            x = rt[-1]; rows.append(dict(date=str(x.get("time", ""))[:10] or dt.date.today().isoformat(), fee=x.get("fee"), available=x.get("available")))
        if rows: out = upsert(DATA / "borrow_daily.csv", rows, ["date"]); status.ok("borrow.iborrowdesk", f"latest fee {rows[-1]['fee']} avail {rows[-1]['available']}"); return out
        status.skip("borrow.iborrowdesk", "no rows"); return read_csv(DATA / "borrow_daily.csv")
    except Exception as e:
        status.fail("borrow.iborrowdesk", e); return read_csv(DATA / "borrow_daily.csv")


def update_analysts(status):
    try:
        import yfinance as yf
        t = yf.Ticker(SYMBOL)
        ud = t.upgrades_downgrades
        if ud is not None and len(ud):
            ud = ud.reset_index(); ud.columns = [c.lower() for c in ud.columns]
            ud["gradedate"] = pd.to_datetime(ud["gradedate"]).dt.strftime("%Y-%m-%d")
            upsert(DATA / "analyst_actions.csv", ud[["gradedate", "firm", "tograde", "fromgrade", "action"]], ["gradedate", "firm", "tograde"], sort=["gradedate"])
        pt = t.analyst_price_targets or {}
        rec = t.recommendations
        recs = rec.iloc[0].to_dict() if rec is not None and len(rec) else {}
        row = dict(date=dt.date.today().isoformat(), pt_current=pt.get("current"), pt_low=pt.get("low"), pt_high=pt.get("high"), pt_mean=pt.get("mean"), pt_median=pt.get("median"),
                   strong_buy=recs.get("strongBuy"), buy=recs.get("buy"), hold=recs.get("hold"), sell=recs.get("sell"), strong_sell=recs.get("strongSell"))
        out = upsert(DATA / "analyst_targets_daily.csv", [row], ["date"]); status.ok("analysts.yahoo", f"PT mean {pt.get('mean')}"); return out
    except Exception as e:
        status.fail("analysts.yahoo", e); return read_csv(DATA / "analyst_targets_daily.csv")


def update_news(status):
    try:
        rows = []
        for q in ["Prenetics", "%22IM8%22+supplement+OR+Beckham+IM8"]:
            r = get(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en", retries=2)
            if not r: continue
            for it in re.findall(r"<item>(.*?)</item>", r.text, re.S):
                title = _html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", (re.search(r"<title>(.*?)</title>", it, re.S) or [None, ""])[1])).strip()
                link = (re.search(r"<link>(.*?)</link>", it, re.S) or [None, ""])[1].strip()
                pub = (re.search(r"<pubDate>(.*?)</pubDate>", it, re.S) or [None, ""])[1].strip()
                src = _html.unescape((re.search(r"<source[^>]*>(.*?)</source>", it, re.S) or [None, ""])[1]).strip()
                try: pub = pd.to_datetime(pub).strftime("%Y-%m-%d")
                except Exception: pass
                rows.append(dict(date=pub, title=title[:200], source=src, link=link, query=q.split("+")[0].replace("%22", "")))
        if rows: out = upsert(DATA / "news.csv", rows, ["link"], sort=["date"]); status.ok("news.google_rss", f"{len(rows)} items"); return out
        status.skip("news.google_rss", "empty"); return read_csv(DATA / "news.csv")
    except Exception as e:
        status.fail("news.google_rss", e); return read_csv(DATA / "news.csv")
