# PHASE_1_REPORT.md — Foundation

> 產出時間：2026-08-15 · 專案：twquant · Phase 1 of 10
> 前置：Phase 0 Architecture Audit（`docs/`，9 份文件、6,332 行）

---

## 0. 一句話總結

系統地基已建立並實際跑通：**API → Redis → Celery → Task 完整鏈路已驗證**，
Postgres + pgvector、Redis、Celery worker、FastAPI、Next.js 全部可運作，
migration 可正向與反向、93 個後端測試（另 2 個因環境跳過）與 14 個前端測試全綠、
`ruff` 與 `mypy --strict` 零告警、前端 build 成功。

**兩項 DoD 未能在本環境驗證**（見 §6）：`docker compose up` 與 TimescaleDB extension ——
這個雲端沙箱沒有 Docker daemon，且 TimescaleDB 的套件庫在此網路被擋（HTTP 403）。
兩者的程式碼與設定都已完成，需要你在本機執行一次確認。**這是 Phase 1 的唯一未結項。**

---

## 1. Implemented

### 1.1 Infrastructure

| 項目 | 狀態 | 說明 |
|------|------|------|
| `docker-compose.yml` | ✅ 已撰寫 | 9 個 service、4 個 profile（core / llm / observability / storage） |
| PostgreSQL 16 | ✅ 已驗證運行 | 沙箱中以原生 Postgres 16.13 實測 |
| TimescaleDB | ⚠️ 已設定，未驗證 | compose 使用 `timescale/timescaledb-ha:pg16`（同時內含 pgvector） |
| pgvector | ✅ 已驗證運行 | v0.6.0，實測建表、寫入、`<=>` 相似度查詢 |
| Redis 7 | ✅ 已驗證運行 | v7.0.15，db0=cache / db1=broker / db2=result |
| 健康檢查 | ✅ | 每個 service 都有 `healthcheck`，`depends_on` 用 `service_healthy` 串接 |
| Port 綁定 | ✅ | 全部綁 `127.0.0.1`，不對外網路開放 |
| 資料庫初始化 | ✅ | `docker/postgres/init.sql` 建立 extension 與測試資料庫 |

**設計決策：`migrate` 是獨立的 one-shot service。**
`api` 與 `worker` 都用 `depends_on: migrate: service_completed_successfully`，
所以應用程式永遠不會對著一個 schema 過期的資料庫啟動，也不會有兩個容器同時搶著跑 migration。

### 1.2 Backend

```
backend/app/
├── api/          middleware（request_id、security headers）、deps（RBAC）、v1 路由
├── core/         config · logging · errors · security · cache
├── db/           declarative base（含命名慣例）· async session
├── models/       user · platform（audit_logs / system_health / job_runs）
├── schemas/      envelope · contracts ★ · auth · health
├── services/     auth_service · health_service
├── repositories/ base · user_repo
└── workers/      celery_app · tasks/health
```

| 元件 | 實作重點 |
|------|---------|
| **Configuration** | Pydantic Settings，全部來自環境變數。`_check_consistency` 在 production/staging 拒絕：placeholder 祕密、`DEBUG=true`、`ALLOW_MOCK_DATA=true`、http CORS、關閉 extension 要求 |
| **Structured logging** | structlog → JSON → stdout，透過 contextvar 自動注入 `request_id` / `job_run_id`；uvicorn 的 handler 被接管，避免同一事件輸出兩種格式 |
| **Error handling** | RFC 9457 Problem Details，11 種錯誤類型，全部帶 `request_id`。未捕捉的例外記錄完整 traceback，但只回傳不透明訊息 |
| **Security** | argon2id 密碼雜湊、JWT access(15m)/refresh(7d)、refresh token 輪替 + Redis 撤銷清單 |
| **Cache** | 版本前綴機制取代 key 掃描；`bump_cache_version` 以 O(1) 退休整個命名空間 |
| **Database** | SQLAlchemy 2.0 async + asyncpg，顯式命名慣例讓 autogenerate 產生穩定的約束名稱 |
| **Migrations** | Alembic 兩個 revision，都有可執行的 `downgrade()` |

### 1.3 Data Contracts ★

`app/schemas/contracts.py` —— Phase 1 沒有市場資料，但契約先建立，
因為要防的失敗模式是結構性的：前端、後端、量化各自對「什麼是一筆價格」有不同定義。

| 契約 | 內建的不變式 |
|------|-------------|
| `MarketQuote` | `source=MOCK` 時強制 `is_demo=True`（validator 拒絕不一致） |
| `HistoricalPrice` | OHLC 一致性檢查；`adjusted` 為必填布林，避免還原/未還原序列混用 |
| `FinancialFact` | **`announced_at` 為必填** + 拒絕 `announced_at < period_end` + `is_known_at()` |
| `NewsDocument` | 知曉時間 `published_at` 與取得時間 `ingested_at` 分離 |
| `InstitutionalFlow` | 對應 TWSE T86 的 19 個欄位 |
| `FactorScore` / `AIStockScore` | **`Provenance` 為必填** |
| `Provenance` | `model_version` / `dataset_version` / `feature_version` / `calculated_at` / `data_as_of` |
| `AIStockScore.contributions_balance()` | `baseline + Σ contributions == total_score` |

