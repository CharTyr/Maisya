"""Maisya 插件版本更新检测、缓存与忽略状态管理。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .pathsafe import path_is_link_like
from .vault import atomic_private_write

logger = logging.getLogger(__name__)

DEFAULT_REPOSITORY_URL = "https://github.com/CharTyr/Maisya"
DEFAULT_CHECK_INTERVAL_SECONDS = 43200.0  # 12 小时
DEFAULT_ERROR_RETRY_SECONDS = 3600.0  # 失败后 1 小时重试
DEFAULT_STARTUP_DELAY_SECONDS = 30.0
_HTTP_TIMEOUT_SECONDS = 6.0
_MAX_HTTP_BYTES = 256 * 1024

_SEMVER_RE = re.compile(
    r"^[vV]?(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_GITHUB_REPO_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def parse_semver(raw: object) -> tuple[tuple[int, int, int, int], tuple[int, str]] | None:
    """解析语义化版本号，返回 ((major, minor, patch, extra), (is_stable, prerelease))。"""
    text = str(raw or "").strip()
    if not text:
        return None
    match = _SEMVER_RE.fullmatch(text)
    if match is None:
        return None
    core = (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3) or 0),
        int(match.group(4) or 0),
    )
    pre = str(match.group(5) or "").strip()
    return (core, (0 if pre else 1, pre))


def _normalize_version_str(raw: object) -> str:
    text = str(raw or "").strip()
    if text[:1] in {"v", "V"} and len(text) > 1 and text[1].isdigit():
        return text[1:]
    return text


def _prerelease_sort_key(pre: str) -> tuple[tuple[int, int | str], ...]:
    parts = []
    for seg in pre.split("."):
        if seg.isdigit():
            parts.append((0, int(seg)))
        else:
            parts.append((1, seg.lower()))
    return tuple(parts)


def is_newer_version(latest: object, current: object) -> bool:
    """判断 latest 是否严格高于 current。无法解析时返回 False。"""
    p_latest = parse_semver(latest)
    p_current = parse_semver(current)
    if p_latest is None or p_current is None:
        return False
    l_core, (l_stable, l_pre) = p_latest
    c_core, (c_stable, c_pre) = p_current
    if l_core != c_core:
        return l_core > c_core
    if l_stable != c_stable:
        return l_stable > c_stable
    if l_stable == 0 and c_stable == 0:
        return _prerelease_sort_key(l_pre) > _prerelease_sort_key(c_pre)
    return False


def is_prerelease(raw: object) -> bool:
    parsed = parse_semver(raw)
    return bool(parsed is not None and parsed[1][0] == 0)


def read_manifest_info(plugin_root: Path | None = None) -> dict[str, str]:
    """从插件根目录 `_manifest.json` 读取当前版本与 GitHub 仓库信息。"""
    root = plugin_root or Path(__file__).resolve().parent.parent
    manifest_path = root / "_manifest.json"
    version = "0.0.0"
    repo_url = DEFAULT_REPOSITORY_URL
    try:
        if manifest_path.is_file() and not path_is_link_like(manifest_path):
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                v = _normalize_version_str(raw.get("version") or "")
                if v:
                    version = v
                urls = raw.get("urls") if isinstance(raw.get("urls"), dict) else {}
                cand_repo = str(
                    urls.get("repository") or urls.get("homepage") or ""
                ).strip()
                if cand_repo.startswith("https://github.com/"):
                    repo_url = cand_repo.rstrip("/")
                    if repo_url.endswith(".git"):
                        repo_url = repo_url[:-4]
    except (OSError, ValueError, TypeError):
        pass
    match = _GITHUB_REPO_RE.fullmatch(repo_url)
    owner = match.group(1) if match else "CharTyr"
    repo = match.group(2) if match else "Maisya"
    clean_repo_url = f"https://github.com/{owner}/{repo}"
    return {
        "current_version": version,
        "owner": owner,
        "repo": repo,
        "repository_url": clean_repo_url,
    }


def _clean_release_notes(
    raw_body: object, *, max_chars: int = 1200
) -> tuple[str, str]:
    """清洗 GitHub Release 正文为纯文本摘要与单行亮点。"""
    text = str(raw_body or "")
    text = _HTML_COMMENT_RE.sub("", text)
    text = _HTML_TAG_RE.sub("", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    cleaned_lines: list[str] = []
    summary = ""
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        cleaned_lines.append(stripped)
        if not summary and not stripped.startswith("#"):
            plain = re.sub(r"^[-*+•]\s+", "", stripped)
            plain = re.sub(r"[*_`~]", "", plain).strip()
            if plain:
                summary = plain[:140]

    notes = "\n".join(cleaned_lines).strip()
    if len(notes) > max_chars:
        notes = notes[: max_chars - 1].rstrip() + "…"
    if not summary and notes:
        first_non_empty = next(
            (ln.lstrip("#").strip() for ln in notes.split("\n") if ln.strip()), ""
        )
        summary = re.sub(r"[*_`~]", "", first_non_empty).strip()[:140]
    return notes, summary


def _safe_github_url(url: object, owner: str, repo: str, tag: str = "") -> str:
    cand = str(url or "").strip()
    prefix = f"https://github.com/{owner}/{repo}"
    if cand.startswith(prefix):
        return cand
    if tag:
        return f"{prefix}/releases/tag/{tag}"
    return f"{prefix}/releases"


def _http_get_json(
    url: str, *, headers: dict[str, str], timeout: float = _HTTP_TIMEOUT_SECONDS
) -> tuple[int, Any, dict[str, str]]:
    if not url.startswith("https://api.github.com/repos/"):
        raise ValueError("仅允许访问 GitHub 官方仓库接口")
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            raw = resp.read(_MAX_HTTP_BYTES + 1)
            if len(raw) > _MAX_HTTP_BYTES:
                raise RuntimeError("GitHub 响应体超出大小上限")
            data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            return status, data, resp_headers
    except urllib.error.HTTPError as err:
        resp_headers = {k.lower(): v for k, v in (err.headers.items() if err.headers else ())}
        if err.code == 304:
            return 304, None, resp_headers
        if err.code == 404:
            return 404, None, resp_headers
        if err.code in {403, 429}:
            raise RuntimeError("GitHub 接口限流，稍后自动重试") from err
        raise RuntimeError(f"GitHub 接口返回异常状态（HTTP {err.code}）") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise RuntimeError("暂无法连接 GitHub（网络超时或不可达）") from err
    except ValueError as err:
        raise RuntimeError("GitHub 返回数据解析失败") from err


def fetch_github_latest(
    owner: str,
    repo: str,
    *,
    etag: str = "",
    current_version: str = "0.0.0",
) -> dict[str, Any]:
    """查询 GitHub 最新正式 Release；若仓库尚无 Release（404）则回退检测稳定 Tag。"""
    base_headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Maisya/{current_version or '0.0.0'}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    rel_headers = dict(base_headers)
    if etag:
        rel_headers["If-None-Match"] = etag

    rel_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    status, data, resp_headers = _http_get_json(rel_url, headers=rel_headers)
    if status == 304:
        return {"not_modified": True, "etag": resp_headers.get("etag") or etag}

    if status == 200 and isinstance(data, dict):
        tag_name = str(data.get("tag_name") or "").strip()
        if tag_name and not data.get("draft") and not data.get("prerelease"):
            latest_ver = _normalize_version_str(tag_name)
            notes, summary = _clean_release_notes(data.get("body") or "")
            return {
                "latest_version": latest_ver,
                "release_name": str(data.get("name") or tag_name).strip()[:120],
                "release_url": _safe_github_url(
                    data.get("html_url"), owner, repo, tag_name
                ),
                "published_at": str(data.get("published_at") or "").strip()[:40],
                "notes": notes,
                "summary": summary,
                "etag": resp_headers.get("etag") or "",
                "source": "github_release",
            }

    # 404 或暂无正式 Release：回退到 tags 列表取最高稳定版本
    tags_url = f"https://api.github.com/repos/{owner}/{repo}/tags?per_page=10"
    t_status, t_data, t_headers = _http_get_json(tags_url, headers=base_headers)
    if t_status == 200 and isinstance(t_data, list):
        best_tag = ""
        best_ver = ""
        for item in t_data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or is_prerelease(name) or parse_semver(name) is None:
                continue
            norm = _normalize_version_str(name)
            if not best_ver or is_newer_version(norm, best_ver):
                best_ver = norm
                best_tag = name
        if best_ver:
            return {
                "latest_version": best_ver,
                "release_name": f"Maisya {best_tag}",
                "release_url": _safe_github_url("", owner, repo, best_tag),
                "published_at": "",
                "notes": "",
                "summary": "",
                "etag": t_headers.get("etag") or "",
                "source": "github_tag",
            }

    return {
        "latest_version": "",
        "release_name": "",
        "release_url": f"https://github.com/{owner}/{repo}/releases",
        "published_at": "",
        "notes": "",
        "summary": "",
        "etag": "",
        "source": "",
    }


class UpdateChecker:
    """Maisya 异步更新检测器：带持久化缓存、ETag、版本忽略与单次私聊防重。"""

    def __init__(
        self,
        state_path: Path,
        *,
        plugin_root: Path | None = None,
        check_enabled: Callable[[], bool] = lambda: True,
        notify_admin: Callable[[], bool] = lambda: True,
        interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        startup_delay_seconds: float = DEFAULT_STARTUP_DELAY_SECONDS,
        fetcher: Callable[..., dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
        redact: Callable[[Any], Any] = lambda v: v,
    ) -> None:
        self.state_path = Path(state_path)
        self.plugin_root = plugin_root
        self._check_enabled = check_enabled
        self._notify_admin = notify_admin
        self.interval_seconds = max(60.0, float(interval_seconds))
        self.startup_delay_seconds = max(0.0, float(startup_delay_seconds))
        self._fetcher = fetcher
        self._clock = clock
        self._redact = redact
        self._started_at = float(clock())
        self._checking = False
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._manifest = read_manifest_info(plugin_root)
        self._state: dict[str, Any] = self._load_state()

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def _default_state(self) -> dict[str, Any]:
        return {
            "checked_at": 0.0,
            "latest_version": "",
            "release_name": "",
            "release_url": f"{self._manifest['repository_url']}/releases",
            "published_at": "",
            "summary": "",
            "notes": "",
            "etag": "",
            "source": "",
            "last_error": "",
            "dismissed_version": "",
            "notified_version": "",
            "notified_at": 0.0,
        }

    def _load_state(self) -> dict[str, Any]:
        base = self._default_state()
        try:
            if self.state_path.is_file() and not path_is_link_like(self.state_path):
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key in base:
                        if key in raw and raw[key] is not None:
                            base[key] = raw[key]
        except (OSError, ValueError, TypeError):
            pass
        return base

    def _save_state(self) -> None:
        try:
            atomic_private_write(
                self.state_path,
                json.dumps(self._state, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        except OSError as exc:
            logger.debug("更新状态持久化失败: %s", exc)

    def _is_check_enabled(self) -> bool:
        try:
            return bool(self._check_enabled())
        except Exception:
            return True

    def _is_notify_admin(self) -> bool:
        try:
            return bool(self._notify_admin())
        except Exception:
            return True

    def status(self) -> dict[str, Any]:
        """返回纯内存快照（零网络 I/O），供 WebUI、overview 与 Pulse 读取。"""
        current = self._manifest["current_version"]
        latest = _normalize_version_str(self._state.get("latest_version") or "")
        has_update = is_newer_version(latest, current)
        dismissed_ver = _normalize_version_str(self._state.get("dismissed_version") or "")
        dismissed = bool(has_update and dismissed_ver and dismissed_ver == latest)
        repo_url = self._manifest["repository_url"]
        rel_url = str(self._state.get("release_url") or "").strip() or f"{repo_url}/releases"
        return {
            "current_version": current,
            "latest_version": latest,
            "has_update": has_update,
            "dismissed": dismissed,
            "show_banner": bool(has_update and not dismissed),
            "dismissed_version": dismissed_ver,
            "notified_version": _normalize_version_str(
                self._state.get("notified_version") or ""
            ),
            "notified_at": float(self._state.get("notified_at") or 0.0),
            "release_name": str(self._state.get("release_name") or ""),
            "release_url": rel_url,
            "repository_url": repo_url,
            "published_at": str(self._state.get("published_at") or ""),
            "summary": str(self._state.get("summary") or ""),
            "notes": str(self._state.get("notes") or ""),
            "source": str(self._state.get("source") or ""),
            "checked_at": float(self._state.get("checked_at") or 0.0),
            "last_error": str(self._state.get("last_error") or ""),
            "checking": bool(self._checking),
            "check_enabled": self._is_check_enabled(),
            "notify_admin": self._is_notify_admin(),
        }

    def due_for_background_check(self) -> bool:
        """判断当前是否应执行一次静默后台版本检测。"""
        if self._checking or not self._is_check_enabled():
            return False
        # 防止单元测试在未注入自定义 fetcher 时触发真实外网请求
        if self._fetcher is None and os.environ.get("PYTEST_CURRENT_TEST"):
            return False
        now = float(self._clock())
        if now - self._started_at < self.startup_delay_seconds:
            return False
        checked_at = float(self._state.get("checked_at") or 0.0)
        if checked_at <= 0.0:
            return True
        wait_secs = (
            min(DEFAULT_ERROR_RETRY_SECONDS, self.interval_seconds)
            if self._state.get("last_error")
            else self.interval_seconds
        )
        return (now - checked_at) >= wait_secs

    async def check(self, *, force: bool = False) -> dict[str, Any]:
        """执行一次版本检查；force=True 时忽略缓存周期立即向 GitHub 查询。"""
        async with self._get_lock():
            now = float(self._clock())
            if not force:
                if not self._is_check_enabled():
                    return self.status()
                checked_at = float(self._state.get("checked_at") or 0.0)
                if checked_at > 0.0 and (now - checked_at) < self.interval_seconds:
                    return self.status()

            prev_latest = _normalize_version_str(
                self._state.get("latest_version") or ""
            )
            use_etag = str(self._state.get("etag") or "")
            if force and (self._state.get("last_error") or not prev_latest):
                use_etag = ""

            fetch_fn = self._fetcher or fetch_github_latest
            self._checking = True
            try:
                res = await asyncio.to_thread(
                    fetch_fn,
                    self._manifest["owner"],
                    self._manifest["repo"],
                    etag=use_etag,
                    current_version=self._manifest["current_version"],
                )
            except Exception as exc:
                err_msg = str(self._redact(str(exc)))[:200] or "检查更新失败"
                self._state["checked_at"] = now
                self._state["last_error"] = err_msg
                await asyncio.to_thread(self._save_state)
                logger.debug("Maisya 更新检查失败: %s", err_msg)
                return self.status()
            finally:
                self._checking = False

            self._state["checked_at"] = now
            self._state["last_error"] = ""

            if isinstance(res, dict) and not res.get("not_modified"):
                latest_ver = _normalize_version_str(res.get("latest_version") or "")
                raw_notes = str(res.get("notes") or "")
                notes, auto_summary = _clean_release_notes(raw_notes)
                summary = str(res.get("summary") or auto_summary or "").strip()[:140]
                self._state["latest_version"] = latest_ver
                self._state["release_name"] = str(res.get("release_name") or "")[:120]
                self._state["release_url"] = _safe_github_url(
                    res.get("release_url"),
                    self._manifest["owner"],
                    self._manifest["repo"],
                    f"v{latest_ver}" if latest_ver else "",
                )
                self._state["published_at"] = str(res.get("published_at") or "")[:40]
                self._state["notes"] = str(self._redact(notes))[:1200]
                self._state["summary"] = str(self._redact(summary))[:140]
                self._state["source"] = str(res.get("source") or "")[:32]
                if "etag" in res:
                    self._state["etag"] = str(res.get("etag") or "")[:128]
            elif isinstance(res, dict) and res.get("etag"):
                self._state["etag"] = str(res.get("etag") or "")[:128]

            await asyncio.to_thread(self._save_state)
            st = self.status()
            if st["has_update"] and st["latest_version"] != prev_latest:
                logger.info(
                    "Maisya 发现新版本 v%s（当前 v%s）：%s",
                    st["latest_version"],
                    st["current_version"],
                    st["release_url"],
                )
            return st

    def dismiss(self, version: str = "") -> dict[str, Any]:
        """忽略指定版本（默认当前检测到的最新版本）的横幅与私聊提醒。"""
        target = _normalize_version_str(
            version or self._state.get("latest_version") or ""
        )
        if target:
            self._state["dismissed_version"] = target
            self._save_state()
        return self.status()

    def undismiss(self) -> dict[str, Any]:
        """恢复已忽略版本的更新提醒。"""
        if self._state.get("dismissed_version"):
            self._state["dismissed_version"] = ""
            self._save_state()
        return self.status()

    def should_notify_admin(self) -> bool:
        """判断是否需要向管理员发送一次私聊更新提醒。"""
        if not self._is_notify_admin():
            return False
        st = self.status()
        if not st["has_update"] or st["dismissed"]:
            return False
        return st["latest_version"] != st["notified_version"]

    def mark_notified(self, version: str = "") -> None:
        """记录指定版本已完成管理员私聊提醒，避免重复打扰。"""
        target = _normalize_version_str(
            version or self._state.get("latest_version") or ""
        )
        if not target:
            return
        self._state["notified_version"] = target
        self._state["notified_at"] = float(self._clock())
        self._save_state()
