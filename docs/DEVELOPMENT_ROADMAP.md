# DEVELOPMENT_ROADMAP.md — 開發路線圖

> 版本 0.1 · 2026-08-15
> 原則：**每個 Phase 都必須交付一個「可用的東西」**，而不是「一堆還不能用的基礎建設」。

---

## 0. 路線圖總覽

| Phase | 主題 | 交付的可用成果 | 相依 |
|-------|------|--------------|------|
| **0** | Architecture Audit | 7 份設計文件（✅ 已完成） | — |
| **1** | Foundation | 可登入的空殼系統、健康檢查頁、CI 綠燈 | 0 |
| **2** | Market Data | **能查任何台股 10 年 K 線的網頁** | 1 |
| **3** | Quant Engine | **能看指標、因子、排行的選股器** | 2 |
| **4** | News Intelligence | **會自動把新聞連到股票的新聞頁** | 2 |
| **5** | AI Score | **首頁排行 + 「為什麼 91 分」的解釋** | 3, 4 |
| **6** | ML & Backtest | **能跑回測並看到防偏誤報告** | 3, 5 |
| **7** | Supply Chain | **可互動的 AI 供應鏈圖 + 新聞傳播** | 4 |
| **8** | Copilot & RAG | **能問問題並得到帶引用的答案** | 5, 6, 7 |
| **9** | Portfolio & Alerts | **持倉風險分析 + 條件警示** | 5 |
| **10** | Production | 監控、備份、效能、安全的完整交付 | 全部 |

### 相依關係圖

```
Phase 0 ──→ Phase 1 ──→ Phase 2 ──┬──→ Phase 3 ──┬──→ Phase 5 ──┬──→ Phase 6
                                   │              │              │
                                   └──→ Phase 4 ──┤              ├──→ Phase 9
                                        │         │              │
                                        └────→ Phase 7 ──────────┴──→ Phase 8
                                                                        │
                                                          全部 ──→ Phase 10
```

**關鍵洞察**：Phase 3 與 Phase 4 可並行（一個純數值、一個純文本，互不干擾）。若時間有限，Phase 3 優先 —— 因為 AI Score 的技術/基本/籌碼三大分項都來自 Phase 3，News 只是其中一項。

---

## 每個 Phase 的通用 Gate（★ 不可跳過）

進入下一個 Phase 之前，**全部**必須通過：

```bash
make lint          # ruff check + ruff format --check + pnpm lint
make typecheck     # mypy --strict + pnpm typecheck
make test          # pytest --cov-fail-under=80 + vitest
make migration     # alembic upgrade head && downgrade -1 && upgrade head
make build         # docker compose build + pnpm build
make api-check     # openapi.json 與 committed 版本一致
make security      # gitleaks + pip-audit + npm audit
make quality       # 資料品質報告：Overall 中位數 > 門檻
```

**任一失敗 → 停止，修好再繼續。不得帶著紅燈進入下一個 Phase。**

每個 Phase 結束時更新：`ARCHITECTURE.md`（若架構有變）、`ERD.md`（若 schema 有變）、`API_SPEC.md`（若端點有變）、`CHANGELOG.md`。

---

## Phase 0 — Architecture ✅ 已完成

### 交付物

| 文件 | 內容 |
|------|------|
| `REPO_AUDIT.md` | Greenfield 確認、四項約束、風險、技術債、資料源實測證據 |
| `ARCHITECTURE.md` | 系統架構、資料流、十問回答、12 條 ADR |
| `ERD.md` | 50+ 資料表、bitemporal 設計、TimescaleDB 政策、遷移順序 |
| `DATA_SOURCES.md` | 已驗證/待驗證/不採用的來源、欄位映射、品質規則 |
| `API_SPEC.md` | REST v1 全端點、統一信封、錯誤模型 |
| `AI_ENGINE.md` | Score 可解釋性、News pipeline、ML pipeline、RAG、Copilot、模型治理 |
| `QUANT_ENGINE.md` | 指標、因子、因子驗證、回測防偏誤、事件研究、Regime、異常 |
| `DEVELOPMENT_ROADMAP.md` | 本文件 |