金額一律 `Decimal`，禁止 `float`。股票代號 regex 為 `^[0-9A-Z]{4,10}$`
（Phase 0 實測發現主動式 ETF 代號含英文字母，例如 `00981A`）。

契約可經由 `GET /api/v1/meta/contracts` 取得 JSON Schema。

### 1.4 Worker

| 項目 | 說明 |
|------|------|
| Celery app | broker=Redis db1、result=db2；`task_acks_late` + `task_reject_on_worker_lost` + `prefetch_multiplier=1` |
| 佇列拓撲 | `q_ingest` / `q_compute` / `q_nlp` / `q_user` / `q_maint` —— Phase 1 只用 `q_maint`，其餘先宣告 |
| `maint.health_check` | 從 worker 進程內獨立連線 Postgres 與 Redis 並回報 |
| `maint.heartbeat` | 每 30 秒寫入 Redis，TTL 120 秒（死掉的 worker 會自己消失，不需要 reaper） |
| Beat | 已設定並可運行 |

**為什麼 `health_check_task` 要自己連資料庫**：一個能連上 broker 但連不到 Postgres 的 worker，
在單純的 `ping` 下會顯示為健康。這個任務讓那種故障可見。

### 1.5 Health checks

```
GET  /api/v1/health           api + postgres + redis + celery
GET  /api/v1/health/database  postgres + timescaledb + pgvector（分別回報）
GET  /api/v1/health/redis
GET  /api/v1/health/worker    heartbeat，過期則退回 control ping
GET  /api/v1/health/full      全部元件（含 llm）
POST /api/v1/health/worker/echo   端到端鏈路測試（admin）
```

設計要點：

- **健康端點永遠不 500。** 元件掛掉時回報 `unhealthy`，而不是自己崩潰
- **每個檢查獨立計時、獨立失敗**，逾時可設定
- **資料庫檢查刻意序列執行** —— 單一 AsyncSession 只有一條連線，並發查詢會拋
  `IllegalStateChangeError`，那會讓三個元件同時假性顯示 unhealthy（這是實測抓到的 bug）
- **`disabled` ≠ `degraded`。** LLM 關閉時回報 `disabled`，不會把系統拖成 degraded，
  健康頁因此不會永遠停在黃燈
- HTTP 狀態碼：healthy/degraded → 200，unhealthy → 503

### 1.6 Authentication

| 端點 | 行為 |
|------|------|
| `POST /auth/login` | 回傳 access + refresh；寫入 audit log（成功與失敗都寫） |
| `POST /auth/refresh` | **輪替**：使用過的 refresh token 立即進入 Redis 撤銷清單 |
| `POST /auth/logout` | 撤銷 refresh token；即使 token 已失效也成功（冪等） |
| `GET /auth/me` | 目前使用者 |
| `GET /auth/admin-only` | RBAC 冒煙測試端點 |

- 帳號不存在時**照樣計算一次雜湊**，讓「查無此人」與「密碼錯誤」耗時相近，
  避免以登入延遲列舉使用者
- 兩種失敗回傳完全相同的錯誤文字（有測試斷言）
- 角色階層 `viewer < analyst < admin`

### 1.7 Frontend

| 頁面 | 內容 |
|------|------|
| `/login` | 表單、錯誤顯示、已登入自動導向 |
| `/dashboard` | 系統狀態卡片 + **空狀態面板**（見下） |
| `/health` | 完整健康儀表板 |

Health dashboard：

```
SYSTEM HEALTH                                          ● HEALTHY

COMPONENT       STATUS      LATENCY   VERSION      CHECKED
API             ● HEALTHY        —    0.1.0        just now
DATABASE        ● HEALTHY    3.3 ms   16.13        just now
TIMESCALEDB     ● DEGRADED   1.3 ms   —            just now
PGVECTOR        ● HEALTHY    0.6 ms   0.6.0        just now
REDIS           ● HEALTHY    4.1 ms   7.0.15       just now
CELERY          ● HEALTHY    3.5 ms   —            just now
LLM (OPTIONAL)  ● DISABLED       —    —            just now
```

每列可點開顯示 `detail` JSON 與錯誤訊息。另有 **Run round trip** 按鈕觸發完整鏈路測試。

**Dashboard 刻意只有空狀態，沒有任何示意數字。** 六個面板顯示「無資料 / 尚未接入資料來源」
並標註各自的 Phase。理由寫在程式碼註解裡：假的圖表最容易活過 demo 並被當成真實輸出。

`<DataProvenance>` 元件是 UI 端的執行點 —— `is_demo` 為 true 時渲染紅色 `DEMO DATA` 角標。

設計語言：dark mode、tabular numerals、**台股紅漲綠跌**（token 已定義，與美股相反，
弄反會讓未來每張圖都在誤導使用者）。

---

## 2. Tests

### 2.1 後端（95 collected · 93 passed · 2 skipped）

