# 每日市场内容包

本项目生成中文每日市场内容包：文字内容和市场分析由结构化 JSON 统一管理。

## 当前输出规范

- 默认只输出市场内容 JSON、市场分析和最终文字报告。
- 默认只输出文字和结构化市场数据；平台文案、图片生成和外部发布均已关闭。
- 数据缺失时显示“待核验”或“暂无可靠数据”，不得编造数值。
- X/Twitter 文本发布适配器已注册并选定为当前 delivery adapter，但默认保持关闭；`DELIVER_ENABLED=false`、全局 Kill Switch 和审批门禁仍会阻断外部发布。结果只写入本地运行目录；图片链路目前也只做本地渲染和 QA。
- 内容包包含 `ai_investment_view`：只输出基于已验证来源的非个性化情景分析、观察框架、证据、风险和失效条件；数据不足时输出“数据不足”，不生成买入、卖出或目标价指令。

## OpenAI 市场内容 JSON

`market_content_openai.py` 负责调用 OpenAI API 生成每日市场内容。OpenAI 返回内容必须是严格 JSON，不能是普通文章、Markdown 或带额外解释的文本。

运行前配置：

```bash
export OPENAI_API_KEY="<your-openai-api-key>"
```

可选模型配置：

```bash
export OPENAI_MODEL="gpt-5"
```

内容模型也可以在不改业务 JSON 契约的前提下选择本地或受控降级后端：

```bash
# 本地 Ollama；不可用时不会自动伪造数据
uv run python main.py --edition evening_premarket_watch --provider ollama

# Gemini；仅在 GEMINI_API_KEY 已配置时可用
uv run python main.py --edition evening_premarket_watch --provider gemini

# Ollama → Gemini → 无数字规则模板
uv run python main.py --edition evening_premarket_watch --provider auto

```

`rule_template` 只会生成“数据暂缺”状态，不会生成价格、涨跌幅或股票事实。
Provider 错误会回到现有错误日志和状态机，不会绕过内容 JSON 校验。

### X/Twitter 发布适配器

当前适配器选择为 `x_twitter`，仅支持文字调用，接口为 X API `POST /2/tweets`；不支持图片上传，也不会复用 X 数据采集凭据。写权限使用 OAuth 1.0a User Context，需要四项独立凭据：Consumer Key、Consumer Secret、Access Token、Access Token Secret，分别存放在 macOS Keychain 的 `x_consumer_key`、`x_consumer_secret`、`x_publish_access_token`、`x_publish_access_token_secret` 项中。缺少任意一项时适配器健康状态为 `unconfigured`。

当前安全状态固定为：

```text
delivery_policy.adapter=x_twitter
delivery_policy.enabled=false
delivery_policy.external_delivery_enabled=false
delivery_policy.global_delivery_kill_switch=true
```

因此本地测试只验证适配器选择和健康检查，不会创建、发送或重试 X 帖子。要进入正式发布准备阶段，还必须经过独立凭据配置、目标账号授权、生产 Canary、人工审批和显式发布门禁。

在本机配置 OAuth 1.0a 凭据时使用隐藏输入，不要把凭据写入 `.env`：

```bash
uv run python scripts/configure_x_twitter_oauth.py
```

运行时默认启用受控 Tool Router：按 Ollama、Gemini、规则模板的健康状态选择内容 Provider，并记录候选工具、拒绝原因和 fallback chain。显式传入 `--provider ollama|gemini|openai|rule_template` 时仍可固定 Provider。

主流程的业务调用统一经过 `Planner → FunctionCall → FunctionExecutor`：市场行情、新闻、正文抽取、内容生成、市场/内容验证和 `final_quality_gate` 都绑定到固定 Python 业务函数。Executor 依据 `config/function_calling_policy.json` 校验参数、步骤权限和调用次数；失败结果交给受控 RepairController，修复成功只重试当前 FunctionCall 一次，并将调用事件写入运行日志。外部发布不在运行时注册表中。

