"""只进不出的凭据保险箱与出站脱敏。"""

import base64
from collections import Counter
import errno
import json
import math
import os
import re
import secrets
import tempfile
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

    def set(self, handle: str, value: str) -> None:
        self._validate_handle(handle)
        if not isinstance(value, str) or not value or len(value.encode()) > 65536:
            raise ValueError("凭据必须为非空文本，且不超过 64 KiB")
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

    def delete(self, handle: str) -> None:
        self._validate_handle(handle)
        (self.path / f"{handle}.enc").unlink(missing_ok=True)

    def handles(self) -> list[str]:
        return sorted(path.stem for path in self.path.glob("*.enc"))

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