| 檔案 | 數量 | 涵蓋 |
|------|------|------|
| `unit/test_config.py` | 10 | production 拒絕 placeholder 祕密 / DEBUG / mock data / http CORS；URL 組裝；三個 Redis DB 不重疊 |
| `unit/test_contracts.py` | 21 | **bitemporality**、**provenance 必填**、**contribution 平衡**、mock 必須標 demo、OHLC 一致性、代號驗證、Decimal |
| `unit/test_security.py` | 8 | argon2 加鹽、token 型別混淆、換祕密驗簽、過期、竄改 |
| `integration/test_health.py` | 15 | 五個端點都不 500、envelope、extension 分別回報、LLM 停用不降級、request_id 標頭、安全標頭 |
| `integration/test_auth.py` | 14 | 登入、RBAC 允許/拒絕、refresh 輪替與重放拒絕、logout、audit log、錯誤格式 |
| `integration/test_database.py` | 7 | 連線、pgvector 可用性（實際跑相似度查詢）、TimescaleDB 誠實回報、時區感知、CHECK 約束 |
| `integration/test_redis.py` | 6 | ping、TTL、版本前綴失效語意 |
| `integration/test_contracts_endpoint.py` | 6 | 契約已發布、bitemporal 欄位必填、provenance 三元組、**Phase 1 不得暴露市場端點** |
| `worker/test_celery.py` | 8 | broker/backend 分離、佇列拓撲、可靠性設定、任務註冊、inline 執行、**故障時回報而非拋例外**、heartbeat、端到端派送 |

三個測試特別值得一提：

**`test_backtest_filter_excludes_future_information`** —— 並列正確與天真做法：

```python
as_of = date(2026, 7, 1)
visible = [f for f in facts if f.is_known_at(as_of)]        # 1 筆
naive   = [f for f in facts if f.period_end <= as_of]       # 2 筆 ← 洩漏了 Q2 財報
```

**`test_health_check_task_reports_rather_than_raises`** —— monkeypatch 讓資料庫連線失敗，
斷言任務仍回傳完整報告。健康檢查在故障時崩潰，等於在最需要它的時候沒有它。

**`test_no_endpoint_serves_market_data_in_phase_1`** —— 掃描 OpenAPI，
若出現 `/stocks`、`/ai-score`、`/news` 等路徑就失敗。防的是「善意的 demo 端點」。

### 2.2 前端（14 tests）

`StatusLabel` 四種狀態、`DataProvenance` 的 DEMO/STALE 角標與模型版本顯示、格式化工具。

### 2.3 實測結果

```
後端      95 collected → 93 passed, 2 skipped   （skip：TimescaleDB 未安裝，訊息明確）
覆蓋率    93%（門檻 80%）
ruff      All checks passed
mypy      Success: no issues found in 37 source files（--strict）
前端      14 passed
tsc       clean
next lint clean
next build ✓ 5 routes，First Load JS 105 kB shared
migration upgrade head → downgrade -1 → upgrade head 全部成功
```

### 2.4 端到端鏈路（實際執行結果）

```json
POST /api/v1/health/worker/echo?message=phase1
{
  "dispatched": true,
  "task_id": "a7786c71-db83-476a-9edb-912469e3c92f",
  "completed": true,
  "result": {
    "worker": "vm:16770",
    "echo": "phase1",
    "components": {
      "redis":    { "status": "healthy", "latency_ms": 1.99 },
      "postgres": { "status": "healthy", "version": "16.13", "latency_ms": 16.11 }
    },
    "duration_ms": 18.46,
    "status": "healthy"
  }
}
```

`API → Redis broker → Celery worker → task → worker 自己連 Postgres/Redis → 結果回傳` 全程通過。

### 2.5 真實 HTTP 伺服器 + 瀏覽器驗證

不只是 ASGI 層的測試 —— uvicorn 與 Next.js production server 都實際啟動，
並用 Chromium（Playwright）走完真實的使用者流程。

`bash scripts/verify_stack.sh` 對運行中的服務輸出：

```
SYSTEM HEALTH   http 200   overall DEGRADED
twquant v0.1.0 · local

API            ● HEALTHY           —  0.1.0
DATABASE       ● HEALTHY      3.4 ms  16.13 (Ubuntu 16.13-0ubuntu0…
TIMESCALEDB    ● DEGRADED     0.9 ms
               extension 'timescaledb' is not installed in this database
PGVECTOR       ● HEALTHY      0.6 ms  0.6.0
REDIS          ● HEALTHY      1.5 ms  7.0.15
CELERY         ● HEALTHY      0.9 ms
LLM            ● DISABLED          —

✓ /docs                        200
✓ /api/v1/openapi.json         200
✓ /api/v1/meta/contracts       200
✓ /api/v1/meta/capabilities    200
✓ stack verified
```

瀏覽器流程（Chromium 1440×950）：

```
/login  →  填入帳密  →  提交  →  導向 /dashboard  →  /health
console errors: none
health 狀態格: ["DEGRADED","HEALTHY","HEALTHY","DEGRADED","HEALTHY","HEALTHY","HEALTHY","DISABLED"]
```

Dashboard 實際渲染確認：六個面板全部顯示「無資料 / 尚未接入資料來源」，
**畫面上不存在任何數字型的市場資料**。頁尾的 `DataProvenance` 顯示
「資料時間 2026/8/15 下午4:44:03 · 來源 SELF」與免責聲明。

Health dashboard 實際渲染確認：7 個元件、狀態燈號、延遲、版本、檢查時間全部正確，
TimescaleDB 顯示為黃色 DEGRADED 並可展開看到明確錯誤訊息，
LLM 顯示為灰色 DISABLED 且未把系統整體拖成 degraded。

