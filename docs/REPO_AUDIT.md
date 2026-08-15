# REPO_AUDIT.md — Phase 0 Repository Audit

> 產出時間：2026-08-15 (Asia/Taipei)
> 稽核對象：本 session 可觸及的所有程式碼位置
> 結論：**Greenfield。目前不存在任何既有程式碼。**

---

## 1. 稽核方法與實際掃描範圍

本文件不是憑印象撰寫。以下是實際執行的檢查與其原始結果。

| # | 檢查項目 | 執行方式 | 結果 |
|---|---------|---------|------|
| 1 | 雲端工作區檔案系統 | `ls -la /home/claude` | 只有 shell dotfiles（`.cache` `.config` `.npm` `.ssh` `.gitconfig`）。**無任何專案目錄** |
| 2 | 使用者上傳檔案 | `ls /mnt/user-data/uploads` | 目錄不存在 → 無上傳檔案 |
| 3 | 產出目錄 | `ls /mnt/user-data/outputs` | 目錄不存在 |
| 4 | 附掛的 Claude Project「stock」 | `project_info` | `docs: []`、`files: []`、`knowledge_size: 0`。**專案內無任何既有文件** |
| 5 | 使用者本機裝置 | `get_device_info` | 裝置 `desktop-bi63624` (win32/x64) 已連線，但 `connectedFolders: []`。**沒有任何資料夾被授權掛載** |
| 6 | 使用者本機 home 目錄清單 | `get_device_info.homeDirectories` | 僅取得名稱（Desktop, Downloads, .vscode, ZeroPulse_1.0, ansel …），未授權故無法讀取內容 |
| 7 | Git remote | 無 remote 資訊、無 repo URL 提供 | N/A |

### 直接推論

- 沒有 `package.json`、`requirements.txt`、`pyproject.toml`、`Dockerfile`、`docker-compose.yml`
- 沒有 migrations、沒有 database schema
- 沒有 `.env` / `.env.example`
- 沒有既有 API、frontend、components、tests、CI/CD、documentation
- 沒有既有技術限制（framework lock-in、legacy schema、既有 API 契約）

**因此「不要直接推翻重寫既有架構」這條原則在本專案不適用 —— 沒有東西可推翻。**
取而代之的風險是相反方向的：**greenfield 最大的風險是一次做太多**。本文件與 `DEVELOPMENT_ROADMAP.md` 的核心任務就是壓制這個風險。

---

## 2. 已確認的專案約束（來自使用者決策）

這四項決策是後續所有架構選型的前提，寫在這裡作為 single source of truth：

| 約束 | 決策 | 對架構的直接影響 |
|------|------|-----------------|
| Repository 狀態 | Greenfield，全新建置 | 可自由選型；必須自建 CI/CD、migration、測試基礎設施 |
| 資料來源 | **官方免費公開來源優先**（TWSE / TPEx / MOPS / TAIFEX OpenAPI） | 無 tick-level、無委託簿深度、無盤中逐筆授權。系統定位為**日頻 + 盤中延遲快照**的研究平台，而非即時交易系統。Provider 介面仍需預留 `LicensedProvider` |
| 運行規模 | **個人自用，單機 Docker Compose** | 不引入 Kafka / Spark / K8s / ClickHouse。PostgreSQL 單一實例（掛載 TimescaleDB + pgvector 兩個 extension）＋ Redis 單一實例同時擔任 cache 與 broker |
| LLM 供應商 | **本地開源模型 (Ollama / vLLM)** | 無 per-token 成本 → 可對全量新聞做 LLM 處理；但**中文金融 NER / 情緒分析品質必須實測驗證**，不可假設。設計上採「字典/規則優先、LLM 補強」的混合策略（詳見 `AI_ENGINE.md` §3） |

---

## 3. Already Exists

```
（無）
```

唯一「已存在」的資產是本次 Phase 0 產出的七份設計文件本身：

```
docs/REPO_AUDIT.md              ← 本文件
docs/ARCHITECTURE.md
docs/ERD.md
docs/DATA_SOURCES.md
docs/API_SPEC.md
docs/AI_ENGINE.md
docs/QUANT_ENGINE.md
docs/DEVELOPMENT_ROADMAP.md
```

---

## 4. Need Modification

```
（無 —— 沒有既有程式碼可修改）
```

---

## 5. Need Creation

依照建置順序排列，括號內為對應 Phase。

### 5.1 基礎設施（Phase 1）

