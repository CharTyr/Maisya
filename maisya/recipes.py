"""角色能力配方（Recipe Template）草稿体检、SSRF 安全拉取、缺件预览与平滑升级。"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import ipaddress
import json
import re
import shutil
import socket
import ssl
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from .abilities import ALLOWED_TASKS, FISH_SEED_RE, HOST_TOOLS, READONLY_TOOLS, SANDBOX_TOOLS
from .memory import (
    PRESETS_DIR,
    SKILL_ID_RE,
    _SKILL_FILE_MAX_BYTES,
    MemoryLayers,
    _is_archive_junk,
    _normalize_archive_path,
    parse_skill_meta,
)
from .pathsafe import check_part, path_is_link_like
from .vault import VaultError, atomic_private_write, private_directory, relative_symlink_parts

def _preset_profile_ids() -> set[str]:
    profiles_dir = PRESETS_DIR / "profiles"
    if not profiles_dir.is_dir():
        return set()
    return {
        entry.name
        for entry in profiles_dir.iterdir()
        if entry.is_dir()
        and ((entry / "PROFILE.md").is_file() or (entry / "profile.toml").is_file())
    }


RECIPE_SCHEMA_VERSION = 1
MAX_RECIPE_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_RECIPE_UNPACKED_BYTES = 16 * 1024 * 1024
MAX_RECIPE_FILE_BYTES = 4 * 1024 * 1024
MAX_RECIPE_ENTRIES = 512

_RAW_TOKEN_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{16,})\b"
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/|/root/|[A-Za-z]:\\Users\\)[^\s\"'`<>]{1,120}")
_NUMERIC_ID_RE = re.compile(r"(?<![.\d/:-])\b[1-9]\d{7,11}\b(?![.\d/:-])")
_SESSION_MARKER_RE = re.compile(r"本群|群名|群号|群友称呼")

_PROFILE_KEY_ORDER = (
    "title",
    "fish_seed",
    "enabled",
    "bound",
    "environment",
    "network",
    "access",
    "task",
    "model",
    "model_name",
    "model_list",
    "fallback_models",
    "max_tokens",
    "context_tokens",
    "temperature",
    "max_steps",
    "web_provider",
    "tools",
    "skills",
    "mcp",
    "apis",
    "secrets",
    "template",
    "sandbox_targets",
    "allow_chats",
    "allow_users",
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        host,
        pinned_ip: str,
        port=None,
        timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
        source_address=None,
        blocksize=8192,
    ):
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            source_address=source_address,
            blocksize=blocksize,
        )
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host,
        pinned_ip: str,
        port=None,
        *,
        server_hostname: str = "",
        timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
        source_address=None,
        context=None,
        blocksize=8192,
    ):
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            source_address=source_address,
            context=context,
            blocksize=blocksize,
        )
        self._pinned_ip = pinned_ip
        self._server_hostname = server_hostname or self.host

    def connect(self):
        raw_sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self.sock = raw_sock
            self._tunnel()
            server_name = self._tunnel_host
        else:
            server_name = self._server_hostname
        tls_ctx = self._context or ssl.create_default_context()
        self.sock = tls_ctx.wrap_socket(raw_sock, server_hostname=server_name)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_ip: str, debuglevel: int = 0):
        super().__init__(debuglevel=debuglevel)
        self._pinned_ip = pinned_ip

    def http_open(self, req):
        pinned_ip = self._pinned_ip

        def _factory(host, **kwargs):
            return _PinnedHTTPConnection(host, pinned_ip, **kwargs)

        return self.do_open(_factory, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip: str, *, server_hostname: str, debuglevel: int = 0):
        super().__init__(debuglevel=debuglevel)
        self._pinned_ip = pinned_ip
        self._server_hostname = server_hostname

    def https_open(self, req):
        pinned_ip = self._pinned_ip
        server_hostname = self._server_hostname

        def _factory(host, **kwargs):
            return _PinnedHTTPSConnection(
                host,
                pinned_ip,
                server_hostname=server_hostname,
                **kwargs,
            )

        return self.do_open(_factory, req)


_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.IPv6Network("64:ff9b:1::/48")


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj.ipv4_mapped is not None and _is_forbidden_ip(ip_obj.ipv4_mapped):
            return True
        if ip_obj.sixtofour is not None and _is_forbidden_ip(ip_obj.sixtofour):
            return True
        if ip_obj.teredo is not None:
            server_ip, client_ip = ip_obj.teredo
            if _is_forbidden_ip(server_ip) or _is_forbidden_ip(client_ip):
                return True
        if ip_obj in _NAT64_LOCAL_USE:
            return True
        if ip_obj in _NAT64_WELL_KNOWN:
            embedded_v4 = ipaddress.IPv4Address(int(ip_obj) & 0xFFFFFFFF)
            if _is_forbidden_ip(embedded_v4):
                return True
    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    ):
        return True
    if isinstance(ip_obj, ipaddress.IPv4Address):
        # Carrier-grade NAT 100.64.0.0/10
        if ip_obj in ipaddress.ip_network("100.64.0.0/10"):
            return True
    return False


def _resolve_public_endpoint(url: str) -> tuple[str, str, str, int]:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("公开配方链接不能为空")
    clean = url.strip()
    if len(clean) > 2048 or any(ch.isspace() for ch in clean):
        raise ValueError("公开配方链接格式无效")
    parsed = urllib.parse.urlsplit(clean)
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("公开配方链接仅支持 https 或 http 协议")
    if parsed.username or parsed.password:
        raise ValueError("公开配方链接不允许包含认证凭据")
    host = (parsed.hostname or "").strip().rstrip(".")
    if (
        not host
        or host.lower() in {"localhost", "localhost.localdomain"}
        or host.lower().endswith(".local")
    ):
        raise ValueError("禁止访问本机或内网域名")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("公开配方链接仅允许标准 80 或 443 端口")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        literal_ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if _is_forbidden_ip(literal_ip):
            raise ValueError("禁止从内网、回环或保留地址拉取配方")
        return clean, host, str(literal_ip), port
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"域名解析失败: {host}") from exc
    if not infos:
        raise ValueError(f"域名无可用公网地址: {host}")
    pinned_ip = ""
    for info in infos:
        ip_str = str(info[4][0]).split("%", 1)[0]
        ip_obj = ipaddress.ip_address(ip_str)
        if _is_forbidden_ip(ip_obj):
            raise ValueError("禁止从内网、回环或保留地址拉取配方")
        if not pinned_ip:
            pinned_ip = str(ip_obj)
    return clean, host, pinned_ip, port


def validate_public_recipe_url(url: str) -> str:
    clean, _host, _pinned_ip, _port = _resolve_public_endpoint(url)
    return clean


_REMOTE_FETCH_BUDGET_S = 7.5
_MAX_REMOTE_REDIRECTS = 4


class _FetchHTTPError(ValueError):
    def __init__(self, code: int):
        super().__init__(f"拉取公开配方失败 (HTTP {code})")
        self.code = code


def _github_tree_or_blob_candidates(raw_url: str) -> list[tuple[str, str]]:
    clean = str(raw_url or "").strip()
    parsed = urllib.parse.urlsplit(clean)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    parts = [p for p in path.split("/") if p]
    if host != "github.com" or len(parts) < 4 or parts[2] not in {"tree", "blob"}:
        return []
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    candidates: list[tuple[str, str]] = []
    if parts[2] == "blob":
        for split_idx in range(4, min(len(parts), 10)):
            ref = "/".join(urllib.parse.unquote(p) for p in parts[3:split_idx])
            rest = "/".join(parts[split_idx:])
            candidates.append(
                (f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{rest}", "")
            )
    else:
        for split_idx in range(4, min(len(parts) + 1, 10)):
            ref = "/".join(urllib.parse.unquote(p) for p in parts[3:split_idx])
            subpath_hint = "/".join(parts[split_idx:])
            candidates.append(
                (f"https://codeload.github.com/{owner}/{repo}/zip/{ref}", subpath_hint)
            )
    return candidates


def normalize_public_recipe_url(raw_url: str) -> tuple[str, str]:
    """将常见 GitHub 页面链接转换为可直接下载的公开归档/原始文件链接，并返回可选子目录提示。"""
    clean = str(raw_url or "").strip()
    parsed = urllib.parse.urlsplit(clean)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    parts = [p for p in path.split("/") if p]
    subpath_hint = ""

    if host == "github.com" and len(parts) >= 2:
        owner, repo = parts[0], parts[1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        if len(parts) == 2:
            return f"https://codeload.github.com/{owner}/{repo}/zip/HEAD", ""
        if len(parts) >= 5 and parts[2] == "releases" and parts[3] == "tag":
            tag_ref = "/".join(parts[4:])
            return f"https://codeload.github.com/{owner}/{repo}/zip/{tag_ref}", ""
        if len(parts) >= 4 and parts[2] == "blob":
            ref = urllib.parse.unquote(parts[3])
            rest = "/".join(parts[4:])
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{rest}", ""
        if len(parts) >= 4 and parts[2] == "tree":
            ref = urllib.parse.unquote(parts[3])
            subpath_hint = "/".join(parts[4:])
            return f"https://codeload.github.com/{owner}/{repo}/zip/{ref}", subpath_hint
    if host == "gist.github.com":
        if len(parts) == 1 and parts[0] not in {"discover", "starred", "forked"}:
            return f"https://gist.github.com/{parts[0]}/raw", ""
        if len(parts) >= 2 and not path.endswith("/raw") and "archive" not in parts:
            user, gist_id = parts[0], parts[1]
            return f"https://gist.github.com/{user}/{gist_id}/raw", ""
    return clean, subpath_hint


def _looks_like_recipe_archive(raw: bytes) -> bool:
    if not raw:
        return False
    if zipfile.is_zipfile(io.BytesIO(raw)):
        return True
    if raw[:2] == b"\x1f\x8b" or (len(raw) > 262 and raw[257:262] == b"ustar"):
        return True
    stripped = raw.strip()
    if stripped.startswith(b"{"):
        try:
            doc = json.loads(stripped.decode("utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("files"), dict):
                return True
        except ValueError:
            pass
    if re.fullmatch(rb"[A-Za-z0-9+/=\s_-]+", stripped) and len(stripped) >= 32:
        try:
            decoded = base64.b64decode(re.sub(rb"\s+", b"", stripped), validate=True)
            if zipfile.is_zipfile(io.BytesIO(decoded)):
                return True
        except ValueError:
            pass
    return False


def _http_get_safe(
    start_url: str,
    *,
    opener=None,
    headers: dict[str, str],
    timeout: float,
    deadline: float | None = None,
) -> tuple[bytes, str]:
    budget_end = (
        deadline
        if deadline is not None
        else time.monotonic() + min(float(timeout or _REMOTE_FETCH_BUDGET_S), _REMOTE_FETCH_BUDGET_S)
    )
    if time.monotonic() >= budget_end:
        raise ValueError("拉取公开配方超时（超过 8 秒预算）")
    current, host, pinned_ip, _port = _resolve_public_endpoint(start_url)
    for _hop in range(_MAX_REMOTE_REDIRECTS):
        remaining = budget_end - time.monotonic()
        if remaining <= 0:
            raise ValueError("拉取公开配方超时（超过 8 秒预算）")
        hop_opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
            _PinnedHTTPHandler(pinned_ip),
            _PinnedHTTPSHandler(pinned_ip, server_hostname=host),
        )
        req = urllib.request.Request(current, headers=headers, method="GET")
        try:
            with hop_opener.open(req, timeout=remaining) as resp:
                chunks = []
                total = 0
                while True:
                    if time.monotonic() >= budget_end:
                        raise ValueError("拉取公开配方超时（超过 8 秒预算）")
                    block = resp.read(65536)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_RECIPE_ARCHIVE_BYTES:
                        raise ValueError("远程配方包超过 8 MiB 安全上限")
                    chunks.append(block)
                raw = b"".join(chunks)
                content_disp = str(resp.headers.get("Content-Disposition") or "")
                fname_match = re.search(r'filename="?([^";\r\n]+)"?', content_disp)
                filename = (
                    fname_match.group(1).strip()
                    if fname_match
                    else Path(urllib.parse.urlsplit(current).path).name
                )
                return raw, filename
        except urllib.error.HTTPError as err:
            if err.code in {301, 302, 303, 307, 308}:
                if time.monotonic() >= budget_end:
                    raise ValueError("拉取公开配方超时（超过 8 秒预算）") from None
                location = err.headers.get("Location")
                if not location:
                    raise ValueError("远程链接重定向缺少目标地址") from None
                next_url = urllib.parse.urljoin(current, location)
                current, host, pinned_ip, _port = _resolve_public_endpoint(next_url)
                continue
            raise _FetchHTTPError(err.code) from None
        except (TimeoutError, socket.timeout):
            raise ValueError("无法连接公开配方地址: 请求超时") from None
        except urllib.error.URLError as err:
            raise ValueError(f"无法连接公开配方地址: {err.reason}") from None
    raise ValueError("公开配方链接重定向次数过多")


def fetch_public_recipe_bytes(
    url: str, *, timeout: float = _REMOTE_FETCH_BUDGET_S
) -> tuple[bytes, str, str]:
    deadline = time.monotonic() + min(
        float(timeout or _REMOTE_FETCH_BUDGET_S), _REMOTE_FETCH_BUDGET_S
    )
    raw_parsed = urllib.parse.urlsplit(str(url or "").strip())
    raw_host = (raw_parsed.hostname or "").lower()
    raw_parts = [p for p in raw_parsed.path.rstrip("/").split("/") if p]
    gist_fallback_id = ""
    if (
        raw_host == "gist.github.com"
        and not raw_parsed.path.rstrip("/").endswith("/raw")
        and "archive" not in raw_parts
    ):
        if len(raw_parts) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{6,64}", raw_parts[0]):
            gist_fallback_id = raw_parts[0]
        elif len(raw_parts) >= 2 and re.fullmatch(r"[A-Za-z0-9_-]{6,64}", raw_parts[1]):
            gist_fallback_id = raw_parts[1]

    candidates = _github_tree_or_blob_candidates(url)
    if not candidates:
        candidates = [normalize_public_recipe_url(url)]
    headers = {
        "User-Agent": "Maisya-RecipeFetcher/1.0",
        "Accept": "application/zip, application/octet-stream, application/gzip, application/json, text/plain, */*",
    }
    raw = b""
    filename = ""
    subpath_hint = ""
    last_404: _FetchHTTPError | None = None
    for idx, (cand_url, cand_subpath) in enumerate(candidates):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("拉取公开配方超时（超过 8 秒预算）")
        try:
            raw, filename = _http_get_safe(
                cand_url, headers=headers, timeout=remaining, deadline=deadline
            )
            subpath_hint = cand_subpath
            last_404 = None
            break
        except _FetchHTTPError as err:
            if err.code == 404 and idx + 1 < len(candidates):
                last_404 = err
                continue
            raise
    if last_404 is not None:
        raise last_404
    if gist_fallback_id and not _looks_like_recipe_archive(raw):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("拉取公开配方超时（超过 8 秒预算）")
        api_headers = {**headers, "Accept": "application/vnd.github+json, application/json"}
        raw, _ = _http_get_safe(
            f"https://api.github.com/gists/{gist_fallback_id}",
            headers=api_headers,
            timeout=remaining,
            deadline=deadline,
        )
        filename = f"gist-{gist_fallback_id}.json"
    return raw, filename, subpath_hint


def _format_toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_toml_value(item) for item in value) + "]"
    raise TypeError(f"不支持的 TOML 字段值类型: {type(value)!r}")


def serialize_profile_toml(config: dict) -> str:
    lines = []
    seen = set()
    for key in _PROFILE_KEY_ORDER:
        if key not in config or config[key] is None:
            continue
        if key == "fish_seed" and config[key] == "":
            continue
        seen.add(key)
        lines.append(f"{key} = {_format_toml_value(config[key])}")
    for key in sorted(set(config) - seen):
        if config[key] is None:
            continue
        if key == "fish_seed" and config[key] == "":
            continue
        lines.append(f"{key} = {_format_toml_value(config[key])}")
    return "\n".join(lines) + "\n"


def _mask_snippet(value: str) -> str:
    clean = str(value or "").strip()
    if len(clean) <= 6:
        return "***"
    return clean[:3] + "…" + clean[-2:]


def scan_recipe_privacy(files: dict[str, bytes], *, contains_secret=lambda b: False) -> list[dict]:
    """扫描待导出配方资产中的凭据明文与本机隐私特征。"""
    findings: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add(severity: str, category: str, path: str, message: str):
        key = (severity, category, f"{path}:{message}")
        if key not in seen and len(findings) < 50:
            seen.add(key)
            findings.append(
                {
                    "severity": severity,
                    "category": category,
                    "path": path,
                    "message": message,
                }
            )

    for rel_path, raw_bytes in sorted(files.items()):
        if contains_secret(raw_bytes):
            add("blocker", "vault_secret", rel_path, "检测到保险箱已登记的凭据明文，禁止导出")
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if _RAW_TOKEN_RE.search(text):
            add("blocker", "raw_token", rel_path, "检测到疑似私钥或硬编码 API Token，禁止导出")
        for m in _IPV4_RE.finditer(text):
            candidate = m.group(0)
            try:
                ip_obj = ipaddress.IPv4Address(candidate)
            except ipaddress.AddressValueError:
                continue
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                add(
                    "warning",
                    "private_ip",
                    rel_path,
                    f"包含内网或本机地址特征 ({candidate})",
                )
        for m in _EMAIL_RE.finditer(text):
            add(
                "warning",
                "email",
                rel_path,
                f"包含邮箱地址特征 ({_mask_snippet(m.group(0))})",
            )
        for m in _HOME_PATH_RE.finditer(text):
            add(
                "warning",
                "local_path",
                rel_path,
                f"包含本机绝对路径特征 ({_mask_snippet(m.group(0))})",
            )
        for m in _NUMERIC_ID_RE.finditer(text):
            add(
                "warning",
                "numeric_id",
                rel_path,
                f"包含 8–12 位数字串（可能为群号或账号 {_mask_snippet(m.group(0))}）",
            )
        for m in _SESSION_MARKER_RE.finditer(text):
            add(
                "warning",
                "session_marker",
                rel_path,
                f"包含具体群聊指向词「{m.group(0)}」",
            )
    return findings


def _extract_profile_routines(plugin, profile_id: str) -> tuple[list[dict], int]:
    """提取与该角色关联的周期/预设工作流配方，自动剥离原会话 chat_id。"""
    if plugin.threads is None:
        return [], 0
    with plugin.threads.connect() as db:
        rows = db.execute(
            "SELECT kind, source, target, reason, payload, status FROM intents "
            "WHERE status IN ('pending', 'deferred', 'done') ORDER BY created DESC LIMIT 120"
        ).fetchall()
    routines = []
    seen = set()
    stripped_chats = 0
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("profile") != profile_id:
            continue
        every_seconds = int(payload.get("every_seconds") or 0)
        if row["source"] != "workflow" and not every_seconds:
            continue
        prompt = str(payload.get("prompt") or row["reason"] or "").strip()
        reason = str(row["reason"] or prompt).strip()
        workspace = str(payload.get("workspace") or "").strip()
        kind = str(row["kind"] or "return").strip()
        dedup_key = (reason, prompt, every_seconds, workspace, kind)
        if not prompt or dedup_key in seen:
            continue
        seen.add(dedup_key)
        if str(row["target"] or "").startswith("chat:"):
            stripped_chats += 1
        rt_id = "rt_" + hashlib.sha256(
            json.dumps(dedup_key, ensure_ascii=False).encode()
        ).hexdigest()[:10]
        routines.append(
            {
                "id": rt_id,
                "title": reason[:120],
                "prompt": plugin.vault.redact(prompt)[:4000],
                "every_seconds": every_seconds,
                "in_seconds": 60,
                "workspace": workspace[:64],
                "kind": kind if kind in {"return", "proactive", "silent"} else "return",
            }
        )
        if len(routines) >= 12:
            break
    return routines, stripped_chats


def _collect_dependency_manifest(plugin, profile, *, secret_notes: dict | None = None) -> dict:
    notes_map = {
        str(k).strip(): str(v).strip()[:200]
        for k, v in (secret_notes or {}).items()
        if isinstance(k, str) and isinstance(v, (str, int, float))
    }
    services_cfg = (
        plugin.runtime.services.load()
        if getattr(plugin, "runtime", None) and getattr(plugin.runtime, "services", None)
        else {}
    )
    servers_cfg = services_cfg.get("servers") or {}
    apis_cfg = services_cfg.get("apis") or {}
    templates_cfg = services_cfg.get("templates") or {}

    if profile.environment != "sandbox":
        sandbox_req = "none"
    elif profile.requires_container:
        sandbox_req = "required_container"
    else:
        sandbox_req = "optional_light_workspace"

    host_capabilities = []
    if "html_render" in profile.tools:
        host_capabilities.append(
            {"name": "render.html2png", "purpose": "宿主离线 HTML 卡片转图片渲染"}
        )
    if "person_read" in profile.tools:
        host_capabilities.append(
            {"name": "person.get_value", "purpose": "宿主群友画像只读查询"}
        )
    if "memory_read" in profile.tools:
        host_capabilities.append(
            {"name": "A_Memorix / memory", "purpose": "宿主长期记忆只读检索"}
        )

    plugins_needed = []
    wp = str(profile.web_provider or "").strip()
    if wp == "google_search_plugin":
        plugins_needed.append(
            {"id": "xxxxx7258.google-search-plugin", "purpose": "角色指定联网搜索插件"}
        )
    elif wp.startswith("plugin:"):
        target_api = wp.split(":", 1)[1]
        plugin_id = target_api.rsplit(".", 1)[0] if "." in target_api else target_api
        plugins_needed.append(
            {"id": plugin_id, "api": target_api, "purpose": "角色指定跨插件联网接口"}
        )

    mcp_list = []
    inferred_secrets: dict[str, str] = {}
    for srv_name in profile.mcp:
        srv = servers_cfg.get(srv_name) or {}
        desc = str(srv.get("description") or "需在接收端登记同名 MCP 服务")[:160]
        creds = list((srv.get("credentials") or {}).values())
        for h in creds:
            inferred_secrets.setdefault(h, f"供 MCP 服务 {srv_name} 使用")
        mcp_list.append(
            {
                "name": srv_name,
                "readonly": srv.get("readonly", True) is not False,
                "credentials": creds,
                "description": desc,
            }
        )

    api_list = []
    for api_alias in profile.apis:
        spec = apis_cfg.get(api_alias) or {}
        api_name = str(spec.get("api_name") or "")
        if "." in api_name:
            plugins_needed.append(
                {
                    "id": api_name.rsplit(".", 1)[0],
                    "api": api_name,
                    "purpose": str(spec.get("description") or f"提供跨插件 API {api_alias}")[:120],
                }
            )
        api_list.append(
            {
                "alias": api_alias,
                "api_name": api_name,
                "readonly": spec.get("readonly", True) is not False,
                "description": str(spec.get("description") or "需在接收端登记同名跨插件 API")[:160],
            }
        )

    template_info = None
    if profile.template:
        tpl = templates_cfg.get(profile.template) or {}
        tpl_secrets = list(tpl.get("secrets") or [])
        for h in tpl_secrets:
            inferred_secrets.setdefault(h, f"供限定执行模板 {profile.template} 使用")
        template_info = {
            "name": profile.template,
            "secrets": tpl_secrets,
            "description": str(tpl.get("description") or "需在接收端配置同名凭据模板")[:160],
        }

    all_handles = []
    for h in (*profile.secrets, *inferred_secrets.keys()):
        if h not in all_handles:
            all_handles.append(h)

    credentials = [
        {
            "handle": handle,
            "purpose": notes_map.get(handle)
            or inferred_secrets.get(handle)
            or "需在接收端保险箱 (/vault set) 绑定对应密钥，配方不包含明文",
        }
        for handle in all_handles
    ]

    return {
        "task_slot": profile.task,
        "model_hint": profile.model_name or profile.model or "",
        "sandbox": sandbox_req,
        "network": profile.network,
        "web_provider": wp or "auto",
        "sandbox_targets": list(profile.sandbox_targets),
        "host_capabilities": host_capabilities,
        "plugins": plugins_needed,
        "mcp": mcp_list,
        "apis": api_list,
        "template": template_info,
        "credentials": credentials,
    }


def _render_recipe_markdown(manifest: dict) -> str:
    prof = manifest["profile"]
    deps = manifest["dependencies"]
    lines = [
        f"# {prof['title']} (`{prof['id']}`) — Maisya 角色能力配方",
        "",
        "> 本文件由 Maisya 配方模板生成。仅包含可移植的角色说明、能力配置、技能包与推荐例行工作流；",
        "> 不包含 MaiBot 核心人设、保险箱凭据明文、聊天记录、会话手账（`MEMORY.md`）或运行中工作区。",
        "",
        "## 1. 角色概览",
        f"- **原始编号**：`{prof['id']}`",
        f"- **显示标题**：{prof['title']}",
        f"- **执行环境**：`{prof['environment']}`（沙箱要求：`{deps['sandbox']}`）",
        f"- **模型槽位**：`{deps['task_slot']}`"
        + (f"（模型提示：`{deps['model_hint']}`）" if deps.get("model_hint") else ""),
        f"- **工具清单**：{', '.join(f'`{t}`' for t in prof.get('tools', [])) or '默认只读工具'}",
        "",
    ]
    skills = manifest.get("skills") or []
    lines.append("## 2. 随包技能集")
    if skills:
        for sk in skills:
            lines.append(f"- **`{sk['id']}`**（{sk.get('title') or sk['id']}）：{sk.get('description') or '无简述'}")
    else:
        lines.append("- 未捆绑额外技能")
    lines.append("")

    routines = manifest.get("routines") or []
    lines.append("## 3. 推荐例行任务（Routines）")
    if routines:
        for rt in routines:
            period = f"每 {rt['every_seconds']} 秒" if rt.get("every_seconds") else "单次触发"
            ws = f"，工作区 `{rt['workspace']}`" if rt.get("workspace") else ""
            lines.append(f"- **{rt['title']}**（{period}{ws}）：`{rt['prompt'][:140]}`")
    else:
        lines.append("- 无预设周期任务")
    lines.append("")

    lines.append("## 4. 外部依赖与本机重绑清单")
    creds = deps.get("credentials") or []
    mcps = deps.get("mcp") or []
    apis = deps.get("apis") or []
    plugins = deps.get("plugins") or []
    if not (creds or mcps or apis or plugins or deps.get("template")):
        lines.append("- 纯内置工具配方，无需额外凭据或外部服务登记，导入即可使用。")
    else:
        for c in creds:
            lines.append(f"- **保险箱凭据句柄 `{c['handle']}`**：{c['purpose']}")
        for m in mcps:
            lines.append(f"- **MCP 服务 `{m['name']}`**：{m['description']}")
        for a in apis:
            lines.append(f"- **跨插件 API `{a['alias']}`** (`{a.get('api_name') or '未指定'}`)：{a['description']}")
        if deps.get("template"):
            t = deps["template"]
            lines.append(f"- **限定执行模板 `{t['name']}`**：{t['description']}")
        for p in plugins:
            lines.append(f"- **宿主插件 `{p['id']}`**：{p['purpose']}")
    lines.append("")
    return "\n".join(lines)


def _assemble_recipe_bundle(plugin, profile_id: str, options: dict | None = None) -> dict:
    opts = options or {}
    include_skills = opts.get("include_skills", True) is not False
    include_notes = opts.get("include_notes", True) is not False
    include_presets = opts.get("include_presets", True) is not False
    include_routines = opts.get("include_routines", True) is not False
    secret_notes = opts.get("secret_notes") if isinstance(opts.get("secret_notes"), dict) else {}

    profile = plugin.profiles.get(profile_id)
    directory = plugin.profiles.path / profile_id
    raw_toml = tomllib.loads((directory / "profile.toml").read_text("utf-8"))

    stripped = {
        "allow_chats": len(raw_toml.get("allow_chats") or []),
        "allow_users": len(raw_toml.get("allow_users") or []),
        "routine_chat_ids": 0,
    }
    clean_toml = dict(raw_toml)
    clean_toml.pop("allow_chats", None)
    clean_toml.pop("allow_users", None)
    original_access = clean_toml.get("access", "everyone")
    if original_access == "listed":
        clean_toml["access"] = "admins"
        clean_toml["bound"] = False
    if not include_skills:
        clean_toml.pop("skills", None)

    files: dict[str, bytes] = {
        "PROFILE.md": (directory / "PROFILE.md").read_bytes(),
        "profile.toml": serialize_profile_toml(clean_toml).encode("utf-8"),
    }

    notes_path = directory / "NOTES.md"
    has_notes = notes_path.is_file() and not notes_path.is_symlink() and bool(notes_path.read_bytes().strip())
    if include_notes and has_notes:
        files["NOTES.md"] = notes_path.read_bytes()

    preset_files_list = []
    preset_root = directory / "preset"
    if preset_root.is_dir() and not preset_root.is_symlink():
        for item_path in sorted(preset_root.rglob("*")):
            if item_path.is_symlink():
                raise ValueError("配方资产不接受符号链接")
            if not item_path.is_file():
                continue
            rel_preset = item_path.relative_to(directory)
            if rel_preset.parts[:2] == ("preset", "skills"):
                continue
            preset_files_list.append(rel_preset.as_posix())
            if include_presets:
                if item_path.stat().st_size > MAX_RECIPE_FILE_BYTES:
                    raise ValueError(f"预设文件过大: {rel_preset.as_posix()}")
                files[rel_preset.as_posix()] = item_path.read_bytes()

    bundled_skills = []
    if include_skills:
        for skill_id in profile.skills:
            data_skill_dir = plugin.data_root / "abilities/skills" / skill_id
            if path_is_link_like(data_skill_dir):
                raise ValueError("技能路径不能经过符号链接")
            preset_skill_dir = directory / "preset/skills" / skill_id
            if (data_skill_dir / "SKILL.md").is_file() or path_is_link_like(
                data_skill_dir / "SKILL.md"
            ):
                managed_root = plugin.data_root
                skill_dir = data_skill_dir
            elif (preset_skill_dir / "SKILL.md").is_file() or path_is_link_like(
                preset_skill_dir / "SKILL.md"
            ):
                managed_root = directory
                skill_dir = preset_skill_dir
            else:
                managed_root = PRESETS_DIR
                skill_dir = PRESETS_DIR / "skills" / skill_id
                if path_is_link_like(skill_dir):
                    raise ValueError("技能路径不能经过符号链接")
            source_md = skill_dir / "SKILL.md"
            try:
                layers = relative_symlink_parts(source_md, managed_root)
            except VaultError:
                raise ValueError("技能路径不能经过符号链接") from None
            if any(path_is_link_like(layer) for layer in layers):
                raise ValueError("技能路径不能经过符号链接")
            md_bytes = MemoryLayers._bytes(source_md, max_bytes=_SKILL_FILE_MAX_BYTES)
            if not md_bytes:
                raise ValueError(f"角色绑定的技能缺失: {skill_id}")
            files[f"preset/skills/{skill_id}/SKILL.md"] = md_bytes
            companion_paths = []
            for extra_path in sorted(skill_dir.rglob("*")):
                if extra_path == source_md or extra_path.is_dir():
                    continue
                try:
                    extra_layers = relative_symlink_parts(extra_path, managed_root)
                except VaultError:
                    raise ValueError("技能附属文件路径不能经过符号链接") from None
                if any(path_is_link_like(layer) for layer in extra_layers):
                    raise ValueError("技能附属文件路径不能经过符号链接")
                rel_extra = extra_path.relative_to(skill_dir)
                if any(part.startswith(".") for part in rel_extra.parts):
                    continue
                extra_bytes = MemoryLayers._bytes(
                    extra_path, max_bytes=_SKILL_FILE_MAX_BYTES
                )
                if len(extra_bytes) > MAX_RECIPE_FILE_BYTES:
                    raise ValueError(f"技能附属文件过大: {skill_id}/{rel_extra.as_posix()}")
                files[f"preset/skills/{skill_id}/{rel_extra.as_posix()}"] = extra_bytes
                companion_paths.append(rel_extra.as_posix())
            meta = parse_skill_meta(skill_id, md_bytes.decode("utf-8", errors="replace"))
            bundled_skills.append(
                {
                    "id": skill_id,
                    "title": meta["title"],
                    "description": meta["description"],
                    "companions": companion_paths,
                }
            )

    routines, stripped_routine_chats = (
        _extract_profile_routines(plugin, profile_id) if include_routines else ([], 0)
    )
    stripped["routine_chat_ids"] = stripped_routine_chats
    dependencies = _collect_dependency_manifest(
        plugin, profile, secret_notes=secret_notes
    )

    manifest = {
        "schema": "maisya.recipe/v1",
        "schema_version": RECIPE_SCHEMA_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": {
            "id": profile.id,
            "title": profile.title,
            "fish_seed": profile.fish_seed,
            "summary": next(
                (
                    line.lstrip("#").strip()[:120]
                    for line in profile.instructions.splitlines()
                    if line.lstrip("#").strip()
                ),
                "",
            ),
            "environment": profile.environment,
            "access": clean_toml.get("access", profile.access),
            "original_access": original_access,
            "task": profile.task,
            "tools": list(profile.tools),
            "skills": list(profile.skills) if include_skills else [],
        },
        "assets": {
            "include_skills": include_skills,
            "include_notes": bool(include_notes and has_notes),
            "include_presets": bool(include_presets and preset_files_list),
            "include_routines": bool(include_routines and routines),
            "preset_files": preset_files_list if include_presets else [],
        },
        "skills": bundled_skills,
        "routines": routines,
        "dependencies": dependencies,
    }
    recipe_md = _render_recipe_markdown(manifest)
    files["recipe.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    files["RECIPE.md"] = recipe_md.encode("utf-8")

    findings = scan_recipe_privacy(
        files, contains_secret=plugin.vault.contains_secret
    )
    total_bytes = sum(len(v) for v in files.values())
    if total_bytes > MAX_RECIPE_UNPACKED_BYTES or len(files) > 256:
        raise ValueError("配方资产总量超过安全上限")

    return {
        "manifest": manifest,
        "files": files,
        "findings": findings,
        "stripped": stripped,
        "available_assets": {
            "skills_count": len(profile.skills),
            "has_notes": has_notes,
            "preset_files_count": len(preset_files_list),
            "routines_count": len(_extract_profile_routines(plugin, profile_id)[0]),
        },
    }


def build_recipe_draft(plugin, profile_id: str, options: dict | None = None) -> dict:
    bundle = _assemble_recipe_bundle(plugin, profile_id, options)
    blockers = [f for f in bundle["findings"] if f["severity"] == "blocker"]
    warnings = [f for f in bundle["findings"] if f["severity"] == "warning"]
    return {
        "profile_id": profile_id,
        "manifest": bundle["manifest"],
        "recipe_markdown": bundle["files"]["RECIPE.md"].decode("utf-8", errors="replace"),
        "findings": bundle["findings"],
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
        "can_export": len(blockers) == 0,
        "stripped": bundle["stripped"],
        "available_assets": bundle["available_assets"],
        "file_list": sorted(bundle["files"].keys()),
    }


def export_recipe_archive(plugin, profile_id: str, options: dict | None = None) -> tuple[bytes, dict]:
    opts = options or {}
    bundle = _assemble_recipe_bundle(plugin, profile_id, opts)
    blockers = [f for f in bundle["findings"] if f["severity"] == "blocker"]
    if blockers:
        raise ValueError(blockers[0]["message"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel_path, content in sorted(bundle["files"].items()):
            archive.writestr(rel_path, content)
    raw = buffer.getvalue()
    if len(raw) > MAX_RECIPE_ARCHIVE_BYTES:
        raise ValueError("导出的配方压缩包超过 8 MiB 上限")
    return raw, {
        "manifest": bundle["manifest"],
        "recipe_markdown": bundle["files"]["RECIPE.md"].decode("utf-8", errors="replace"),
        "findings": bundle["findings"],
        "stripped": bundle["stripped"],
        "bytes": len(raw),
    }


def _validated_archive_rel(raw_name: str) -> str:
    rel = _normalize_archive_path(raw_name)
    if not rel or _is_archive_junk(rel):
        return ""
    for part in rel.split("/"):
        check_part(part)
    return rel


def _extract_archive_files(
    raw_bytes: bytes, *, subpath_hint: str = "", return_prefix: bool = False
) -> dict[str, bytes] | tuple[dict[str, bytes], str]:
    if not isinstance(raw_bytes, (bytes, bytearray)) or not raw_bytes:
        raise ValueError("配方包内容为空")
    raw = bytes(raw_bytes)
    if len(raw) > MAX_RECIPE_ARCHIVE_BYTES:
        raise ValueError("配方包超过 8 MiB 上限")

    entries: dict[str, bytes] = {}
    total_unpacked = 0

    # Allow pasted Base64 text or GitHub Gist API JSON if not already binary zip/tar
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        stripped_txt = raw.strip()
        if stripped_txt.startswith(b"{"):
            try:
                gist_doc = json.loads(stripped_txt.decode("utf-8"))
            except ValueError:
                gist_doc = None
            if isinstance(gist_doc, dict) and isinstance(gist_doc.get("files"), dict):
                gist_files = gist_doc["files"]
                if len(gist_files) == 1:
                    only_item = next(iter(gist_files.values()))
                    if isinstance(only_item, dict) and not only_item.get("truncated"):
                        cand_b64 = str(only_item.get("content") or "").strip().encode("utf-8")
                        if re.fullmatch(rb"[A-Za-z0-9+/=\s_-]+", cand_b64) and len(cand_b64) >= 32:
                            try:
                                dec = base64.b64decode(re.sub(rb"\s+", b"", cand_b64), validate=True)
                                if zipfile.is_zipfile(io.BytesIO(dec)):
                                    raw = dec
                            except ValueError:
                                pass
                if not zipfile.is_zipfile(io.BytesIO(raw)):
                    if len(gist_files) > MAX_RECIPE_ENTRIES:
                        raise ValueError("配方 Gist 文件条目过多")
                    for fname, fmeta in gist_files.items():
                        if not isinstance(fmeta, dict):
                            continue
                        if fmeta.get("truncated"):
                            raise ValueError("Gist 文件过大已被截断，请改用 .zip 配方包链接")
                        rel = _validated_archive_rel(str(fname))
                        if not rel:
                            continue
                        data = str(fmeta.get("content") or "").encode("utf-8")
                        if len(data) > MAX_RECIPE_FILE_BYTES:
                            raise ValueError(f"配方文件过大: {rel}")
                        total_unpacked += len(data)
                        if total_unpacked > MAX_RECIPE_UNPACKED_BYTES:
                            raise ValueError("配方解压总量超过 16 MiB 上限")
                        entries[rel] = data
        elif re.fullmatch(rb"[A-Za-z0-9+/=\s_-]+", stripped_txt) and len(stripped_txt) >= 32:
            try:
                decoded = base64.b64decode(re.sub(rb"\s+", b"", stripped_txt), validate=True)
                if zipfile.is_zipfile(io.BytesIO(decoded)):
                    raw = decoded
            except ValueError:
                pass

    if entries:
        pass
    elif zipfile.is_zipfile(io.BytesIO(raw)):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_RECIPE_ENTRIES:
                    raise ValueError("配方压缩包条目过多")
                for info in infos:
                    if (info.external_attr >> 16) & 0o170000 not in {0, 0o100000, 0o040000}:
                        raise ValueError("配方压缩包不接受符号链接或特殊文件")
                    rel = _validated_archive_rel(info.filename)
                    if not rel or info.is_dir():
                        continue
                    if rel in entries:
                        raise ValueError(f"配方压缩包存在重复路径: {rel}")
                    if info.file_size > MAX_RECIPE_FILE_BYTES:
                        raise ValueError(f"配方文件过大: {rel}")
                    with archive.open(info, "r") as stream:
                        data = stream.read(MAX_RECIPE_FILE_BYTES + 1)
                    if len(data) > MAX_RECIPE_FILE_BYTES:
                        raise ValueError(f"配方文件过大: {rel}")
                    total_unpacked += len(data)
                    if total_unpacked > MAX_RECIPE_UNPACKED_BYTES:
                        raise ValueError("配方压缩包解压总量超过 16 MiB 上限")
                    entries[rel] = data
        except zipfile.BadZipFile:
            raise ValueError("ZIP 配方包已损坏或格式无效") from None
    else:
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
                count = 0
                for member in archive:
                    count += 1
                    if count > MAX_RECIPE_ENTRIES:
                        raise ValueError("配方压缩包条目过多")
                    if member.type in (tarfile.XHDTYPE, tarfile.XGLTYPE):
                        continue
                    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                        raise ValueError("配方压缩包不接受符号链接或特殊文件")
                    rel = _validated_archive_rel(member.name)
                    if not rel or member.isdir() or not member.isfile():
                        continue
                    if member.size > MAX_RECIPE_FILE_BYTES:
                        raise ValueError(f"配方文件过大: {rel}")
                    stream = archive.extractfile(member)
                    if stream is None:
                        continue
                    with stream:
                        data = stream.read(MAX_RECIPE_FILE_BYTES + 1)
                    if len(data) > MAX_RECIPE_FILE_BYTES:
                        raise ValueError(f"配方文件过大: {rel}")
                    total_unpacked += len(data)
                    if total_unpacked > MAX_RECIPE_UNPACKED_BYTES:
                        raise ValueError("配方压缩包解压总量超过 16 MiB 上限")
                    entries[rel] = data
        except (tarfile.TarError, EOFError, OSError):
            raise ValueError("无法识别配方包格式，请提供有效的 .zip / .tar.gz 配方包或 Base64 文本") from None

    # Locate root containing PROFILE.md + profile.toml
    hint = subpath_hint.strip("/")
    candidate_roots = []
    for rel in sorted(entries):
        if rel.endswith("/profile.toml") or rel == "profile.toml":
            prefix = rel[: -len("profile.toml")].rstrip("/")
            mirror_md = f"{prefix}/PROFILE.md" if prefix else "PROFILE.md"
            if mirror_md in entries:
                candidate_roots.append(prefix)
    if not candidate_roots:
        raise ValueError("配方包内未找到有效的 PROFILE.md 与 profile.toml")

    chosen_prefix = ""
    if hint:
        for cand in candidate_roots:
            if cand == hint or cand.endswith("/" + hint):
                chosen_prefix = cand
                break
        else:
            raise ValueError(f"配方归档中未找到指定子目录: {hint}")
    else:
        chosen_prefix = min(candidate_roots, key=lambda p: p.count("/"))

    normalized: dict[str, bytes] = {}
    prefix_slash = f"{chosen_prefix}/" if chosen_prefix else ""
    for rel, data in entries.items():
        if prefix_slash:
            if not rel.startswith(prefix_slash):
                continue
            sub = rel[len(prefix_slash) :]
        else:
            sub = rel
        if (
            sub not in {"PROFILE.md", "profile.toml", "NOTES.md", "recipe.json", "RECIPE.md"}
            and not sub.startswith("preset/")
        ):
            # In repository archives that have extra docs like README.md, ignore only if recipe.json or GitHub root wrapper was stripped;
            # for direct flat archives, enforce strict allowlist so arbitrary files cannot slip into profile dir.
            if chosen_prefix:
                continue
            raise ValueError(f"配方包含未允许的文件: {sub}")
        normalized[sub] = data

    if len(normalized) > 256:
        raise ValueError("配方内有效文件数量超过 256 上限")
    if return_prefix:
        return normalized, chosen_prefix
    return normalized


def _repack_clean_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, data in sorted(files.items()):
            zf.writestr(rel, data)
    return buf.getvalue()


def _parse_and_validate_recipe_files(plugin, files: dict[str, bytes], *, fallback_id: str = "imported") -> dict:
    for rel, data in files.items():
        if plugin.vault.contains_secret(data):
            raise ValueError(f"配方资产 {rel} 含已知凭据明文，已拒绝加载")
        if _RAW_TOKEN_RE.search(data.decode("utf-8", errors="ignore")):
            raise ValueError(f"配方资产 {rel} 含疑似私钥或硬编码 Token，已拒绝加载")

    if len(files["PROFILE.md"]) > 65536 or len(files["profile.toml"]) > 65536:
        raise ValueError("PROFILE.md 或 profile.toml 超过 64 KiB 上限")

    raw_cfg = tomllib.loads(files["profile.toml"].decode("utf-8"))
    allowed_fields = {
        "title",
        "model",
        "model_name",
        "model_list",
        "fallback_models",
        "task",
        "max_tokens",
        "context_tokens",
        "temperature",
        "max_steps",
        "web_provider",
        "environment",
        "tools",
        "access",
        "network",
        "mcp",
        "apis",
        "secrets",
        "template",
        "sandbox_targets",
        "bound",
        "enabled",
        "skills",
        "allow_users",
        "allow_chats",
        "fish_seed",
    }
    if set(raw_cfg) - allowed_fields:
        raise ValueError("配方 profile.toml 包含未声明字段")
    if "model_list" in raw_cfg:
        ml = raw_cfg["model_list"]
        if (
            not isinstance(ml, list)
            or len(ml) > 16
            or not all(isinstance(m, str) and 1 <= len(m.strip()) <= 256 for m in ml)
        ):
            raise ValueError("配方 model_list 字段格式无效")
        raw_cfg["model_list"] = [m.strip() for m in ml]
    fish_seed = raw_cfg.get("fish_seed", "")
    if fish_seed != "" and (
        not isinstance(fish_seed, str) or not FISH_SEED_RE.fullmatch(fish_seed)
    ):
        raise ValueError("配方小鱼头像种子格式无效")
    environment = raw_cfg.get("environment", "readonly")
    if environment not in {"readonly", "sandbox"}:
        raise ValueError("配方执行环境无效")
    tools = raw_cfg.get("tools", sorted(READONLY_TOOLS))
    allowed_tools = SANDBOX_TOOLS if environment == "sandbox" else HOST_TOOLS
    if not isinstance(tools, list) or not all(
        isinstance(t, str) and t in allowed_tools for t in tools
    ):
        raise ValueError("配方含未注册或越权工具")

    # Strip sender-specific chat/user allowlists on ingest as well
    raw_cfg.pop("allow_chats", None)
    raw_cfg.pop("allow_users", None)
    if raw_cfg.get("access") == "listed":
        raw_cfg["access"] = "admins"
        raw_cfg["bound"] = False

    manifest = {}
    if "recipe.json" in files:
        try:
            parsed_manifest = json.loads(files["recipe.json"].decode("utf-8"))
            if isinstance(parsed_manifest, dict):
                manifest = parsed_manifest
        except ValueError as exc:
            raise ValueError("recipe.json 格式无效") from exc

    source_id = (
        str((manifest.get("profile") or {}).get("id") or fallback_id or "imported").strip()
    )
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", source_id):
        source_id = "imported_role"

    # Extract bundled skills under preset/skills/<id>/SKILL.md
    bundled_skills: dict[str, dict] = {}
    for rel, data in sorted(files.items()):
        parts = rel.split("/")
        if len(parts) >= 2 and parts[0] == "preset" and parts[1] == "skills":
            if len(parts) < 4:
                raise ValueError(f"配方内技能目录结构无效: {rel}")
            sk_id = parts[2]
            if not SKILL_ID_RE.fullmatch(sk_id):
                raise ValueError(f"配方内技能编号无效: {sk_id}")
            entry = bundled_skills.setdefault(sk_id, {"md": b"", "companions": []})
            sub_rel = "/".join(parts[3:])
            if sub_rel == "SKILL.md":
                entry["md"] = data
            else:
                MemoryLayers._validate_companion_rel(sub_rel)
                entry["companions"].append((sub_rel, data))

    skills_preview = []
    local_skills = set(plugin.memory.skills()) if plugin.memory else set()
    preset_skills = plugin.memory.preset_skills() if plugin.memory else set()
    referenced_skills = [s for s in (raw_cfg.get("skills") or []) if isinstance(s, str)]
    for sk_id in sorted(set(referenced_skills) | set(bundled_skills)):
        b_info = bundled_skills.get(sk_id)
        if b_info and b_info["md"]:
            meta = parse_skill_meta(sk_id, b_info["md"].decode("utf-8", errors="replace"))
            skills_preview.append(
                {
                    "id": sk_id,
                    "title": meta["title"],
                    "description": meta["description"],
                    "bundled": True,
                    "companions_count": len(b_info["companions"]),
                    "exists_locally": sk_id in local_skills,
                    "is_preset": sk_id in preset_skills,
                }
            )
        else:
            skills_preview.append(
                {
                    "id": sk_id,
                    "title": sk_id,
                    "description": "未随包附带，依赖接收端本地同名技能",
                    "bundled": False,
                    "companions_count": 0,
                    "exists_locally": sk_id in local_skills,
                    "is_preset": sk_id in preset_skills,
                }
            )

    raw_routines = manifest.get("routines") if isinstance(manifest.get("routines"), list) else []
    routines = []
    for idx, item in enumerate(raw_routines[:12]):
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        title = str(item.get("title") or prompt).strip()
        if not prompt:
            continue
        every_sec = int(item.get("every_seconds") or 0)
        if every_sec and every_sec < 300:
            every_sec = 300
        routines.append(
            {
                "id": str(item.get("id") or f"rt_{idx + 1}")[:32],
                "title": title[:120],
                "prompt": prompt[:4000],
                "every_seconds": every_sec,
                "in_seconds": max(30, min(86400, int(item.get("in_seconds") or 60))),
                "workspace": re.sub(r"[^\w-]", "", str(item.get("workspace") or ""))[:64],
                "kind": item.get("kind") if item.get("kind") in {"return", "proactive", "silent"} else "return",
            }
        )

    instructions = files["PROFILE.md"].decode("utf-8", errors="replace")
    summary = next(
        (
            line.lstrip("#").strip()[:140]
            for line in instructions.splitlines()
            if line.lstrip("#").strip()
        ),
        "",
    )
    return {
        "source_id": source_id,
        "config": raw_cfg,
        "instructions": instructions,
        "summary": summary,
        "has_notes": "NOTES.md" in files,
        "preset_files": sorted(
            rel
            for rel in files
            if rel.startswith("preset/") and not rel.startswith("preset/skills/")
        ),
        "bundled_skills": bundled_skills,
        "skills": skills_preview,
        "routines": routines,
        "manifest": manifest,
    }


def compute_recipe_alignment(plugin, parsed: dict, *, discovery: dict | None = None) -> dict:
    """对照本机宿主探测、保险箱句柄、已登记服务与沙箱状态生成缺件对齐报告。"""
    cfg = parsed["config"]
    deps = (parsed.get("manifest") or {}).get("dependencies") or {}
    disc = discovery or {}
    vault_handles = set(plugin.vault.handles()) if plugin.vault else set()
    services_cfg = (
        plugin.runtime.services.load()
        if getattr(plugin, "runtime", None) and getattr(plugin.runtime, "services", None)
        else {}
    )
    registered_servers = set((services_cfg.get("servers") or {}).keys())
    registered_apis = set((services_cfg.get("apis") or {}).keys())
    registered_templates = set((services_cfg.get("templates") or {}).keys())
    sandbox_ready = bool(
        getattr(plugin, "runtime", None) and plugin.runtime.sandbox.ready
    )

    loaded_plugin_ids = {
        str(p.get("id") or "")
        for p in (disc.get("plugins") or [])
        if isinstance(p, dict) and p.get("enabled", True) is not False
    }
    loaded_api_full = {
        f"{a.get('plugin_id')}.{a.get('name')}"
        for a in (disc.get("apis") or [])
        if isinstance(a, dict) and a.get("plugin_id") and a.get("name")
    }
    available_models = disc.get("models") if isinstance(disc.get("models"), dict) else {}

    items: list[dict] = []
    env = cfg.get("environment", "readonly")
    tools = tuple(cfg.get("tools") or sorted(READONLY_TOOLS))
    requires_container = env == "sandbox" and bool(
        cfg.get("secrets")
        or cfg.get("sandbox_targets")
        or cfg.get("network") == "restricted"
        or cfg.get("template")
        or "credential_call" in tools
        or not (set(tools) - {"sandbox_exec", "credential_call"})
    )

    if env == "sandbox":
        if sandbox_ready:
            items.append(
                {
                    "kind": "sandbox",
                    "name": "Docker / runsc 容器沙箱",
                    "status": "ready",
                    "detail": "本机隔离沙箱已就绪，可执行完整 sandbox_exec",
                }
            )
        elif requires_container:
            items.append(
                {
                    "kind": "sandbox",
                    "name": "Docker / runsc 容器沙箱",
                    "status": "missing",
                    "detail": "此配方使用容器专属隔离能力（凭据/受限网络/纯命令），需先启用容器沙箱",
                }
            )
        else:
            items.append(
                {
                    "kind": "sandbox",
                    "name": "轻量工作区自动降级",
                    "status": "fallback",
                    "detail": "本机未开容器，导入后自动以无容器轻量工作区运行（保留 workspace_file / html_render / result_attach）",
                }
            )

    task_slot = str(cfg.get("task") or "utils")
    if available_models:
        slot_ok = task_slot in available_models or task_slot in ALLOWED_TASKS
        items.append(
            {
                "kind": "model_slot",
                "name": f"模型槽位 {task_slot}",
                "status": "ready" if slot_ok else "missing",
                "detail": "宿主模型槽位可用" if slot_ok else f"宿主未发现槽位 {task_slot}",
            }
        )

    cred_notes = {
        str(c.get("handle")): str(c.get("purpose") or "")
        for c in (deps.get("credentials") or [])
        if isinstance(c, dict) and c.get("handle")
    }
    all_secret_handles = list(cfg.get("secrets") or [])
    for h in cred_notes:
        if h not in all_secret_handles:
            all_secret_handles.append(h)

    missing_binding = False
    for handle in all_secret_handles:
        present = handle in vault_handles
        if not present and handle in (cfg.get("secrets") or []):
            missing_binding = True
        purpose = cred_notes.get(handle) or "需在保险箱绑定同名凭据句柄"
        items.append(
            {
                "kind": "secret",
                "name": f"凭据句柄 {handle}",
                "status": "ready" if present else "missing",
                "detail": f"已在本机保险箱就绪（{purpose}）" if present else f"待绑定：{purpose}",
            }
        )

    for srv in cfg.get("mcp") or []:
        present = srv in registered_servers
        if not present:
            missing_binding = True
        items.append(
            {
                "kind": "mcp",
                "name": f"MCP 服务 {srv}",
                "status": "ready" if present else "missing",
                "detail": "本机已登记同名 MCP 服务" if present else "需在「设置 -> 能力服务」登记同名 MCP 服务",
            }
        )

    for api_alias in cfg.get("apis") or []:
        present = api_alias in registered_apis
        if not present:
            missing_binding = True
        items.append(
            {
                "kind": "api",
                "name": f"跨插件 API {api_alias}",
                "status": "ready" if present else "missing",
                "detail": "本机已登记同名 API 映射" if present else "需在「设置 -> 能力服务」登记同名 API",
            }
        )

    tpl_name = str(cfg.get("template") or "").strip()
    if tpl_name:
        present = tpl_name in registered_templates
        if not present:
            missing_binding = True
        items.append(
            {
                "kind": "template",
                "name": f"凭据模板 {tpl_name}",
                "status": "ready" if present else "missing",
                "detail": "本机已登记同名限定执行模板" if present else "需在「设置 -> 能力服务」登记同名模板",
            }
        )

    for p_item in deps.get("plugins") or []:
        if not isinstance(p_item, dict) or not p_item.get("id"):
            continue
        pid = str(p_item["id"])
        api_ref = str(p_item.get("api") or "")
        if not loaded_plugin_ids and not loaded_api_full:
            continue
        ok = pid in loaded_plugin_ids or (api_ref and api_ref in loaded_api_full)
        items.append(
            {
                "kind": "plugin",
                "name": f"宿主插件 {pid}",
                "status": "ready" if ok else "missing",
                "detail": p_item.get("purpose") or ("宿主已加载" if ok else "宿主当前未加载该插件"),
            }
        )

    for sk in parsed["skills"]:
        if not sk["bundled"] and not sk["exists_locally"]:
            items.append(
                {
                    "kind": "skill",
                    "name": f"技能 {sk['id']}",
                    "status": "missing",
                    "detail": "配方未随包附带且本机不存在该技能",
                }
            )

    missing_count = sum(1 for it in items if it["status"] == "missing")
    return {
        "items": items,
        "missing_count": missing_count,
        "missing_binding": missing_binding,
        "requires_rebind": missing_binding or bool(cfg.get("sandbox_targets")),
        "ready": missing_count == 0,
    }


def build_recipe_preview(
    plugin,
    raw_bytes: bytes,
    *,
    filename: str = "",
    subpath_hint: str = "",
    target_hint: str = "",
    discovery: dict | None = None,
    source_url: str = "",
) -> tuple[bytes, dict]:
    fallback = re.sub(
        r"\.(?:zip|tar\.gz|tgz|tar|json)$", "", Path(filename).name, flags=re.IGNORECASE
    )
    fallback = re.sub(r"-recipe$", "", fallback, flags=re.IGNORECASE)
    files, chosen_prefix = _extract_archive_files(
        raw_bytes, subpath_hint=subpath_hint, return_prefix=True
    )
    if chosen_prefix:
        leaf = chosen_prefix.rsplit("/", 1)[-1]
        inferred_id = (
            leaf
            if "/" in chosen_prefix
            else re.sub(r"-(?:main|master|HEAD)$", "", leaf)
        )
    else:
        inferred_id = fallback
    parsed = _parse_and_validate_recipe_files(plugin, files, fallback_id=inferred_id or fallback)
    clean_zip = _repack_clean_zip(files)
    alignment = compute_recipe_alignment(plugin, parsed, discovery=discovery)
    privacy_warnings = [
        f
        for f in scan_recipe_privacy(files, contains_secret=plugin.vault.contains_secret)
        if f["severity"] == "warning"
    ]

    candidate_id = str(target_hint or parsed["source_id"]).strip()
    existing_info = None
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", candidate_id):
        target_dir = plugin.profiles.path / candidate_id
        if target_dir.is_dir() and not target_dir.is_symlink():
            try:
                cur_prof = plugin.profiles.get(candidate_id)
                cur_doc = plugin.memory.read(
                    "profile",
                    target=candidate_id,
                    document="PROFILE.md",
                    allow_custom=True,
                )
                existing_info = {
                    "id": candidate_id,
                    "title": cur_prof.title,
                    "fish_seed": cur_prof.fish_seed,
                    "is_preset": candidate_id in _preset_profile_ids(),
                    "enabled": cur_prof.enabled,
                    "bound": cur_prof.bound,
                    "access": cur_prof.access,
                    "allow_chats_count": len(cur_prof.allow_chats),
                    "allow_users_count": len(cur_prof.allow_users),
                    "version": cur_doc["version"],
                }
            except (ValueError, OSError):
                existing_info = {"id": candidate_id, "exists": True}

    suggested_copy_id = candidate_id
    if existing_info is not None:
        for idx in range(2, 50):
            cand = f"{candidate_id[:56]}-{idx}"
            if not (plugin.profiles.path / cand).exists():
                suggested_copy_id = cand
                break

    raw_preview_fish = str(
        parsed["config"].get("fish_seed")
        or ((parsed.get("manifest") or {}).get("profile") or {}).get("fish_seed")
        or ""
    ).strip()
    preview_fish_seed = raw_preview_fish if FISH_SEED_RE.fullmatch(raw_preview_fish) else ""

    return clean_zip, {
        "source_id": parsed["source_id"],
        "suggested_target": candidate_id,
        "suggested_copy_id": suggested_copy_id,
        "existing_profile": existing_info,
        "source_url": source_url,
        "profile": {
            "id": parsed["source_id"],
            "title": str(parsed["config"].get("title") or parsed["source_id"]),
            "fish_seed": preview_fish_seed,
            "summary": parsed["summary"],
            "enabled": parsed["config"].get("enabled", True) is not False,
            "environment": parsed["config"].get("environment", "readonly"),
            "access": parsed["config"].get("access", "everyone"),
            "task": parsed["config"].get("task", "utils"),
            "tools": list(parsed["config"].get("tools") or sorted(READONLY_TOOLS)),
            "has_notes": parsed["has_notes"],
            "preset_files": parsed["preset_files"],
        },
        "skills": parsed["skills"],
        "routines": parsed["routines"],
        "alignment": alignment,
        "privacy_warnings": privacy_warnings,
        "enabled_chats": list(plugin.settings.enabled_chats),
    }


def _install_bundled_skills(
    plugin, bundled_skills: dict[str, dict], *, overwrite: bool = False
) -> list[str]:
    installed = []
    preset_set = plugin.memory.preset_skills()
    skills_root = plugin.data_root / "abilities/skills"
    if any(
        path_is_link_like(layer)
        for layer in relative_symlink_parts(skills_root, plugin.data_root)
    ):
        raise ValueError("目标技能根目录包含符号链接，拒绝更新")
    for sk_id, info in sorted(bundled_skills.items()):
        md_bytes = info.get("md") or b""
        if not md_bytes:
            continue
        dest_dir = skills_root / sk_id
        if any(
            path_is_link_like(layer)
            for layer in relative_symlink_parts(dest_dir, plugin.data_root)
        ):
            raise ValueError("目标技能目录包含符号链接，拒绝更新")
        skill_md_path = dest_dir / "SKILL.md"
        if path_is_link_like(skill_md_path):
            raise ValueError("目标技能文件包含符号链接，拒绝更新")
        exists = skill_md_path.is_file()
        if exists and (not overwrite or sk_id in preset_set):
            continue
        for sub_rel, comp_data in info.get("companions") or []:
            plugin.memory._guard_companion_bytes(sub_rel, comp_data)
        private_directory(dest_dir, stop_at=plugin.data_root)
        if exists:
            plugin.memory._clear_skill_companions(dest_dir)
        cur = plugin.memory.read("skill", target=sk_id, allow_custom=True)
        md_text = md_bytes.decode("utf-8", errors="replace")
        if cur["content"] != md_text or not exists:
            plugin.memory.write(
                "skill",
                md_text,
                target=sk_id,
                expected_version=cur["version"],
                author="recipe_import",
                allow_custom=True,
            )
        for sub_rel, comp_data in info.get("companions") or []:
            cur_p = dest_dir
            for part in Path(sub_rel).parts[:-1]:
                cur_p = cur_p / part
                if path_is_link_like(cur_p):
                    raise ValueError("技能附属文件目录包含符号链接，拒绝更新")
                private_directory(cur_p, stop_at=dest_dir)
            comp_file = dest_dir / sub_rel
            if path_is_link_like(comp_file):
                raise ValueError("技能附属文件包含符号链接，拒绝更新")
            atomic_private_write(comp_file, comp_data)
        installed.append(sk_id)
    return installed


def _resolve_routine_chat(
    plugin, chat_id: str, platform: str = ""
) -> tuple[str, str, set[str]]:
    """解析例行任务的目标会话规范流、真实平台以及全部别名。"""
    from .trust import parse_stream_spec

    target = str(chat_id or "").strip()
    hint_plat = str(platform or "").strip()
    if not target:
        return "", hint_plat, set()

    parsed = parse_stream_spec(target)
    resolved_plat = parsed[0] if parsed else ""
    messages = getattr(plugin, "messages", None)

    known_sid = ""
    if hasattr(plugin, "resolve_known_session_id"):
        known_sid = str(plugin.resolve_known_session_id(target) or "").strip()

    if not resolved_plat and messages is not None:
        for probe in (target, known_sid):
            if not probe:
                continue
            route = messages.route_for(probe) if hasattr(messages, "route_for") else None
            if isinstance(route, dict) and str(route.get("platform") or "").strip():
                resolved_plat = str(route["platform"]).strip()
                break
            plat_from_msg = (
                str(messages.platform_for(probe) or "").strip()
                if hasattr(messages, "platform_for")
                else ""
            )
            if plat_from_msg:
                resolved_plat = plat_from_msg
                break

    if not resolved_plat:
        enabled_chats = tuple(
            getattr(getattr(plugin, "settings", None), "enabled_chats", ()) or ()
        )
        matched_plats: set[str] = set()
        hint_matched = False
        for entry in enabled_chats:
            ep = parse_stream_spec(entry)
            if not ep:
                continue
            if ep[2] == target or entry == target:
                matched_plats.add(ep[0])
                if hint_plat and ep[0] == hint_plat:
                    hint_matched = True
        if hint_matched:
            resolved_plat = hint_plat
        elif len(matched_plats) == 1 and (not hint_plat or hint_plat == "qq"):
            resolved_plat = next(iter(matched_plats))

    effective_plat = resolved_plat or hint_plat
    canonical_chat = target
    if parsed is not None:
        canonical_chat = target
    elif hasattr(plugin, "canonical_stream_spec"):
        cand = str(plugin.canonical_stream_spec(target, effective_plat) or "").strip()
        if cand:
            canonical_chat = cand
            cand_parsed = parse_stream_spec(cand)
            if cand_parsed is not None:
                effective_plat = cand_parsed[0]

    aliases: set[str] = {target, canonical_chat}
    if known_sid:
        aliases.add(known_sid)
    for probe in (target, canonical_chat, known_sid):
        if not probe:
            continue
        if hasattr(plugin, "resolve_known_session_id"):
            known = str(plugin.resolve_known_session_id(probe) or "").strip()
            if known:
                aliases.add(known)
        if hasattr(plugin, "chat_aliases_for"):
            for plat_probe in dict.fromkeys((effective_plat, hint_plat, "")):
                aliases.update(
                    str(a).strip()
                    for a in (plugin.chat_aliases_for(probe, plat_probe) or ())
                    if str(a).strip()
                )
    return canonical_chat, effective_plat, aliases


def _is_routine_chat_enabled(plugin, chat_id: str, platform: str = "") -> bool:
    canonical_chat, effective_plat, aliases = _resolve_routine_chat(
        plugin, chat_id, platform
    )
    if not canonical_chat:
        return False
    enabled_chats = set(
        getattr(getattr(plugin, "settings", None), "enabled_chats", ()) or ()
    )
    plat_candidates = [effective_plat] if effective_plat else ["qq", ""]
    for probe in sorted(aliases):
        if probe in enabled_chats:
            return True
        if hasattr(plugin, "enabled_for"):
            for plat_cand in plat_candidates:
                if plugin.enabled_for(probe, plat_cand):
                    return True
    return False


def _prevalidate_routine_mount(
    plugin,
    *,
    routines: list[dict],
    selected_ids: list[str],
    chat_id: str,
    platform: str = "",
    final_enabled: bool,
    final_bound: bool,
) -> None:
    wanted = set(selected_ids or [])
    if not wanted or not chat_id:
        return
    if not any(rt["id"] in wanted or "*" in wanted for rt in routines):
        return
    if plugin.intents is None:
        raise ValueError("意图调度器尚未就绪，无法挂载例行任务")
    if not _is_routine_chat_enabled(plugin, chat_id, platform):
        raise ValueError("挂载例行任务的目标会话尚未在 enabled_chats 中启用")
    if not final_enabled or not final_bound:
        raise ValueError(
            "角色当前处于停用或待绑定凭据状态，请先完成本机绑定并启用后再挂载例行任务"
        )


def _mount_selected_routines(
    plugin,
    *,
    profile_id: str,
    routines: list[dict],
    selected_ids: list[str],
    chat_id: str,
    platform: str = "",
) -> list[dict]:
    if not selected_ids or not chat_id or plugin.intents is None:
        return []
    if not _is_routine_chat_enabled(plugin, chat_id, platform):
        raise ValueError("挂载例行任务的目标会话尚未在 enabled_chats 中启用")
    profile = plugin.profiles.get(profile_id)
    if not profile.enabled or not profile.bound:
        raise ValueError("角色当前处于停用或待绑定凭据状态，请先完成本机绑定并启用后再挂载例行任务")

    canonical_chat, effective_plat, aliases = _resolve_routine_chat(
        plugin, chat_id, platform
    )
    wanted = set(selected_ids)
    existing_rows: list[dict] = []
    for alias in sorted(aliases):
        existing_rows.extend(plugin.intents.list(f"chat:{alias}"))
    existing_keys = set()
    for r in existing_rows:
        if r.get("status") not in {"pending", "deferred"}:
            continue
        p = r.get("payload") or {}
        existing_keys.add((p.get("profile"), p.get("prompt"), int(p.get("every_seconds") or 0)))

    scheduled = []
    for rt in routines:
        if rt["id"] not in wanted and "*" not in wanted:
            continue
        dedup = (profile_id, rt["prompt"], int(rt["every_seconds"] or 0))
        if dedup in existing_keys:
            continue
        payload = {
            "platform": effective_plat,
            "profile": profile_id,
            "workspace": rt.get("workspace") or "",
            "prompt": rt["prompt"],
            "every_seconds": int(rt.get("every_seconds") or 0),
            "scheduled_admin": True,
        }
        row = plugin.intents.schedule(
            kind=rt.get("kind") or "return",
            source="workflow",
            target=f"chat:{canonical_chat}",
            due_at=plugin.intents.clock() + int(rt.get("in_seconds") or 60),
            reason=rt["title"],
            payload=payload,
            bypass_limits=True,
        )
        existing_keys.add(dedup)
        scheduled.append({"intent_id": row["intent_id"], "title": rt["title"]})
    return scheduled


def import_recipe_archive(
    plugin,
    raw_bytes: bytes,
    *,
    target: str,
    mode: str = "copy",
    install_skills: bool = True,
    overwrite_skills: bool | None = None,
    mount_routines: list[str] | None = None,
    routine_chat_id: str = "",
    routine_platform: str = "",
) -> dict:
    if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", target):
        raise ValueError("目标角色编号无效（仅允许字母开头，含字母、数字、下划线或连字符）")
    if mode not in {"copy", "update"}:
        raise ValueError("导入模式仅支持 copy（另存副本）或 update（平滑升级）")
    effective_overwrite_skills = (
        (mode == "update") if overwrite_skills is None else bool(overwrite_skills)
    )

    files = _extract_archive_files(raw_bytes)
    parsed = _parse_and_validate_recipe_files(plugin, files, fallback_id=target)
    alignment = compute_recipe_alignment(plugin, parsed)

    target_dir = plugin.profiles.path / target
    exists = target_dir.exists() or target_dir.is_symlink()
    if mode == "copy" and exists:
        raise ValueError(f"角色 {target} 已存在；请选择「更新能力配方」或填写新的副本编号")
    if mode == "update" and not exists:
        raise ValueError(f"本地不存在角色 {target}，无法执行平滑升级")

    incoming_cfg = dict(parsed["config"])
    requires_admin = bool(
        incoming_cfg.get("secrets")
        or incoming_cfg.get("template")
        or incoming_cfg.get("sandbox_targets")
        or incoming_cfg.get("network") == "restricted"
        or "credential_call" in (incoming_cfg.get("tools") or [])
    )
    if requires_admin:
        incoming_cfg["access"] = "admins"

    if mode == "copy":
        copy_cfg = dict(incoming_cfg)
        copy_cfg.pop("allow_chats", None)
        copy_cfg.pop("allow_users", None)
        if alignment["requires_rebind"]:
            copy_cfg["bound"] = False
        else:
            copy_cfg["bound"] = copy_cfg.get("bound", True) is not False

        _prevalidate_routine_mount(
            plugin,
            routines=parsed["routines"],
            selected_ids=list(mount_routines or []),
            chat_id=str(routine_chat_id or "").strip(),
            platform=routine_platform,
            final_enabled=copy_cfg.get("enabled", True) is not False,
            final_bound=bool(copy_cfg["bound"]),
        )

        stage = Path(tempfile.mkdtemp(prefix="recipe-", dir=plugin.profiles.path))
        try:
            for rel, data in files.items():
                if rel in {"recipe.json", "RECIPE.md", "profile.toml"}:
                    continue
                dest = stage / rel
                private_directory(dest.parent)
                atomic_private_write(dest, data)
            atomic_private_write(
                stage / "profile.toml",
                serialize_profile_toml(copy_cfg).encode("utf-8"),
            )
            stage.rename(target_dir)
            try:
                created_profile = plugin.profiles.get(target)
                seeded_versions = plugin.memory.seed_profile(target)
            except BaseException:
                shutil.rmtree(target_dir, ignore_errors=True)
                plugin.memory.purge_profile(target)
                raise
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

        installed_skills = (
            _install_bundled_skills(
                plugin, parsed["bundled_skills"], overwrite=effective_overwrite_skills
            )
            if install_skills
            else []
        )
        mounted = _mount_selected_routines(
            plugin,
            profile_id=target,
            routines=parsed["routines"],
            selected_ids=list(mount_routines or []),
            chat_id=str(routine_chat_id or "").strip(),
            platform=routine_platform,
        )
        return {
            "id": created_profile.id,
            "mode": "copy",
            "bound": created_profile.bound,
            "enabled": created_profile.enabled,
            "installed_skills": installed_skills,
            "mounted_routines": mounted,
            "versions": seeded_versions,
            "alignment": alignment,
        }

    # Smooth upgrade (`mode == "update"`): preserve local allowlists, enabled, workspaces, and valid bindings!
    local_profile = plugin.profiles.get(target)
    merged_cfg = dict(incoming_cfg)
    if not str(merged_cfg.get("fish_seed") or "").strip() and local_profile.fish_seed:
        merged_cfg["fish_seed"] = local_profile.fish_seed
    merged_cfg["enabled"] = local_profile.enabled
    if local_profile.allow_chats:
        merged_cfg["allow_chats"] = list(local_profile.allow_chats)
    else:
        merged_cfg.pop("allow_chats", None)
    if local_profile.allow_users:
        merged_cfg["allow_users"] = list(local_profile.allow_users)
    else:
        merged_cfg.pop("allow_users", None)

    if requires_admin:
        merged_cfg["access"] = "admins"
    elif local_profile.access == "listed" and (local_profile.allow_chats or local_profile.allow_users):
        merged_cfg["access"] = "listed"
    else:
        merged_cfg["access"] = incoming_cfg.get("access", local_profile.access)

    incoming_targets = set(incoming_cfg.get("sandbox_targets") or ())
    local_targets = set(local_profile.sandbox_targets or ())
    update_needs_rebind = bool(
        alignment.get("missing_binding")
        or not incoming_targets.issubset(local_targets)
    )
    if update_needs_rebind:
        merged_cfg["bound"] = False
    else:
        merged_cfg["bound"] = local_profile.bound

    _prevalidate_routine_mount(
        plugin,
        routines=parsed["routines"],
        selected_ids=list(mount_routines or []),
        chat_id=str(routine_chat_id or "").strip(),
        platform=routine_platform,
        final_enabled=bool(merged_cfg["enabled"]),
        final_bound=bool(merged_cfg["bound"]),
    )

    # Update preset starter files atomically without touching runtime workspaces
    for rel, data in files.items():
        if rel.startswith("preset/"):
            dest = target_dir / rel
            if any(
                path_is_link_like(layer)
                for layer in relative_symlink_parts(dest.parent, target_dir)
            ):
                raise ValueError("角色预设目录包含符号链接，拒绝更新")
            cur_p = target_dir
            for part in Path(rel).parts[:-1]:
                cur_p = cur_p / part
                if path_is_link_like(cur_p):
                    raise ValueError("角色预设目录包含符号链接，拒绝更新")
                private_directory(cur_p, stop_at=target_dir)
            if path_is_link_like(dest):
                raise ValueError("角色预设文件包含符号链接，拒绝更新")
            atomic_private_write(dest, data)

    cur_toml = plugin.memory.read(
        "profile", target=target, document="profile.toml", allow_custom=True
    )
    next_toml_text = serialize_profile_toml(merged_cfg)
    if cur_toml["content"] != next_toml_text:
        saved_toml = plugin.memory.write(
            "profile",
            next_toml_text,
            target=target,
            document="profile.toml",
            expected_version=cur_toml["version"],
            author="recipe_update",
            allow_custom=True,
        )
        toml_ver = saved_toml["version"]
    else:
        toml_ver = cur_toml["version"]

    cur_md = plugin.memory.read(
        "profile", target=target, document="PROFILE.md", allow_custom=True
    )
    if cur_md["content"] != parsed["instructions"]:
        saved_md = plugin.memory.write(
            "profile",
            parsed["instructions"],
            target=target,
            document="PROFILE.md",
            expected_version=cur_md["version"],
            author="recipe_update",
            allow_custom=True,
        )
        md_ver = saved_md["version"]
    else:
        md_ver = cur_md["version"]

    notes_ver = None
    if "NOTES.md" in files:
        incoming_notes = files["NOTES.md"].decode("utf-8", errors="replace")
        cur_notes = plugin.memory.read("notes", target=target, allow_custom=True)
        if cur_notes["content"] != incoming_notes:
            saved_notes = plugin.memory.write(
                "notes",
                incoming_notes,
                target=target,
                expected_version=cur_notes["version"],
                author="recipe_update",
                allow_custom=True,
            )
            notes_ver = saved_notes["version"]
        else:
            notes_ver = cur_notes["version"]

    updated_profile = plugin.profiles.get(target)
    installed_skills = (
        _install_bundled_skills(
            plugin, parsed["bundled_skills"], overwrite=effective_overwrite_skills
        )
        if install_skills
        else []
    )
    mounted = _mount_selected_routines(
        plugin,
        profile_id=target,
        routines=parsed["routines"],
        selected_ids=list(mount_routines or []),
        chat_id=str(routine_chat_id or "").strip(),
        platform=routine_platform,
    )
    return {
        "id": updated_profile.id,
        "mode": "update",
        "bound": updated_profile.bound,
        "enabled": updated_profile.enabled,
        "preserved": {
            "allow_chats": list(updated_profile.allow_chats),
            "allow_users": list(updated_profile.allow_users),
            "enabled": updated_profile.enabled,
            "bound": updated_profile.bound,
            "fish_seed": updated_profile.fish_seed,
        },
        "installed_skills": installed_skills,
        "mounted_routines": mounted,
        "versions": {
            "PROFILE.md": md_ver,
            "profile.toml": toml_ver,
            **({"NOTES.md": notes_ver} if notes_ver is not None else {}),
        },
        "alignment": alignment,
    }
