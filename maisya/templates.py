"""即开即用场景模板与跨插件只读 @API 一键安全挂载。"""

from __future__ import annotations

import re
from typing import Any

from .abilities import PROFILE_ID_RE


SCENE_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "researcher",
        "name": "深度调研员",
        "tagline": "多源联网交叉查证、结构化对比表与离线精排长图简报",
        "default_profile_id": "researcher",
        "icon_shape": "compass",
        "theme_color": "#2F6FED",
        "session_mode": "ephemeral",
        "toolsets": ["context", "web", "workspace"],
        "skills": ["source-checking", "research-report"],
        "max_steps": 10,
        "timeout_seconds": 240,
        "try_prompt": "帮我对比一下最近几个热门开源项目的核心差异，整理成一张清晰对比图发出来",
        "instructions": (
            "# 深度调研员\n\n"
            "你是负责多源联网检索、事实核查与精排简报的调研专家。\n\n"
            "## 工作准则\n"
            "- 涉及时效资讯、价格、版本或争议话题时，至少交叉核实两个独立来源，优先一手官方文档或原始发布页。\n"
            "- 结论先行，关键数据与出处清楚对应；信息不足或未查到时如实说明，绝不编造链接与数字。\n"
            "- 需要直观展示多维度对比或要点汇总时，优先用 render_report 或 html_render 渲染离线图卡，并通过 result_attach 挂载交付。\n"
        ),
    },
    {
        "id": "group_digest",
        "name": "群聊日报官",
        "tagline": "持续维护订阅源与群内热议，按固定节奏生成天光简报卡",
        "default_profile_id": "digest",
        "icon_shape": "scroll",
        "theme_color": "#D97706",
        "session_mode": "workspace",
        "toolsets": ["context", "web", "workspace", "schedule"],
        "extra_tools": ["digest_feed"],
        "skills": ["news-digest", "source-checking"],
        "max_steps": 12,
        "timeout_seconds": 300,
        "try_prompt": "帮我把最近一天群里讨论最热的话题和最新订阅资讯整理成一张简报卡",
        "instructions": (
            "# 群聊日报官\n\n"
            "你是负责持续策展与定时简报的编辑角色。\n\n"
            "## 工作准则\n"
            "- 在工作区内维护订阅源与去重记录，避免重复推送已发过的旧消息。\n"
            "- 结合 chat_read 采样与 digest_feed 聚合结果，精选最有信息量的内容，按清晰分区排版。\n"
            "- 使用离线 html_render 生成干净易读的简报卡片并通过 result_attach 挂载待发。\n"
        ),
    },
    {
        "id": "trpg_host",
        "name": "跑团 / 游戏主持人",
        "tagline": "跨轮持久记住角色卡与剧情状态，内置真随机骰点与精确数值结算",
        "default_profile_id": "trpg_host",
        "icon_shape": "star",
        "theme_color": "#7C3AED",
        "session_mode": "persistent",
        "toolsets": ["context", "workspace", "exec"],
        "skills": ["ongoing-workspace"],
        "max_steps": 8,
        "timeout_seconds": 180,
        "try_prompt": "我们开一局五分钟微悬疑跑团，先给我生成一张角色卡并掷一次 1d100 侦查检定",
        "instructions": (
            "# 跑团 / 游戏主持人\n\n"
            "你是负责主持文字跑团、推理游戏与长期互动局的主持人。\n\n"
            "## 工作准则\n"
            "- 所有骰点、随机抽签、血量与数值增减必须通过 calc 工具完成，严禁口算或编造随机结果。\n"
            "- 关键角色卡、物品栏与场景进展保存在工作区状态文件中，跨轮延续不丢设定。\n"
            "- 叙事紧凑有画面感，每轮结尾给出 2 到 3 个自然行动选项方便玩家接招；需要角色卡或地图时可用 html_render 出图。\n"
        ),
    },
    {
        "id": "watcher",
        "name": "定时盯梢员",
        "tagline": "持续追踪版本发布、公告或状态变动，只在真正有变化时主动提醒",
        "default_profile_id": "watcher",
        "icon_shape": "globe",
        "theme_color": "#059669",
        "session_mode": "workspace",
        "toolsets": ["context", "web", "workspace", "schedule"],
        "skills": ["source-checking", "ongoing-workspace"],
        "max_steps": 8,
        "timeout_seconds": 180,
        "try_prompt": "帮我盯一下某个开源项目的最新 Release，有新版本发布时立刻告诉我更新亮点",
        "instructions": (
            "# 定时盯梢员\n\n"
            "你是负责持续监测公开页面、版本发布与关键公告的盯梢角色。\n\n"
            "## 工作准则\n"
            "- 每次检查后将最新版本号、时间戳与关键指纹写入工作区状态文件，下次运行先对比历史快照。\n"
            "- 若相比上次无实质变化，明确回报「暂无新变动」并保持简短，不制造重复噪音。\n"
            "- 检测到真正变化时，给出变化摘要、原始出处链接与影响要点。\n"
        ),
    },
)

