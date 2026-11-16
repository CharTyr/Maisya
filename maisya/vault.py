"""只进不出的凭据保险箱与出站脱敏。"""

import base64
from collections import Counter
from collections.abc import Iterable
import errno
import hashlib
import json
import math
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, quote_from_bytes

from cryptography.fernet import Fernet, InvalidToken

from .pathsafe import (
    acquire_lock_handle,
    create_exclusive,
    path_is_link_like,
    release_lock_handle,
    replace_atomic,
    safe_chmod,
)


class VaultError(RuntimeError):
    pass


_HEX_LOWER_RE = re.compile(r"[0-9a-f]{16,}")


def _is_high_entropy_secret(raw: bytes) -> bool:
    """仅高熵且足够长的秘密才生成全局脱敏变体，避免短词与常见词误伤正常文本。"""
    if len(raw) < 8:
        return False
    counts = Counter(raw)
    if len(counts) < 4:
        return False
    total = len(raw)
    entropy = -sum((count / total) * math.log2(count / total) for count in counts.values())
    return entropy >= 2.0


def _single_secret_variants(raw: bytes, text: str | None) -> set[str]:
    if not _is_high_entropy_secret(raw):
        return set()
    std_b64 = base64.b64encode(raw).decode("ascii")
    url_b64 = base64.urlsafe_b64encode(raw).decode("ascii")
    hex_lower = raw.hex()
    variants: set[str] = {
        std_b64,
        std_b64.rstrip("="),
        url_b64,
        url_b64.rstrip("="),
        hex_lower,
        hex_lower.upper(),
        quote_from_bytes(raw, safe=""),
    }
    if text is not None:
        variants.update(
            {
                text,
                json.dumps(text, ensure_ascii=False)[1:-1],
                json.dumps(text, ensure_ascii=True)[1:-1],
                quote(text, safe=""),
            }
        )
    else:
        variants.add(repr(raw)[2:-1])
    return {item for item in variants if item}


def secret_variants(value: str | bytes | bytearray) -> set[str]:
    if not value:
        return set()
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        try:
            text: str | None = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        result = _single_secret_variants(raw, text)
        if text is not None and "\n" in text:
            for line in text.splitlines():
                stripped = line.strip()
                if stripped:
                    result.update(
                        _single_secret_variants(stripped.encode("utf-8"), stripped)
                    )
        elif b"\n" in raw:
            for line_bytes in raw.splitlines():
                stripped_bytes = line_bytes.strip()
                if stripped_bytes:
                    result.update(_single_secret_variants(stripped_bytes, None))
        return result
    if isinstance(value, str):
        raw = value.encode("utf-8")
        result = _single_secret_variants(raw, value)
        if "\n" in value:
            for line in value.splitlines():
                stripped = line.strip()
                if stripped:
                    result.update(
                        _single_secret_variants(stripped.encode("utf-8"), stripped)
                    )
        return result
    return set()


def _extract_redact_targets(secrets) -> tuple[list[str], list[bytes]]:
    str_set: set[str] = set()
    byte_set: set[bytes] = set()
    for item in secrets or ():
        if isinstance(item, (bytes, bytearray)):
            if item:
                byte_set.add(bytes(item))
        elif isinstance(item, str) and item:
            str_set.add(item)
            byte_set.add(item.encode("utf-8"))
    for s in str_set:
        if len(s) >= 16 and len(s) % 2 == 0 and _HEX_LOWER_RE.fullmatch(s):
            cand = bytes.fromhex(s)
            if (
                base64.b64encode(cand).decode("ascii") in str_set
                or s.upper() in str_set
            ):
                byte_set.add(cand)
    return (
        sorted(str_set, key=len, reverse=True),
        sorted(byte_set, key=len, reverse=True),
    )


def _redact_with_targets(value, str_targets: list[str], byte_targets: list[bytes]):
    if isinstance(value, str):
        for secret in str_targets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, bytes):
        for secret in byte_targets:
            value = value.replace(secret, b"[REDACTED]")
        return value
    if isinstance(value, bytearray):
        raw = bytes(value)
        for secret in byte_targets:
            raw = raw.replace(secret, b"[REDACTED]")
        return bytearray(raw)
    if isinstance(value, list):
        return [_redact_with_targets(item, str_targets, byte_targets) for item in value]
    if isinstance(value, tuple):
        return tuple(
            _redact_with_targets(item, str_targets, byte_targets) for item in value
        )
    if isinstance(value, dict):
        return {
            _redact_with_targets(key, str_targets, byte_targets): _redact_with_targets(
                item, str_targets, byte_targets
            )
            for key, item in value.items()
        }
    return value