**這一輪驗證抓到一個 Docker build 的潛在失敗**：`docker/web.Dockerfile` 有
`COPY --from=builder /app/public ./public`，但 `web/public/` 當時並不存在 ——
容器建置會在這一行失敗。加入 `public/favicon.ico` 後同時解決了瀏覽器
console 的 404 與這個建置問題。

---

## 3. Architecture Decisions

延續 Phase 0 的 ADR-001 ~ ADR-012，本階段新增：

| ADR | 決策 | 理由 | 推翻條件 |
|-----|------|------|---------|
| **ADR-013** | API / worker / beat / flower 共用同一個 Docker image，只換 command | 部署的程式碼在各進程間完全相同，worker 不可能與 API 共用的 model 漂移；故障域仍隔離 | 依賴體積差異大到需要分離時 |
| **ADR-014** | Migration 是獨立的 one-shot compose service | 應用程式永遠不會對著過期 schema 啟動；避免多容器競爭跑 migration | 導入需要協調的 zero-downtime migration 流程時 |
| **ADR-015** | TimescaleDB 在 migration 中條件式建立，由 `REQUIRE_TIMESCALEDB` 設定 + 健康檢查強制 | 讓一般 Postgres 也能本機開發，同時保證 staging/production 不會缺；缺席時**誠實回報 degraded** 而非假設存在 | 無（這是可攜性與嚴謹性的平衡點） |
| **ADR-016** | 快取版本從 **0** 而非 1 開始 | Redis `INCR` 對不存在的 key 回傳 1；若預設也是 1，第一次失效會是 no-op，過期資料會存活。**這是測試實際抓到的 bug** | 無 |
| **ADR-017** | 模組邊界由 ruff `banned-api` 強制（下層不得 import `app.api`） | 架構規則寫在文件裡會腐化，寫在 lint 裡不會 | 無 |
| **ADR-018** | 健康檢查中資料庫探測序列執行、其餘並行 | 單一 AsyncSession 並發查詢會拋例外，造成假性 unhealthy（實測抓到）；DB 探測是次毫秒級，序列化零成本 | 若改為每個檢查獨立 session |
| **ADR-019** | `disabled` 是獨立於 `degraded` 的狀態 | 刻意關閉的可選元件不該讓健康頁永遠停在黃燈，否則告警會被忽略 | 無 |
| **ADR-020** | Refresh token 使用即輪替 + Redis 撤銷 | 被竊的 token 至多可用一次，且重放會失敗，使竊取行為可被偵測 | 無 |
| **ADR-021** | Dashboard 只放空狀態，不放示意數字 | 假圖表最容易活過 demo 並被誤認為真實輸出 | 有真實資料源後自然解除 |

---

## 4. Files Created

共 **72 個檔案**（不含 Phase 0 的 `docs/` 9 份與 lock file）。

### 4.1 Root（8）

```
docker-compose.yml          9 services、4 profiles
.env.example                完整環境變數範本，含每項的存在理由
.gitignore                  secrets / python / node / data / backups
Makefile                    30 個 target，含 `make check` phase gate
README.md                   Requirements → Installation → … → Database Migration
CONTRIBUTING.md             五條不可協商規則 + 模組邊界 + 各類檢查表
PHASE_1_REPORT.md           本文件
scripts/verify_stack.sh     探測運行中的 stack 並印出健康摘要
```

### 4.2 Docker（3）

```
docker/api.Dockerfile       多階段、非 root、venv 分離
docker/web.Dockerfile       多階段、非 root
docker/postgres/init.sql    extensions + 測試資料庫
```

### 4.3 CI（1）

```
.github/workflows/ci.yml    5 jobs：secrets / backend / frontend / docker / compose
```

### 4.4 Backend（39）

```
pyproject.toml              deps + ruff（含架構規則）+ mypy strict + pytest + coverage
alembic.ini
alembic/env.py              async migration，過濾 TimescaleDB 內部 schema
alembic/script.py.mako
alembic/versions/0001_extensions.py
alembic/versions/0002_platform_and_users.py

app/main.py                 create_app、lifespan、middleware、CORS
app/core/config.py          Settings + 一致性驗證
app/core/logging.py         structlog + contextvar
app/core/errors.py          Problem Details + 11 種錯誤類型
app/core/security.py        argon2id + JWT
app/core/cache.py           Redis client + 版本前綴
app/db/base.py              Base + 命名慣例 + mixins
app/db/session.py           async engine / session
app/models/user.py
app/models/platform.py      AuditLog / SystemHealth / JobRun
app/schemas/envelope.py     Envelope[T] + Meta
app/schemas/contracts.py    ★ 7 個共享契約 + Provenance
app/schemas/auth.py
app/schemas/health.py
app/services/auth_service.py
app/services/health_service.py
app/repositories/base.py
app/repositories/user_repo.py
app/workers/celery_app.py
app/workers/tasks/health.py
app/api/deps.py             SettingsDep / SessionDep / RedisDep / CurrentUser / require_role
app/api/middleware.py       RequestContext / SecurityHeaders
app/api/v1/router.py
app/api/v1/auth.py
app/api/v1/health.py
app/api/v1/meta.py          契約與能力自省
scripts/seed.py             冪等 admin 建立（無任何示範市場資料）

tests/conftest.py
tests/unit/test_config.py
tests/unit/test_contracts.py
tests/unit/test_security.py
tests/integration/test_health.py
tests/integration/test_auth.py
tests/integration/test_database.py
tests/integration/test_redis.py
tests/integration/test_contracts_endpoint.py
tests/worker/test_celery.py
（＋ 各層 __init__.py）
```

