# DATA_SOURCES.md — 資料來源規格書

> 版本 0.1 · 2026-08-15
> 原則：**只寫已驗證的來源。未驗證的一律標記，不得寫進 provider 實作。**

---

## 0. 驗證狀態圖例

| 標記 | 意義 |
|------|------|
| ✅ VERIFIED | 本 session（2026-08-15）實際請求成功並確認回傳結構 |
| ⚠️ UNVERIFIED | 端點存在於公開文件，但本環境無法驗證（403 / robots.txt / 需本機執行）→ **Phase 2 第一件事是在本機重測** |
| ❌ INVALID | 實測不存在（常見網路謠傳路徑），**禁止使用** |
| 🔒 LICENSED | 需授權，目前不採用，僅保留 Provider 介面 |

---

## 1. 資料來源總表

| # | 來源 | 用途 | 授權 | 狀態 |
|---|------|------|------|------|
| S1 | TWSE OpenAPI | 上市：指數、公司基本資料、月營收、財報、ESG、股利 | 政府開放資料 | ✅ |
| S2 | TWSE RWD Web API | 上市：**可指定日期**的歷史日線、三大法人、融資券 | 公開網站 | ✅ |
| S3 | TPEx OpenAPI | 上櫃：日收盤、三大法人、本益比、融資券 | 政府開放資料 | ⚠️ |
| S4 | TAIFEX OpenAPI | 期貨/選擇權：三大法人、大額交易人、每日行情 | 政府開放資料 | ⚠️ |
| S5 | MOPS 公開資訊觀測站 | 財報、重大訊息、法說會、月營收 | 公開 | ⚠️ |
| S6 | 政府資料開放平臺 data.gov.tw | 各資料集的授權條款與 metadata | 政府資料開放授權條款第 1 版 | ✅ |
| S7 | 財經媒體 RSS | 新聞情報 | 各站條款，僅取標題+摘要+連結 | ⚠️ |
| S8 | 美股資料（NVDA/AMD/AVGO…） | Lead-Lag 模型 | 待選型 | ⚠️ |
| S9 | 券商 / 資料商即時行情 | tick、委託簿 | 需付費授權 | 🔒 |

---

## 2. S1 — TWSE OpenAPI ✅

```
Base URL : https://openapi.twse.com.tw/v1
認證      : 無
格式      : JSON array（所有值皆為字串）
日期格式  : 民國年 YYYMMDD（例 1150813 = 2026-08-13）
```

### 2.1 已實測端點

#### `GET /exchangeReport/MI_INDEX` ✅
各類指數收盤行情。

實測回傳欄位：
```
日期, 指數, 收盤指數, 漲跌, 漲跌點數, 漲跌百分比, 特殊處理註記
```
實測樣本：
```json
{"日期":"1150813","指數":"發行量加權股價指數","收盤指數":"46021.48",
 "漲跌":"+","漲跌點數":"503.41","漲跌百分比":"1.11"}
{"日期":"1150813","指數":"寶島股價指數","收盤指數":"51102.16",
 "漲跌":"+","漲跌點數":"555.99","漲跌百分比":"1.10"}
```
→ 對應表：`index_prices`

#### `GET /exchangeReport/STOCK_DAY_ALL` ✅
全市場最新交易日個股成交資訊。

欄位：
```
Date, Code, Name, TradeVolume, TradeValue, OpeningPrice, HighestPrice,
LowestPrice, ClosingPrice, Change, Transaction
```
**限制（重要）**：無日期參數，只回傳最近一個交易日；非交易日回傳前一交易日且**無休市標記**。
→ 對應表：`daily_prices`（每日增量用）

#### `GET /opendata/t187ap03_L` ✅
上市公司基本資料，33 個欄位。實測首筆為 1101 台灣水泥。

完整欄位：
```
出表日期, 公司代號, 公司名稱, 公司簡稱, 外國企業註冊地國, 產業別, 住址,
營利事業統一編號, 董事長, 總經理, 發言人, 發言人職稱, 代理發言人, 總機電話,
成立日期, 上市日期, 普通股每股面額, 實收資本額, 私募股數, 特別股,
編制財務報表類型, 股票過戶機構, 過戶電話, 過戶地址, 簽證會計師事務所,
簽證會計師1, 簽證會計師2, 英文簡稱, 英文通訊地址, 傳真機號碼,
電子郵件信箱, 網址, 已發行普通股數或TDR原股發行股數
```

