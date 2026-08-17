# Recorded fixtures — real TWSE responses

Every file here is a **verbatim** response captured from a live TWSE endpoint —
the first six on **2026-08-15**, the four corporate action files on
**2026-08-17**. Nothing is hand-authored, synthesised or idealised. They exist so
that the ingestion pipeline can be verified against the real shape of the data —
including the parts that are annoying:

| Fixture | Endpoint | Real-world quirks it preserves |
|---------|----------|-------------------------------|
| `holiday_schedule_2026.json` | `openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule` | Entries that are **not** closures (`國曆新年開始交易日`, `農曆春節前最後交易日`); settlement-only days; `<br>` tags inside descriptions |
| `stock_day_2330_202607.json` | `www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY` | ROC dates `115/07/01`; comma thousands separators; **leading-space zero** `" 0.00"` on 2026-07-27; `notes` documenting the `**` face-value-change marker |
| `mi_index_1150814.json` | `openapi.twse.com.tw/v1/exchangeReport/MI_INDEX` | Sign carried in a separate `漲跌` field, not in the number |
| `stock_day_all_1150814.json` | `openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL` | Alphanumeric ETF codes (`00400A`); `Change` as a signed decimal string |
| `t86_20260814.json` | `www.twse.com.tw/rwd/zh/fund/T86` | **Trailing-space padded names** (`"力積電          "`); 19 institutional columns; negative values |
| `t187ap03_L.json` | `openapi.twse.com.tw/v1/opendata/t187ap03_L` | Padded par value (`"新台幣                 10.0000元"`); full-width dash `"－ "` meaning null; ROC-free `YYYYMMDD` listing dates |
| `twt48u_all_1150817.json` | `openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL` | **`尚未公告` in a numeric field** (`SubscriptionPricePerShare`) — parsed as a number it becomes a rights issue priced at zero; `''` and `'0'` both used for "no cash"; English field names on a Chinese-content endpoint |
| `twt49u_2026.json` | `www.twse.com.tw/rwd/zh/exRight/TWT49U` | ROC dates as `115年01月06日`; thousands separators in prices; `N/A` and `''` as distinct empty forms; a URL embedded inside a parenthesised field |
| `twtauu_2019_2026.json` | `www.twse.com.tw/rwd/zh/reducation/TWTAUU` | ROC dates as `108/01/02`; `'--'` as a null token; **symbol padded with trailing spaces before a comma** (`'2429  ,20181219'`); note TWSE's own spelling `reducation` |
| `t187ap45_L_1150816.json` | `openapi.twse.com.tw/v1/opendata/t187ap45_L` | `<br>` inside `決議（擬議）進度`; `'0.0'` and `'0.01000000'` as inconsistent zero forms; empty `股東會日期` when only the board has resolved |

## Why these are not "mock data"

These are genuine exchange responses. `MockProvider` fabricates values and is
forbidden in production; `ReplayProvider` serves *these* bytes, so the parsed
result is real TWSE data with a recorded transport. Records ingested this way
keep `source='TWSE'` and additionally carry `transport='REPLAY'` in
`raw_ingestions`, so provenance never claims a live fetch that did not happen.

## Coverage limits

The `stock_day_all` and `t187ap03_L` fixtures are **truncated samples** (8 and 3
records). They are sufficient for contract and pipeline tests; they are not a
market snapshot. Any statistic in `PHASE_2_REPORT.md` states its record count
explicitly rather than implying full coverage.


## The four corporate action fixtures, and why there are four

No single endpoint can drive a price adjustment. This was established by
fetching them, not by reading documentation:

| Endpoint | Gives | Does not give |
|----------|-------|---------------|
| `TWT48U_ALL` (預告表) | Ex-date **before** it happens, decomposed into cash, stock ratio and subscription terms | Reference prices; anything already past |
| `TWT49U` (計算結果表) | Ex-date, pre-close and reference price, combined 權值+息值 | Any announcement date |
| `t187ap45_L` (股利分派情形) | Board and shareholder meeting dates — the knowledge time | **No ex-date at all** |
| `TWTAUU` (減資恢復買賣) | Capital reductions with pre/post prices and the reason | The cash amount returned per share |

Two consequences worth stating plainly.

`t187ap45_L` cannot on its own adjust anything, because it never says when the
adjustment takes effect. Its value is the announcement dates, without which
`announced_at` would have to be estimated from the ex-date — which is the exact
look-ahead this project's bitemporal design exists to prevent.

`TWTAUU` reports prices but not the cash returned. For 退還股款 the observable
equation has two unknowns — the cash per share and the share ratio — and one
price change, so the two cannot be separated from this source alone. Any
`cash_returned_per_share` derived from it would be a guess wearing a number's
clothes, and the field stays null until a source that actually publishes it is
added.

Sampling: `twtauu_2019_2026.json` is complete — 174 rows is the entire
2019–2026 history and only 20 KB. The other three are sampled, one row per
quirk, because the live responses are up to 2.4 MB. The sampling script's
selection criteria are the quirk list in the table above.
