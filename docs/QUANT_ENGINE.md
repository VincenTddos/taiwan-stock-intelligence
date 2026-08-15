# QUANT_ENGINE.md — 量化引擎規格

> 版本 0.1 · 2026-08-15
> 涵蓋：技術指標、量化因子、因子模型、回測引擎、事件研究、市場 Regime、異常偵測的統計基礎

---

## 0. 設計原則

1. **所有指標與因子都是純函式。** 輸入 DataFrame，輸出 DataFrame，無副作用、無 I/O、無全域狀態 → 可單元測試、可重現、可向量化。
2. **所有計算只用 `as_of` 當下可得的資料。** 任何函式簽名都帶 `as_of`，且實作中不得存取 `as_of` 之後的列。
3. **參數不寫死。** 所有視窗長度、閾值來自 `FactorSpec` / 設定，並記入 `feature_version`。
4. **NaN 是合法輸出。** 資料不足時回傳 NaN，**不做前向填補**（除非該指標定義本身要求），因為填補會製造虛假訊號。
5. **統計顯著性必須報告。** 任何「歷史上平均反應 X%」的陳述都要附樣本數與 t 值。

---

## 1. 技術指標（Technical Analysis Engine）

### 1.1 實作方式

```python
# indicators/momentum.py
def rsi(close: pl.Series, window: int = 14) -> pl.Series:
    """Wilder's RSI. 前 window 期回傳 null."""
```

- 使用 **polars** 為主（比 pandas 快 5–10 倍且記憶體友善），全市場批次計算時走 `group_by('stock_id').agg(...)` 或分組 map
- **不依賴 TA-Lib 作為執行期依賴**（C 編譯安裝在跨平台上是麻煩），但**用 TA-Lib 作為測試的黃金基準**（dev dependency）
- 輸出寫入 `technical_indicators.indicators` JSONB

### 1.2 指標清單

#### Trend

| 指標 | 參數 | 定義要點 |
|------|------|---------|
| `ma_{n}` | 5,10,20,60,120,240 | 簡單移動平均 |
| `ema_{n}` | 12,26,60 | 指數移動平均，`α = 2/(n+1)` |
| `ma_slope_{n}` | 20,60 | `(ma_t - ma_{t-5}) / ma_{t-5}` |
| `price_vs_ma_{n}` | 20,60,240 | `(close - ma_n) / ma_n` |
| `ma_alignment` | — | 多頭排列(1)／空頭排列(-1)／糾結(0)：`ma5>ma20>ma60` |
| `adx_14`, `di_plus`, `di_minus` | 14 | Wilder ADX，趨勢強度 |
| `aroon_up/down_25` | 25 | |
| `supertrend_10_3` | 10, 3 | ATR 通道趨勢 |

#### Momentum

| 指標 | 參數 |
|------|------|
| `roc_{n}` | 5,10,20,60,120 |
| `rsi_{n}` | 6,14,24 |
| `macd`, `macd_signal`, `macd_hist` | 12,26,9 |
| `kd_k`, `kd_d` | 9,3,3（台股慣用 KD） |
| `williams_r_14` | 14 |
| `cci_20` | 20 |
| `momentum_{n}` | 20,60,120（`close_t / close_{t-n} - 1`） |
| `relative_strength_taiex_{n}` | 20,60（個股報酬 − 大盤報酬） |

#### Volatility

| 指標 | 定義 |
|------|------|
| `atr_14` | Wilder ATR |
| `atr_pct_14` | `atr_14 / close` |
| `hv_{n}` | 20,60：`std(log_return) × sqrt(252)` |
| `parkinson_hv_20` | 用 High/Low 的高效波動估計 |
| `bb_upper/mid/lower_20_2` | Bollinger |
| `bb_width_20` | `(upper - lower) / mid` |
| `bb_percent_b` | `(close - lower) / (upper - lower)` |
| `vol_regime` | 當前 HV20 在過去 250 日的百分位 |
| `downside_deviation_60` | 只計負報酬的標準差 |

#### Volume