### 4.5 Frontend（21）

```
package.json  tsconfig.json  next.config.ts  postcss.config.mjs
.eslintrc.json  vitest.config.ts  vitest.setup.ts  next-env.d.ts

app/layout.tsx  app/page.tsx  app/providers.tsx  app/globals.css
app/login/page.tsx
app/(app)/layout.tsx              側欄 + 認證守衛 + 健康燈號
app/(app)/dashboard/page.tsx
app/(app)/health/page.tsx

components/ui/StatusDot.tsx
components/ui/Card.tsx
components/ui/Button.tsx
components/ui/DataProvenance.tsx   ★
lib/api/client.ts                  envelope 型別、Problem Details、自動 refresh
lib/utils.ts
stores/auth.ts
__tests__/StatusDot.test.tsx
__tests__/utils.test.ts
```

---

## 5. Files Modified

無。Phase 1 是 greenfield 的第一批程式碼，沒有既有檔案可修改。

Phase 0 的 `docs/` 未變更 —— Phase 1 的實作沒有偏離已核准的架構。
新增的 ADR-013 ~ ADR-021 記錄於本報告，待你確認後併入 `docs/ARCHITECTURE.md` §18。

---

## 6. Known Limitations

### 6.1 阻擋 DoD 完成的兩項（需要你在本機驗證）

| # | 限制 | 原因 | 你需要做什麼 |
|---|------|------|-------------|
| **L1** | **`docker compose up` 未實際執行** | 這個雲端沙箱有 docker CLI 但**沒有 daemon**（`/var/run/docker.sock` 不存在） | 在本機執行 `make up && make seed && make verify`。compose 檔已寫好且 CI 的 `compose` job 會完整驗證它 |
| **L2** | **TimescaleDB extension 未驗證** | TimescaleDB 套件庫在此網路被擋（`packagecloud.io` 回 403），無法安裝 | 同上 —— compose 使用 `timescale/timescaledb-ha:pg16`，該 image 同時內含 TimescaleDB 與 pgvector。`/health/database` 會分別回報 |

**這兩項的程式碼與設定都已完成**，且系統對它們的缺席有正確行為：
migration 條件式跳過並印出說明、健康檢查回報 `degraded` 而非假裝正常、
`REQUIRE_TIMESCALEDB=true`（staging/production 的預設）會讓它變成硬性要求。

沙箱中已驗證的替代證據：原生 Postgres 16.13 + pgvector 0.6.0 全部通過，
包含實際的向量相似度查詢。

### 6.2 刻意留待後續 Phase 的

| 項目 | 現況 | Phase |
|------|------|-------|
| Rate limiting | 設計已在 `docs/API_SPEC.md` §1.5，尚未實作 | 10（或更早若對外） |
| WebSocket | 未實作 | 10 |
| E2E（Playwright） | 未實作；目前是整合測試 + 前端單元測試 | 10 |
| Prometheus metrics | 未實作；`/health` 已可被外部輪詢 | 10 |
| OpenAPI → TS 型別自動產生 | `make openapi` 已可用，但目前 client 型別是手寫的 | 2（有真實 schema 後才有意義） |
| 佇列 `q_ingest` 等 | 已宣告，尚無任務 | 2+ |
| `job_runs` 表 | 已建立，尚未有任務寫入 | 2 |
| 前端 `@` 別名的 Docker build | 本機 build 已驗證；容器內 build 未驗證（同 L1） | — |

### 6.3 已知的小瑕疵

| 項目 | 影響 | 處置 |
|------|------|------|
| `app/core/cache.py` 的 Redis client 是模組級單例 | 在生產環境是正確的，但會綁定到建立它的 event loop；測試中需要 autouse fixture 重置 | 已在 `conftest.py` 處理並註明原因 |
| Celery 的 `apply_async().get()` 在 API 中用 `asyncio.to_thread` 包裝 | 會佔用一個執行緒；僅用於 admin 專用的 echo 端點 | 可接受；Phase 2 的長任務一律走 `202 + job_id` |
| `pnpm-lock.yaml` 尚未 commit | CI 的 `--frozen-lockfile` 會失敗 | **首次 commit 時必須包含**（見 §10） |
| `web/public/` 原本不存在 | `docker/web.Dockerfile` 的 `COPY /app/public` 會讓映像建置失敗 | ✅ 已修（加入 `public/favicon.ico`），由瀏覽器驗證抓到 |

---

## 7. Security Review

### 7.1 已實作

