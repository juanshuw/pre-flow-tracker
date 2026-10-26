"""SEC：EDGAR 申报流（Form 3/4/144、13D/G、6-K、F-1/EFFECT、S-8、20-F）、Form 4 逐笔、13F 持有人（全文检索 + 信息表解析）、失败交收 FTD、股本。"""
import datetime as dt, io, os, re, time, zipfile, html as _html
import pandas as pd
from .common import DATA, CIK, CUSIP, COMPANY_QUERY, UA_SEC, HISTORY_START, get, read_csv, upsert, load_json, save_json

SEC_H = {"Accept-Encoding": "gzip, deflate"}
CIK_INT = str(int(CIK))


def _sec_get(url, **kw):
    r = get(url, ua=UA_SEC, headers=SEC_H, **kw); time.sleep(0.15); return r


def update_filings(status):
    """EDGAR submissions JSON → data/edgar_filings.csv（全部表单，含链接）。"""
    try:
        r = _sec_get(f"https://data.sec.gov/submissions/CIK{CIK}.json")
        j = r.json(); rec = j["filings"]["recent"]
        rows = []
        for i in range(len(rec["form"])):
            fd = rec["filingDate"][i]
            if fd < HISTORY_START: continue
            acc = rec["accessionNumber"][i]; prim = rec["primaryDocument"][i]
            rows.append(dict(filing_date=fd, form=rec["form"][i], accession=acc, primary_doc=prim, description=(rec.get("primaryDocDescription") or [""] * 9999)[i] if rec.get("primaryDocDescription") else "",
                             url=f"https://www.sec.gov/Archives/edgar/data/{CIK_INT}/{acc.replace('-', '')}/{prim}", index_url=f"https://www.sec.gov/Archives/edgar/data/{CIK_INT}/{acc.replace('-', '')}/"))
        out = upsert(DATA / "edgar_filings.csv", rows, ["accession"], sort=["filing_date", "accession"])
        status.ok("sec.edgar_filings", f"{len(out)} filings; latest {out.filing_date.max() if len(out) else ''}"); return out
    except Exception as e:
        status.fail("sec.edgar_filings", e); return read_csv(DATA / "edgar_filings.csv")


def _xml_tags(tag, src):
    return [re.sub(r"\s+", " ", x).strip() for x in re.findall(rf"<{tag}>(.*?)</{tag}>", src, re.S)]


def _val(block, tag):
    m = re.search(rf"<{tag}>\s*(?:<value>)?\s*([^<]*)", block, re.S); return m.group(1).strip() if m else ""


def update_form4(status, filings):
    """解析新的 Form 4/3 原始 XML（去掉 xslF345X06/ 前缀）。"""
    try:
        old = read_csv(DATA / "edgar_form4.csv"); done = set(old.accession.astype(str)) if len(old) else set()
        todo = filings[filings.form.isin(["4", "4/A", "3", "3/A"]) & ~filings.accession.astype(str).isin(done)] if len(filings) else pd.DataFrame()
        rows = []
        for _, f in todo.iterrows():
            raw = f.url.replace("xslF345X06/", "").replace("xslF345X05/", "")
            r = _sec_get(raw)
            if not r: rows.append(dict(accession=f.accession, filing_date=f.filing_date, form=f.form, owner="", title="", tx_date="", code="", shares="", price="", acq_disp="", post_shares="", security="", note="fetch failed")); continue
            s = r.text
            owner = (_xml_tags("rptOwnerName", s) or [""])[0]; title = (_xml_tags("officerTitle", s) or [""])[0]
            isdir = (_xml_tags("isDirector", s) or ["0"])[0]; isoff = (_xml_tags("isOfficer", s) or ["0"])[0]; is10 = (_xml_tags("isTenPercentOwner", s) or ["0"])[0]
            rel = title or ("Director" if isdir in ("1", "true") else "") or ("10% owner" if is10 in ("1", "true") else "")
            n = 0
            for blk in re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", s, re.S):
                rows.append(dict(accession=f.accession, filing_date=f.filing_date, form=f.form, owner=owner, title=rel, tx_date=_val(blk, "transactionDate"), code=_val(blk, "transactionCode"), shares=_val(blk, "transactionShares"),
                                 price=_val(blk, "transactionPricePerShare"), acq_disp=_val(blk, "transactionAcquiredDisposedCode"), post_shares=_val(blk, "sharesOwnedFollowingTransaction"), security=_val(blk, "securityTitle"), note="")); n += 1
            for blk in re.findall(r"<derivativeTransaction>(.*?)</derivativeTransaction>", s, re.S):
                rows.append(dict(accession=f.accession, filing_date=f.filing_date, form=f.form, owner=owner, title=rel, tx_date=_val(blk, "transactionDate"), code=_val(blk, "transactionCode"), shares=_val(blk, "transactionShares"),
                                 price=_val(blk, "transactionPricePerShare"), acq_disp=_val(blk, "transactionAcquiredDisposedCode"), post_shares=_val(blk, "sharesOwnedFollowingTransaction"), security="[衍生] " + _val(blk, "securityTitle"), note="")); n += 1
            if n == 0:
                for blk in re.findall(r"<nonDerivativeHolding>(.*?)</nonDerivativeHolding>", s, re.S):
                    rows.append(dict(accession=f.accession, filing_date=f.filing_date, form=f.form, owner=owner, title=rel, tx_date="", code="H", shares="", price="", acq_disp="", post_shares=_val(blk, "sharesOwnedFollowingTransaction"), security=_val(blk, "securityTitle"), note="holding only"))
            if n == 0 and not rows: rows.append(dict(accession=f.accession, filing_date=f.filing_date, form=f.form, owner=owner, title=rel, tx_date="", code="", shares="", price="", acq_disp="", post_shares="", security="", note="no transactions"))
        if rows:
            out = upsert(DATA / "edgar_form4.csv", rows, ["accession", "tx_date", "code", "shares", "security"], sort=["filing_date", "accession"]); status.ok("sec.form4", f"+{len(todo)} filings parsed"); return out
        status.ok("sec.form4", "no new Form 4"); return old
    except Exception as e:
        status.fail("sec.form4", e); return read_csv(DATA / "edgar_form4.csv")


