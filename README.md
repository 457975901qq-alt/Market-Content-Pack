# 每日市场内容包

一个面向中文读者的、可审计的每日市场内容生成流程。项目把行情、新闻、GitHub AI 项目和模型生成结果组织成结构化 JSON，再生成早盘或晚盘文字报告。

> 默认只做本地生成、校验和审计，不向 X、邮件或其他平台发送内容。项目输出的是信息整理与情景分析，不构成个性化投资建议。

## 你可以用它做什么

| 能力 | 说明 |
| --- | --- |
| 两个内容版本 | `morning_close_review`：收盘复盘与亚洲开盘前风险；`evening_premarket_watch`：美股盘前催化剂、预期变化与风险 |
| 结构化输出 | 生成严格 JSON、15 个固定主题栏目、分析文字和来源证据 |
| 行情交叉核对 | Yahoo Finance Chart 为主源；可用时使用 Massive 作为第二来源。核心资产是 VOO、QQQM ETF 代理，不是 SPX、NDX 指数本身 |
| 多模型 | OpenAI、Ollama、Gemini 和无数字规则模板；所有 Provider 最终都经过同一套 JSON 校验 |
| 可恢复运行 | 运行状态、计划、决策、artifact hash 和 Reviewer 结果分目录保存，支持 resume |
| 安全门禁 | 数据缺失、来源冲突、日期错误、JSON 不完整或审批不满足时 fail-closed，不生成虚假事实，也不自动发布 |

## 快速开始

### 1. 安装

要求 Python 3.11 或更高版本，并安装 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
cp .env.example .env
```

`.env` 只在本机使用，已被 `.gitignore` 排除。至少选择一个内容 Provider：

```bash
# 无模型、无 API Key 的安全演练模式
MARKET_CONTENT_PROVIDER=rule_template

# 或使用本地 Ollama
MARKET_CONTENT_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:9b

# 或使用远程模型
# MARKET_CONTENT_PROVIDER=openai，需要 OPENAI_API_KEY
# MARKET_CONTENT_PROVIDER=gemini，需要 GEMINI_API_KEY
```

### 2. 运行测试

```bash
uv run pytest -q
```

只检查 Self-Healing Canary 的离线 fixture：

```bash
SELF_HEALING_CANARY_MODE=true \
uv run python -m self_healing.canary --fixture
```

### 3. 生成一版本地内容

```bash
uv run python main.py \
  --edition evening_premarket_watch \
  --provider rule_template \
  --dry-run \
  --shadow-run
```

运行默认写入本地 `outputs/`、`logs/` 和 `runtime/`，不会触发外部投递。若要运行早盘版本，把 edition 改为：

```text
morning_close_review
```

## 版本与运行时间

所有时间使用 `Asia/Tokyo`：

| edition | 用途 | 配置时间 |
| --- | --- | --- |
| `morning_close_review` | 上一交易时段收盘复盘、亚洲开盘前风险 | 06:30 |
| `evening_premarket_watch` | 美股盘前催化剂、预期变化、盘前风险 | 17:30 |

使用 `--enforce-schedule` 时，程序只接受对应时间窗口内的启动请求：

```bash
uv run python main.py \
  --edition morning_close_review \
  --provider auto \
  --dry-run \
  --enforce-schedule
