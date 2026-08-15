# API_SPEC.md — REST API v1 規格

> 版本 0.1 · 2026-08-15
> FastAPI · OpenAPI 3.1 自動產生於 `/api/v1/openapi.json`，Swagger UI 於 `/docs`
> 前端型別由 `openapi-typescript` 產生 → 前後端契約不可能漂移

---

## 1. 通則

### 1.1 Base URL 與版本

```
http://localhost:8000/api/v1
```

破壞性變更 → 新版本前綴 `/api/v2`，舊版並存至少一個 Phase。

### 1.2 統一回應信封（★ 每個端點都必須遵守）

```jsonc
{
  "data": <T | T[]>,
  "meta": {
    "data_timestamp": "2026-08-15T05:30:00Z",   // 資料本身的時間
    "trading_date":   "2026-08-15",             // 對應交易日
    "source":         ["TWSE"],                 // 資料來源（授權標示要求）
    "model_version":  "stockrank-lgbm-v1.4",    // 若涉及模型推論
    "feature_version":"core-v1.2",
    "dataset_version":"2026-08-15",
    "confidence":     0.82,                     // 若涉及模型推論
    "is_demo":        false,                    // ★ MockProvider 產生時為 true
    "is_stale":       false,                    // 資料超過 expected_lag
    "quality":        { "overall": 98.4, "freshness": 98.0, "completeness": 99.5 },
    "cache":          { "hit": true, "age_seconds": 42 },
    "request_id":     "01J2XK9..."
  },
  "pagination": {                               // 僅列表端點
    "page": 1, "page_size": 50, "total": 1832, "total_pages": 37,
    "next_cursor": "eyJ..."                     // 大型列表用 cursor
  }
}
```

**為什麼 meta 是強制的**：這是「所有即時市場相關回答都必須附資料時間、來源、模型版本、信心度」原則的技術落實。前端的 `<DataProvenance>` 元件直接消費 `meta`，任何缺 `meta` 的端點在 integration test 中會失敗。

### 1.3 錯誤模型（RFC 9457 Problem Details）

```jsonc
{
  "type":   "https://twquant.local/errors/data-not-available",
  "title":  "Data not available",
  "status": 404,
  "detail": "No price data for 9999 on 2026-08-15",
  "instance": "/api/v1/stocks/9999/prices",
  "request_id": "01J2XK9...",
  "errors": [ { "field": "symbol", "message": "not found" } ]
}
```

| Status | 使用時機 |
|--------|---------|
| 400 | 參數格式錯誤 |
| 401 | 未認證 |
| 403 | 無權限（RBAC） |
| 404 | 資源不存在 |
| 409 | 衝突（重複建立） |
| 422 | 語意錯誤（如 start_date > end_date） |
| 429 | 超過 rate limit（附 `Retry-After`） |
| 451 | 資料因授權限制不可提供 |
| 503 | 上游 provider 不可用（附 `Retry-After`，且 `meta.is_stale`） |

**重要**：**資料缺失不得回傳假資料。** 沒有資料就是 404 或 `data: []` + `meta.is_stale: true`。

### 1.4 認證

```
Authorization: Bearer <access_token>
```
- Access token 15 分鐘；Refresh token 7 天（存 Redis，可撤銷）
- 角色：`admin` / `analyst` / `viewer`

### 1.5 Rate Limit

| 端點類別 | viewer | analyst | admin |
|---------|--------|---------|-------|
| 一般讀取 | 120/min | 300/min | 600/min |
| 排行/篩選 | 30/min | 60/min | 120/min |
| Copilot | 10/min | 30/min | 60/min |
| Backtest / Event Study | 3/hour | 20/hour | 100/hour |

回應標頭：`X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`

### 1.6 分頁

- Offset 分頁：`?page=1&page_size=50`（page_size 上限 200）
- Cursor 分頁（新聞、時序）：`?cursor=<opaque>&limit=50`

### 1.7 非同步作業

任何預期 > 2 秒的操作：

```http
POST /api/v1/backtest
→ 202 Accepted
{
  "data": { "job_id": "bt_01J2XK9", "status": "PENDING",
            "poll_url": "/api/v1/jobs/bt_01J2XK9",
            "estimated_seconds": 45 },
  "meta": {...}
}
```