### DoD
- [x] 全部 8 份文件產出
- [x] 資料來源實測並區分驗證狀態
- [ ] **使用者確認架構方向** ← 目前卡在這裡

---

## Phase 1 — Foundation

> **目標**：一個能跑起來、能登入、CI 綠燈的空殼。沒有任何業務功能，但每一塊地基都是對的。

### 任務

#### 1.1 Repository 骨架
- [ ] 依 `ARCHITECTURE.md` 附錄 A 建立目錄結構
- [ ] `pyproject.toml`（uv 管理），固定主要依賴版本
- [ ] `web/package.json`（pnpm），Next.js 15 + TS strict
- [ ] `.gitignore`（含 `.env`、`*.dump`、`data/`、`models/`）
- [ ] `Makefile` 收攏所有常用指令
- [ ] `README.md`（快速啟動 5 步驟）

#### 1.2 Docker Compose
- [ ] `postgres`：官方 timescaledb 映像 + `CREATE EXTENSION vector, pg_trgm`
- [ ] `redis`：db0 = cache，db1 = broker
- [ ] `api`：uvicorn，掛載程式碼支援熱重載
- [ ] `worker` / `beat`：Celery
- [ ] `web`：Next.js dev server
- [ ] `ollama`（profile `llm`）、`minio`（profile `storage`）、`flower`（profile `observability`）
- [ ] 所有密碼從 `.env` 讀取，`.env.example` 完整

#### 1.3 Core 基礎設施
- [ ] `core/config.py`：Pydantic Settings，含 `APP_ENV` 一致性檢查（production 禁 Mock、禁 debug、必須有真 secret）
- [ ] `core/logging.py`：structlog JSON + request_id / job_run_id contextvar
- [ ] `core/errors.py`：Problem Details 例外體系 + FastAPI exception handler
- [ ] `core/security.py`：argon2id、JWT 簽發驗證、refresh token（Redis）
- [ ] `core/cache.py`：Redis 客戶端 + 版本前綴機制 + `@cached` decorator
- [ ] `core/ratelimit.py`：滑動視窗
- [ ] `db/session.py`：SQLAlchemy 2.0 async engine + session dependency
- [ ] `db/base.py`：Declarative base + 共用 mixin（timestamps、soft delete）

#### 1.4 Migration 與最小 Schema
- [ ] Alembic 設定 + `001_extensions`
- [ ] `002_master`（markets, sectors, industries, stocks, trading_calendar, entity_aliases, corporate_actions）
- [ ] `003_platform`（版本表、job_runs、audit_logs、system_health、raw_ingestions）
- [ ] `015_user` 的子集：users（登入需要）
- [ ] Seed script：markets、預設 admin 使用者

#### 1.5 API 骨架
- [ ] FastAPI app + CORS + 例外處理 + request_id middleware
- [ ] `POST /auth/login` / `POST /auth/refresh` / `POST /auth/logout` / `GET /auth/me`
- [ ] `GET /health`（DB / Redis / LLM 檢查）
- [ ] RBAC dependency（`require_role("admin")`）
- [ ] 統一回應信封的 `ResponseEnvelope[T]` 泛型 + `meta` 建構器
- [ ] Audit log middleware（寫入操作）

#### 1.6 Celery 骨架
- [ ] Celery app + 5 個 queue 定義 + beat schedule 檔
- [ ] `@job` decorator：idempotency 鎖、job_runs 記錄、timeout、structlog
- [ ] 一個 `ping` 示範 job

#### 1.7 前端骨架
- [ ] App Router 版面（側欄 + 頂欄 + dark mode）
- [ ] 登入頁 + 受保護路由
- [ ] `lib/api/` client（含 401 自動 refresh）
- [ ] TanStack Query provider + Zustand store
- [ ] shadcn/ui 初始化 + 台股紅漲綠跌的 theme token
- [ ] `<DataProvenance>` 元件（顯示 meta）
- [ ] 系統健康頁（消費 `/health`）