KEYWORDS = {"回购": r"repurchase program|share repurchase|buy-?back|repurchased", "增发/注册": r"registered direct offering|public offering of|private placement|pricing of (its|the|an) |closing of (its|the|an) .{0,40}offering|securities purchase agreement|placement agent",
            "权证": r"warrant", "业绩": r"financial results|quarter(ly)? results|shareholder letter|results for the (first|second|third|fourth) quarter|full[- ]year results", "合作/代言": r"partnership with|official .{0,30}partner|brand ambassador|ambassador|collaboration with",
            "人事": r"appoint(s|ed|ment) (of )?|resign(s|ed|ation)|steps? down|joins? (the )?board|named .{0,40}(chief|president|director)|new chief", "比特币": r"bitcoin|BTC", "指引": r"raises? (its )?(full[- ]year |fiscal |20\d\d )?(revenue )?guidance|guidance (range|to)|outlook for", "GC融资": r"General Catalyst|Customer Value Fund",
            "内部人买入": r"open[- ]market (share )?purchase|purchased .{0,60}shares .{0,40}open market|insider (purchase|buying)"}


def update_6k_flags(status, filings):
    """6-K/EFFECT/13D/13G/144/F-1 文本关键词标签 → data/edgar_events.csv。"""
    try:
        old = read_csv(DATA / "edgar_events.csv"); done = set(old.accession.astype(str)) if len(old) else set()
        want = filings[filings.form.str.match(r"^(6-K|EFFECT|SC 13D|SC 13D/A|SC 13G|SC 13G/A|144|F-1|F-1/A|F-3|F-3/A|424B\d|S-8|20-F|20-F/A)$", na=False) & ~filings.accession.astype(str).isin(done)] if len(filings) else pd.DataFrame()
        rows = []
        for _, f in want.head(40).iterrows():    # 每次最多处理 40 份，避免首跑过长
            txt = ""
            r = _sec_get(f.url, stream_cap=3_000_000) if f.form not in ("EFFECT",) else None
            if r:
                t = re.sub(r"(?is)<(script|style).*?</\1>", " ", r.text); t = _html.unescape(re.sub(r"(?s)<[^>]+>", " ", t)); txt = re.sub(r"\s+", " ", t)
            tags = [k for k, pat in KEYWORDS.items() if re.search(pat, txt, re.I)]
            pct = ""; snippet_extra = ""
            if f.form.startswith("SC 13"):
                raw = r.text if r else ""
                m = re.search(r"<percentOfClass>\s*([0-9]{1,3}\.?[0-9]*)", raw) or re.search(r"Percent of class[^0-9]{0,120}([0-9]{1,2}\.?[0-9]*)\s*%", txt, re.I); pct = m.group(1) if m else ""
                m2 = re.search(r"<aggregateAmountOwned>\s*([0-9,]+)", raw) or re.search(r"Aggregate amount beneficially owned[^0-9]{0,120}([0-9][0-9,]{3,})", txt, re.I)
                if m2: snippet_extra = f"持股 {m2.group(1)} 股"
            snippet = ""
            m = re.search(r"EXPLANATORY NOTE\s*(.{20,400}?)(?=\s*(INCORPORATION BY REFERENCE|SIGNATURE|EXHIBIT|$))", txt, re.S) or re.search(r"(On [A-Z][a-z]+ \d{1,2}, 20\d\d,? [^.]{20,300}\.)", txt)
            if m: snippet = re.sub(r"\s+", " ", m.group(1))[:320]
            rows.append(dict(accession=f.accession, filing_date=f.filing_date, form=f.form, tags="|".join(tags), pct_of_class=pct, snippet=(snippet_extra + " " + snippet).strip(), url=f.url))
        if rows:
            out = upsert(DATA / "edgar_events.csv", rows, ["accession"], sort=["filing_date", "accession"]); status.ok("sec.edgar_events", f"+{len(rows)} docs tagged"); return out
        status.ok("sec.edgar_events", "no new docs"); return old
    except Exception as e:
        status.fail("sec.edgar_events", e); return read_csv(DATA / "edgar_events.csv")


