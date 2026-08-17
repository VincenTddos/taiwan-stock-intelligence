# ARCHITECTURE.md — AI Taiwan Stock Intelligence Platform

> 版本 0.1 · 2026-08-15 · Phase 0 產出
> 專案代號：**twquant**
> 定位：**日頻 + 盤中延遲快照**的台股 AI 量化研究平台（非即時交易系統）
>
> ⚠️ **關於本系列文件中的數字**：所有 JSON 範例、分數、統計量（如 `total_score: 91.2`、
> `caar: 0.0142`、`impact_score: 0.92`）都是**格式示意**，用來定義資料結構與欄位語意，
> **不是真實的市場資料或已驗證的統計結果**。唯一經過實測的數字在 `DATA_SOURCES.md`
> 中標記為 ✅ VERIFIED 的區塊。實作時這些示意值必須全部由真實計算取代 ——
> 任何示意值洩漏到產品 UI 都屬於 `REPO_AUDIT.md` §9 定義的嚴重缺陷。

---

## 目錄

1. [System Overview](#1-system-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Data Flow](#3-data-flow)
4. [Market Data Pipeline](#4-market-data-pipeline)
5. [News Pipeline](#5-news-pipeline)
6. [AI Pipeline](#6-ai-pipeline)
7. [Database Architecture](#7-database-architecture)
8. [API Architecture](#8-api-architecture)
9. [Frontend Architecture](#9-frontend-architecture)
10. [Background Jobs](#10-background-jobs)
11. [Cache Strategy](#11-cache-strategy)
12. [Security](#12-security)
13. [Observability](#13-observability)
14. [Testing Strategy](#14-testing-strategy)
15. [Deployment Strategy](#15-deployment-strategy)
16. [Scaling Strategy](#16-scaling-strategy)
17. [十個架構決策問題的回答](#17-十個架構決策問題的回答)
18. [Architecture Decision Index](#18-architecture-decision-index)

---

## 1. System Overview

### 1.1 這個系統要回答的問題

系統的價值不在於「顯示了多少資料」，而在於能否回答這七個問題，且**每個答案都能追溯到證據**：

| 問題 | 由哪些模組共同回答 |
|------|------------------|
| 現在市場發生了什麼？ | Market Regime + Sector Intelligence + News Intelligence + Anomaly |
| 哪些 AI 產業正在變強？ | Sector Intelligence + Supply Chain Graph + Factor Engine |
| 哪些股票正在出現異常？ | Anomaly Engine（量、價、波動、新聞、情緒、相關性斷裂） |
| 這則新聞會影響哪些台股？ | News Entity Extraction → Stock Linker → Supply Chain 傳播 |
| 哪些股票四面向同時轉強？ | AI Scoring Engine（技術/基本/籌碼/情緒 分項 + 交叉篩選） |
| 這個 AI Score 為什麼是 91？ | Score Explanation（每個貢獻項的 contribution + evidence + model_version） |
| 這個策略歷史回測如何？ | Backtest Engine（point-in-time 資料 + walk-forward） |

### 1.2 設計哲學（五條，衝突時依序仲裁）

1. **Correctness > Features。** 一個帶 look-ahead bias 的漂亮回測比沒有回測更糟，因為它會讓人虧錢。
2. **Provenance > Convenience。** 每個數字都要能回答「哪來的、什麼時間、哪個模型、哪版特徵」。做不到的數字不顯示。
3. **Explainable > Accurate。** 一個 AUC 0.62 但能解釋的模型，比 AUC 0.65 的黑盒更有用 —— 因為使用者要做決策的是人。
4. **Boring > Clever。** 單機 Docker Compose 能解決的事，不引入 Kafka。
5. **Degrade gracefully。** 任一 provider / LLM / 外部服務掛掉，系統降級但不整體失效。

### 1.3 系統邊界（明確寫出「不做什麼」）

| 不做 | 原因 |
|------|------|
| 下單 / 券商串接 | 這是研究平台，不是交易系統。降低法遵與資安面積 |
| 逐筆成交 / 委託簿深度 | 免費來源不提供，且無授權 |
| 保證獲利 / 明牌 / 確定性預測 | 所有模型輸出一律標示為機率性推論 |
| 多租戶 SaaS | 個人自用，RBAC 保留但不做計費、組織管理 |
| 期權定價 / 衍生品 | 超出範圍，未來可作為獨立 module |

### 1.4 21 個核心模組與其歸屬層

```
┌─ Presentation ──────────────────────────────────────────────┐
│  18 Dashboard      19 Authentication      21 Admin Console   │
└─────────────────────────────────────────────────────────────┘
┌─ Intelligence ──────────────────────────────────────────────┐
│  16 AI Research Copilot        17 RAG Knowledge Engine       │
│  07 AI Scoring Engine          15 Alert Engine               │
└─────────────────────────────────────────────────────────────┘
┌─ Analytics ─────────────────────────────────────────────────┐
│  04 Technical Analysis   05 Quant Engine    08 ML Prediction │
│  09 Anomaly Detection    10 Event Detection 11 Sector Intel  │
│  12 Supply Chain Graph   13 Portfolio       14 Backtesting   │
└─────────────────────────────────────────────────────────────┘
┌─ Data ──────────────────────────────────────────────────────┐
│  01 Market Data Engine   02 News Intelligence  03 Financial  │
│                    + Data Quality Layer                      │
└─────────────────────────────────────────────────────────────┘
┌─ Platform ──────────────────────────────────────────────────┐
│  20 Observability   Scheduler   Cache   Config   Audit       │
└─────────────────────────────────────────────────────────────┘
```

**依賴方向永遠向下。** Analytics 不得直接呼叫外部 API；只能讀 Data 層落地後的表。這條規則保證了「回測用的資料 = 生產用的資料」。

---

## 2. Architecture Diagram

### 2.1 部署視圖（單機 Docker Compose）

```
                        ┌──────────────────────┐
                        │   Browser (User)     │
                        └──────────┬───────────┘
                                   │ https
                    ┌──────────────▼──────────────┐
                    │  caddy / nginx (reverse px) │
                    └───┬─────────────────────┬───┘
                        │                     │
            ┌───────────▼────────┐   ┌────────▼─────────────┐
            │  web               │   │  api                 │
            │  Next.js 15        │──▶│  FastAPI (uvicorn)   │
            │  :3000             │   │  :8000               │
            └────────────────────┘   └───┬──────────────┬───┘
                                         │              │
     ┌───────────────────────────────────┼──────────────┼─────────────┐
     │                                   │              │             │
┌────▼──────────────────┐   ┌────────────▼───┐   ┌──────▼─────┐  ┌────▼──────┐
│ postgres:16           │   │ redis:7        │   │ ollama     │  │ minio     │
│  + timescaledb        │   │  db0 cache     │   │  LLM +     │  │ (raw資料  │
│  + pgvector           │   │  db1 broker    │   │  embedding │  │  歸檔)    │
│  :5432                │   │  :6379         │   │  :11434    │  │  :9000    │
└───────────────────────┘   └────────┬───────┘   └────────────┘  └───────────┘
     ▲          ▲                    │
     │          │          ┌─────────▼──────────┐   ┌──────────────┐
     │          └──────────│ worker (celery)    │   │ beat         │
     │                     │  x N concurrency   │◀──│ (scheduler)  │
     │                     └─────────┬──────────┘   └──────────────┘
     │                               │
     │                     ┌─────────▼──────────┐
     │                     │  External Sources  │
     └─────────────────────│  TWSE / TPEx /     │
                           │  TAIFEX / MOPS /   │
                           │  News RSS          │
                           └────────────────────┘

  觀測：flower(:5555) · /health · /metrics(Prometheus text) · structlog→stdout
```

**為什麼 API 與 Worker 分離但共用 codebase**：同一個 `twquant` package，兩個進入點（`uvicorn api.main:app` / `celery -A jobs.app worker`）。共用 model、provider、config，避免邏輯漂移；但故障域隔離 —— worker 打爆記憶體不會拖垮 API。

### 2.2 邏輯視圖（模組依賴）

```
                       ┌────────────────────────┐
                       │   AI Research Copilot  │  ← 只能透過 Tool 存取下層
                       └───────────┬────────────┘
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
     ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
     │  RAG Engine     │  │  AI Score       │  │  Backtest /     │
     │  (pgvector)     │  │  + Explanation  │  │  Event Study    │
     └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
              │                    │                    │
              │      ┌─────────────┼─────────────┐      │
              │      ▼             ▼             ▼      │
              │  ┌────────┐  ┌──────────┐  ┌─────────┐  │
              │  │ Factor │  │ Technical│  │ ML Pred │  │
              │  │ Engine │  │ Indicator│  │ (LGBM)  │  │
              │  └────┬───┘  └─────┬────┘  └────┬────┘  │
              │       └────────────┼────────────┘       │
              │                    ▼                    │
              │        ┌────────────────────────┐       │
              └───────▶│   Curated Data Layer   │◀──────┘
                       │  (PostgreSQL / TSDB)   │
                       └───────────┬────────────┘
                                   ▲
                       ┌───────────┴────────────┐
                       │  Data Quality Layer    │
                       └───────────┬────────────┘
                                   ▲
              ┌────────────────────┼────────────────────┐
       ┌──────┴──────┐      ┌──────┴──────┐      ┌──────┴──────┐
       │ Market Data │      │  Financial  │      │    News     │
       │  Provider   │      │  Provider   │      │  Provider   │
       └─────────────┘      └─────────────┘      └─────────────┘
```

---

## 3. Data Flow

### 3.1 三層資料模型（Medallion 精簡版）

```
RAW              →  NORMALIZED         →  CURATED           →  DERIVED
原始 payload         型別/單位/時區統一      正規化業務實體        指標/因子/分數
────────────────────────────────────────────────────────────────────────
存檔案系統/MinIO      staging table        core tables         analytics tables
不可變、可重放         可丟棄可重建          唯一真實來源          可從 curated 重算
保留 90 天            保留 7 天             永久                永久（帶版本）
```

**RAW 層必須存在**，理由有三：
1. Provider 改版時可以回頭 replay 重建，不用重打 API
2. 資料爭議時可以驗證「是我們解析錯還是來源錯」
3. 回測結果可重現（reproducibility）

### 3.2 端到端資料流

```
                          Data Sources
                                │
              ┌─────────────────┼─────────────────┐
              ↓                 ↓                 ↓
           Market            News             Financial
       (TWSE/TPEx/TAIFEX)   (RSS/API)          (MOPS)
              │                 │                 │
              └────────┬────────┴────────┬────────┘
                       ↓                 ↓
                 RAW ARCHIVE      (raw_ingestions 表記錄
                 (MinIO/FS)        來源/URL/hash/抓取時間)
                       ↓
              ┌────────────────┐
              │  Normalization │  民國年→西元 · 字串→數值 · 千分位
              │                │  "--"/""→NULL · 時區→UTC(+存 Asia/Taipei)
              └────────┬───────┘
                       ↓
              ┌────────────────┐
              │  Validation    │  ← DataValidationEngine
              │                │  失敗 → quarantine 表 + alert，不進 curated
              └────────┬───────┘
                       ↓
              ┌────────────────┐
              │  Data Storage  │  ← PostgreSQL / TimescaleDB
              │  (bitemporal)  │     event_time + knowledge_time
              └────────┬───────┘
                       ↓
              ┌────────────────┐
              │ Feature Eng.   │  ← 只讀 curated，產出帶 feature_version
              └────────┬───────┘
                       ↓
       ┌───────────────┼───────────────┐
       ↓               ↓               ↓
    Quant            NLP              ML
  (indicators     (entity/         (LightGBM
   factors)        sentiment)       inference)
       │               │               │
       └───────────────┼───────────────┘
                       ↓
              ┌────────────────┐
              │   AI Engine    │  ← Score 合成 + Explanation
              └────────┬───────┘
                       ↓
              ┌────────────────┐
              │  API / Cache   │
              └────────┬───────┘
                       ↓
                  Dashboard
```

### 3.3 時間語意（整個系統最關鍵的設計）

每筆分析用資料都攜帶三個時間：

| 欄位 | 意義 | 用途 |
|------|------|------|
| `event_time` / `trading_date` / `period_end` | 事情**發生**的時間 | 對齊、繪圖 |
| `announced_at` / `published_at` | 資訊**公開**的時間 | **回測過濾的唯一依據** |
| `ingested_at` | 我們**取得**的時間 | 資料新鮮度監控、debug |

**硬性規則**：任何回測 / 特徵計算的 `as_of` 過濾，一律使用 `announced_at <= as_of`，**永不**使用 `event_time <= as_of`。
違反此規則的 SQL 會被 CI 的靜態檢查攔下（見 §14.4）。

---

## 4. Market Data Pipeline

### 4.1 Provider 抽象（Adapter Pattern）

```python
# providers/base.py
class BaseMarketDataProvider(ABC):
    name: str
    supports: set[Capability]      # {DAILY_OHLCV, INDEX, INSTITUTIONAL, INTRADAY, ORDERBOOK}
    rate_limit: RateLimitSpec

    @abstractmethod
    async def fetch_daily_ohlcv(
        self, trading_date: date, symbols: list[str] | None = None
    ) -> list[RawRecord]: ...

    @abstractmethod
    async def fetch_index(self, trading_date: date) -> list[RawRecord]: ...

    @abstractmethod
    async def fetch_institutional(self, trading_date: date) -> list[RawRecord]: ...

    async def health(self) -> ProviderHealth: ...
```

```
BaseMarketDataProvider
    ├── TWSEProvider          上市：OpenAPI(快照) + RWD web API(歷史)
    ├── TPExProvider          上櫃：OpenAPI(快照) + web API(歷史)
    ├── TAIFEXProvider        期貨/選擇權三大法人、大額交易人
    ├── LicensedProvider      介面預留，未實作（無授權）
    └── MockProvider          僅 dev/test，import 時檢查 APP_ENV
```

**MockProvider 的防呆**（Phase 1 就要寫）：

```python
class MockProvider(BaseMarketDataProvider):
    def __init__(self, settings: Settings):
        if settings.APP_ENV == "production":
            raise RuntimeError("MockProvider is forbidden in production")
        self._demo = True
```

且所有經 MockProvider 產生的資料，資料列上帶 `source='MOCK'`，API 回應帶 `"demo": true`，前端**強制**顯示 `DEMO DATA` 紅色角標。這是不可繞過的。

### 4.2 Provider Registry 與降級

```python
registry = ProviderRegistry(
    daily_ohlcv=[TWSEProvider, TPExProvider],   # 依市場路由
    index=[TWSEProvider],
    institutional=[TWSEProvider, TPExProvider],
)
```

- 每個 provider 有獨立的 **circuit breaker**（連續 5 次失敗 → 開路 10 分鐘）
- 開路時：有 fallback provider 則切換；沒有則該資料集標記 `stale`，**但系統其他部分照常運作**
- `system_health` 表記錄每個 provider 的最後成功時間 → Dashboard 顯示

### 4.3 韌性四件套（每個外呼都必備）

| 機制 | 實作 | 預設值 |
|------|------|--------|
| Timeout | `httpx.Timeout(connect=5, read=20)` | 20s |
| Retry | `tenacity` 指數退避 + jitter，只對 5xx / 連線錯誤重試（4xx 不重試） | 3 次，1s→2s→4s |
| Rate limit | Redis token bucket（跨 worker 共享） | 3 req/s per host，可設定 |
| Cache | 依端點語意設 TTL（見 §11） | — |

### 4.4 歷史回補策略（Phase 2 核心工程）

```
優先序 1：全市場單日端點（一次請求 = 一天全市場）
          10 年 × 250 交易日 ≈ 2,500 次請求
優先序 2：個股單月端點（僅用於補洞）
          由 data_gaps 表驅動，只補真正缺的
```

回補 job 特性：
- **可中斷可續跑**：`backfill_progress` 表記錄 (dataset, cursor_date, status)
- **idempotent**：`ON CONFLICT (symbol, trading_date) DO UPDATE`
- **交易日曆驅動**：非交易日直接跳過，不打 API
- **raw 快取**：同一 (endpoint, date) 已有 raw 檔就不重打

### 4.5 交易日曆

台股日曆不能用「週一到週五」硬算 —— 有農曆春節、颱風假、補行上班日（週六開市）。

實作：`trading_calendar` 表，來源為 TWSE 每年公告的休市日期表 + 用「當日全市場成交量 > 0」做交叉驗證。缺日曆時，job 拒絕執行而非猜測。

---

## 5. News Pipeline

```
News Sources (RSS / 官方公告 / 財經媒體開放來源)
        ↓
  ┌─────────────┐
  │  Fetcher    │  遵守 robots.txt · 帶 UA · rate limit · ETag/Last-Modified
  └──────┬──────┘
         ↓  raw HTML/XML → RAW archive
  ┌─────────────┐
  │ Normalizer  │  抽正文(trafilatura) · 統一時區 · 清 boilerplate
  └──────┬──────┘
         ↓
  ┌─────────────┐
  │ Deduplicator│  三層：URL canonical → SimHash(標題+前 200 字) → embedding 相似度 > 0.93
  └──────┬──────┘         同一事件多家轉載 → 合併為 news_cluster
         ↓
  ┌─────────────┐
  │ Entity      │  字典比對(Aho-Corasick, 由 t187ap03_L 自動建 alias) 
  │ Extraction  │  + LLM 補充未知實體(產品/技術/人名)
  └──────┬──────┘
         ↓
  ┌─────────────┐
  │ Stock Linker│  DIRECT(字典命中) → SUPPLIER/CUSTOMER/COMPETITOR(供應鏈圖)
  │             │  → INDUSTRY/THEMATIC(產業標籤) → MACRO
  └──────┬──────┘
         ↓
  ┌─────────────┐
  │ Sentiment   │  中文金融情緒：LLM 分類 + 規則詞典校準，輸出 [-1,1] + confidence
  └──────┬──────┘
         ↓
  ┌─────────────┐
  │ Event       │  EARNINGS / GUIDANCE / ORDER_WIN / CAPEX / M&A / REGULATION /
  │ Classifier  │  SUPPLY_SHOCK / RATING_CHANGE / MACRO / GEOPOLITICS ...
  └──────┬──────┘
         ↓
  ┌─────────────┐
  │ Impact      │  impact = f(relation_strength, sentiment, event_type_weight,
  │ Scorer      │            source_credibility, novelty)  ← 全部可設定、可解釋
  └──────┬──────┘
         ↓
  ┌─────────────┐
  │ Reaction    │  T+1/3/5/10/20 的實際報酬與異常報酬回填 → 用來校準 impact_score
  │ Analysis    │  （這是讓系統會「學習」的閉環）
  └─────────────┘
         ↓
   news_stock_relations / news_events / news_embeddings
```

**關鍵設計：Reaction Analysis 閉環。** 影響力分數不是憑空給的 —— 事件發生後 T+N 回填實際異常報酬，統計「這類事件在這類股票上的歷史平均反應」，反過來校準未來的 impact_score。這讓 impact_score 從「LLM 的猜測」變成「歷史統計 + 模型推論」。詳見 `AI_ENGINE.md` §4。

---

## 6. AI Pipeline

```
                    Curated Data
                         │
        ┌────────────────┼────────────────┐
        ↓                ↓                ↓
  Technical         Fundamental       News/Sentiment
  Features          Features          Features
  (指標/動能/量能)   (成長/品質/評價)   (情緒/熱度/事件)
        │                │                │
        │           Institutional         │
        │           Features              │
        │                │                │
        └────────────────┼────────────────┘
                         ↓
              ┌──────────────────────┐
              │   Feature Store      │  feature_version + as_of
              │  (ml_features 表)    │  point-in-time correct
              └──────────┬───────────┘
                         │
            ┌────────────┴────────────┐
            ↓                         ↓
    ┌───────────────┐        ┌────────────────┐
    │ Rule-based    │        │  ML Models     │
    │ Sub-scores    │        │  (LightGBM)    │
    │ 透明、可審計    │        │  P(return>θ)   │
    └───────┬───────┘        └────────┬───────┘
            │                         │
            └────────────┬────────────┘
                         ↓
              ┌──────────────────────┐
              │  AI Score Composer   │  加權合成（權重存 DB，可設定）
              │  + Explanation       │  每項貢獻度 + evidence
              └──────────┬───────────┘
                         ↓
              ai_scores + ai_score_contributions
                         ↓
              ┌──────────────────────┐
              │  Copilot (Tool Call) │  ← 只讀上面算好的結果
              │  + RAG (pgvector)    │
              └──────────────────────┘
```

**重要：LLM 不參與分數計算。** LLM 的角色是（a）新聞理解、（b）把算好的結果翻譯成自然語言。分數本身由確定性程式碼 + 可版本化的 ML 模型產生。這是可解釋性與可重現性的前提。

---

## 7. Database Architecture

### 7.1 為什麼是「一個 PostgreSQL」

單機情境下，PostgreSQL 16 + TimescaleDB + pgvector 三合一容器可同時扮演：

- **關聯式核心**：stocks / users / portfolios / news …
- **時序資料庫**：hypertable + compression + continuous aggregate（取代 InfluxDB）
- **向量資料庫**：pgvector HNSW 索引（取代 Qdrant / Milvus）

好處是**跨域 JOIN 直接可做**：「找出 AI 供應鏈中、近 20 天 AI Score 上升最快、且有正面新聞的股票」在單一 SQL 內完成。若拆三個資料庫，這個查詢要在應用層做三次往返 + 手動 join。

### 7.2 儲存分層

| 資料 | 存放 | 理由 |
|------|------|------|
| 主資料（stocks / sectors / users / portfolios / alerts） | PostgreSQL 一般表 | 需要交易與外鍵 |
| 日線、指標、因子、分數 | TimescaleDB hypertable（`chunk_time_interval = 1 month`） | 時間分區裁剪 + 壓縮 |
| 分鐘線（未來） | hypertable + compression policy（30 天後壓縮） | 量大，壓縮率通常 >10x |
| 新聞向量 | pgvector `vector(1024)` + HNSW | 與新聞 metadata 同表可過濾 |
| 快取 / rate limit / session | Redis | 易失、需 TTL |
| Celery broker + result | Redis db1 | 與 cache 隔離 DB index |
| RAW payload / 模型 artifact / 回測明細 | MinIO（S3 相容）或檔案系統 | 大 blob 不進 DB |

### 7.3 兩個必須遵守的 schema 慣例

**(1) Bitemporal 基本面表**

```sql
-- 錯誤示範（會造成 look-ahead bias）
SELECT revenue FROM monthly_revenue WHERE period = '2026-06';

-- 正確：帶 announced_at
SELECT revenue FROM monthly_revenue
WHERE symbol = '2330' AND announced_at <= :as_of
ORDER BY period_end DESC LIMIT 1;
```

**(2) 所有衍生資料帶版本三元組**

```
(dataset_version, feature_version, model_version)
```

任何 `ai_scores` 的一列都能回答「這個 92 分是用哪一版模型、哪天的資料、哪些特徵算的」。

完整表結構見 `ERD.md`。

---

## 8. API Architecture

- **風格**：REST + JSON，版本前綴 `/api/v1`
- **框架**：FastAPI（自動產生 OpenAPI 3.1 → `/api/v1/openapi.json`、Swagger `/docs`）
- **契約**：所有 request/response 都是 Pydantic v2 model，前端用 `openapi-typescript` 產生 TS 型別 → **前後端型別不可能漂移**
- **一律非阻塞**：任何預期 > 2 秒的操作（回測、事件研究、批次評分）回傳 `202 Accepted + job_id`，前端輪詢 `/api/v1/jobs/{id}`
- **統一 envelope**：每個回應都帶 `meta`（`data_timestamp` / `source` / `model_version` / `is_demo` / `cache`）—— 這是「所有回答都必須附資料時間與來源」原則的 API 層落實

```json
{
  "data": [...],
  "meta": {
    "data_timestamp": "2026-08-15T05:30:00Z",
    "trading_date": "2026-08-15",
    "source": ["TWSE"],
    "model_version": "stockrank-lgbm-v1.4",
    "dataset_version": "2026-08-15",
    "is_demo": false,
    "cache": {"hit": true, "age_seconds": 42},
    "quality": {"overall": 98.5}
  }
}
```

詳見 `API_SPEC.md`。

---

## 9. Frontend Architecture

### 9.1 技術選型

| 層 | 選擇 | 理由 |
|----|------|------|
| Framework | Next.js 15 App Router | RSC 可選、路由檔案化、與 Node 生態一致 |
| 語言 | TypeScript strict | 與後端 OpenAPI 型別對接 |
| 樣式 | Tailwind CSS + shadcn/ui | 密集資訊介面需要高度客製，shadcn 是可複製到專案的原始碼而非黑盒元件庫 |
| 伺服器狀態 | TanStack Query | 快取、背景重取、去重、樂觀更新 |
| 客戶端狀態 | Zustand | 只放真正的 UI 狀態（版面、篩選、選中的股票） |
| 圖表 | TradingView Lightweight Charts (Apache-2.0) | K 線效能最好且授權明確 |
| 資料視覺化 | Recharts / visx（熱力圖、雷達圖） | 非 K 線圖表 |
| 圖譜 | Cytoscape.js 或 react-force-graph | 供應鏈圖需要互動與 layout 演算法 |
| 表格 | TanStack Table + TanStack Virtual | 3,000 檔股票必須虛擬化 |

### 9.2 目錄結構

```
web/
├── app/
│   ├── (dashboard)/page.tsx          市場總覽
│   ├── stock/[symbol]/page.tsx       個股頁（tab 化）
│   ├── screener/page.tsx             選股器
│   ├── sector/page.tsx               產業輪動
│   ├── supply-chain/page.tsx         供應鏈圖
│   ├── portfolio/page.tsx
│   ├── backtest/page.tsx
│   ├── alerts/page.tsx
│   ├── copilot/page.tsx
│   └── admin/page.tsx
├── components/
│   ├── ui/                           shadcn 元件
│   ├── charts/                       圖表封裝
│   ├── market/                       業務元件
│   └── meta/DataProvenance.tsx       ★ 顯示 timestamp/source/model/confidence
├── lib/
│   ├── api/                          由 OpenAPI 產生的 client + 型別
│   ├── query/                        TanStack Query keys 與 hooks
│   └── format/                       數字/日期/漲跌色（台股紅漲綠跌）
└── stores/
```

### 9.3 設計語言

方向：**Bloomberg Terminal 的資訊密度 + TradingView 的圖表 + Linear 的克制**。

- Dark mode 為預設（金融從業者的實際使用情境）
- 台股慣例：**紅漲綠跌**（與美股相反，這是硬性需求）
- 等寬數字字體（`font-variant-numeric: tabular-nums`）—— 數字要能對齊掃視
- 動畫僅用於狀態轉換提示，不做裝飾性動畫
- 鍵盤優先：`/` 全域搜尋、`g s` 跳個股、`j/k` 列表移動、`?` 快捷鍵表

### 9.4 效能規範（硬性）

| 規範 | 做法 |
|------|------|
| 不得每次 render 重打 API | TanStack Query `staleTime` 依資料類型設定（盤中報價 30s、日線 5min、財報 1h） |
| 大表不得一次載入 | Server-side pagination + TanStack Virtual |
| 圖表不重繪整張 | Lightweight Charts 的 `update()` 增量更新 |
| 重計算 memo 化 | `useMemo` / `useDeferredValue` 於篩選與排序 |
| 首頁不阻塞 | 各區塊獨立 Suspense boundary，慢的區塊不擋快的 |
| 即時推送 | Phase 10 才做 WebSocket（`/ws/quotes`）；在那之前用 polling，因為資料本身是日頻 |

---

## 10. Background Jobs

### 10.1 排程表（台北時間；台股 09:00–13:30）

| Job | Cron (Asia/Taipei) | 型態 | Timeout | 重試 |
|-----|-------------------|------|---------|------|
| `calendar.sync` | 每年 12/15 + 每月 1 日 | 全量 | 5m | 3 |
| `market.snapshot` | 交易日 09:05–13:30 每 5 分 | 增量 | 60s | 2 |
| `market.eod_prices` | 交易日 14:30 | 增量 | 15m | 5 |
| `market.institutional` | 交易日 16:00 | 增量 | 10m | 5 |
| `market.margin_short` | 交易日 17:00 | 增量 | 10m | 3 |
| `financial.monthly_revenue` | 每日 20:00（每月 1–10 日為主） | 增量 | 20m | 3 |
| `financial.statements` | 每日 20:30 | 增量 | 30m | 3 |
| `news.fetch` | 每 10 分 | 增量 | 5m | 2 |
| `news.process` | 每 15 分 | 佇列消化 | 30m | 2 |
| `news.reaction_backfill` | 每日 18:00 | 回填 T+1/3/5/10/20 | 20m | 3 |
| `quant.indicators` | 交易日 15:00 | 全量重算當日 | 20m | 3 |
| `quant.factors` | 交易日 15:20 | 全量 | 20m | 3 |
| `ml.inference` | 交易日 15:40 | batch | 30m | 2 |
| `ai.score` | 交易日 16:30（法人資料後） | 全量 | 20m | 3 |
| `anomaly.detect` | 交易日 16:45 | 全量 | 10m | 2 |
| `sector.rotation` | 交易日 17:00 | 全量 | 10m | 2 |
| `regime.detect` | 交易日 17:10 | 全量 | 5m | 2 |
| `alerts.evaluate` | 交易日每 15 分 + 17:30 | 條件掃描 | 5m | 2 |
| `rag.embed` | 每小時 | 增量 | 30m | 2 |
| `quality.check` | 每小時 | 檢查 | 5m | 1 |
| `model.monitor` | 每日 22:00 | 漂移監控 | 10m | 1 |
| `report.daily` | 交易日 18:00 | 產出當日簡報 | 10m | 2 |
| `db.backup` | 每日 03:00 | `pg_dump` | 30m | 2 |

### 10.2 每個 Job 的強制要求

```python
@app.task(bind=True, autoretry_for=(TransientError,),
          retry_backoff=True, retry_jitter=True, max_retries=5,
          soft_time_limit=..., time_limit=...)
@idempotent(key=lambda **kw: f"{kw['dataset']}:{kw['trading_date']}")
@traced                       # 產生 job_run_id，寫入 job_runs 表
def eod_prices(self, trading_date: date): ...
```

- **Idempotent**：同一 (job, 參數) 重跑結果一致。用 Redis 分散式鎖防重入
- **Timeout**：`soft_time_limit` 先拋例外讓 job 自己清理，`time_limit` 強制殺
- **Logging**：進入/離開/筆數/耗時，全部 structlog 結構化
- **Failure handling**：失敗寫 `job_runs.status='failed'` + 錯誤摘要 → Dashboard 紅燈
- **交易日守門**：非交易日的市場類 job 直接 skip 並記錄 `skipped_non_trading_day`

---

## 11. Cache Strategy

### 11.1 三層快取

```
L1  進程內 LRU（cachetools）    交易日曆、股票主檔、產業對照   TTL 5–60 min
L2  Redis                      API 回應、計算結果、rate limit  TTL 見下表
L3  PostgreSQL 物化表           指標/因子/分數（本身就是快取）   由 job 重算
```

### 11.2 TTL 表

| 內容 | Key 樣式 | TTL | 失效方式 |
|------|---------|-----|---------|
| 盤中快照 | `quote:{symbol}` | 30s | TTL |
| 日線序列 | `bars:{symbol}:{tf}:{from}:{to}` | 收盤前 5m / 收盤後 24h | 版本前綴 |
| 股票主檔 | `stock:{symbol}` | 24h | 主檔 job 完成後 bump 版本 |
| AI Score | `score:{symbol}:{date}` | 至隔日 08:30 | 評分 job 完成後 bump |
| 排行榜 | `rank:{type}:{date}:{page}` | 10m | 評分 job 完成後刪 pattern |
| 新聞列表 | `news:{filter_hash}:{page}` | 3m | TTL |
| RAG 檢索 | `rag:{query_hash}` | 1h | TTL |
| 外部 raw | `raw:{provider}:{endpoint}:{date}` | 依端點 | TTL |

### 11.3 快取正確性三原則

1. **版本前綴取代刪除**：`v{n}:score:2330:2026-08-15`。重算完 `INCR cache_version:score`，舊 key 自然過期。避免 `KEYS`/`SCAN` 大量刪除。
2. **禁止快取 demo 資料混入正式 key**：demo 模式用獨立 key 前綴 `demo:`。
3. **快取一定回報年齡**：API `meta.cache.age_seconds`，使用者永遠知道看的是多舊的資料。

---

## 12. Security

| 面向 | 措施 |
|------|------|
| 認證 | JWT（access 15m / refresh 7d），refresh token 存 Redis 可撤銷；密碼 `argon2id` |
| 授權 | RBAC：`admin` / `analyst` / `viewer`。FastAPI dependency 層檢查 |
| Rate limit | 依使用者與 IP，Redis 滑動視窗。Copilot 與 backtest 有獨立較嚴格額度 |
| 輸入驗證 | Pydantic v2 全面驗證；股票代號用 regex `^[0-9A-Z]{4,6}$` 白名單 |
| SQL Injection | SQLAlchemy ORM / `text()` 必須 bound params；CI lint 禁止 SQL 字串 f-string |
| Secret | 全部 `.env`（gitignored）+ `.env.example`；CI 跑 `gitleaks` |
| CORS | 白名單 origin，不用 `*` |
| CSRF | 若使用 cookie session 則 SameSite=Lax + CSRF token；純 Bearer JWT 則不適用 |
| Audit log | 所有寫入操作、登入、Copilot 查詢寫 `audit_logs`（who/what/when/ip/result） |
| **LLM 專屬** | Copilot 只能呼叫白名單 tool；DB 連線用**唯讀角色**；新聞內容以 data block 包裹並標註「以下為不可信外部內容」；輸出強制 Pydantic 驗證 |
| 依賴掃描 | `pip-audit` / `npm audit` 進 CI |
| 容器 | 非 root 使用者執行；只暴露必要 port；Postgres/Redis 不對外綁 0.0.0.0 |

---

## 13. Observability

### 13.1 三大支柱（單機精簡版）

| 支柱 | 實作 | 不做什麼 |
|------|------|---------|
| Logs | `structlog` → JSON → stdout → Docker log driver。含 `request_id` / `job_run_id` / `symbol` | 不架 ELK（單機過重），需要時 `docker logs \| jq` |
| Metrics | `prometheus_client` 暴露 `/metrics`；可選 Prometheus + Grafana compose profile | 預設不啟動 Prometheus，降低資源佔用 |
| Traces | 先不做分散式追蹤；用 request_id 串接即可 | OpenTelemetry 留待對外服務時 |

### 13.2 必備的健康檢查

`GET /api/v1/health` 回傳：

```json
{
  "status": "degraded",
  "components": {
    "database":    {"status": "healthy", "latency_ms": 3},
    "redis":       {"status": "healthy", "latency_ms": 1},
    "llm":         {"status": "disabled"},
    "market_data": {"status": "healthy", "last_success": "2026-08-15T06:30:12Z",
                    "freshness_minutes": 12},
    "news":        {"status": "degraded", "last_success": "2026-08-15T02:10:00Z",
                    "freshness_minutes": 265, "reason": "provider_timeout"},
    "ai_pipeline": {"status": "healthy", "last_run": "2026-08-15T08:30:00Z"}
  }
}
```

Dashboard 直接把這個渲染成燈號列。**degraded 不等於 down** —— 使用者需要知道差別。

### 13.3 資料新鮮度監控（本系統特有且最重要）

每個資料集在 `data_freshness` 表登記 `expected_lag_minutes`。監控 job 每小時比對 `now() - last_ingested_at > expected_lag`，超過即標 stale 並在 API `meta.quality` 反映。

**規則：stale 的資料照樣顯示，但一定要標示。** 隱藏 stale 資料比顯示它更危險。

### 13.4 模型監控

見 `AI_ENGINE.md` §8。監控項目：prediction drift、feature drift（PSI）、calibration（Brier score / reliability curve）、實際命中率隨時間變化。

---

## 14. Testing Strategy

### 14.1 測試金字塔

```
        ┌──────────┐
        │   E2E    │  Playwright，~10 條關鍵路徑
        ├──────────┤
        │Integration│ testcontainers 起真 Postgres+Redis，~80 條
        ├──────────┤
        │   Unit    │  純函式，指標/因子/回測/風險，~400 條
        └──────────┘
        + 專屬的 ML / Data 正確性測試（最關鍵，見 14.4）
```

### 14.2 Unit Test

- **指標正確性**：用手算或 TA-Lib 的已知答案做黃金測試（golden test），不是「跑得動就好」
- **因子**：邊界案例（除權息、停牌、上市未滿 N 天、分母為 0）
- **回測**：交易成本、滑價、部位計算的算術正確性
- **Normalizer**：用**真實 payload fixture**（民國年、千分位、`"--"`、空字串）

### 14.3 Integration Test

用 `testcontainers` 起真實 Postgres（含 timescaledb/pgvector）與 Redis：
- Migration 可正向 upgrade 與反向 downgrade
- API 端點的完整往返
- Provider 用 `respx` mock HTTP（不打真 API），但驗證 retry / rate limit / circuit breaker 行為
- 完整 pipeline：raw → normalize → validate → curated → indicator → score

### 14.4 ★ 資料正確性測試（本專案的靈魂）

這四條測試如果沒過，整個平台的所有數字都是垃圾：

| 測試 | 方法 |
|------|------|
| **No look-ahead bias** | 建構合成資料集，其中「未來資訊」被塗成極端值。若特徵計算或回測有偷看，指標會爆表 → 測試失敗。另外靜態掃描：任何 `WHERE period_end <= :as_of` 而非 `announced_at` 的查詢，CI 直接擋 |
| **No data leakage** | 訓練集與測試集的時間區間不得重疊；`walk_forward_splits()` 的每個 fold 斷言 `train.max_date < test.min_date - embargo_days` |
| **Feature consistency** | 同一 (symbol, as_of, feature_version) 用 training path 與 serving path 各算一次，斷言完全相等（training/serving skew 檢測） |
| **Model reproducibility** | 固定 seed + 固定 dataset_version，訓練兩次，斷言 metrics 完全一致 |

### 14.5 E2E（Playwright）

登入 → Dashboard 載入 → 搜尋 2330 → 個股頁各 tab → AI Score 展開解釋 → 加入自選 → 建立 Alert → Copilot 問一題並看到引用來源。

### 14.6 品質門檻（CI 擋門）

```
ruff check . && ruff format --check .
mypy --strict twquant/
pytest --cov=twquant --cov-fail-under=80
alembic upgrade head && alembic downgrade -1 && alembic upgrade head
pnpm lint && pnpm typecheck && pnpm build
gitleaks detect
```

**任一失敗 → 不得進入下一個 Phase。**

---

## 15. Deployment Strategy

### 15.1 三種環境

| 環境 | 用途 | 資料源 | LLM |
|------|------|--------|-----|
| `local` | 開發 | MockProvider 或真實 provider（可切換） | 可關閉 |
| `staging` | 驗收 | 真實 provider，獨立 DB | 開啟 |
| `production` | 自用正式 | 真實 provider，**MockProvider 被禁用** | 開啟 |

環境由 `APP_ENV` 決定，啟動時 `Settings` 做一致性檢查（例如 production 必須有真 secret、必須關 debug、必須非 Mock）。

### 15.2 Compose profiles

```
docker compose --profile core up          # postgres redis api worker beat web
docker compose --profile llm up           # + ollama
docker compose --profile observability up # + prometheus grafana flower
docker compose --profile storage up       # + minio
```

低配機器可以只跑 `core`，系統以降級模式運作。

### 15.3 Migration 與資料保護

- Schema 變更一律 Alembic，**禁止手改 DB**
- 破壞性變更（drop column）分兩步部署：先停止寫入 → 觀察一個週期 → 再刪除
- 每次 `docker compose up` 前自動 `pg_dump` 快照（保留最近 3 份）
- Seed 資料（產業分類、供應鏈初始圖、交易日曆）用可重跑的 seed script，非 migration

### 15.4 備份

```
每日 03:00  pg_dump --format=custom  → backups/pg_YYYYMMDD.dump  (保留 7 天 + 每月 1 份保留 12 個月)
每週       MinIO/raw 目錄 rsync 到外接儲存
還原演練    每季一次，記錄在 runbook
```

---

## 16. Scaling Strategy

**原則：Start Simple → Measure → Scale。每個擴展動作都要有觸發指標，不預先擴展。**

| 階段 | 觸發指標 | 動作 |
|------|---------|------|
| S0 現況 | — | 單機 compose，全部單實例 |
| S1 | EOD pipeline > 30 分鐘 | Celery worker 併發數↑；因子計算改 polars lazy |
| S2 | `daily_prices`/`intraday` > 5,000 萬列或查詢 > 1s | 啟用 TimescaleDB compression + continuous aggregate |
| S3 | 分鐘線查詢仍慢 | 加 continuous aggregate 做 5m/15m/1h 預聚合 |
| S4 | Redis 記憶體 > 70% | broker 與 cache 分離為兩個實例 |
| S5 | 向量 > 500 萬且檢索 > 200ms | 調 HNSW 參數 → 仍不足才評估獨立向量庫 |
| S6 | 需要對外多使用者 | API 水平擴展（無狀態）+ 反向代理負載平衡 + Postgres read replica |
| S7 | 需要真即時 tick | 才引入串流（Redis Streams 先於 Kafka）+ `LicensedProvider` |
| S8 | 單機無法承載 | 才考慮 K8s。**在此之前一律不碰** |

**明確不做**：Kafka（S7 之前）、Spark（polars 在單機足以處理 TB 以下）、K8s（S8 之前）、ClickHouse（TimescaleDB 撐不住才換）。

---

## 17. 十個架構決策問題的回答

### Q1. 哪些功能應該是 synchronous？

使用者在等、且能在 **< 500ms** 內從已落地資料回答的讀取操作：

- 個股報價 / K 線 / 指標 / 因子 / AI Score 讀取（皆為預算好的結果）
- 股票搜尋、自選股、投資組合檢視
- 新聞列表與詳情、排行榜、產業熱力圖
- Alert CRUD、使用者設定
- 健康檢查

原則：**同步 API 只讀不算。** 任何需要現場計算的東西都不是同步。

### Q2. 哪些功能應該是 asynchronous？

- 所有外部資料抓取（market / news / financial ingestion）
- 指標、因子、AI Score、ML 推論的批次計算
- **回測**（秒到分鐘級）→ `202 + job_id`
- **事件研究**（跨全市場統計）→ 同上
- 新聞 NLP 處理（LLM 推論慢）
- Embedding 生成
- Alert 條件掃描與通知發送
- 報表產生、資料品質檢查、模型監控、備份

**Copilot 是混合的**：對話串流用 SSE 同步回應，但它呼叫的 tool 若是重運算（如 `run_backtest`），tool 內部轉為 async job 並回傳「已排入佇列，完成後通知」而非讓對話卡住。

### Q3. 哪些資料應該進 PostgreSQL（一般表）？

需要**交易一致性、外鍵約束、頻繁更新**的實體資料：

`users` `roles` `stocks` `markets` `sectors` `industries` `supply_chain_nodes` `supply_chain_edges` `portfolios` `positions` `transactions` `alerts` `alert_events` `watchlists` `model_versions` `dataset_versions` `job_runs` `audit_logs` `system_health` `trading_calendar` `news`（metadata）`news_entities` `news_stock_relations` `backtests` `backtest_metrics`

### Q4. 哪些資料應該進 Redis？

**易失、需 TTL、高頻讀、可重建**：

- API 回應快取、計算結果快取
- Rate limit 計數器（滑動視窗）
- 分散式鎖（job idempotency）
- Session / refresh token 撤銷清單
- Celery broker + result backend（db1）
- 盤中最新報價快照（TTL 30s）
- 排行榜的短期快取

**不放**：任何唯一真實來源的資料。Redis 全清應該只造成變慢，不造成資料遺失。

### Q5. 哪些資料適合 TimescaleDB（hypertable）？

以時間為主軸、**只追加、按時間範圍查詢**的資料：

| 表 | 分區間隔 | 壓縮 |
|----|---------|------|
| `daily_prices` | 1 month | 1 年後 |
| `intraday_prices`（未來） | 1 day | 30 天後 |
| `index_prices` | 1 month | 1 年後 |
| `institutional_trading` | 1 month | 1 年後 |
| `margin_short` | 1 month | 1 年後 |
| `technical_indicators` | 1 month | 6 個月後 |
| `factor_scores` | 1 month | 6 個月後 |
| `ai_scores` | 1 month | 6 個月後 |
| `ml_predictions` | 1 month | 6 個月後 |
| `ml_features` | 1 month | 3 個月後（量最大） |
| `anomalies` | 1 month | — |
| `data_quality_scores` | 1 month | 3 個月後 |

### Q6. 哪些資料適合 Object Storage？

- **RAW payload 歸檔**（JSON/HTML 原始回應）—— 量大、只在 replay 時讀
- **模型 artifact**（`.txt` LightGBM booster、前處理 pipeline pickle）
- **回測逐筆交易明細**（一次回測可能數萬列，存 parquet 比進 DB 便宜）
- **每日報表 PDF/HTML 快照**
- **DB 備份**

單機用 MinIO（S3 相容），未來換雲端 S3 只改 endpoint。

### Q7. 哪些資料需要向量資料庫（pgvector）？

- `news_embeddings.embedding`：新聞向量（去重 + 語意檢索 + RAG）
- `document_chunks.embedding`：財報、法說會逐字稿、MOPS 公告、產業報告、公司業務描述的 chunk 向量（`documents.doc_type` 區分類型，`COMPANY_PROFILE` 即用於「找相似公司」）
- （Phase 7 選配）事件向量：在 `documents` 中以 `doc_type='EVENT_SUMMARY'` 承載，用於「找出歷史上類似的事件」—— 這是「歷史上發生類似事件時股票怎麼走」功能的核心

**不需要向量的**：所有數值型時序資料。相似度用統計方法（correlation / DTW）比 embedding 更正確。

### Q8. 哪些任務應該使用 Queue？

- 所有排程 job（Celery beat → queue → worker）
- 使用者觸發的重運算：回測、事件研究、自訂選股掃描、Copilot 的重工具
- 扇出型任務：新聞處理（每則新聞一個 task）、批次 embedding
- 通知發送

**Queue 分流**（同一 Redis 不同 queue name，避免長任務餓死短任務）：

```
q_ingest     資料抓取（IO 密集，高併發）
q_compute    指標/因子/評分（CPU 密集，低併發）
q_nlp        LLM 推論（序列化，併發 1–2，避免 GPU 爭用）
q_user       使用者觸發（回測等，中優先）
q_maint      備份/監控/清理（最低優先）
```

### Q9. 哪些模型需要 batch inference？

**幾乎全部。** 因為資料源是日頻：

- AI Score 各分項模型（每日收盤後全市場 ~2,000 檔）
- ML 報酬機率模型 `P(5D return > 3%)` 等
- 因子模型、異常偵測、市場 regime 分類
- 新聞情緒與事件分類（批次處理當日新聞）
- Embedding 生成

批次的好處：可以在單一 DataFrame 上向量化，比逐檔推論快 100 倍以上，且天然帶 `as_of` 一致性。

### Q10. 哪些模型需要 real-time inference？

在目前資料授權下，**只有兩個**：

1. **Copilot 的 LLM 生成**（使用者對話，必須即時，用 SSE 串流）
2. **RAG 的 query embedding**（單一查詢向量化，毫秒級）

未來若取得盤中授權資料，再加入：盤中異常偵測（`AnomalyEngine` 的線上版）、盤中新聞的即時影響評估。屆時走 Redis Streams + 常駐 consumer，模型從 registry 載入記憶體，不需要改變上層架構 —— 這正是把 Provider 與 Engine 解耦的回報。

---

## 18. Architecture Decision Index

Each decision is recorded as its own document under [`docs/adr/`](adr/), which
holds the full context, rationale, consequences and the condition under which
the decision should be revisited. This section is an index only — it is
deliberately not a copy.

| ADR | Title | Status | Decision | |
|-----|-------|--------|----------|---|
| **001** | Single PostgreSQL for relational, time-series and vector data | Accepted | One PostgreSQL instance carries relational, time-series and vector workloads. | [→](adr/ADR-001-single-postgresql-for-relational-time-series-and-vector-data.md) |
| **002** | Celery with a Redis broker, rather than RQ or Dramatiq | Accepted | Background work runs on Celery with Redis as broker and result backend. | [→](adr/ADR-002-celery-with-a-redis-broker-rather-than-rq-or-dramatiq.md) |
| **003** | FastAPI, Pydantic v2 and SQLAlchemy 2.0 | Accepted | Backend is FastAPI + Pydantic v2 + SQLAlchemy 2.0 async + Alembic. | [→](adr/ADR-003-fastapi-pydantic-v2-and-sqlalchemy-20.md) |
| **004** | Next.js App Router with client-side data fetching | Accepted | Next.js App Router, but data is fetched client-side via TanStack Query. | [→](adr/ADR-004-nextjs-app-router-with-client-side-data-fetching.md) |
| **005** | The LLM does not participate in numeric calculation | Accepted | The LLM never computes a number that is stored or displayed as a measurement. | [→](adr/ADR-005-the-llm-does-not-participate-in-numeric-calculation.md) |
| **006** | Dictionary-first entity recognition, with the LLM as reinforcement | Accepted | Entity recognition matches an exchange-derived alias dictionary first; the LLM only supplements. | [→](adr/ADR-006-dictionary-first-entity-recognition-with-the-llm-as-reinforc.md) |
| **007** | Bitemporal fundamental data | Accepted | Fundamental records store both `period_end` and `announced_at`; history filters on the latter. | [→](adr/ADR-007-bitemporal-fundamental-data.md) |
| **008** | The supply-chain graph lives in PostgreSQL, not Neo4j | Accepted | The supply-chain graph is Postgres tables traversed with recursive CTEs, not Neo4j. | [→](adr/ADR-008-the-supply-chain-graph-lives-in-postgresql-not-neo4j.md) |
| **009** | LightGBM as the first-stage model | Accepted | The first ML models are LightGBM, not sequence models. | [→](adr/ADR-009-lightgbm-as-the-first-stage-model.md) |
| **010** | No order execution | Accepted | The platform does not connect to brokers and does not place orders. | [→](adr/ADR-010-no-order-execution.md) |
| **011** | The LLM is an optional service | Accepted | Ollama is opt-in; core works unchanged with `ENABLE_LLM=false`. | [→](adr/ADR-011-the-llm-is-an-optional-service.md) |
| **012** | Red for up, green for down | Accepted | Red means up and green means down, following Taiwanese market convention. | [→](adr/ADR-012-red-for-up-green-for-down.md) |
| **013** | One Docker image for API, worker, beat and Flower | Accepted | API, worker, beat and Flower run from one image, differing only by command. | [→](adr/ADR-013-one-docker-image-for-api-worker-beat-and-flower.md) |
| **014** | Migrations run as a one-shot compose service | Accepted | `alembic upgrade head` runs as a one-shot service that must exit zero before anything starts. | [→](adr/ADR-014-migrations-run-as-a-one-shot-compose-service.md) |
| **015** | TimescaleDB is created conditionally and enforced by configuration | Accepted | TimescaleDB is created only when available; `REQUIRE_TIMESCALEDB` makes it mandatory outside local/test. | [→](adr/ADR-015-timescaledb-is-created-conditionally-and-enforced-by-configu.md) |
| **016** | Cache namespace versions start at zero | Accepted | A missing cache-version counter reads as 0, so the first invalidation is not a no-op. | [→](adr/ADR-016-cache-namespace-versions-start-at-zero.md) |
| **017** | Module boundaries are enforced by the linter | Accepted | `ruff` bans importing `app.api` from lower layers, so the layering cannot silently rot. | [→](adr/ADR-017-module-boundaries-are-enforced-by-the-linter.md) |
| **018** | Database health probes run sequentially | Accepted | Database health probes run sequentially; one AsyncSession cannot serve concurrent statements. | [→](adr/ADR-018-database-health-probes-run-sequentially.md) |
| **019** | `disabled` is a first-class health status, distinct from `degraded` | Accepted | An intentionally-disabled component reports `disabled` and never degrades system status. | [→](adr/ADR-019-disabled-is-a-first-class-health-status-distinct-from-degrad.md) |
| **020** | Refresh tokens rotate on use and are revocable | Accepted | Refresh tokens rotate on use and land on a Redis denylist, making theft detectable and logout real. | [→](adr/ADR-020-refresh-tokens-rotate-on-use-and-are-revocable.md) |
| **021** | The dashboard shows empty states, never placeholder numbers | Accepted | Panels without a data source render an empty state — never a sample number, never a zero. | [→](adr/ADR-021-the-dashboard-shows-empty-states-never-placeholder-numbers.md) |
| **022** | Corporate actions are a separate provider abstraction | Accepted | A dedicated `CorporateActionProvider`, resolved from the registry; no exchange endpoint reaches the domain or service layer. | [→](adr/ADR-022-corporate-actions-are-a-separate-provider-abstraction.md) |
| **023** | Adjusted prices require verified corporate action coverage | Accepted | Adjustment raises unless coverage for that symbol and range was recorded; an empty result is not evidence that nothing happened. | [→](adr/ADR-023-adjustment-requires-verified-corporate-action-coverage.md) |

**Phase 0** produced ADR-001 – ADR-012 (system shape and principles). **Phase 1** produced ADR-013 – ADR-021 (decisions forced by building it, three of which were prompted by defects the test suite found). **Phase 3 design** produced ADR-022 – ADR-023 (corporate actions, and the precondition they impose on every adjusted series).

Adding a decision: copy the structure of an existing file, take the next number, and add one row here. A decision worth arguing about later is worth a file; a decision nobody will question does not need one.
-----|------|------|-------------|
| ADR-001 | 單一 PostgreSQL 承載關聯 + 時序 + 向量 | 單機省資源；跨域 JOIN 是本產品的核心查詢型態 | 向量 > 500 萬且檢索 > 200ms；或時序 > 5 億列 |
| ADR-002 | Celery + Redis 而非 RQ / Dramatiq | beat 排程成熟、Flower 可觀測、社群大 | 若只剩 < 5 個 job 可簡化為 APScheduler |
| ADR-003 | FastAPI + Pydantic v2 + SQLAlchemy 2.0 | 型別安全、自動 OpenAPI、async 原生 | 無 |
| ADR-004 | Next.js App Router 但先全 client-side 取數 | 個人自用不需 SSR SEO；降低複雜度 | 對外服務需要首屏效能時改 RSC |
| ADR-005 | LLM 不參與分數計算 | 可解釋性與可重現性；LLM 不確定性不可進入量化結果 | 無（這是原則性決策） |
| ADR-006 | 字典優先的實體辨識，LLM 僅補強 | 中文公司別名有限且可列舉；LLM 在此任務上會 hallucinate | 若實測 LLM NER F1 > 0.95 可調整權重，但字典仍為 ground truth |
| ADR-007 | Bitemporal 基本面表 | 這是避免 look-ahead bias 唯一可靠的方法 | 無（原則性決策） |
| ADR-008 | 供應鏈圖存 Postgres 而非 Neo4j | 規模小，recursive CTE 足夠；少一個服務 | 3 跳查詢 > 500ms |
| ADR-009 | LightGBM 為第一階段模型 | 金融 tabular data 上的可靠 baseline；訓練快、可解釋（SHAP）、CPU 即可 | baseline 建立後才評估序列模型 |
| ADR-010 | 不做即時交易與下單 | 縮小法遵與資安面積；產品定位是研究 | 無 |
| ADR-011 | Ollama 為可選服務（`ENABLE_LLM`） | 個人機器資源有限，系統不得因 LLM 缺席而失效 | 無 |
| ADR-012 | 紅漲綠跌 | 台股使用者慣例 | 提供設定開關但預設不變 |

---

## 附錄 A：Repository 目錄結構（Phase 1 建立）

```
twquant/
├── docker-compose.yml
├── docker/
│   ├── api.Dockerfile
│   ├── web.Dockerfile
│   └── postgres/init.sql          # CREATE EXTENSION timescaledb, vector
├── .env.example
├── Makefile
├── pyproject.toml
├── alembic.ini
├── alembic/versions/
├── docs/                          # ← Phase 0 產出
│   ├── REPO_AUDIT.md
│   ├── ARCHITECTURE.md
│   ├── ERD.md
│   ├── DATA_SOURCES.md
│   ├── API_SPEC.md
│   ├── AI_ENGINE.md
│   ├── QUANT_ENGINE.md
│   └── DEVELOPMENT_ROADMAP.md
├── twquant/                       # Python package
│   ├── core/                      # config, logging, errors, deps, security
│   ├── db/                        # models/, session, repositories/
│   ├── providers/                 # base, twse, tpex, taifex, mops, news/, mock
│   ├── ingest/                    # 各 dataset 的 ingestion 邏輯
│   ├── quality/                   # validation, quality score, quarantine
│   ├── calendar/                  # 交易日曆
│   ├── indicators/                # 技術指標（純函式）
│   ├── factors/                   # 因子（純函式）
│   ├── scoring/                   # AI Score 合成 + explanation
│   ├── ml/                        # features/, train/, registry/, inference/
│   ├── backtest/                  # engine, portfolio, costs, metrics
│   ├── anomaly/
│   ├── events/                    # event detection + event study
│   ├── sectors/                   # rotation, breadth
│   ├── regime/
│   ├── graph/                     # supply chain
│   ├── news/                      # normalize, dedup, ner, linker, sentiment
│   ├── rag/                       # chunk, embed, retrieve
│   ├── copilot/                   # tools/, agent, prompts
│   ├── portfolio/
│   ├── alerts/
│   ├── api/v1/                    # routers/, schemas/, deps
│   └── jobs/                      # celery app, tasks/, beat schedule
├── tests/
│   ├── unit/  integration/  ml/  e2e/  fixtures/
└── web/                           # Next.js
```

---

## 附錄 B：文件間的一致性契約

| 概念 | 定義於 | 引用於 |
|------|--------|--------|
| 資料表名稱 | `ERD.md` | ARCHITECTURE / API_SPEC / QUANT_ENGINE |
| API 路徑與 schema | `API_SPEC.md` | ARCHITECTURE §8 / 前端 |
| Provider 端點與授權 | `DATA_SOURCES.md` | ARCHITECTURE §4 / ERD |
| 因子與指標定義 | `QUANT_ENGINE.md` | AI_ENGINE / ERD |
| Score 組成與權重鍵名 | `AI_ENGINE.md` | API_SPEC / ERD |
| Phase 範圍與驗收 | `DEVELOPMENT_ROADMAP.md` | 全部 |

修改任一份文件時，必須檢查此表對應的下游文件。