def redact_values(value, secrets):
    if not secrets:
        return value
    str_targets, byte_targets = _extract_redact_targets(secrets)
    if not str_targets and not byte_targets:
        return value
    return _redact_with_targets(value, str_targets, byte_targets)


def relative_symlink_parts(path: Path, stop_at: Path) -> tuple[Path, ...]:
    """返回 stop_at 之下的逐层路径，避免受宿主机祖先目录软链影响。"""
    target = Path(path)
    anchor = Path(stop_at)
    try:
        rel = target.relative_to(anchor)
    except ValueError:
        lex_target = Path(os.path.abspath(os.fspath(target)))
        lex_anchor = Path(os.path.abspath(os.fspath(anchor)))
        try:
            rel = lex_target.relative_to(lex_anchor)
            anchor = lex_anchor
        except ValueError:
            raise VaultError("保险箱路径不能经过符号链接") from None
    layers: list[Path] = []
    cur = anchor
    for part in rel.parts:
        cur = cur / part
        layers.append(cur)
    return tuple(layers)


def private_directory(path: Path, *, stop_at: Path | None = None) -> Path:
    target = Path(path)
    if stop_at is not None:
        if any(path_is_link_like(layer) for layer in relative_symlink_parts(target, stop_at)):
            raise VaultError("保险箱路径不能经过符号链接")
    elif path_is_link_like(target):
        raise VaultError("保险箱路径不能经过符号链接")
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    safe_chmod(target, 0o700)
    return target


def atomic_private_write(path: Path, content: bytes) -> None:
    if path_is_link_like(path):
        raise VaultError("保险箱文件不能是符号链接")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        safe_chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


_META_FILE = "vault_meta.json"
_BLOCKED_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "PYTHONPATH",
        "PYTHONHOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SHELL",
        "USER",
        "IFS",
        "ENV",
        "BASH_ENV",
    }
)
_BLOCKED_ENV_PREFIXES = ("LD_", "DYLD_")