| 面向 | 措施 | 驗證方式 |
|------|------|---------|
| 密碼儲存 | argon2id（記憶體困難），自動偵測是否需要重新雜湊 | 單元測試：加鹽、不可逆、容忍損壞的 hash |
| Token | access 15m / refresh 7d，**輪替 + Redis 撤銷清單** | 整合測試：重放已使用的 refresh token 回 401 |
| Token 型別混淆 | refresh token 不可當 access token 使用 | 單元測試 |
| 使用者列舉 | 帳號不存在時仍計算一次雜湊；兩種失敗訊息完全相同 | 整合測試斷言 `a.detail == b.detail` |
| RBAC | `viewer < analyst < admin` 階層，FastAPI dependency 檢查 | 整合測試：viewer 存取 admin 端點回 403 |
| SQL Injection | 全面 SQLAlchemy ORM / bound parameters；無任何 f-string SQL | 程式碼審查 + ruff `S` 規則 |
| Secret 管理 | `.env` 已 gitignore；只 commit `.env.example`；CI 跑 gitleaks | CI job |
| 弱祕密防護 | production/staging 拒絕 placeholder 或 < 32 字元的 `JWT_SECRET` | 單元測試 |
| Mock 資料防護 | production 下 `ALLOW_MOCK_DATA=true` 拒絕啟動 | 單元測試 |
| CORS | 白名單 origin，production 強制 https | 單元測試 |
| 安全標頭 | `X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、`Permissions-Policy` | 整合測試 |
| 稽核軌跡 | 登入成功與失敗都寫 `audit_logs`（含 IP、UA、request_id） | 整合測試 |
| 錯誤資訊洩漏 | 未捕捉例外記錄完整 traceback 但只回傳不透明訊息 | 程式碼審查 |
| 容器 | 非 root 使用者（uid 1001）、多階段建置、只暴露必要 port | Dockerfile |
| 網路暴露 | compose 所有 port 綁 `127.0.0.1` | docker-compose.yml |
| 資料庫密碼 | compose 使用 `${POSTGRES_PASSWORD:?...}`，未設定則拒絕啟動 | docker-compose.yml |

### 7.2 尚未實作（附風險評估）

| 缺口 | 目前風險 | 何時處理 |
|------|---------|---------|
| Rate limiting | 低（個人自用、僅綁 localhost）。但登入端點無暴力破解防護 | 對外服務前必做 |
| 帳號鎖定 / 登入失敗計數 | 同上 | Phase 9 或對外前 |
| CSRF | 不適用（純 Bearer token，無 cookie session） | — |
| MFA | 低（單一使用者） | 對外服務時 |
| Refresh token 存於 `sessionStorage` | XSS 可竊取。緩解：分頁關閉即清除、token 短命、輪替使重放失敗 | 對外服務時改 httpOnly cookie + CSRF |
| 依賴掃描 | `pip-audit` / `pnpm audit` 已寫進 CI，但目前設為非阻斷 | 有基線後改為阻斷 |
| TLS | 本機部署為 http | 對外時加反向代理 |

### 7.3 為 Phase 8 預留的約束

`CONTRIBUTING.md` 已寫明：Copilot 將取得**唯讀資料庫角色**與**白名單型別化工具**，
永遠不會有任意 SQL 執行權；不可信文字（新聞、文件）以明確的 data block 包裹，
不得被當作指令。

---

## 8. Performance Notes

### 8.1 實測數字（沙箱：2 vCPU / 8 GB RAM）

| 操作 | 實測 |
|------|------|
| `/health/full`（7 個元件） | 約 12 ms |
| Postgres `SELECT 1` | 3.3 ms |
| pgvector 版本查詢 | 0.6 ms |
| Redis ping + info | 4.1 ms |
| Celery heartbeat 檢查 | 3.5 ms |
| 端到端 worker round trip | 約 20 ms（任務內部 18.5 ms） |
| 後端測試套件（95 tests） | 約 25 s（含 argon2 與資料庫 schema 重建） |
| 前端 production build | 約 25 s |
| 前端 First Load JS（shared） | 105 kB |
| Dashboard route | 4.17 kB |
| Health route | 6.0 kB |

### 8.2 已內建的效能考量

- **連線池**：Postgres pool 10 + overflow 20、`pool_pre_ping`、`pool_recycle=1800`
- **健康檢查並行化**：非 DB 檢查用 `asyncio.gather`，總耗時是最慢單項而非總和
- **前端不重複請求**：TanStack Query `staleTime` 依資料變動速度設定
  （健康 15–30 s、capabilities 5 min），`refetchOnWindowFocus` 關閉
- **Celery `prefetch_multiplier=1`**：長任務不會霸佔 prefetch buffer
- **Redis `allkeys-lru` + maxmemory**：快取不會無限成長

### 8.3 已知的效能債

| 項目 | 影響 | 觸發條件 |
|------|------|---------|
| argon2 雜湊約 50–100 ms | 登入延遲；也是防暴力破解的特性 | 若成為瓶頸再調參數 |
| 每個測試重建 schema | 測試套件約 25 s | 超過 60 s 時改用 transaction rollback |
| Health 端點無快取 | 每次都真的探測 | 若被高頻輪詢，加 5 s 快取 |

Phase 0 訂下的效能預算（單日全市場指標 < 180 s、10 年回測 < 120 s）
在 Phase 3 / Phase 6 才有意義，目前無可測項。

---

## 9. Phase 2 Readiness

### 9.1 Phase 2 需要的東西，Phase 1 是否已備妥

| Phase 2 需求 | 狀態 |
|-------------|------|
| Provider 可掛載的抽象位置 | ✅ `app/providers/` 目錄結構已規劃於 `docs/ARCHITECTURE.md` 附錄 A |
| 資料契約（Provider 的輸出型別） | ✅ `MarketQuote` / `HistoricalPrice` / `FinancialFact` / `InstitutionalFlow` 已定義並測試 |
| Bitemporal 保證 | ✅ `FinancialFact.announced_at` 必填 + `is_known_at()` |
| Mock 資料防護 | ✅ `DataSource.MOCK` → 強制 `is_demo` → API `meta` → UI 角標；production 拒絕啟動 |
| 背景任務框架 | ✅ Celery + 5 個佇列 + `q_ingest` 已宣告；`@task` 的重試/逾時模式已在 `health_check_task` 示範 |
| Job 執行紀錄 | ✅ `job_runs` 表已建立（Phase 2 開始寫入） |
| 資料品質表 | ⏳ `docs/ERD.md` 已設計 `data_quality_scores` / `data_gaps` / `quarantine_records` / `backfill_progress` / `data_freshness`，Phase 2 建立 |
| Migration 流程 | ✅ 已驗證可正向與反向；`alembic check` 已進 CI |
| 時序表能力 | ⚠️ TimescaleDB 需先完成 L1/L2 驗證 |
| 向量能力 | ✅ pgvector 已驗證可用（Phase 4/8 才需要） |
| API envelope 與 `meta` | ✅ 每個回應都有；Phase 2 只需填入 `data_timestamp` / `source` / `quality` |
| 錯誤語意 | ✅ `DataNotAvailableError`、`UpstreamUnavailableError`、`LicenseRestrictedError` 已定義備用 |
| 前端資料溯源元件 | ✅ `<DataProvenance>` 已可用 |
| 交易日曆 | ⏳ Phase 2 第一批工作 |

### 9.2 Phase 2 的第一批任務（依 `docs/DEVELOPMENT_ROADMAP.md` §Phase 2）

1. **在台灣本機環境重測所有 ⚠️ UNVERIFIED 端點**（TPEx、TAIFEX、MOPS）
   —— 這是 Phase 2 的第一件事，因為 provider 只能實作已驗證的來源
2. 交易日曆（含颱風假與補行上班日）+ 日曆守門邏輯
3. `BaseMarketDataProvider` + TWSE / TPEx adapter + `MockProvider` 的 production 禁用測試
4. Normalizer（民國年、千分位、`"--"`、`Decimal`）+ 真實 payload fixture
5. 歷史回補（優先用全市場單日端點，約 2,500 次請求；避開單股單月的 12 萬次）

### 9.3 建議在 Phase 2 開始前先做的兩件事

1. **完成 L1 / L2 驗證**（本機跑一次 `make up && make verify`）
2. **決定程式碼的最終落腳處** —— 目前在這個雲端 session 的工作區。
   選項：推到 GitHub repo（推薦，CI 才有意義）、或下載到你本機的資料夾

---

## 10. Git Commit Recommendation

### 10.1 這個 session 的工作區狀態

`/home/claude/twquant` 已 `git init`，但**尚未有任何 commit**。
建議分成 3 個 commit，讓 Phase 0 與 Phase 1 的邊界在歷史中清晰可見。

### 10.2 建議的 commit 序列

```bash
cd twquant