| 指標 | 定義 |
|------|------|
| `volume_ma_{n}` | 5,20,60 |
| `volume_ratio_{n}` | `volume / volume_ma_n` |
| `obv` | On-Balance Volume |
| `obv_slope_20` | |
| `vwap_{n}` | 20（`Σ(turnover) / Σ(volume)`，用實際成交金額而非 typical price） |
| `price_vs_vwap_20` | |
| `mfi_14` | Money Flow Index |
| `volume_breakout` | `volume_ratio_20 > 2 AND close > ma_20` |
| `turnover_rate` | `volume / shares_outstanding` |
| `amihud_illiquidity_20` | `mean(|return| / turnover)` — 流動性因子的基礎 |

#### Market

| 指標 | 定義 |
|------|------|
| `beta_{n}` | 60,250：對 TAIEX 的 OLS beta |
| `alpha_{n}` | 同上迴歸截距（年化） |
| `corr_taiex_{n}` | 60,250 |
| `corr_sector_60` | 對所屬產業指數 |
| `idio_vol_60` | 殘差波動（市場模型殘差的標準差） |
| `max_drawdown_{n}` | 60,250 |

### 1.3 台股特有的處理

| 情境 | 處理 |
|------|------|
| 除權息 | 用 `corporate_actions.adjust_factor` 還原後再算指標。**未還原的價格算出的 MA 會有假跳空** |
| 漲跌停 | `limit_up`/`limit_down` 為 true 的日子，成交量可能極低而失真 → 部分指標標記低可信度 |
| 停牌 | `is_suspended` 的日子不參與計算，且不視為缺漏 |
| 新上市 | 上市未滿 `window` 日 → 該指標為 null，**不用較短視窗代替** |
| 減資 | `adjust_factor` 必須涵蓋減資，否則會出現假的巨幅漲跌 |

---

## 2. 因子引擎（Factor Engine）

### 2.1 因子規格（`FactorSpec`）

每個因子由宣告式規格定義，存於 `feature_versions.spec`：

```python
FactorSpec(
    name="momentum_12_1",
    category="momentum",
    description="12 個月動能，跳過最近 1 個月（避免短期反轉）",
    formula="close[t-21] / close[t-252] - 1",
    inputs=["daily_prices.close"],
    lookback_days=252,
    min_periods=200,
    winsorize=(0.01, 0.99),
    standardize="zscore",           # zscore / rank / sector_neutral_zscore
    neutralize=["sector", "size"],   # 中性化維度
    direction=+1,                    # +1 表示越大越好
)
```

### 2.2 十大因子類別

#### Value（價值）

| 因子 | 定義 | 註記 |
|------|------|------|
| `earnings_yield` | `EPS(TTM) / price` | 用倒數而非 PE，避免負值與無限大 |
| `book_to_price` | `每股淨值 / price` | |
| `sales_to_price` | `每股營收(TTM) / price` | |
| `fcf_yield` | `每股自由現金流(TTM) / price` | |
| `dividend_yield` | 近 12 個月現金股利 / price | |
| `ev_to_ebitda_inv` | `1 / EV/EBITDA` | |

> **PE 的陷阱**：虧損公司 PE 為負或無意義。一律用 earnings yield 並對負值單獨分組處理。

#### Growth（成長）

| 因子 | 定義 |
|------|------|
| `revenue_yoy` | 最新月營收 YoY |
| `revenue_yoy_3m` | 近 3 月營收合計 YoY |
| `revenue_acceleration` | `revenue_yoy - revenue_yoy_prev` |
| `eps_yoy` | 最新季 EPS YoY |
| `eps_growth_4q` | 近 4 季 EPS 合計 vs 前 4 季 |
| `operating_income_yoy` | |
| `revenue_surprise` | 實際月營收 vs 近 12 月趨勢外推的殘差 z 值 |

> **月營收是台股相對美股的資訊優勢** —— 每月都有一次基本面更新，比季報頻率高 3 倍。這應該是 Growth 因子的主力。

#### Quality（品質）

| 因子 | 定義 |
|------|------|
| `roe` / `roa` / `roic` | TTM |
| `gross_margin` / `operating_margin` | TTM |
| `margin_stability` | 近 8 季毛利率的標準差（越低越好，direction=-1） |
| `accruals` | `(淨利 - 營運現金流) / 總資產`（越低越好） |
| `debt_to_equity` | direction=-1 |
| `interest_coverage` | |
| `cash_conversion` | `營運現金流 / 淨利` |
| `earnings_variability` | 近 12 季 EPS 的變異係數，direction=-1 |

