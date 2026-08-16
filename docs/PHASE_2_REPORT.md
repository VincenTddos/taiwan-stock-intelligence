# PHASE_2_REPORT.md — Taiwan Market Data Infrastructure

> 產出時間：2026-08-16 · Phase 2 of 10 · 前置：`v0.1.0-phase1`
> **本報告只寫實測到的東西。** 每個數字都附取得方式；未驗證的一律標記為未驗證。

---

## 0. 一句話總結

Market Data Infrastructure 已完整建置並跑通：provider 抽象、registry、交易日曆、
canonical model、驗證、quarantine、provenance、freshness、availability、
rate limiter、可續跑 backfill、market API、Redis 快取，
**全部用真實 TWSE 回應端到端驗證過**。215 個測試通過、覆蓋率 85%、
`ruff` 與 `mypy --strict` 零告警、migration 可逆且無 drift。

**一個環境限制**：這個雲端容器的對外連線被允許清單阻擋，
`curl` 到 `openapi.twse.com.tw` / `www.twse.com.tw` / `www.tpex.org.tw` 全部回 `http=000`。
因此 **live HTTP 抓取無法在此環境執行**。所有 provider 程式碼已完成，
並以**逐字錄製的真實 TWSE 回應**驗證整條管線（見 §2）。
批次歷史回補需在有網路的機器執行 —— 指令見 §11。

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

**這是本環境實際落地的資料，不是預估。**

| 資料集 | 筆數 | 涵蓋範圍 |
|--------|------|---------|
| `trading_calendar` | 365 | 2026 全年（243 交易日 / 98 週末 / 24 休市） |
| `daily_prices` | 30 | 2330 於 2026-07（22 筆）+ 8 檔 ETF 於 2026-08-14 |
| `index_quotes` | 12 | 2026-08-14 各類指數 |
| `institutional_flow` | 28 | 2026-08-14，4 檔 × 7 種投資人類別 |
| `stock_master` | 3 | 1101 / 1102 / 1103（SCD2 current） |
| `raw_ingestions` | 6 | 每筆 canonical row 都指向其中之一 |
| `data_quarantine` | 0 | 真實資料無一筆被拒 |
| `data_sources` | 5 | 2 ACTIVE / 3 UNVERIFIED |

**這不是市場快照。** fixture 是**截斷樣本**（`stock_day_all` 8 筆、`t187ap03_L` 3 筆），
足以驗證管線，不足以代表市場。上表每個數字都標了實際筆數，不做任何全覆蓋暗示。

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

實測於 2 vCPU / 8 GB 沙箱，資料來自 `ingestion_metrics` 表：

| 資料集 | 筆數 | 總耗時 |
|--------|------|--------|
| `index_quotes` | 12 | 9 ms |
| `daily_prices`（快照） | 8 | 7 ms |
| `daily_prices`（2330 一個月） | 22 | 23 ms |
| `institutional_flow` | 28 | 10 ms |

**這些數字沒有網路時間**（replay transport），所以只代表 parse + validate + persist。
真實 ingestion 會由 provider 延遲主導。`ingestion_metrics` 分開記錄
`provider_ms / parse_ms / validation_ms / persist_ms`，
所以有網路後可以直接看出瓶頸在哪 —— 這正是「先量測再最佳化」的準備工作。

**目前沒有做任何最佳化**：沒有 COPY、沒有 partitioning、沒有 materialized view。
依 ARCHITECTURE §16，等有真實 benchmark 再決定。

---

## 8. Database statistics

migration `0003_market_data_infrastructure` 新增 13 張表（總計 18 張）：

**Canonical**：`trading_calendar` `stock_master` `daily_prices` `index_quotes`
`institutional_flow` `corporate_actions` `market_status`

**Operations**：`data_sources` `raw_ingestions` `data_quarantine` `data_freshness`
`backfill_checkpoints` `ingestion_metrics`

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

```bash
# 1. 起服務並套用 migration
make up
make migrate
make seed
python -m scripts.seed_sources

# 2. 確認 live 抓取可行（PROVIDER_MODE 預設就是 live）
cd backend && .venv/bin/python - <<'PY'
import asyncio
from app.providers.twse import TWSEProvider
async def main():
    p = TWSEProvider()
    print(await p.health())
    r = await p.get_market_index()
    print("index records:", r.record_count, "as_of:", r.metadata.data_as_of)
    await p.aclose()
asyncio.run(main())
PY

# 3. 建立交易日曆（必須先做 — 沒有日曆，ingestion 會把資料丟進 quarantine）
make ingest-calendar YEAR=2026

# 4. 每日資料
make ingest-daily

# 5. 歷史回補（可中斷，重跑會從 checkpoint 續跑）
make backfill DATASET=institutional_flow FROM=2019-01-01 TO=2026-08-15

# 6. 驗證
make verify
curl -s localhost:8000/api/v1/market/data-operations | jq '.data.overall, .data.datasets'
```

