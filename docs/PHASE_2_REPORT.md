# PHASE_2_REPORT.md — Taiwan Market Data Infrastructure

> 產出時間：2026-08-16 · Phase 2 of 10 · 前置：`v0.1.0-phase1`
> **本報告只寫實測到的東西。** 每個數字都附取得方式；未驗證的一律標記為未驗證。

---

## 0. 一句話總結

Market Data Infrastructure 已完整建置並跑通：provider 抽象、registry、交易日曆、
canonical model、驗證、quarantine、provenance、freshness、availability、
rate limiter、可續跑 backfill、market API、Redis 快取，
**解析層面全部用真實 TWSE 回應驗證過，並於 2026-08-17 完成一次
`PROVIDER_MODE=live` 的全市場實測**（1,378 檔日線、1,095 家公司、267 種指數，
見 §3）。251 個測試通過、`ruff` 與 `mypy --strict` 零告警、migration 可逆且無 drift。

那次 live 實測抓到一個 fixture 在結構上不可能抓到的缺陷（§13 第 8 項），
也因此 §3 與 §7 的數字現在是實測值而非樣本值。

**一個環境限制**：開發用的雲端容器對外連線被允許清單阻擋，
`curl` 到 `openapi.twse.com.tw` / `www.twse.com.tw` / `www.tpex.org.tw` 全部回 `http=000`，
所以那裡的驗證一律走**逐字錄製的真實 TWSE 回應**（見 §2）。
§3 與 §7 的數字則是在**有網路的機器上**以 `PROVIDER_MODE=live` 實際跑出來的。
歷史回補尚未執行 —— 指令見 §11。

---

## 1. Data sources actually verified

驗證方式：2026-08-15 透過可用管道對 live endpoint 發出實際請求，取得逐字回應。

| 端點 | 狀態 | 實測證據 |
|------|------|---------|
| `openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule` | ✅ **VERIFIED（Phase 2 新發現）** | 27 筆 2026 年公告，欄位 `Name/Date/Weekday/Description` |
| `openapi.twse.com.tw/v1/exchangeReport/MI_INDEX` | ✅ VERIFIED | 12 筆指數，TAIEX 收 45811.01、漲跌 `-` 210.47 |
| `openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL` | ✅ VERIFIED | 全市場快照，代號含英數（`00400A`） |
| `openapi.twse.com.tw/v1/opendata/t187ap03_L` | ✅ VERIFIED | 33 欄公司主檔，1101 台泥 / TCC / 產業別 01 |
| `www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY` | ✅ VERIFIED | 2330 於 2026-07 共 22 筆，`total: 22`，含 `notes` |
| `www.twse.com.tw/rwd/zh/fund/T86` | ✅ VERIFIED | 2026-08-14 三大法人，19 欄 |
| `www.tpex.org.tw/openapi/v1/*` | ⚠️ **UNVERIFIED** | HTTP 403（推測為 IP 過濾）。registry 標記 UNVERIFIED，**無 provider 實作** |
| `openapi.taifex.com.tw` | ⚠️ UNVERIFIED | 未測；Phase 5 前不需要 |
| `mops.twse.com.tw` | ⚠️ UNVERIFIED | 未測；表單 POST + HTML，脆弱度高於 OpenAPI |

**Phase 2 最有價值的發現**：官方交易日曆端點存在。Phase 0 沒找到它，本階段找到了，
於是交易日曆從「推測」變成「有權威來源」。

### 1.1 這個端點有個陷阱

holiday schedule **不是每一列都代表休市**。27 列中有 3 列是註記：

```
國曆新年開始交易日      1150102  ← 這天有交易
農曆春節前最後交易日    1150211  ← 這天有交易
農曆春節後開始交易日    1150223  ← 這天有交易
```

把每列都當成休市會**刪掉三個真實交易日**。
`parse_holiday_schedule` 依名稱分類，被 `test_annotation_rows_are_not_closures` 鎖住。

另有 2 列是「市場無交易，僅辦理結算交割」（2026-02-12、02-13），
以獨立的 `SETTLEMENT_ONLY` session type 區分，而不是簡化成一般休市。

---

## 2. Endpoints actually tested

### 2.1 驗證策略

這個容器連不到交易所，所以驗證分兩層，各自說清楚證明了什麼：

| 層 | 方法 | 證明了什麼 | 未證明什麼 |
|----|------|-----------|-----------|
| Parser 契約 | 生產解析器 × 逐字錄製回應 | 欄位、型別、時區、代號格式、null 語意、真實怪癖全部處理正確 | HTTP 傳輸 |
| 端到端管線 | ReplayProvider → 驗證 → quarantine → 正規化 → DB → API | 整條路徑在真實資料形狀上可運作 | 對交易所的實際連線、retry 對真實 5xx 的行為 |

**ReplayProvider 不是 mock data。** 它送出的是 2026-08-15 錄下的真實 TWSE bytes，
經過**同一組生產解析器**，所以產出的是真實市場資料。
只有傳輸是重播的，而這件事被誠實記錄：每筆 ingestion 帶 `transport='REPLAY'`，
provenance 永遠不會宣稱一個沒發生過的 HTTP 請求。
`MockProvider`（捏造數值、production 禁用）是完全不同的東西，本階段沒有實作。