#### Momentum（動能）

| 因子 | 定義 |
|------|------|
| `momentum_12_1` | 標準學術動能 |
| `momentum_6_1` | |
| `momentum_1m` | 短期，通常 direction=-1（反轉效應） |
| `rs_vs_taiex_60` | 相對強度 |
| `rs_vs_sector_60` | 產業內相對強度 |
| `52w_high_proximity` | `close / max(high, 250)` |
| `momentum_consistency` | 近 60 日中上漲天數比例 |

#### Volatility（波動）

`hv_60`、`idio_vol_60`、`beta_250`、`downside_deviation_60`、`max_drawdown_250` — 全部 direction=-1（低波動異象）

#### Liquidity（流動性）

`avg_turnover_20d`、`turnover_rate_20d`、`amihud_illiquidity_60`、`zero_volume_days_60`

#### Size（規模）

`log_market_cap`（direction=-1，小型股效應）、`float_market_cap`

#### Sentiment（情緒）

| 因子 | 來源 |
|------|------|
| `news_sentiment_7d` | `news.sentiment` 加權平均（權重 = importance × credibility） |
| `news_sentiment_change` | 7d vs 前 7d |
| `sentiment_dispersion` | 情緒分歧度（標準差）—— 分歧大常伴隨波動 |

#### News（新聞熱度）

| 因子 | 定義 |
|------|------|
| `news_count_ratio_7d` | 近 7 日新聞量 / 過去 60 日日均 |
| `news_importance_sum_7d` | |
| `news_novelty_7d` | 新聞 embedding 與過去 30 日新聞的平均距離（越新穎越高） |
| `event_intensity_7d` | 高權重事件（EARNINGS/ORDER_WIN/GUIDANCE）的加權計數 |

#### AI Exposure（AI 曝險）★ 本產品特色

| 因子 | 定義 |
|------|------|
| `ai_supply_chain_centrality` | 在 AI 供應鏈圖中的加權度中心性 |
| `ai_theme_strength` | `Σ(edge.strength)` 到 AI 主題節點，含跳數衰減 |
| `ai_revenue_share` | 若年報揭露則用實際比例，否則用 `supply_chain_edges.revenue_share` |
| `ai_news_share_60d` | 該股新聞中含 AI 相關實體的比例 |
| `ai_beta` | 對「AI 概念股等權組合報酬」的迴歸 beta |

> `ai_beta` 是純統計量，**不依賴任何主觀分類**，可用來驗證主觀的 AI 供應鏈標籤是否正確。兩者背離時應該告警（可能是圖譜過期）。

### 2.3 因子處理流程（每個因子都走一遍）

```
raw_value
   ↓  1. 缺失處理：不足 min_periods → NaN（不填補）
   ↓  2. Winsorize：截斷 1% / 99% 極端值（防單一離群值主導）
   ↓  3. 產業中性化：raw - sector_median（可設定開關）
   ↓  4. 市值中性化：對 log_market_cap 迴歸取殘差（可設定開關）
   ↓  5. 標準化：zscore（全市場）或 rank → percentile
   ↓  6. 方向調整：direction = -1 者取負號
final_score  → factor_scores 表
```

### 2.4 因子有效性驗證（Phase 3 的 DoD）

每個因子上線前必須通過：

| 檢驗 | 通過標準 |
|------|---------|
| **IC（Information Coefficient）** | `corr(factor_t, forward_return_{t+20})` 的時序平均 \|IC\| > 0.02 |
| **Rank IC** | Spearman 版本，\|Rank IC\| > 0.02 |
| **IC IR** | `mean(IC) / std(IC)` > 0.3 |
| **分層單調性** | 依因子分 5 組，Q1→Q5 的平均報酬單調（或至少 Q5-Q1 顯著） |
| **多空價差** | `Q5 - Q1` 年化報酬 t 值 > 2 |
| **覆蓋率** | 全市場非 NaN 比例 > 70% |
| **穩定性** | 相鄰月份的因子值 rank correlation > 0.5（不能太跳動） |
| **與既有因子相關性** | 與任一既有因子 \|corr\| < 0.8（否則冗餘） |

未通過的因子**可以保留在資料庫供研究，但不得進入 AI Score**。這條規則寫在 `AI_ENGINE.md` §2。