_TEMPLATE_BY_ID: dict[str, dict[str, Any]] = {t["id"]: t for t in SCENE_TEMPLATES}

_UNSAFE_API_TOKENS: frozenset[str] = frozenset(
    {
        "delete",
        "del",
        "remove",
        "rm",
        "drop",
        "purge",
        "clear",
        "reset",
        "destroy",
        "wipe",
        "truncate",
        "write",
        "set",
        "put",
        "post",
        "patch",
        "update",
        "upsert",
        "create",
        "add",
        "insert",
        "modify",
        "edit",
        "save",
        "send",
        "broadcast",
        "publish",
        "reply",
        "exec",
        "execute",
        "run",
        "eval",
        "shell",
        "cmd",
        "ban",
        "kick",
        "mute",
        "unmute",
        "revoke",
        "grant",
        "approve",
        "admin",
        "restart",
        "shutdown",
        "stop",
        "kill",
        "upload",
        "import",
        "migrate",
        "pay",
        "transfer",
        "charge",
    }
)

_UNSAFE_API_CN_MARKERS: tuple[str, ...] = (
    "删除",
    "清空",
    "重置",
    "销毁",
    "写入",
    "修改",
    "更新",
    "发送消息",
    "群发",
    "踢出",
    "禁言",
    "封禁",
    "重启",
    "关闭服务",
    "执行命令",
    "转账",
    "扣款",
    "授权",
    "吊销",
)


def is_safe_readonly_api(api_name: str, description: str = "") -> tuple[bool, str]:
    """判断宿主跨插件 @API 名称与说明是否属于可一键挂载的低风险只读接口。"""
    clean = str(api_name or "").strip()
    if not clean or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", clean):
        return False, "跨插件 API 名称格式无效"
    if clean.startswith(("chartyr.maisya.", "maisya_")) or ".maisya_" in clean:
        return False, "不能挂载 Maisya 自身内部接口"
    tokens = set(re.findall(r"[a-z0-9]+", clean.lower()))
    hit_tokens = sorted(tokens & _UNSAFE_API_TOKENS)
    if hit_tokens:
        return (
            False,
            f"接口名称含高危或写操作词（{', '.join(hit_tokens[:3])}），为防止越权，禁止一键按只读挂载；如需管理员高权调用，请在「外部服务」手动审慎登记。",
        )
    desc = str(description or "").strip()
    for marker in _UNSAFE_API_CN_MARKERS:
        if marker in desc:
            return (
                False,
                f"接口说明含写操作或敏感动作（{marker}），禁止一键按只读挂载。",
            )
    return True, ""


def derive_api_handle(api_name: str) -> str:
    """将宿主点分 API 名称规范化为合法的 services.json / profile.apis 句柄。"""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(api_name or "").strip()).strip("_-")
    if not cleaned:
        cleaned = "plugin_api"
    if not cleaned[0].isalpha():
        cleaned = "api_" + cleaned
    return cleaned[:64]