| 項目 | 說明 |
|------|------|
| `docker-compose.yml` | postgres(+timescaledb+pgvector) / redis / api / worker / beat / web / ollama / flower |
| `Dockerfile.api`, `Dockerfile.web` | 多階段建置 |
| `.env.example` | 所有 secret 的鍵名與說明；**`.env` 必須進 `.gitignore`** |
| `Makefile` | `make up / migrate / test / lint / typecheck / seed` |
| `.github/workflows/ci.yml` | lint → typecheck → unit test → migration check → build |
| `pyproject.toml` (uv/poetry) | FastAPI, SQLAlchemy 2.x, Alembic, Pydantic v2, pandas, polars, httpx, tenacity, structlog |
| `web/package.json` | Next.js 15 App Router, TypeScript, Tailwind, shadcn/ui, TanStack Query, Zustand, lightweight-charts |
| `alembic/` | 初始 migration |
| `core/config.py` | Pydantic Settings，所有設定來自環境變數 |
| `core/logging.py` | structlog JSON logger + request_id / job_id correlation |

### 5.2 資料層（Phase 2）

| 項目 | 說明 |
|------|------|
| `providers/base.py` | `BaseMarketDataProvider` 抽象類別（Adapter Pattern） |
| `providers/twse.py` / `tpex.py` / `taifex.py` / `mops.py` | 各交易所 adapter，含 retry / rate limit / cache |
| `providers/mock.py` | **僅限 dev/test**，啟動時檢查 `APP_ENV != production` 否則 raise |
| `ingest/` | 各資料集的 ingestion job（歷史回補 + 每日增量） |
| `quality/` | `DataValidationEngine` + `DataQualityScore` |
| `calendar/` | 台股交易日曆（含休市、颱風假、半日交易） |

### 5.3 分析層（Phase 3–6）

`indicators/`、`factors/`、`scoring/`、`ml/`（feature store / training / registry / inference）、`backtest/`、`anomaly/`、`event_study/`、`regime/`

### 5.4 情報層（Phase 4, 7, 8）

`news/`（provider / normalizer / dedup / NER / linker / sentiment / event）、`graph/`（supply chain）、`rag/`、`copilot/`（tool calling）

### 5.5 應用層（Phase 9–10）

`api/v1/`、`web/app/`、`alerts/`、`portfolio/`、`observability/`、`admin/`

---

## 6. Potential Problems（現在就能預見的問題）

依「發生機率 × 傷害程度」排序。

### P1 — 免費資料源只給「最新一個交易日」的快照

**證據（本次實測）**：`openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL` 與 `tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes` 都**沒有日期參數**，只回傳最近一個交易日。非交易日會回傳前一個交易日的資料，**且不帶任何「今天休市」的標記**。

**後果**：如果每日 job 無腦寫入，會在連假期間把同一天的資料重複寫成多天 → 回測資料直接污染。

**對策**：
1. 所有 ingestion 以**資料自身攜帶的 `Date` 欄位**為準，不用 `today()`
2. 寫入採 `ON CONFLICT (symbol, trading_date) DO UPDATE`（idempotent）
3. 交易日曆表獨立維護，job 先查交易日曆再決定是否執行
4. 歷史回補改走 **TWSE RWD web API**（實測可帶 `date` 參數，見 §7 證據）

### P2 — 歷史回補的 rate limit 是整個 Phase 2 的瓶頸

TWSE `rwd/zh/afterTrading/STOCK_DAY` 一次只回傳**單一個股的單一個月**。約 1,000 檔上市股 × 10 年 × 12 個月 ≈ **12 萬次請求**。若無節流會被封 IP。

**對策**：優先使用「全市場單日」型端點做回補（一次請求拿全市場一天 → 10 年約 2,400 次請求，少 50 倍），只有缺漏時才用單股端點補。並設 `RateLimiter`（token bucket，預設 3 req/s、可設定）＋ 指數退避 + 本地 raw response 快取（避免重跑時重打）。

### P3 — 資料值全部是字串、日期是民國年

實測 TWSE / TPEx 所有欄位（含數字）皆為 JSON string，日期為民國年格式（`1150731` = 2026-07-31），數字含千分位逗號（`"37,500,000"`），停牌或無資料時為 `"--"` 或空字串。

**對策**：Normalizer 層強制型別轉換 + 明確的 null 語意，並列入 unit test 的固定案例（用真實 payload 存成 fixture）。

### P4 — 中文金融 NER 用本地開源模型的品質是未知數

「台積電 / 2330 / TSMC / 台灣積體電路製造股份有限公司」是同一實體；「奇鋐 / 3017 / AVC」同理。純靠 LLM 抽取會有 hallucination 與漏抽。

**對策**：**字典優先**。用 TWSE `t187ap03_L`（實測含 `公司名稱`/`公司簡稱`/`英文簡稱`/`公司代號`，共 33 欄）自動建立 alias 字典 → Aho-Corasick 精確比對；LLM 只負責**關係判定與情緒**，且輸出必須通過 Pydantic schema 驗證，驗證失敗即丟棄而非猜測。

