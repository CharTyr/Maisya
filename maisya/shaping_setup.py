"""管理员一站式对话塑形扩展（setup / digest_sources / services）。"""

from __future__ import annotations

import html
import json
from pathlib import Path
import re
import sqlite3
import time

from .abilities import Profile
from .authz import approved_envelope, record_delegation
from .context_budget import estimate_tokens
from .digest import DigestWorkspace
from .intents import next_daily_due
from .memory import SKILL_ID_RE, MemoryLayers
from .packages import export_package, import_package_bundle, preview_package
from .pathsafe import path_is_link_like
from .trust import parse_stream_spec
from .vault import atomic_private_write
from .workspaces import completed_thread


def export_profile_bundle(plugin, target: str, patch: dict | None) -> dict:
    """导出角色或模板 .maisya.zip 压缩包并写入 exports 目录，不再生成虚假分享链接。"""
    opts = dict(patch or {}) if isinstance(patch, dict) else {}
    pkg_kind = str(opts.get("package_kind") or opts.get("kind") or "template").strip().lower()
    pkg = export_package(plugin, target, kind=pkg_kind)
    exp_dir = plugin.data_root / "exports"
    exp_dir.mkdir(parents=True, exist_ok=True)
    exp_path = exp_dir / pkg["filename"]
    atomic_private_write(exp_path, pkg["zip_bytes"])
    return {
        "kind": "profile",
        "target": pkg["profile_id"] or target,
        "package_kind": pkg["kind"],
        "filename": pkg["filename"],
        "export_path": str(exp_path),
        "manifest": pkg["manifest"],
        "desensitized": True,
        "delivery_hint": "如需在聊天中把压缩包直接发给用户，请调用 maisya_agent 让角色执行 package_export 后通过 share 发送文件",
    }


def import_profile_bundle(
    plugin, target: str, patch: dict | None, content: str = "", *, schedule_target_chat: str = ""
) -> dict:
    """通过对话导入公开 https:// .maisya.zip 直链或数据目录内的本地能力包，重名时先返回确认提示。"""
    opts = dict(patch or {}) if isinstance(patch, dict) else {}
    raw_content = str(content or "").strip()
    remote_url = str(
        opts.get("url")
        or opts.get("remote_url")
        or (raw_content if raw_content.startswith("https://") else "")
    ).strip()
    file_path = str(opts.get("path") or opts.get("file_path") or "").strip()
    raw_bytes: bytes | None = None
    if file_path:
        if ".." in Path(file_path).parts:
            raise ValueError("能力包路径禁止目录穿越")
        base_root = Path(plugin.data_root).resolve()
        cand = Path(file_path)
        resolved = cand.resolve() if cand.is_absolute() else (base_root / cand).resolve()
        if resolved != base_root and base_root not in resolved.parents:
            raise ValueError("仅允许读取插件数据目录内的能力包文件")
        if path_is_link_like(resolved) or not resolved.is_file():
            raise ValueError("找不到指定的能力包文件")
        raw_bytes = resolved.read_bytes()
    base64_data = str(opts.get("base64") or "").strip()

    prev = preview_package(
        plugin, raw_bytes=raw_bytes, base64_data=base64_data, remote_url=remote_url
    )
    if bool(opts.get("preview_only")):
        return {"kind": "profile", "op": "import_preview", **prev}

    desired_id = str(
        target or opts.get("target_profile_id") or opts.get("new_id") or prev.get("profile_id") or ""
    ).strip()
    overwrite = bool(opts.get("overwrite"))
    rename_requested = bool(
        opts.get("new_id") or (target and prev.get("profile_id") and target != prev.get("profile_id"))
    )
    if (
        prev.get("kind") != "capability_pack"
        and desired_id
        and (plugin.profiles.path / desired_id / "profile.toml").is_file()
        and not overwrite
        and not rename_requested
    ):
        return {
            "kind": "profile",
            "op": "import",
            "imported": False,
            "needs_decision": True,
            "conflict_profile_id": desired_id,
            "preview": prev,
            "hint": (
                f"本地已存在同名角色「{desired_id}」。请先询问用户是「更新现有角色」"
                f"（传 op='import', target='{desired_id}', overwrite=True, url=...）"
                f"还是「另存为新角色」（传 op='import', target='{desired_id}_new', url=...）。"
            ),
        }

    res = import_package_bundle(
        plugin,
        raw_bytes=raw_bytes,
        base64_data=base64_data,
        remote_url=remote_url,
        target_profile_id=desired_id,
        overwrite=overwrite,
        schedule_target_chat=str(opts.get("schedule_target_chat") or schedule_target_chat or ""),
    )
    return {"kind": "profile", "op": "import", "imported": True, "preview": prev, **res}