```http
GET /api/v1/jobs/{job_id}
→ { "data": { "job_id":"...", "status":"RUNNING", "progress":0.62,
              "result_url": null, "error": null } }
```

---

## 2. 端點總覽

```
/api/v1
├── /auth               登入、刷新、登出、我是誰
├── /market             大盤、指數、廣度、regime、健康度
├── /stocks             股票主檔、搜尋、報價、K線、指標、同業
├── /quotes             批次報價（自選股用）
├── /financials         財報、月營收、估值
├── /institutional      三大法人、融資券
├── /news               新聞列表、詳情、關聯股票、事件
├── /sectors            產業列表、輪動、熱力圖
├── /ai-score           分數、解釋、排行、權重設定
├── /factors            因子分數、因子排行
├── /predictions        ML 推論結果
├── /anomalies          異常清單
├── /events             事件清單、事件研究
├── /supply-chain       供應鏈圖、節點、路徑
├── /screener           自訂條件選股
├── /portfolio          投資組合、持倉、績效、風險
├── /backtest           回測提交、結果
├── /alerts             警示 CRUD、觸發紀錄
├── /copilot            AI 對話（SSE 串流）
├── /jobs               非同步作業狀態
├── /health             健康檢查
└── /admin              資料品質、job 監控、模型管理
```

---

## 3. Market

### `GET /market/overview`
首頁核心端點，一次取回市場全貌（減少往返）。

```jsonc
{
  "data": {
    "indices": [
      { "code":"TAIEX", "name":"發行量加權股價指數", "close":46021.48,
        "change":503.41, "change_pct":1.11, "volume":..., "turnover":... }
    ],
    "regime": {
      "regime":"RISK_ON", "score":72.4,
      "probabilities":{"RISK_ON":0.62,"SIDEWAYS":0.24,"RISK_OFF":0.14},
      "inputs":{"volatility":18.2,"breadth":0.63,"index_momentum":0.041,
                "foreign_futures_net_oi":24310}
    },
    "breadth": { "advancing":812, "declining":534, "unchanged":121,
                 "above_ma20_pct":0.631, "new_high_52w":47, "new_low_52w":12 },
    "institutional": { "foreign_net_value": 12400000000,
                       "trust_net_value": 2100000000,
                       "dealer_net_value": -430000000 },
    "top_sectors":   [ { "sector_id":3, "name":"半導體", "strength_score":88.2,
                         "rank":1, "rank_change_5d":+2, "state":"TOP" } ],
    "top_stocks":    [ { "symbol":"2330", "name":"台積電", "ai_score":91.2,
                         "change_pct":2.1, "rank":1 } ],
    "anomaly_count": 17,
    "breaking_news": [ { "id":8891, "title":"...", "published_at":"...",
                         "importance":0.86, "related_symbols":["3017","2330"] } ]
  },
  "meta": {...}
}
```

### `GET /market/regime?days=90`
Regime 歷史序列，用於畫 regime 時間軸。

### `GET /market/breadth?from=&to=`
### `GET /market/indices?codes=TAIEX,TPEX&from=&to=`

---

## 4. Stocks

### `GET /stocks/search?q=台積&limit=10`
支援代號、中文名、簡稱、英文名模糊搜尋（走 `entity_aliases` + pg_trgm）。

### `GET /stocks/{symbol}`
主檔 + 最新報價 + 最新 AI Score 摘要。

### `GET /stocks/{symbol}/prices`

| 參數 | 說明 |
|------|------|
| `timeframe` | `1d`(預設) / `1w` / `1m`；`1m`~`60m` 需授權資料，未啟用時回 451 |
| `from` / `to` | ISO date |
| `limit` | 預設 250 |
| `adjusted` | `true`(預設，還原權值) / `false` |

```jsonc
{
  "data": {
    "symbol":"2330",
    "timeframe":"1d",
    "adjusted": true,
    "bars":[ {"t":"2026-07-01","o":2495,"h":2505,"l":2475,"c":2505,
              "v":37500000,"turnover":93600000000,"trades":38210} ]
  },
  "meta": {"source":["TWSE"], "quality":{"overall":99.1}, ...}
}
```