当前执行层还启用了受控 Agent Loop（`config/agent_policy.json`）：每轮先观察状态、依赖、已完成 artifact 和预算，再选择下一个已注册步骤；它可以跳过已完成步骤、在受控范围内重排和记录重规划，但不能调用 `deliver`/`canary_deliver`、Shell、未注册工具或绕过 `final_quality_gate`/`reviewer_gate`。每次决策写入运行目录的 `logs/agent_loop.jsonl`，预算、停滞和依赖异常均 fail-closed。该层是可审计的 Agent 控制器，不会把任意模型输出变成代码执行权限。

Agent V1 的通用运行时位于 `agent/` 与 `runtime/`：`AgentState`、`AgentAction`、`AgentPlanner`、`DailyMarketAgent`、`FinishPolicy` 和 `RecoveryPolicy` 通过现有 Function Calling Executor 执行动作。每次动作都会保存 state hash checkpoint，并可通过现有 SQLite audit 记录恢复；缺行情、来源冲突、Provider/JSON 失败和 Reviewer reject 可触发受控 re-plan。`runtime/executor.py` 不创建第二套 Registry，只适配现有注册函数或测试注入的固定 adapter。

规划文件写入 `runtime/plans/<run_id>.json`，决策文件写入 `runtime/decisions/<run_id>.json`，决策审计写入 `logs/market_content_decisions.log`。Shadow run 使用自己的 `runtime/shadow/<run_id>/plans`、`decisions` 和日志目录。resume 会读取既有计划，已成功且 artifact 有效的步骤仍由状态机跳过，只重新规划未完成步骤。

### 真实结构化行情链路

market_quotes.py 是独立的结构化行情采集步骤，位于素材采集之后、内容生成之前：

collect_sources -> collect_market_quotes -> generate_content -> final_validation

行情默认使用 Yahoo Finance Chart 作为主源、Google Finance 作为第二来源。生产核心资产为 VOO（标普500 ETF 代理）和 QQQM（纳斯达克100 ETF 代理）；它们不是指数值。默认要求第二来源交叉核对。来源缺失、价格冲突、未来时间戳或超过新鲜度窗口时，行情 artifact 为 status=failed，流程不会继续生成内容或发送。

策略文件为 config/market_data_policy.json。可通过环境变量调整：

MARKET_REQUIRE_CROSSCHECK=true
MARKET_SECONDARY_PROVIDER=google_finance
MARKET_SOURCE_CONFLICT_THRESHOLD=0.02
MARKET_MAX_STALENESS_HOURS=120
MARKET_STOCK_SYMBOLS=NVDA,MSFT,AAPL

结构化 artifact 会写入运行目录的 market_sources/market_quotes.json，并带有 market_data_version、每个报价的 data_timestamp、来源 URL、交叉核对结果和 freshness 结果。内容 JSON 通过 market_data_version 与 market_data_hash 记录使用的行情版本；最终内容校验会验证 VOO/QQQM 两个必需核心资产已经传入内容包。

### 早晚版路由

每日内容必须明确指定一个版本：

```bash
# 06:30 Asia/Tokyo：上一交易时段收盘复盘 + 亚洲开盘前风险
uv run python main.py --edition morning_close_review

# 17:30 Asia/Tokyo：美股盘前催化剂 + 预期变化 + 风险
uv run python main.py --edition evening_premarket_watch
```

两版使用独立 Prompt、独立 `market_session` 和独立字段集合：

- `morning_close_review`：`close_review`，字段为 `previous_session_summary`、`asia_open_risk`、`close_review_focus`。
- `evening_premarket_watch`：`premarket_watch`，字段为 `premarket_catalysts`、`expectation_changes`、`premarket_risks`。

程序按 `config/edition_profiles.json` 固定 `data_cutoff`、来源窗口和调度时间，不使用模型运行时当前时间自行生成截止时间。新 run 的产物位于 `outputs/runs/<run_id>/`，因此早晚版不会覆盖彼此。

需要严格限制只能在 06:30 或 17:30 附近启动时，加上调度校验：

```bash
uv run python main.py --edition morning_close_review --enforce-schedule
```

输出文件：

