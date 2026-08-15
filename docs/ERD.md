# ERD.md — 資料庫設計

> 版本 0.1 · 2026-08-15
> PostgreSQL 16 + TimescaleDB + pgvector
> 所有時間欄位一律 `TIMESTAMPTZ`（UTC 儲存），日期欄位為台北交易日 `DATE`

---

## 0. 全域慣例（不可違反）

| 慣例 | 規則 |
|------|------|
| 主鍵 | `BIGSERIAL id`，或時序表用複合鍵 `(symbol, trading_date)` |
| 時間 | 時間點用 `TIMESTAMPTZ`；交易日用 `DATE`（台北時區語意，DB 不做轉換） |
| 金額/價格 | `NUMERIC(18,4)`，**禁止 FLOAT** |
| 股數 | `BIGINT`（單位：股） |
| 比率/分數 | `NUMERIC(10,6)` |
| 字串代號 | `VARCHAR(10)`，加 `CHECK (symbol ~ '^[0-9A-Z]{4,10}$')`（ETF 代號含字母） |
| 軟刪除 | 主資料用 `deleted_at TIMESTAMPTZ`；時序表不刪除 |
| 稽核欄位 | 所有表有 `created_at`、可變表加 `updated_at` |
| 來源欄位 | 所有外部資料表有 `source VARCHAR(20) NOT NULL` |
| 版本欄位 | 所有衍生資料有 `dataset_version` / `feature_version` / `model_version` 之一或多 |
| 命名 | snake_case、表名複數、外鍵 `{table_singular}_id` |
| ★ 時間語意 | 基本面/新聞類表**必須**同時有事件時間與知曉時間（bitemporal） |

---

## 1. 高層 ERD

```
┌──────────────────── MASTER ────────────────────┐
│  markets ──┬─ stocks ──┬── industries          │
│            │           ├── sectors             │
│  trading_calendar      └── entity_aliases      │
│  corporate_actions                             │
└────────────────────────────────────────────────┘
                     │
      ┌──────────────┼──────────────┬─────────────┐
      ▼              ▼              ▼             ▼
┌──────────┐  ┌─────────────┐ ┌───────────┐ ┌──────────┐
│ MARKET   │  │ FUNDAMENTAL │ │  FLOW     │ │  NEWS    │
│ daily_   │  │ financials  │ │institutio-│ │ news     │
│ prices   │  │ financial_  │ │nal_trading│ │ news_    │
│ index_   │  │  metrics    │ │margin_    │ │ entities │
│ prices   │  │ monthly_    │ │ short     │ │ news_    │
│ intraday │  │  revenue    │ │           │ │ stock_   │
│ (future) │  │             │ │           │ │ relations│
│ order_   │  │             │ │           │ │ news_    │
│ books    │  │             │ │           │ │ events   │
│ (future) │  │             │ │           │ │ news_emb │
└────┬─────┘  └──────┬──────┘ └─────┬─────┘ └────┬─────┘
     └───────────────┴──────┬───────┴────────────┘
                            ▼
              ┌──────────────────────────┐
              │      DERIVED             │
              │  technical_indicators    │
              │  factor_scores           │
              │  ml_features             │
              │  ml_predictions          │
              │  ai_scores               │
              │  ai_score_contributions  │
              │  anomalies               │
              │  market_regimes          │
              │  sector_metrics          │
              └────────────┬─────────────┘
                           │
      ┌────────────────────┼────────────────────┐
      ▼                    ▼                    ▼
┌───────────┐    ┌──────────────────┐   ┌──────────────┐
│  GRAPH    │    │   RESEARCH       │   │  USER        │
│ supply_   │    │ events           │   │ users        │
│ chain_    │    │ event_studies    │   │ portfolios   │
│ nodes     │    │ backtests        │   │ positions    │
│ supply_   │    │ backtest_trades  │   │ transactions │
│ chain_    │    │ backtest_metrics │   │ watchlists   │
│ edges     │    │ documents        │   │ alerts       │
│           │    │ document_chunks  │   │ alert_events │
└───────────┘    └──────────────────┘   └──────────────┘

┌──────────────── PLATFORM ─────────────────┐
│ dataset_versions  feature_versions        │
│ model_versions    job_runs                │
│ audit_logs        system_health           │
│ data_quality_scores  data_gaps            │
│ raw_ingestions    scoring_weights         │
└───────────────────────────────────────────┘
```

---

## 2. MASTER

### 2.1 `markets`