---

## 3. Factor Model（多因子模型）

### 3.1 橫斷面迴歸（Fama-MacBeth）

每個交易日執行：

```
r_{i,t+1} = α_t + Σ_k β_{k,t} · f_{k,i,t} + ε_{i,t}
```

- 逐日估計因子報酬 `β_{k,t}`
- 對時序取平均與 t 值 → 判斷因子是否有持續的風險溢酬
- 輸出：`factor_returns` 序列（可用於歸因分析）

### 3.2 個股 FactorScore 合成

```
FactorScore_i = Σ_k w_k · z_{k,i}
```

其中 `w_k` 可為：
- **等權**（baseline，最穩健）
- **IC 加權**：`w_k ∝ mean(IC_k)`，滾動 250 日估計
- **IC-IR 加權**：`w_k ∝ IC_k / std(IC_k)`
- **回歸權重**：由 Fama-MacBeth 的 `mean(β_k)` 決定

預設用 **IC-IR 加權，且權重每季更新一次**（過度頻繁更新會過擬合）。權重與更新時間存入 `feature_versions.spec`。

### 3.3 風險模型（Phase 9 Portfolio 用）

簡化的多因子風險模型：

```
Cov(r) = B · F · B' + D
```
- `B`：個股對因子的曝險矩陣
- `F`：因子報酬共變異數（250 日）
- `D`：特異風險對角矩陣（`idio_vol_60²`）

用於投資組合的風險分解與集中度分析。**不用於最佳化配權**（個人自用場景下，等權或市值加權更穩健，且避免最佳化器對估計誤差的放大）。

---

## 4. Backtesting Engine ★

### 4.1 架構

```
StrategyConfig ─→ ┌──────────────────────────────┐
                  │  Universe Builder            │  ★ 含下市股
                  └───────────┬──────────────────┘
                              ↓ 每個 rebalance 日
                  ┌──────────────────────────────┐
                  │  Point-in-Time Data Loader   │  ★ announced_at <= as_of
                  └───────────┬──────────────────┘
                              ↓
                  ┌──────────────────────────────┐
                  │  Signal Generator            │
                  └───────────┬──────────────────┘
                              ↓ T 日產生訊號
                  ┌──────────────────────────────┐
                  │  Execution Simulator         │  ★ T+1 開盤成交
                  │  + Cost Model + Slippage     │
                  └───────────┬──────────────────┘
                              ↓
                  ┌──────────────────────────────┐
                  │  Portfolio Accountant        │  持倉、現金、除權息
                  └───────────┬──────────────────┘
                              ↓
                  ┌──────────────────────────────┐
                  │  Metrics Calculator          │
                  └──────────────────────────────┘
```

### 4.2 六大偏誤與具體防範

| 偏誤 | 為什麼會發生 | 本系統的防範 |
|------|-------------|-------------|
| **Look-ahead bias** | 用了當時還沒公布的資料 | 所有 loader 走 `announced_at <= as_of`；`PointInTimeLoader` 是唯一入口，直接查 curated 表的程式碼會被 CI 攔下 |
| **Survivorship bias** | 股票池只含今天還活著的股票 | `Universe Builder` 預設 `include_delisted=true`，用 `listing_date <= as_of < COALESCE(delisting_date, ∞)` 建池。下市股在下市日以最後成交價（或清算價）平倉 |
| **Data leakage** | 特徵計算時用了未來資訊（如全期間 z-score） | 標準化只用 `as_of` 之前的橫斷面；任何 `expanding`/`rolling` 都必須 `closed='left'` |
| **Future information leakage** | 訊號日與成交日相同 | 強制 `execution.delay_days >= 1`，預設 T 日收盤後產生訊號 → T+1 開盤成交 |
| **Delisted stock bias** | 下市股票資料缺失被當成「沒發生」 | `data_gaps` 記錄；下市當日強制平倉並計入損失 |
| **Corporate action errors** | 除權息造成假跳空被當成報酬 | 一律用 `adjust_factor` 還原價計算報酬；現金股利計入報酬但不計入價格 |

### 4.3 成本模型（台股實際費率）