#### 1.8 CI
- [ ] GitHub Actions：lint → typecheck → test → migration → build → security
- [ ] `testcontainers` 起 Postgres/Redis 的 integration test 範例
- [ ] Coverage 門檻 80%

### DoD
- [ ] `docker compose up` 後，瀏覽器可登入並看到健康檢查頁全綠
- [ ] `make test` 全過，coverage ≥ 80%
- [ ] `alembic upgrade head → downgrade -1 → upgrade head` 無錯
- [ ] CI 綠燈
- [ ] `.env` 不在版控中，`gitleaks` 無發現
- [ ] `APP_ENV=production` 且使用 MockProvider 時，啟動即失敗（測試驗證）

---

## Phase 2 — Market Data

> **目標**：能在網頁上查任何台股的 10 年 K 線，且每個數字都能追溯來源與品質。

### 任務

#### 2.1 資料來源驗證（★ 第一件事）
- [ ] 在**台灣本機環境**逐一實測 `DATA_SOURCES.md` 中所有 ⚠️ 端點
- [ ] 更新 `DATA_SOURCES.md` 的驗證狀態與實際欄位名
- [ ] 每個採用端點存一份真實 response 為 `tests/fixtures/`
- [ ] 核對每個資料集在 data.gov.tw 的授權條款 → `docs/ATTRIBUTION.md`

#### 2.2 Provider 層
- [ ] `BaseMarketDataProvider` + `Capability` enum + `ProviderRegistry`
- [ ] `HTTPClient` 基底：timeout / tenacity retry / token bucket rate limit / circuit breaker / raw 存檔
- [ ] `TWSEProvider`（OpenAPI 快照 + RWD 歷史）
- [ ] `TPExProvider`
- [ ] `MockProvider`（+ production 禁用測試）
- [ ] Provider health 寫入 `system_health`

#### 2.3 Normalizer
- [ ] 民國年、千分位、`"--"`、`"X"` 標記的轉換函式（每個都有 unit test）
- [ ] 每個 dataset 的欄位映射（依 `DATA_SOURCES.md` §11）
- [ ] Decimal 而非 float

#### 2.4 交易日曆
- [ ] 抓取 TWSE 年度休市公告 → `trading_calendar`
- [ ] 用「當日全市場成交量 > 0」交叉驗證，設 `verified_by_volume`
- [ ] 10 年回填
- [ ] 日曆缺失時 job 拒絕執行的守門邏輯

#### 2.5 Schema 與 Ingestion
- [ ] Migration `004_market` / `005_fundamental` / `006_flow` / `007_quality`
- [ ] 股票主檔 ingestion（`t187ap03_L`）→ 同時自動建 `entity_aliases`
- [ ] 日線 ingestion（每日增量 + 歷史回補）
- [ ] 指數 ingestion
- [ ] 三大法人 ingestion（RWD T86，全市場單日型）
- [ ] 融資券 ingestion
- [ ] 月營收 + 財報 ingestion（★ 帶 `announced_at`）
- [ ] 除權息 ingestion + `adjust_factor` 計算 job
- [ ] `backfill_progress` 可中斷可續跑機制

#### 2.6 Data Quality Layer
- [ ] `DataValidationEngine` + `DATA_SOURCES.md` §12 的全部規則
- [ ] FATAL → `quarantine_records`；WARN → `quality_flags`
- [ ] `DataQualityScore` 計算 job
- [ ] `data_gaps` 偵測 job + 自動補洞

#### 2.7 API
- [ ] `/stocks/search` `/stocks/{symbol}` `/stocks/{symbol}/prices` `/quotes`
- [ ] `/market/indices` `/market/breadth`
- [ ] `/stocks/{symbol}/institutional` `/margin`
- [ ] `/stocks/{symbol}/financials` `/revenue`（★ 支援 `as_of`）
- [ ] `/admin/data-quality` `/admin/data-gaps` `/admin/backfill`