```sql
CREATE TABLE markets (
    id          SMALLSERIAL PRIMARY KEY,
    code        VARCHAR(10) UNIQUE NOT NULL,   -- TWSE / TPEX / EMERGING / US
    name_zh     VARCHAR(50) NOT NULL,
    name_en     VARCHAR(50) NOT NULL,
    timezone    VARCHAR(40) NOT NULL DEFAULT 'Asia/Taipei',
    currency    CHAR(3)     NOT NULL DEFAULT 'TWD',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 2.2 `stocks`

```sql
CREATE TABLE stocks (
    id                  BIGSERIAL PRIMARY KEY,
    symbol              VARCHAR(10) NOT NULL,
    market_id           SMALLINT NOT NULL REFERENCES markets(id),
    name_zh             VARCHAR(100) NOT NULL,
    short_name_zh       VARCHAR(50),
    name_en             VARCHAR(150),
    short_name_en       VARCHAR(50),
    security_type       VARCHAR(20) NOT NULL,   -- COMMON / ETF / TDR / REIT / WARRANT
    industry_id         INT REFERENCES industries(id),
    listing_date        DATE,
    delisting_date      DATE,                   -- ★ NOT NULL 即為已下市，回測必用
    is_active           BOOLEAN NOT NULL DEFAULT true,
    par_value           NUMERIC(18,4),
    paid_in_capital     NUMERIC(20,2),
    shares_outstanding  BIGINT,
    tax_id              VARCHAR(20),
    chairman            VARCHAR(100),
    ceo                 VARCHAR(100),
    spokesperson        VARCHAR(100),
    website             VARCHAR(255),
    address             VARCHAR(255),
    source              VARCHAR(20) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    CONSTRAINT uq_stocks_symbol_market UNIQUE (symbol, market_id),
    CONSTRAINT ck_stocks_symbol CHECK (symbol ~ '^[0-9A-Z]{4,10}$')
);
CREATE INDEX ix_stocks_active   ON stocks(is_active) WHERE deleted_at IS NULL;
CREATE INDEX ix_stocks_industry ON stocks(industry_id);
```

> **`delisting_date` 是避免 survivorship bias 的關鍵。** 回測的股票池必須包含當時存在、後來下市的股票。任何 `WHERE is_active = true` 的回測查詢都是錯的。

### 2.3 `industries` / `sectors`

```sql
CREATE TABLE sectors (          -- 大類：半導體、電子零組件、金融、傳產…
    id        SERIAL PRIMARY KEY,
    code      VARCHAR(20) UNIQUE NOT NULL,
    name_zh   VARCHAR(50) NOT NULL,
    name_en   VARCHAR(50),
    parent_id INT REFERENCES sectors(id),        -- 支援階層
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE industries (       -- 交易所產業別（TWSE 產業別代碼）
    id         SERIAL PRIMARY KEY,
    code       VARCHAR(20) UNIQUE NOT NULL,
    name_zh    VARCHAR(50) NOT NULL,
    sector_id  INT REFERENCES sectors(id),
    source     VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**注意**：交易所的「產業別」（如「半導體業」）太粗，無法支撐「CoWoS 概念股」這類需求。因此另有 `supply_chain_nodes`（§8）承載細緻主題標籤，兩者並存互補。

### 2.4 `entity_aliases`（News NER 的字典來源）

```sql
CREATE TABLE entity_aliases (
    id          BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(20) NOT NULL,   -- STOCK / PERSON / PRODUCT / TECH / COUNTRY / ORG
    stock_id    BIGINT REFERENCES stocks(id),   -- entity_type=STOCK 時必填
    alias       VARCHAR(150) NOT NULL,
    lang        CHAR(2) NOT NULL DEFAULT 'zh',
    is_ambiguous BOOLEAN NOT NULL DEFAULT false, -- 例如「聯電」vs「聯發科」前綴衝突
    priority    SMALLINT NOT NULL DEFAULT 100,   -- 衝突時取高優先
    source      VARCHAR(20) NOT NULL,            -- AUTO_FROM_MASTER / MANUAL / LLM_PROPOSED
    approved    BOOLEAN NOT NULL DEFAULT false,  -- LLM 提議的別名需人工核准
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_alias UNIQUE (entity_type, alias, lang)
);
CREATE INDEX ix_alias_lookup ON entity_aliases(alias) WHERE approved;
```

自動建立來源：TWSE `t187ap03_L` 的 `公司名稱`/`公司簡稱`/`英文簡稱`/`公司代號` 四欄（見 `DATA_SOURCES.md` §2.1）。

### 2.5 `trading_calendar`

```sql
CREATE TABLE trading_calendar (
    market_id       SMALLINT NOT NULL REFERENCES markets(id),
    calendar_date   DATE NOT NULL,
    is_trading_day  BOOLEAN NOT NULL,
    session_type    VARCHAR(20) NOT NULL DEFAULT 'FULL', -- FULL / HALF / CLOSED
    open_time       TIME,                                -- 09:00
    close_time      TIME,                                -- 13:30
    note            VARCHAR(100),                        -- 春節 / 颱風 / 補行上班
    source          VARCHAR(20) NOT NULL,
    verified_by_volume BOOLEAN NOT NULL DEFAULT false,   -- 用成交量交叉驗證過
    PRIMARY KEY (market_id, calendar_date)
);
```

> 所有市場類 job 的第一步：查 `trading_calendar`。**日曆缺該日 → job 拒絕執行並告警，不得猜測。**

### 2.6 `corporate_actions`（還原權值必需）

```sql
CREATE TABLE corporate_actions (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        BIGINT NOT NULL REFERENCES stocks(id),
    action_type     VARCHAR(20) NOT NULL,   -- CASH_DIV / STOCK_DIV / SPLIT / REVERSE_SPLIT
                                            -- / RIGHTS_ISSUE / CAPITAL_REDUCTION
    ex_date         DATE NOT NULL,          -- 除權息交易日（事件時間）
    announced_at    TIMESTAMPTZ NOT NULL,   -- ★ 公告時間（知曉時間）
    payment_date    DATE,
    cash_dividend   NUMERIC(18,6),
    stock_dividend  NUMERIC(18,6),          -- 每股配股數
    split_ratio     NUMERIC(18,6),
    adjust_factor   NUMERIC(18,10) NOT NULL,-- 累積還原因子（由 job 計算）
    source          VARCHAR(20) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_ca UNIQUE (stock_id, action_type, ex_date)
);
```

**設計決策**：`daily_prices` 存**未還原**原始價（忠於來源），還原價由 `adjust_factor` 在查詢時計算或存於物化視圖 `daily_prices_adjusted`。理由：來源資料改版時不需重寫歷史；且「當時實際成交價」在事件研究中有其意義。

---

## 3. MARKET（TimescaleDB Hypertables）

### 3.1 `daily_prices`

```sql
CREATE TABLE daily_prices (
    stock_id      BIGINT NOT NULL REFERENCES stocks(id),
    trading_date  DATE   NOT NULL,
    open          NUMERIC(18,4),
    high          NUMERIC(18,4),
    low           NUMERIC(18,4),
    close         NUMERIC(18,4),
    prev_close    NUMERIC(18,4),
    change        NUMERIC(18,4),
    change_pct    NUMERIC(10,6),
    volume        BIGINT,          -- 股
    turnover      NUMERIC(20,2),   -- 元
    trade_count   INT,
    limit_up      BOOLEAN,
    limit_down    BOOLEAN,
    is_suspended  BOOLEAN NOT NULL DEFAULT false,
    source        VARCHAR(20) NOT NULL,
    quality_flags TEXT[],          -- 觸發的 WARN 規則 ID
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trading_date),
    CONSTRAINT ck_ohlc CHECK (
        low IS NULL OR high IS NULL OR (
            low <= high AND low <= COALESCE(open, low) AND COALESCE(open, low) <= high
            AND low <= COALESCE(close, low) AND COALESCE(close, low) <= high
        )
    )
);
SELECT create_hypertable('daily_prices', 'trading_date',
                         chunk_time_interval => INTERVAL '1 month');
CREATE INDEX ix_dp_date ON daily_prices(trading_date DESC);
```

### 3.2 `index_prices`

```sql
CREATE TABLE index_prices (
    index_code    VARCHAR(30) NOT NULL,   -- TAIEX / TPEX / 電子類指數 …
    trading_date  DATE NOT NULL,
    open  NUMERIC(18,4), high NUMERIC(18,4),
    low   NUMERIC(18,4), close NUMERIC(18,4) NOT NULL,
    change NUMERIC(18,4), change_pct NUMERIC(10,6),
    volume BIGINT, turnover NUMERIC(20,2),
    source VARCHAR(20) NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (index_code, trading_date)
);
SELECT create_hypertable('index_prices','trading_date', chunk_time_interval=>INTERVAL '1 year');
```

### 3.3 `market_stats`（大盤統計／市場廣度）

```sql
CREATE TABLE market_stats (
    market_id       SMALLINT NOT NULL REFERENCES markets(id),
    trading_date    DATE NOT NULL,
    total_volume    BIGINT,
    total_turnover  NUMERIC(20,2),
    total_trades    BIGINT,
    advancing       INT,      -- 上漲家數
    declining       INT,
    unchanged       INT,
    limit_up_count  INT,
    limit_down_count INT,
    new_high_52w    INT,
    new_low_52w     INT,
    above_ma20_pct  NUMERIC(10,6),   -- 市場廣度核心指標
    above_ma60_pct  NUMERIC(10,6),
    source          VARCHAR(20) NOT NULL,
    PRIMARY KEY (market_id, trading_date)
);
```

### 3.4 `intraday_prices` / `order_books`（★ 保留但不建立）

```sql
-- 需要 LicensedProvider 授權後才啟用。
-- migration 中以註解保留，schema 先定義以確保未來加入時不需改動上層。
--
-- CREATE TABLE intraday_prices (
--     stock_id BIGINT NOT NULL, ts TIMESTAMPTZ NOT NULL,
--     timeframe VARCHAR(5) NOT NULL,  -- 1m/5m/15m/30m/1h
--     open/high/low/close NUMERIC(18,4), volume BIGINT,
--     source VARCHAR(20) NOT NULL,
--     PRIMARY KEY (stock_id, timeframe, ts)
-- );
-- SELECT create_hypertable('intraday_prices','ts', chunk_time_interval=>INTERVAL '1 day');
--
-- CREATE TABLE order_books (
--     stock_id BIGINT NOT NULL, ts TIMESTAMPTZ NOT NULL,
--     bids JSONB, asks JSONB, source VARCHAR(20) NOT NULL,
--     PRIMARY KEY (stock_id, ts)
-- );
```

---

## 4. FUNDAMENTAL（★ Bitemporal）

### 4.1 `financials`

```sql
CREATE TABLE financials (
    id              BIGSERIAL PRIMARY KEY,
    stock_id        BIGINT NOT NULL REFERENCES stocks(id),
    statement_type  VARCHAR(20) NOT NULL,  -- INCOME / BALANCE / CASHFLOW
    fiscal_year     SMALLINT NOT NULL,
    fiscal_quarter  SMALLINT,              -- NULL = 年報
    period_end      DATE NOT NULL,         -- ★ 事件時間
    announced_at    TIMESTAMPTZ NOT NULL,  -- ★ 知曉時間（回測唯一依據）
    announced_at_is_estimated BOOLEAN NOT NULL DEFAULT false,
    report_type     VARCHAR(20) NOT NULL,  -- CONSOLIDATED / PARENT
    is_restated     BOOLEAN NOT NULL DEFAULT false,
    revision        SMALLINT NOT NULL DEFAULT 0,   -- 同期別的第 n 次修正
    currency        CHAR(3) NOT NULL DEFAULT 'TWD',
    unit            VARCHAR(10) NOT NULL DEFAULT 'TWD',
    data            JSONB NOT NULL,        -- 完整科目（科目代碼 → 金額）
    source          VARCHAR(20) NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_fin UNIQUE (stock_id, statement_type, period_end, report_type, revision),
    CONSTRAINT ck_fin_time CHECK (announced_at::date >= period_end)
);
CREATE INDEX ix_fin_pit ON financials(stock_id, announced_at DESC);
```

> **Point-in-time 查詢範例**（回測中唯一合法的寫法）：
> ```sql
> SELECT DISTINCT ON (stock_id, statement_type)
>        stock_id, statement_type, period_end, data
> FROM financials
> WHERE stock_id = ANY(:ids) AND announced_at <= :as_of
> ORDER BY stock_id, statement_type, announced_at DESC, revision DESC;
> ```
> `revision` 讓「財報後續更正」也能正確重現當時所見。

### 4.2 `financial_metrics`（從 `financials.data` 展開的常用指標）

```sql
CREATE TABLE financial_metrics (
    stock_id       BIGINT NOT NULL REFERENCES stocks(id),
    period_end     DATE NOT NULL,
    announced_at   TIMESTAMPTZ NOT NULL,   -- ★ 繼承自來源
    revenue                 NUMERIC(20,2),
    gross_profit            NUMERIC(20,2),
    operating_income        NUMERIC(20,2),
    net_income              NUMERIC(20,2),
    eps                     NUMERIC(18,4),
    total_assets            NUMERIC(20,2),
    total_liabilities       NUMERIC(20,2),
    total_equity            NUMERIC(20,2),
    operating_cashflow      NUMERIC(20,2),
    free_cashflow           NUMERIC(20,2),
    gross_margin            NUMERIC(10,6),
    operating_margin        NUMERIC(10,6),
    net_margin              NUMERIC(10,6),
    roe                     NUMERIC(10,6),
    roa                     NUMERIC(10,6),
    debt_to_equity          NUMERIC(10,6),
    current_ratio           NUMERIC(10,6),
    revenue_yoy             NUMERIC(10,6),
    eps_yoy                 NUMERIC(10,6),
    dataset_version         VARCHAR(30) NOT NULL,
    PRIMARY KEY (stock_id, period_end, announced_at)
);
SELECT create_hypertable('financial_metrics','period_end', chunk_time_interval=>INTERVAL '1 year');
```

### 4.3 `monthly_revenue`（台股特有的高頻基本面訊號）

```sql
CREATE TABLE monthly_revenue (
    stock_id        BIGINT NOT NULL REFERENCES stocks(id),
    revenue_month   DATE NOT NULL,          -- 該月第一天，事件時間
    announced_at    TIMESTAMPTZ NOT NULL,   -- ★ 知曉時間（法定次月 10 日前）
    announced_at_is_estimated BOOLEAN NOT NULL DEFAULT false,
    revenue         NUMERIC(20,2) NOT NULL,
    revenue_mom     NUMERIC(10,6),
    revenue_yoy     NUMERIC(10,6),
    cum_revenue     NUMERIC(20,2),
    cum_revenue_yoy NUMERIC(10,6),
    note            TEXT,
    source          VARCHAR(20) NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, revenue_month, announced_at)
);
```

### 4.4 `valuation_daily`

```sql
CREATE TABLE valuation_daily (
    stock_id     BIGINT NOT NULL REFERENCES stocks(id),
    trading_date DATE NOT NULL,
    pe_ratio     NUMERIC(12,4),
    pb_ratio     NUMERIC(12,4),
    dividend_yield NUMERIC(10,6),
    market_cap   NUMERIC(20,2),
    ps_ratio     NUMERIC(12,4),
    ev_ebitda    NUMERIC(12,4),
    source       VARCHAR(20) NOT NULL,
    PRIMARY KEY (stock_id, trading_date)
);
SELECT create_hypertable('valuation_daily','trading_date', chunk_time_interval=>INTERVAL '1 month');
```

---

## 5. FLOW（籌碼）

### 5.1 `institutional_trading`

欄位直接對應 `DATA_SOURCES.md` §11.3 的映射表。

```sql
CREATE TABLE institutional_trading (
    stock_id            BIGINT NOT NULL REFERENCES stocks(id),
    trading_date        DATE NOT NULL,
    foreign_buy         BIGINT,
    foreign_sell        BIGINT,
    foreign_net         BIGINT,
    foreign_dealer_net  BIGINT,
    trust_buy           BIGINT,
    trust_sell          BIGINT,
    trust_net           BIGINT,
    dealer_self_net     BIGINT,
    dealer_hedge_net    BIGINT,
    dealer_net          BIGINT,
    total_net           BIGINT,
    -- 衍生（由 job 計算，方便查詢）
    foreign_net_value   NUMERIC(20,2),   -- 淨買賣股數 × 均價
    total_net_value     NUMERIC(20,2),
    net_to_volume_pct   NUMERIC(10,6),   -- 三大法人淨額 / 當日成交量
    source              VARCHAR(20) NOT NULL,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trading_date)
);
SELECT create_hypertable('institutional_trading','trading_date', chunk_time_interval=>INTERVAL '1 month');
```

### 5.2 `margin_short`（融資融券、借券）

```sql
CREATE TABLE margin_short (
    stock_id          BIGINT NOT NULL REFERENCES stocks(id),
    trading_date      DATE NOT NULL,
    margin_buy        BIGINT, margin_sell BIGINT, margin_redeem BIGINT,
    margin_balance    BIGINT, margin_limit BIGINT, margin_usage_pct NUMERIC(10,6),
    short_sell        BIGINT, short_cover BIGINT, short_redeem BIGINT,
    short_balance     BIGINT,
    sbl_short_balance BIGINT,          -- 借券賣出餘額
    margin_short_ratio NUMERIC(10,6),  -- 券資比
    source            VARCHAR(20) NOT NULL,
    PRIMARY KEY (stock_id, trading_date)
);
SELECT create_hypertable('margin_short','trading_date', chunk_time_interval=>INTERVAL '1 month');
```

### 5.3 `futures_institutional`（台指期三大法人，Market Regime 用）

```sql
CREATE TABLE futures_institutional (
    contract        VARCHAR(20) NOT NULL,   -- TX / MTX / TE / TF
    trading_date    DATE NOT NULL,
    investor_type   VARCHAR(20) NOT NULL,   -- FOREIGN / TRUST / DEALER
    long_oi         BIGINT, short_oi BIGINT, net_oi BIGINT,
    net_oi_value    NUMERIC(20,2),
    source          VARCHAR(20) NOT NULL,
    PRIMARY KEY (contract, trading_date, investor_type)
);
```

---

## 6. NEWS

### 6.1 `news_sources`

```sql
CREATE TABLE news_sources (
    id           SERIAL PRIMARY KEY,
    code         VARCHAR(30) UNIQUE NOT NULL,
    name         VARCHAR(100) NOT NULL,
    base_url     VARCHAR(255),
    feed_url     VARCHAR(255),
    source_type  VARCHAR(20) NOT NULL,   -- OFFICIAL / IR / GOV / MEDIA / AGGREGATOR
    credibility  NUMERIC(4,3) NOT NULL,  -- 見 DATA_SOURCES §8.3
    robots_ok    BOOLEAN NOT NULL DEFAULT false,
    license_note TEXT,
    is_active    BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 6.2 `news`

```sql
CREATE TABLE news (
    id            BIGSERIAL PRIMARY KEY,
    source_id     INT NOT NULL REFERENCES news_sources(id),
    external_id   VARCHAR(255),
    url           VARCHAR(1000) NOT NULL,
    url_canonical VARCHAR(1000) NOT NULL,
    url_hash      CHAR(64) NOT NULL,          -- sha256(url_canonical)
    title         VARCHAR(500) NOT NULL,
    summary       TEXT,                        -- ★ 只存摘要，不長期保存全文
    lang          CHAR(2) NOT NULL DEFAULT 'zh',
    published_at  TIMESTAMPTZ NOT NULL,        -- ★ 知曉時間
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    simhash       BIGINT,                      -- 近似去重
    cluster_id    BIGINT,                      -- 同一事件的多篇報導
    is_duplicate  BOOLEAN NOT NULL DEFAULT false,
    sentiment       NUMERIC(6,4),              -- [-1, 1]
    sentiment_conf  NUMERIC(5,4),
    importance      NUMERIC(5,4),              -- 綜合重要性 [0,1]
    processing_status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                        -- PENDING / NORMALIZED / LINKED / SCORED / FAILED
    model_version   VARCHAR(50),
    CONSTRAINT uq_news_url UNIQUE (url_hash)
);
CREATE INDEX ix_news_pub     ON news(published_at DESC);
CREATE INDEX ix_news_cluster ON news(cluster_id);
CREATE INDEX ix_news_status  ON news(processing_status) WHERE processing_status <> 'SCORED';
```

### 6.3 `news_entities`

```sql
CREATE TABLE news_entities (
    id           BIGSERIAL PRIMARY KEY,
    news_id      BIGINT NOT NULL REFERENCES news(id) ON DELETE CASCADE,
    entity_type  VARCHAR(20) NOT NULL,  -- STOCK/PERSON/PRODUCT/TECH/COUNTRY/ORG/POLICY/EVENT
    entity_text  VARCHAR(150) NOT NULL, -- 原文中出現的字串
    normalized   VARCHAR(150),          -- 正規化名稱
    stock_id     BIGINT REFERENCES stocks(id),
    alias_id     BIGINT REFERENCES entity_aliases(id),
    extraction_method VARCHAR(20) NOT NULL,  -- DICTIONARY / LLM / HYBRID
    confidence   NUMERIC(5,4) NOT NULL,
    char_start   INT, char_end INT,     -- 原文位置 → evidence 定位
    model_version VARCHAR(50),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_ne_news  ON news_entities(news_id);
CREATE INDEX ix_ne_stock ON news_entities(stock_id);
```

### 6.4 `news_stock_relations`（★ 核心表）

```sql
CREATE TABLE news_stock_relations (
    id             BIGSERIAL PRIMARY KEY,
    news_id        BIGINT NOT NULL REFERENCES news(id) ON DELETE CASCADE,
    stock_id       BIGINT NOT NULL REFERENCES stocks(id),
    relation_type  VARCHAR(20) NOT NULL,
        -- DIRECT / SUPPLIER / CUSTOMER / COMPETITOR / INDUSTRY / THEMATIC / MACRO
    hop_count      SMALLINT NOT NULL DEFAULT 0,   -- 供應鏈傳播跳數
    impact_score   NUMERIC(5,4) NOT NULL,         -- [0,1]
    impact_direction SMALLINT NOT NULL,           -- +1 / 0 / -1
    confidence     NUMERIC(5,4) NOT NULL,
    sentiment      NUMERIC(6,4),
    time_horizon   VARCHAR(10) NOT NULL,          -- INTRADAY / SHORT / MEDIUM / LONG
    evidence       JSONB NOT NULL,
        -- {"snippets":[{"text":"...","char_start":..,"char_end":..}],
        --  "path":["NVDA","GPU","Foundry","2330"],
        --  "rule":"supply_chain_hop", "components":{...}}
    model_version  VARCHAR(50) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_nsr UNIQUE (news_id, stock_id, relation_type)
);
CREATE INDEX ix_nsr_stock ON news_stock_relations(stock_id, created_at DESC);
CREATE INDEX ix_nsr_impact ON news_stock_relations(impact_score DESC);
```

### 6.5 `news_events`

```sql
CREATE TABLE news_events (
    id           BIGSERIAL PRIMARY KEY,
    news_id      BIGINT NOT NULL REFERENCES news(id) ON DELETE CASCADE,
    event_type   VARCHAR(40) NOT NULL,
        -- EARNINGS / GUIDANCE / ORDER_WIN / CAPEX / M&A / DIVESTITURE /
        -- PRODUCT_LAUNCH / SUPPLY_SHOCK / PRICE_CHANGE / RATING_CHANGE /
        -- REGULATION / LITIGATION / MANAGEMENT_CHANGE / MACRO / GEOPOLITICS
    event_subtype VARCHAR(40),
    severity     NUMERIC(5,4),
    confidence   NUMERIC(5,4) NOT NULL,
    event_time   TIMESTAMPTZ,        -- 事件本身發生的時間（可能早於 published_at）
    model_version VARCHAR(50) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 6.6 `news_embeddings`

```sql
CREATE TABLE news_embeddings (
    news_id        BIGINT PRIMARY KEY REFERENCES news(id) ON DELETE CASCADE,
    embedding      vector(1024) NOT NULL,       -- BGE-M3 為 1024 維，換模型需 migration
    embedding_model VARCHAR(50) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_news_emb ON news_embeddings
    USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
```

### 6.7 `news_reactions`（影響力校準的閉環）

```sql
CREATE TABLE news_reactions (
    news_id       BIGINT NOT NULL REFERENCES news(id) ON DELETE CASCADE,
    stock_id      BIGINT NOT NULL REFERENCES stocks(id),
    horizon_days  SMALLINT NOT NULL,       -- 1/3/5/10/20
    raw_return    NUMERIC(12,6),
    abnormal_return NUMERIC(12,6),         -- 扣除市場/產業基準
    car           NUMERIC(12,6),           -- 累積異常報酬
    volume_ratio  NUMERIC(12,6),
    volatility_ratio NUMERIC(12,6),
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (news_id, stock_id, horizon_days)
);
```

---

## 7. DERIVED（分析結果）

### 7.1 `technical_indicators`

```sql
CREATE TABLE technical_indicators (
    stock_id      BIGINT NOT NULL REFERENCES stocks(id),
    trading_date  DATE NOT NULL,
    indicators    JSONB NOT NULL,   -- {"rsi_14":62.3,"macd":1.2,"macd_signal":0.9,...}
    feature_version VARCHAR(30) NOT NULL,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trading_date, feature_version)
);
SELECT create_hypertable('technical_indicators','trading_date', chunk_time_interval=>INTERVAL '1 month');
CREATE INDEX ix_ti_gin ON technical_indicators USING gin (indicators jsonb_path_ops);
```

> **為什麼用 JSONB 而不是 80 個欄位**：指標會持續增加，每加一個就 migration 一次不可行。JSONB + GIN 索引在讀取效能上足夠（且大部分查詢是「取某股某日全部指標」）。需要高頻篩選的少數指標（如 rsi_14）另建 generated column + B-tree 索引。

### 7.2 `factor_scores`

```sql
CREATE TABLE factor_returns (      -- Fama-MacBeth 逐日橫斷面迴歸的因子報酬
    trading_date  DATE NOT NULL,
    factor_name   VARCHAR(40) NOT NULL,
    factor_return NUMERIC(14,8) NOT NULL,   -- 該日因子報酬 β_{k,t}
    t_stat        NUMERIC(12,6),
    n_stocks      INT NOT NULL,
    r_squared     NUMERIC(10,6),
    feature_version VARCHAR(30) NOT NULL,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trading_date, factor_name, feature_version)
);
SELECT create_hypertable('factor_returns','trading_date', chunk_time_interval=>INTERVAL '1 year');

CREATE TABLE factor_scores (
    stock_id      BIGINT NOT NULL REFERENCES stocks(id),
    trading_date  DATE NOT NULL,
    factor_name   VARCHAR(40) NOT NULL,   -- value/growth/momentum/quality/volatility/
                                          -- liquidity/size/sentiment/news/ai_exposure
    raw_value     NUMERIC(18,6),
    zscore        NUMERIC(12,6),          -- 全市場標準化
    percentile    NUMERIC(6,4),           -- [0,1]
    sector_zscore NUMERIC(12,6),          -- 產業內標準化
    feature_version VARCHAR(30) NOT NULL,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trading_date, factor_name, feature_version)
);
SELECT create_hypertable('factor_scores','trading_date', chunk_time_interval=>INTERVAL '1 month');
```

### 7.3 `ai_scores` + `ai_score_contributions`（★ 可解釋性的載體）

```sql
CREATE TABLE ai_scores (
    stock_id        BIGINT NOT NULL REFERENCES stocks(id),
    trading_date    DATE NOT NULL,
    total_score     NUMERIC(6,3) NOT NULL,   -- 0–100
    technical_score     NUMERIC(6,3),
    fundamental_score   NUMERIC(6,3),
    institutional_score NUMERIC(6,3),
    momentum_score      NUMERIC(6,3),
    news_score          NUMERIC(6,3),
    sentiment_score     NUMERIC(6,3),
    industry_score      NUMERIC(6,3),
    ai_trend_score      NUMERIC(6,3),
    valuation_score     NUMERIC(6,3),
    risk_score          NUMERIC(6,3),
    anomaly_score       NUMERIC(6,3),
    rank_overall    INT,
    rank_in_sector  INT,
    percentile      NUMERIC(6,4),
    score_delta_5d  NUMERIC(6,3),           -- 支撐「AI Score 上升最快」查詢
    score_delta_20d NUMERIC(6,3),
    confidence      NUMERIC(5,4) NOT NULL,
    data_quality    NUMERIC(6,3),           -- 引用當日 DataQualityScore
    weights_id      BIGINT NOT NULL REFERENCES scoring_weights(id),
    model_version   VARCHAR(50) NOT NULL,
    feature_version VARCHAR(30) NOT NULL,
    dataset_version VARCHAR(30) NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trading_date, model_version)
);
SELECT create_hypertable('ai_scores','trading_date', chunk_time_interval=>INTERVAL '1 month');
CREATE INDEX ix_ais_rank ON ai_scores(trading_date, total_score DESC);

CREATE TABLE ai_score_contributions (
    id            BIGSERIAL PRIMARY KEY,
    stock_id      BIGINT NOT NULL,
    trading_date  DATE NOT NULL,
    model_version VARCHAR(50) NOT NULL,
    component     VARCHAR(50) NOT NULL,    -- 'technical.momentum_20d'
    label_zh      VARCHAR(100) NOT NULL,   -- '技術動能'
    contribution  NUMERIC(8,4) NOT NULL,   -- ★ 帶正負號，加總 = total_score
    raw_value     NUMERIC(18,6),
    percentile    NUMERIC(6,4),
    weight        NUMERIC(8,6) NOT NULL,
    evidence      JSONB,                   -- {"rsi_14":72.1,"vs_sector_avg":+8.2,
                                           --  "source":"TWSE","as_of":"2026-08-15"}
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_asc_lookup ON ai_score_contributions(stock_id, trading_date, model_version);
```

> `SUM(contribution) = total_score` 是一條**資料庫層的不變式**，由 integration test 斷言。這保證「Why 92?」的回答永遠加得起來。

### 7.4 `scoring_weights`（權重必須可設定、有版本）

```sql
CREATE TABLE scoring_weights (
    id           BIGSERIAL PRIMARY KEY,
    name         VARCHAR(50) NOT NULL,     -- 'default' / 'growth_tilt' / 'user:123'
    version      SMALLINT NOT NULL,
    weights      JSONB NOT NULL,
        -- {"technical":0.20,"fundamental":0.20,"institutional":0.15,
        --  "news":0.15,"industry":0.10,"momentum":0.10,"risk":-0.10}
    is_active    BOOLEAN NOT NULL DEFAULT false,
    created_by   BIGINT REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_sw UNIQUE (name, version)
);
```

> **禁止把權重寫死在前端或程式碼。** 前端從 `/api/v1/ai-score/weights` 讀取，管理員可在 Admin Console 調整並產生新版本；歷史分數永遠指向當時的 `weights_id`，可重現。

### 7.5 `ml_features` / `ml_predictions`

```sql
CREATE TABLE ml_features (
    stock_id        BIGINT NOT NULL REFERENCES stocks(id),
    as_of_date      DATE NOT NULL,          -- ★ 特徵的知曉截止日
    feature_set     VARCHAR(50) NOT NULL,   -- 'core_v1'
    feature_version VARCHAR(30) NOT NULL,
    features        JSONB NOT NULL,
    label_5d_ret    NUMERIC(12,6),          -- 訓練用標籤（未來資訊，僅訓練時填）
    label_10d_ret   NUMERIC(12,6),
    label_20d_excess NUMERIC(12,6),
    labels_filled_at TIMESTAMPTZ,           -- ★ 標籤何時被回填 → 防止推論時誤用
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, as_of_date, feature_set, feature_version)
);
SELECT create_hypertable('ml_features','as_of_date', chunk_time_interval=>INTERVAL '1 month');

CREATE TABLE ml_predictions (
    stock_id      BIGINT NOT NULL REFERENCES stocks(id),
    trading_date  DATE NOT NULL,
    model_id      BIGINT NOT NULL REFERENCES model_versions(id),
    target        VARCHAR(50) NOT NULL,     -- 'P(5D_return>3%)'
    probability   NUMERIC(8,6),
    expected_value NUMERIC(12,6),
    confidence    NUMERIC(5,4),
    prediction_interval JSONB,              -- {"lower":..,"upper":..,"level":0.8}
    top_features  JSONB,                    -- SHAP top-k → 可解釋性
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_id, trading_date, model_id, target)
);
SELECT create_hypertable('ml_predictions','trading_date', chunk_time_interval=>INTERVAL '1 month');
```

> **`labels_filled_at` 的用意**：推論路徑的程式碼在讀 `ml_features` 時，SELECT 中**不得包含任何 `label_*` 欄位**。這由 repository 層的兩個獨立方法（`get_for_training()` / `get_for_inference()`）強制，且有測試斷言。

### 7.6 `anomalies`

```sql
CREATE TABLE anomalies (
    id            BIGSERIAL PRIMARY KEY,
    stock_id      BIGINT NOT NULL REFERENCES stocks(id),
    trading_date  DATE NOT NULL,
    anomaly_type  VARCHAR(40) NOT NULL,
        -- VOLUME_SPIKE / PRICE_SPIKE / VOLATILITY_SPIKE / NEWS_SPIKE /
        -- SENTIMENT_SPIKE / CORRELATION_BREAK / SECTOR_DIVERGENCE / FLOW_ANOMALY
    score         NUMERIC(6,3) NOT NULL,    -- 0–100
    zscore        NUMERIC(12,6),
    baseline      JSONB,                    -- {"mean":..,"std":..,"window":60}
    observed      JSONB,                    -- {"volume":..,"pct_change":..}
    detector      VARCHAR(50) NOT NULL,     -- 'zscore_v1' / 'isolation_forest_v1'
    model_version VARCHAR(50),
    explanation   JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_anom UNIQUE (stock_id, trading_date, anomaly_type, detector)
);
SELECT create_hypertable('anomalies','trading_date', chunk_time_interval=>INTERVAL '1 month');
```

### 7.7 `market_regimes` / `sector_metrics`

```sql
CREATE TABLE market_regimes (
    market_id     SMALLINT NOT NULL REFERENCES markets(id),
    trading_date  DATE NOT NULL,
    regime        VARCHAR(20) NOT NULL,   -- BULL/BEAR/SIDEWAYS/HIGH_VOL/RISK_ON/RISK_OFF
    regime_score  NUMERIC(6,3),
    probabilities JSONB NOT NULL,         -- 各 regime 的機率
    inputs        JSONB NOT NULL,         -- {"volatility":..,"breadth":..,
                                          --  "index_momentum":..,"foreign_flow":..}
    model_version VARCHAR(50) NOT NULL,
    PRIMARY KEY (market_id, trading_date, model_version)
);

CREATE TABLE sector_metrics (
    sector_id     INT NOT NULL REFERENCES sectors(id),
    trading_date  DATE NOT NULL,
    momentum_score      NUMERIC(6,3),
    breadth_score       NUMERIC(6,3),   -- 成分股中上漲/站上均線比例
    volume_score        NUMERIC(6,3),
    institutional_score NUMERIC(6,3),
    news_momentum_score NUMERIC(6,3),
    ai_exposure_score   NUMERIC(6,3),
    strength_score      NUMERIC(6,3) NOT NULL,   -- 合成
    rank                INT,
    rank_change_5d      INT,
    state               VARCHAR(20),    -- TOP / EMERGING / FALLING / WEAK
    feature_version VARCHAR(30) NOT NULL,
    PRIMARY KEY (sector_id, trading_date, feature_version)
);
```

---

## 8. GRAPH（AI 供應鏈）

```sql
CREATE TABLE supply_chain_nodes (
    id          BIGSERIAL PRIMARY KEY,
    node_type   VARCHAR(20) NOT NULL,   -- THEME / SEGMENT / COMPANY
    code        VARCHAR(50) UNIQUE NOT NULL,   -- 'AI' / 'COWOS' / 'HBM' / 'STOCK:2330'
    name_zh     VARCHAR(100) NOT NULL,
    name_en     VARCHAR(100),
    stock_id    BIGINT REFERENCES stocks(id),  -- node_type=COMPANY 時填
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE supply_chain_edges (
    id            BIGSERIAL PRIMARY KEY,
    from_node_id  BIGINT NOT NULL REFERENCES supply_chain_nodes(id),
    to_node_id    BIGINT NOT NULL REFERENCES supply_chain_nodes(id),
    edge_type     VARCHAR(20) NOT NULL,   -- SUPPLIER/CUSTOMER/COMPETITOR/PARTNER/
                                          -- BELONGS_TO/THEME_OF
    strength      NUMERIC(5,4) NOT NULL,  -- [0,1]
    revenue_share NUMERIC(5,4),           -- 該關係佔營收比重（若已知）
    evidence      JSONB NOT NULL,
        -- {"type":"annual_report","url":"...","quote":"...","as_of":"2025-12-31"}
    confidence    NUMERIC(5,4) NOT NULL,
    valid_from    DATE NOT NULL,          -- ★ 關係也有時效
    valid_to      DATE,
    source        VARCHAR(30) NOT NULL,   -- MANUAL / ANNUAL_REPORT / NEWS_INFERRED
    verified_by   BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_edge UNIQUE (from_node_id, to_node_id, edge_type, valid_from)
);
CREATE INDEX ix_edge_from ON supply_chain_edges(from_node_id);
CREATE INDEX ix_edge_to   ON supply_chain_edges(to_node_id);
```

> **`evidence` 為 NOT NULL 是刻意的。** 供應鏈關係若沒有來源（年報、法說會、新聞、官方公告），就不能進圖。這防止「AI 幻想的供應鏈」污染整個新聞傳播邏輯。
> **`valid_from`/`valid_to`** 讓回測能取得「當時的供應鏈關係」，而不是用今天的關係去解讀三年前的新聞。

多跳查詢用 recursive CTE：

```sql
WITH RECURSIVE propagation AS (
    SELECT to_node_id, strength, 1 AS hop, ARRAY[from_node_id, to_node_id] AS path
    FROM supply_chain_edges
    WHERE from_node_id = :seed AND :as_of BETWEEN valid_from AND COALESCE(valid_to,'9999-12-31')
  UNION ALL
    SELECT e.to_node_id, p.strength * e.strength * 0.7, p.hop + 1, p.path || e.to_node_id
    FROM propagation p JOIN supply_chain_edges e ON e.from_node_id = p.to_node_id
    WHERE p.hop < 3 AND NOT e.to_node_id = ANY(p.path)
      AND p.strength * e.strength > 0.15
)
SELECT * FROM propagation;
```
（`0.7` 為每跳衰減係數，存於設定而非寫死。）

---

## 9. RESEARCH

### 9.1 `events` / `event_studies`

```sql
CREATE TABLE events (
    id           BIGSERIAL PRIMARY KEY,
    event_type   VARCHAR(40) NOT NULL,
    scope        VARCHAR(20) NOT NULL,   -- STOCK / SECTOR / MARKET / GLOBAL
    stock_id     BIGINT REFERENCES stocks(id),
    sector_id    INT REFERENCES sectors(id),
    external_symbol VARCHAR(20),         -- 'NVDA' 等外部標的
    title        VARCHAR(300) NOT NULL,
    event_date   DATE NOT NULL,          -- 事件時間
    announced_at TIMESTAMPTZ NOT NULL,   -- ★ 知曉時間
    magnitude    NUMERIC(12,6),          -- 例如 NVDA 當日漲幅
    metadata     JSONB,
    news_id      BIGINT REFERENCES news(id),
    source       VARCHAR(30) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE event_studies (
    id            BIGSERIAL PRIMARY KEY,
    name          VARCHAR(150) NOT NULL,
    event_filter  JSONB NOT NULL,      -- 定義納入哪些事件
    universe      JSONB NOT NULL,      -- 定義觀察哪些股票
    benchmark     VARCHAR(30) NOT NULL DEFAULT 'TAIEX',
    estimation_window  INT NOT NULL DEFAULT 120,   -- 估計期交易日
    event_window_pre   INT NOT NULL DEFAULT 5,
    event_window_post  INT NOT NULL DEFAULT 20,
    model         VARCHAR(20) NOT NULL DEFAULT 'MARKET_MODEL', -- MARKET_ADJUSTED / MARKET_MODEL / FF3
    status        VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    results       JSONB,
        -- {"n_events":42,"by_horizon":{"1":{"aar":0.014,"caar":0.014,
        --   "t_stat":2.31,"p_value":0.026,"positive_ratio":0.64}, ...}}
    dataset_version VARCHAR(30) NOT NULL,
    created_by    BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ
);
```

### 9.2 `backtests` / `backtest_trades` / `backtest_metrics`

```sql
CREATE TABLE backtests (
    id              BIGSERIAL PRIMARY KEY,
    name            VARCHAR(150) NOT NULL,
    user_id         BIGINT REFERENCES users(id),
    strategy_config JSONB NOT NULL,
        -- {"universe":{...},"entry":{...},"exit":{...},"sizing":{...},
        --  "rebalance":"weekly","max_positions":20}
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    initial_capital NUMERIC(20,2) NOT NULL,
    costs           JSONB NOT NULL,
        -- {"commission_bps":14.25,"tax_bps":30,"slippage_bps":10,
        --  "min_commission":20}   ← 台股：手續費 0.1425%、賣出交易稅 0.3%
    status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    error_message   TEXT,
    dataset_version VARCHAR(30) NOT NULL,
    feature_version VARCHAR(30),
    model_version   VARCHAR(50),
    code_version    VARCHAR(40) NOT NULL,   -- git sha → 完全可重現
    random_seed     INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE TABLE backtest_trades (
    id            BIGSERIAL PRIMARY KEY,
    backtest_id   BIGINT NOT NULL REFERENCES backtests(id) ON DELETE CASCADE,
    stock_id      BIGINT NOT NULL REFERENCES stocks(id),
    side          VARCHAR(4) NOT NULL,     -- BUY / SELL
    trade_date    DATE NOT NULL,
    price         NUMERIC(18,4) NOT NULL,
    shares        BIGINT NOT NULL,
    commission    NUMERIC(18,4) NOT NULL,
    tax           NUMERIC(18,4) NOT NULL,
    slippage      NUMERIC(18,4) NOT NULL,
    realized_pnl  NUMERIC(20,2),
    entry_reason  JSONB,                   -- 為什麼買 → 可解釋性
    exit_reason   JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_bt_trades ON backtest_trades(backtest_id, trade_date);

CREATE TABLE backtest_metrics (
    backtest_id   BIGINT PRIMARY KEY REFERENCES backtests(id) ON DELETE CASCADE,
    total_return  NUMERIC(12,6), cagr NUMERIC(12,6), annual_return NUMERIC(12,6),
    volatility    NUMERIC(12,6),
    sharpe        NUMERIC(12,6), sortino NUMERIC(12,6), calmar NUMERIC(12,6),
    max_drawdown  NUMERIC(12,6), max_drawdown_days INT,
    win_rate      NUMERIC(12,6), profit_factor NUMERIC(12,6),
    avg_win       NUMERIC(20,2), avg_loss NUMERIC(20,2),
    trade_count   INT, turnover NUMERIC(12,6),
    total_commission NUMERIC(20,2), total_tax NUMERIC(20,2), total_slippage NUMERIC(20,2),
    benchmark_return NUMERIC(12,6), alpha NUMERIC(12,6), beta NUMERIC(12,6),
    information_ratio NUMERIC(12,6),
    equity_curve_uri VARCHAR(500),          -- parquet 於 object storage
    monthly_returns  JSONB,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 9.3 `documents` / `document_chunks`（RAG 知識庫）

```sql
CREATE TABLE documents (
    id           BIGSERIAL PRIMARY KEY,
    doc_type     VARCHAR(30) NOT NULL,  -- ANNUAL_REPORT / QUARTERLY / MOPS_ANNOUNCEMENT /
                                        -- EARNINGS_CALL / INDUSTRY_REPORT / COMPANY_PROFILE
    stock_id     BIGINT REFERENCES stocks(id),
    title        VARCHAR(300) NOT NULL,
    url          VARCHAR(1000),
    period_end   DATE,
    published_at TIMESTAMPTZ NOT NULL,   -- ★ 知曉時間（RAG 也要防 look-ahead）
    lang         CHAR(2) NOT NULL DEFAULT 'zh',
    storage_uri  VARCHAR(500),           -- 原檔於 object storage
    checksum     CHAR(64) NOT NULL,
    source       VARCHAR(30) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_doc UNIQUE (checksum)
);

CREATE TABLE document_chunks (
    id            BIGSERIAL PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index   INT NOT NULL,
    content       TEXT NOT NULL,
    token_count   INT,
    section       VARCHAR(200),          -- 章節標題 → 引用時顯示
    page_number   INT,
    embedding     vector(1024),
    embedding_model VARCHAR(50),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_chunk UNIQUE (document_id, chunk_index)
);
CREATE INDEX ix_chunk_emb ON document_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
CREATE INDEX ix_chunk_fts ON document_chunks
    USING gin (to_tsvector('simple', content));   -- 中文用 pg_bigm 或 zhparser 更佳
```

> **Hybrid retrieval**：向量檢索 + BM25/全文檢索，用 Reciprocal Rank Fusion 合併。純向量檢索在「找 2330 的 CoWoS 產能」這類含專有名詞的查詢上表現差。

---

## 10. USER

```sql
CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    email         VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,       -- argon2id
    display_name  VARCHAR(100),
    role          VARCHAR(20) NOT NULL DEFAULT 'viewer',  -- admin/analyst/viewer
    is_active     BOOLEAN NOT NULL DEFAULT true,
    settings      JSONB NOT NULL DEFAULT '{}',
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE watchlists (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       VARCHAR(100) NOT NULL,
    stock_ids  BIGINT[] NOT NULL DEFAULT '{}',
    sort_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE portfolios (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            VARCHAR(100) NOT NULL,
    base_currency   CHAR(3) NOT NULL DEFAULT 'TWD',
    initial_capital NUMERIC(20,2),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE transactions (
    id            BIGSERIAL PRIMARY KEY,
    portfolio_id  BIGINT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    stock_id      BIGINT NOT NULL REFERENCES stocks(id),
    side          VARCHAR(4) NOT NULL,
    trade_date    DATE NOT NULL,
    price         NUMERIC(18,4) NOT NULL,
    shares        BIGINT NOT NULL,
    commission    NUMERIC(18,4) NOT NULL DEFAULT 0,
    tax           NUMERIC(18,4) NOT NULL DEFAULT 0,
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE positions (          -- 由 transactions 推導的物化持倉
    portfolio_id  BIGINT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    stock_id      BIGINT NOT NULL REFERENCES stocks(id),
    shares        BIGINT NOT NULL,
    avg_cost      NUMERIC(18,4) NOT NULL,
    total_cost    NUMERIC(20,2) NOT NULL,
    realized_pnl  NUMERIC(20,2) NOT NULL DEFAULT 0,
    first_buy_date DATE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (portfolio_id, stock_id)
);

CREATE TABLE alerts (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          VARCHAR(150) NOT NULL,
    alert_type    VARCHAR(30) NOT NULL,
        -- PRICE / VOLUME / AI_SCORE / NEWS / INSTITUTIONAL / TECHNICAL /
        -- ANOMALY / SECTOR / PORTFOLIO
    scope         JSONB NOT NULL,     -- {"stock_ids":[...]} 或 {"universe":"all"}
    conditions    JSONB NOT NULL,
        -- {"op":"AND","children":[
        --   {"metric":"ai_score.total","op":">","value":90},
        --   {"metric":"volume_ratio_20d","op":">","value":2},
        --   {"metric":"institutional.foreign_net","op":">","value":1000000}]}
    priority      VARCHAR(10) NOT NULL DEFAULT 'NORMAL',  -- LOW/NORMAL/HIGH
    channels      TEXT[] NOT NULL DEFAULT '{IN_APP}',     -- IN_APP/EMAIL/WEBHOOK
    cooldown_minutes INT NOT NULL DEFAULT 1440,
    is_active     BOOLEAN NOT NULL DEFAULT true,
    last_fired_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE alert_events (
    id            BIGSERIAL PRIMARY KEY,
    alert_id      BIGINT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    stock_id      BIGINT REFERENCES stocks(id),
    triggered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    trading_date  DATE NOT NULL,
    snapshot      JSONB NOT NULL,     -- 觸發當下所有相關指標值 → 事後可稽核
    matched_conditions JSONB NOT NULL,
    delivered     BOOLEAN NOT NULL DEFAULT false,
    delivered_at  TIMESTAMPTZ,
    read_at       TIMESTAMPTZ
);
CREATE INDEX ix_ae_user_time ON alert_events(triggered_at DESC);
```

---

## 11. PLATFORM

```sql
CREATE TABLE dataset_versions (
    id            BIGSERIAL PRIMARY KEY,
    version       VARCHAR(30) UNIQUE NOT NULL,  -- '2026-08-15' 或 semver
    description   TEXT,
    date_range    daterange NOT NULL,
    row_counts    JSONB NOT NULL,               -- {"daily_prices":12345678,...}
    quality_summary JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE feature_versions (
    id            BIGSERIAL PRIMARY KEY,
    version       VARCHAR(30) UNIQUE NOT NULL,
    feature_set   VARCHAR(50) NOT NULL,
    spec          JSONB NOT NULL,       -- 每個特徵的定義、視窗、來源欄位
    code_version  VARCHAR(40) NOT NULL, -- git sha
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE model_versions (
    id              BIGSERIAL PRIMARY KEY,
    model_name      VARCHAR(50) NOT NULL,      -- 'stockrank-lgbm'
    version         VARCHAR(30) NOT NULL,      -- 'v1.4'
    task            VARCHAR(30) NOT NULL,      -- CLASSIFICATION/REGRESSION/RANKING
    target          VARCHAR(50) NOT NULL,      -- 'P(5D_return>3%)'
    algorithm       VARCHAR(30) NOT NULL,      -- LightGBM
    hyperparameters JSONB NOT NULL,
    feature_version VARCHAR(30) NOT NULL REFERENCES feature_versions(version),
    dataset_version VARCHAR(30) NOT NULL REFERENCES dataset_versions(version),
    train_range     daterange NOT NULL,
    valid_range     daterange NOT NULL,
    test_range      daterange NOT NULL,
    embargo_days    SMALLINT NOT NULL DEFAULT 5,   -- ★ 防洩漏的隔離期
    metrics         JSONB NOT NULL,
        -- {"auc":0.62,"precision@50":0.31,"brier":0.19,"log_loss":0.65,
        --  "ic":0.043,"rank_ic":0.051}
    calibration     JSONB,
    feature_importance JSONB,
    artifact_uri    VARCHAR(500) NOT NULL,
    random_seed     INT NOT NULL,
    code_version    VARCHAR(40) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'TRAINED',  -- TRAINED/SHADOW/ACTIVE/RETIRED
    trained_at      TIMESTAMPTZ NOT NULL,
    activated_at    TIMESTAMPTZ,
    retired_at      TIMESTAMPTZ,
    CONSTRAINT uq_mv UNIQUE (model_name, version)
);

CREATE TABLE model_monitoring (
    id            BIGSERIAL PRIMARY KEY,
    model_id      BIGINT NOT NULL REFERENCES model_versions(id),
    trading_date  DATE NOT NULL,
    prediction_count INT,
    prediction_mean  NUMERIC(12,6),
    prediction_std   NUMERIC(12,6),
    prediction_psi   NUMERIC(12,6),   -- vs 訓練期分布
    feature_psi      JSONB,           -- 各特徵的 PSI
    realized_auc     NUMERIC(12,6),   -- 標籤成熟後回填
    realized_brier   NUMERIC(12,6),
    calibration_error NUMERIC(12,6),
    alert_level      VARCHAR(10),     -- OK / WARN / CRITICAL
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_mm UNIQUE (model_id, trading_date)
);

CREATE TABLE raw_ingestions (
    id            BIGSERIAL PRIMARY KEY,
    provider      VARCHAR(30) NOT NULL,
    endpoint      VARCHAR(200) NOT NULL,
    params        JSONB NOT NULL,
    request_at    TIMESTAMPTZ NOT NULL,
    http_status   SMALLINT,
    response_hash CHAR(64),
    storage_uri   VARCHAR(500),      -- 原始 payload 位置
    record_count  INT,
    duration_ms   INT,
    error         TEXT,
    job_run_id    BIGINT,
    CONSTRAINT uq_raw UNIQUE (provider, endpoint, response_hash)
);

CREATE TABLE job_runs (
    id            BIGSERIAL PRIMARY KEY,
    job_name      VARCHAR(80) NOT NULL,
    params        JSONB,
    status        VARCHAR(20) NOT NULL,  -- RUNNING/SUCCESS/FAILED/SKIPPED/TIMEOUT
    skip_reason   VARCHAR(50),           -- 'non_trading_day' / 'already_done'
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    duration_ms   INT,
    records_in    INT, records_out INT, records_rejected INT,
    error         TEXT,
    retry_count   SMALLINT NOT NULL DEFAULT 0,
    worker        VARCHAR(80)
);
CREATE INDEX ix_jr_recent ON job_runs(job_name, started_at DESC);

CREATE TABLE data_quality_scores (
    id            BIGSERIAL PRIMARY KEY,
    dataset       VARCHAR(50) NOT NULL,
    stock_id      BIGINT REFERENCES stocks(id),   -- NULL = 整個 dataset 的彙總
    trading_date  DATE NOT NULL,
    freshness     NUMERIC(6,3), completeness NUMERIC(6,3),
    consistency   NUMERIC(6,3), source_quality NUMERIC(6,3),
    overall       NUMERIC(6,3) NOT NULL,
    violations    JSONB,          -- {"P03":2,"T02":1}
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- PostgreSQL 的 PRIMARY KEY 不接受運算式，且 NULL 不參與唯一性比較，
-- 因此用兩個 partial unique index 取代複合主鍵：
CREATE UNIQUE INDEX uq_dqs_stock ON data_quality_scores(dataset, stock_id, trading_date)
    WHERE stock_id IS NOT NULL;
CREATE UNIQUE INDEX uq_dqs_agg   ON data_quality_scores(dataset, trading_date)
    WHERE stock_id IS NULL;

CREATE TABLE data_freshness (           -- 每個資料集的新鮮度契約與現況
    dataset               VARCHAR(50) PRIMARY KEY,
    expected_lag_minutes  INT NOT NULL,       -- 相對收盤/預期公布時間
    last_ingested_at      TIMESTAMPTZ,
    last_trading_date     DATE,
    is_stale              BOOLEAN NOT NULL DEFAULT false,
    checked_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE backfill_progress (        -- 歷史回補的可中斷可續跑游標
    id            BIGSERIAL PRIMARY KEY,
    dataset       VARCHAR(50) NOT NULL,
    scope         VARCHAR(50) NOT NULL DEFAULT 'ALL',  -- 'ALL' 或個股 symbol
    range_start   DATE NOT NULL,
    range_end     DATE NOT NULL,
    cursor_date   DATE NOT NULL,        -- 下一個要處理的交易日
    status        VARCHAR(20) NOT NULL DEFAULT 'RUNNING', -- RUNNING/PAUSED/DONE/FAILED
    processed     INT NOT NULL DEFAULT 0,
    failed        INT NOT NULL DEFAULT 0,
    last_error    TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_backfill UNIQUE (dataset, scope, range_start, range_end)
);

CREATE TABLE llm_failures (             -- LLM 結構化輸出失敗紀錄（走規則路徑的降級樣本）
    id            BIGSERIAL PRIMARY KEY,
    task          VARCHAR(50) NOT NULL,   -- ner / sentiment / event / narrative
    ref_type      VARCHAR(30),            -- news / ai_score
    ref_id        BIGINT,
    model         VARCHAR(80) NOT NULL,
    attempt       SMALLINT NOT NULL,
    error_type    VARCHAR(50) NOT NULL,   -- SCHEMA_INVALID / TIMEOUT / REFUSAL / EMPTY
    raw_output    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE data_gaps (
    id           BIGSERIAL PRIMARY KEY,
    dataset      VARCHAR(50) NOT NULL,
    stock_id     BIGINT REFERENCES stocks(id),
    gap_start    DATE NOT NULL,
    gap_end      DATE NOT NULL,
    reason       VARCHAR(100),    -- 'suspended' / 'not_listed' / 'source_missing'
    status       VARCHAR(20) NOT NULL DEFAULT 'OPEN',  -- OPEN/FILLED/ACCEPTED
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ
);

CREATE TABLE quarantine_records (
    id           BIGSERIAL PRIMARY KEY,
    dataset      VARCHAR(50) NOT NULL,
    raw_payload  JSONB NOT NULL,
    violations   JSONB NOT NULL,
    job_run_id   BIGINT REFERENCES job_runs(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at  TIMESTAMPTZ,
    resolution   VARCHAR(30)     -- ACCEPTED / DISCARDED / SOURCE_FIXED
);

CREATE TABLE audit_logs (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT REFERENCES users(id),
    action       VARCHAR(80) NOT NULL,
    resource     VARCHAR(80),
    resource_id  VARCHAR(80),
    request_id   VARCHAR(50),
    ip_address   INET,
    user_agent   VARCHAR(300),
    payload      JSONB,
    result       VARCHAR(20) NOT NULL,   -- SUCCESS / DENIED / ERROR
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_time ON audit_logs(created_at DESC);

CREATE TABLE system_health (
    component     VARCHAR(50) PRIMARY KEY,
    status        VARCHAR(20) NOT NULL,   -- HEALTHY/DEGRADED/DOWN/DISABLED
    last_success  TIMESTAMPTZ,
    last_check    TIMESTAMPTZ NOT NULL DEFAULT now(),
    latency_ms    INT,
    detail        JSONB
);
```

---

## 12. TimescaleDB 政策

```sql
-- 壓縮（節省 5–20 倍空間）
ALTER TABLE daily_prices SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'stock_id',
    timescaledb.compress_orderby   = 'trading_date DESC');
SELECT add_compression_policy('daily_prices', INTERVAL '1 year');

ALTER TABLE ml_features SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'stock_id');
SELECT add_compression_policy('ml_features', INTERVAL '3 months');

-- 連續聚合：週線/月線（避免每次查詢重算）
CREATE MATERIALIZED VIEW weekly_prices
WITH (timescaledb.continuous) AS
SELECT stock_id,
       time_bucket('1 week', trading_date) AS week_start,
       first(open, trading_date)  AS open,
       max(high)                  AS high,
       min(low)                   AS low,
       last(close, trading_date)  AS close,
       sum(volume)                AS volume,
       sum(turnover)              AS turnover
FROM daily_prices
GROUP BY stock_id, week_start;

SELECT add_continuous_aggregate_policy('weekly_prices',
    start_offset => INTERVAL '3 months',
    end_offset   => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day');

-- 保留政策：只對可重建的中間資料
SELECT add_retention_policy('data_quality_scores', INTERVAL '2 years');
-- ★ 絕不對 daily_prices / financials / news 設 retention
```

---

## 13. 兩個資料庫層級的不變式（integration test 斷言）

```sql
-- INV-1: AI Score 的貢獻項加總必須等於總分（誤差 < 0.01）
SELECT s.stock_id, s.trading_date, s.total_score, SUM(c.contribution) AS sum_contrib
FROM ai_scores s
JOIN ai_score_contributions c
  ON c.stock_id = s.stock_id AND c.trading_date = s.trading_date
 AND c.model_version = s.model_version
GROUP BY s.stock_id, s.trading_date, s.total_score, s.model_version
HAVING ABS(s.total_score - SUM(c.contribution)) > 0.01;
-- 必須回傳 0 列

-- INV-2: 任何 announced_at 早於 period_end 的基本面資料都是錯的
SELECT COUNT(*) FROM financials WHERE announced_at::date < period_end;
-- 必須為 0
```

---

## 14. 遷移順序（Alembic revision 規劃）

```
001_extensions          CREATE EXTENSION timescaledb, vector, pg_trgm
002_master              markets, sectors, industries, stocks, entity_aliases,
                        trading_calendar, corporate_actions
003_platform            dataset/feature/model_versions, job_runs, audit_logs,
                        system_health, raw_ingestions
004_market              daily_prices, index_prices, market_stats (+ hypertables)
005_fundamental         financials, financial_metrics, monthly_revenue, valuation_daily
006_flow                institutional_trading, margin_short, futures_institutional
007_quality             data_quality_scores, data_freshness, data_gaps,
                        quarantine_records, backfill_progress
008_derived_quant       technical_indicators, factor_scores, factor_returns
009_news                news_sources, news, news_entities, news_stock_relations,
                        news_events, news_embeddings, news_reactions, llm_failures
010_scoring             scoring_weights, ai_scores, ai_score_contributions
011_ml                  ml_features, ml_predictions, model_monitoring
012_analytics           anomalies, market_regimes, sector_metrics
013_graph               supply_chain_nodes, supply_chain_edges
014_research            events, event_studies, backtests, backtest_trades,
                        backtest_metrics, documents, document_chunks
015_user                users, watchlists, portfolios, transactions, positions,
                        alerts, alert_events
016_policies            compression, continuous aggregates, retention
```

每個 revision 必須有可執行的 `downgrade()`，且 CI 跑 `upgrade head → downgrade -1 → upgrade head`。