**這個端點是整個 News Intelligence 的基石**：`公司名稱` / `公司簡稱` / `英文簡稱` / `公司代號` 直接構成實體別名字典（見 `AI_ENGINE.md` §3.2）。
→ 對應表：`stocks`, `entity_aliases`

#### `GET /opendata/t187ap05_P` ✅（文件確認）
公開發行公司月營收彙總。
→ 對應表：`monthly_revenue`

#### `GET /opendata/t187ap45_L` ✅（文件確認）
上市公司股利分派情形 → 用於還原權值計算。
→ 對應表：`corporate_actions`

#### `GET /opendata/t187ap46_L_1` ~ `_21` ✅（文件確認）
上市公司 ESG 資訊揭露（溫室氣體、能源、水資源、廢棄物、人力發展、董事會、資安、供應鏈管理…）。
→ Phase 7+ 選配，可作為 Quality / ESG 因子來源。

### 2.2 已確認**不存在**的路徑 ❌

| 路徑 | 實測 | 說明 |
|------|------|------|
| `/v1/fund/T86` | 404 | 網路上常見的錯誤引用。正確的個股三大法人請用 §3 的 RWD 端點 |
| `/v1/exchangeReport/BFI82U` | 404 | 同上 |

> **教訓**：不要相信部落格文章裡的端點路徑，一律實測。

### 2.3 Rate limit

TWSE 未公開明確的 rate limit。採保守策略：**3 req/s、同一端點 5 分鐘內不重複請求（走 raw cache）**。回補作業排在非交易時段。

---

## 3. S2 — TWSE RWD Web API ✅（歷史回補主力）

```
Base URL : https://www.twse.com.tw/rwd/zh
格式      : JSON（{"stat","fields","data","date","title"}）
特性      : ★ 可指定日期參數 → 歷史回補的唯一可行路徑
```

### 3.1 `GET /afterTrading/STOCK_DAY` ✅

```
?date=YYYYMMDD&stockNo=2330&response=json
```
回傳該股票**該月份**的每日資料。

實測（`date=20260701&stockNo=2330`）：
```
stat   : "OK"
fields : ["日期","成交股數","成交金額","開盤價","最高價","最低價",
          "收盤價","漲跌價差","成交筆數","註記"]
data[0]: 115/07/01 · 37.5M 股 · 93.6B 元 · 開2495 高2505 低2475 收2505 · +95
data[1]: 115/07/02 · 35.9M 股 · 88.4B 元 · 開2450 高2480 低2445 收2465 · -40
```

**用途**：補洞。單股單月一次請求 → 全市場 10 年需 ~12 萬次請求，**不可作為主要回補手段**。

### 3.2 `GET /fund/T86` ✅（個股三大法人買賣超）

```
?date=YYYYMMDD&selectType=ALL&response=json
```
回傳**該日全市場**每檔股票的三大法人明細。

實測（`date=20260814`）19 個欄位：
```
證券代號, 證券名稱,
外陸資買進股數(不含外資自營商), 外陸資賣出股數(不含外資自營商), 外陸資買賣超股數(不含外資自營商),
外資自營商買進股數, 外資自營商賣出股數, 外資自營商買賣超股數,
投信買進股數, 投信賣出股數, 投信買賣超股數,
自營商買賣超股數,
自營商買進股數(自行買賣), 自營商賣出股數(自行買賣), 自營商買賣超股數(自行買賣),
自營商買進股數(避險), 自營商賣出股數(避險), 自營商買賣超股數(避險),
三大法人買賣超股數
```

**這是全市場單日型端點 → 回補 10 年只需 ~2,500 次請求。** 這是回補策略的關鍵。
→ 對應表：`institutional_trading`

> 注意：實測資料中出現 `00403A 主動統一升級50`、`00981A 主動統一台股增長` 等**主動式 ETF**，代號含英文字母。股票代號欄位不可假設為純數字四碼，`symbol VARCHAR(10)` 且 regex 為 `^[0-9A-Z]{4,10}$`。

### 3.3 待驗證的同族端點 ⚠️

| 端點 | 用途 | 對應表 |
|------|------|--------|
| `/afterTrading/MI_INDEX?date=&type=ALL` | 全市場單日 OHLCV（回補主力候選） | `daily_prices` |
| `/afterTrading/FMTQIK?date=` | 市場成交統計（成交量、金額、筆數、指數） | `market_stats` |
| `/afterTrading/BWIBBU_d?date=` | 個股本益比、殖利率、股價淨值比 | `valuation_daily` |
| `/afterTrading/MI_MARGN?date=` | 融資融券餘額 | `margin_short` |
| `/afterTrading/TWT93U?date=` | 借券賣出 | `margin_short` |
| `/afterTrading/TWTASU?date=` | 暫停交易證券 | `daily_prices.is_suspended` |