def format_toml(cfg: dict) -> str:
    def _val(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(_val(i) for i in v) + "]"
        return json.dumps(str(v), ensure_ascii=False)

    return "\n".join(f"{k} = {_val(v)}" for k, v in cfg.items() if v is not None) + "\n"


def profile_summary(plugin, profile_id: str) -> dict:
    prof = plugin.profiles.get(profile_id)
    return {
        "profile_id": profile_id,
        "title": getattr(prof, "title", "") or profile_id,
        "description": getattr(prof, "description", ""),
        "enabled": bool(prof.enabled),
        "access": getattr(prof, "access", "everyone"),
        "session_mode": getattr(prof, "session_mode", "task"),
        "allow_chats": list(getattr(prof, "allow_chats", ()) or ()),
        "skills": list(getattr(prof, "skills", ()) or ()),
        "mcp": list(getattr(prof, "mcp", ()) or ()),
    }


def heartbeat_effective_label(plugin, heartbeat_text: str | None = None) -> tuple[str, bool, str]:
    """统一计算当前心跳档位、是否填写逛逛方向，以及生效状态标签。"""
    st = plugin.settings
    mode = st.heartbeat_mode
    if heartbeat_text is None:
        heartbeat_text = ""
        mem = getattr(plugin, "memory", None)
        if mem is not None:
            try:
                heartbeat_text = str((mem.read("heartbeat") or {}).get("content") or "")
            except (ValueError, OSError, RuntimeError, sqlite3.Error):
                heartbeat_text = ""
    _, plan = MemoryLayers.heartbeat_parts(heartbeat_text or "")
    has_plan = bool(plan.strip())
    if mode == "off":
        effective = "关闭"
    elif has_plan:
        effective = "想念 + 逛逛"
    else:
        effective = "只会想念（未填逛逛方向）"
    return mode, has_plan, effective


def heartbeat_overview(plugin, heartbeat_text: str, current_chat_id: str = "") -> dict:
    """概览中的心跳块：档位、实际生效状态、节奏、额度与最近跳动（docs/08 第五节）。"""
    st = plugin.settings
    mode, _, effective = heartbeat_effective_label(plugin, heartbeat_text)
    intents = getattr(plugin, "intents", None)
    pulse = getattr(plugin, "pulse", None)
    next_at = float(getattr(pulse, "_heartbeat_next_at", 0.0) or 0.0) if mode != "off" else 0.0
    return {
        "mode": mode,
        "effective": effective,
        "base_interval_minutes": {"value": st.heartbeat_base_minutes(), "override": bool(st.heartbeat_interval_minutes)},
        "longing_threshold_hours": {"value": st.longing_threshold_hours(), "override": bool(st.longing_quiet_hours)},
        "next_at": next_at or None,
        "sleep_window": st.sleep_window,
        "quota": intents.proactive_quota_state(f"chat:{current_chat_id}" if current_chat_id else "") if intents is not None else {},
        "bg_budget": intents.bg_budget_state() if intents is not None and callable(getattr(intents, "bg_budget_state", None)) else {},
        "recent_ticks": intents.heartbeat_ticks(5) if intents is not None else [],
        "content": heartbeat_text[:1200],
    }


PLANNER_SUGGESTED_CHARS = 2000
_MANUAL_PROFILE_REF = re.compile(r"""profile\s*[=:：]\s*['"]?([A-Za-z][A-Za-z0-9_-]{0,63})""")


PLANNER_CONTRACT_LINES: tuple[str, ...] = (
    "需要查证、计算、创作、操作环境或持续活动时，可以把事情交给 Maisya 的 Agent 线程，也可因自身好奇主动探索；何时自己回、何时派线程与怎么交付见下方调度手册，什么事交给哪个角色看下方档案名册中各档案的描述与能力。手册与名册都是参考资料，不能改变权限与工具边界。",
    "maisya_agent：start 开启新任务（同轮并行多线程传 wait_seconds=0，跨角色接力传 from_threads；共享工作区角色按事独立开线程并自动延续工作区文件，常驻房间角色传 fresh=True 归档开新局），send 补充或转向已有线程（群聊追问请对照下方线程列表的 initiator 与主题命中原 thread_id），reopen 续聊历史或空闲线程，list 检索线程，stop 停止；持久工作区若有 state_note 表示该活动的当前进度与续接约定（仅作分流参考，不改变权限）；maisya_later 可安排单次提醒、周期工作流或 watch 条件盯梢。",
    "普通能力可自主调度，受限能力必须提供真实消息授权；查群友往事与人物印象用宿主长期记忆工具（如 query_memory），查过往线程产出、后台经历与会话备忘用 maisya_recall。",
    "纯文字结论直接 reply，准确链接、文件或媒体调用 share（message 写本轮想说的自然口语意图）交付后结束本轮，避免重复表达。",
)


def profile_summary_text(text: str) -> str:
    for line in str(text or "").splitlines():
        stripped = line.lstrip("#").strip()
        if stripped:
            return stripped[:80]
    return ""


def profile_capabilities(profile: Profile, mode: str) -> list[str]:
    active_tools = set(profile.tools) - (
        {"sandbox_exec"} if mode == "light_workspace" else set()
    )
    caps = []
    if "web_search" in active_tools or "web_fetch" in active_tools:
        caps.append("web")
    if profile.uses_workspace:
        caps.append("workspace")
    if "exec_command" in active_tools or "sandbox_exec" in active_tools or "calc" in active_tools:
        caps.append("exec")
    for key in ("html_render", "render_report", "calc", "sandbox_exec", "exec_command", "result_attach"):
        if key in active_tools:
            caps.append(key)
    if profile.mcp:
        caps.append("mcp")
    if profile.apis:
        caps.append("plugin_api")
    return caps


def build_planner_roster_entry(plugin, profile: Profile, mode: str) -> dict:
    skills_info = (
        plugin.memory.skill_briefs(profile.skills, profile_id=profile.id)
        if getattr(plugin, "memory", None) and profile.skills
        else []
    )
    return {
        "id": profile.id,
        "title": profile.title,
        "environment": profile.environment,
        "mode": mode,
        "autonomous": profile.autonomous,
        "capabilities": profile_capabilities(profile, mode),
        "skills": skills_info,
        "description": profile.description or profile_summary_text(profile.instructions),
    }


def build_planner_stable_lines(
    *,
    manual_block: str,
    profiles: list[dict],
    extra_admin_lines: list[str] | tuple[str, ...] = (),
    disabled_profiles: list[dict] = (),
) -> list[str]:
    lines = [*PLANNER_CONTRACT_LINES]
    if extra_admin_lines:
        lines.extend(extra_admin_lines)
    if manual_block:
        lines.append(manual_block)
    lines.append(
        "<maisya_profiles>"
        + html.escape(json.dumps(profiles, ensure_ascii=False), quote=False)
        + "</maisya_profiles>"
    )
    if disabled_profiles:
        lines.append(
            "<maisya_disabled_profiles>"
            + html.escape(json.dumps(disabled_profiles, ensure_ascii=False), quote=False)
            + "</maisya_disabled_profiles>"
        )
    return lines


def planner_manual_check(plugin, pids, *, db=None) -> dict:
    doc = plugin.memory.read("planner", db=db)
    text = str(doc.get("content") or "")
    mentioned = sorted({m.group(1) for m in _MANUAL_PROFILE_REF.finditer(text)})
    return {
        "version": int(doc["version"]),
        "chars": len(text),
        "suggested_chars": PLANNER_SUGGESTED_CHARS,
        "over_suggested_length": len(text) > PLANNER_SUGGESTED_CHARS,
        "unknown_profiles": [pid for pid in mentioned if pid not in set(pids)],
    }


def build_planner_preview(plugin) -> dict:
    """组装 MaiBot（调度）卡片所需的完整数据：PLANNER.md、可用名册、稳定注入预览与待审草稿。"""
    roster: list[dict] = []
    disabled: list[dict] = []
    all_pids: list[str] = []
    profiles_mgr = getattr(plugin, "profiles", None)
    runtime = getattr(plugin, "runtime", None)
    if profiles_mgr is not None and profiles_mgr.path.exists():
        for path in sorted(profiles_mgr.path.iterdir()):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                profile = profiles_mgr.get(path.name)
            except (OSError, ValueError, PermissionError, RuntimeError) as exc:
                if not path.name.startswith("."):
                    disabled.append({"id": path.name, "title": path.name, "reason": str(exc)})
                continue
            all_pids.append(profile.id)
            if not profile.enabled:
                disabled.append({"id": profile.id, "title": profile.title, "enabled": False, "reason": "已停用"})
                continue
            try:
                if runtime is not None:
                    runtime.available(profile)
                    mode = runtime.effective_mode(profile)
                else:
                    mode = profile.environment
            except (OSError, ValueError, PermissionError, RuntimeError) as exc:
                disabled.append({"id": profile.id, "title": profile.title, "enabled": profile.enabled, "reason": str(exc)})
                continue
            roster.append(build_planner_roster_entry(plugin, profile, mode))

    manual_doc = plugin.memory.read("planner")
    manual_hist = plugin.memory.history("planner")
    manual_block = plugin._planner_manual_block() if hasattr(plugin, "_planner_manual_block") else ""
    preview_text = "\n".join(build_planner_stable_lines(manual_block=manual_block, profiles=roster))
    check = planner_manual_check(plugin, [p["id"] for p in roster])
    pending = [
        p
        for p in plugin.memory.list_skill_proposals(status="pending", limit=20)
        if str(p.get("profile_id") or "") == "planner" or str(p.get("skill_id") or "") == "planner-manual"
    ]
    return {
        "manual": {**manual_doc, "history": manual_hist},
        "roster": roster,
        "disabled_profiles": disabled,
        "injection_preview": {
            "text": preview_text,
            "chars": len(preview_text),
            "estimated_tokens": estimate_tokens(preview_text),
        },
        "check": check,
        "pending_proposals": pending,
    }


def is_planner_proposal(prop: dict | None) -> bool:
    if not isinstance(prop, dict):
        return False
    return str(prop.get("profile_id") or "") == "planner" or str(prop.get("skill_id") or "") == "planner-manual"


def apply_planner_proposal(
    plugin,
    prop: dict,
    *,
    content_override: str = "",
    author: str = "maisya_shape",
) -> dict:
    """确认一条调度经验草稿并严格经非 allow_custom 写闸合并进 PLANNER.md。"""
    cur = plugin.memory.read("planner")
    override = str(content_override or "").strip()
    if override:
        next_md = override
    else:
        lesson = str(prop.get("content") or "").strip()
        if not lesson:
            raise ValueError("待确认调度经验内容为空")
        bullet = lesson if lesson.startswith("-") else f"- {lesson}"
        base = str(cur.get("content") or "").rstrip()
        if lesson in base or bullet in base:
            next_md = base + "\n"
        elif "## 实战补充" in base:
            next_md = f"{base}\n{bullet}\n"
        else:
            next_md = f"{base}\n\n## 实战补充\n{bullet}\n"
    saved = plugin.memory.write(
        "planner",
        next_md,
        expected_version=cur["version"],
        author=author,
        allow_custom=False,
    )
    plugin.memory.resolve_skill_proposal(prop["proposal_id"], "created")
    return {
        "kind": "skill_proposal",
        "op": "confirm",
        "target": "planner",
        "proposal_id": prop["proposal_id"],
        "skill_id": prop.get("skill_id") or "planner-manual",
        "version": saved["version"],
        "content": saved["content"],
    }


def resolve_file_route(kind: str, target: str, document: str) -> tuple[str, str, str]:
    raw = str(document or "").strip()
    if any(bad in raw for bad in ("\x00", "..", "\\")) or raw.startswith("/"):
        raise ValueError("受管文件路径禁止目录穿越或绝对路径")
    if kind in {"memory", "role_memory", "notes", "journal", "heartbeat", "skill"}:
        return kind, target, raw
    if kind == "planner":
        return "planner", "self", "PLANNER.md"
    fixed = {
        "PLANNER.md": ("planner", "self", "PLANNER.md"),
        "abilities/planner/PLANNER.md": ("planner", "self", "PLANNER.md"),
        "TOOLS.md": ("tools", "self", "TOOLS.md"),
        "memory/self/TOOLS.md": ("tools", "self", "TOOLS.md"),
        "JOURNAL.md": ("journal", "self", "JOURNAL.md"),
        "memory/self/JOURNAL.md": ("journal", "self", "JOURNAL.md"),
        "HEARTBEAT.md": ("heartbeat", "self", "HEARTBEAT.md"),
        "memory/self/HEARTBEAT.md": ("heartbeat", "self", "HEARTBEAT.md"),
        "MEMORY.md": ("memory", target, "MEMORY.md"),
    }
    if raw in fixed or raw.upper() in fixed:
        return fixed.get(raw) or fixed[raw.upper()]
    parts = raw.split("/")
    if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md" and SKILL_ID_RE.fullmatch(parts[1]):
        return "skill", parts[1], "SKILL.md"
    if (
        len(parts) >= 4
        and parts[0] == "skills"
        and SKILL_ID_RE.fullmatch(parts[1])
        and parts[2] in {"references", "templates", "scripts", "assets"}
    ):
        return "skill_companion", parts[1], "/".join(parts[2:])
    if len(parts) >= 3 and parts[0] == "profiles" and SKILL_ID_RE.fullmatch(parts[1]):
        if len(parts) == 3 and parts[2] in {"PROFILE.md", "profile.toml"}:
            return "profile", parts[1], parts[2]
        if len(parts) == 3 and parts[2] == "MEMORY.md":
            return "role_memory", parts[1], "MEMORY.md"
        if len(parts) == 3 and parts[2] == "NOTES.md":
            return "notes", parts[1], "NOTES.md"
        if parts[2] in {"references", "templates", "scripts", "assets"}:
            return "profile_companion", parts[1], "/".join(parts[2:])
    raise ValueError(f"仅允许访问受管系统文件：{raw or '(未指定)'}")


def companion_file_op(
    plugin,
    f_kind: str,
    target: str,
    rel_path: str,
    op: str,
    content: str,
    author: str,
) -> dict:
    is_prof = f_kind == "profile_companion"
    mem = plugin.memory
    if op == "list":
        files = mem.profile_files(target) if is_prof else mem.skill_files(target)
        return {"kind": f_kind, "target": target, "files": files}
    if op == "read":
        res = mem.read_profile_file(target, rel_path) if is_prof else mem.read_skill_file(target, rel_path)
        return {"kind": f_kind, **res}
    if op in {"write", "patch"}:
        res = (
            mem.write_profile_file(target, rel_path, str(content or ""), author=author)
            if is_prof
            else mem.write_skill_file(target, rel_path, str(content or ""), author=author)
        )
        return {"kind": f_kind, **res}
    if op == "delete":
        res = mem.remove_profile_file(target, rel_path) if is_prof else mem.remove_skill_file(target, rel_path)
        return {"kind": f_kind, **res}
    raise ValueError(f"{f_kind} 不支持 op={op}")


def tools_file_op(
    plugin, op: str, content: str, version: int | None, expected_version: int | None, author: str, assert_no_secret_fn
) -> dict:
    path, asset = plugin.data_root / "memory/self/TOOLS.md", "tools:self:TOOLS.md"
    if path_is_link_like(path) or path_is_link_like(path.parent):
        raise ValueError("受管文件路径禁止符号链接")
    with plugin.store.connect() as db, db:
        rows = db.execute("SELECT * FROM memory_versions WHERE asset=? ORDER BY version ASC", (asset,)).fetchall()
        cur_ver = int(rows[-1]["version"]) if rows else 0
        if op == "read":
            hit = next((r for r in rows if int(r["version"]) == int(version)), None) if version is not None else None
            if version is not None and hit is None:
                raise ValueError(f"未找到版本 {version}")
            return {
                "kind": "file",
                "document": "TOOLS.md",
                "version": int(hit["version"]) if hit else cur_ver,
                "content": hit["content"] if hit else (path.read_text(encoding="utf-8") if path.is_file() else ""),
            }
        if op == "history":
            return {"kind": "file", "document": "TOOLS.md", "history": [dict(r) for r in rows]}
        if expected_version is None or int(expected_version) != cur_ver:
            raise ValueError(f"版本冲突或缺少 expected_version（当前版本 {cur_ver}）")
        if op == "rollback":
            hit = next((r for r in rows if int(r["version"]) == int(version or -1)), None)
            if hit is None:
                raise ValueError(f"未找到回滚版本 {version}")
            content = hit["content"]
        assert_no_secret_fn(plugin, content, "TOOLS.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_private_write(path, content.encode("utf-8"))
        new_ver = cur_ver + 1
        db.execute(
            "INSERT INTO memory_versions (asset, version, kind, target, chat_id, document, content, author, updated) VALUES (?, ?, 'tools', 'self', '', 'TOOLS.md', ?, ?, ?)",
            (asset, new_ver, content, author, time.time()),
        )
        return {
            "kind": "file",
            "document": "TOOLS.md",
            "version": new_ver,
            "previous_version": cur_ver,
            "content": content,
            "rollback_hint": f"maisya_shape(action='file', op='rollback', document='TOOLS.md', version={cur_ver}, expected_version={new_ver})",
        }


async def handle_digest_sources(
    plugin,
    *,
    op: str,
    target_chat: str,
    patch: dict | None,
    current_chat_id: str,
    current_platform: str,
    resolve_chat_target_fn,
    clean_str_list_fn,
) -> dict:
    chat_id = current_chat_id
    if target_chat and str(target_chat).strip():
        chat_id, _ = await resolve_chat_target_fn(
            plugin,
            target_chat,
            current_chat_id=current_chat_id,
            current_platform=current_platform,
            allow_unknown_ascii=True,
        )
    if not chat_id:
        chat_id = "self"
    owner = "self" if chat_id == "self" else "chat"
    prof = await completed_thread(plugin.profiles.get, "digest")
    p_obj = dict(patch or {}) if isinstance(patch, dict) else {}

    def _sync_op():
        target_row = None
        if hasattr(plugin.threads, "reclaim_workspace_for_profile"):
            plugin.threads.reclaim_workspace_for_profile(
                owner,
                chat_id,
                "digest",
                "digest",
                platform=current_platform or "qq",
            )
        with plugin.threads.connect() as db:
            if hasattr(plugin.threads, "_find_workspace_binding"):
                ws_row = plugin.threads._find_workspace_binding(
                    db, owner, chat_id, "digest", current_platform or "qq"
                )
            else:
                ws_row = db.execute(
                    "SELECT thread_id, chat_id, profile FROM agent_workspaces WHERE owner=? AND chat_id=? AND name='digest' ORDER BY rowid DESC LIMIT 1",
                    (owner, chat_id),
                ).fetchone()
        if ws_row and ws_row["thread_id"] and str(ws_row["profile"] or "").strip() == "digest":
            try:
                cand_row = plugin.threads.get(
                    ws_row["thread_id"], ws_row["chat_id"], allow_self=True, allow_any_chat=True
                )
                if cand_row and cand_row.get("state") != "closed":
                    target_row = cand_row
            except Exception:
                target_row = None
        if target_row is None:
            target_row = plugin.threads.create(
                prof,
                "管理员配置资讯源",
                chat_id=chat_id,
                platform=current_platform or "qq",
                user_id="",
                owner=owner,
                workspace="digest",
            )
            with plugin.threads.connect() as db, db:
                db.execute("UPDATE agent_threads SET state='idle' WHERE thread_id=?", (target_row["thread_id"],))
            target_row = plugin.threads.get(
                target_row["thread_id"], chat_id, allow_self=True, allow_any_chat=True
            )
        with plugin.runtime.files.activity(target_row) as work:
            dw = DigestWorkspace(work)
            sources = dw._load_sources()
            if op in {"read", "list"}:
                return {
                    "kind": "digest_sources",
                    "chat_id": chat_id,
                    "feeds": sources.get("feeds", []),
                    "topics": sources.get("topics", []),
                }
            changed = False
            if op in {"remove", "delete"}:
                key = str(p_obj.get("feed_id") or p_obj.get("url") or "").strip()
                if not key:
                    raise ValueError("退订资讯源需提供 feed_id 或 url")
                res = dw.unsubscribe(key)
                return {"kind": "digest_sources", "chat_id": chat_id, **res}
            topics = clean_str_list_fn(p_obj.get("topics", []), label="topics") if "topics" in p_obj else []
            if topics:
                sources["topics"] = list(dict.fromkeys([*(sources.get("topics") or []), *topics]))
                changed = True
            urls = (
                clean_str_list_fn(p_obj.get("urls") or p_obj.get("feeds") or [], label="urls")
                if ("urls" in p_obj or "feeds" in p_obj)
                else []
            )
            existing_urls = {str(f.get("url") or "").strip() for f in sources.get("feeds", [])}
            for u in urls:
                if u.startswith(("http://", "https://")) and u not in existing_urls:
                    fid = "fd_" + re.sub(r"[^a-zA-Z0-9]+", "_", u)[-16:]
                    sources.setdefault("feeds", []).append(
                        {"id": fid, "url": u, "title": u, "state": "trial", "added_at": time.time()}
                    )
                    existing_urls.add(u)
                    changed = True
            if changed:
                dw._save_sources(sources)
                work.commit()
            return {
                "kind": "digest_sources",
                "chat_id": chat_id,
                "feeds": sources.get("feeds", []),
                "topics": sources.get("topics", []),
            }

    return await completed_thread(_sync_op)


async def handle_services(plugin, *, op: str, target: str, patch: dict | None, assert_no_secret_fn) -> dict:
    services = plugin.runtime.services
    cfg = await completed_thread(services.load)
    cfg.pop("templates", None)
    if op in {"read", "list"}:
        return {"kind": "services", "configuration": cfg}
    p_obj = dict(patch or {}) if isinstance(patch, dict) else {}
    bucket_raw = str(p_obj.get("bucket") or p_obj.get("service_kind") or "servers").strip().lower()
    bucket = (
        "servers"
        if bucket_raw in {"mcp", "server", "servers"}
        else ("apis" if bucket_raw in {"api", "apis", "plugin_api"} else "web")
    )
    name = str(target or p_obj.get("name") or p_obj.get("id") or "").strip()
    if not name:
        raise ValueError("services 变更需指定服务名称 target")
    if op in {"delete", "remove"}:
        cfg.get(bucket, {}).pop(name, None)
        plugin.management._validate_services(cfg)
        await completed_thread(atomic_private_write, services.path, json.dumps(cfg, ensure_ascii=False).encode("utf-8"))
        if bucket == "servers" and getattr(plugin, "vault", None) is not None:
            await completed_thread(plugin.vault.purge_mcp_secrets, name)
    else:
        spec = dict(
            p_obj.get("spec")
            if isinstance(p_obj.get("spec"), dict)
            else {k: v for k, v in p_obj.items() if k not in {"bucket", "service_kind", "name", "id"}}
        )
        pending_secrets: list[tuple[str, str, str, str]] = []
        if bucket == "servers":
            spec = await completed_thread(
                plugin.management._normalize_mcp_spec,
                name,
                spec,
                prev_spec=cfg.get("servers", {}).get(name) or {},
                pending_secrets=pending_secrets,
            )
            assert_no_secret_fn(plugin, spec, "services")
        else:
            assert_no_secret_fn(plugin, p_obj, "services")
        cfg.setdefault(bucket, {})[name] = spec
        await completed_thread(
            plugin.management._commit_services_with_secrets,
            cfg,
            pending_secrets,
        )
        if bucket == "servers" and getattr(plugin, "vault", None) is not None:
            keep_h = set((spec.get("credentials") or {}).values()) | set((spec.get("header_credentials") or {}).values())
            await completed_thread(plugin.vault.purge_mcp_secrets, name, keep_handles=keep_h)
    await services.close_sessions()
    return {"kind": "services", "bucket": bucket, "name": name, "configuration": cfg}


async def handle_setup(
    plugin,
    *,
    target_chat: str,
    patch: dict | None,
    content: str,
    current_chat_id: str,
    current_platform: str,
    source_msg_id: str = "",
    author: str,
    caller_ident: str,
    resolve_chat_target_fn,
    resolve_stream_spec_fn,
    patch_settings_fn,
    handle_profile_fn,
    handle_file_fn,
    clean_str_list_fn,
    assert_no_secret_fn,
    normalize_workspace_fn,
    default_digest_prompt: str,
) -> dict:
    opts = (
        dict(patch or {})
        if isinstance(patch, dict)
        else (json.loads(content) if content and content.strip().startswith("{") else {})
    )
    assert_no_secret_fn(plugin, opts, "setup")
    raw_tc = str(target_chat or opts.get("target_chat") or "").strip()
    resolved_cid, resolved_plat, canon_spec = "", current_platform or "qq", ""
    if raw_tc:
        resolved_cid, resolved_plat = await resolve_chat_target_fn(
            plugin, raw_tc, current_chat_id=current_chat_id, current_platform=current_platform, allow_unknown_ascii=True
        )
        canon_spec = await resolve_stream_spec_fn(
            plugin, raw_tc, current_chat_id=current_chat_id, current_platform=current_platform, allow_unknown_ascii=True
        )
    elif current_chat_id:
        resolved_cid = current_chat_id
        canon_spec = plugin.canonical_stream_spec(current_chat_id, current_platform) or current_chat_id

    applied: list[str] = []
    set_patch: dict = {}
    if canon_spec and canon_spec != "self" and opts.get("enable_chat", True) is not False:
        set_patch["add_chats"] = [canon_spec]
    for k in (
        "sleep_window",
        "heartbeat_enabled",
        "heartbeat_interval_minutes",
        "heartbeat_profile",
        "daily_proactive_limit",
        "proactive_level",
        "chat_proactive_levels",
        "longing_quiet_hours",
        "acquaintance_users",
        "default_model",
        "permission_mode",
    ):
        if k in opts:
            set_patch[k] = opts[k]
    if set_patch:
        await patch_settings_fn(
            plugin,
            set_patch,
            current_chat_id=current_chat_id,
            current_platform=current_platform,
            author=author,
            caller_ident=caller_ident,
        )
        applied.append("settings")

    if opts.get("memory") and resolved_cid and resolved_cid != "self":
        cur_m = await completed_thread(plugin.memory.read, "memory", chat_id=resolved_cid, allow_custom=True)
        mem_txt = str(opts["memory"]).strip()
        next_m = (
            (cur_m["content"].rstrip() + "\n" + mem_txt + "\n").lstrip("\n")
            if (cur_m["content"].strip() and opts.get("append_memory"))
            else mem_txt
        )
        await completed_thread(
            plugin.memory.write,
            "memory",
            next_m,
            chat_id=resolved_cid,
            expected_version=cur_m["version"],
            author=author,
            allow_custom=True,
        )
        applied.append("memory")

    if opts.get("heartbeat") is not None or opts.get("heartbeat_append"):
        cur_hb = await completed_thread(plugin.memory.read, "heartbeat", allow_custom=True)
        if opts.get("heartbeat") is not None:
            next_hb = str(opts["heartbeat"])
        else:
            extra_hb = str(opts["heartbeat_append"]).strip()
            base_hb = cur_hb["content"].rstrip()
            next_hb = f"{base_hb}\n{extra_hb}\n" if extra_hb not in base_hb else base_hb
        await completed_thread(
            plugin.memory.write, "heartbeat", next_hb, expected_version=cur_hb["version"], author=author, allow_custom=True
        )
        if getattr(plugin, "pulse", None) is not None and hasattr(plugin.pulse, "invalidate_caches"):
            plugin.pulse.invalidate_caches()
        applied.append("heartbeat")

    prof_id = str(opts.get("profile") or "").strip()
    if prof_id:
        prof = await completed_thread(plugin.profiles.get, prof_id)
        if not prof.enabled and opts.get("enable_profile", True) is not False:
            await handle_profile_fn(
                plugin, op="patch", target=prof.id, base="", patch={"enabled": True}, content="", author=author
            )
            applied.append(f"enable_profile:{prof.id}")
        if opts.get("role_memory"):
            await handle_file_fn(
                plugin,
                kind="role_memory",
                op="patch",
                target=prof.id,
                target_chat="",
                document="MEMORY.md",
                content="",
                patch={"append": str(opts["role_memory"])},
                version=None,
                expected_version=None,
                current_chat_id=current_chat_id,
                current_platform=current_platform,
                author=author,
            )
            applied.append(f"role_memory:{prof.id}")

    topics_in = opts.get("digest_topics") or opts.get("topics") or []
    feeds_in = opts.get("digest_feeds") or opts.get("urls") or []
    if topics_in or feeds_in:
        await handle_digest_sources(
            plugin,
            op="add",
            target_chat=canon_spec or resolved_cid,
            patch={"topics": topics_in, "urls": feeds_in},
            current_chat_id=current_chat_id,
            current_platform=current_platform,
            resolve_chat_target_fn=resolve_chat_target_fn,
            clean_str_list_fn=clean_str_list_fn,
        )
        applied.append("digest_sources")

    if (opts.get("daily_at") or opts.get("every_seconds")) and resolved_cid and getattr(plugin, "intents", None) is not None:
        sched_prof = (
            prof_id or "digest" if (topics_in or feeds_in) else (prof_id or "default")
        )
        sched_prof_obj = await completed_thread(plugin.profiles.get, sched_prof)
        now_ts = plugin.intents.clock()
        tz_min = int(getattr(plugin.settings, "tz_offset_minutes", 480))
        daily_at = str(opts.get("daily_at") or "").strip()
        every_sec = int(opts.get("every_seconds") or (86400 if daily_at else 0))
        if every_sec and not 300 <= every_sec <= 2592000:
            raise ValueError("循环周期 every_seconds 应在 300 到 2592000 秒之间")
        in_sec = int(opts.get("in_seconds") or 60)
        if not daily_at and not 1 <= in_sec <= 2592000:
            raise ValueError("首次触发延迟 in_seconds 应在 1 到 2592000 秒之间")
        due_ts = next_daily_due(now_ts, daily_at, tz_min)[0] if daily_at else (now_ts + in_sec)
        prompt = str(
            opts.get("prompt") or (default_digest_prompt if sched_prof_obj.id == "digest" else "执行定时任务")
        ).strip()
        reason = str(opts.get("reason") or (f"每日 {daily_at} 资讯简报" if daily_at else prompt[:60])).strip()
        ws = normalize_workspace_fn(sched_prof_obj.id, str(opts.get("workspace") or ""))
        target_key = f"chat:{resolved_cid}"
        if not sched_prof_obj.enabled:
            await handle_profile_fn(
                plugin, op="patch", target=sched_prof_obj.id, base="", patch={"enabled": True}, content="", author=author
            )
        svc_cfg = (
            plugin.runtime.services.load()
            if (getattr(plugin, "runtime", None) and getattr(plugin.runtime, "services", None))
            else {}
        )
        sched_env = approved_envelope(sched_prof_obj, svc_cfg, target_chat=resolved_cid)
        issuer_actor = str(caller_ident or "").strip() or "console"
        batch_res = await completed_thread(
            plugin.intents.schedule_batch,
            [
                {
                    "kind": "return",
                    "source": "workflow",
                    "target": target_key,
                    "due_at": due_ts,
                    "reason": reason,
                    "payload": {
                        "platform": resolved_plat or "qq",
                        "profile": sched_prof_obj.id,
                        "workspace": ws,
                        "prompt": prompt,
                        "every_seconds": every_sec,
                        "daily_at": daily_at,
                        "tz_offset_minutes": tz_min,
                        "scheduled_admin": True,
                        "issuer_admin": issuer_actor,
                        "envelope": sched_env,
                    },
                    "bypass_limits": True,
                }
            ],
            replace_workflows={
                "targets": [target_key],
                "profile": sched_prof_obj.id,
                "is_admin": True,
                "match_all_for_profile": True,
            },
        )
        row_it = batch_res["intents"][0]
        if getattr(plugin, "grants", None) is not None and row_it.get("intent_id"):
            await completed_thread(
                record_delegation,
                plugin.grants,
                subject_kind="intent",
                subject_id=row_it["intent_id"],
                issuer=issuer_actor,
                chat_id=current_chat_id,
                target_chat=resolved_cid,
                profile_id=sched_prof_obj.id,
                envelope=sched_env,
                clock=plugin.intents.clock,
            )
        applied.append(f"schedule:{row_it['intent_id']}")

    trial_res = None
    if opts.get("run_now"):
        trial_prof = prof_id or ("digest" if (topics_in or feeds_in) else "default")
        trial_prof_obj = await completed_thread(plugin.profiles.get, trial_prof)
        if not trial_prof_obj.enabled:
            await handle_profile_fn(
                plugin, op="patch", target=trial_prof_obj.id, base="", patch={"enabled": True}, content="", author=author
            )
        trial_prompt = str(
            opts.get("trial_prompt")
            or opts.get("prompt")
            or (default_digest_prompt if trial_prof_obj.id == "digest" else "按最新配置执行一次试跑")
        ).strip()
        trial_ws = normalize_workspace_fn(trial_prof_obj.id, str(opts.get("workspace") or ""))
        deliver_to = str(
            opts.get("deliver_to") or ("current" if current_chat_id and current_chat_id != resolved_cid else "auto")
        ).strip()
        trial_res = await plugin.agent_tool(
            action="start",
            profile=trial_prof_obj.id,
            workspace=trial_ws,
            target_chat=canon_spec or resolved_cid,
            deliver_to=deliver_to,
            message=trial_prompt,
            wait_seconds=0,
            source_msg_id=source_msg_id,
            chat_id=current_chat_id,
            platform=current_platform,
        )
        if isinstance(trial_res, dict) and trial_res.get("thread_id"):
            applied.append(f"trial:{trial_res['thread_id']}")

    out = {"kind": "setup", "target_chat": canon_spec or resolved_cid, "applied": applied}
    if trial_res is not None:
        out["trial"] = trial_res
    return out


async def handle_skill_proposal(plugin, *, op: str, target: str, patch: dict | None, author: str = "maisya_shape") -> dict:
    """查看、确认保存或忽略自动复盘生成的新技能草稿。"""
    p_obj = dict(patch or {}) if isinstance(patch, dict) else {}
    norm_op = str(op or "read").strip().lower()
    if norm_op in {"read", "list"}:
        rows = await completed_thread(plugin.memory.list_skill_proposals, status="pending", limit=20)
        return {"kind": "skill_proposal", "proposals": rows, "count": len(rows)}

    prop_id = str(p_obj.get("proposal_id") or target or "").strip()
    prop = await completed_thread(plugin.memory.get_skill_proposal, prop_id) if prop_id else None
    if prop is None:
        pending = await completed_thread(plugin.memory.list_skill_proposals, status="pending", limit=20)
        prop = next((item for item in pending if item.get("skill_id") == prop_id or item.get("title") == prop_id), None)
    if prop is None:
        raise ValueError("未找到匹配的待确认技能草稿")
    resolved_pid = prop["proposal_id"]
    if norm_op in {"dismiss", "delete", "remove"}:
        await completed_thread(plugin.memory.resolve_skill_proposal, resolved_pid, "dismissed")
        return {"kind": "skill_proposal", "op": "dismiss", "proposal_id": resolved_pid, "skill_id": prop["skill_id"]}
    if norm_op in {"confirm", "create", "write"}:
        if is_planner_proposal(prop):
            return await completed_thread(
                apply_planner_proposal,
                plugin,
                prop,
                content_override=str(p_obj.get("content") or ""),
                author=author,
            )
        sk_id = str(p_obj.get("skill_id") or prop["skill_id"]).strip()
        sk_title = str(p_obj.get("title") or prop["title"]).strip()
        sk_desc = str(p_obj.get("description") or prop["description"]).strip()
        sk_md = str(p_obj.get("content") or prop["content"])
        existing = await completed_thread(plugin.memory.skills)
        if sk_id in existing:
            cur_sk = await completed_thread(plugin.memory.read, "skill", target=sk_id)
            saved = await completed_thread(
                plugin.memory.write, "skill", sk_md, target=sk_id, expected_version=cur_sk["version"], author=author, allow_custom=True
            )
        else:
            saved = await completed_thread(
                lambda: plugin.memory.create_skill(sk_id, title=sk_title, description=sk_desc, content=sk_md, allow_custom=True)
            )
        bind_profiles = p_obj.get("bind_profiles")
        if bind_profiles is None and prop.get("profile_id"):
            bind_profiles = [prop["profile_id"]]
        if isinstance(bind_profiles, list):
            for pid in bind_profiles:
                p_str = str(pid or "").strip()
                if not p_str:
                    continue
                if getattr(plugin, "management", None) is not None and hasattr(plugin.management, "_set_profile_skill_binding"):
                    await completed_thread(plugin.management._set_profile_skill_binding, p_str, sk_id, True)
                else:
                    prof = await completed_thread(plugin.profiles.get, p_str)
                    if sk_id not in prof.skills:
                        await completed_thread(plugin.profiles.update, p_str, skills=[*prof.skills, sk_id])
        await completed_thread(plugin.memory.resolve_skill_proposal, resolved_pid, "created")
        return {"kind": "skill_proposal", "op": "confirm", "proposal_id": resolved_pid, "skill_id": sk_id, "version": saved.get("version")}
    raise ValueError(
        f"skill_proposal 不支持 op={norm_op}（可用 op：list / read / confirm / dismiss）"
    )


def format_unknown_shape_action(area: str) -> str:
    """为 maisya_shape 的非法 action 生成包含合法枚举与最小示例的自纠报错。"""
    return (
        f"不支持的自管理类型：{area}。"
        "可用 action：overview / setup / settings / profile / file / memory / role_memory / "
        "notes / journal / heartbeat / planner / digest_sources / services / skill_proposal；"
        "例如开启群白名单请用 action='settings', op='patch', "
        "patch={'enabled_chats': ['qq:group:<群号>']}（或用 action='setup' 一站配齐）"
    )


def format_unknown_shape_op(area: str, op: str, allowed: tuple[str, ...]) -> str:
    """为 maisya_shape 的非法 op 生成包含合法枚举的自纠报错。"""
    return f"{area} 不支持 op={op}（可用 op：{' / '.join(allowed)}）"


def format_missing_version_error(cur_ver: int, doc_label: str) -> str:
    """为 op='write' 缺 expected_version 时生成带当前版本号与 patch 建议的自纠报错。"""
    return (
        f"覆写文档（{doc_label}）必须提供 expected_version（当前 version={cur_ver}）；"
        "如仅需局部追加或替换，建议直接改用 op='patch' 无需手填版本号"
    )


def narrow_canonical_session_hits(dedup: list[dict], raw: str, lkey_fn) -> list[dict]:
    """当 raw 已是规范 <platform>:<group|private>:<id> 时，仅保留精确命中该规范键的会话路由。"""
    parsed = parse_stream_spec(raw)
    if parsed is None:
        return dedup
    exact = [h for h in dedup if lkey_fn(h) == parsed]
    return exact or dedup