- `outputs/runs/<run_id>/market_content/market_content.json`

错误日志：

- `logs/market_content_errors.log`

校验规则：

- API 返回后先解析 JSON；解析失败会停止流程。
- `date`、`timezone`、`summary`、`key_points`、`analysis_text.title`、`analysis_text.sections` 不能为空。
- `date` 必须等于当前 `Asia/Tokyo` 日期。
- `timezone` 必须是 `Asia/Tokyo`。
- API 无返回、空字符串、JSON 解析失败、关键字段缺失、日期不一致时，不生成内容，不发送到任何平台。

测试本地 JSON 校验：

```bash
MARKET_EDITION=morning_close_review python3 market_content_openai.py --edition morning_close_review --raw-response-file /tmp/market_content_valid.json
```

## GitHub AI 开源项目数据源

`github_ai_projects.py` 使用 GitHub REST API `search/repositories` 抓取热门 AI 开源项目，并输出 JSON 与 Markdown。

覆盖关键词：

- `AI Agent`
- `LLM`
- `MCP`
- `RAG`
- `workflow automation`

抓取规则：

- 每个关键词取前 5 个仓库。
- 过滤 `description` 为空的仓库。
- 提取 `name`、`full_name`、`html_url`、`description`、`stargazers_count`、`forks_count`、`language`、`topics`、`updated_at`。
- 按 stars、forks、更新时间和关键词相关度综合排序。
- 最终筛选 3 个项目进入每日市场内容包的 AI 开源项目板块。

配置：

```bash
export GITHUB_TOKEN="<your-github-token>"
```

## 统一素材层

`source_router.py` 将 RSS、GitHub 以及 X、Exa、Jina 路由统一为素材字段，并输出：

```text
market_sources/normalized_materials.json
market_sources/filtered_materials.json
market_sources/source_status.json
```

`RSS_FEEDS` 以逗号分隔配置 RSS / Atom 地址；未设置时会使用默认市场源：
Yahoo Finance、MarketWatch、Federal Reserve、NVIDIA Blog、OpenAI News、CNBC Tech、
The Verge AI。`RSS_ITEMS_PER_FEED` 控制每个源最多读取的条数，默认 8。
来源并行度和跨运行缓存策略集中写在 `config/source_policy.json`：默认并行读取 RSS，
所有素材会按当前 edition 的 `data_cutoff` 过滤；未来时间的素材会被丢弃并计入
`future_items_discarded`。GitHub 共享缓存默认关闭，避免上一轮运行的项目混入当前 run；
只有显式设置 `GITHUB_SHARED_CACHE_ENABLED=true` 且缓存未过期时才允许复用。
X、Exa、Jina 未接入时会明确记录
`unavailable` 及原因，不会把缺失来源当作成功采集。Shadow 运行的素材文件位于
`outputs/shadow/<run_id>/market_sources/`。

项目本身不安装 cron 或 launchd 调度器。当前定时任务由 Codex automation 负责调用上述
两个统一入口；如迁移到其他机器，应只保留一个调度来源，避免 Codex automation、cron、
launchd 和 Docker 同时触发同一版本。

## 运行完整流程

```bash
uv run python main.py --edition evening_premarket_watch
```

新的入口支持隔离运行和断点恢复：

```bash
uv run python main.py --edition evening_premarket_watch --shadow-run --raw-response-file tests/fixtures/market_content_valid_20260719.json
uv run python main.py --edition evening_premarket_watch --resume <run_id>
uv run python main.py --edition evening_premarket_watch --resume <run_id> --from-step final_validation
```

## 已恢复的安全功能层

- `source_router.py` 通过 Agent Reach 的 X、Exa、Jina、RSS 和 GitHub 路由生成统一素材；每个路由的成功、部分可用或不可用状态都会写入 `market_sources/source_status.json`。
- `function_calling/` 提供白名单业务函数、Pydantic 参数校验、调用次数限制和统一结果；外部发布不在可调用注册表中。
- `reviewer_agent.py` 在文本 QA 和最终质量门禁通过后只读复核内容和来源，写入 `runtime/reviews/<run_id>/review_result.json`。Reviewer 不能改写原始 artifact，也不能批准发送。
- `evals/` 是离线确定性评测入口，只读取 fixture/历史样本，报告中的 `delivered` 固定为 `false`。

