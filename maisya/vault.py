"""只进不出的凭据保险箱与出站脱敏。"""

import base64
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken


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


def private_directory(path: Path) -> Path:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise VaultError("保险箱路径不能经过符号链接")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def atomic_private_write(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise VaultError("保险箱文件不能是符号链接")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Vault:
    def __init__(self, path: Path):
        self.path = private_directory(path)
        self._redactions: set[str] = set()
        key_path = path / "master.key"
        if key_path.is_symlink():
            raise VaultError("保险箱密钥不能是符号链接")
        if not key_path.exists():
            if list(path.glob("*.enc")):
                raise VaultError("保险箱密钥缺失，已停止读取")
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(Fernet.generate_key())
                output.flush()
                os.fsync(output.fileno())
        key_path.chmod(0o600)
        try:
            self._cipher = Fernet(key_path.read_bytes())
            for encrypted in path.glob("*.enc"):
                if encrypted.is_symlink():
                    raise VaultError("保险箱文件不能是符号链接")
                encrypted.chmod(0o600)
                record = json.loads(self._cipher.decrypt(encrypted.read_bytes()))
                if record["handle"] != encrypted.stem or not isinstance(
                    record["value"], str
                ):
                    raise ValueError
                self.protect(record["value"])
        except (InvalidToken, ValueError, KeyError, TypeError):
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
        if path.is_symlink():
            raise VaultError("凭据文件不能是符号链接")
        try:
            record = json.loads(self._cipher.decrypt(path.read_bytes()))
            if record["handle"] != handle or not isinstance(record["value"], str):
                raise ValueError("凭据结构无效")
            self.protect(record["value"])
            return record["value"]
        except (InvalidToken, ValueError, KeyError):
            raise VaultError("凭据读取失败") from None

    def protect(self, value: str) -> None:
        self._redactions.update(secret_variants(value))

    def redact(self, value):
        return redact_values(value, self._redactions)

    def contains_secret(self, content):
        if not isinstance(content, bytes):
            raise TypeError("凭据检查需要字节内容")
        return any(secret.encode() in content for secret in self._redactions)
