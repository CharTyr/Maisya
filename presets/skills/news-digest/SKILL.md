---
name: news-digest
description: 资讯简报标准与验真技能：一站式 collect 采编、发现一手 RSS 源、硬门槛与五维精选打分、中文采编规范、封面自动过滤与主色提取，以及生成离线天光简报卡。
---

# 资讯简报工作流方法论

## 0. 高效四步采集流
1. 读当前会话与会话手账偏好（`chat_read` / `message_read` / `memory_read`），明确近期兴趣、读者背景、正负反馈与应避开的已推/聊透话题。
2. 将当前关注主题拆分为 **3~4 个不同子方向**（如：新作发布与实机进展、开发者日志与机制拆解、口碑销量与在线硬数据、垂直媒体专访与工具引擎动态），构造 **8~12 组中英文具体短词**（2~6 个词，避免宽泛主页词），**一次调用** `digest_feed(action="collect", queries=[...], topics=[...], max_items=20)` 完成：冷启动自动试订精选初始源、轮询订阅、多引擎广搜、程序化剔除首页/年月归档/百科、并发抓取原文 `source_text` 并缓存完整正文至工作区池 `digest_pool.json`、提取精确发布时间、`image_url` 与 `has_numbers` 标记，以及独立博客 RSS 顺手试订。无需再手工循环十几次单引擎搜索；若当前活跃订阅不足 6 个，可按需顺手调用一次 `seed` 补全同类一手源。
3. 目标尽量精选 **3~5 条**高信噪比条目（宁缺毋滥，若池子不足 3 条高分内容不硬凑），按下方采编规范撰写中文 `title` / `brief`（110~180 字，2~4 句）/ `why`（<= 55 字）/ `topic` / `source`、抽取可逐字命中的 `quote` 并评五维分，交给 `digest_feed(action="score_select", max_items=5)`（`collect` 己缓存原文，提交时**无需在参数里重复粘贴长段 `source_text`**，把输出空间留给丰富中文摘要和 `viz` 图解）；优先保留 `collect` 返回的 `image_url`，当 `has_numbers` 为真且原文确有可核实数据对比/里程碑时（尤其无封面配图的条目）积极附带轻量 `viz` 图解。
4. 若有入选，执行 `digest_feed(action="render_card_html", group_title="<会话名称或专题>", slot_label="<时段>", max_items=5, output_path="out/digest-card.html")` → `html_render(html_path="out/digest-card.html", output_path="out/digest-card.png", width=600, selector=".card")` → `result_attach(paths=["out/digest-card.png"])`；零条入选时安静收尾。

## 1. 找源与试订纪律
- 冷启动无订阅源且给出关注话题时，`collect` 或 `seed` 会从精选官方/一手源目录按关键词精准消歧（例如区分独立开发/开源工具与独立游戏）后并发试订最多 4 个 `trial` 源；抓取中遇到带 `<link rel="alternate">` 的独立博客也会顺手试订最多 1 个优质源。
- 也可手动用 `digest_feed(action="discover", url=...)` 探测作者博客、项目发布日志、Substack / Buttondown 个人通信并进入 14 天试用期；退订支持传入 `feed_id`、规范化 URL、来源名称或域名。
- 拒绝全站型泛社区或高噪声聚合域名（大型代码托管平台全站、大众问答/短视频/社交信息流），也不把评论 RSS 当正源；**同时注意，权威垂直媒体的深度专访、评测拆解与数据盘点属于一手采访/原创报道，不应当成二手水文误杀**。
- 试用期每轮最多取 2 条候选；14 天内满 10 条候选仍零入选、到期零入选、累计净无用反馈 >= 2，或连续 3 天抓取失败 >= 5 次会自动退订；非 RSS 来源在 30 天内跨越至少 3 个自然日积累 4 条均分 >= 4.0 且获正向互动时会自动纳管试订。