```python
class TWCostModel:
    commission_rate = 0.001425        # 券商手續費 0.1425%（買賣皆收）
    commission_discount = 1.0         # 券商折扣，可設定（如 0.6 = 六折）
    min_commission = 20               # 最低手續費 20 元
    tax_rate_sell = 0.003             # 證券交易稅 0.3%（僅賣出）
    tax_rate_sell_etf = 0.001         # ETF 為 0.1%
    slippage_bps = 10                 # 預設滑價，可依流動性動態調整
```

**動態滑價模型**（比固定 bps 更真實）：

```
slippage_bps = base_bps + impact_coef × (order_value / avg_turnover_20d) × 10000
```
訂單佔日均成交額比例越高，滑價越大。`impact_coef` 預設 0.1，可設定。

**流動性限制**：單日買入不得超過該股 20 日均量的 X%（預設 10%），超過部分順延至下一交易日 —— 否則回測會出現「買進一檔小型股 10 億」這種不可能的交易。

### 4.4 Walk-Forward 驗證

```
|--- train 3y ---|-- embargo 5d --|- test 6m -|
      |--- train 3y ---|-- embargo --|- test 6m -|
            |--- train 3y ---|-- embargo --|- test 6m -|
```

- **Embargo period**：train 與 test 之間留 `max(label_horizon) + 1` 個交易日的空窗，防止標籤重疊造成的洩漏
- **Purging**：訓練集中移除其標籤區間與測試集重疊的樣本
- 每個 fold 獨立訓練、獨立評估，最後彙總 out-of-sample 績效

### 4.5 回測指標（完整清單）

#### 報酬

`total_return` · `cagr` · `annual_return` · `monthly_returns` · `best_month` · `worst_month`

#### 風險調整

| 指標 | 公式 |
|------|------|
| `sharpe` | `(mean(r) - rf) / std(r) × √252` |
| `sortino` | `(mean(r) - rf) / downside_std × √252` |
| `calmar` | `cagr / |max_drawdown|` |
| `information_ratio` | `mean(r - r_bench) / std(r - r_bench) × √252` |
| `omega_ratio` | `Σ(gains) / Σ(losses)` above threshold |

> `rf`（無風險利率）用台灣 10 年期公債殖利率或央行重貼現率，存於設定，**不預設為 0**（會虛高 Sharpe）。

#### 回撤

`max_drawdown` · `max_drawdown_days`（水下天數）· `avg_drawdown` · `recovery_factor` · `ulcer_index`

#### 交易

`win_rate` · `profit_factor` · `avg_win` · `avg_loss` · `payoff_ratio` · `expectancy` · `trade_count` · `avg_holding_days` · `turnover`（年化換手率）

#### 成本

`total_commission` · `total_tax` · `total_slippage` · `cost_drag`（成本佔總報酬比例）

> **`cost_drag` 是最誠實的指標。** 一個換手率 20 倍的策略，成本可能吃掉全部超額報酬。UI 必須把成本前後的績效並列顯示。

#### 對標

`benchmark_return` · `alpha` · `beta` · `correlation` · `up_capture` · `down_capture` · `excess_return`

### 4.6 統計穩健性檢驗（避免過擬合的自我防衛）

回測完成後自動執行：

| 檢驗 | 目的 |
|------|------|
| **參數敏感度** | 主要參數 ±20% 掃描，若績效崩潰 → 標記「參數脆弱」 |
| **子期間穩定性** | 切成 3 個等長子期間，各自 Sharpe 皆 > 0 才算穩健 |
| **隨機基準比較** | 同樣換手率、同樣持股數的隨機選股 1,000 次，計算策略績效的百分位 |
| **Deflated Sharpe Ratio** | 修正多次試驗造成的選擇偏誤 |
| **交易成本敏感度** | 滑價 ×2、×3 時績效如何 |

結果寫入 `backtest_metrics` 並在 UI 顯示。**任何 Sharpe > 2 的回測結果，UI 一律附加「請檢查是否過擬合」的提示** —— 台股日頻策略要達到 Sharpe > 2 極為罕見。

---

## 5. Event Study Engine

### 5.1 方法論

標準的事件研究（Brown & Warner / MacKinlay）：

```
估計期              事件期
[-250, -21]  ...  [-5, +20]
     ↓
估計正常報酬模型
```

**三種正常報酬模型**：