### `GET /stocks/{symbol}/indicators?date=&names=rsi_14,macd,ma_20`
### `GET /stocks/{symbol}/peers?by=industry|supply_chain&limit=10`
### `GET /stocks/{symbol}/overview`
個股頁一次取回：報價、AI Score、因子、法人、最新新聞、風險、供應鏈鄰居。

### `GET /quotes?symbols=2330,2454,3017`
批次報價，上限 100 檔。自選股列表用。

---

## 5. Financials

### `GET /stocks/{symbol}/financials?statement=INCOME&periods=12&as_of=`

★ `as_of` 參數是 point-in-time 查詢的入口。省略時等同「現在」。

```jsonc
{
  "data": [
    { "period_end":"2026-06-30", "fiscal_year":2026, "fiscal_quarter":2,
      "announced_at":"2026-08-14T09:32:00Z",
      "announced_at_is_estimated": false,
      "revenue":..., "gross_margin":0.5821, "operating_margin":0.4712,
      "net_income":..., "eps":15.32, "roe":0.2841, "revenue_yoy":0.2214 }
  ],
  "meta": { "as_of":"2026-08-15", "source":["TWSE","MOPS"], ... }
}
```

### `GET /stocks/{symbol}/revenue?months=36`
月營收序列（含 MoM / YoY / 累計 YoY）。

### `GET /stocks/{symbol}/valuation?from=&to=`

---

## 6. Institutional

### `GET /stocks/{symbol}/institutional?days=60`
### `GET /institutional/ranking?date=&type=foreign_net&limit=50&market=TWSE`
### `GET /stocks/{symbol}/margin?days=60`

---

## 7. News

### `GET /news`

| 參數 | 說明 |
|------|------|
| `symbols` | 逗號分隔，篩選關聯個股 |
| `sectors` | 產業篩選 |
| `event_types` | `EARNINGS,ORDER_WIN,...` |
| `min_importance` | 0–1 |
| `sentiment` | `positive` / `negative` / `neutral` |
| `from` / `to` | 時間範圍 |
| `cursor` / `limit` | 分頁 |

```jsonc
{
  "data":[{
    "id":8891,
    "title":"NVIDIA Blackwell 需求優於預期，供應鏈全面拉貨",
    "summary":"...",
    "url":"https://...",
    "source":{"code":"MEDIA_X","name":"...","credibility":0.75},
    "published_at":"2026-08-15T01:20:00Z",
    "sentiment":0.72, "sentiment_confidence":0.81,
    "importance":0.86,
    "events":[{"event_type":"GUIDANCE","confidence":0.79}],
    "entities":[{"type":"ORG","text":"NVIDIA","normalized":"NVIDIA"},
                {"type":"PRODUCT","text":"Blackwell"}],
    "related_stocks":[
      {"symbol":"2330","name":"台積電","relation_type":"CUSTOMER",
       "hop_count":1,"impact_score":0.92,"impact_direction":1,
       "confidence":0.84,"time_horizon":"MEDIUM"},
      {"symbol":"3017","name":"奇鋐","relation_type":"THEMATIC",
       "hop_count":2,"impact_score":0.77,"impact_direction":1,"confidence":0.66}
    ],
    "model_version":"news-pipeline-v0.9"
  }],
  "pagination":{...}, "meta":{...}
}
```

### `GET /news/{id}`
含完整 `evidence`（原文片段位置、供應鏈路徑）。

### `GET /news/{id}/reactions`
該新聞後 T+1/3/5/10/20 各關聯股票的實際報酬與異常報酬。

### `GET /stocks/{symbol}/news?days=30`

### `GET /news/momentum?scope=sector|stock&window=5`
新聞熱度動能排行（新聞量 + 情緒 + 重要性的加權變化率）。

---

## 8. Sectors

### `GET /sectors`
### `GET /sectors/rotation?date=`

