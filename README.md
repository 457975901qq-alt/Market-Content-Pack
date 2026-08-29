# 每日市场内容包

一个面向中文读者的、可审计的每日市场内容生成项目。它把行情、新闻、GitHub AI 项目和模型生成结果整理成结构化 JSON，并输出早盘或晚盘文字内容。

Daily Market Content Pack is a Python pipeline for producing structured Chinese market briefings. It validates sources, market data, model output, and delivery readiness as separate steps.

> 默认只在本地生成、校验和记录审计结果，不向 X、邮件或其他平台发送内容。报告只用于信息整理与非个性化情景分析，不构成投资建议。

## 功能介绍

### 早盘与晚盘内容

项目提供两个互相隔离的内容版本：

- `morning_close_review`：上一交易时段收盘复盘、亚洲开盘前风险。
- `evening_premarket_watch`：美股盘前催化剂、预期变化和盘前风险。

两个版本有独立 Prompt、字段集合、数据窗口和输出目录，不会互相覆盖。时间统一使用 `Asia/Tokyo`，默认配置时间分别是 06:30 和 17:30。

### 结构化市场内容

每次运行生成严格 JSON，而不是让模型直接输出无法校验的长文。内容包括摘要、重点、重要资产、宏观事件、风险因素、分析文字、15 个固定主题栏目、来源证据和 `ai_investment_view`。

没有可靠来源时，栏目会明确标记为 `unavailable` 并写入“数据暂缺”；程序不会用模型猜测价格、新闻或项目事实。

### 行情与来源校验

行情和内容采用独立素材层：

```text
collect_sources → collect_market_quotes → generate_content → final_validation
```

默认使用 Yahoo Finance Chart 作为主要行情来源，并可使用 Massive 进行交叉核对。内容一致性要求的核心资产是 `VOO` 和 `QQQM`，它们是标普 500 与纳斯达克 100 的 ETF 代理，不是 SPX、NDX 指数本身。

### 多 Provider 与受控回退

支持 OpenAI、Ollama、Gemini 和无数字规则模板。不同 Provider 只负责生成原始文本，最终都必须经过同一套 JSON schema、日期、时区、行情、来源和 Reviewer 校验。

### 安全门禁与可恢复运行

流程通过白名单 Function Registry、参数校验、调用次数限制、状态 checkpoint、artifact hash、Reviewer 和 fail-closed 门禁控制风险。Agent Loop 只能选择已注册的业务步骤，不能执行 Shell、修改代码或调用外部发布函数。

每次运行有独立的 `<run_id>`，支持从已有状态恢复未完成步骤，适合离线演练、Shadow run 和受控 Canary。

## 技术栈

- 运行时：Python 3.11+、`uv`、Pydantic。
- 内容 Provider：OpenAI、Ollama、Gemini、`rule_template`。
- 数据来源：Yahoo Finance Chart、Massive、RSS、GitHub，以及可选的 X、Exa、Jina 路由。
- 质量控制：JSON schema 校验、来源/行情校验、Reviewer、离线回归评测和 Self-Healing Canary。
- 测试：Pytest。
- 可选观测：Docker Compose + Phoenix + OpenTelemetry。

## 目录结构

```text
.
├── main.py                    # 稳定的项目入口
├── build_daily_market_pack.py # 主流程、Agent Loop 与断点恢复
├── market_content_openai.py   # 严格 JSON 生成与内容校验
├── market_quotes.py           # 结构化行情采集与交叉核对
├── source_router.py           # RSS/GitHub/可选外部来源统一入口
├── reviewer_agent.py          # 只读质量复核
├── agent/                     # Agent 状态、动作、规划与完成策略
├── runtime/                   # 计划、决策、checkpoint、审核和恢复状态
├── config/                    # 版本、数据、Provider、发布和安全策略
├── evals/                     # 离线数据集与评测实验
├── tests/                     # 单元、集成、回归和安全测试
├── deploy/                    # 调度器示例配置
├── Dockerfile                 # Python 运行镜像
└── docker-compose.yml         # app 与 Phoenix 本地服务
```

## 本地运行

### 1. 安装依赖