### P5 — Look-ahead bias 的來源不只是價格

真正的陷阱在財報與月營收：**財報的「所屬期別」和「公布時間」差好幾個月**。若用 `fiscal_period` 對齊回測，等於偷看未來。

**對策**：所有基本面表採 **bitemporal 設計** —— 同時存 `period_end`（事件時間）與 `announced_at`（知曉時間），回測一律以 `announced_at <= as_of` 過濾。這是硬性規範，寫進 `ERD.md` 與 lint 規則。

### P6 — 單機資源天花板

Ollama 跑 14B 級模型需 ~10GB VRAM 或大量 RAM；同時跑 Postgres + Redis + Next.js + Celery worker，個人機器可能吃緊。

**對策**：Ollama 為獨立 compose service 且**可關閉**（`ENABLE_LLM=false` 時 News 走純規則路徑、Copilot 顯示未啟用）。系統不得因為 LLM 不在就整個掛掉。

### P7 — 「即時行情」的合法性邊界

`mis.twse.com.tw` 的盤中報價端點在本次驗證中被 robots.txt 阻擋，無法確認其對程式化存取的授權範圍。

**對策**：Phase 2 不使用任何未確認授權的端點。系統定位明確標示為「日頻 + 延遲快照」，UI 上每個數字都標 `data_timestamp` 與 `source`。若日後取得授權，實作 `LicensedProvider` 即可，不動上層。

---

## 7. 資料來源實測證據（Phase 0 已驗證）

以下端點在 2026-08-15 由本 session 實際請求並確認回傳結構。完整清冊見 `DATA_SOURCES.md`。

| 端點 | 狀態 | 實測回傳 |
|------|------|---------|
| `openapi.twse.com.tw/v1/exchangeReport/MI_INDEX` | ✅ 200 | 欄位 `日期/指數/收盤指數/漲跌/漲跌點數/漲跌百分比/特殊處理註記`；實際樣本 `1150813, 發行量加權股價指數, 46021.48, +, 503.41, 1.11` |
| `openapi.twse.com.tw/v1/opendata/t187ap03_L` | ✅ 200 | 上市公司基本資料，33 欄，首筆為 1101 台灣水泥 |
| `openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL` | ✅ 200 | 全市場最新交易日 OHLCV；欄位 `Date/Code/Name/OpeningPrice/HighestPrice/LowestPrice/ClosingPrice/TradeVolume/TradeValue/Transaction/Change` |
| `www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date=&stockNo=` | ✅ 200 | **可指定日期** → 歷史回補主力。實測 2330 於 2026-07 的資料，`stat: OK` |
| `www.twse.com.tw/rwd/zh/fund/T86?date=&selectType=ALL` | ✅ 200 | **可指定日期**的個股三大法人買賣超，19 欄（外陸資 / 外資自營商 / 投信 / 自營商自行 / 自營商避險 / 三大法人合計） |
| `www.tpex.org.tw/openapi/v1/...` | ⚠️ 403 (本 session) | 端點存在且公開文件確認可用，但雲端環境被擋。**Phase 2 需在本機環境重新驗證** |
| `mis.twse.com.tw/stock/api/getStockInfo.jsp` | ⚠️ robots.txt 禁止 | 授權範圍未確認，**Phase 2 不採用** |
| `openapi.twse.com.tw/v1/fund/T86`、`/v1/exchangeReport/BFI82U` | ❌ 404 | 這兩條路徑不存在於 OpenAPI（常見的網路謠傳路徑），改用上方 RWD 端點 |

> ⚠️ 這張表的意義：**已驗證的才寫進 provider，沒驗證的一律標記待驗證**。這是「不產生 fake API」原則的具體執行方式。

---

## 8. Technical Debt（此刻主動接受的技術債，附償還條件）

| # | 債務 | 為什麼現在接受 | 償還觸發條件 |
|---|------|--------------|-------------|
| TD-1 | Redis 同時當 cache 與 Celery broker | 單機省一個 service，個人用量下無爭用問題 | worker 阻塞 cache 讀取，或 broker 記憶體 > 1GB 時分離為兩個 Redis DB / 實例 |
| TD-2 | 沒有獨立 feature store（用 Postgres 表 + view 代替） | Feast 之類的元件在單機是純負擔 | 特徵計算超過 10 分鐘或 training/serving skew 出現時再導入 |
| TD-3 | ML 只做 batch inference，不做 real-time | 資料本身是日頻，real-time inference 無意義 | 取得盤中授權資料後 |
| TD-4 | 供應鏈圖存在 Postgres（`supply_chain_nodes/edges` + recursive CTE），不用 Neo4j | 節點量級 ~3,000 檔股票 + ~200 主題，遞迴查詢完全夠 | 需要 3 跳以上路徑分析且查詢 > 500ms 時 |
| TD-5 | Alert 用輪詢（Celery beat 每 N 分鐘掃描）而非事件驅動 | 日頻資料下延遲可接受 | 接入即時資料源後改為 event-driven |
| TD-6 | 前端無 SSR 資料預取，先全用 client-side TanStack Query | 個人自用，首屏時間非關鍵指標 | 對外服務時改用 RSC + streaming |