```jsonc
{
  "data":{
    "date":"2026-08-15",
    "sectors":[
      {"sector_id":3,"name":"半導體","strength_score":88.2,"rank":1,
       "rank_change_5d":2,"state":"TOP",
       "components":{"momentum":91.0,"breadth":85.3,"volume":78.2,
                     "institutional":92.1,"news_momentum":89.4,"ai_exposure":95.0}}
    ],
    "summary":{"top":["半導體","電子零組件"],"emerging":["散熱","電源"],
               "falling":["航運"],"weak":["生技"]}
  }
}
```

### `GET /sectors/{id}/stocks?sort=ai_score&limit=50`
### `GET /sectors/heatmap?date=&metric=change_pct|ai_score|institutional`

---

## 9. AI Score ★

### `GET /ai-score/{symbol}?date=`

```jsonc
{
  "data":{
    "symbol":"2330",
    "trading_date":"2026-08-15",
    "total_score":91.2,
    "rank_overall":7, "rank_in_sector":2, "percentile":0.9962,
    "score_delta_5d":+3.4, "score_delta_20d":+11.8,
    "components":{
      "technical":88.1,"fundamental":93.4,"institutional":90.2,
      "momentum":86.7,"news":89.5,"sentiment":84.2,"industry":95.0,
      "ai_trend":94.1,"valuation":61.3,"risk":72.8,"anomaly":18.0
    },
    "confidence":0.84,
    "weights_id":3,
    "model_version":"stockrank-v1.4",
    "feature_version":"core-v1.2",
    "dataset_version":"2026-08-15",
    "data_quality":98.4,
    "disclaimer":"本分數為模型推論結果，非投資建議，不代表未來績效。"
  },
  "meta":{...}
}
```

### `GET /ai-score/{symbol}/explain?date=` ★★

```jsonc
{
  "data":{
    "symbol":"2330","trading_date":"2026-08-15","total_score":91.2,
    "baseline":50.0,
    "contributions":[
      {"component":"technical.momentum_20d","label_zh":"技術動能（20日）",
       "contribution":+14.2,"weight":0.20,"raw_value":0.184,"percentile":0.94,
       "evidence":{"return_20d":0.184,"vs_sector_median":+0.061,
                   "rsi_14":68.2,"source":"TWSE","as_of":"2026-08-15"}},
      {"component":"fundamental.revenue_growth","label_zh":"營收成長",
       "contribution":+13.1,"weight":0.20,"raw_value":0.2214,"percentile":0.91,
       "evidence":{"revenue_yoy":0.2214,"period":"2026-06",
                   "announced_at":"2026-07-10T08:00:00Z","source":"TWSE"}},
      {"component":"institutional.foreign_net_20d","label_zh":"外資連續買超",
       "contribution":+12.4,"weight":0.15,"raw_value":8.42e9,"percentile":0.97,
       "evidence":{"net_value_20d":8420000000,"consecutive_days":9}},
      {"component":"news.momentum","label_zh":"AI 新聞動能",
       "contribution":+10.8,"weight":0.15,"raw_value":0.78,
       "evidence":{"news_count_7d":23,"avg_sentiment":0.61,
                   "top_news_ids":[8891,8874]}},
      {"component":"valuation.pe_percentile","label_zh":"評價偏高",
       "contribution":-4.1,"weight":0.10,"raw_value":0.88,
       "evidence":{"pe_ratio":28.4,"pe_percentile_5y":0.88}},
      {"component":"technical.rsi_overbought","label_zh":"RSI 超買",
       "contribution":-3.2,"weight":0.05,"raw_value":68.2,
       "evidence":{"rsi_14":68.2,"threshold":70}}
    ],
    "narrative":"台積電目前 91.2 分，主要來自技術動能與營收成長...",
    "narrative_model":"local-llm/qwen2.5-14b",
    "checksum_ok": true      // SUM(contributions) + baseline == total_score
  }
}
```

> `checksum_ok` 由後端計算，前端在 `false` 時**不顯示解釋**並回報異常。

### `GET /ai-score/ranking`

| 參數 | 說明 |
|------|------|
| `date` | 預設最新交易日 |
| `sector_id` | 篩選產業 |
| `min_score` | 最低分數 |
| `sort` | `total` / `delta_5d` / `delta_20d` |
| `market` | TWSE / TPEX / ALL |
| `page` / `page_size` | |

> `sort=delta_20d` 直接支援「找出最近 20 天 AI Score 上升最快的股票」。