class Vault:
    def __init__(self, path: Path):
        try:
            self.path = private_directory(path)
        except VaultError:
            raise
        except OSError:
            raise VaultError("保险箱目录不可用") from None
        self._redactions: set[str] = set()
        self._byte_redactions: set[bytes] = set()
        key_path = self.path / "master.key"
        lock_path = self.path / ".master.lock"
        if path_is_link_like(lock_path):
            raise VaultError("保险箱锁文件不能是符号链接")
        try:
            lock_handle = acquire_lock_handle(lock_path)
        except OSError:
            raise VaultError("保险箱初始化加锁失败") from None
        try:
            safe_chmod(lock_path, 0o600)
            if path_is_link_like(key_path):
                raise VaultError("保险箱密钥不能是符号链接")
            has_enc = any(self.path.glob("*.enc"))
            need_create = not key_path.exists()
            if not need_create:
                safe_chmod(key_path, 0o600)
                if key_path.stat().st_size == 0:
                    if has_enc:
                        raise VaultError("保险箱主密钥为空或已损坏，已停止读取")
                    key_path.unlink(missing_ok=True)
                    need_create = True
            if need_create:
                if has_enc:
                    raise VaultError("保险箱密钥缺失，已停止读取")
                key_bytes = Fernet.generate_key()
                temp_path = self.path / f".master.{secrets.token_hex(8)}.tmp"
                descriptor = create_exclusive(temp_path, 0o600)
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        descriptor = -1
                        output.write(key_bytes)
                        output.flush()
                        os.fsync(output.fileno())
                    try:
                        os.link(temp_path, key_path)
                    except FileExistsError:
                        pass
                    except OSError as error:
                        if error.errno not in {
                            errno.EPERM,
                            errno.ENOTSUP,
                            errno.EXDEV,
                            errno.EOPNOTSUPP,
                        }:
                            raise
                        if not key_path.exists():
                            replace_atomic(
                                self.path, "master.key", key_bytes, mode=0o600
                            )
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
            if path_is_link_like(key_path):
                raise VaultError("保险箱密钥不能是符号链接")
            safe_chmod(key_path, 0o600)
            key_raw = key_path.read_bytes()
        except VaultError:
            raise
        except OSError:
            raise VaultError("保险箱密钥初始化失败") from None
        finally:
            release_lock_handle(lock_handle)

        try:
            self._cipher = Fernet(key_raw)
            for encrypted in self.path.glob("*.enc"):
                if path_is_link_like(encrypted):
                    raise VaultError("保险箱文件不能是符号链接")
                safe_chmod(encrypted, 0o600)
                record = json.loads(self._cipher.decrypt(encrypted.read_bytes()))
                if record["handle"] != encrypted.stem or not isinstance(
                    record["value"], str
                ):
                    raise ValueError
                self.protect(record["value"])
        except VaultError:
            raise
        except (InvalidToken, ValueError, KeyError, TypeError, OSError):
            raise VaultError("保险箱内容校验失败，已停止读取") from None

    @staticmethod
    def _validate_handle(handle: str) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", handle):
            raise ValueError("凭据句柄只能包含字母、数字、下划线和短横线")

    @property
    def meta_path(self) -> Path:
        return self.path / _META_FILE

    def _load_meta(self) -> dict[str, dict]:
        meta_path = self.meta_path
        if path_is_link_like(meta_path):
            raise VaultError("凭据目录文件不能是符号链接")
        if not meta_path.exists():
            return {}
        safe_chmod(meta_path, 0o600)
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise VaultError("凭据目录文件损坏或不可读") from None
        if not isinstance(data, dict):
            raise VaultError("凭据目录结构无效")
        cleaned: dict[str, dict] = {}
        for key, item in data.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                continue
            fields_raw = item.get("fields") or []
            fields = (
                [str(f).strip() for f in fields_raw if str(f).strip()]
                if isinstance(fields_raw, (list, tuple))
                else []
            )
            cleaned[key] = {
                "handle": key,
                "label": str(item.get("label") or key).strip(),
                "domain": str(item.get("domain") or "").strip(),
                "fields": fields,
                "kind": str(item.get("kind") or "secret").strip(),
                "note": str(item.get("note") or "").strip(),
                "updated_at": float(item.get("updated_at") or 0.0),
            }
        return cleaned

    def _save_meta(self, meta: dict[str, dict]) -> None:
        if path_is_link_like(self.meta_path):
            raise VaultError("凭据目录文件不能是符号链接")
        payload = json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        if self.contains_secret(payload):
            raise VaultError("凭据元数据不能包含明文秘密")
        try:
            atomic_private_write(self.meta_path, payload)
        except OSError:
            raise VaultError("凭据目录写入失败") from None

    def _check_meta_fields_safe(self, values: Iterable[str]) -> None:
        for item in values:
            if item and self.contains_secret(item.encode("utf-8")):
                raise VaultError("凭据元数据不能包含明文秘密")

    def set(
        self,
        handle: str,
        value: str,
        *,
        label: str | None = None,
        domain: str | None = None,
        fields: Iterable[str] | None = None,
        kind: str | None = None,
        note: str | None = None,
    ) -> None:
        self._validate_handle(handle)
        if not isinstance(value, str) or not value or len(value.encode()) > 65536:
            raise ValueError("凭据必须为非空文本，且不超过 64 KiB")
        meta = self._load_meta()
        existing = meta.get(handle, {})
        resolved_label = existing.get("label") or handle if label is None else str(label).strip() or handle
        resolved_domain = existing.get("domain", "") if domain is None else str(domain).strip()
        resolved_fields = (
            list(existing.get("fields") or [])
            if fields is None
            else [str(f).strip() for f in fields if str(f).strip()]
        )
        resolved_kind = existing.get("kind") or "secret" if kind is None else str(kind).strip() or "secret"
        resolved_note = existing.get("note", "") if note is None else str(note).strip()
        meta_fields = [
            resolved_label,
            resolved_domain,
            resolved_kind,
            resolved_note,
            *resolved_fields,
        ]
        self._check_meta_fields_safe(meta_fields)
        for item in meta_fields:
            if item and (value in item or any(v in item for v in secret_variants(value))):
                raise VaultError("凭据元数据不能包含明文秘密")
        try:
            token = self._cipher.encrypt(
                json.dumps({"handle": handle, "value": value}).encode()
            )
            atomic_private_write(self.path / f"{handle}.enc", token)
        except VaultError:
            raise
        except OSError:
            raise VaultError(f"凭据 {handle} 写入失败") from None
        self.protect(value)
        meta[handle] = {
            "handle": handle,
            "label": resolved_label,
            "domain": resolved_domain,
            "fields": resolved_fields,
            "kind": resolved_kind,
            "note": resolved_note,
            "updated_at": round(time.time(), 6),
        }
        self._save_meta(meta)

    def set_meta(
        self,
        handle: str,
        *,
        label: str | None = None,
        domain: str | None = None,
        fields: Iterable[str] | None = None,
        kind: str | None = None,
        note: str | None = None,
    ) -> dict:
        self._validate_handle(handle)
        meta = self._load_meta()
        existing = meta.get(handle, {})
        resolved_label = (
            existing.get("label") or handle if label is None else str(label).strip() or handle
        )
        resolved_domain = (
            existing.get("domain", "") if domain is None else str(domain).strip()
        )
        resolved_fields = (
            list(existing.get("fields") or [])
            if fields is None
            else [str(f).strip() for f in fields if str(f).strip()]
        )
        resolved_kind = (
            existing.get("kind") or "secret" if kind is None else str(kind).strip() or "secret"
        )
        resolved_note = existing.get("note", "") if note is None else str(note).strip()
        self._check_meta_fields_safe(
            [resolved_label, resolved_domain, resolved_kind, resolved_note, *resolved_fields]
        )
        record = {
            "handle": handle,
            "label": resolved_label,
            "domain": resolved_domain,
            "fields": resolved_fields,
            "kind": resolved_kind,
            "note": resolved_note,
            "updated_at": round(time.time(), 6),
        }
        meta[handle] = record
        self._save_meta(meta)
        return dict(record)

    def get_meta(self, handle: str) -> dict | None:
        self._validate_handle(handle)
        meta = self._load_meta()
        if handle in meta:
            return dict(meta[handle])
        enc = self.path / f"{handle}.enc"
        if enc.exists():
            return {
                "handle": handle,
                "label": handle,
                "domain": "",
                "fields": [],
                "kind": "secret",
                "note": "",
                "updated_at": round(enc.stat().st_mtime, 6),
            }
        return None

    def list_meta(self, *, include_internal: bool = False) -> list[dict]:
        meta = self._load_meta()
        live_handles = set(self.handles(include_internal=True))
        result: list[dict] = []
        for handle in sorted(live_handles):
            entry = meta.get(handle)
            if entry is None:
                enc = self.path / f"{handle}.enc"
                entry = {
                    "handle": handle,
                    "label": handle,
                    "domain": "",
                    "fields": [],
                    "kind": "secret",
                    "note": "",
                    "updated_at": round(enc.stat().st_mtime, 6) if enc.exists() else 0.0,
                }
            if not include_internal and entry.get("kind") == "mcp_inline":
                continue
            result.append(dict(entry))
        return result

    def delete_meta(self, handle: str) -> bool:
        self._validate_handle(handle)
        if not self.meta_path.exists():
            return False
        meta = self._load_meta()
        if handle not in meta:
            return False
        meta.pop(handle)
        self._save_meta(meta)
        return True

    def delete(self, handle: str) -> None:
        self._validate_handle(handle)
        (self.path / f"{handle}.enc").unlink(missing_ok=True)
        self.delete_meta(handle)

    def handles(self, *, include_internal: bool = False) -> list[str]:
        all_handles = sorted(path.stem for path in self.path.glob("*.enc"))
        if include_internal:
            return all_handles
        try:
            meta = self._load_meta()
        except VaultError:
            meta = {}
        return [
            h
            for h in all_handles
            if (meta.get(h) or {}).get("kind") != "mcp_inline"
        ]

    @staticmethod
    def _mcp_secret_handle(service: str, key: str, slot: str = "env") -> str:
        svc_str = str(service or "")
        slot_str = str(slot or "env")
        key_str = str(key or "")
        safe_svc = re.sub(r"[^A-Za-z0-9_-]+", "_", svc_str).strip("_-")[:20] or "svc"
        svc_hash = hashlib.sha256(svc_str.encode("utf-8")).hexdigest()[:8]
        digest = hashlib.sha256(
            f"{len(svc_str)}:{svc_str}|{len(slot_str)}:{slot_str}|{len(key_str)}:{key_str}".encode("utf-8")
        ).hexdigest()[:12]
        return f"mcp_in_{safe_svc}_{svc_hash}_{digest}"

    def set_mcp_secret(self, service: str, key: str, value: str, *, slot: str = "env") -> str:
        """加密保存直接在 MCP 配置中填写的密钥（归类为内部句柄，不混入用户密码保险箱列表）。"""
        svc_str = str(service or "")
        handle = self._mcp_secret_handle(svc_str, key, slot=slot)
        self.set(
            handle,
            value,
            label=f"{svc_str}.{key}",
            domain=f"mcp:{svc_str}",
            kind="mcp_inline",
            note=f"mcp:{svc_str}:{slot}",
        )
        if slot == "header" and isinstance(value, str):
            parts = value.strip().split(None, 1)
            if len(parts) == 2 and parts[0].lower() in {"bearer", "token", "basic", "apikey"} and len(parts[1].strip()) >= 6:
                self.protect(parts[1].strip())
        return handle

    def purge_mcp_secrets(self, service: str, *, keep_handles: Iterable[str] = ()) -> int:
        keep = set(keep_handles or ())
        svc_str = str(service or "")
        safe_svc = re.sub(r"[^A-Za-z0-9_-]+", "_", svc_str).strip("_-")[:20] or "svc"
        svc_hash = hashlib.sha256(svc_str.encode("utf-8")).hexdigest()[:8]
        exact_prefix = f"mcp_in_{safe_svc}_{svc_hash}_"
        try:
            meta = self._load_meta()
        except VaultError:
            meta = {}
        removed = 0
        for h in self.handles(include_internal=True):
            if h in keep:
                continue
            info = meta.get(h) or {}
            if info.get("kind") == "mcp_inline" and (
                h.startswith(exact_prefix)
                or info.get("domain") == f"mcp:{svc_str}"
                or info.get("note") in {f"mcp:{svc_str}:env", f"mcp:{svc_str}:header"}
            ):
                self.delete(h)
                removed += 1
        return removed

    def for_execution(self, handle):
        self._validate_handle(handle)
        path = self.path / (handle + ".enc")
        if path_is_link_like(path):
            raise VaultError(f"凭据 {handle} 文件不能是符号链接")
        try:
            record = json.loads(self._cipher.decrypt(path.read_bytes()))
            if record["handle"] != handle or not isinstance(record["value"], str):
                raise ValueError("凭据结构无效")
            self.protect(record["value"])
            return record["value"]
        except FileNotFoundError:
            raise VaultError(f"凭据 {handle} 不存在或未配置") from None
        except (InvalidToken, ValueError, KeyError, TypeError, OSError):
            raise VaultError(f"凭据 {handle} 读取或校验失败") from None

    def inject_env_secrets(
        self,
        handles: Iterable[str],
        *,
        env: dict[str, str] | None = None,
        prefix: str = "MAISYA_SECRET_",
    ) -> dict[str, str]:
        merged = dict(env or {})
        for handle in handles:
            secret = self.for_execution(handle)
            normalized = re.sub(r"[^A-Za-z0-9_]", "_", handle).upper()
            env_key = f"{prefix}{normalized}"
            if env_key in _BLOCKED_ENV_NAMES or any(
                env_key.startswith(pre) for pre in _BLOCKED_ENV_PREFIXES
            ):
                raise VaultError(f"凭据环境变量名受保护：{env_key}")
            merged[env_key] = secret
        return merged

    def protect(self, value: str | bytes | bytearray) -> None:
        if isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
            if _is_high_entropy_secret(raw):
                self._byte_redactions.add(raw)
        self._redactions.update(secret_variants(value))

    def redact(self, value):
        return redact_values(value, self._redactions | self._byte_redactions)

    def contains_secret(self, content):
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("凭据检查需要字节内容")
        blob = bytes(content)
        if any(secret in blob for secret in self._byte_redactions):
            return True
        return any(secret.encode("utf-8") in blob for secret in self._redactions)


def inject_env_secrets(
    vault: Vault,
    handles: Iterable[str],
    *,
    env: dict[str, str] | None = None,
    prefix: str = "MAISYA_SECRET_",
) -> dict[str, str]:
    return vault.inject_env_secrets(handles, env=env, prefix=prefix)


def redact_secrets(value, values):
    if isinstance(values, Vault):
        return values.redact(value)
    if isinstance(values, (str, bytes, bytearray)):
        items = [values]
    elif isinstance(values, Iterable):
        items = list(values)
    else:
        return value
    patterns: set[str | bytes] = set()
    for item in items:
        if isinstance(item, (bytes, bytearray)):
            raw = bytes(item)
            if _is_high_entropy_secret(raw):
                patterns.add(raw)
            patterns.update(secret_variants(raw))
        elif isinstance(item, str) and item:
            patterns.update(secret_variants(item))
    return redact_values(value, patterns)
