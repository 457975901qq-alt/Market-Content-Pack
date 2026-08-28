import json
import tomllib
from pathlib import Path


path = Path("/Users/ara/.codex/automations/automation/automation.toml")
data = tomllib.loads(path.read_text())

standard = """

用户确认的固定视觉标准：
- 以后以 2026-07-08 重做版为标准：参考用户给的“每日市场内容包”截图，做成白底多卡片财经仪表盘，而不是单张长文海报。
- 默认至少输出 2 张图：第 1 张为三列“每日市场内容包”主仪表盘；第 2 张为 Serenity 今日信息简报。内容较多时继续拆成第 3/N 张。
- 主仪表盘固定包含：今日市场总览、重点板块表现、Top 3 市场催化剂、宏观日历、全球央行、国际事件、大宗商品/加密、资金流、情绪仪表盘、市场结论、来源索引。
- Serenity 专页固定包含：4 格观点卡、AI/科技链条流程图、观点提炼、提及标的热力、验证清单、原帖链接索引。
- 正常读取 GitHub 的日期，GitHub AI 新项目作为主图短模块或独立详情页呈现；不要为了塞 GitHub 压缩 Serenity 或主图可读性。
- 图片里不要出现抖音、小红书、X/Twitter、公众号等平台发布文案或文案摘要；这些平台文案必须单独作为线程文字发送。
- 2026-07-08 用户反馈旧版整体视觉不舒服，后续默认改用“calm 晨报终端”风格：暖灰背景、深海军蓝标题栏、大圆角浅色面板、轻阴影、少边框、少图标、少彩色胶囊；用状态条、仪表盘、时间块和清单框表达信息，避免密密麻麻的小卡片碎片感。
- 每张图优先 5-7 个大模块，不做过多小卡；正文宁可拆图也不要挤在卡片边缘。标题和结论必须完整显示，不使用容易截断的长单行。
- 图像应明显比文字更占视觉权重：每个信息卡优先使用图标、条形图、仪表盘、流程箭头、热力标签、迷你折线、日历格；正文只保留短句。
- 如果生成结果不像参考图，或文字明显多于图形，必须重做后再发送。
"""

if "用户确认的固定视觉标准：" not in data["prompt"]:
    anchor = "图片视觉参考：采用“每日市场内容包”仪表盘风格。"
    data["prompt"] = data["prompt"].replace(anchor, standard + "\n" + anchor)

order = [
    "version",
    "id",
    "kind",
    "name",
    "prompt",
    "status",
    "rrule",
    "target_thread_id",
    "created_at",
    "updated_at",
]

lines = []
for key in order:
    value = data[key]
    if isinstance(value, str):
        lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    else:
        lines.append(f"{key} = {value}")

path.write_text("\n".join(lines) + "\n")
print("updated")
