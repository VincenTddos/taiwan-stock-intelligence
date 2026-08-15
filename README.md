# twquant — AI Taiwan Stock Intelligence Platform

台股 AI 大數據量化研究、新聞情報與智慧分析平台。

> **目前狀態：Phase 1 — Foundation。**
> 這個部署**沒有任何市場資料**。沒有股價、沒有 AI Score、沒有新聞、沒有回測。
> Phase 1 的交付物是一個可測試、可觀測、可持續擴充的系統地基 ——
> 以及三條從第一天就用程式強制執行的規則（見下方 [核心規則](#核心規則)）。
>
> 完整架構設計見 [`docs/`](docs/)。開發路線圖見 [`docs/DEVELOPMENT_ROADMAP.md`](docs/DEVELOPMENT_ROADMAP.md)。

---

## Requirements

| 元件 | 版本 | 說明 |
|------|------|------|
| Docker + Docker Compose | 24+ / v2 | 唯一必要的執行環境 |
| Python | 3.11+ | 僅本機開發（不用 Docker 時）需要 |
| Node.js | 22+ | 同上 |
| pnpm | 9+ | 前端套件管理 |
| uv | 最新 | Python 套件管理（`pip install uv`） |
| PostgreSQL | 16 + TimescaleDB + pgvector | 由 compose 提供 |
| Redis | 7 | 由 compose 提供 |

硬體：4 GB RAM 可跑核心服務。啟用本地 LLM（`--profile llm`）另需 8–16 GB。

---

## Installation

```bash
git clone <repo-url> twquant
cd twquant

cp .env.example .env
# 編輯 .env，把所有 CHANGE-ME 換掉。至少要換這兩個：
#   POSTGRES_PASSWORD
#   JWT_SECRET        （≥32 字元；產生方式見下）
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

`.env` 已被 `.gitignore` 排除，且 CI 會用 gitleaks 掃描。
`APP_ENV=staging|production` 時，設定驗證會**拒絕啟動**任何仍是預設值的祕密 ——
忘記換值會在開機時大聲失敗，而不是安靜地帶著已知密碼上線。

---

## Environment Variables

完整清單與說明在 [`.env.example`](.env.example)。以下是會改變系統行為的幾個：

| 變數 | 預設 | 為什麼重要 |
|------|------|-----------|
| `APP_ENV` | `local` | `production` 會啟用一組額外的設定檢查（禁 DEBUG、禁 mock data、CORS 必須是 https、祕密不得為預設值） |
| `ALLOW_MOCK_DATA` | `true` | **production 必須為 false。** 這是「假資料絕不可被當成真實市場資料呈現」的執行點 |
| `ENABLE_LLM` | `false` | LLM 是可選的。關閉時，行情、量化、資料庫、API、回測**全部照常運作** |
| `REQUIRE_TIMESCALEDB` | `true` | 只有在本機用一般 Postgres 開發時才設 false；staging/production 不允許 |
| `JWT_SECRET` | placeholder | 少於 32 字元或仍是預設值時，staging/production 拒絕啟動 |
| `WORKER_CONCURRENCY` | `2` | Celery worker 併發數 |

---

## Docker Setup

```bash
make up          # postgres + redis + api + worker + beat + web
make seed        # 建立初始 admin 帳號（會印出密碼）
make verify      # 探測所有元件並印出健康摘要
```

| 服務 | 位址 |
|------|------|
| Web | http://localhost:3000 |
| API 文件 | http://localhost:8000/docs |
| OpenAPI | http://localhost:8000/api/v1/openapi.json |
| Health | http://localhost:8000/api/v1/health/full |

Profiles（低配機器可以只跑核心）：

```bash
docker compose --profile llm up -d              # + Ollama
docker compose --profile observability up -d    # + Flower (:5555)
docker compose --profile storage up -d          # + MinIO (:9000/:9001)
make up-all                                     # 全部
```

所有 port 都綁在 `127.0.0.1`，不對外網路開放。

---

## Development

不使用 Docker 跑應用層（資料庫仍建議用 compose）：

```bash
make setup                       # 建 venv、裝 node_modules、建立 .env
docker compose up -d postgres redis

make migrate                     # 套用 migration
make seed                        # 建立 admin

# 四個終端機（或用 make up）
make api                         # FastAPI，含 reload
make worker                      # Celery worker
make beat                        # Celery 排程
make web                         # Next.js dev server
```

### 驗證非同步鏈路是通的

Health dashboard 上的 **Run round trip** 按鈕（或 `POST /api/v1/health/worker/echo`）
會派送一個 `health_check_task` 走完整條路徑：

```
API  →  Redis (broker)  →  Celery worker  →  Task
                                              ↓
                          worker 自己連線 Postgres 與 Redis 並回報
```

這比單純 ping broker 更有意義：一個能連上 broker 但連不到資料庫的 worker，
在 ping 測試下會顯示為健康。

---

## Testing

```bash
make test              # 後端 + 前端
make test-backend      # pytest（需要 Postgres + Redis）
make test-frontend     # vitest
make coverage          # 覆蓋率報告，低於 80% 失敗
```

後端測試分層：

| 目錄 | 內容 | 需要 |
|------|------|------|
| `tests/unit/` | 設定驗證、資料契約、密碼與 JWT | 無 |
| `tests/integration/` | API、資料庫、Redis、契約端點 | Postgres + Redis |
| `tests/worker/` | Celery 設定與任務；`-m worker` 需要活的 worker | Redis（+ worker） |

沒有活的 worker 時，`-m worker` 的測試會**明確 skip 並說明原因**，
而不是安靜地通過 —— 綠燈必須代表真的驗過。

---

## Lint

```bash
make lint        # ruff check + ruff format --check + next lint
make format      # 自動修正
make typecheck   # mypy --strict + tsc --noEmit
```

Lint 不只是風格。`ruff` 設定中有一條**架構規則**：

```toml
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"app.api".msg = "Lower layers must not import from app.api"
```

`services/` 或 `repositories/` 反向 import `app.api` 會直接讓 lint 失敗。
模組邊界由工具維護，不靠自律。

---

## Build

```bash
make check       # 完整 Phase gate：lint + typecheck + tests + migration
docker compose build
```

`make check` 就是 CI 跑的東西。**任一項失敗就不得進入下一個 Phase。**

---

## Database Migration

```bash
make migrate                       # alembic upgrade head
make migrate-down                  # 回退一版
make migrate-check                 # up → down → up（CI 也跑這個）
make revision m="add stocks table" # 自動產生 migration
```

規則：

- Schema 變更**一律**透過 Alembic，禁止手改資料庫
- 每個 revision 都必須有可執行的 `downgrade()`
- CI 執行 `alembic check`，確保 migration 與 model 沒有漂移
- TimescaleDB 在 migration `0001` 中是**條件式**建立：有就建、沒有就記錄並繼續。
  它在 staging/production 的強制性由 `REQUIRE_TIMESCALEDB` 與 `/health/database` 負責

---

## 核心規則

這三條不是文件裡的約定，是程式碼裡的約束。

### 1. 假資料不可能被當成真實資料

```python
# app/schemas/contracts.py
@model_validator(mode="after")
def _mock_implies_demo(self) -> MarketQuote:
    if self.source is DataSource.MOCK and not self.is_demo:
        raise ValueError("MOCK-sourced data must set is_demo=True")
```

`is_demo` 從資料契約一路傳到 API 的 `meta` 再到前端的 `DataProvenance` 元件，
在 UI 上呈現為紅色 `DEMO DATA` 角標。
production 環境下 `ALLOW_MOCK_DATA=true` 會讓應用**拒絕啟動**。

### 2. 財報資料不可能造成 look-ahead bias

```python
class FinancialFact(BaseModel):
    period_end: date          # 事件時間
    announced_at: datetime    # ★ 知曉時間 —— 必填

    def is_known_at(self, as_of: date) -> bool:
        return self.announced_at.date() <= as_of
```

`announced_at` 是必填欄位：一筆不知道何時公開的財報**在型別上就無法存在**。
另有驗證器拒絕 `announced_at < period_end`。
`tests/unit/test_contracts.py::test_backtest_filter_excludes_future_information`
直接對比了正確做法與天真做法的差異。

### 3. 每個衍生數字都能追溯

```python
class Provenance(BaseModel):
    model_version: str | None
    dataset_version: str | None
    feature_version: str | None
    calculated_at: datetime      # 必填
    data_as_of: date             # 必填
```

`FactorScore` 與 `AIStockScore` 都無法在缺少 `Provenance` 的情況下建構。
`AIStockScore.contributions_balance()` 驗證
`baseline + Σ contributions == total_score` —— 「為什麼是 91 分」的答案永遠加得起來。

契約可透過 `GET /api/v1/meta/contracts` 取得 JSON Schema，
前端與未來的服務都以此為準，不各自定義。

---

## Architecture

```
backend/app/
├── api/            HTTP 層：路由、依賴、middleware
├── core/           設定、日誌、錯誤、安全、快取
├── db/             engine、session、declarative base
├── models/         SQLAlchemy ORM
├── schemas/        Pydantic：envelope、契約、請求/回應
├── services/       業務邏輯
├── repositories/   所有 SQL 都在這裡
└── workers/        Celery app 與任務

web/
├── app/            Next.js App Router
├── components/     UI 元件
├── lib/api/        型別化的 API client
└── stores/         Zustand
```

依賴方向永遠向下：`api → services → repositories → db`。

Celery 佇列拓撲（Phase 1 只用到 `q_maint`，其餘先宣告好）：

```
q_ingest   資料抓取（IO 密集）
q_compute  指標／因子／評分（CPU 密集）
q_nlp      LLM 推論（序列化，避免 GPU 爭用）
q_user     使用者觸發（回測等）
q_maint    備份／監控／健康檢查
```

完整設計：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
[`docs/ERD.md`](docs/ERD.md) · [`docs/API_SPEC.md`](docs/API_SPEC.md)

---

## API

| 端點 | 說明 |
|------|------|
| `POST /api/v1/auth/login` | 取得 access + refresh token |
| `POST /api/v1/auth/refresh` | 輪替 refresh token（舊的立即失效） |
| `POST /api/v1/auth/logout` | 撤銷 refresh token |
| `GET /api/v1/auth/me` | 目前使用者 |
| `GET /api/v1/health` | 存活 + 核心相依 |
| `GET /api/v1/health/database` | Postgres / TimescaleDB / pgvector 分別回報 |
| `GET /api/v1/health/redis` | |
| `GET /api/v1/health/worker` | Celery（heartbeat + control ping） |
| `GET /api/v1/health/full` | 全部元件 |
| `POST /api/v1/health/worker/echo` | 端到端鏈路測試（admin） |
| `GET /api/v1/meta/contracts` | 共享資料契約的 JSON Schema |
| `GET /api/v1/meta/capabilities` | 這個部署現在真正能做什麼 |

每個回應都是統一信封：

```json
{
  "data": { },
  "meta": {
    "data_timestamp": "...", "source": ["TWSE"],
    "model_version": null, "confidence": null,
    "is_demo": false, "is_stale": false,
    "cache": { "hit": false, "age_seconds": null },
    "request_id": "..."
  }
}
```

錯誤是 RFC 9457 Problem Details，並帶著同一個 `request_id`，
使用者回報的問題可以直接對到日誌行。

---

## Disclaimer

本系統為**研究工具**。所有輸出（包含未來 Phase 的模型分數、機率預測與回測結果）
均為模型推論，不構成投資建議，不代表未來績效。
系統不提供下單功能，也不會產生「保證獲利」「一定上漲」這類內容。

資料來源標示：臺灣證券交易所、證券櫃檯買賣中心、公開資訊觀測站
（依政府資料開放授權條款）。詳見 [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md)。