def update_13f(status, max_new=None):
    max_new = max_new or int(os.environ.get("MAX_13F_NEW", "60"))
    """13F 持有人：EDGAR 全文检索 (efts) 找出信息表里含公司名的 13F-HR 文件，直接抓该信息表，正则取含本 CUSIP 的条目。"""
    try:
        old = read_csv(DATA / "sec_13f_holders.csv"); done = set(old.accession.astype(str)) if len(old) else set()
        seen = load_json(DATA / "sec_13f_seen.json", {"ids": []}); seen_ids = set(seen["ids"])
        start = (pd.Timestamp(HISTORY_START) - pd.Timedelta(days=60)).strftime("%Y-%m-%d"); end = dt.date.today().strftime("%Y-%m-%d")
        hits = []
        for form_q in ("13F-HR", "13F-HR%2FA"):          # 注意：forms=13F-HR,13F-HR/A 的逗号写法会漏掉绝大多数命中，必须分开查
            for frm in range(0, 2000, 100):
                url = f"https://efts.sec.gov/LATEST/search-index?q={COMPANY_QUERY.replace(' ', '%20')}&forms={form_q}&dateRange=custom&startdt={start}&enddt={end}&from={frm}"
                r = _sec_get(url)
                if not r: break
                j = r.json(); hs = j.get("hits", {}).get("hits", [])
                hits += hs
                if len(hs) < 100: break
        rows = []; n = 0
        for h in hits:
            _id = h["_id"]
            if _id in seen_ids: continue
            src = h["_source"]; adsh = src.get("adsh") or _id.split(":")[0]; fname = _id.split(":")[1] if ":" in _id else ""
            ciks = src.get("ciks") or []; names = src.get("display_names") or []
            filer_cik = ciks[0] if ciks else ""; filer = names[0] if names else ""
            period = src.get("period_ending", ""); filed = src.get("file_date", ""); form = src.get("form", "")
            if not fname or not filer_cik: seen_ids.add(_id); continue
            url = f"https://www.sec.gov/Archives/edgar/data/{int(filer_cik)}/{adsh.replace('-', '')}/{fname}"
            r = _sec_get(url, stream_cap=60_000_000); n += 1
            if not r: rows.append(dict(accession=adsh, filer=filer, filer_cik=filer_cik, period=period, filed=filed, form=form, cusip="", shares="", value_usd="", put_call="", discretion="", note="fetch failed", url=url)); seen_ids.add(_id); continue
            s = r.text; found = 0
            for blk in re.findall(r"<(?:ns1:)?infoTable>(.*?)</(?:ns1:)?infoTable>", s, re.S | re.I):
                if CUSIP not in blk and "PRENETICS" not in blk.upper(): continue
                cus = _val(blk, r"(?:ns1:)?cusip"); shares = re.search(r"<(?:ns1:)?sshPrnamt>\s*([\d.]+)", blk); val = re.search(r"<(?:ns1:)?value>\s*([\d.]+)", blk); pc = re.search(r"<(?:ns1:)?putCall>\s*([^<]+)", blk); disc = re.search(r"<(?:ns1:)?investmentDiscretion>\s*([^<]+)", blk)
                title = re.search(r"<(?:ns1:)?titleOfClass>\s*([^<]+)", blk)
                rows.append(dict(accession=adsh, filer=filer, filer_cik=filer_cik, period=period, filed=filed, form=form, cusip=cus, title=title.group(1).strip() if title else "", shares=shares.group(1) if shares else "", value_usd=val.group(1) if val else "",
                                 put_call=pc.group(1).strip() if pc else "", discretion=disc.group(1).strip() if disc else "", note="", url=url)); found += 1
            if not found: rows.append(dict(accession=adsh, filer=filer, filer_cik=filer_cik, period=period, filed=filed, form=form, cusip="", title="", shares="", value_usd="", put_call="", discretion="", note="name matched but no infoTable row (text 13F or different CUSIP)", url=url))
            seen_ids.add(_id)
            if n >= max_new: break
        save_json(DATA / "sec_13f_seen.json", {"ids": sorted(seen_ids)})
        if rows:
            out = upsert(DATA / "sec_13f_holders.csv", rows, ["accession", "cusip", "put_call", "title"], sort=["period", "filed", "filer"]); status.ok("sec.13f", f"{len(hits)} hits; parsed {n} new filings"); return out
        status.ok("sec.13f", f"{len(hits)} hits; nothing new"); return old
    except Exception as e:
        status.fail("sec.13f", e); return read_csv(DATA / "sec_13f_holders.csv")


