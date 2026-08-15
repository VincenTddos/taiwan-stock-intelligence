# twquant — AI Taiwan Stock Intelligence Platform

**Phase 0 設計文件包** · 2026-08-15

台股 AI 大數據量化研究、新聞情報與智慧分析平台的完整架構設計。
本階段**只有文件，沒有程式碼** —— 這是刻意的，符合「完成 Architecture Audit 之前不大量修改程式碼」的開發原則。

---

## 閱讀順序

| # | 文件 | 讀它來回答什麼 | 篇幅 |
|---|------|--------------|------|
| 1 | [`REPO_AUDIT.md`](REPO_AUDIT.md) | 現在有什麼？要建什麼？風險在哪？ | 251 行 |
| 2 | [`ARCHITECTURE.md`](ARCHITECTURE.md) | 系統長什麼樣？為什麼這樣選？ | 1,078 行 |
| 3 | [`DATA_SOURCES.md`](DATA_SOURCES.md) | 資料從哪來？哪些已驗證？ | 502 行 |
| 4 | [`ERD.md`](ERD.md) | 資料怎麼存？67 張表的完整設計 | 1,470 行 |
| 5 | [`API_SPEC.md`](API_SPEC.md) | 80 個端點的契約 | 853 行 |
| 6 | [`QUANT_ENGINE.md`](QUANT_ENGINE.md) | 指標、因子、回測怎麼算才不會錯 | 711 行 |
| 7 | [`AI_ENGINE.md`](AI_ENGINE.md) | AI Score、新聞、ML、RAG、Copilot | 861 行 |
| 8 | [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md) | Phase 0–10 的任務與驗收條件 | 529 行 |

**趕時間的話**：讀 `REPO_AUDIT.md` §6（可預見的問題）+ `ARCHITECTURE.md` §17（十個架構決策）+ `DEVELOPMENT_ROADMAP.md` §0（路線圖總覽）。

---

## 四項已確認的專案約束

| 約束 | 決策 |
|------|------|
| Repository | Greenfield，無既有程式碼 |
| 資料來源 | 官方免費公開來源（TWSE / TPEx / TAIFEX / MOPS） |
| 運行規模 | 個人自用，單機 Docker Compose |
| LLM | 本地開源模型（Ollama / vLLM），可關閉 |

---

## 這套設計的三個核心主張

### 1. Bitemporal 是避免 look-ahead bias 的唯一可靠方法

財報的「所屬期別」和「公布時間」差 45 天以上。所有基本面表同時存 `period_end`（事件時間）與 `announced_at`（知曉時間），回測一律以 `announced_at <= as_of` 過濾。這條規則有 CI 靜態檢查與注入式測試把關。

### 2. LLM 不參與任何數值計算

LLM 負責理解新聞、翻譯結果成自然語言。所有進入資料庫的數字由確定性程式碼或已註冊的 ML 模型產生。這是可解釋性與可重現性的前提，也讓系統在 LLM 關閉時仍保有 80% 功能。

### 3. 每個數字都能追溯

任何 AI Score 都能回答「哪一版模型、哪天的資料、哪些特徵、哪些權重」。`SUM(contributions) + baseline = total_score` 是資料庫層的不變式。

---

## 已實測驗證的資料來源（2026-08-15）

| 端點 | 狀態 |
|------|------|
| `openapi.twse.com.tw/v1/exchangeReport/MI_INDEX` | ✅ 各類指數收盤 |
| `openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL` | ✅ 全市場最新交易日 OHLCV |
| `openapi.twse.com.tw/v1/opendata/t187ap03_L` | ✅ 上市公司基本資料 33 欄 |
| `www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY` | ✅ **可指定日期**的歷史日線 |
| `www.twse.com.tw/rwd/zh/fund/T86` | ✅ **可指定日期**的全市場三大法人 19 欄 |
| `openapi.twse.com.tw/v1/fund/T86`、`/exchangeReport/BFI82U` | ❌ 404，網路謠傳路徑，禁用 |

完整清冊與驗證狀態見 `DATA_SOURCES.md`。

---

## 下一步

Phase 0 已完成，等待確認後進入 Phase 1（Foundation）。
需要決定的三件事列於 `DEVELOPMENT_ROADMAP.md` 文末。
