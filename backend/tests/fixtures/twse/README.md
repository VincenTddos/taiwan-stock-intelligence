# Recorded fixtures — real TWSE responses

Every file here is a **verbatim** response captured from a live TWSE endpoint on
**2026-08-15**. Nothing is hand-authored, synthesised or idealised. They exist so
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