| 模型 | 公式 | 適用 |
|------|------|------|
| Market-adjusted | `E[r_i] = r_m` | 樣本少、快速估計 |
| Market model | `E[r_i] = α_i + β_i·r_m`（OLS 於估計期） | **預設** |
| Fama-French 3 factor | 加入 SMB、HML | 需先建立台股 FF 因子 |

**異常報酬**：
```
AR_{i,t} = r_{i,t} - E[r_{i,t}]
CAR_i(t1,t2) = Σ AR_{i,t}
AAR_t = (1/N) Σ_i AR_{i,t}
CAAR(t1,t2) = Σ AAR_t
```

**檢定統計量**：
```
t = CAAR / (σ(CAR) / √N)
```
同時報告 **BMP 檢定**（Boehmer-Musumeci-Poulsen，對事件誘發的變異數增加穩健）與 **符號檢定**（無母數）。

### 5.2 台股情境的實作要點

| 議題 | 處理 |
|------|------|
| 事件日定義 | 用 `announced_at`。若在收盤後公布，事件日 = 下一交易日 |
| 漲跌停 | 漲跌停日的報酬被截斷 → 標記並在敏感度分析中排除 |
| 事件叢集 | 多檔股票同日發生同一事件（如 NVDA 財報）→ 橫斷面相關，標準誤要用 crude dependence adjustment |
| 樣本過少 | N < 10 時只報告描述統計，**不做顯著性宣稱** |
| 估計期污染 | 估計期內若有其他重大事件 → 該樣本標記，可選擇排除 |

### 5.3 應用一：NVDA → 台股供應鏈

```
輸入：external_symbol='NVDA', event_type='EARNINGS', magnitude_min=0.03
universe：supply_chain 中 AI 主題 2 跳內的台股
輸出：
  by_horizon: T+1/3/5/10/20 的 AAR / CAAR / t / p / positive_ratio
  by_stock:   每檔股票的歷史平均反應與樣本數
  caveats:    樣本數限制、期間結構性變化提醒
```

### 5.4 應用二：Lead-Lag 模型

```
對每個 (us_symbol, tw_symbol) 對：
  1. 對齊時區與交易日（美股 T 日收盤 → 台股 T+1 日）
  2. 計算 lead-lag 相關：corr(r_us[t], r_tw[t+k])，k ∈ [0, 3]
  3. 迴歸：r_tw[t+1] = α + β·r_us[t] + ε
  4. 條件反應：|r_us| > threshold 時的 r_tw 分布
輸出必須包含：n_samples, beta, se, t_stat, r_squared, 期間
```

**硬性規範**：Lead-Lag 的任何數字都必須由上述計算產生。文件與 UI 中出現的範例值（如「NVDA +5% → 台積電 +1.4%」）在實作前一律標示為 `ILLUSTRATIVE ONLY`，實作後替換為真實統計並附樣本數。

---

## 6. Market Regime Detection

### 6.1 輸入特徵

| 維度 | 特徵 |
|------|------|
| 波動 | TAIEX 20 日 HV、HV 的 250 日百分位、VIX 台指（若可得） |
| 廣度 | `above_ma20_pct`、`above_ma60_pct`、新高/新低比、漲跌家數比 |
| 動能 | TAIEX 20/60 日報酬、指數與 MA60 的距離 |
| 量能 | 成交金額 20 日均 vs 250 日均 |
| 相關性 | 個股間平均相關係數（相關性飆高 = 恐慌） |
| 籌碼 | 外資現貨淨買賣、外資台指期未平倉淨額 |

### 6.2 兩種方法並行

**方法 A：規則式（透明、可解釋，作為 baseline）**

```
score = w1·z(momentum) + w2·z(breadth) - w3·z(volatility)
        - w4·z(correlation) + w5·z(foreign_flow)

score > +1.0        → RISK_ON
-1.0 ≤ score ≤ +1.0 → SIDEWAYS
score < -1.0        → RISK_OFF
volatility_pct > 0.85 → 額外標記 HIGH_VOL
trend: TAIEX > MA240 且 MA60 上升 → BULL / 反之 BEAR
```

**方法 B：Hidden Markov Model（統計式）**

3–4 狀態 Gaussian HMM，輸入為 (報酬, 波動, 廣度)。輸出各狀態的後驗機率。