#### 2.8 前端
- [ ] 股票搜尋（全域 `/` 快捷鍵）
- [ ] 個股頁：K 線圖（Lightweight Charts）+ 成交量 + 期間切換
- [ ] 法人買賣超圖表
- [ ] 財報與月營收表格
- [ ] 每個區塊帶 `<DataProvenance>`
- [ ] Admin：資料品質儀表板

### DoD
- [ ] 10 年全市場日線回填完成，`data_gaps` 中每個缺口都有記錄原因
- [ ] 最近 60 交易日的 `DataQualityScore` Overall 中位數 > 95
- [ ] 2330 的 10 年 K 線在網頁上正確顯示（與 TWSE 官網抽查 10 個日期一致）
- [ ] 還原權值後的報酬與公開資訊比對誤差 < 0.5%
- [ ] 每日 EOD pipeline 在 30 分鐘內完成
- [ ] Provider 全部斷線時，API 仍回應（`meta.is_stale=true`），不 500
- [ ] Normalizer 對 fixture 的所有邊界案例測試通過

---

## Phase 3 — Quant Engine

> **目標**：一個能用因子篩選、排序全市場的選股器。

### 任務
- [ ] `indicators/` 全部指標（依 `QUANT_ENGINE.md` §1.2）+ TA-Lib 黃金測試
- [ ] `factors/` 全部因子 + `FactorSpec` 宣告式定義
- [ ] ★ **look-ahead 注入測試對每個因子參數化執行**
- [ ] 因子處理管線（winsorize → 中性化 → 標準化 → 方向）
- [ ] 因子有效性驗證報告（IC / Rank IC / IC-IR / 分層報酬 / 覆蓋率 / 穩定性 / 相關性）
- [ ] 未達標因子標記為 research-only（不進 AI Score）
- [ ] Migration `008_derived_quant`
- [ ] 每日 `quant.indicators` / `quant.factors` job
- [ ] 歷史回算 10 年指標與因子
- [ ] `feature_versions` 第一版正式紀錄
- [ ] API：`/stocks/{symbol}/indicators` `/factors` `/factors/ranking` `/screener`
- [ ] 前端：選股器（動態條件建構器 + 虛擬化結果表 + 顯示通過條件的實際值）
- [ ] 前端：個股頁的技術指標區與因子雷達圖

### DoD
- [ ] 所有指標通過 TA-Lib 黃金測試（rtol 1e-6）
- [ ] 所有因子通過 look-ahead 注入測試
- [ ] 因子有效性報告產出，至少 15 個因子達標
- [ ] 因子相關矩陣中無 |corr| > 0.8 的重複因子（或已擇一保留）
- [ ] 單日全市場指標 + 因子計算 < 180 秒
- [ ] 選股器對 2,000 檔股票的複合條件查詢 < 2 秒

---

## Phase 4 — News Intelligence

> **目標**：新聞自動連到股票，且每個關聯都有 evidence。

### 任務
- [ ] 新聞來源評估：robots.txt、RSS 可用性、授權條款 → `news_sources` seed
- [ ] Migration `009_news`
- [ ] `NewsFetcher`（ETag / Last-Modified、rate limit、robots 遵守）
- [ ] `NewsNormalizer`（trafilatura 抽正文、時區、boilerplate 清除）
- [ ] `NewsDeduplicator`（URL hash → SimHash → embedding，三層）
- [ ] `entity_aliases` 自動建立（由 Phase 2 的股票主檔）+ Aho-Corasick 匹配器
- [ ] 規則消歧（代號前後文、短別名上下文、ambiguous 處理）
- [ ] `LLMProvider` 抽象 + `OllamaProvider` + `NoopProvider`
- [ ] LLM 實體補充（產品/技術/人名），結構化輸出 + Pydantic 驗證 + 重試
- [ ] `NewsStockLinker`（DIRECT + 產業 + 主題；供應鏈傳播留待 Phase 7）
- [ ] `SentimentAnalyzer`（規則詞典 + LLM 融合）
- [ ] `EventClassifier`
- [ ] `ImpactScorer`
- [ ] `news.reaction_backfill` job（T+1/3/5/10/20 異常報酬）
- [ ] `news_embeddings` + HNSW 索引
- [ ] 人工標註集：100 篇新聞的股票關聯、情緒、事件類型
- [ ] API：`/news` `/news/{id}` `/news/{id}/reactions` `/stocks/{symbol}/news` `/news/momentum`
- [ ] 前端：新聞流（篩選、關聯股票標籤、情緒色彩）、新聞詳情（evidence 高亮）

