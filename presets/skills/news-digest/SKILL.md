---
name: news-digest
description: 资讯简报标准与验真技能：一站式 collect 采编、发现一手 RSS 源、硬门槛与五维精选打分、原文核验的数据图解，以及生成离线简报卡。
---

# 资讯简报工作流方法论

## 0. 高效四步采集流
1. 读群聊与手账偏好（`chat_read` / `message_read` / `memory_read`），明确近期兴趣与应避开的已聊透话题。
2. 构造多角度中英文具体检索词（如具体发布事件、版本更新、论文方法、开发者长文关键词，避免宽泛主页词），**一次调用** `digest_feed(action="collect", queries=[...], topics=[...])` 完成：冷启动自动试订精选初始源、轮询订阅、多引擎广搜、程序化剔除首页/年月归档/百科、并发抓取原文 `source_text`、精确发布时间与 `has_numbers` 标记，以及独立博客 RSS 顺手试订。无需再手工循环十几次单引擎搜索。
3. 对精选候选逐字抽取 `quote`、评五维分；仅在 `has_numbers` 为真且确有必要可视化时附带克制的 `viz` 图解对象，交给 `digest_feed(action="score_select")`。
4. 若有入选，执行 `digest_feed(action="render_card_html")` → `html_render` → `result_attach`；零条入选时安静收尾。

## 1. 找源与试订纪律
- 冷启动无订阅源且给出关注话题时，`collect` 或 `seed` 会从精选官方/一手源目录自动试订最多 4 个 `trial` 源；抓取中遇到带 `<link rel="alternate">` 的独立博客也会顺手试订最多 1 个优质源。
- 也可手动用 `digest_feed(action="discover", url=...)` 探测作者博客、项目发布日志、Substack / Buttondown 个人通信并进入 14 天试用期。
- 拒绝全站型泛社区或高噪声聚合域名（大型代码托管平台全站、大众问答/短视频/社交信息流），也不把评论 RSS 当正源。
- 试用期每轮最多取 2 条候选；14 天内满 10 条候选仍零入选、到期零入选、累计净无用反馈 >= 2，或连续 3 天抓取失败 >= 5 次会自动退订；非 RSS 来源在 30 天内跨越至少 3 个自然日积累 4 条均分 >= 4.0 且获正向互动时会自动纳管试订。

## 2. 逐字核验与硬过滤规则
- 先核对 `collect` 抓回的原文或 `web_fetch` 补抓内容，不靠搜索摘要拍脑袋；每个候选都给出真实 `source_text` 片段和可逐字命中的 `quote`。
- **图解严格防幻觉**：只有当原文确有两组以上清晰可对比数值、排行或阶段节点（`has_numbers == true`）时才附带 `viz`（仅用标准 `viz_spec` 组件：`stat` / `stats` / `compare` / `bars` / `line` / `share` / `timeline` / `table` / `list` / `takeaway`；注意：`bars`/`share`/`stats`/`timeline` 均使用 `items` 数组，其中 `timeline` 每项含 `time`+`title`、`share` 每项含 `label`+非负 `value`），图解中出现的所有数值必须能在 `source_text` 找到依据。`score_select` 校验图解失败时只会丢弃该条图解，不会误杀新闻本体。
- **时效红线**：新闻类超过 7 天直接淘汰；教程/指南类缺明确发布时间或超过 60 天直接淘汰。
- **拒绝水货与硬广**：付费墙、纯转发二手水文、首页或 `/2026/` 索引页、百科词条、未命中高相关兴趣的政务/司法宣传、近 14 天已出现过或近 3 天同话题已达 3 条上限的内容一律过滤。
- 每轮教程/指南类最多保留 1 篇；若同件事有多家报道，只保留信息量与一手度最高的一篇。

## 3. 五维核心分 + 探索视野
- 五项核心分（1~5 分，共同算平均值 `avg`）：
  - `info`（信息密度与硬干货）
  - `source`（一手程度与信源可信度）
  - `relevance`（与当下讨论或个人手账关注点的关联度）
  - `timeliness`（时效新鲜度）
  - `chat`（是否带得动接话和讨论）
- 另评两项独立信号（不计入 `avg`）：
  - `novelty <= 2` 直接淘汰（刚刚聊过或众人皆知的内容不推）；
  - `surprise >= 4` 且 `info >= 4`、`chat >= 4`、`timeliness >= 3`，即使 `relevance` 仅 2 分，只要附带一句自然过渡说明 `bridge`，也可申请 0~2 个探索视野席位。

## 4. 交付礼仪
- 把精选条目的正文与配图规范渲染进工作区 `out/digest-card.html` -> `out/digest-card.png` 并 `result_attach`，简短列出标题、看点和原文链接。
- **宁缺毋滥**：零条入选时安静结束，不为了凑数降门槛。