def update_ftd(status):
    """SEC 失败交收半月文件（cnsfailsYYYYMM{a,b}.zip）。未发布的文件下次再试。"""
    try:
        done = load_json(DATA / "sec_ftd_periods.json", {"done": []}); done_set = set(done["done"])
        today = dt.date.today(); periods = []
        m = pd.Timestamp(HISTORY_START).to_period("M")
        while m <= pd.Timestamp(today).to_period("M"):
            for half in ("a", "b"): periods.append(f"{m.strftime('%Y%m')}{half}")
            m += 1
        rows = []; got = []
        for p in periods:
            if p in done_set: continue
            r = _sec_get(f"https://www.sec.gov/files/data/fails-deliver-data/cnsfails{p}.zip", retries=1)
            if not r or len(r.content) < 1000 or r.content[:2] != b"PK": continue
            z = zipfile.ZipFile(io.BytesIO(r.content))
            for name in z.namelist():
                for line in z.read(name).decode("latin-1").splitlines():
                    parts = line.split("|")
                    if len(parts) >= 6 and parts[1] == CUSIP:
                        rows.append(dict(date=f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:8]}", qty=int(float(parts[3])), price=parts[5], period=p))
            got.append(p)
        if got:
            if rows: upsert(DATA / "sec_ftd.csv", rows, ["date"])
            # 只有“已过发布期”的文件才标记 done（b 半月通常次月中旬后出，a 半月在当月底后出）
            for p in got:
                yy, mm, half = int(p[:4]), int(p[4:6]), p[6]
                due = dt.date(yy, mm, 1) + (dt.timedelta(days=45) if half == "a" else dt.timedelta(days=62))
                if today > due: done_set.add(p)
            save_json(DATA / "sec_ftd_periods.json", {"done": sorted(done_set)})
            status.ok("sec.ftd", f"fetched {got}; +{len(rows)} rows")
        else: status.ok("sec.ftd", "no new period files")
        return read_csv(DATA / "sec_ftd.csv")
    except Exception as e:
        status.fail("sec.ftd", e); return read_csv(DATA / "sec_ftd.csv")


def update_shares(status):
    """公司事实：dei:EntityCommonStockSharesOutstanding（20-F 口径）。"""
    try:
        r = _sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json")
        j = r.json(); facts = j["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
        df = pd.DataFrame(facts)[["end", "val", "fy", "form", "filed"]].sort_values(["end", "filed"])
        out = upsert(DATA / "sec_shares_outstanding.csv", df, ["end", "filed", "val"]); status.ok("sec.shares", f"latest {df.iloc[-1].val:,} as of {df.iloc[-1].end}"); return out
    except Exception as e:
        status.fail("sec.shares", e); return read_csv(DATA / "sec_shares_outstanding.csv")