**回補中斷後直接重跑同一條指令即可** —— `backfill_checkpoints` 會從游標繼續，
不會從 2019 重來。

執行後請把 `market/data-operations` 的輸出給我，我會把實測數字補進本報告的 §3、§7。

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

六個，都在寫完之後、commit 之前被抓到：

| # | 缺陷 | 為什麼危險 |
|---|------|-----------|
| 1 | **指數漲跌符號全部相反** | `"-"` 既是 `漲跌` 欄的合法符號，也是 null token。走 `clean()` 之後回傳 None，所有下跌變成上漲。TAIEX 那天實際跌 210.47，被記成漲 210.47 |
| 2 | **限流器同毫秒成員碰撞** | sorted set 成員是 `{ms}-{id}`，同一毫秒兩個請求成員相同，ZADD 變成覆寫而非新增，限制被悄悄突破 |
| 3 | **`valid_from` 用了上市日期** | 把知曉時間和事件時間搞混 —— SCD2 查 1990 年會回傳 2026 年才知道的資料。正是這整套設計要防的錯 |
| 4 | **point-in-time 過濾器包含 `ingested_at`** | 十年回補讓每筆歷史資料的 `ingested_at` 都是今天，於是任何對過去的查詢都回傳空 —— 一個看起來正確的防護變成靜默的空回測 |
| 5 | **多列 upsert 對異質 key 崩潰** | 三大法人的 DEALER/TOTAL 只有 net，其餘有 buy/sell。SQLAlchemy 從第一列推導欄位，缺欄位的列直接編譯失敗 |
| 6 | **`.gitignore` 吞掉整個 models 套件**（Phase 1 遺留） | `models/` 沒有前導斜線，會在**任何深度**比對，於是 `backend/app/models/` 從 Phase 1 起就不在版控裡。`git status` 全綠、本機測試全過，但 clean clone 根本 import 不起來 |

第 6 項不是這個 Phase 寫壞的，是 commit 前逐項檢查 `git ls-files` 時，發現
`v0.1.0-phase1` 這個 tag 裡**一個 ORM model 都沒有**。

值得記下的是**為什麼沒被抓到**。CI 是對 `actions/checkout` 的內容跑的，也就是
版控裡的內容 —— 這個缺陷 CI 一跑就會炸。但 remote 還沒設定，所以 CI 從來沒有真的
執行過一次；到目前為止唯一的驗證管道是本機 working tree，而 working tree 裡檔案
是在的。這是「CI 設定好了」和「CI 跑過了」之間的差別，也是為什麼下面 §14 把設定
remote 列為 Phase 3 的前置而不是雜項。

修法是把 data 區段的 pattern 全部錨定到 repo root（`/models/`），ML artefact 的
兩個實際落點單獨列出。修完之後補了一道本機也能跑的驗證：`git clone` 到乾淨目錄
→ import → 跑完整測試，**215 passed / 3 skipped**。收進 `make clone-check`，
併入 `make check`，以後每個 Phase gate 都跑，不依賴 remote 是否存在。

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
| 1 | GitHub remote confirmed | ⏳ | repo 名稱 `taiwan-stock-intelligence` 已收到；**仍需 owner 才能組出完整 URL**，依指示不猜測 |
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
| 28 | CI green | ⏳ | 每個步驟本地逐一通過（含新增的 clean-clone 驗證）；**待推上 GitHub 才算真的綠** |

**24 項完成、2 項待你提供 GitHub owner、1 項需外部來源、1 項需 CI 環境。**

---

## 16. 停止點

依指示，Phase 2 到此結束，**不自動進入 Phase 3**。

需要你的：

1. **GitHub owner** —— 給我 `https://github.com/<owner>/taiwan-stock-intelligence.git`，我設定 remote 並 push（含 tag）
2. **在有網路的機器執行 §11**，把 `data-operations` 輸出給我，我補上實測覆蓋數字
3. **Corporate actions 來源的決定** —— TWSE `t187ap45_L`（股利分派）是已驗證的 OpenAPI 端點，是最省事的起點；MOPS 涵蓋更全但脆弱
4. 確認後再下 Phase 3 指令