## Phoenix / OpenTelemetry

Phoenix UI 作为独立 Docker 服务运行，app 只使用 OpenTelemetry 客户端依赖，不把 Phoenix 服务端 Python 依赖塞进本机 Python 3.14 环境：

```bash
docker compose up -d phoenix
# UI: http://127.0.0.1:6006
PHOENIX_TRACING_ENABLED=true \
PHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:4317 \
uv run python main.py --edition morning_close_review --shadow-run --raw-response-file tests/fixtures/market_content_morning_valid_20260719.json
```

本地运行日志中的 `trace.jsonl` 是 Phoenix 不可用时的审计后备；Phoenix 不可用不会阻断内容流程。当前运行策略明确禁止 `--enable-images`，图片渲染模块仅保留用于离线回归测试。

## Obsidian 本地连接

Obsidian 使用本地 Markdown vault，不需要云端 API。`.env` 中配置：

```bash
OBSIDIAN_VAULT_PATH=/Users/ara/Documents/Ara-Knowledge
OBSIDIAN_CODEX_FOLDER=Codex
```

检查连接：

```bash
python obsidian_adapter.py --health
```

写入一条测试笔记：

```bash
python obsidian_adapter.py --write-test
```

默认会写入 `Ara-Knowledge/Codex/`，Obsidian 打开 vault 后会自动显示这些 Markdown 笔记。

## Docker

```bash
docker compose build app
docker compose run --rm app uv run pytest -q
docker compose up -d phoenix
```

容器不安装 Ollama；app 通过 `host.docker.internal:11434` 访问宿主机。若宿主机 Ollama 只监听 `127.0.0.1`，Docker 内连接会失败，需要人工将 Ollama 配置为可被 Docker Desktop 网关访问，不能在项目里伪造健康状态。

Shadow 运行写入 `outputs/shadow/<run_id>/`，日志写入 `logs/shadow/<run_id>/`，状态和审核包写入 `runtime/shadow/<run_id>/`。旧的 Canary 运行记录仅作为历史审计保留，不会触发发布。

每个步骤的状态、错误和 artifact hash 写入状态文件和 `outputs/.../logs/steps.jsonl`。恢复时会校验文件存在性、非空、运行 ID 和 SHA-256；损坏的产物会从对应步骤及下游重新执行。

## Self-Healing Canary

Self-Healing V1 只允许执行预先定义的低风险修复：Ollama 健康检查与单次启动、采集器的有限退避重试、缺失行情的定向重采与校验，以及 Gemini JSON 的结构化修复/规则模板回退。它不会修改 Python、Schema、Renderer、Prompt、发布规则或正式状态，也不会自动发送。

运行隔离的五类故障 fixture 验收：

```bash
SELF_HEALING_CANARY_MODE=true SELF_HEALING_FAULT=none \
  uv run python -m self_healing.canary --fixture
```

故障注入只在 `SELF_HEALING_CANARY_MODE=true` 时允许；生产入口如果检测到非 `none` 的 `SELF_HEALING_FAULT`，会在启动阶段拒绝执行。Canary 修复检查点写入 `state/canary/repairs/`，测试输出写入 `outputs/shadow/`，汇总报告写入 `reports/canary_self_healing_report.json`。所有结果都强制 `delivered=false`。

fixture 报告中的 `fixture_ready=true` 只表示受控策略验收通过；在没有真实环境 Canary 记录前，`production_ready` 会保持 `false`。

真实业务链路的非发布 Shadow dry-run 示例：

```bash
uv run python main.py \
  --edition evening_premarket_watch \
  --provider rule_template \
  --dry-run \
  --shadow-run \
  --run-id market_YYYYMMDD_HHMM \
  --raw-response-file tests/fixtures/market_content_valid_20260719.json
```

该命令会执行内容、文字 QA 和审核入口，只写入本地分析产物，不包含外部发布动作。