### `GET /ai-score/weights` / `PUT /ai-score/weights`（admin）
權重讀寫。PUT 建立新版本而非覆蓋，回傳 `weights_id`。

### `POST /ai-score/simulate`（analyst+）
用自訂權重即時重算排行（不落地），用於權重探索。

---

## 10. Factors

### `GET /stocks/{symbol}/factors?date=`
### `GET /factors/ranking?factor=momentum&date=&limit=50`
### `GET /factors/correlation?date=&window=250`
因子相關矩陣，用於檢查因子是否冗餘。

---

## 11. Predictions

### `GET /predictions/{symbol}?date=`

```jsonc
{
  "data":{
    "symbol":"2330","trading_date":"2026-08-15",
    "predictions":[
      {"target":"P(5D_return>3%)","probability":0.41,"confidence":0.72,
       "model":"ret5d-lgbm-v1.2",
       "top_features":[{"name":"momentum_20d","shap":+0.084},
                       {"name":"foreign_net_20d","shap":+0.061},
                       {"name":"rsi_14","shap":-0.022}]},
      {"target":"P(20D_outperform_TAIEX)","probability":0.58,"confidence":0.69}
    ],
    "disclaimer":"以上為機率性模型推論，非價格預測，不構成投資建議。"
  },
  "meta":{"model_version":"ret5d-lgbm-v1.2","confidence":0.72,...}
}
```

**API 層強制規範**：`predictions` 陣列的每個元素必須有 `probability` 或 `expected_value` **加上** `confidence`。**禁止回傳點估計的「明日價格」。**

---

## 12. Anomalies

### `GET /anomalies?date=&min_score=70&types=VOLUME_SPIKE,NEWS_SPIKE&limit=50`

```jsonc
{
  "data":[{
    "symbol":"3017","name":"奇鋐","trading_date":"2026-08-15",
    "anomalies":[
      {"type":"VOLUME_SPIKE","score":96.0,"zscore":5.82,
       "baseline":{"mean_volume_60d":18200000,"std":6100000,"window":60},
       "observed":{"volume":75000000,"ratio":4.12}},
      {"type":"PRICE_SPIKE","score":81.0,"zscore":3.14,
       "observed":{"change_pct":0.048}},
      {"type":"NEWS_SPIKE","score":88.0,
       "observed":{"news_count_1d":21,"baseline_mean":4.0,"ratio":5.25}}
    ],
    "composite_score":96.0,
    "explanation":{"summary":"成交量為 60 日均量 4.1 倍，同時新聞量激增 425%...",
                   "related_news_ids":[8891,8902]}
  }]
}
```

### `GET /stocks/{symbol}/anomalies?days=90`

---

## 13. Events & Event Study

### `GET /events?scope=&types=&from=&to=`

### `POST /events/study` → `202 + job_id`

```jsonc
// Request
{
  "name":"NVDA 財報後台股 AI 供應鏈反應",
  "event_filter":{"external_symbol":"NVDA","event_type":"EARNINGS",
                  "from":"2020-01-01","to":"2026-06-30",
                  "magnitude_min":0.03},
  "universe":{"supply_chain_node":"AI","max_hop":2},
  "benchmark":"TAIEX",
  "model":"MARKET_MODEL",
  "estimation_window":120,
  "event_window":{"pre":5,"post":20}
}
```

### `GET /events/study/{id}`

```jsonc
{
  "data":{
    "id":42,"status":"COMPLETED","n_events":21,"n_stocks":38,
    "by_horizon":{
      "-1":{"aar":0.0021,"caar":0.0021,"t_stat":0.84,"p_value":0.402,"positive_ratio":0.53},
      "1": {"aar":0.0142,"caar":0.0142,"t_stat":3.21,"p_value":0.002,"positive_ratio":0.71},
      "3": {"aar":0.0061,"caar":0.0203,"t_stat":2.88,"p_value":0.006,"positive_ratio":0.66},
      "5": {"aar":0.0034,"caar":0.0237,"t_stat":2.41,"p_value":0.018,"positive_ratio":0.62},
      "10":{"aar":0.0012,"caar":0.0249,"t_stat":1.92,"p_value":0.061,"positive_ratio":0.58},
      "20":{"aar":-0.0008,"caar":0.0241,"t_stat":1.44,"p_value":0.156,"positive_ratio":0.55}
    },
    "by_stock":[{"symbol":"2330","caar_5d":0.0181,"n":21,"t_stat":2.10}],
    "volume_change":{"1":2.14,"3":1.62},
    "volatility_change":{"1":1.83,"3":1.41},
    "caveats":["樣本數 21 場，統計檢定力有限",
               "估計期含 2020 疫情極端波動，market model 參數可能不穩定"],
    "dataset_version":"2026-08-15"
  }
}
```