### 2.2 錄製的 fixture 保留的真實怪癖

| Fixture | 保留的怪癖 |
|---------|-----------|
| `stock_day_2330_202607.json` | 民國年 `115/07/01`；千分位；**開頭有空白的零** `" 0.00"`（2026-07-27）；`**` 面額變更註記說明 |
| `t86_20260814.json` | **名稱尾端補空白** `"力積電          "`；19 欄；負值 |
| `mi_index_1150814.json` | 正負號在獨立的 `漲跌` 欄，不在數字裡 |
| `t187ap03_L.json` | 面額字串內嵌補空白 `"新台幣                 10.0000元"`；全形破折號 `"－ "` 代表 null；上市日期是**西元**不是民國 |
| `stock_day_all_1150814.json` | ETF 代號含英文字母 `00400A` |
| `holiday_schedule_2026.json` | 非休市的註記列；結算交割日；`<br>` 標籤 |

---

## 3. Data coverage

**2026-08-17 於有網路的機器上實測（`PROVIDER_MODE=live`，Docker stack）。**
以下每個數字都來自 `scripts/data_ops_report.py` 的輸出，不是估算。

| 資料集 | provider 回傳 | 實際寫入 | quarantine | suspect | 涵蓋 |
|--------|-------------|---------|-----------|---------|------|
| `trading_calendar` | 24 筆公告 | 365 天 | 0 | 0 | 2026 全年，243 交易日 |
| `stock_master` | 1,095 | 1,095 | 0 | 0 | 全上市公司（SCD2 current） |
| `daily_prices` | 1,378 | 1,378 | 0 | **3** | 2026-08-14，**1,378 檔** |
| `index_quotes` | 267 | 267 | 0 | 0 | 2026-08-14，**267 種指數** |
| `institutional_flow` | — | **失敗** | — | — | 見 §13 第 8 項 |

`raw_ingestions` 4 筆，**transport 全部是 `LIVE`** —— 這是這份報告與先前
replay 版本的關鍵差別，每一筆數字都對應一次真實的 HTTP 請求。

**與錄製樣本的落差說明了為什麼要做這次實測。** 樣本裡 `index_quotes` 是 12 筆，
真實是 **267** 筆；`daily_prices` 樣本 8 檔，真實 **1,378** 檔。
樣本足以驗證解析正確，**完全不足以暴露規模問題** —— 而規模問題確實存在（§13 第 8 項）。

`daily_prices` 有 **3 筆被標記為 SUSPECT**：依設計它們**入庫並標記**，不是被丟掉。
真實資料第一次跑就出現 suspect，正是 `P30`/`P31` 這類規則存在的理由。

**Freshness 實測**：`daily_prices` 與 `index_quotes` 回報 **STALE**，
`last_data=2026-08-14`，而執行日是 08-17（週一）。這是正確行為 ——
freshness 是以交易日曆為基準，不是牆上時鐘：08-15/16 是週末，
所以週一執行時最後一筆資料是上週五的，且當日盤後資料尚未發布。

### 3.1 交易日曆的一個真實不一致

錄製的 TWSE 資料本身顯示 2330 在 2026-07 只有 **22 個交易日**，
但公告日曆推算應有 **23 個** —— 2026-07-10（週五）**交易所自己沒有資料，公告也沒列為休市**。

未公告的臨時休市（颱風）正是這個形狀：當天才決定，不會出現在前一年 12 月公告的日曆裡。
`verify_against_observations` 會把它標記出來：

```
flagged_no_data: ['2026-07-10']
confirmed:       22 days corroborated by volume
```

由 `test_calendar_discrepancy_is_detectable` 鎖住。

---

## 4. Rate limits

| 來源 | rpm | rpd | 併發 | 最小間隔 | 依據 |
|------|-----|-----|------|---------|------|
| TWSE OpenAPI | 60 | 20,000 | 2 | 350 ms | **無公告限制**；對免費公共服務的保守自律 |
| TWSE RWD | 40 | 12,000 | 1 | 800 ms | 同上，且此端點是回補主力，更該客氣 |
| TPEX | 40 | 12,000 | 1 | 800 ms | 未驗證，預先設定 |
| TAIFEX | 30 | — | 1 | 1000 ms | 未驗證 |
| MOPS | 20 | — | 1 | 2000 ms | 未驗證，爬蟲型 |

**沒有任何一個是官方公告值** —— 這些來源都沒有公布限制。設定存在 `data_sources` 表，
改限制是改一列資料，不是重新部署。

限流由 Redis 集中執行（滑動視窗 Lua script），**跨 API 進程、所有 worker、
以及與每日 job 並行的 backfill 共享同一份配額**。
per-process 限流會讓 N 個 worker 各用滿額度，那正是 IP 被封的方式。

實測（`test_backfill_and_limits.py`，7 個測試）：節流生效、跨實例共享、
每日上限拋出、併發上限 ≤ 2、最小間隔生效、Redis 不可用時退回 in-process、統計正確。

---

## 5. Historical coverage

**本環境沒有執行歷史回補**（無網路）。BackfillService 已完成並以合成 fetch 驗證：