**Phase 2 Task 1**：逐一實測這些端點，把結果補進本表，未通過的不實作。

---

## 4. S3 — TPEx OpenAPI ⚠️

```
Base URL : https://www.tpex.org.tw/openapi/v1
認證      : 無（公開文件說明「plain GET is enough」）
本 session 狀態: 403（雲端 IP 被擋，非端點問題）
```

| 端點 | 用途 |
|------|------|
| `/tpex_mainboard_daily_close_quotes` | 上櫃每日收盤行情（OHLC、成交股數、金額、筆數、最佳買賣價、次日參考價與漲跌停價） |
| `/tpex_mainboard_peratio_analysis` | 上櫃本益比、殖利率、股價淨值比 |
| `/tpex_3insti_trading_daily`（名稱待確認） | 上櫃三大法人 |
| `/tpex_margin_balance`（名稱待確認） | 上櫃融資融券 |

**已知限制（與 TWSE OpenAPI 相同）**：
- 只回傳最新一個交易日，無日期參數
- 非交易日回傳前一交易日，無休市標記
- 民國年日期、值皆為字串
- **價格未還原權值**

**歷史回補**：需走 TPEx 網站的 `afterTrading` 端點（可帶 `date`），Phase 2 驗證。

> ⚠️ **Phase 2 必做**：在使用者本機（台灣 IP）重新驗證所有 TPEx 端點，把實際欄位名寫回本文件。雲端環境的 403 不代表端點不可用。

---

## 5. S4 — TAIFEX OpenAPI ⚠️

```
Base URL : https://openapi.taifex.com.tw
```

| 資料 | 用途 |
|------|------|
| 三大法人期貨/選擇權未平倉 | **台指期外資淨部位是判斷 Market Regime 的重要輸入** |
| 大額交易人未沖銷部位 | 主力方向 |
| 每日行情 | 台指期、選擇權 |

**已知限制**：OpenAPI 只提供最新交易日；歷史需走 `www.taifex.com.tw` 的下載頁。

Phase 5（Market Regime）之前不需要，列為 Phase 5 前置任務。

---

## 6. S5 — MOPS 公開資訊觀測站 ⚠️

```
https://mops.twse.com.tw
```

| 資料 | 重要性 | 說明 |
|------|--------|------|
| 綜合損益表 / 資產負債表 / 現金流量表 | ★★★ | 基本面因子的來源 |
| 重大訊息 | ★★★ | 事件偵測、**且帶精確公告時間戳** |
| 法人說明會資訊與簡報 | ★★ | RAG 知識庫素材 |
| 每月營收 | ★★★ | 台股特有的高頻基本面訊號 |
| 董監持股、股權分散表 | ★ | 籌碼面補充 |

### 6.1 為什麼 MOPS 是 look-ahead bias 的主戰場

財報的「所屬期別」（`period_end`，例如 2026-Q2）與「公告時間」（`announced_at`，例如 2026-08-14 17:32）差距可達 45 天以上。

**若用 `period_end` 對齊回測，等於在 2026-06-30 就知道 8 月才公布的財報。** 這會讓回測績效嚴重虛高。

**強制規範**：
```sql
-- financials 表必須有這兩個欄位，且 announced_at NOT NULL
period_end    DATE      NOT NULL
announced_at  TIMESTAMPTZ NOT NULL   -- 從 MOPS 公告時間取得，不可推估
```
若某筆資料無法取得可靠的 `announced_at`，則以「該期別法定申報截止日」作為保守替代，並在 `announced_at_is_estimated = true` 標記。**回測時可選擇排除估計值**。

台股法定申報期限（作為保守估計的依據）：
- Q1 / Q3 財報：季後 45 天內
- Q2 財報：季後 45 天內
- 年報：年度結束後 75 天內（一般公司）
- 月營收：次月 10 日前

### 6.2 存取方式

MOPS 為表單 POST 查詢，需處理：session、驗證碼（部分頁面）、HTML 表格解析。
**Phase 2 策略**：優先使用 TWSE OpenAPI 的 `t187ap` 系列（結構化 JSON，已含財報彙總），MOPS 爬取只用於 OpenAPI 未涵蓋的重大訊息與法說會。降低爬蟲脆弱性。

