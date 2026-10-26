"""每日任务入口：抓取所有数据源（每个独立 try/except，失败不影响其他）→ 汇总 dashboard.json。
用法：python run_daily.py [--skip finra.otc_monthly,social.reddit] [--no-build]
"""
import argparse, datetime as dt, sys, time, traceback
import pandas as pd
from tracker.common import DATA, Status, read_csv
from tracker import prices, finra, sec, nasdaq, others, build


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--skip", default=""); ap.add_argument("--no-build", action="store_true"); a = ap.parse_args()
    skip = set(x.strip() for x in a.skip.split(",") if x.strip()); st = Status(); t0 = time.time()

    def run(name, fn, *args):
        if name in skip: st.skip(name, "skipped by flag"); return None
        try: return fn(st, *args)
        except Exception as e:
            st.fail(name, f"{e!r}"); traceback.print_exc(); return None

    daily = run("prices.daily", prices.update_daily); daily = daily if daily is not None else read_csv(DATA / "prices_daily.csv")
    run("prices.peers", prices.update_peers)
    run("prices.intraday", prices.update_intraday, daily)
    run("finra.regsho", finra.update_regsho); run("finra.weekly", finra.update_weekly); run("finra.monthly", finra.update_monthly); run("finra.si", finra.update_short_interest)
    filings = run("sec.filings", sec.update_filings); filings = filings if filings is not None else read_csv(DATA / "edgar_filings.csv")
    run("sec.form4", sec.update_form4, filings); run("sec.events", sec.update_6k_flags, filings); run("sec.13f", sec.update_13f); run("sec.ftd", sec.update_ftd); run("sec.shares", sec.update_shares)
    run("nasdaq.cb", nasdaq.update_circuit_breaker); run("nasdaq.threshold", nasdaq.update_threshold); run("nasdaq.institutional", nasdaq.update_institutional); run("nasdaq.insiders", nasdaq.update_insiders); run("nasdaq.si", nasdaq.update_short_interest)
    run("options", others.update_options); run("social.stocktwits", others.update_stocktwits); run("social.reddit", others.update_reddit); run("borrow", others.update_borrow); run("analysts", others.update_analysts); run("news", others.update_news)

    rows = pd.DataFrame(st.rows); rows["run_ts"] = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    log = DATA / "run_status.csv"; old = read_csv(log)
    pd.concat([old, rows], ignore_index=True).tail(2000).to_csv(log, index=False)
    print(rows[["source", "status", "note"]].to_string(index=False))
    if not a.no_build:
        try:
            out = build.build(st.rows); k = out["kpis"]
            print(f"\nDASHBOARD {out['meta']['last_trade_date']} close {k['close']} ({k['ret_pct']}%) vol x{k['vol_x_adv20']} short% {k['short_pct']} offexch% {k['offexch_pct']} | stage {k['stage']} score {k['score']} | flags {out['latest']['flags']}")
        except Exception as e:
            print("BUILD FAILED", repr(e)); traceback.print_exc(); sys.exit(1)
    print(f"done in {time.time() - t0:.0f}s; fails: {[r['source'] for r in st.rows if r['status'] == 'fail']}")


if __name__ == "__main__": main()
