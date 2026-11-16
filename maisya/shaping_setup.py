"""管理员一站式对话塑形扩展（setup / digest_sources / services）。"""

from __future__ import annotations

import json
from pathlib import Path
import re
import time

from .digest import DigestWorkspace
from .intents import next_daily_due
from .memory import SKILL_ID_RE
from .packages import export_package, import_package_bundle, preview_package
from .pathsafe import path_is_link_like
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


def resolve_file_route(kind: str, target: str, document: str) -> tuple[str, str, str]:
    raw = str(document or "").strip()
    if any(bad in raw for bad in ("\x00", "..", "\\")) or raw.startswith("/"):
        raise ValueError("受管文件路径禁止目录穿越或绝对路径")
    if kind in {"memory", "role_memory", "notes", "journal", "heartbeat", "skill"}:
        return kind, target, raw
    fixed = {
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
    if len(parts) == 3 and parts[0] == "profiles" and SKILL_ID_RE.fullmatch(parts[1]):
        if parts[2] in {"PROFILE.md", "profile.toml"}:
            return "profile", parts[1], parts[2]
        if parts[2] == "MEMORY.md":
            return "role_memory", parts[1], "MEMORY.md"
        if parts[2] == "NOTES.md":
            return "notes", parts[1], "NOTES.md"
    raise ValueError(f"仅允许访问受管系统文件：{raw or '(未指定)'}")


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
        with plugin.threads.connect() as db:
            ws_row = db.execute(
                "SELECT thread_id, chat_id FROM agent_workspaces WHERE owner=? AND chat_id=? AND name='digest' ORDER BY rowid DESC LIMIT 1",
                (owner, chat_id),
            ).fetchone()
        if ws_row and ws_row["thread_id"]:
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
        if not sched_prof_obj.enabled:
            await handle_profile_fn(
                plugin, op="patch", target=sched_prof_obj.id, base="", patch={"enabled": True}, content="", author=author
            )
        now_ts = plugin.intents.clock()
        tz_min = int(getattr(plugin.settings, "tz_offset_minutes", 480))
        daily_at = str(opts.get("daily_at") or "").strip()
        every_sec = int(opts.get("every_seconds") or (86400 if daily_at else 0))
        due_ts = next_daily_due(now_ts, daily_at, tz_min)[0] if daily_at else (now_ts + int(opts.get("in_seconds") or 60))
        prompt = str(
            opts.get("prompt") or (default_digest_prompt if sched_prof_obj.id == "digest" else "执行定时任务")
        ).strip()
        reason = str(opts.get("reason") or (f"每日 {daily_at} 资讯简报" if daily_at else prompt[:60])).strip()
        ws = normalize_workspace_fn(sched_prof_obj.id, str(opts.get("workspace") or ""))
        target_key = "self" if resolved_cid == "self" else f"chat:{resolved_cid}"
        for old_it in plugin.intents.list_all(status="pending", limit=60):
            if old_it.get("target") == target_key and (old_it.get("payload") or {}).get("profile") == sched_prof_obj.id:
                plugin.intents.cancel(old_it["intent_id"], target_key, is_admin=True)
        row_it = await completed_thread(
            plugin.intents.schedule,
            kind="return",
            source="workflow",
            target=target_key,
            due_at=due_ts,
            reason=reason,
            payload={
                "platform": resolved_plat or "qq",
                "profile": sched_prof_obj.id,
                "workspace": ws,
                "prompt": prompt,
                "every_seconds": every_sec,
                "daily_at": daily_at,
                "tz_offset_minutes": tz_min,
                "scheduled_admin": True,
            },
            bypass_limits=True,
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
    raise ValueError(f"skill_proposal 不支持 op={norm_op}")