---

## 7. S6 — 授權條款

TWSE / TPEx / TAIFEX 的 OpenAPI 資料集多數登錄於 **政府資料開放平臺（data.gov.tw）**，適用 **政府資料開放授權條款第 1 版**：可自由重製、公開傳輸、改作、編輯及為商業目的利用，**需標示資料來源**。

**落實方式**：
1. 每筆資料在 DB 帶 `source` 欄位
2. API 回應 `meta.source` 陣列
3. Dashboard 頁尾與各資料區塊標示「資料來源：臺灣證券交易所 / 證券櫃檯買賣中心」
4. `docs/ATTRIBUTION.md` 列出所有資料集與其授權

> 個別資料集的授權條款可能不同，Phase 2 逐一在 data.gov.tw 上核對並記錄於本文件。

---

## 8. S7 — 新聞來源 ⚠️

### 8.1 採用原則（法遵優先）

| 允許 | 禁止 |
|------|------|
| 官方 RSS / Atom feed | 繞過付費牆 |
| 交易所與 MOPS 公告（本身就是公開資訊） | 忽略 robots.txt |
| 政府新聞稿 | 全文轉載後對外散布 |
| 公司官網新聞稿 | 高頻爬取造成對方負擔 |

**儲存策略**：資料庫存「標題 + 摘要（前 N 字）+ 原始連結 + 抓取時間」；**全文只在本機處理管線中短暫使用，不對外顯示、不長期保存全文**。UI 一律導向原始連結。這同時解決版權與儲存成本。

### 8.2 候選來源（Phase 4 逐一確認 robots.txt 與 RSS 可用性）

| 類別 | 來源 | 備註 |
|------|------|------|
| 官方公告 | TWSE 重大訊息、MOPS 重訊 | 最高可信度，有精確時間戳 |
| 官方公告 | 公司官網 Investor Relations | 一手資訊 |
| 政策 | 經濟部、國發會、金管會新聞稿 | 政策事件 |
| 財經媒體 | 各主要財經媒體之公開 RSS | 需逐一確認條款 |
| 國際 | 半導體/AI 產業媒體之公開 RSS | 用於 NVDA 等美股事件 |

### 8.3 來源可信度（`source_credibility`，影響 impact_score）

```
1.00  交易所 / MOPS 官方公告
0.95  公司官方新聞稿
0.90  政府部會新聞稿
0.75  主流財經媒體原創報導
0.60  媒體轉載/編譯
0.40  分析評論、專欄
0.20  未具名消息來源、傳聞
```
此表存於 `news_sources` 表，可調整、有版本。

---

## 9. S8 — 美股資料（Lead-Lag 模型）⚠️

用途：`ARCHITECTURE` 中的「美股 → 台股 Lead-Lag」。需要 NVDA / AMD / AVGO / MU / MSFT / AMZN / GOOGL / META 的日線與財報日期。

候選：
- `yfinance`（非官方 API，條款上僅供個人使用，穩定性不保證）
- Stooq / Nasdaq 官方公開檔案
- 付費 API（Polygon / Tiingo，個人方案便宜）

**Phase 7 決策點**，暫不實作。介面上仍為 `BaseMarketDataProvider` 的一個實作（`USEquityProvider`），故不影響架構。

> **硬性規範**：Lead-Lag 的所有數字（相關係數、beta、歷史反應幅度）**必須來自實際歷史統計**，且要附樣本數與統計顯著性。禁止手寫任何「NVDA +5% → 台積電 +1.4%」這類未經計算的數字。

---

## 10. S9 — 授權即時資料 🔒

**目前不採用。** 僅在程式碼中保留 `LicensedProvider(BaseMarketDataProvider)` 的抽象與其 `Capability` 宣告（`INTRADAY_TICK`, `ORDER_BOOK`, `REALTIME_QUOTE`）。

介面已預留的資料結構：`intraday_prices`、`order_books` 兩張表在 `ERD.md` 中定義但**不建立**（migration 中註解保留），待取得授權時啟用。

**明確禁止**：使用任何未確認授權的即時報價端點。本次驗證中 `mis.twse.com.tw` 被 robots.txt 阻擋，因此不納入。

---

## 11. 欄位對應（Normalizer 映射表）

### 11.1 通用轉換規則