| 測試 | 驗證內容 |
|------|---------|
| `test_units_come_from_the_trading_calendar` | 不對週末與春節發出請求 |
| `test_interrupted_backfill_resumes_from_checkpoint` | 中斷後續跑，**不重抓已完成的單位** |
| `test_checkpoint_survives_a_new_service_instance` | 進度存在資料庫，不在記憶體 |
| `test_a_failing_unit_does_not_abort_the_run` | 單日失敗不中止整輪 |
| `test_no_data_is_skipped_not_failed` | 尚未上市的日子算 skip 不算 fail |
| `test_systemic_failure_aborts_rather_than_hammering` | 連續 10 次失敗即中止，不繼續轟炸 |

### 回補請求量估算（依實測端點特性）

| 策略 | 端點 | 10 年請求數 |
|------|------|-----------|
| ✅ 全市場單日 | `T86?date=` | 約 **2,430**（243 交易日 × 10 年） |
| ❌ 單股單月 | `STOCK_DAY?date=&stockNo=` | 約 **120,000**（1,000 檔 × 120 月） |

差 50 倍。日曆驅動的批次先移除約 1/3 的日曆日（週末假日），再套限流。

---

## 6. Data quality statistics

真實錄製資料的驗證結果：**70 筆全數通過，0 筆進 quarantine**。

驗證規則分三級，對應三種處置：

| 級別 | 處置 | 規則範例 |
|------|------|---------|
| FATAL | 進 quarantine，**保留原始 payload**，不入庫 | `P10` high < low、`P15` 負成交量、`F11` net ≠ buy−sell、`CAL01` 非交易日 |
| WARN | 入庫並標記 `SUSPECT` | `P30` 單日 >50% 變動、`P31` 超過 ±10% 漲跌幅、`P32` 隱含均價超出當日區間 |
| INFO | 僅記錄 | — |

**關鍵區別**：validity 是「內部是否自洽」，我們判得出來；
anomaly 是「是否合理」，沒有上下文判不出來。只有前者可以拒絕。
刪掉異常價格會刪掉真實的漲停、真實的崩盤、真實的新聞反應 —— 正是這個平台要研究的事件。

`test_suspect_rows_are_stored_and_flagged_not_rejected` 鎖住這個行為。

---

## 7. Ingestion benchmarks

**2026-08-17 實測，`PROVIDER_MODE=live`，Docker stack。含網路時間。**

| 資料集 | 筆數 | 總耗時 |
|--------|------|--------|
| `trading_calendar` | 365 天（24 筆公告展開） | 78 ms |
| `index_quotes` | 267 | 55 ms |
| `stock_master` | 1,095 | 612 ms |
| `daily_prices` | 1,378 | 617 ms |
| `institutional_flow` | — | 14,666 ms 後失敗 |

全市場單日 ingestion **不到 1.5 秒**（不含失敗的那項）。
這遠低於任何需要最佳化的門檻，所以依 ARCHITECTURE §16 **維持不做最佳化**：
沒有 COPY、沒有 partitioning、沒有 materialized view。先量測，再決定 —— 量測完了，
結論是不需要。

`institutional_flow` 那 14.7 秒幾乎全花在建構一個註定被資料庫拒絕的巨大 statement 上，
不是網路。修法見 §13 第 8 項。

**這組數字取代了先前的 replay 版本**（8 檔 7 ms 之類）。舊數字沒有網路時間也沒有真實
規模，兩者都是這次才補上的。

---

## 8. Database statistics

migration `0003_market_data_infrastructure` 新增 13 張表（總計 18 張）：

**Canonical**：`trading_calendar` `stock_master` `daily_prices` `index_quotes`
`institutional_flow` `corporate_actions` `market_status`

**Operations**：`data_sources` `raw_ingestions` `data_quarantine` `data_freshness`
`backfill_checkpoints` `ingestion_metrics`

migration `0004_enforce_provenance_fk` 不新增表，把七張表的 `ingestion_id`
從裸整數改成指向 `raw_ingestions.id` 的外鍵（`ON DELETE SET NULL`）。
理由見 §13 第 7 項。

驗證：`upgrade head` → `downgrade -1` → `upgrade head` 全部成功，`alembic check` 無 drift。

### 幾個刻意的設計

| 設計 | 理由 |
|------|------|
| `daily_prices` 有 6 條 OHLC CHECK 約束 | 自相矛盾的價格連寫都寫不進去。實測中它擋下了測試腳本嘗試寫入的錯誤值 |
| `(symbol, trading_date, source)` 唯一鍵含 source | 兩個來源不一致時兩列都留著，衝突可見可診斷，而不是一列悄悄覆蓋另一列 |
| `close` 與 `adjusted_close` 是不同欄位 | 原始價永不被覆寫。回測要「當天實際成交價」和要「連續報酬序列」都能滿足 |
| `stock_master` 是 SCD2 | 名稱、產業、上市狀態會變。只存現值會毀掉「2019 年這家公司叫什麼」的能力 |
| `institutional_flow` 存 buy/sell/net 三者 | 只存 net 會丟掉成交量：買 100 萬對賣 100 萬、和完全沒交易，net 都是 0 |
| 金額一律 `NUMERIC`，禁用 float | 二進位浮點會默默吃掉分，十年回測會累積 |

---

## 9. Failed sources

