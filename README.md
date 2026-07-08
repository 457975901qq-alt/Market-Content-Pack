# 每日市场内容包

本项目生成中文每日市场内容包图片简报，并把平台发布文案作为线程文字单独发送。

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

输出文件：

- `outputs/market_content/market_content.json`
- `outputs/market_content/douyin.md`

错误日志：

- `logs/market_content_errors.log`

校验规则：

- API 返回后先解析 JSON；解析失败会停止流程。
- `date`、`timezone`、`summary`、`key_points`、`image_text.title`、`image_text.sections` 不能为空。
- `date` 必须等于当前 `Asia/Tokyo` 日期。
- `timezone` 必须是 `Asia/Tokyo`。
- API 无返回、空字符串、JSON 解析失败、关键字段缺失、日期不一致时，不生成图片，不发送到任何平台。

测试本地 JSON 校验：

```bash
python3 market_content_openai.py --raw-response-file /tmp/market_content_valid.json
```

成功路径测试示例：

```bash
python3 market_content_openai.py --raw-response-file /tmp/market_content_tests/valid.json
python3 render_market_pack_calm_20260708.py
```

失败路径测试示例：

```bash
python3 market_content_openai.py --raw-response-file /tmp/market_content_tests/empty.json
python3 market_content_openai.py --raw-response-file /tmp/market_content_tests/invalid_json.txt
python3 market_content_openai.py --raw-response-file /tmp/market_content_tests/missing_fields.json
python3 market_content_openai.py --raw-response-file /tmp/market_content_tests/wrong_date.json
python3 market_content_openai.py --raw-response-file /tmp/market_content_tests/blank_content.json
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
- 最终筛选 3 个项目进入每日市场内容包的“AI开源项目”板块。

## 配置 GITHUB_TOKEN

不要把 API Key 写入代码或提交到仓库。运行前在 shell 环境中配置：

```bash
export GITHUB_TOKEN="<your-github-token>"
```

建议使用只读权限 token。当前脚本只调用公开的 GitHub REST API 搜索接口，不需要仓库写权限。

## 运行

完整运行每日市场内容包：

```bash
bash run_market_content_pack.sh
```

只刷新 GitHub AI 项目数据：

```bash
python3 github_ai_projects.py
```

输出文件：

- `outputs/github_ai_projects/ai_open_source_projects.json`
- `outputs/github_ai_projects/ai_open_source_projects.md`

生成每日市场内容包图片：

```bash
python3 build_daily_market_pack.py
```

`build_daily_market_pack.py` 会先尝试刷新 GitHub 数据源，再渲染图片。如果 `GITHUB_TOKEN` 缺失或 GitHub API 调用失败，会写入错误日志，并用 fallback 项目继续生成图片。

## 定时运行

项目提供 `run_market_content_pack.sh` 作为统一运行入口。脚本会：

- 自动进入项目目录。
- 读取本地 `.env`（如果存在）。
- 使用 `PYTHON_BIN` 指定的 Python，默认 `/usr/bin/python3`。
- 运行 `build_daily_market_pack.py`。
- 使用 `tmp/market_content.lock` 防止并发触发。
- 将输出写入 `logs/scheduler_run.log`。

launchd 配置文件：

```text
~/Library/LaunchAgents/com.market.content.pack.plist
```

由于当前项目位于 macOS 受隐私保护的 `Documents` 目录，LaunchAgent 可能无法读取项目文件或写入项目 `logs/`。如果 `logs/launchd_stderr.log` 出现 `Operation not permitted`，需要二选一：

- 在系统设置中给 `/bin/bash` 和 `/usr/bin/python3` 授权 Full Disk Access。
- 将项目迁移到非隐私保护目录，例如 `~/market-content-pack`，并同步更新 `run_market_content_pack.sh` 和 plist 中的项目路径。

每天触发时间：

- 06:30 Asia/Tokyo
- 17:30 Asia/Tokyo

加载和测试：

```bash
launchctl load ~/Library/LaunchAgents/com.market.content.pack.plist
launchctl start com.market.content.pack
launchctl list | grep market
```

结果检查：

```bash
tail -n 100 logs/scheduler_run.log
tail -n 100 logs/market_content_errors.log
ls -lh outputs/market_content/
```

## outputs 目录

`outputs/` 保存生成的 JSON、Markdown 和图片产物，只作为本地运行结果使用，不提交到 Git。需要重新生成时运行对应脚本即可。

## 错误日志

错误日志路径：

```text
logs/error.log
logs/scheduler_run.log
logs/market_content_errors.log
```

日志可能包含错误堆栈和失败关键词，但不会记录 `GITHUB_TOKEN`。