**兩者並行顯示。** 一致時信心高；不一致時 UI 顯示「訊號分歧」—— 這本身就是有用的資訊。

### 6.3 防止 regime 頻繁跳動

- 加入**最短持續期**（預設 5 個交易日）：新 regime 需連續 5 日才確認切換
- 顯示時同時給「當前 regime」與「未平滑的原始訊號」

---

## 7. Anomaly Detection

### 7.1 分層偵測

**Layer 1：統計基準（透明、快速）**

```
z = (x_t - rolling_mean(x, 60)) / rolling_std(x, 60)
anomaly if |z| > threshold
```

| 類型 | 觀測量 | 預設閾值 |
|------|--------|---------|
| `VOLUME_SPIKE` | `log(volume)` | z > 3 |
| `PRICE_SPIKE` | `|return|` | z > 3 |
| `VOLATILITY_SPIKE` | `hv_5` | z > 2.5 |
| `NEWS_SPIKE` | 日新聞量 | z > 3 |
| `SENTIMENT_SPIKE` | 日均情緒變化 | z > 2.5 |
| `FLOW_ANOMALY` | 三大法人淨額 / 均量 | z > 3 |

> 用 `log(volume)` 而非 `volume`，因為成交量分布高度右偏，直接算 z-score 會過度敏感。

**Layer 2：多變量（Isolation Forest）**

用 (return, log_volume, hv, foreign_net_ratio, news_count) 五維特徵訓練 Isolation Forest，輸出 anomaly score。捕捉「單項都不極端但組合罕見」的情況。

**Layer 3：關係型**

| 類型 | 定義 |
|------|------|
| `CORRELATION_BREAK` | 個股與所屬產業指數的 20 日相關性，相對 250 日基準下降 > 2σ |
| `SECTOR_DIVERGENCE` | 個股報酬 − 產業中位數報酬 的 z 值 > 3 |
| `LEAD_LAG_BREAK` | 應該跟隨的美股標的大漲但本股未動（或反向） |

### 7.2 複合異常分數

```
composite = 100 × (1 - Π_j (1 - p_j))
```
其中 `p_j = min(1, |z_j| / 6)` 為各單項的標準化強度。多項同時異常時分數趨近 100。

### 7.3 輸出必須可解釋

```
🚨 ANOMALY  3017 奇鋐  2026-08-15  複合分數 96

  成交量   75,000,000 股   基準 18,200,000 ± 6,100,000 (60日)   z=+5.82  (+312%)
  報酬     +4.8%           基準 +0.1% ± 1.5%                    z=+3.14
  新聞量   21 則           基準 4.0 ± 3.2                       z=+5.25  (+425%)

  相關新聞：#8891 NVIDIA Blackwell 需求優於預期…（情緒 +0.72，影響 0.77）
  資料時間：2026-08-15 13:30 (TWSE) · 偵測器：zscore_v1 + iforest_v1
```

**每個異常都要顯示 baseline，否則「+312%」沒有意義。**

---

## 8. Sector Rotation

### 8.1 六個分項

| 分項 | 計算 |
|------|------|
| `momentum_score` | 產業指數（或成分股等權組合）的 20/60 日報酬百分位 |
| `breadth_score` | 成分股中站上 MA20 的比例、上漲家數比 |
| `volume_score` | 產業成交金額佔大盤比重的變化（資金流入） |
| `institutional_score` | 產業三大法人淨買超金額 / 產業總市值 |
| `news_momentum_score` | 產業相關新聞量 × 情緒 的 7 日變化 |
| `ai_exposure_score` | 產業內個股 `ai_supply_chain_centrality` 的市值加權平均 |

### 8.2 合成與狀態分類

```
strength = Σ w_i · z(component_i)   → 映射到 0–100
```

| 狀態 | 條件 |
|------|------|
| `TOP` | strength 排名前 20% 且 rank_change_5d ≥ 0 |
| `EMERGING` | strength 中段但 rank_change_5d ≥ +3（★ 最有價值的訊號） |
| `FALLING` | rank_change_5d ≤ -3 |
| `WEAK` | strength 排名後 20% |

### 8.3 輪動圖（RRG, Relative Rotation Graph）