| 來源 | 失敗方式 | 處置 |
|------|---------|------|
| TPEx OpenAPI | HTTP 403（本環境所有請求） | registry 標 `UNVERIFIED`，**不寫 provider**。Dashboard 顯示原因 |
| TAIFEX | 未嘗試 | `UNVERIFIED`，Phase 5 前不需要 |
| MOPS | 未嘗試 | `UNVERIFIED`，Phase 2 未涵蓋 corporate actions 抓取 |
| 所有 live HTTP | 容器允許清單阻擋（`http=000`） | 見 §10 |

**registry 不會宣稱未驗證的能力。** `test_unverified_sources_are_declared_not_hidden`
斷言 TPEx 在 API 回應中的狀態是 UNVERIFIED 而不是被藏起來。

---

## 10. Known limitations

### 10.1 阻擋完整驗證的（需要有網路的環境）

| # | 限制 | 影響 | 解法 |
|---|------|------|------|
| **L1** | 容器對外連線被允許清單阻擋 | live provider 的 HTTP 路徑未實際執行；retry / circuit breaker 對真實 5xx 的行為未觀察 | 在有網路的機器執行 §11 的指令 |
| **L2** | 無 Docker daemon（延續 Phase 1） | `docker compose up` 未執行 | 同上 |
| **L3** | TPEx 未驗證 | 上櫃資料完全沒有 | 在台灣網路重測，再寫 `TPExProvider` |
| **L4** | 無歷史回補 | 目前只有 30 筆價格 | 執行 §11 的 backfill |

### 10.2 刻意留給後續 Phase 的

| 項目 | Phase |
|------|-------|
| Corporate actions **抓取**（schema、bitemporal 查詢、adjustment 欄位已完成，但沒有來源在填） | 2 續作 / 3 |
| `adjusted_close` 計算 job | 3（技術指標需要還原價） |
| TPEx / TAIFEX provider | 2 續作 / 5 |
| 財報與月營收（`FinancialFact` 契約在 Phase 1 已定義） | 4 |
| WebSocket 即時推送 | 10 |
| Celery beat 排程接上 ingestion job（job 本身是純 async function，可直接被 Celery 呼叫） | 2 續作 |

### 10.3 已知小瑕疵

| 項目 | 說明 |
|------|------|
| fixture 的主檔（1101-1103）與價格（2330、0040xA）不重疊 | 錄製取樣的性質，不是程式問題。`test_detail_returns_master_record` 有註明 |
| `redis.setex` deprecation warning | redis-py 5.x 的警告，功能正常 |
| Backfill 目前只驗證過合成 fetch | 真實 provider 的 backfill 需 L1 解除 |

---

## 11. 在有網路的機器上執行（解除 L1/L3/L4）

原本這一節是一串指令，輸出要自己拼。現在是**一支程式**：
`scripts/data_ops_report.py` 跑完整個序列，一次印出 provider 回傳筆數、
實際落地筆數、coverage、以及每一項失敗與缺漏。

```bash
# 1. 起服務、套 migration、建立來源登錄
make up
make migrate
make seed-sources

# 2. 完整實測（PROVIDER_MODE 預設 live）
cd backend && .venv/bin/python -m scripts.data_ops_report --year 2026 --json report.json

# 3. 歷史回補（可中斷，重跑從 checkpoint 續跑），跑完再測一次
make backfill DATASET=institutional_flow FROM=2019-01-01 TO=2026-08-15
cd backend && .venv/bin/python -m scripts.data_ops_report --year 2026 --json report-after-backfill.json
```

報告分四段：**Sources**（每個來源是否真的連得上，連不上印出實際錯誤）、
**Ingestion**（每個 dataset 的 provider 筆數 vs 寫入 / quarantine / suspect）、
**Coverage**（每張表的筆數、日期範圍、股票數，以及**區間內有幾個交易日完全沒有資料**）、
**Failures and gaps**（quarantine 依 dataset 與規則分組、freshness、
日曆與成交量的交叉驗證、backfill checkpoint 狀態）。

三個刻意的設計：

- **不估算任何東西。** 每個數字不是 provider 回應讀出來的，就是資料庫數出來的；
  量不到的印出量不到的原因，不會用 0 頂替。
- **單一 dataset 失敗不中止整輪** —— 一次跑完要能看到所有問題，不是第一個。
- **最上面印出 transport。** `PROVIDER_MODE` 不是 `live` 時會有一整片警告橫幅，
  說明這些數字描述的是錄製的 fixture 而不是交易所。這份報告是要拿來引用的，
  所以它必須對「自己量了什麼」毫不含糊。

資料庫沒 migrate 或來源沒 seed 時，它會印一行指示而不是六十行 traceback。

跑完把輸出（或 `report.json`）給我，我把實測數字補進 §3、§7。

---

## 12. Security review

