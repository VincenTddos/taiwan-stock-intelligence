# PHASE_4_FOUNDATION.md — News Intelligence: architecture, contracts, schema

> 產出時間：2026-08-20 · Phase 4 的第一段（地基）
> 範圍：architecture / data contract / provider abstraction / schema / tests
> **不含**：NER、stock linking、sentiment、event classification、impact score、
> API、前端新聞頁。依指示，這一段完成後停下。

---

## 0. 一句話總結

Phase 4 的地基完成：**新聞來源必須先證明「可以讀」才能被讀**，
資料表沒有地方放全文，關聯沒有證據就存不進去，推論不能宣稱與發布者同等的確定性。

這一段沒有處理任何一則新聞。它建立的是**之後的管線做不到的事**。

---

## 1. 為什麼從 TWSE OpenAPI 開始，而不是從媒體 RSS

實測 TWSE OpenAPI 的 143 個端點，找到三個新聞類端點，採用其中兩個：

| 端點 | 內容 | 授權 | 爬蟲 |
|------|------|------|------|
| `/opendata/t187ap04_L` | 上市公司每日重大訊息 | 政府資料開放授權條款 v1 | 不涉及 |
| `/news/newsList` | 證交所新聞 | 同上 | 不涉及 |

**`t187ap04_L` 的價值遠超預期。** 它同時帶三樣東西：

```
發言日期 + 發言時間   →  精確的知曉時間（不是日級近似）
公司代號              →  來源自己的個股歸屬（不是字典比對推論）
事實發生日            →  事件實際發生的日期，經常早於發言日
```

三個不同的時鐘，而且都是真的。這使它成為目前唯一一個
**`DIRECT` 關聯屬於證據而非推論**的來源 —— 完全不需要 NER 就能建立第一條連結。

實測抓到三個怪癖（全是來源的）：主旨欄位名結尾有空格、`發言時間` 是不補零的
`HHMMSS`（`65903` 是 06:59:03）、`說明` 欄位放的是公告全文。

---

## 2. 建了什麼

```
app/providers/news.py          NewsProvider 抽象、RobotsPolicy、SourceType、
                               DEFAULT_CREDIBILITY、assert_permitted()
app/providers/twse_news.py     兩個 parser + 兩個 provider（走 replay transport）
app/models/news.py             6 張表
app/schemas/contracts.py       + NewsEntityMention / NewsStockLink / NewsEventTag /
                               EvidenceSnippet / ExtractionMethod / TimeHorizon
alembic/0008_news.py           migration
scripts/seed_news_sources.py   2 個已驗證啟用、3 個候選（未啟用）
tests/integration/test_news_foundation.py   38 個測試
```

### 資料表

| 表 | 用途 |
|---|---|
| `entity_aliases` | 名稱字典（由股票主檔自動建立；`is_ambiguous` 與 `approved` 是關鍵欄位） |
| `news_sources` | 發布者登錄，含**為什麼可以讀它** |
| `news` | 標題、摘要、連結 —— **沒有全文欄位** |
| `news_entities` | 文中提到的實體，含字元位置（evidence 錨點） |
| `news_stock_relations` | 「這則新聞關於這家公司」的主張，`evidence` NOT NULL |
| `news_events` | 事件分類，`event_time` 與 `published_at` 分開 |

`news_embeddings` **刻意還沒建** —— 向量維度取決於還沒做的模型選擇，
先猜一個 `vector(1024)` 只是製造一次要撤回的 migration。

---

## 3. 這一段強制的四件事

### 3.1 權限是正面事實（ADR-029）

一個新聞來源要被抓取，必須先記錄三件事：robots 政策、授權說明、以及**誰在什麼時候確認的**。
三處各自把關：

- **CHECK 約束** —— `is_active` 為 true 時三者缺一不可。這是 `UPDATE` 繞不過去的那道
- **registry** —— 拒絕未啟用的來源
- **provider 自己** —— 獨立於資料列再檢查一次

三者不是冗餘。列記錄的是「當時決定了什麼」，類別記錄的是「程式實際會去抓什麼」；
一個指向沒人檢查過的路徑的 provider，應該自己失敗，而不是繼承另一列的核准。

錯誤訊息本身也是設計的一部分：**「它沒有反對」不是許可**。
要防的不是有人決定忽略 robots.txt，是有人從來沒想到要去看。

### 3.2 沒有地方放全文

`news` 有 `title` 與 `summary`，沒有 `body`。§8.1 的儲存政策說全文只在管線中短暫使用、
不長期保存 —— 有 `body` 欄位的話，這句話就變成「靠自律」，而自律是週二凌晨兩點會失效的東西。

測試直接斷言 `body` / `content` / `full_text` / `article_text` / `html` 都不存在於表上。

### 3.3 沒有證據就不是關聯

`news_stock_relations.evidence` 是 NOT NULL 且不能是空物件。
一個無法被檢視的關聯，是一個沒有人能反對的關聯 —— 而沒有人能反對的推論，
正是供應鏈第二跳變成事實的方式。

同一張表也禁止 `DIRECT` 帶非零跳數：把傳播來的關聯存成 `DIRECT`，
看起來與公司被直接點名完全一樣，而兩者的差別正是折價的全部依據。