X 軸 = 相對強度（vs TAIEX，正規化），Y 軸 = 相對強度動能。四象限：
```
Improving | Leading
----------+---------
Lagging   | Weakening
```
產業在四象限中的移動軌跡即為輪動路徑。前端用散點圖 + 尾跡呈現。

---

## 9. 效能預算

| 作業 | 資料規模 | 目標時間 | 策略 |
|------|---------|---------|------|
| 單日全市場技術指標 | 2,000 檔 × ~60 指標 | < 60s | polars group_by + 向量化 |
| 單日全市場因子 | 2,000 檔 × ~50 因子 | < 120s | 同上 + 快取中間結果 |
| 單日 AI Score | 2,000 檔 | < 60s | 讀已算好的指標/因子 |
| 10 年回測（週再平衡） | 2,000 檔 × 2,500 日 | < 120s | 預載入全部資料到記憶體再迴圈；避免逐日查 DB |
| 事件研究（50 事件 × 40 股） | | < 30s | 向量化 CAR 計算 |
| Isolation Forest 訓練 | 2,000 × 250 樣本 | < 30s | 每週重訓一次即可 |

**記憶體預算**：10 年全市場日線約 `2,000 × 2,500 × 8 欄 × 8 bytes ≈ 320 MB` → 完全可以全部載入記憶體做回測。這是選擇「單機 + polars」而非分散式的根本理由。

---

## 10. 測試規範

### 10.1 指標黃金測試

```python
@pytest.mark.parametrize("indicator,params", GOLDEN_CASES)
def test_indicator_matches_talib(indicator, params, sample_ohlcv):
    ours = getattr(indicators, indicator)(sample_ohlcv, **params)
    theirs = getattr(talib, indicator.upper())(sample_ohlcv.close, **params)
    assert_allclose(ours.drop_nulls(), theirs[~np.isnan(theirs)], rtol=1e-6)
```

### 10.2 Look-ahead 注入測試（★ 最重要的一條）

```python
def test_no_lookahead_in_factor(factor_spec):
    """把 as_of 之後的資料改成極端值，因子值必須完全不變。"""
    df = load_test_data()
    baseline = compute_factor(df, factor_spec, as_of=CUTOFF)

    poisoned = df.with_columns(
        pl.when(pl.col("trading_date") > CUTOFF)
          .then(pl.col("close") * 1000)
          .otherwise(pl.col("close")).alias("close")
    )
    result = compute_factor(poisoned, factor_spec, as_of=CUTOFF)
    assert_frame_equal(baseline, result)   # 任何差異都代表偷看了未來
```

這個測試對**每一個因子**參數化執行。這是整個系統最重要的一條測試。

### 10.3 回測正確性測試

| 測試 | 內容 |
|------|------|
| 手算對照 | 3 筆交易的完整損益手算，斷言引擎結果一致（含手續費、交易稅、滑價） |
| 買入持有 | 策略 = 全倉買入 2330 並持有，斷言結果 ≈ 還原股價報酬（誤差 < 成本） |
| 零成本 | 成本設為 0 時，`total_commission = total_tax = total_slippage = 0` |
| 除權息 | 跨除權息日的持倉，斷言現金股利入帳且報酬正確 |
| 下市股 | 股票池含下市股時，斷言在下市日平倉且損益計入 |
| 執行延遲 | `delay_days=1` 時，斷言沒有任何交易的成交日 = 訊號日 |

### 10.4 統計函式測試

事件研究的 t 統計量、CAR 計算，用已發表論文的公開範例資料驗證，或用蒙地卡羅模擬驗證（在無效應的隨機資料上，5% 顯著水準下的偽陽性率應接近 5%）。

---

## 11. Phase 3 Definition of Done

- [ ] 所有指標通過 TA-Lib 黃金測試
- [ ] 所有因子通過 look-ahead 注入測試
- [ ] 所有因子的 IC / IC-IR / 分層報酬報告產出，未達標者標記為 research-only
- [ ] 因子相關矩陣產出，高度相關（>0.8）的因子擇一保留
- [ ] 全市場單日指標 + 因子計算 < 180 秒
- [ ] `feature_versions` 表有第一版正式紀錄，spec 完整可重現
- [ ] 回測引擎通過六項正確性測試
- [ ] 「買入持有 2330」回測結果與實際還原報酬誤差 < 0.5%