def list_scene_templates(plugin=None) -> list[dict[str, Any]]:
    """返回内置场景模板列表（附加当前是否已有同名角色的标记）。"""
    existing_ids: set[str] = set()
    if plugin is not None and getattr(plugin, "profiles", None) is not None:
        try:
            existing_ids = {p.id for p in plugin.profiles.list()}
        except Exception:
            existing_ids = set()
    items = []
    for tpl in SCENE_TEMPLATES:
        items.append(
            {
                "id": tpl["id"],
                "name": tpl["name"],
                "tagline": tpl["tagline"],
                "default_profile_id": tpl["default_profile_id"],
                "icon_shape": tpl["icon_shape"],
                "theme_color": tpl["theme_color"],
                "session_mode": tpl["session_mode"],
                "toolsets": list(tpl["toolsets"]),
                "skills": list(tpl["skills"]),
                "try_prompt": tpl["try_prompt"],
                "installed": tpl["default_profile_id"] in existing_ids,
            }
        )
    return items


def apply_scene_template(
    plugin,
    template_id: str,
    *,
    profile_id: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """一键套用场景模板：创建新角色或按需更新同名角色并启用。"""
    tid = str(template_id or "").strip()
    tpl = _TEMPLATE_BY_ID.get(tid)
    if tpl is None:
        raise ValueError(
            f"未知场景模板：{tid}（可用：{', '.join(sorted(_TEMPLATE_BY_ID))}）"
        )
    pid = str(profile_id or tpl["default_profile_id"]).strip()
    if not PROFILE_ID_RE.fullmatch(pid):
        raise ValueError("角色编号只能包含字母、数字、下划线或横线，且以字母或数字开头")

    available_skills = (
        set(plugin.memory.skills())
        if getattr(plugin, "memory", None) is not None
        else set()
    )
    resolved_skills = [s for s in tpl["skills"] if s in available_skills]
    existing_ids = {p.id for p in plugin.profiles.list()}

    extra_tools = tuple(tpl.get("extra_tools") or ())
    if pid in existing_ids:
        if not overwrite and pid != tpl["default_profile_id"]:
            raise ValueError(f"角色编号 {pid} 已存在；请换一个编号或传入 overwrite=true")
        existing = plugin.profiles.get(pid)
        changes: dict[str, Any] = {
            "enabled": True,
            "session_mode": tpl["session_mode"],
            "toolsets": list(tpl["toolsets"]),
            "skills": resolved_skills,
            "max_steps": tpl["max_steps"],
            "timeout_seconds": tpl["timeout_seconds"],
        }
        if extra_tools:
            merged_tools = list(dict.fromkeys([*existing.tools, *extra_tools]))
            changes["tools"] = merged_tools
        if overwrite:
            changes["title"] = tpl["name"]
            changes["name"] = tpl["name"]
            changes["description"] = tpl["tagline"]
            changes["instructions"] = tpl["instructions"]
        prof = plugin.profiles.update(pid, **changes)
        action_taken = "updated"
    else:
        create_kwargs: dict[str, Any] = {
            "title": tpl["name"],
            "name": tpl["name"],
            "description": tpl["tagline"],
            "icon_shape": tpl["icon_shape"],
            "theme_color": tpl["theme_color"],
            "enabled": True,
            "session_mode": tpl["session_mode"],
            "toolsets": list(tpl["toolsets"]),
            "skills": resolved_skills,
            "instructions": tpl["instructions"],
            "max_steps": tpl["max_steps"],
            "timeout_seconds": tpl["timeout_seconds"],
        }
        if extra_tools:
            from .abilities import expand_toolsets

            create_kwargs["tools"] = list(
                dict.fromkeys([*expand_toolsets(tpl["toolsets"]), *extra_tools])
            )
        prof = plugin.profiles.create(pid, **create_kwargs)
        action_taken = "created"

    return {
        "template_id": tid,
        "profile_id": prof.id,
        "action": action_taken,
        "session_mode": prof.session_mode,
        "enabled": prof.enabled,
        "try_prompt": tpl["try_prompt"],
    }
