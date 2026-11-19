"""Maisya 一键体检诊断（配置、模型健康、联网、离线出图、沙箱与档案健康；零密钥泄露）。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .scout import call_capability
from .workspaces import completed_thread


def _status_rank(status: str) -> int:
    return {"error": 3, "warn": 2, "info": 1, "ok": 0}.get(status, 0)


async def run_doctor(plugin, *, mgmt=None) -> dict[str, Any]:
    """执行 6 项只读诊断检查，返回可供管理台与命令行直接展示的体检报告。"""
    redact = (
        plugin.vault.redact
        if getattr(plugin, "vault", None) is not None
        else (lambda x: x)
    )
    checks: list[dict[str, Any]] = []

    # 1. 基础配置与白名单
    settings = getattr(plugin, "settings", None)
    plugin_enabled = bool(
        getattr(getattr(settings, "plugin", None), "enabled", True)
    )
    enabled_chats = list(getattr(settings, "enabled_chats", ()) or ())
    admins = list(getattr(plugin, "admins", ()) or ())
    if not plugin_enabled:
        checks.append(
            {
                "id": "config",
                "title": "基础配置与白名单",
                "status": "error",
                "summary": "插件当前处于停用状态。",
                "fix": "在设置页打开「启用插件」开关后生效。",
                "metrics": {"enabled_chats": len(enabled_chats), "admins": len(admins)},
            }
        )
    elif not enabled_chats:
        checks.append(
            {
                "id": "config",
                "title": "基础配置与白名单",
                "status": "warn",
                "summary": f"已识别 {len(admins)} 位管理员，但尚未开启任何聊天白名单。",
                "fix": "在设置页「启用范围」勾选至少一个群聊或私聊，或在管理员私聊直接说「开启这个群」。",
                "metrics": {"enabled_chats": 0, "admins": len(admins)},
            }
        )
    elif not admins:
        checks.append(
            {
                "id": "config",
                "title": "基础配置与白名单",
                "status": "warn",
                "summary": f"已开启 {len(enabled_chats)} 个会话，但未配置管理员。",
                "fix": "在设置页添加管理员账号（如 qq:你的QQ号），以便在聊天中授权改配与审批。",
                "metrics": {"enabled_chats": len(enabled_chats), "admins": 0},
            }
        )
    else:
        checks.append(
            {
                "id": "config",
                "title": "基础配置与白名单",
                "status": "ok",
                "summary": f"已启用 {len(enabled_chats)} 个会话、{len(admins)} 位管理员，插件运行中。",
                "fix": "",
                "metrics": {"enabled_chats": len(enabled_chats), "admins": len(admins)},
            }
        )

    # 2. 模型目录与实时调用健康
    available_models: list[str] = []
    model_probe_err = ""
    try:
        if mgmt is not None and hasattr(mgmt, "_collect_host_models"):
            host_models = await mgmt._collect_host_models(force=False, probe_host=True)
            available_models = list(host_models.get("names") or [])
        else:
            raw_models = await call_capability(
                plugin.ctx, "llm.get_available_models", timeout_ms=4000
            )
            if isinstance(raw_models, dict):
                available_models = [
                    str(m).strip()
                    for m in (raw_models.get("models") or [])
                    if isinstance(m, str) and str(m).strip()
                ]
    except Exception as exc:
        model_probe_err = str(redact(str(exc)))[:120]

    health_snap: dict[str, dict] = {}
    if getattr(plugin, "runtime", None) is not None and hasattr(
        plugin.runtime, "model_health_snapshot"
    ):
        health_snap = plugin.runtime.model_health_snapshot()

    flaky_models = [
        name
        for name, stat in health_snap.items()
        if int(stat.get("timeouts") or 0) >= 2
        or int(stat.get("consecutive_failures") or 0) >= 2
    ]
    profiles_list = (
        await completed_thread(plugin.profiles.list)
        if getattr(plugin, "profiles", None) is not None
        else []
    )
    enabled_profiles = [p for p in profiles_list if getattr(p, "enabled", True)]
    avail_set = set(available_models)
    missing_bound = []
    if avail_set:
        for p in enabled_profiles:
            for m in getattr(p, "model_list", ()) or ():
                if m and m not in avail_set:
                    missing_bound.append(f"{p.id}:{m}")
    if model_probe_err and not available_models:
        checks.append(
            {
                "id": "models",
                "title": "模型目录与健康度",
                "status": "warn",
                "summary": f"未能读取宿主模型清单（{model_probe_err}），线程将按宿主默认槽位运行。",
                "fix": "检查 MaiBot 宿主 model_config.toml 是否配置有效模型。",
                "metrics": {"available": 0, "flaky": len(flaky_models)},
            }
        )
    elif missing_bound:
        checks.append(
            {
                "id": "models",
                "title": "模型目录与健康度",
                "status": "warn",
                "summary": f"发现角色绑定了宿主清单中不存在的模型：{', '.join(missing_bound[:4])}。",
                "fix": "在「能力」页为对应角色改选宿主已有模型，并补充 1 个备选模型。",
                "metrics": {
                    "available": len(available_models),
                    "missing_bound": missing_bound[:6],
                    "flaky": flaky_models[:6],
                },
            }
        )
    elif flaky_models:
        checks.append(
            {
                "id": "models",
                "title": "模型目录与健康度",
                "status": "warn",
                "summary": f"以下模型近期多次超时或失败：{', '.join(flaky_models[:4])}（已自动启用默认槽位兜底）。",
                "fix": "建议在「能力」页为对应角色设置更稳的备选模型。",
                "metrics": {
                    "available": len(available_models),
                    "flaky": flaky_models[:6],
                    "tracked": len(health_snap),
                },
            }
        )
    else:
        checks.append(
            {
                "id": "models",
                "title": "模型目录与健康度",
                "status": "ok",
                "summary": f"宿主可用模型 {len(available_models)} 个，已绑定模型状态正常。",
                "fix": "",
                "metrics": {
                    "available": len(available_models),
                    "tracked": len(health_snap),
                },
            }
        )

    # 3. 公网检索能力（覆盖宿主统一联网、已接管第三方 google-search-plugin、已绑定 MCP/API 与其他宿主搜索插件）
    web_status: dict[str, Any] = {}
    web_err = ""
    try:
        if getattr(plugin, "runtime", None) is not None and getattr(
            plugin.runtime, "web", None
        ) is not None:
            web_status = await asyncio.wait_for(
                plugin.runtime.web.status(), timeout=4.0
            )
    except Exception as exc:
        web_err = str(redact(str(exc)))[:120]

    ready_channels: list[str] = []
    # 通道 A：宿主内置统一联网（传空 query 仅触发配置校验，不消耗外部额度）
    try:
        raw_web_probe = await call_capability(
            plugin.ctx, "web.search", timeout_ms=2500, query=""
        )
        if isinstance(raw_web_probe, dict):
            if raw_web_probe.get("success") is not False:
                ready_channels.append("宿主统一联网（web.search）")
            else:
                probe_err_txt = str(raw_web_probe.get("error") or "")
                if "query 必须是非空字符串" in probe_err_txt:
                    ready_channels.append("宿主统一联网（web.search）")
    except Exception as exc:
        if "query 必须是非空字符串" in str(exc):
            ready_channels.append("宿主统一联网（web.search）")

    # 通道 B：Maisya 原生直通接管的第三方插件（xxxxx7258.google-search-plugin）
    tp_enabled = bool(web_status.get("third_party_enabled")) if isinstance(web_status, dict) else False
    tp_engines = list(web_status.get("third_party_engines") or []) if isinstance(web_status, dict) else []
    tp_default = str(web_status.get("third_party_default") or "").strip() if isinstance(web_status, dict) else ""
    if tp_enabled and (tp_engines or tp_default):
        eng_desc = f"默认：{tp_default or tp_engines[0]}"
        if tp_engines:
            eng_desc += f"，已启用 {', '.join(str(e) for e in tp_engines[:4])}"
        ready_channels.append(f"第三方插件 google-search-plugin（{eng_desc}）")

    # 通道 C：Maisya 外部服务登记的检索通道（MCP / 跨插件 API / 自定义 provider）
    services_bound = list(web_status.get("services_bound") or []) if isinstance(web_status, dict) else []
    custom_providers = list(web_status.get("custom_providers") or []) if isinstance(web_status, dict) else []
    if "search" in services_bound or custom_providers:
        bound_label = ", ".join(custom_providers[:3]) if custom_providers else "services.web.search"
        ready_channels.append(f"外部检索服务（{bound_label}）")

    # 通道 D：兼容旧结构或扫描宿主已加载的其他第三方搜索插件
    other_search_plugins: list[str] = []
    if not ready_channels:
        try:
            raw_loaded = await call_capability(
                plugin.ctx, "component.list_loaded_plugins", timeout_ms=2500
            )
            loaded_items = (
                raw_loaded.get("plugins") or []
                if isinstance(raw_loaded, dict)
                else (raw_loaded if isinstance(raw_loaded, list) else [])
            )
            search_kw = (
                "search",
                "tavily",
                "exa",
                "bocha",
                "serp",
                "brave",
                "searx",
                "duckduckgo",
                "搜索",
                "联网",
            )
            ignore_kw = ("eh-search", "history-search", "soutubot")
            for item in loaded_items:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("id") or "").strip()
                pname = str(item.get("name") or "").strip()
                hay = f"{pid} {pname}".lower()
                if (
                    not pid
                    or pid in {"chartyr.maisya", "xxxxx7258.google-search-plugin"}
                    or any(ig in hay for ig in ignore_kw)
                ):
                    continue
                if any(kw in hay for kw in search_kw):
                    other_search_plugins.append(f"{pname}({pid})" if pname and pname != pid else pid)
        except Exception:
            other_search_plugins = []

    if ready_channels:
        checks.append(
            {
                "id": "web_search",
                "title": "公网检索能力",
                "status": "ok",
                "summary": f"公网检索可用：{'；'.join(ready_channels)}。",
                "fix": "",
                "metrics": {
                    "ready_count": len(ready_channels),
                    "third_party_engines": tp_engines[:6],
                },
            }
        )
    elif other_search_plugins:
        checks.append(
            {
                "id": "web_search",
                "title": "公网检索能力",
                "status": "info",
                "summary": f"检测到宿主已加载第三方搜索插件（{', '.join(other_search_plugins[:3])}）：主脑对话可直接调用其工具；后台 Agent 线程尚未绑定专属检索源。",
                "fix": "若需让 Maisya 后台线程 web_search 也复用外部检索，可在「能力 -> 外部连接」登记对应接口，或使用 google-search-plugin / 宿主统一联网。",
                "metrics": {"ready_count": 0, "other_plugins": other_search_plugins[:4]},
            }
        )
    elif web_err:
        checks.append(
            {
                "id": "web_search",
                "title": "公网检索能力",
                "status": "warn",
                "summary": f"检索通道状态检查失败：{web_err}",
                "fix": "在「能力 -> 外部连接」核对搜索通道配置或联网权限。",
                "metrics": {"ready_count": 0},
            }
        )
    else:
        checks.append(
            {
                "id": "web_search",
                "title": "公网检索能力",
                "status": "warn",
                "summary": "尚未检测到可用的联网搜索通道（宿主统一联网与第三方检索插件均未就绪）。",
                "fix": "可启用宿主统一联网、启用并配置 google-search-plugin，或在「能力 -> 外部连接」接入 MCP 检索服务。",
                "metrics": {"ready_count": 0},
            }
        )

    # 4. 离线图文渲染 (html_render)
    render_ok = False
    render_err = ""
    try:
        raw_render = await call_capability(
            plugin.ctx,
            "render.html2png",
            timeout_ms=4500,
            html="<div style='padding:4px;font:12px sans-serif'>ok</div>",
            selector="div",
            viewport={"width": 64, "height": 32},
            device_scale_factor=1.0,
            full_page=False,
            allow_network=False,
        )
        if isinstance(raw_render, dict) and raw_render.get("success") is not False:
            inner = raw_render.get("result") if isinstance(raw_render.get("result"), dict) else raw_render
            if any(
                inner.get(k)
                for k in ("image_base64", "png_base64", "base64", "data", "bytes", "path")
            ):
                render_ok = True
            else:
                render_err = "宿主渲染接口返回空图像"
        else:
            render_err = str(
                (raw_render.get("error") if isinstance(raw_render, dict) else "")
                or "宿主未提供 render.html2png 能力"
            )
    except Exception as exc:
        render_err = str(exc)

    if render_ok:
        checks.append(
            {
                "id": "html_render",
                "title": "离线图文卡片渲染",
                "status": "ok",
                "summary": "离线 HTML 转图片（render.html2png）实测正常，可直接出图卡与简报。",
                "fix": "",
                "metrics": {"ready": True},
            }
        )
    else:
        checks.append(
            {
                "id": "html_render",
                "title": "离线图文卡片渲染",
                "status": "warn",
                "summary": f"离线出图暂不可用（{str(redact(render_err))[:100]}）；纯文字与网页研究不受影响。",
                "fix": "如需生成精美图卡或群日报图片，请确认宿主已安装并启用 html2png / Playwright 渲染能力。",
                "metrics": {"ready": False},
            }
        )

    # 5. 容器沙箱环境
    docker_info = {"enabled": False, "ready": False, "available": False, "error": ""}
    if mgmt is not None and hasattr(mgmt, "_docker_status"):
        try:
            docker_info = await completed_thread(mgmt._docker_status)
        except Exception as exc:
            docker_info["error"] = str(exc)
    else:
        docker_info["enabled"] = bool(
            getattr(getattr(settings, "sandbox", None), "enabled", False)
        )
    if not docker_info.get("enabled"):
        checks.append(
            {
                "id": "sandbox",
                "title": "容器沙箱环境",
                "status": "info",
                "summary": "当前为轻量无容器模式（查资料、精确计算、离线出图与日常任务全部可用）。",
                "fix": "如需让 Agent 在隔离容器内跑 Python / Shell 脚本，可在设置页高级项中开启 Docker 沙箱。",
                "metrics": {"enabled": False, "ready": False},
            }
        )
    elif docker_info.get("ready") and docker_info.get("available"):
        checks.append(
            {
                "id": "sandbox",
                "title": "容器沙箱环境",
                "status": "ok",
                "summary": "Docker 隔离沙箱已就绪，可安全执行脚本与数据处理。",
                "fix": "",
                "metrics": {"enabled": True, "ready": True},
            }
        )
    else:
        d_err = str(redact(str(docker_info.get("error") or "Docker 不可用")))[:100]
        checks.append(
            {
                "id": "sandbox",
                "title": "容器沙箱环境",
                "status": "warn",
                "summary": f"已开启沙箱但当前未就绪（{d_err}），线程将自动降级为轻量工作区模式。",
                "fix": "确认宿主机 Docker 守护进程已启动且权限正常，或关闭沙箱使用轻量模式。",
                "metrics": {"enabled": True, "ready": False},
            }
        )

    # 6. 能力档案、备选模型与自省健康
    single_model_no_fb = [
        p.id
        for p in enabled_profiles
        if str(getattr(p, "model_name", "") or getattr(p, "model", "") or "").strip()
        and not (getattr(p, "fallback_models", ()) or ())
    ]
    services_cfg = {}
    if getattr(plugin, "runtime", None) is not None and getattr(
        plugin.runtime, "services", None
    ) is not None:
        try:
            services_cfg = await completed_thread(plugin.runtime.services.load)
        except Exception:
            services_cfg = {}
    known_mcps = set((services_cfg.get("servers") or {}).keys())
    known_apis = set((services_cfg.get("apis") or {}).keys())
    broken_bindings = []
    for p in enabled_profiles:
        for m in getattr(p, "mcp_servers", ()) or ():
            if m not in known_mcps:
                broken_bindings.append(f"{p.id}:mcp/{m}")
        for a in getattr(p, "apis", ()) or ():
            if a not in known_apis:
                broken_bindings.append(f"{p.id}:api/{a}")

    learning_diag = {}
    learning_obj = getattr(getattr(plugin, "runtime", None), "learning", None)
    if learning_obj is not None and hasattr(learning_obj, "diagnostics"):
        learning_diag = learning_obj.diagnostics()

    if not enabled_profiles:
        checks.append(
            {
                "id": "profiles",
                "title": "能力档案与扩展绑定",
                "status": "error",
                "summary": "当前没有任何已启用的能力档案，MaiBot 将无法派发后台线程。",
                "fix": "在「能力」页至少启用默认角色，或套用一个场景模板。",
                "metrics": {"total": len(profiles_list), "enabled": 0},
            }
        )
    elif broken_bindings:
        checks.append(
            {
                "id": "profiles",
                "title": "能力档案与扩展绑定",
                "status": "warn",
                "summary": f"检测到失效的扩展引用：{', '.join(broken_bindings[:4])}。",
                "fix": "在「能力」页重新登记对应 MCP/API 或取消勾选失效绑定。",
                "metrics": {
                    "total": len(profiles_list),
                    "enabled": len(enabled_profiles),
                    "broken_bindings": broken_bindings[:6],
                },
            }
        )
    elif single_model_no_fb:
        checks.append(
            {
                "id": "profiles",
                "title": "能力档案与扩展绑定",
                "status": "warn",
                "summary": f"已启用 {len(enabled_profiles)} 个档案；其中 {', '.join(single_model_no_fb[:4])} 仅绑了单模型未配备选（超时时已自动回退宿主默认槽位）。",
                "fix": "建议在「能力」页为单模型角色顺手补充 1 个备选模型，高峰期更稳。",
                "metrics": {
                    "total": len(profiles_list),
                    "enabled": len(enabled_profiles),
                    "single_model_no_fallback": single_model_no_fb[:6],
                    "learning_scheduled": int(learning_diag.get("scheduled") or 0),
                },
            }
        )
    else:
        checks.append(
            {
                "id": "profiles",
                "title": "能力档案与扩展绑定",
                "status": "ok",
                "summary": f"已启用 {len(enabled_profiles)}/{len(profiles_list)} 个能力档案，模型与扩展绑定健康。",
                "fix": "",
                "metrics": {
                    "total": len(profiles_list),
                    "enabled": len(enabled_profiles),
                    "learning_scheduled": int(learning_diag.get("scheduled") or 0),
                },
            }
        )

    worst = "ok"
    counts = {"ok": 0, "warn": 0, "error": 0, "info": 0}
    for c in checks:
        st = str(c.get("status") or "info")
        counts[st] = counts.get(st, 0) + 1
        if _status_rank(st) > _status_rank(worst):
            worst = st
    overall = "ok" if worst == "info" else worst
    summary = (
        f"共 {len(checks)} 项检查：{counts['ok']} 项正常"
        + (f"，{counts['info']} 项提示" if counts["info"] else "")
        + (f"，{counts['warn']} 项建议关注" if counts["warn"] else "")
        + (f"，{counts['error']} 项异常" if counts["error"] else "")
        + "。"
    )
    return redact(
        {
            "status": overall,
            "summary": summary,
            "checked_at": round(time.time(), 3),
            "counts": counts,
            "checks": checks,
            "model_health": health_snap,
        }
    )