> **`caveats` 欄位是強制的。** 事件研究最容易被過度解讀，系統必須主動說出樣本數與統計限制。

### `GET /events/lead-lag?us_symbol=NVDA&threshold=0.05`
歷史統計：美股標的大漲/大跌時，台股供應鏈的歷史平均反應（含樣本數、標準差、t 值）。

---

## 14. Supply Chain

### `GET /supply-chain/graph?root=AI&max_hop=3&as_of=`

```jsonc
{
  "data":{
    "nodes":[
      {"id":1,"code":"AI","type":"THEME","name":"AI","level":0},
      {"id":12,"code":"COWOS","type":"SEGMENT","name":"CoWoS","level":1},
      {"id":301,"code":"STOCK:2330","type":"COMPANY","name":"台積電",
       "symbol":"2330","level":2,"ai_score":91.2,"change_pct":2.1,
       "market_cap":...}
    ],
    "edges":[
      {"from":1,"to":12,"type":"THEME_OF","strength":0.95,"confidence":0.9,
       "evidence":{"type":"industry_taxonomy"}},
      {"from":12,"to":301,"type":"BELONGS_TO","strength":0.92,"confidence":0.95,
       "evidence":{"type":"annual_report","url":"...","quote":"...","as_of":"2025-12-31"}}
    ],
    "as_of":"2026-08-15"
  }
}
```

### `GET /supply-chain/stocks/{symbol}/neighbors?types=SUPPLIER,CUSTOMER&max_hop=2`
### `GET /supply-chain/path?from=NVDA&to=3017`
回傳傳播路徑與每跳強度 → 用於解釋「為什麼這則 NVIDIA 新聞會影響奇鋐」。

---

## 15. Screener

### `POST /screener/run`

```jsonc
{
  "universe":{"markets":["TWSE","TPEX"],"min_market_cap":1e9,
              "exclude_types":["WARRANT"]},
  "filters":{
    "op":"AND",
    "children":[
      {"metric":"ai_score.total","op":">=","value":80},
      {"metric":"factor.momentum.percentile","op":">=","value":0.8},
      {"metric":"institutional.foreign_net_20d","op":">","value":0},
      {"metric":"financial.revenue_yoy","op":">=","value":0.15},
      {"op":"OR","children":[
        {"metric":"supply_chain.theme","op":"in","value":["AI","COWOS"]},
        {"metric":"news.momentum_7d","op":">=","value":0.7}]}
    ]
  },
  "sort":{"field":"ai_score.total","order":"desc"},
  "limit":50,
  "as_of":"2026-08-15"
}
```

回應含每檔股票**通過了哪些條件與其實際值**（可解釋性）。

### `GET /screener/metrics`
可用的 metric 清單與其定義、單位、來源 → 前端動態生成篩選器 UI。

---

## 16. Portfolio

```
GET    /portfolio                       列表
POST   /portfolio                       建立
GET    /portfolio/{id}                  詳情（持倉 + 市值 + 損益）
POST   /portfolio/{id}/transactions     新增交易
GET    /portfolio/{id}/performance?from=&to=   績效曲線 vs 大盤
GET    /portfolio/{id}/risk             風險分析
GET    /portfolio/{id}/exposure         曝險分析
```

`GET /portfolio/{id}/risk` 回應：

