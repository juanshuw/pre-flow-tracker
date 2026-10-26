# IM8 / PRE 参与方交易行为追踪（GitHub Actions 每日抓取 + GitHub Pages 看板）

追踪 Prenetics（NASDAQ: PRE，IM8 母公司）所有市场参与方的交易行为：散户、做市商/高频、暗池机构、13F 机构、空头、内部人与大股东、公司本身、卖方分析师、期权。穷尽**免费公开**数据源，每天自动抓取、增量落库（CSV 提交回仓库）、生成静态看板。

看板：`https://<user>.github.io/pre-flow-tracker/`（仓库 Settings → Pages → Source: main /docs）

## 数据源（全部免费、免 key）

| 频率 | 数据 | 来源 | 回答什么 |
|---|---|---|---|
| 日 | 日线 OHLCV（Yahoo 主，Stooq / Nasdaq 补缺） | yfinance、stooq.com、api.nasdaq.com | 价格、成交、换手、均量倍数、收盘位置、均线、RSI、布林 |
| 日 | 1 分钟线 → VWAP、收盘/VWAP、开盘与尾盘 30 分钟占比、最大 1 分钟成交 | yfinance（仅回溯 7 天，每天跑即累积） | 卖压出现在一天中的什么时候；弱收盘 |
| 日 | 逐日卖空量 / 场外（TRF）成交量 | FINRA Reg SHO（API `regShoDaily` + CNMS 日文件） | 卖空占场外（<35% 多头卖；>55% 做市商供货）、场外占比（>55% 疑似大宗） |
| 日 | 卖空熔断（Rule 201）名单、Reg SHO 门槛名单 | nasdaqtrader.com | 盘中跌 ≥10% 触发；持续 FTD |
| 日 | 期权链 OI / 成交 | yfinance | 衍生品对冲规模 |
| 日 | Stocktwits 消息流与关注数、Reddit 帖子 | 公开 API | 散户关注度 |
| 日 | 借券费率 / 可借量 | iBorrowDesk（IBKR 口径，云端可达性不稳定） | 空头拥挤度 |
| 日 | 分析师评级动作、目标价、评级分布 | yfinance | 卖方动作 |
| 日 | 新闻 | Google News RSS | 事件 |
| 周 | 每个暗池（ATS）与每家做市商/批发商在该股的周成交股数、笔数、金额 | FINRA OTC Transparency（API `weeklySummary`，匿名 + dateRangeFilters） | 机构换手（暗池占比 vs 基线）、零售流（Citadel 单列）、大宗平台 |
| 月 | 月度 OTC 合计 | FINRA `monthlySummary` | 备查 |
| 双周 | 空头持仓、DTC | FINRA `consolidatedShortInterest`（另抓 Nasdaq 口径交叉） | 空头在加还是在减 |
| 半月 | 失败交收 FTD | SEC `cnsfails` 文件 | 裸空/交收压力 |
| 事件 | 全部 EDGAR 申报（Form 3/4/144、13D/G、6-K、F-1/EFFECT、S-8、20-F） | SEC submissions JSON | 内部人、大股东、公司动作（Form 4 逐笔解析；6-K 关键词标签：回购/增发/权证/业绩/合作/人事；13D/G 持股比例） |
| 季 | 13F 持有人 | SEC 全文检索 efts + 信息表解析（按 CUSIP） | 机构名单与季度变动 |
| 快照 | Nasdaq 机构持股、内部人汇总、空头 | api.nasdaq.com（云端可能被挡，best-effort） | 交叉核对 |

拿不到（付费或不存在）：逐笔买卖方身份（美股不存在）；逐笔成交/交易所拆分（Polygon/Databento）；Ortex/S3 借券；Nasdaq RTAT 零售流；FactSet/LSEG 全球持仓。

## 看板内容

1. KPI：收盘/涨跌、成交倍数、收盘/VWAP、尾盘占比、场外与卖空占比、空头持仓、13F 持股、RSI、阶段与见底打分、当日标签。
2. 见底打分：12 项指标（价格锚、超卖、恐慌日、枯竭、下跌日收盘 vs VWAP、吸筹/派发、下跌日卖空占比、空头趋势、暗池占比、新买方、板块、散户关注）→ 阶段 A 派发 / B 主跌 / C 恐慌或枯竭 / D 筑底确认。
3. 参与方矩阵：每类参与方的最新信号、读法、来源、截至日期。
4. 图表：价格 + 成交（按卖空占比着色）、周度暗池占比、空头持仓/DTC、FTD、期权 OI、关注度；各暗池周成交表。
5. 表：13F 本期 vs 上期、Nasdaq 机构快照、Form 4 逐笔、Nasdaq 内部人、EDGAR 申报（带标签）、新闻、分析师、数据源状态；全部 CSV 可下载。

## 运行

```bash
pip install -r requirements.txt
python run_daily.py                      # 全量（首跑约 5–10 分钟：回填 2025 年以来历史）
python run_daily.py --skip sec.13f       # 跳过某些源
python -m http.server 8765 --directory docs   # 本地看板 http://localhost:8765
```

GitHub Actions：`.github/workflows/daily.yml`，每天 02:00 UTC（北京 10:00）+ 工作日 11:00 UTC 补抓；也可在 Actions 页手动触发。数据以 CSV/JSON 提交回仓库（`data/`、`docs/data/`）。

可选 Secrets：`SEC_UA`（SEC 要求 UA 含联系方式，如 `name email`）；`FINRA_CLIENT_ID` / `FINRA_CLIENT_SECRET`（FINRA Public 凭证，$0，提高配额；匿名也能跑）。

## 换标的

`tracker/common.py` 顶部或环境变量：`TRACK_SYMBOL`、`TRACK_CIK`、`TRACK_CUSIP`、`TRACK_COMPANY_QUERY`、`TRACK_SHARES`；价值锚在 `data/anchors.json` 覆盖。

## 文件

```
run_daily.py            入口（每源独立 try/except → run_status）
tracker/common.py       路径、HTTP、CSV 增量合并、交易日历
tracker/prices.py       日线/1 分钟线/同行；派生指标
tracker/finra.py        Reg SHO 日度、OTC 周度/月度、空头持仓；周度汇总
tracker/sec.py          EDGAR 申报、Form 4、6-K 标签、13F、FTD、股本
tracker/nasdaq.py       熔断名单、门槛名单、机构/内部人/空头快照
tracker/others.py       期权、Stocktwits/Reddit、借券、分析师、新闻
tracker/build.py        dashboard.json（标签、打分、参与方矩阵）
docs/index.html         看板（Chart.js，静态）
data/                   全部原始 CSV/JSON（提交回仓库）
```