| 來源型態 | 轉換 | 範例 |
|---------|------|------|
| 民國年日期 `1150731` | `date(1150731//10000 + 1911, ...)` | → `2026-07-31` |
| 民國年日期 `115/07/01` | 同上 | → `2026-07-01` |
| 千分位數字 `"37,500,000"` | 去逗號 → `int` | → `37500000` |
| 無資料 `"--"` / `""` / `"　"` | → `NULL` | |
| 漲跌方向 `"+"` / `"-"` / `"X"` / `""` | 併入數值正負；`X` 表示除權息無法比較 → `NULL` + flag | |
| 價格 `"2,505.00"` | `Decimal`（**不用 float**） | 金額計算一律 Decimal |
| 成交股數 | 轉為「股」；注意有些端點是「千股」 | 需逐端點確認 |

### 11.2 `daily_prices` 欄位映射

| 目標欄位 | TWSE STOCK_DAY_ALL | TWSE RWD STOCK_DAY | TPEx daily_close_quotes |
|---------|-------------------|-------------------|------------------------|
| `trading_date` | `Date` | `日期` | `Date` |
| `symbol` | `Code` | （查詢參數） | `SecuritiesCompanyCode` |
| `open` | `OpeningPrice` | `開盤價` | `Open` |
| `high` | `HighestPrice` | `最高價` | `High` |
| `low` | `LowestPrice` | `最低價` | `Low` |
| `close` | `ClosingPrice` | `收盤價` | `Close` |
| `volume` | `TradeVolume` | `成交股數` | `TradingShares` |
| `turnover` | `TradeValue` | `成交金額` | `TransactionAmount` |
| `trade_count` | `Transaction` | `成交筆數` | `TransactionNumber` |
| `change` | `Change` | `漲跌價差` | `Change` |
| `source` | `'TWSE'` | `'TWSE'` | `'TPEX'` |

> TPEx 欄位名為 Phase 2 待驗證，上表為公開文件描述，實測後修正。

### 11.3 `institutional_trading` 欄位映射（TWSE T86）

| 目標欄位 | 來源欄位 |
|---------|---------|
| `foreign_buy` | 外陸資買進股數(不含外資自營商) |
| `foreign_sell` | 外陸資賣出股數(不含外資自營商) |
| `foreign_net` | 外陸資買賣超股數(不含外資自營商) |
| `foreign_dealer_net` | 外資自營商買賣超股數 |
| `trust_buy` / `trust_sell` / `trust_net` | 投信買進/賣出/買賣超股數 |
| `dealer_self_net` | 自營商買賣超股數(自行買賣) |
| `dealer_hedge_net` | 自營商買賣超股數(避險) |
| `dealer_net` | 自營商買賣超股數 |
| `total_net` | 三大法人買賣超股數 |

---

## 12. 資料品質規則（`DataValidationEngine` 的規則表）

每條規則有 `severity`：`FATAL`（拒絕入庫，進 quarantine）/ `WARN`（入庫但標記）/ `INFO`（僅記錄）。

### 12.1 通用規則

| ID | 規則 | Severity |
|----|------|----------|
| G01 | 必填欄位不得為 NULL（symbol, trading_date, source） | FATAL |
| G02 | `trading_date` 必須存在於 `trading_calendar` 且 `is_trading_day = true` | FATAL |
| G03 | `trading_date` 不得為未來日期 | FATAL |
| G04 | 同一 (symbol, trading_date, source) 不得重複 | FATAL（改為 upsert） |
| G05 | `symbol` 必須存在於 `stocks` 表（否則觸發主檔更新後重試） | WARN |
| G06 | `ingested_at` 必須存在 | FATAL |

### 12.2 價格規則

| ID | 規則 | Severity |
|----|------|----------|
| P01 | `low <= open, close <= high` 且 `low <= high` | FATAL |
| P02 | 所有價格 > 0 | FATAL |
| P03 | 相對前一交易日漲跌幅絕對值 > 10.5%（台股漲跌幅限制 10%，留緩衝） | WARN（可能是除權息、首日上市、無漲跌幅限制股） |
| P04 | 相對前一交易日漲跌幅絕對值 > 50% | FATAL（幾乎必為資料錯誤） |
| P05 | `volume >= 0`；`volume = 0` 時 OHLC 應相等或為 NULL | WARN |
| P06 | `turnover` 與 `volume × 均價` 誤差 > 20% | WARN |
| P07 | 連續 N 日（預設 5）價格完全相同 | WARN（可能停牌或資料凍結） |

### 12.3 時序規則