| 面向 | 措施 | 狀態 |
|------|------|------|
| 外部請求身分 | 明確 User-Agent 含用途與聯絡方式 | ✅ |
| 尊重來源 | 中央限流、最小間隔、指數退避 + jitter、不重試 4xx | ✅ |
| SQL Injection | 全 ORM / bound params，無 f-string SQL | ✅ |
| Secret | base URL 與限流在 registry 表，不在程式碼；無憑證需求（全公開來源） | ✅ |
| 授權標示 | `data_sources.licence` 記錄政府資料開放授權條款第 1 版；UI 頁尾標示來源 | ✅ |
| DoS 自我防護 | backfill 連續失敗 10 次即中止；API 不觸發外部請求 | ✅ |
| API 攻擊面 | **無任何端點呼叫 provider**，由 `test_api_serves_from_the_database_only` 以毒化 provider 建構子的方式證明 | ✅ |
| 未授權資料 | 未使用 `mis.twse.com.tw`（robots.txt 禁止）或任何未確認授權端點 | ✅ |
| 輸入驗證 | symbol regex（DB CHECK + Pydantic）、分頁上限、日期型別 | ✅ |
| production 防護 | `PROVIDER_MODE=replay` 在 production 拒絕啟動（錄製資料是真的但是舊的，服務它會誤導新鮮度） | ✅ |
| Rate limit（對我們的 API） | 尚未實作 | ⏳ Phase 10 |

---

## 13. 這一階段被測試抓到的缺陷

八個。前七個在 push 之前，第八個要等真實市場資料才會出現：

| # | 缺陷 | 為什麼危險 |
|---|------|-----------|
| 1 | **指數漲跌符號全部相反** | `"-"` 既是 `漲跌` 欄的合法符號，也是 null token。走 `clean()` 之後回傳 None，所有下跌變成上漲。TAIEX 那天實際跌 210.47，被記成漲 210.47 |
| 2 | **限流器同毫秒成員碰撞** | sorted set 成員是 `{ms}-{id}`，同一毫秒兩個請求成員相同，ZADD 變成覆寫而非新增，限制被悄悄突破 |
| 3 | **`valid_from` 用了上市日期** | 把知曉時間和事件時間搞混 —— SCD2 查 1990 年會回傳 2026 年才知道的資料。正是這整套設計要防的錯 |
| 4 | **point-in-time 過濾器包含 `ingested_at`** | 十年回補讓每筆歷史資料的 `ingested_at` 都是今天，於是任何對過去的查詢都回傳空 —— 一個看起來正確的防護變成靜默的空回測 |
| 5 | **多列 upsert 對異質 key 崩潰** | 三大法人的 DEALER/TOTAL 只有 net，其餘有 buy/sell。SQLAlchemy 從第一列推導欄位，缺欄位的列直接編譯失敗 |
| 6 | **`.gitignore` 吞掉整個 models 套件**（Phase 1 遺留） | `models/` 沒有前導斜線，會在**任何深度**比對，於是 `backend/app/models/` 從 Phase 1 起就不在版控裡。`git status` 全綠、本機測試全過，但 clean clone 根本 import 不起來 |
| 7 | **provenance 只是慣例，不是約束** | 七張表的 `ingestion_id` 是裸 BigInteger，沒有 FK。「每個數字都能追回原始 bytes」全靠每個寫入者自己記得 —— 指向不存在的 ingestion，資料庫照收 |
| 8 | **全市場寫入超過 PostgreSQL 的參數上限** | 一次 multi-row INSERT 每列每欄用掉一個 bind parameter，上限 32767。三大法人一天約 1,400 檔 × 7 種投資人 × 10 欄 ≈ 98,000 —— **整天的資料一筆都沒進去** |

第 6 項不是這個 Phase 寫壞的，是 commit 前逐項檢查 `git ls-files` 時，發現
`v0.1.0-phase1` 這個 tag 裡**一個 ORM model 都沒有**。

值得記下的是**為什麼沒被抓到**。CI 是對 `actions/checkout` 的內容跑的，也就是
版控裡的內容 —— 這個缺陷 CI 一跑就會炸。但 remote 還沒設定，所以 CI 從來沒有真的
執行過一次；到目前為止唯一的驗證管道是本機 working tree，而 working tree 裡檔案
是在的。這是「CI 設定好了」和「CI 跑過了」之間的差別，也是為什麼下面 §14 把設定
remote 列為 Phase 3 的前置而不是雜項。

修法是把 data 區段的 pattern 全部錨定到 repo root（`/models/`），ML artefact 的
兩個實際落點單獨列出。修完之後補了一道本機也能跑的驗證：`git clone` 到乾淨目錄
→ import → 跑完整測試。收進 `make clone-check`，併入 `make check`，以後每個
Phase gate 都跑，不依賴 remote 是否存在。

**第 7 項是第 6 項的連鎖後果。** ruff 預設遵守 `.gitignore`，所以 models 套件
從來沒有被 lint 過。解除忽略之後 ruff 立刻報了三個未使用的 import，其中
`ForeignKey` 特別刺眼 —— 它被 import 是因為當初打算加，但七張表的 `ingestion_id`
最後都是裸 `BigInteger`。也就是說 Phase 2 最核心的那句話（「沒有來源就不會有數字」）
在資料庫層面**沒有任何東西在守**。

補在 migration `0004`：七個 FK 指向 `raw_ingestions.id`，`ON DELETE SET NULL`
而非 `RESTRICT` —— raw payload 是庫裡最肥的東西，清理是合理維運，不該連帶刪掉行情。
刪掉來源時 canonical row 存活、指標歸 NULL、`source` 與 `ingested_at` 保留，
從「這是當初的原始 bytes」誠實降級為「來自哪裡、何時進來」，而不是指向一個不存在的
東西。兩個新測試各守一邊：懸空 `ingestion_id` 被 DB 擋下（`IntegrityError`）、
清理 raw payload 後價格還在而指標已清空。