## 2. 中文采编硬规范与逐字核验
- **标题 `title`**：必须翻译或改写为自然通顺的中文标题（保留关键专有名词，如 `Rust` / `Cloudflare` / `Claude` / `Steam`）。
- **事实摘要 `brief`（必填，建议 110~180 字，2~4 句）**：必须写 2~4 句有内容、自然口语化的中文事实摘要，讲透「这是什么作品或事件 / 核心玩法或技术机制亮点 / 这次最新进展与关键数字」，让读者不用点进原文也能看明白来龙去脉；禁止一句话干瘪梗概或复制英文原文（缺少 `brief` 与 `summary` 会被 `score_select` 直接拒收）。
- **点睛推荐 `why`（必填，<= 55 字）**：每条必须填写 1 句中文点睛，说明为什么值得当前读者关心或讨论。
- **话题标签 `topic`（<= 6 字）**：用 2~4 字中文短词标注（如 `系统工程` / `AI工具` / `开源协议`）。
- **干净来源 `source`（<= 20 字符）**：只填干净的媒体名或域名（如 `Cloudflare Blog`、`simonwillison.net`），严禁填大模型评语或内部打分数字。
- **原文核验**：先核对 `collect` 抓回的原文或 `web_fetch` 补抓内容，不靠搜索摘要拍脑袋；每个候选都给出可逐字命中原文的 `quote`（`collect` 已缓存 `source_text` 至候选池，`score_select` 会对照缓存池或参数里的 `source_text` 自动验真）。
- **图解严格防幻觉**：当原文确有两组以上清晰可对比数值、排行或阶段节点（`has_numbers == true`）时，**尤其对于缺少封面配图的条目**，积极附带 `viz`（仅用标准 `viz_spec` 组件：`stat` / `stats` / `compare` / `bars` / `line` / `share` / `timeline` / `table` / `list` / `takeaway`；注意：`bars`/`share`/`stats`/`timeline` 均使用 `items` 数组，其中 `timeline` 每项含 `time`+`title`、`share` 每项含 `label`+非负 `value`），图解中出现的所有数值必须能在 `source_text` 找到依据。`score_select` 校验图解失败时只会丢弃该条图解，不会误杀新闻本体。
- **时效与去噪红线**：新闻类超过 7 天直接淘汰；教程/指南类缺明确发布时间或超过 60 天直接淘汰。付费墙、纯转发二手水文、首页或 `/2026/` 索引页、百科词条、未命中高相关兴趣的政务/司法宣传、近 14 天已出现过或近 3 天同话题已达 3 条上限的内容一律过滤。每轮教程/指南类最多保留 1 篇；同件事只保留信息量与一手度最高的一篇。

## 3. 五维核心分 + 探索视野
- 五项核心分（1~5 分，共同算平均值 `avg`）：
  - `info`（信息密度与硬干货）
  - `source`（一手程度与信源可信度）
  - `relevance`（与当前会话讨论或会话手账关注点的关联度）
  - `timeliness`（时效新鲜度）
  - `chat`（是否带得动接话和讨论）
- 另评两项独立信号（不计入 `avg`）：
  - `novelty <= 2` 直接淘汰（刚刚聊过或众人皆知的内容不推）；
  - `surprise >= 4` 且 `info >= 4`、`chat >= 4`、`timeliness >= 3`，即使 `relevance` 仅 2 分，只要附带一句自然过渡说明 `bridge`，也可申请 0~2 个探索视野席位。

## 4. 封面配图与卡片渲染交付礼仪
- 优先保留 `collect` 返回的 `image_url`（或文章页 `og:image`/`twitter:image`），调用 `digest_feed(action="render_card_html", group_title="<会话名称或专题>", slot_label="<时段>", max_items=5, output_path="out/digest-card.html")`。
- `render_card_html` 会自动并发抓图、过滤站点 Logo / 小图标 / `.svg` / 纯色或纯底大字横幅，从合格封面提取主色并生成固定 `600px` 宽的五档天光视觉卡片（`2x` 缩放后输出 `1200px` 高清图）。
- 随后固定调用 `html_render(html_path="out/digest-card.html", output_path="out/digest-card.png", width=600, selector=".card")` 截图，并通过 `result_attach(paths=["out/digest-card.png"])` 随结果回传。
- **图文分离、宁缺毋滥**：图片已承载完整标题、摘要与看点，文字回复尽量简洁（只附条目编号/标题与原文链接，不与卡片重复长篇摘要）；零条入选时安静结束，不为了凑数降门槛。
