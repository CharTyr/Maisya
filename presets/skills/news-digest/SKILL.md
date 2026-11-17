---
name: news-digest
description: 资讯简报标准与验真技能：一站式 collect 采编、发现一手 RSS 源、硬门槛与五维精选打分、中文采编规范、封面自动过滤与主色提取，以及生成离线天光简报卡。
---

# 资讯简报工作流方法论

## 0. 高效四步采集流
1. 读当前会话与历史偏好：`<memory_context>`（`MEMORY.md`、`chat_title`、`chat_type`）与 `<methods>`（`NOTES.md`、`SKILL.md`）已直接注入在上下文中，**不要再用 `workspace_file` 查找它们**。先调 `chat_read(limit=80, hours=168)`（自动按一周跨时段抽样并过滤纯表情水贴）摸底真实最近讨论，再结合宽泛检索 `memory_read(query="近期讨论话题 兴趣 关注 作品 日常", limit=12)` 梳理完整多元兴趣版图、正负反馈与应避开的已推/聊透话题；旧 `MEMORY.md` 仅作参照，若真实聊天呈多领域分布，按近期讨论实际比重全面覆盖，勿锁死在单一垂直领域。
2. 将提炼出的关注面拆分为 **3~4 个不同子方向**（若成员偏好跨多个领域，按兴趣比例跨领域拼盘分配子方向，不挤进单一细分品类；子方向示例：一手发布与版本动态、深度拆解与复盘、社区/行业硬数据、文化/工具/作品新动向），构造 **8~12 组中英文具体短词**（2~6 个词，避免宽泛主页词），**一次调用** `digest_feed(action="collect", queries=[...], topics=[...], max_items=20)` 完成：冷启动自动试订精选初始源、轮询订阅、多引擎广搜、程序化剔除首页/年月归档/百科、并发抓取原文 `source_text` 并缓存完整正文至工作区池 `digest_pool.json`、提取精确发布时间、`image_url` 与 `has_numbers` 标记，以及独立博客 RSS 顺手试订。无需再手工循环十几次单引擎搜索；**若某个子方向素材不够需要补搜，请直接再调用一次 `digest_feed(action="collect", queries=[...])`（6 小时池会自动合并缓存正文），不要直接把裸 `web_search` 摘要当原文提报**；若当前活跃订阅不足 6 个，可按需顺手调用一次 `seed` 补全同类一手源。
3. 目标是在池子素材充足时稳定成卡 **4~5 条**高信噪比条目（宁缺毋滥，仅在池子确实不足时少选）。为避免个别候选因历史去重或核验被过滤导致成卡偏少，请在池子充足时**在单次 `digest_feed(action="score_select", max_items=5)` 调用中一次提交 `5~7` 条高分候选作为筛选余量**；按下方采编规范撰写纯正通顺的中文 `title` / `brief`（110~180 字，2~4 句）/ `why`（<= 55 字）/ `topic` / `source`、抽取**保持原文语言逐字复制的 `quote`（中文原文复制中文原句、英文原文复制英文原句，严禁跨语言翻译）**并评五维分（`collect` 已缓存原文，提交时**无需在参数里重复粘贴长段 `source_text`**）；优先保留 `collect` 返回的 `image_url`，仅当**缺少封面配图（`image_url` 为空）且原文具备 `>= 2` 组可并列对比数值或时间线节点**时才附带轻量 `viz` 图解。
4. 若有入选，优先用 `<memory_context>` 提供的 `chat_title`（如 `<chat_title> · 近期关注`）或中性专题抬头（如 `近期关注简报`，勿替综合兴趣会话擅自贴单一垂直标签）作为 `group_title`，执行 `digest_feed(action="render_card_html", group_title="<会话抬头或专题>", slot_label="<时段>", max_items=5, output_path="out/digest-card.html")` → `html_render(html_path="out/digest-card.html", output_path="out/digest-card.png", width=600, selector=".card")` → `result_attach(paths=["out/digest-card.png"])`；零条入选时安静收尾。