### DoD
- [ ] 股票連結 Precision > 0.95、Recall > 0.85（對 100 篇標註集）
- [ ] 去重 cluster 純度 > 0.9（對 500 篇含轉載樣本）
- [ ] 情緒與人工標註 Spearman > 0.6
- [ ] 事件分類 macro-F1 > 0.7
- [ ] **`ENABLE_LLM=false` 時管線完整跑完**（規則路徑）
- [ ] Prompt injection 測試：10 個惡意樣本皆不執行其中指令
- [ ] 每日新聞處理在 30 分鐘內完成

---

## Phase 5 — AI Score

> **目標**：首頁的排行榜，以及點開任一檔看到「為什麼 91 分」。

### 任務
- [ ] Migration `010_scoring` `012_analytics`
- [ ] `scoring_weights` 預設版本 seed
- [ ] 11 個分項的訊號定義與映射函式（全部從設定讀取）
- [ ] `ScoreComposer`：合成 + 校準（橫斷面百分位）
- [ ] `ScoreExplainer`：貢獻度分解，保證 `baseline + Σ contributions = total`
- [ ] `AnomalyEngine`：Layer 1 z-score + Layer 2 Isolation Forest + Layer 3 關係型
- [ ] `SectorIntelligence`：6 個分項 + 強度合成 + 狀態分類 + RRG 座標
- [ ] `RegimeDetector`：規則式 + HMM 並行 + 最短持續期平滑
- [ ] 每日 job：`ai.score` `anomaly.detect` `sector.rotation` `regime.detect`
- [ ] 歷史回算 5 年 AI Score（供趨勢與回測使用）
- [ ] API：`/ai-score/*` `/anomalies` `/sectors/*` `/market/overview` `/market/regime`
- [ ] 前端：**首頁 Dashboard**（市場總覽、Regime、產業熱力圖、AI 排行、異常雷達、快訊）
- [ ] 前端：AI Score 解釋元件（瀑布圖 + 貢獻列表 + evidence 展開）
- [ ] 前端：產業輪動頁（RRG 散點 + 排名變化）

### DoD
- [ ] `SUM(contributions) + baseline = total_score` 對全市場全日期成立（DB 不變式測試）
- [ ] 分數分布標準差 > 12，無過度集中
- [ ] 分數 20 日 rank 自相關 > 0.5
- [ ] **高分組（前 10%）的 forward 20 日超額報酬顯著為正**（t > 2）
- [ ] 改 `scoring_weights` 後重算，分數變化符合預期且不需重新部署
- [ ] 首頁在 2 秒內完成首屏（各區塊獨立 Suspense）
- [ ] 每個顯示分數的地方都有 disclaimer

---

## Phase 6 — ML & Backtest

> **目標**：能跑回測，且回測報告會誠實告訴你有沒有偏誤。

### 任務

#### 6.1 Backtest（先做，因為 ML 需要它驗證）
- [ ] Migration `014_research`（backtests / trades / metrics）
- [ ] `PointInTimeLoader`（唯一的回測資料入口）
- [ ] `UniverseBuilder`（★ 含下市股）
- [ ] `ExecutionSimulator`（T+1 開盤、流動性限制、動態滑價）
- [ ] `TWCostModel`（手續費 0.1425%、交易稅 0.3%、最低 20 元）
- [ ] `PortfolioAccountant`（除權息、現金股利、部位）
- [ ] `MetricsCalculator`（`QUANT_ENGINE.md` §4.5 全部指標）
- [ ] 統計穩健性檢驗（參數敏感度、子期間、隨機基準、Deflated Sharpe、成本敏感度）
- [ ] `bias_checks` 自我聲明機制
- [ ] 六項回測正確性測試