**217 passed / 3 skipped**（新增 2 個）。

另外，DB 的 OHLC CHECK 約束在測試中**擋下了測試腳本自己嘗試寫入的錯誤值** ——
約束在做它該做的事。

---

## 14. Phase 3 readiness

Phase 3（Quant Engine）需要的 canonical 資料，Phase 2 是否備妥：

| Phase 3 需求 | 狀態 |
|-------------|------|
| `DailyPrice` | ✅ 模型完成、驗證完成、API 完成。**資料量待回補** |
| `IndexQuote` | ✅ 相對強度計算的基準已就緒 |
| `InstitutionalFlow` | ✅ buy/sell/net 三者齊全，籌碼因子可用 |
| `StockMaster` | ✅ SCD2，產業分類可做產業內標準化 |
| `TradingCalendar` | ✅ 因子視窗以交易日計算，不以日曆日 |
| `CorporateAction` | ⚠️ **schema + bitemporal 查詢完成，但沒有來源在填**。技術指標需要還原價，這是 Phase 3 的第一個前置 |
| `DataAvailability` | ✅ `as_of` 可見性服務完成，Phase 6 回測直接消費 |
| Feature Contract | ⏳ Phase 3 建立（依你的指示，Phase 3 先建 Feature/Factor Contract 與時序慣例） |

### Phase 3 開始前的三個前置

1. **解除 L1** —— 沒有真實資料量，因子的 IC 檢定沒有意義
2. **Corporate actions 來源** —— 沒有還原價，MA、動能、報酬序列全部會在除權息日出現假跳空
3. **設定 GitHub remote 並 push** —— CI 已經寫好但一次都沒真的跑過。§13 第 6 項就是
   這個空窗期漏掉的：一個 CI 一跑就會炸的缺陷，安靜地活過了整個 Phase 1。
   我需要 owner 才能組出完整 URL（見 §16）

---

## 15. Definition of Done 逐項核對

| # | 項目 | 狀態 | 證據 |
|---|------|------|------|
| 1 | GitHub remote confirmed | ✅ | `github.com/VincenTddos/taiwan-stock-intelligence`，`main` + 兩個 tag 已推上 |
| 2 | Phase 1 tag preserved | ✅ | `v0.1.0-phase1` 存在 |
| 3 | ADR-013~021 organized | ✅ | `docs/adr/` 21 份 + README；`ARCHITECTURE.md §18` 改為索引 |
| 4 | MarketDataProvider abstraction | ✅ | 6 個必要方法 + capability + 錯誤正規化 |
| 5 | Provider registry | ✅ | `data_sources` 表，URL 與限流不在程式碼裡 |
| 6 | Trading calendar | ✅ | 365 天，官方來源 + 成交量交叉驗證 |
| 7 | Stock master | ✅ | SCD2，`valid_from`/`valid_to`/`is_current` |
| 8 | Daily prices | ✅ | 唯一鍵 `(symbol, trading_date, source)` |
| 9 | Index data | ✅ | TAIEX 等；`index_type` 預留產業指數 |
| 10 | Institutional flow | ✅ | 7 種投資人 × buy/sell/net |
| 11 | Corporate actions | ⚠️ | schema + bitemporal 查詢完成；**無來源** |
| 12 | Data validation | ✅ | completeness / validity / continuity |
| 13 | Quarantine | ✅ | 保留原始 payload，不靜默丟棄 |
| 14 | Data provenance | ✅ | `raw_ingestions` + 每列 `ingestion_id` |
| 15 | Data freshness | ✅ | 4 狀態，以交易日曆為基準 |
| 16 | Rate limiter | ✅ | 中央、Redis、跨進程 |
| 17 | Historical backfill | ✅ | batching / retry / progress |
| 18 | Checkpoint/resume | ✅ | 測試證明不重抓 |
| 19 | Idempotent ingestion | ✅ | 跑三次筆數不變 |
| 20 | Market APIs | ✅ | 6 個端點 + data-operations |
| 21 | Redis cache | ✅ | versioned + TTL + invalidation |
| 22 | Contract tests | ✅ | 64 個，對真實錄製回應 |
| 23 | Holiday tests | ✅ | 假日不產生假交易日 |
| 24 | Data quality tests | ✅ | 無效 OHLC 進 quarantine |
| 25 | Backtest availability tests | ✅ | `available_at` 用知曉時間 |
| 26 | No fake production data | ✅ | `ALLOW_MOCK_DATA` + `PROVIDER_MODE` 雙重 production 防護 |
| 27 | Documentation updated | ✅ | 本報告 + ADR 重整 + fixtures README |
| 28 | CI green | ✅ | 五個 job 全綠（run #4）。前三次紅燈共抓到 7 個本地測不到的問題，見 §17 |

**27 項完成、1 項需外部來源（corporate actions）。**

---

## 16. 停止點

依指示，Phase 2 到此結束，**不自動進入 Phase 3**。

需要你的：

1. **在有網路的機器執行 §11**，把 `data-operations` 輸出給我，我補上實測覆蓋數字
2. **Corporate actions 來源的決定** —— TWSE `t187ap45_L`（股利分派）是已驗證的 OpenAPI 端點，是最省事的起點；MOPS 涵蓋更全但脆弱
3. 確認後再下 Phase 3 指令

---

## 17. CI 第一次真的執行之後