```jsonc
{
  "data":{
    "volatility_annual":0.284,"beta_vs_taiex":1.14,
    "max_drawdown":-0.182,"var_95_1d":-0.031,"cvar_95_1d":-0.047,
    "concentration":{"top1_weight":0.34,"top5_weight":0.78,"hhi":0.21},
    "sector_exposure":[{"sector":"半導體","weight":0.52,"vs_taiex":+0.21}],
    "factor_exposure":[{"factor":"momentum","exposure":+1.24},
                       {"factor":"value","exposure":-0.83}],
    "ai_exposure":0.68,
    "correlation_matrix_uri":"/api/v1/portfolio/1/correlation",
    "warnings":["半導體曝險 52%，集中度高於大盤 21 個百分點",
                "前 5 大持股佔 78%，單一事件風險顯著"]
  }
}
```

---

## 17. Backtest

### `POST /backtest` → `202 + job_id`

```jsonc
{
  "name":"AI Score > 85 週再平衡",
  "start_date":"2019-01-01","end_date":"2026-06-30",
  "initial_capital":1000000,
  "universe":{"markets":["TWSE"],"min_avg_turnover_20d":50000000,
              "include_delisted":true},          // ★ 預設 true，防 survivorship bias
  "entry":{"op":"AND","children":[
    {"metric":"ai_score.total","op":">=","value":85}]},
  "exit":{"op":"OR","children":[
    {"metric":"ai_score.total","op":"<","value":60},
    {"metric":"holding_days","op":">=","value":20},
    {"metric":"stop_loss_pct","op":"<=","value":-0.08}]},
  "sizing":{"method":"equal_weight","max_positions":20},
  "rebalance":"weekly",
  "costs":{"commission_bps":14.25,"tax_bps":30,"slippage_bps":10,
           "min_commission":20},
  "execution":{"price":"next_open","delay_days":1}   // ★ 訊號 T 日、T+1 開盤成交
}
```

### `GET /backtest/{id}`

```jsonc
{
  "data":{
    "id":17,"status":"COMPLETED",
    "metrics":{
      "total_return":1.842,"cagr":0.1621,"annual_return":0.1621,
      "volatility":0.2413,"sharpe":0.672,"sortino":0.941,"calmar":0.612,
      "max_drawdown":-0.2648,"max_drawdown_days":187,
      "win_rate":0.542,"profit_factor":1.38,
      "avg_win":18420,"avg_loss":-12180,
      "trade_count":842,"turnover":4.21,
      "total_commission":118400,"total_tax":249300,"total_slippage":83100,
      "benchmark_return":1.214,"alpha":0.0312,"beta":1.08,
      "information_ratio":0.41
    },
    "equity_curve_url":"/api/v1/backtest/17/equity",
    "trades_url":"/api/v1/backtest/17/trades",
    "monthly_returns":{...},
    "reproducibility":{"dataset_version":"2026-08-15","feature_version":"core-v1.2",
                       "model_version":"stockrank-v1.4","code_version":"a3f9c21",
                       "random_seed":42},
    "bias_checks":{
      "look_ahead":"PASS","survivorship":"PASS (含 87 檔下市股)",
      "data_leakage":"PASS","corporate_action_adjusted":true,
      "execution_delay_applied":true
    },
    "disclaimer":"歷史回測不代表未來績效。已扣除手續費、交易稅與估計滑價。"
  }
}
```

> **`bias_checks` 是強制欄位。** 每次回測都要自我聲明通過了哪些偏誤檢查。任一項為 FAIL 時，前端以紅色橫幅顯示且不顯示績效數字。

### `GET /backtest/{id}/trades?page=`
### `GET /backtest/{id}/equity`

---

## 18. Alerts

```
GET    /alerts
POST   /alerts
PATCH  /alerts/{id}
DELETE /alerts/{id}
POST   /alerts/{id}/test        用當日資料試跑，回傳會觸發哪些股票
GET    /alerts/events?unread=true&limit=50
POST   /alerts/events/{id}/read
```

---

## 19. Copilot ★

### `POST /copilot/chat`（SSE 串流）

```jsonc
// Request
{
  "conversation_id":"c_01J...",   // 可選
  "message":"為什麼奇鋐今天大漲？",
  "context":{"symbol":"3017","date":"2026-08-15"}
}
```

SSE 事件序列：

