# 每日市场内容包

本项目生成中文每日市场内容包：文字内容由结构化 JSON 统一管理，图片包由程序渲染，平台配文作为独立文本输出。

## 当前图片包规范

新版图片流程固定输出 **9 张 PNG**：

1. 封面
2. 市场总览
3. 宏观数据与全球央行
4. 大宗商品与地缘政治
5. AI 与半导体
6. 大型科技与重点资产
7. 事件日历与 OPEX
8. ETF 资金流与市场结构
9. GitHub 热门 AI 项目与本周总结

所有图片统一为：

- 1080×1920
- 9:16 竖版
- 暖白橙黑编辑杂志风
- 四周保留安全区
- 数据页统一品牌栏、页码、主标题、一句结论和橙色装饰线
- 数据缺失时显示“待核验”或“暂无可靠数据”，不得编造数值

设计配置集中在：

```text
market_pack_design.py
```

新版渲染器：

```text
render_market_pack_unified.py
```

生成后质量检查：

```text
validate_market_image_pack.py
```

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

## 运行完整流程

```bash
python3 build_daily_market_pack.py
```

执行顺序：

1. 生成并校验市场内容 JSON。
2. 刷新 GitHub AI 项目数据；失败时记录日志，不虚构项目数据。
3. 生成统一标题系统的 9 张图片。
4. 检查页数、顺序、PNG 格式和 1080×1920 尺寸。
5. 任一步失败即返回非零退出码，阻止正式发送。

只重新渲染图片：

```bash
python3 render_market_pack_unified.py
python3 validate_market_image_pack.py
```

图片输出：

```text
outputs/market_image_pack/
```

清单：

```text
outputs/market_image_pack/manifest.json
```

图片错误日志：

```text
logs/market_image_pack_errors.log
```

## 旧版渲染器

`render_market_pack_calm_20260708.py` 暂时保留用于回退和对照，但完整构建入口已切换到 `render_market_pack_unified.py`。

## outputs 目录

`outputs/` 保存生成的 JSON、Markdown 和图片产物，只作为本地运行结果使用，不提交到 Git。需要重新生成时运行对应脚本即可。