要求 Python 3.11 或更高版本，并安装 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
cp .env.example .env
```

`.env` 只用于本机配置，已被 `.gitignore` 排除。不要把 API Key、Cookie、私钥、SMTP 密码或运行输出提交到仓库。

### 2. 选择 Provider

无模型的离线安全演练：

```bash
MARKET_CONTENT_PROVIDER=rule_template
```

使用本地 Ollama：

```bash
MARKET_CONTENT_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:9b
```

使用远程模型时，只在本机环境变量或 `.env` 中配置对应密钥：

```bash
# OpenAI
OPENAI_API_KEY=<your-openai-api-key>
OPENAI_MODEL=gpt-5

# Gemini
GEMINI_API_KEY=<your-gemini-api-key>
GEMINI_MODEL=gemini-3.5-flash
```

### 3. 运行测试

```bash
uv run pytest -q
```

只运行 Self-Healing 的离线故障 fixture：

```bash
SELF_HEALING_CANARY_MODE=true \
uv run python -m self_healing.canary --fixture
```

查看本地 Provider、Docker、Phoenix 和历史运行指标：

```bash
uv run python healthcheck.py
```

报告写入 `reports/system_health.md` 和 `reports/system_health.json`。

### 4. 生成每日内容

推荐先使用规则模板进行本地 Shadow run：

```bash
uv run python main.py \
  --edition evening_premarket_watch \
  --provider rule_template \
  --dry-run \
  --shadow-run
```

运行早盘版本：

```bash
uv run python main.py \
  --edition morning_close_review \
  --provider auto \
  --dry-run \
  --shadow-run
```

完整运行会尝试读取实时来源和行情；网络不可用、凭据缺失或行情交叉核对失败时，流程会记录阻断原因并停止，不会生成虚假结果。

## 版本与数据窗口

| edition | 内容重点 | 默认时间 | 固定字段 |
| --- | --- | --- | --- |
| `morning_close_review` | 收盘复盘、亚洲开盘前风险 | 06:30 | `previous_session_summary`、`asia_open_risk`、`close_review_focus` |
| `evening_premarket_watch` | 美股盘前催化剂、预期变化、风险 | 17:30 | `premarket_catalysts`、`expectation_changes`、`premarket_risks` |

需要严格限制启动时间时，加上：

```bash
uv run python main.py \
  --edition morning_close_review \
  --provider auto \
  --dry-run \
  --enforce-schedule
```

截止时间、来源窗口、Prompt 版本和字段集合由 `config/edition_profiles.json` 固定，不使用模型当前时间自行推断。

## 输出文件与数据约定

每次运行使用独立目录：

```text
outputs/runs/<run_id>/
├── market_content/market_content.json
├── market_sources/normalized_materials.json
├── market_sources/filtered_materials.json
└── market_sources/market_quotes.json

runtime/plans/<run_id>.json
runtime/decisions/<run_id>.json
runtime/reviews/<run_id>/review_result.json
logs/market_content_errors.log
```

JSON 校验规则包括：

- `date` 必须等于当前 `Asia/Tokyo` 日期，`timezone` 必须为 `Asia/Tokyo`。
- `summary`、`key_points`、`analysis_text` 和版本专属字段不能为空。
- `daily_sections` 必须完整覆盖固定的 15 个栏目，每项都要有状态、内容和证据。
- `ai_investment_view` 只能输出观察框架、证据、风险和失效条件，不输出买入、卖出、加仓、减仓、目标价或止损价指令。
- `VOO`、`QQQM` 行情必须经过时间戳、新鲜度和来源一致性检查。

`outputs/`、`logs/`、`runtime/`、`state/` 和 `reports/` 都是本地生成物，默认不提交到 Git。

## 数据来源配置

行情策略位于 `config/market_data_policy.json`，常用配置为：

```bash
MARKET_REQUIRE_CROSSCHECK=true
MARKET_SECONDARY_PROVIDER=massive
MARKET_SOURCE_CONFLICT_THRESHOLD=0.02
MARKET_MAX_STALENESS_HOURS=120
MARKET_STOCK_SYMBOLS=NVDA,MSFT,AAPL
```

素材策略位于 `config/source_policy.json`。默认 RSS 源包括 Yahoo Finance、MarketWatch、Federal Reserve、NVIDIA、OpenAI、CNBC 和 The Verge；可用 `RSS_FEEDS` 和 `RSS_ITEMS_PER_FEED` 覆盖。

GitHub AI 项目由 `github_ai_projects.py` 通过 GitHub REST API 搜索 AI Agent、LLM、MCP、RAG 和 workflow automation 相关仓库。Token 是可选的，只保存在本机：

```bash
GITHUB_TOKEN=<your-github-token>
```

没有可用来源时，系统记录 `unavailable`，不会把缺失数据当作成功。

## 断点恢复与 Shadow run

恢复未完成运行：

```bash
uv run python main.py \
  --edition evening_premarket_watch \
  --resume <run_id>