## 1. 找源与试订纪律
- 冷启动无订阅源且给出关注话题时，`collect` 或 `seed` 会从精选官方/一手源目录按关键词精准消歧（例如区分独立开发/开源工具与独立游戏）后并发试订最多 4 个 `trial` 源；抓取中遇到带 `<link rel="alternate">` 的独立博客也会顺手试订最多 1 个优质源。
- 也可手动用 `digest_feed(action="discover", url=...)` 探测作者博客、项目发布日志、Substack / Buttondown 个人通信并进入 14 天试用期；退订支持传入 `feed_id`、规范化 URL、来源名称或域名。
- 拒绝全站型泛社区或高噪声聚合域名（大型代码托管平台全站、大众问答/短视频/社交信息流），也不把评论 RSS 当正源；**同时注意，权威垂直媒体的深度专访、评测拆解与数据盘点属于一手采访/原创报道，不应当成二手水文误杀**。
- 试用期每轮最多取 2 条候选；14 天内满 10 条候选仍零入选、到期零入选、累计净无用反馈 >= 2，或连续 3 天抓取失败 >= 5 次会自动退订；非 RSS 来源在 30 天内跨越至少 3 个自然日积累 4 条均分 >= 4.0 且获正向互动时会自动纳管试订。

## 2. 中文采编硬规范与逐字核验
- **标题 `title`**：必须翻译或改写为纯正自然、口语通顺的中文标题（只保留必要的专有名词、作品名或技术缩写，如 `Rust` / `Cloudflare` / `Blender` / `Steam` / `Claude`；**严禁出现半英半中的普通名词/动词夹杂**，如禁止写 `"重 engineering"`，应写 `"重工程"` 或 `"重引擎开发"`）。
- **事实摘要 `brief`（必填，建议 110~180 字，2~4 句）**：必须写 2~4 句有内容、自然口语化的中文事实摘要，讲透「这是什么作品或事件 / 核心机制或技术亮点 / 这次最新进展与关键数字」，让读者不用点进原文也能看明白来龙去脉；禁止一句话干瘪梗概或复制英文原文（缺少 `brief` 与 `summary` 会被 `score_select` 直接拒收）。
- **点睛推荐 `why`（必填，<= 55 字）**：每条必须填写 1 句中文点睛，说明为什么值得当前读者关心或讨论。
- **话题标签 `topic`（<= 6 字）**：用 2~4 字中文短词标注（如 `系统工程` / `AI工具` / `开源协议`）。
- **干净来源 `source`（<= 20 字符）**：只填干净的媒体名或域名（如 `Cloudflare Blog`、`simonwillison.net`），严禁填大模型评语或内部打分数字。
- **原文核验**：先核对 `collect` 抓回的原文或 `web_fetch` 补抓内容，不靠搜索摘要拍脑袋；每个候选的 `quote` **必须保持与该条 `source_text` 同一语言逐字复制**（中文原文复制中文原句、英文原文复制英文原句，**严禁将中文原文翻译成英文或改写词句**；`collect` 已缓存 `source_text` 至候选池，`score_select` 会对照缓存池或参数里的 `source_text` 自动验真）。
- **图解严格克制与防幻觉**：仅当条目**缺少封面配图（`image_url` 为空）且原文确有 `>= 2` 项可并列对比数值、前后对比或阶段节点**时再附带 `viz`（有封面配图的条目保持卡片清爽，不叠冗余图解；严禁为单个孤立数字生成 `stats`/`bars` 或无 `delta` 的 `stat`，单指标会被系统自动丢弃）。组件仅用标准 `viz_spec`（键名为 `"type"`：`stats` / `stat`(须带 `delta`) / `compare` / `bars` / `line` / `share` / `timeline` / `table` / `list` / `takeaway`；其中 `bars`/`share`/`stats`/`timeline` 均使用 `items` 数组，数值类 `value` 填有限数字并**显式填好 `"unit"` 或 `"display"`** 避免光秃数字，`timeline` 每项含 `time`+`title`，`share` 每项含 `label`+非负 `value`）。图解中所有数值与单位必须能在 `source_text` 找到依据；`score_select` 校验图解失败时只会丢弃图解，不会误杀新闻本体。
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
- 优先保留 `collect` 返回的 `image_url`（或文章页 `og:image`/`twitter:image`），调用 `digest_feed(action="render_card_html", group_title="<会话抬头或专题>", slot_label="<时段>", max_items=5, output_path="out/digest-card.html")`。
- `render_card_html` 会自动并发抓图、过滤站点 Logo / 小图标 / `.svg` / 纯色或纯底大字横幅，从合格封面提取主色并生成固定 `600px` 宽的五档天光视觉卡片（`2x` 缩放后输出 `1200px` 高清图）。
- 随后固定调用 `html_render(html_path="out/digest-card.html", output_path="out/digest-card.png", width=600, selector=".card")` 截图，并通过 `result_attach(paths=["out/digest-card.png"])` 随结果回传。
- **图文分离、宁缺毋滥**：图片已承载完整标题、摘要与看点，文字回复尽量简洁（只附条目编号/标题与原文链接，不与卡片重复长篇摘要）；零条入选时安静结束，不为了凑数降门槛。
