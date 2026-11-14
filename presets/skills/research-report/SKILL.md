---
name: research-report
description: 深度调研与结构化图文报告制作技能：规范联网检索核证、source_text 数字诚实校验，以及 render_report 网页/长图双模交付。
---

# 调研与可视化报告工作规范

本技能用于将多来源检索结论、对比数据与本地插图整理为结构化、可审计、零外部网络依赖的独立 HTML 报告或群聊长图卡片。

## 1. 证据采集与整理流程

1. 先用 `workspace_file(action="list")` 查看 `in/` 与 `project/` 中是否已有上游资料或历史草稿。
2. 使用 `web_search` 与 `web_fetch` 采集一手公告、官方文档、财报/基准测试原文及时间戳，将原始关键摘录保存在 `project/` 笔记中。
3. 若需配图或脚本生成图表，图片文件必须落在工作区 `in/`、`project/` 或 `out/` 目录内，且为合法 PNG、JPEG、WebP 或 GIF（单张不超过 3 MiB、全篇累计不超过 8 MiB）；严禁在 `image` 块中填入 `http://`/`https://` 外链或 `..` 路径。

## 2. 数据诚实纪律（`source_text` 核验）

- 在调用 `render_report` 时，把本次已核实的一手原文关键数据片段汇总传入 `source_text`。
- 展示内容（含标题、副标题、指标值、对比要点、图表数值、表格、标签页正文等）中**所有大于 10 的数字**，都必须在 `source_text` 中有对应原型或四舍五入容差命中。
- 严禁脑补未查证的百分比、排名、年份、价格或估值；缺失数字宁可留空、写“未披露”或改为定性描述，也绝不编造。

## 3. 版式与组件编排

按阅读需要组合 `blocks`（最多 64 个区块）：
- 叙事与说明：`heading`、`text`、`note`、`sources`、`image`
- 指标与结构对比：`stat`、`stats`、`compare`、`bars`、`line`、`share`、`timeline`、`table`、`list`、`takeaway`、`tabs`

根据交付场景选择 `layout`（支持 `theme="light"` 或 `theme="dark"`）：

- **`layout="page"`（独立交互网页报告）**：
  适合篇幅较长、含 `tabs` 多视图切换或需要网页浏览/保存的完整调研报告。直接输出自包含 HTML5 文档（`out/*.html`），可再用 `result_attach` 交付网页附件或给出清晰结论摘要。
- **`layout="card"`（780px 静态长图卡片）**：
  适合发往 QQ / 群聊的精选一页图解或结论卡片。此模式会自动展开所有 `tabs` 为连续章节、禁用任何截断滚动并移除全部 `<script>`。先用 `render_report(layout="card", path="out/<name>.html", ...)` 生成卡片网页，再调用 `html_render(html_path="out/<name>.html", output_path="out/<name>.png", width=780)` 截成长图，最后通过 `result_attach` 附带 `out/<name>.png` 发出。

## 4. 交付自检

- 核对 `sources` 列表列明了真实参考出处（标题、日期/说明、URL）。
- 确认 `out/` 下的 `.html` 与 `.png` 产物非空且已用 `result_attach` 显式附带所需交付物。
- 输出结论时只陈述实际查证与工具返回的结果，不擅自加码夸大或捏造未核实数字。