#### 6.2 ML
- [ ] Migration `011_ml`
- [ ] `FeatureBuilder`（120 特徵，`core_v1`）→ `ml_features`
- [ ] `LabelGenerator`（獨立延遲 job，記錄 `labels_filled_at`）
- [ ] Repository 層的 `get_for_training()` / `get_for_inference()` 分離
- [ ] `WalkForwardSplitter`（embargo + purging）
- [ ] LightGBM 訓練 pipeline（固定 seed、early stopping、樣本權重）
- [ ] 評估（AUC / Precision@K / Brier / Calibration / Rank IC / 分層）
- [ ] Isotonic 校準
- [ ] `ModelRegistry`（artifact → MinIO，metadata → `model_versions`）
- [ ] Shadow 部署機制
- [ ] `ml.inference` 每日 batch job + SHAP top-k
- [ ] `model.monitor` 每日 job（PSI / realized AUC / calibration error）
- [ ] ★ 四項 ML 正確性測試

#### 6.3 API / 前端
- [ ] `/backtest` `/backtest/{id}` `/trades` `/equity` `/predictions/{symbol}`
- [ ] `/admin/models` `/admin/models/{id}/activate` `/monitoring`
- [ ] 前端：回測建構器 + 結果頁（權益曲線、回撤、月報酬熱圖、成本分解、bias_checks 橫幅）
- [ ] 前端：個股頁的 ML 預測區（機率 + 信心 + SHAP + disclaimer）
- [ ] 前端：Admin 模型監控頁

### DoD
- [ ] 六項回測正確性測試全過
- [ ] 「買入持有 2330」回測與實際還原報酬誤差 < 0.5%
- [ ] 四項 ML 正確性測試全過（無 look-ahead、無洩漏、特徵一致、可重現）
- [ ] Walk-forward 每個 fold AUC > 0.52，標準差 < 0.03
- [ ] 校準曲線斜率 0.8–1.2
- [ ] **AUC > 0.65 時自動觸發洩漏檢查告警**（驗證此機制有效）
- [ ] 10 年週再平衡回測 < 120 秒
- [ ] 回測結果可用 `dataset_version + code_version + seed` 完整重現（跑兩次結果相同）

---

## Phase 7 — Supply Chain Graph

> **目標**：一張可互動的 AI 供應鏈圖，且新聞會沿著它傳播。

### 任務
- [ ] Migration `013_graph`
- [ ] AI 產業分類體系設計（THEME → SEGMENT → COMPANY 三層）
- [ ] 初始節點 seed：AI / GPU / ASIC / Foundry / CoWoS / HBM / PCB / CCL / Cooling / Power / Optical / Server / Testing / IP …
- [ ] 邊建立（★ 每條邊必須有 evidence：年報、法說會、官方公告）
- [ ] `valid_from` / `valid_to` 時效管理
- [ ] Admin 圖譜編輯介面（人工新增/審核關係）
- [ ] LLM 從年報/新聞提議關係 → `approved=false` 待人工核准
- [ ] Recursive CTE 傳播查詢 + 衰減參數
- [ ] `ai_beta` 因子（統計驗證主觀分類）+ 背離告警
- [ ] News → Supply Chain 傳播（Phase 4 的 Linker 升級：SUPPLIER / CUSTOMER / COMPETITOR / THEMATIC）
- [ ] `EventStudyEngine`（market model / market-adjusted / BMP 檢定 / 事件叢集處理）
- [ ] Lead-Lag 模型（美股 → 台股，含樣本數與 t 值）
- [ ] 美股資料 provider（`USEquityProvider`）
- [ ] API：`/supply-chain/*` `/events` `/events/study` `/events/lead-lag`
- [ ] 前端：供應鏈圖（Cytoscape，zoom / filter / click node / 顯示 AI Score 與新聞）
- [ ] 前端：事件研究頁（CAAR 曲線 + 統計表 + caveats）