```
event: tool_call
data: {"tool":"get_stock_price","args":{"symbol":"3017","date":"2026-08-15"}}

event: tool_result
data: {"tool":"get_stock_price","summary":"收盤 812 元，+4.8%，量 7,500 萬股"}

event: tool_call
data: {"tool":"get_stock_news","args":{"symbol":"3017","days":3}}

event: tool_result
data: {"tool":"get_stock_news","summary":"3 日內 21 則新聞，平均情緒 +0.68"}

event: token
data: {"text":"奇鋐今日上漲 4.8%"}

event: token
data: {"text":"，主要與以下三項可觀察到的因素相關："}

...

event: citations
data: {"citations":[
  {"type":"price","source":"TWSE","as_of":"2026-08-15T05:30:00Z",
   "url":"/stocks/3017"},
  {"type":"news","news_id":8891,"title":"...","published_at":"...",
   "url":"https://..."},
  {"type":"anomaly","anomaly_id":331,"score":96.0}]}

event: done
data: {"conversation_id":"c_01J...","model":"local-llm/qwen2.5-14b",
       "tools_used":["get_stock_price","get_stock_news","get_anomalies"],
       "data_timestamp":"2026-08-15T05:30:00Z",
       "confidence":"medium",
       "disclaimer":"以上為根據可取得資料的分析，非投資建議。"}
```

**硬性規範**：
1. Copilot 的每個事實陳述都必須對應到一個 `citation`。無法引用的內容不得輸出。
2. 若工具查無資料，Copilot 必須明說「沒有查到 X 的資料」，**禁止推測**。
3. 每個回應結尾必須有 `data_timestamp` 與 `disclaimer`。

### `GET /copilot/tools`
列出可用工具與其 schema（透明度；也讓前端顯示「AI 用了哪些工具」）。

### `GET /copilot/conversations` / `GET /copilot/conversations/{id}`

---

## 20. Jobs / Health / Admin

```
GET  /jobs/{job_id}
GET  /health                      公開，見 ARCHITECTURE §13.2
GET  /health/detailed             admin

GET  /admin/data-quality?dataset=&from=&to=
GET  /admin/data-gaps?status=OPEN
POST /admin/backfill              觸發回補 → 202 + job_id
GET  /admin/jobs?status=FAILED&limit=50
POST /admin/jobs/{name}/trigger   手動觸發
GET  /admin/models
POST /admin/models/{id}/activate  模型上線（前一版轉 RETIRED）
GET  /admin/models/{id}/monitoring
GET  /admin/quarantine
GET  /admin/audit-logs
```

---

## 21. WebSocket（Phase 10）

```
WS /ws/quotes?symbols=2330,3017      盤中報價推送（需授權資料源）
WS /ws/alerts                        警示即時推送
WS /ws/jobs/{job_id}                 長任務進度
```

在取得即時資料授權前，`/ws/quotes` 回傳 451 並附說明。`/ws/alerts` 與 `/ws/jobs` 不受限制。

---

## 22. OpenAPI 產生與型別同步

```bash
# 後端：FastAPI 自動產生
curl localhost:8000/api/v1/openapi.json > web/openapi.json

# 前端：產生 TypeScript 型別
pnpm openapi-typescript web/openapi.json -o web/lib/api/schema.d.ts
```

CI 檢查：若 `openapi.json` 與 commit 中的版本不一致 → **build 失敗**。這保證前端型別永遠與後端同步。

---

## 23. API 設計檢查表（每新增一個端點都要過）

- [ ] 回應含完整 `meta`（timestamp / source / version / quality）
- [ ] 錯誤回應符合 Problem Details 格式
- [ ] 列表端點有分頁且 `page_size` 有上限
- [ ] 預期 > 2 秒的操作回 `202 + job_id`
- [ ] 涉及模型推論的回應有 `confidence` 與 `disclaimer`
- [ ] 涉及未來預測的回應是機率而非點估計
- [ ] MockProvider 資料的回應 `meta.is_demo = true`
- [ ] 有對應的 Pydantic response model（自動進 OpenAPI）
- [ ] 有 integration test 覆蓋成功與失敗路徑
- [ ] 有 rate limit 分類
- [ ] 有 RBAC 標註
- [ ] 寫入類操作有 audit log