| ID | 規則 | Severity |
|----|------|----------|
| T01 | 資料日期不得早於該股 `listing_date` | FATAL |
| T02 | 相鄰交易日之間不得有交易日缺漏（比對 `trading_calendar`） | WARN → 觸發補洞 job |
| T03 | `announced_at >= period_end`（財報公告不得早於期末） | FATAL |
| T04 | `ingested_at >= announced_at` | FATAL |
| T05 | 時區必須為 UTC 儲存（DB 層以 `TIMESTAMPTZ` 保證） | FATAL |

### 12.4 跨來源一致性

| ID | 規則 | Severity |
|----|------|----------|
| X01 | 同一 (symbol, date) 兩個來源的收盤價差異 > 0.5% | WARN + 記錄 `source_conflict` |
| X02 | 三大法人合計 ≠ 各項加總 | WARN |
| X03 | 指數成分股漲跌與指數方向嚴重背離（> 3σ） | INFO |

### 12.5 新鮮度規則

| 資料集 | `expected_lag`（交易日） | 超過即 stale |
|--------|------------------------|-------------|
| `daily_prices` | 收盤後 90 分鐘 | ✔ |
| `index_prices` | 收盤後 60 分鐘 | ✔ |
| `institutional_trading` | 收盤後 180 分鐘 | ✔ |
| `margin_short` | 收盤後 240 分鐘 | ✔ |
| `news` | 30 分鐘 | ✔ |
| `monthly_revenue` | 每月 11 日 | ✔ |
| `financials` | 法定申報截止日 + 3 日 | ✔ |

---

## 13. DataQualityScore 計算

每個 (dataset, symbol, trading_date) 產生一組分數，存 `data_quality_scores`。

```
Freshness    = 100 × max(0, 1 - (actual_lag - expected_lag) / expected_lag)
Completeness = 100 × (非 NULL 必要欄位數 / 必要欄位總數)
               × (實際交易日筆數 / 應有交易日筆數)   ← 用於區間查詢
Consistency  = 100 - 10 × (WARN 規則違反數) - 40 × (FATAL 規則違反數)
SourceQuality= 來源基準分（TWSE/TPEx = 100，媒體推導 = 70，Mock = 0）

Overall = 0.30×Freshness + 0.30×Completeness + 0.25×Consistency + 0.15×SourceQuality
```

範例輸出：
```
2330  2026-08-15
  Freshness       98.0
  Completeness    99.5
  Consistency     97.0
  SourceQuality  100.0
  ─────────────────────
  Overall         98.4
```

**API 契約**：任何回傳市場資料的端點，`meta.quality.overall` 必須存在。前端在 Overall < 90 時顯示黃色警示，< 70 時顯示紅色並要求使用者確認。

---

## 14. Phase 2 資料來源驗證清單（Definition of Done）

- [ ] 在台灣本機環境重測所有 ⚠️ 端點，更新本文件的狀態標記
- [ ] 每個採用的端點都有一份真實 response 存為 `tests/fixtures/{provider}/{endpoint}.json`
- [ ] 每個端點的欄位映射寫入 §11 並有對應的 normalizer unit test
- [ ] 交易日曆完成 10 年回填並用「當日全市場成交量 > 0」交叉驗證
- [ ] 全市場日線回補 10 年，`data_gaps` 表為空或每個缺口都有記錄原因
- [ ] `DataQualityScore` 對全市場最近 60 交易日的 Overall 中位數 > 95
- [ ] 每個資料集的授權條款在 data.gov.tw 上核對並記錄於 `docs/ATTRIBUTION.md`
- [ ] `MockProvider` 在 `APP_ENV=production` 下啟動即 raise 的測試通過

---

## Sources

本文件中標記 ✅ 的端點於 2026-08-15 實際請求驗證。參考資料：

- [臺灣證券交易所 OpenAPI](https://openapi.twse.com.tw/)
- [盤後資訊 > 個股日成交資訊 ｜ 政府資料開放平臺](https://data.gov.tw/dataset/11549)
- [STOCK_DAY_ALL endpoint usage and limits](https://twmarketdata.com/en/answers/twse-stock-day-all-endpoint-en)
- [tpex_mainboard_daily_close_quotes usage and limits](https://twmarketdata.com/en/answers/tpex-mainboard-daily-close-quotes-en)
- [證券櫃檯買賣中心 OpenAPI](https://www.tpex.org.tw/openapi/)
- [TWSEMCPServer — TWSE/TPEx/TAIFEX OpenAPI 整合專案](https://github.com/twjackysu/TWSEMCPServer)