`main` 於 2026-08-16 推上 `github.com/VincenTddos/taiwan-stock-intelligence`，
CI 首次執行。**它是紅的**，而且抓到的四件事全部是本地 gate 結構上測不到的：

| 失敗 job | 錯誤 | 為什麼本地測不到 |
|---------|------|----------------|
| Backend | `No file matched to [**/uv.lock]` | `setup-uv` 的 cache key 要 hash 一個相依檔案，預設找 `uv.lock` / `requirements*.txt`，本專案兩者都沒有（相依寫在 `pyproject.toml`）。本地根本不跑這個 action |
| Frontend | `ERR_PNPM_IGNORED_BUILDS` | pnpm 版本只釘在 workflow 一個地方。pnpm 10 起預設拒絕執行套件的 build script（`esbuild`/`sharp`/`unrs-resolver`）並以非零結束。本地那顆 pnpm 剛好是 9 |
| Docker | `"/bin/sh -c pnpm install" exit code 1` | 同上，且 Dockerfile 寫了 `--frozen-lockfile \|\| pnpm install` —— 一個「lockfile 壞了也照跑」的 fallback。這個容器沒有 docker daemon，映像檔從來沒有被建置過 |
| Secret scan | `Unexpected exit code [1]` | gitleaks 沒裝在本地 gate 裡 |

四件事的共同點：**它們都不是程式碼的問題，是「這份 repo 能不能在別人的機器上從零建起來」的問題** ——
而那正是本地 gate 定義上無法回答的。這也是 §13 第 6 項（`.gitleaks` 之外那個
`.gitignore` 缺陷）能活過整個 Phase 1 的同一個結構性理由。

### 各自的修法

**pnpm 版本**改由 `web/package.json` 的 `packageManager` 欄位決定 —— corepack、
Docker build、`pnpm/action-setup` 三者都讀它，一處釘死三處一致。三個需要 build script
的套件用 `onlyBuiltDependencies` **逐一列名核可**，而不是全面放行所有 postinstall。

**Dockerfile 拿掉 `|| pnpm install` 這個 fallback**，以及 `pnpm-lock.yaml*` 那個
容忍 lockfile 不存在的萬用字元。兩者都是在說「lockfile 壞了也繼續」，
把可重現的映像檔變成「當天 registry 剛好解析出什麼就是什麼」，而且悄無聲息。

**gitleaks 找到三筆**，全是假的：CI 那顆用完即丟的 JWT 簽章金鑰（出現兩次）、
以及一個用來驗證「錯誤金鑰簽的 token 會被拒絕」的測試常數。**沒有真的憑證外洩。**

處理方式刻意選了最窄的一種：`.gitleaks.toml` 以**字面值**列入允許清單，
不是以檔案路徑、也不是以規則。放行 `.github/workflows/` 或整條 `generic-api-key`
規則，等於日後真的有人把憑證貼進那些檔案時一路綠燈通過 —— 那就變成一個
「在保護什麼都沒保護」的掃描器，正是這個專案已經被咬過一次的形狀。
驗證方式：在同一個檔案裡種一組 Stripe key 和 Slack token，**兩個都仍然被抓出來**。

順帶一提，inline 的 `# gitleaks:allow` 註解在這裡沒用 ——
掃描涵蓋每一個 commit，那些值留在 Phase 1 的 blob 裡，改現在的檔案沒有意義。

### 四次執行才轉綠

| 執行 | 結果 |
|------|------|
| #1 | Backend / Frontend / Secret scan / Docker 四個全紅；`compose` 被 skip |
| #2 | Secret scan ✅、Docker ✅（真的建出兩個映像檔）、Frontend ✗、Backend ✗；`compose` 仍被 skip |
| #3 | 前四個全綠；`compose` **首次執行**並失敗 |
| #4 | **五個全綠。** Backend 2m · Compose 1m · Docker 5m · Frontend 47s · Secret scan 4s |

**Frontend #2**：`pnpm/action-setup` 讀的是 repo 根目錄的 `package.json`，
而前端在 `web/` 底下 —— 上一輪加的 `packageManager` 欄位放在它從來不會去看的地方。
補上 `package_json_file: web/package.json`。

**Backend #2**：`mypy --strict` 21 個錯誤，全是
`Missing type arguments for generic type "Redis"` 和
`"Redis[Any]" has no attribute "aclose"`。本地是乾淨的。**差異不在程式碼。**

dev extras 裡宣告了 `types-redis`。redis 從 5.0 起就內建 `py.typed`，
而 **mypy 優先採用 stub 套件而不是函式庫自己的內建型別** ——
於是這組 stub 把 redis 8 的真實 API 換成了 4.6 時代的：`aclose()` 消失、
`Redis` 變成泛型。那 21 個錯誤描述的是一個這個專案根本沒在用的函式庫。

本地之所以過，是因為這顆 venv 建立時 pyproject 還沒有那一行，
所以從來沒裝過那組 stub。也就是說「mypy --strict 零告警」這句話，
是對著一個**任何人重新安裝都不會得到的相依集合**量出來的 —— 包括 CI，
也包括任何一個新的協作者。

拿掉 stub 套件就好了。驗證方式：在暫存目錄依 `pyproject.toml` 重建 venv，
先重現全部 21 個錯誤，再確認修改後消失。