### 3.4 推論不能宣稱與發布者同等的確定性

`news_entities` 的 CHECK 約束：`confidence = 1` 只有 `SOURCE_ATTRIBUTED` 可以用。
字典命中「台積」很強，但仍然是推論 —— 這個字串出現在別的公司名稱裡，也出現在價格旁邊。
確定性 1.0 的推論，是下游任何東西都無法折價的數字。

Pydantic 契約與資料庫約束**各擋一次**：契約擋管線寫入，約束擋其他所有東西 ——
fixture、migration、事故現場手打的 INSERT。

---

## 4. 因子的 input dependency 與 research/production 狀態

依指示，News 因子必須明確標示依賴與狀態。已完成：

**每個不可算的因子現在說出誰欠它的輸入**（`FactorSpec.provided_by`）：

```
 6  Phase 2 financial statement ingestion (t187ap06/t187ap07), not yet built
11  Phase 2 financial statement ingestion, not yet built
 7  Phase 2 monthly revenue ingestion (t187ap05_P), not yet built
 4  Phase 4 news ingestion and event classification
 3  Phase 4 news sentiment analysis
 5  Phase 7 supply chain graph (ai_beta needs Phase 5's AI basket)
 1  a free-float source; TWSE publishes shares in issue, not float
```

「缺 `news_sentiment_7d`」告訴讀者少了什麼；「Phase 4 news sentiment analysis」
告訴他什麼時候會有 —— 後者才是實際會被問的問題。

**production/research 狀態現在是明講的**，不是讓呼叫端自己從 verdict 推導：
`Verdict.production_eligible` 只有 `PASS` 為真，API 直接回傳這個欄位，
前端 `/factors` 顯示 `PRODUCTION` / `RESEARCH` 並在展開時說明為什麼不合格。

會自己猜的呼叫端會往寬鬆的方向猜，而寬鬆的那個猜測正是把未驗證因子放進分數的那個。

**Phase 3 的門檻沒有任何放寬。** coverage 70%、|IC| 0.02、|Rank IC| 0.02、
IC-IR 0.3、|t(Q5−Q1)| 2.0、stability 0.5、相關性 0.8 全部不變。
目前仍然是 0 個因子達標，這個數字不會因為 Phase 4 而變好看。

---

## 5. 測試

38 個測試，分五組：

| 組 | 內容 |
|---|---|
| 權限 | 資料庫拒絕未驗證即啟用、拒絕沒有授權說明、registry 拒絕未啟用來源、provider 自己拒絕自己、可信度預設依發布者類型 |
| Parser | 發言時間存活成 instant（06:59:03 而非 65903 點）、事件時間與發言時間分開且確實較早、公司自我歸屬、主旨欄位的尾隨空格、只保留摘要、payload 變形要報錯、產生的 id 穩定 |
| Transport | replay 不觸網、`since` 是 advisory 且套用在已取得的資料上、兩個 provider 各自宣告能看到什麼、production 下拒絕 replay |
| 契約 | 推論不得確定、推論必須帶證據、關聯不得無證據、DIRECT ⇄ 零跳數雙向、傳播必須記錄路徑、offset 必須有序 |
| Schema | 沒有 body 欄位、發布時間不得晚於取得時間、空證據存不進去、DIRECT 不得帶跳數、資料庫也擋確定性推論、模糊別名保留並標記、模型發現的別名等人核准 |

全套：**506 passed，覆蓋率 87.4%**。

---

## 6. 刻意還沒做的

| 項目 | 為什麼還沒做 |
|------|-------------|
| NewsFetcher 的 ETag / Last-Modified | provider 抽象已預留，但要有第二次抓取才有意義 |
| 去重（URL / SimHash / embedding） | 三層中的第三層需要 embedding 模型決定 |
| entity_aliases 自動建立 + Aho-Corasick | 表已建、規則已定義，建立 job 屬於下一段 |
| LLM provider / 實體補充 / sentiment | 下一段。`ENABLE_LLM=false` 時必須跑完的規則路徑也在下一段 |
| stock linking / impact score / event classification | 下一段 |
| news API 與前端新聞頁 | 下一段 |
| `news_embeddings` + HNSW | 等模型選擇 |

---

## 7. 停止點

依指示，這一段到此結束。

下一段（Phase 4 第二段）建議順序：

1. `entity_aliases` 自動建立 job（由已有的股票主檔，不需要新的外部資料）
2. news ingestion job（沿用 Phase 2 的 pipeline：provenance → 驗證 → 隔離 → 冪等 upsert）
3. 規則路徑的 linking / sentiment / event classification（`ENABLE_LLM=false` 必須跑完）
4. LLM 補充層 + prompt injection 測試
5. reaction backfill（需要價格歷史 —— 與 L1 相關，但只需要有新聞的那些個股）

**與 L1 的關係**：新聞取得與處理完全不依賴價格歷史。
只有 §3.5 的 reaction 閉環（T+1/3/5/10/20 異常報酬）需要價格，
而且只需要**有新聞的那幾檔**的歷史，不是全市場十年。