Agent V1 可选启用模型辅助规划：

```bash
AGENT_PLANNER_MODE=hybrid AGENT_PLANNER_PROVIDER=ollama \
uv run python main.py --edition evening_premarket_watch --shadow-run --run-mode shadow_canary
```

模型只允许建议已注册 Function，工具参数仍由 Executor 校验；模型输出异常、未知工具或越过门禁时自动回到规则 Planner。只读查看稳定性窗口：

```bash
uv run python scripts/canary_status.py
uv run python scripts/canary_status.py --json
```

如果需要同时验收图片链路：

```bash
uv run python main.py \
  --edition evening_premarket_watch \
  --provider rule_template \
  --enable-images \
  --shadow-run \
  --run-id market_YYYYMMDD_HHMM
```

生产放行前运行预检：

```bash
uv run python -m production_preflight --run-id <shadow_run_id>
```

预检是 fail-closed 的。只有发送策略、生产更新开关、图片生成与 QA、发布适配器、真实环境 Canary，以及指定 Shadow 运行全部满足条件时才返回 `ready=true`；当前文本模式会明确报告图片链路和发布适配器缺失，不会误判为生产可用。

发送授权还要求：非 dry-run、邮件适配器健康、运行 ID 与 artifact hash 匹配、人工批准人和未过期批准记录。`delivery_gate.py` 默认不发送；必须由明确配置的发布流程显式调用适配器。

当前邮件渠道使用 SMTP。凭证只通过环境变量提供，不写入仓库：

```bash
export DELIVERY_SMTP_HOST="smtp.example.com"
export DELIVERY_SMTP_PORT="587"
export DELIVERY_SMTP_USERNAME="sender@example.com"
export DELIVERY_SMTP_PASSWORD="<secret>"
export DELIVERY_EMAIL_FROM="sender@example.com"
export DELIVERY_EMAIL_TO="receiver@example.com"
```

完成真实批准后，使用独立发送入口：

```bash
uv run python -m deliver_run \
  --run-id <production_run_id> \
  --approval-file <approval.json> \
  --confirm-production-send
```

该入口拒绝 Shadow/Canary 运行，并要求图片 QA、策略、SMTP 健康、批准记录和 artifact hash 全部匹配。

执行顺序：

1. 生成并校验市场内容 JSON。
2. 刷新 GitHub AI 项目数据；失败时记录日志，不虚构项目数据。
3. 生成平台文字与市场分析。
4. 执行文本一致性、来源和质量门禁。
5. 任一步失败即返回非零退出码，阻止正式发送。

## 系统健康报告

单独运行实时健康检查：

```bash
python3 healthcheck.py
```

输出文件：

- `reports/system_health.md`
- `reports/system_health.json`

健康检查会读取 Ollama、Gemini、Docker 和 Phoenix 的实际状态，并从
`logs/task_runs.jsonl` 和运行日志计算任务成功率、失败次数、Fallback 次数和平均运行时间。服务不可用时记录具体阻断原因，不再写入“未检查”。每日构建成功或失败后也会自动刷新报告。

## outputs 目录

`outputs/` 保存生成的 JSON 和 Markdown 产物，只作为本地运行结果使用，不提交到 Git。需要重新生成时运行对应脚本即可。

## L6-5 版本发布、灰度与回滚

发布控制默认只做本地预检和离线演练，不启用邮件、外部投递、Keychain 写入或 LaunchAgent 安装：

```bash
python3 tools/release.py prepare --version <version> --allow-dirty
python3 tools/release.py preflight --version <version>
python3 tools/release.py verify-package releases/packages/market-pipeline-<version>.tar.gz
python3 tools/release.py drill
python3 tools/release.py status
```

`promote` 默认只输出预览；灰度或正式激活必须提供人工批准、角色、理由和至少两次 Canary 结果。`rollback` 同样需要人工批准。发布锁、SQLite release 字段、checkpoint 版本兼容性和部署漂移检查会 fail-closed；历史输出、运行中的任务和数据库不会被发布或回滚操作删除。