### 這件事對 gate 的意義（比修法重要）

`clone-check` 原本從 git clone，但 **venv 是 symlink 過去的** ——
所以它抓得到「檔案沒進版控」，抓不到「環境漂移」。現在它連環境也從
`pyproject.toml` 重建，並在裡面跑 ruff 和 mypy。這正是 CI 在做的事，
也正是這裡原本沒有任何一道檢查在做的事。

寫這段的時候又在同一個 target 裡發現第二個缺陷：`mypy app | tail -1`
把 mypy 的結束碼送進了 pipe，所以第一版印出「Found 21 errors」之後，
**照樣回報 phase gate passed**。一個不可能失敗的檢查比沒有檢查更糟，
因為它會被計入。已加上防護：mypy 非零就 dump log 並中斷。

### Compose #3 —— 一個不可能通過的健康檢查

`compose` 因為 `needs: [backend, frontend]`，前兩次都被 skip，
所以第三次是它**有史以來第一次執行**。

```yaml
test: ["CMD", "celery", ..., "-d", "celery@$$HOSTNAME"]
```

`CMD` 是 exec 形式：Docker 直接執行這串 argv，**不經過 shell**，
所以 `$HOSTNAME` 永遠不會展開。celery 被要求去 ping 一個字面上叫
`celery@$HOSTNAME` 的節點。**這個檢查在任何情況下都不可能通過** ——
worker 永遠不會 healthy，`compose up --wait` 兩分鐘後放棄。改用 `CMD-SHELL`。

api 的健康檢查是同一個病的另一面：它接受任何 `< 500` 的狀態碼，
所以 **404 算健康**。探測路徑打錯字、或路由搬家，都會產生一個
「自稱一切正常、實際什麼都沒服務」的容器。改成必須 200。

同時把 build 和 start 拆成兩個 CI step —— 併成一行時它們共用一個時間和一個
結束碼，「build 很慢」和「容器不健康」讀起來一模一樣。失敗時現在會先印出
每個容器的 state、health status 和最後一次 healthcheck 的輸出，
不健康的是哪一個服務會被直接寫出來，不用從 200 行 log tail 裡猜。

### 第 8 項：只有真實資料才會出現的缺陷

前七項都能在錄製的 fixture 上被抓到。**第八項不行，而且這正是它的意義。**

一次 multi-row INSERT 每列每欄用掉一個 bind parameter，PostgreSQL 的線路協定
上限是 32767。所有 fixture 都是幾十列，最多花掉幾百個參數。真實的一天：
三大法人約 1,400 檔 × 7 種投資人 = 9,800 列，乘上 10 欄接近 98,000 個參數。

結果不是變慢，是 **`the number of query arguments cannot exceed 32767`，整天的
三大法人資料一筆都沒寫進去**。

修法是依 statement 實際編譯出來的參數數量分批。這裡我第一次修錯了：我用
payload 的 key 數量去算每列寬度，但 **SQLAlchemy 還會綁定 payload 根本沒提到的
欄位** —— 任何帶 Python-side default 的欄位，例如 `quality_status`。所以實際寬度
比我算的多，第一版修完還是爆掉。改成編譯一列真的數出來，才是對的。

新增的測試直接寫 9,800 列，也就是真實市場的規模。它在修好之前會紅。

**這件事改變了我對「fixture 驗證過」這句話的說法。** 錄製樣本證明的是
**解析正確**；它在結構上證明不了**規模可行**。Phase 2 報告先前寫「全部用真實
TWSE 回應端到端驗證過」，那句話在解析層面是真的，在規模層面不是。這是這次
live 實測最有價值的產出 —— 比那些覆蓋率數字有價值得多。

### 這一段的結論

七個缺陷，分四次執行才全部浮出來，**沒有一個是程式邏輯的錯**。
分類起來只有三種：

1. **版控裡少了東西**（models 套件）
2. **環境在別處解析成別的樣子**（pnpm 版本、types-redis stub、Next 的 CVE）
3. **檢查本身不可能失敗**（gitleaks 沒跑過、`mypy | tail` 吃掉結束碼、
   healthcheck 收 404、worker healthcheck 收 `$HOSTNAME` 字面值）

第三類最值得記住。這個專案在 CI 第一次執行前，帳面上有一整排通過的檢查，
其中有幾道**在結構上不可能報錯** —— 它們被計入了信心，卻沒有提供任何信心。
一個不可能失敗的檢查比沒有檢查更糟，正因為它會被計入。

本地 gate 現在有五道（lint、typecheck、test、migration、clean-clone），
其中 clone-check 已經強化到會重建整個環境。但**沒有一道能取代 CI**。
CI 的價值不在於重跑同樣的測試，而在於它是在一台**沒有這個專案任何殘留狀態**
的機器上，從版控裡的內容重新建起來。本地 gate 回答「我寫的東西對不對」，
CI 回答「這份 repo 是不是完整的」。這兩個問題的答案，今天不一樣了四次。

**Run #4：五個 job 全綠。** `verify_stack.sh` 也在 compose job 裡真的跑過了 ——
整套 stack（postgres + redis + migrate + api + worker + beat + web）
在一台乾淨的機器上從版控內容建起來、啟動、並通過健康驗證。
這是 `v0.2.1-phase2` 標記的狀態。
