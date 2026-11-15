"""只进不出的凭据保险箱与出站脱敏。"""

import base64
import errno
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from .pathsafe import create_exclusive, path_is_link_like, replace_atomic, safe_chmod


class VaultError(RuntimeError):
    pass


def secret_variants(value: str) -> set[str]:
    if not value:
        return set()
    result = {
        value,
        json.dumps(value, ensure_ascii=False)[1:-1],
        json.dumps(value, ensure_ascii=True)[1:-1],
        base64.b64encode(value.encode()).decode(),
        quote(value, safe=""),
    }
    if "\n" in value:
        result.update(
            line.strip() for line in value.splitlines() if len(line.strip()) >= 8
        )
    return result


def redact_values(value, secrets):
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact_values(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            redact_values(key, secrets): redact_values(item, secrets)
            for key, item in value.items()
        }
    return value


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
        self.path = private_directory(path)
        self._redactions: set[str] = set()
        key_path = path / "master.key"
        if path_is_link_like(key_path):
            raise VaultError("保险箱密钥不能是符号链接")
        if not key_path.exists():
            if list(path.glob("*.enc")):
                raise VaultError("保险箱密钥缺失，已停止读取")
            key_bytes = Fernet.generate_key()
            temp_path = path / f".master.{secrets.token_hex(8)}.tmp"
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
                    if error.errno not in {errno.EPERM, errno.ENOTSUP, errno.EXDEV, errno.EOPNOTSUPP}:
                        raise
                    if not key_path.exists():
                        replace_atomic(path, "master.key", key_bytes, mode=0o600)
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
        try:
            self._cipher = Fernet(key_path.read_bytes())
            for encrypted in path.glob("*.enc"):
                if path_is_link_like(encrypted):
                    raise VaultError("保险箱文件不能是符号链接")
                safe_chmod(encrypted, 0o600)
                record = json.loads(self._cipher.decrypt(encrypted.read_bytes()))
                if record["handle"] != encrypted.stem or not isinstance(
                    record["value"], str
                ):
                    raise ValueError
                self.protect(record["value"])
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
        token = self._cipher.encrypt(
            json.dumps({"handle": handle, "value": value}).encode()
        )
        atomic_private_write(self.path / f"{handle}.enc", token)
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
            raise VaultError("凭据文件不能是符号链接")
        try:
            record = json.loads(self._cipher.decrypt(path.read_bytes()))
            if record["handle"] != handle or not isinstance(record["value"], str):
                raise ValueError("凭据结构无效")
            self.protect(record["value"])
            return record["value"]
        except (InvalidToken, ValueError, KeyError, TypeError, OSError):
            raise VaultError("凭据读取失败") from None

    def protect(self, value: str) -> None:
        self._redactions.update(secret_variants(value))

    def redact(self, value):
        return redact_values(value, self._redactions)

    def contains_secret(self, content):
        if not isinstance(content, bytes):
            raise TypeError("凭据检查需要字节内容")
        return any(secret.encode() in content for secret in self._redactions)