```

从指定步骤重新执行：

```bash
uv run python main.py \
  --edition evening_premarket_watch \
  --resume <run_id> \
  --from-step final_validation
```

Shadow run 使用独立的 `outputs/shadow/`、`runtime/shadow/` 和日志目录。它用于验证流程，结果固定为 `delivered=false`。

查看稳定性窗口：

```bash
uv run python scripts/canary_status.py
uv run python scripts/canary_status.py --json
```

## Docker 与 Phoenix

Docker 主要用于隔离运行环境和启动可选的 Phoenix 观测服务。Phoenix 不是内容生成的必需依赖：

```bash
docker compose up -d phoenix
docker compose run --rm app uv run pytest -q
```

启动完整的本地 Shadow run：

```bash
docker compose run --rm app \
  uv run python main.py \
  --edition evening_premarket_watch \
  --provider rule_template \
  --dry-run \
  --shadow-run
```

Phoenix UI 默认地址为 `http://127.0.0.1:6006`。Docker 不会自动安装 Ollama；容器访问宿主机模型时，需要正确配置 `OLLAMA_BASE_URL`。

## 调度与部署

项目本身不会自动安装 cron 或 launchd。迁移到其他机器时只保留一个调度来源，避免 Codex automation、cron、launchd 和 Docker 同时触发同一版本。

调度器入口：

```bash
uv run python -m tools.scheduler --help
uv run python -m tools.scheduler list
```

`deploy/com.ara.daily-market-scheduler.plist` 只是 macOS 示例配置，使用前需要将其中的项目路径替换为本机实际路径。

## 生产预检与外部投递

默认策略始终偏向本地和只读：

- `DELIVER_ENABLED=false`。
- `GLOBAL_DELIVERY_KILL_SWITCH=true`。
- `external_delivery_enabled=false`。
- 测试、Shadow 和 Canary 结果固定为 `delivered=false`。
- 外部投递不在运行时 Function Registry 中，Agent 不能自行调用发布动作。

生产预检示例：

```bash
uv run python -m production_preflight --run-id <shadow_run_id>
```

即使预检通过，正式邮件发送仍需要独立批准文件、健康的 SMTP 适配器、匹配的 artifact hash 和显式确认：

```bash
uv run python -m deliver_run \
  --run-id <production_run_id> \
  --approval-file <approval.json> \
  --confirm-production-send
```

不要在 README、`.env`、日志或命令历史中写入 SMTP 密码和第三方 Token。

## 常用命令

```bash
uv sync                                      # 安装/同步依赖
uv run pytest -q                             # 全量测试
uv run python main.py --help                # 查看主入口参数
uv run python healthcheck.py                 # 生成健康报告
uv run python -m self_healing.canary --help  # 查看 Canary 参数
uv run python scripts/canary_status.py --json # 查看稳定性窗口
uv run python -m production_preflight        # 生产放行预检
```

## 当前限制

- 远程模型、实时行情和外部来源受网络、凭据、限流和供应商权限影响；不可用时会明确失败或降级，不保证每次都能生成完整事实内容。
- Massive 当前只承担有限的历史行情交叉核对能力，不能被当作 SPX、DJI 或 NDX 实时主数据源。
- VOO、QQQM 是 ETF 代理，报告不能据此声称已经取得对应指数的真实指数值。
- 当前仓库不提供用户系统、Web UI 或云端数据库；运行产物保存在本地目录。
- 项目未声明开源许可证。公开仓库不等于自动授予复制、修改或商业使用权；如需明确授权，请补充合适的 LICENSE 文件。