---

## 9. Security Risks（greenfield 階段就要防的）

| 風險 | 對策 | 落實 Phase |
|------|------|-----------|
| Secret 進版控 | `.gitignore` 排除 `.env`；只 commit `.env.example`；CI 加 `gitleaks` 掃描 | Phase 1 |
| 資料庫預設密碼 | compose 不寫死密碼，一律讀 `.env`；首次啟動腳本強制產生隨機密碼 | Phase 1 |
| SQL Injection | 全面使用 SQLAlchemy ORM / bound parameter；**禁止 f-string 拼 SQL**，加 lint 規則 | Phase 1 |
| Copilot 的 SQL 工具被 prompt injection 利用 | Copilot **不得**擁有任意 SQL 執行權。只暴露白名單 tool（見 `AI_ENGINE.md` §7），每個 tool 參數用 Pydantic 驗證，資料庫連線使用唯讀角色 | Phase 8 |
| 新聞內容中的 prompt injection | 新聞文字進入 LLM 前包在明確的 data 區塊並加 system 指令；LLM 輸出強制 schema 驗證，不接受自由文字指令 | Phase 4 |
| 對外爬取被視為濫用 | 所有 provider 帶明確 User-Agent、遵守 rate limit、尊重 robots.txt；違反者不納入 | Phase 2 |
| 單機無備份 | `pg_dump` 每日排程 + 保留 7 份；raw response 另存 object storage（本地 MinIO 或檔案系統） | Phase 10 |
| JWT secret 弱 / 無輪替 | 使用 `secrets.token_urlsafe(64)`，refresh token 存 Redis 可撤銷 | Phase 1 |
| 無稽核軌跡 | `audit_logs` 表記錄所有寫入操作與 Copilot 查詢 | Phase 1 |

---

## 10. Scalability Risks

| 風險 | 何時會爆 | 預留的擴展路徑 |
|------|---------|--------------|
| `daily_prices` 資料量 | ~2,000 檔 × 250 交易日 × 20 年 ≈ 1,000 萬列 —— TimescaleDB hypertable 完全無壓力 | 若加入 1 分 K（2,000 × 270 根 × 250 天/年 ≈ 1.35 億列/年）→ 啟用 compression policy + continuous aggregate |
| 因子計算全市場重算 | 每日 ~2,000 檔 × ~80 個因子，pandas/polars 向量化下數十秒 | 超過 5 分鐘 → 改 polars lazy + 只算增量視窗 |
| 新聞 embedding 量 | 每日 ~500 則 × 365 天 = 18 萬向量，pgvector HNSW 索引足夠 | > 500 萬向量時評估獨立向量庫 |
| LLM 吞吐 | 本地 14B 模型每則新聞數秒 → 每日 500 則需數十分鐘 | 分批 + 只對「通過初篩（有股票關聯）」的新聞跑 LLM；必要時換小模型或 vLLM 批次推論 |
| 回測併發 | 單機 CPU 綁定 | Celery worker 併發數可設定；長回測走 queue 並回傳 job_id 而非同步等待 |

---

## 11. Phase 0 的 Definition of Done

- [x] 完整掃描所有可觸及的程式碼位置，確認 greenfield
- [x] 確認四項專案約束（repo / 資料源 / 規模 / LLM）
- [x] 實測驗證主要資料來源端點，區分「已驗證 / 待驗證 / 不採用」
- [x] 產出 `ARCHITECTURE.md`
- [x] 產出 `ERD.md`
- [x] 產出 `DATA_SOURCES.md`
- [x] 產出 `API_SPEC.md`
- [x] 產出 `AI_ENGINE.md`
- [x] 產出 `QUANT_ENGINE.md`
- [x] 產出 `DEVELOPMENT_ROADMAP.md`
- [x] 列出 Already Exists / Need Modification / Need Creation / Potential Problems / Technical Debt / Security Risks / Scalability Risks
- [ ] **使用者確認架構方向後**，才進入 Phase 1

> Phase 0 尚未寫任何應用程式碼 —— 這是刻意的，符合「在完成 Architecture Audit 之前不要大量修改程式碼」的指示。