### DoD
- [ ] 每條 edge 都有非空 evidence（DB 約束 + 測試）
- [ ] 供應鏈圖至少涵蓋 300 檔台股、12 個 segment
- [ ] 3 跳傳播查詢 < 500ms
- [ ] `ai_beta` 與主觀 AI 標籤的一致性 > 70%，背離者已人工檢視
- [ ] 事件研究在合成無效應資料上的偽陽性率接近 5%（統計正確性驗證）
- [ ] Lead-Lag 的每個數字都附 n、beta、se、t、期間
- [ ] **文件與 UI 中不存在任何未經計算的示例數字**

---

## Phase 8 — Copilot & RAG

> **目標**：能問「為什麼奇鋐今天大漲」並得到帶引用的答案。

### 任務
- [ ] Migration：`documents` / `document_chunks`
- [ ] Embedding 模型評測集（100 題中文金融檢索）→ 決定模型
- [ ] 文件 ingestion（財報、重訊、法說會、公司描述）
- [ ] Chunking 策略（依文件類型）
- [ ] `rag.embed` job + HNSW 索引
- [ ] Hybrid retrieval（dense + sparse + RRF）+ `published_at <= as_of` 過濾
- [ ] 20 個 Copilot tools（`AI_ENGINE.md` §7.2）+ Pydantic 參數驗證
- [ ] 唯讀 DB 角色
- [ ] Agent loop（最多 8 輪）
- [ ] `<untrusted_data>` 包裹 + system prompt 防護
- [ ] Fact Checker 後處理（數字比對）
- [ ] SSE 串流端點
- [ ] 50 題 Copilot 評測集 + 自動化評測腳本
- [ ] 前端：Copilot 對話介面（工具呼叫可視化、引用卡片、串流）

### DoD
- [ ] 中文金融檢索 Recall@5 > 0.7、MRR > 0.5
- [ ] Copilot 評測：工具選擇正確率 > 85%、事實正確率 > 95%
- [ ] **幻覺率 < 2%**
- [ ] 「應該說不知道」題目的正確率 > 90%
- [ ] 引用完整率 100%、違禁詞率 0%
- [ ] Prompt injection 測試 10/10 通過
- [ ] 七個目標問題（`AI_ENGINE.md` §7.5）全部能正確回答

---

## Phase 9 — Portfolio & Alerts

### 任務
- [ ] Migration `015_user` 剩餘部分
- [ ] `PortfolioAccountant`（交易 → 持倉 → 損益，含除權息）
- [ ] 績效計算（時間加權報酬 vs 大盤）
- [ ] 風險分析（波動、beta、VaR、CVaR、最大回撤）
- [ ] 曝險分析（產業、因子、AI 曝險、集中度 HHI）
- [ ] 風險模型（`QUANT_ENGINE.md` §3.3）
- [ ] 自動警告產生（集中度過高、單一事件風險）
- [ ] `AlertEngine`：條件樹解析器（AND/OR 巢狀）
- [ ] `alerts.evaluate` job（盤中每 15 分 + 收盤後）
- [ ] Cooldown、優先級、通知管道（in-app / email / webhook）
- [ ] `POST /alerts/{id}/test`（試跑）
- [ ] API：`/portfolio/*` `/alerts/*`
- [ ] 前端：投資組合頁（持倉、損益、績效曲線、風險雷達、曝險圓餅）
- [ ] 前端：警示管理（條件建構器、觸發歷史）

### DoD
- [ ] 手算 10 筆交易的損益，與系統結果完全一致（含手續費、交易稅、除權息）
- [ ] 警示條件樹支援 3 層巢狀且測試覆蓋
- [ ] 警示不重複觸發（cooldown 生效測試）
- [ ] 風險指標與獨立計算（如 Excel）比對一致