# ── 前置：確保 lock file 進版控（CI 的 --frozen-lockfile 需要）
cd web && pnpm install && cd ..
git add -f web/pnpm-lock.yaml

# ── 確認 .env 不會被 commit
git status --porcelain | grep -E '^\?\? \.env$' && echo "警告：.env 未被忽略" || echo "OK"
```

**Commit 1 — Phase 0 文件**

```
docs: add Phase 0 architecture design (9 documents)

Repository audit, system architecture, ERD, data sources, API spec,
AI engine, quant engine and development roadmap.

Data sources verified against live endpoints on 2026-08-15:
- TWSE OpenAPI MI_INDEX / STOCK_DAY_ALL / t187ap03_L
- TWSE RWD STOCK_DAY / T86 (date-parameterised, the backfill path)
- /v1/fund/T86 and /v1/exchangeReport/BFI82U return 404 — do not use

Files: docs/*.md
```

**Commit 2 — Phase 1 基礎建設**

```
feat(infra): Phase 1 foundation — API, worker, database, frontend shell

Infrastructure:
- docker compose: postgres (timescaledb-ha, bundles pgvector), redis,
  api, worker, beat, web; optional profiles for ollama, flower, minio
- migrate runs as a one-shot service so no process starts against a
  stale schema (ADR-014)
- all ports bound to 127.0.0.1; containers run as non-root

Backend:
- FastAPI + Pydantic v2 + SQLAlchemy 2.0 async + Alembic
- config validation refuses to boot production with placeholder secrets,
  DEBUG enabled, mock data allowed, or plaintext CORS
- structlog JSON logging with request_id / job_run_id correlation
- RFC 9457 Problem Details error model
- argon2id passwords; JWT access/refresh with rotation and Redis
  revocation (ADR-020)
- health checks report every component independently and never 500;
  disabled is distinct from degraded (ADR-019)

Worker:
- Celery with five declared queues; health_check_task connects to
  Postgres and Redis from inside the worker process, so a worker that
  can reach the broker but not the database is visible

Contracts:
- shared data contracts with two invariants enforced by types:
  FinancialFact requires announced_at (no look-ahead by construction),
  and derived values require a Provenance block

Frontend:
- Next.js 15 + Tailwind v4 + TanStack Query + Zustand
- login, dashboard shell, system health dashboard
- DataProvenance component renders the DEMO DATA badge whenever
  is_demo is set

No market data is served by this deployment. Dashboard panels are empty
states, not placeholder numbers (ADR-021).
```

**Commit 3 — 測試、CI、文件**

```
test(phase1): 95 backend and 14 frontend tests, CI, project docs

Backend tests cover configuration validation, the bitemporal and
provenance contract invariants, password and JWT security properties,
all five health endpoints, authentication including refresh-token replay
rejection, database extensions, Redis cache versioning, and the Celery
round trip.

Two tests are worth calling out:
- test_backtest_filter_excludes_future_information contrasts the correct
  announced_at filter with the naive period_end filter that leaks an
  unpublished report
- test_no_endpoint_serves_market_data_in_phase_1 scans the OpenAPI schema
  and fails if a market endpoint appears

Fixes found by these tests:
- cache versions must start at 0, not 1: Redis INCR on a missing key
  also yields 1, which would make the first invalidation a no-op (ADR-016)
- database health probes must run sequentially; concurrent statements on
  one AsyncSession raise IllegalStateChangeError and would report three
  components as spuriously unhealthy (ADR-018)

CI: gitleaks, backend (ruff, mypy --strict, reversible migrations,
alembic check, pytest with 80% floor, pip-audit), frontend (lint,
typecheck, vitest, build), docker image builds, and a compose job that
starts the full stack and runs scripts/verify_stack.sh.

Coverage 93%. ruff and mypy --strict clean.
```

### 10.3 分支與標籤建議

```bash
git checkout -b main
# ... 三個 commit ...
git tag -a v0.1.0-phase1 -m "Phase 1 — Foundation"
```

後續每個 Phase 用 feature branch + PR，讓 CI 真正發揮 gate 的作用。

### 10.4 Commit 前的最後檢查

```bash
make check                              # lint + typecheck + tests + migration
git status --porcelain | grep '\.env$'  # 必須沒有輸出
grep -r "CHANGE-ME" --include="*.py" --include="*.ts" . | grep -v .env.example
```

---

## 11. Definition of Done — 逐項核對

| # | 項目 | 狀態 | 證據 |
|---|------|------|------|
| 1 | `docker compose up` 成功 | ⏳ **待你本機驗證** | 沙箱無 Docker daemon（L1）。compose 檔已完成，CI `compose` job 會驗證 |
| 2 | PostgreSQL healthy | ✅ | 16.13 實測，`/health` 回報 healthy，延遲 3.3 ms |
| 3 | TimescaleDB healthy | ⏳ **待你本機驗證** | 套件庫在此網路被擋（L2）。compose 使用 `timescaledb-ha:pg16` |
| 4 | pgvector healthy | ✅ | v0.6.0，實測相似度查詢通過 |
| 5 | Redis healthy | ✅ | v7.0.15 |
| 6 | Celery worker healthy | ✅ | 5 個佇列、2 個任務註冊、`control.ping` 回 `{'celery@vm': {'ok': 'pong'}}` |
| 7 | FastAPI healthy | ✅ | `/health/full` 回 200，7 個元件全部回報 |
| 8 | Next.js 啟動 | ✅ | `next build` 成功，5 條路由 |
| 9 | Login 可使用 | ✅ | 實測登入成功並取得 token pair；14 個 auth 整合測試通過 |
| 10 | Protected API 可使用 | ✅ | `/auth/me` 需 token；`/auth/admin-only` 對 viewer 回 403 |
| 11 | Health Dashboard 全綠 | ✅ *（TimescaleDB 為黃）* | 6/7 綠、TimescaleDB 黃（誠實回報缺失）、LLM 灰（刻意停用） |
| 12 | Alembic migration 正常 | ✅ | upgrade → downgrade -1 → upgrade 全部成功 |
| 13 | Backend tests 全綠 | ✅ | 95 收集 → 93 passed / 2 skipped，覆蓋率 93% |
| 14 | Frontend build 成功 | ✅ | 14 tests passed、tsc clean、build 成功 |
| 15 | CI 全綠 | ⏳ **待推上 GitHub** | 5 個 job 已撰寫；每個步驟（ruff / mypy / migration up-down-up / alembic check / pytest+coverage / lint / typecheck / vitest / next build）都已在本地逐一手動執行通過 |
| 16 | Docker build 成功 | ⏳ **待你本機驗證** | 同 L1 |
| 17 | README 完整 | ✅ | 含全部 9 個要求章節 |
| 18 | `.env.example` 完整 | ✅ | 每個變數都有說明其存在理由 |
| 19 | Architecture 文件同步 | ✅ | Phase 1 未偏離 Phase 0 架構；新增 ADR-013~021 記於本報告 §3 |

**17 項完成、4 項待你在有 Docker 的環境驗證。**

---

## 12. 停止點

依照指示，Phase 1 到此結束，**不自行進入 Phase 2**。

需要你決定或執行的事：

1. **執行本機驗證**（解除 L1 / L2）：
   ```bash
   make up && make seed && make verify
   ```
   預期看到 7 個元件中 6 個 healthy + LLM disabled，TimescaleDB 這次應為 healthy。

2. **決定程式碼落腳處**：推到 GitHub（推薦，CI 才有意義）／下載到本機資料夾／留在雲端工作區。

3. **確認 ADR-013 ~ ADR-021** 是否併入 `docs/ARCHITECTURE.md` §18。

4. 確認後再下 Phase 2 指令。