```

程序不会根据模型当前时间自行拼接数据截止时间；版本配置中的 `data_cutoff`、来源窗口和字段集合是固定的。

## Provider 选择

| Provider | 凭据 | 适用场景 |
| --- | --- | --- |
| `rule_template` | 无 | 离线演练、故障回退；只输出“数据暂缺”等保守状态，不生成价格事实 |
| `ollama` | 本地服务 | 本机模型生成，不需要云端 API Key |
| `openai` | `OPENAI_API_KEY` | 使用 OpenAI 生成严格 JSON |
| `gemini` | `GEMINI_API_KEY` | 使用 Gemini 生成严格 JSON |
| `auto` | 视可用服务而定 | 由受控 Tool Router 按健康状态选择并记录 fallback chain |

无论选择哪个 Provider，结果都会经过 JSON 解析、schema 校验、日期/时区校验、来源校验和 Reviewer。模型不能执行 Shell、未注册函数或外部发布动作。

## 输出与审计文件

每次运行有独立的 `<run_id>`，主要产物位于：

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

`outputs/`、`logs/`、`runtime/`、`state/` 和 `reports/` 是本地生成物，默认不提交到 Git。JSON 内容的核心要求包括：

- `date` 必须是当前 `Asia/Tokyo` 日期；`timezone` 必须是 `Asia/Tokyo`。
- `summary`、`key_points`、`analysis_text` 和版本专属字段不能为空。
- `daily_sections` 必须完整覆盖固定的 15 个栏目；没有可靠来源时标记为 `unavailable` 并写入“数据暂缺”。
- `ai_investment_view` 只能提供非个性化的观察框架、证据、风险和失效条件，不输出买入、卖出、目标价或止损价指令。

## 数据来源与行情规则

`source_router.py` 统一整理 RSS、GitHub 以及可选的 X、Exa、Jina 来源，写出标准化素材。未接入或不可用的来源会明确记录为 `unavailable`，不会被当作采集成功。

行情链路为：

```text
collect_sources → collect_market_quotes → generate_content → final_validation
```

默认必需的核心资产为 `VOO` 和 `QQQM`。它们是用于内容一致性的 ETF 代理，不应在文档或报告中被误写成 SPX、NDX 指数。行情缺失、冲突、过期或时间戳异常时，流程停止生成。

相关配置在 `config/market_data_policy.json`，常用环境变量为：

```bash
MARKET_REQUIRE_CROSSCHECK=true
MARKET_SECONDARY_PROVIDER=massive
MARKET_SOURCE_CONFLICT_THRESHOLD=0.02
MARKET_MAX_STALENESS_HOURS=120
MARKET_STOCK_SYMBOLS=NVDA,MSFT,AAPL
```

## 断点恢复与 Shadow run

为指定运行 ID 恢复未完成步骤：

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

Shadow run 使用隔离的输出、日志和状态目录；它适合验证流程，不会发送外部消息：

```bash
uv run python main.py \
  --edition evening_premarket_watch \
  --provider rule_template \
  --dry-run \
  --shadow-run \
  --run-id market_YYYYMMDD_HHMM
```

## 安全与发布边界

- 密钥只从环境变量或本机 Keychain 读取，不写入仓库、日志、trace 或发布 manifest。
- `DELIVER_ENABLED=false`、`GLOBAL_DELIVERY_KILL_SWITCH=true` 和 dry-run 是默认值。
- 外部投递不在运行时 Function Registry 中；Agent Loop 不能调用 `deliver` 或 `canary_deliver`。
- 生产预检和人工审批是独立门禁；测试、Shadow 和 Canary 结果统一标记 `delivered=false`。
- 不要把 `.env`、运行输出、Cookie、私钥或 SMTP 凭据提交到 Git。

生产预检示例：

```bash
uv run python -m production_preflight --run-id <shadow_run_id>
```

即使预检通过，正式发送仍需要独立批准文件、健康的投递适配器、匹配的 artifact hash 和显式确认。默认配置不会发送。

## GitHub AI 项目数据

`github_ai_projects.py` 通过 GitHub REST API 搜索 AI Agent、LLM、MCP、RAG 和 workflow automation 相关仓库，筛选后写入每日内容包。配置 token：

```bash
GITHUB_TOKEN=<your-github-token>
```

Token 只保存在本机 `.env` 或 Keychain 中；没有 token 时应记录来源不可用，不得伪造项目数据。

## Phoenix 与 Docker（可选）

Phoenix 只作为本地观测服务，主流程不依赖 Phoenix 才能完成：

```bash
docker compose up -d phoenix
```

默认 UI 地址为 `http://127.0.0.1:6006`。Docker 不会自动安装 Ollama；容器访问宿主机模型时需要正确配置 `OLLAMA_BASE_URL`。

## 项目结构

```text
main.py                    # 稳定入口
build_daily_market_pack.py # 主流程与断点恢复
market_content_openai.py   # 严格 JSON 生成与校验
market_quotes.py           # 结构化行情采集与交叉核对
source_router.py           # 新闻与外部素材统一入口
reviewer_agent.py          # 只读质量复核
agent/ runtime/            # Agent 状态、规划与恢复
config/                    # 版本、数据源、运行和安全策略
tests/                     # 单元、回归和离线评测
```

## 许可证

当前仓库未声明开源许可证。公开仓库不等于自动授予复制、修改或商业使用权；如需他人按明确条款使用，请先补充合适的 LICENSE 文件。