---

## Phase 10 — Production

### 任務
- [ ] Prometheus metrics + Grafana dashboard（compose profile）
- [ ] 完整 `/health` 與資料新鮮度監控
- [ ] 錯誤追蹤（Sentry 或自建 error 表 + 告警）
- [ ] 效能：API p95 < 300ms、首頁首屏 < 2s、大表虛擬化驗證
- [ ] WebSocket（`/ws/alerts` `/ws/jobs`）
- [ ] 安全：完整 rate limit、`gitleaks` CI、`pip-audit`/`npm audit`、容器非 root
- [ ] 滲透測試檢查表（OWASP Top 10 逐項）
- [ ] 備份：`pg_dump` 每日 + 保留政策 + **還原演練**
- [ ] TimescaleDB compression + continuous aggregate 啟用
- [ ] E2E 測試（Playwright，10 條關鍵路徑）
- [ ] 文件：README、部署 runbook、故障排除、ATTRIBUTION、CHANGELOG
- [ ] 使用者文件：功能說明 + **免責聲明頁**

### DoD
- [ ] 所有 Gate 全綠
- [ ] E2E 10 條路徑全過
- [ ] 還原演練成功（從備份完整重建）
- [ ] 連續 7 天無人工介入正常運行，每日 pipeline 全部成功
- [ ] 安全檢查表逐項通過
- [ ] 冷啟動（全新機器 → `docker compose up` → 可用）< 30 分鐘

---

## 風險與緩解（跨 Phase）

| 風險 | 影響 Phase | 緩解 |
|------|-----------|------|
| 資料源改版或封鎖 | 2+ | Provider 抽象 + raw archive + circuit breaker；改版時只改 adapter |
| 歷史回補被 rate limit 擋 | 2 | 優先用全市場單日端點；非交易時段執行；可中斷可續跑 |
| 中文 NER 品質不如預期 | 4 | 字典優先策略是主力，LLM 只是補強；品質不足時降低 LLM 權重即可 |
| 本地 LLM 效能不足 | 4, 8 | `LLMProvider` 抽象可換模型/換供應商；`NoopProvider` 保證降級可用 |
| 因子全部無效（IC 太低） | 3, 5 | 這是可能的真實結果。誠實報告，AI Score 改以「資訊整合」而非「預測」定位 |
| ML 模型過擬合 | 6 | Walk-forward + embargo + purging + Deflated Sharpe + AUC 上限告警 |
| 供應鏈圖維護成本高 | 7 | 從 300 檔核心股開始，用 `ai_beta` 統計驗證；LLM 提議 + 人工核准降低人力 |
| 單機資源不足 | 全部 | compose profile 可選擇性啟動；`ENABLE_LLM=false` 降級 |
| 範圍蔓延 | 全部 | 每個 Phase 有明確 DoD；未達 DoD 不進下一個 Phase |

---

## 給自己的三條開發紀律

1. **不要為了看到畫面而跳過 Phase 2 的資料品質。** 錯的資料會污染後面所有 Phase，且錯誤會被 AI Score 放大並包裝成看起來很專業的數字。
2. **不要在 Phase 3 之前寫任何 AI Score 的程式碼。** 沒有驗證過的因子，Score 就只是把雜訊加權平均。
3. **每個 Phase 結束時，問自己：「如果現在停在這裡，這個系統對我有用嗎？」** 如果答案是否，代表這個 Phase 的切分方式錯了。

---

## 下一步（等待使用者確認）

Phase 0 已完成。進入 Phase 1 之前需要確認：

1. 架構方向是否符合預期（特別是 ADR-001 單一 Postgres、ADR-005 LLM 不參與計算、ADR-007 bitemporal）
2. 是否要調整 Phase 順序（例如先做 Phase 4 News 而非 Phase 3 Quant）
3. 程式碼要建在哪裡：這個雲端 session 的工作區、還是使用者本機的資料夾（需先連接）、還是推到 GitHub repo

確認後即可開始 Phase 1。
