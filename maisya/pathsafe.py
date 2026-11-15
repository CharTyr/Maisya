"""跨平台路径安全校验、无符号链接文件操作、文件锁与受限子进程环境。"""

from __future__ import annotations

import errno
import ntpath
import os
import re
import secrets
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_ORIG_OPEN = os.open
_OPEN_HAS_DIR_FD = os.open in getattr(os, "supports_dir_fd", ())

_WINDOWS_INVALID_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{index}" for index in range(10)),
        *(f"LPT{index}" for index in range(10)),
    }
)
_DRIVE_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _eloop_errnos() -> set[int]:
    errnos = {40, 62}
    eloop = getattr(errno, "ELOOP", None)
    if isinstance(eloop, int):
        errnos.add(eloop)
    return errnos


def _open_extra(*, nonblock: bool = False) -> int:
    flags = 0
    for name in ("O_BINARY", "O_NOINHERIT", "O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    if nonblock:
        flags |= getattr(os, "O_NONBLOCK", 0)
    return flags


def has_dir_fd() -> bool:
    """判断当前运行时是否安全支持基于 dir_fd 与 O_NOFOLLOW/O_DIRECTORY 的操作。"""
    if os.name == "nt":
        return False
    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        return False
    supports = getattr(os, "supports_dir_fd", ())
    if os.open in supports:
        return True
    return bool(
        _OPEN_HAS_DIR_FD and os.open is not _ORIG_OPEN and _ORIG_OPEN in supports
    )


def is_link_like(st: os.stat_result) -> bool:
    """判断 stat 结果是否表示符号链接或 Windows junction/reparse point。"""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_reparse_tag", 0))


def path_is_link_like(path: os.PathLike[str] | str) -> bool:
    """不跟随链接检查路径本身是否为符号链接或 reparse point。"""
    try:
        return is_link_like(os.lstat(path))
    except (FileNotFoundError, NotADirectoryError):
        return False


def check_part(part: str) -> str:
    """校验单个路径段在 POSIX 与 Windows 下均安全且合法。"""
    if not isinstance(part, str) or not part:
        raise ValueError("工作区路径无效")
    if part in {".", ".."}:
        raise ValueError("工作区路径越界")
    for ch in part:
        if ord(ch) < 32 or ch in _WINDOWS_INVALID_CHARS:
            raise ValueError(f"路径段包含非法字符: {part!r}")
    if part.endswith((".", " ")):
        raise ValueError(f"路径段不能以点或空格结尾: {part!r}")
    stem = part.split(".", 1)[0].rstrip(" ").upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"路径段不能使用保留设备名: {part!r}")
    return part


def split_rel(rel: str) -> tuple[str, ...]:
    """拆分并逐段校验正斜杠相对路径。"""
    if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel:
        raise ValueError("工作区路径无效")
    if rel.startswith("/") or (len(rel) >= 2 and rel[1] == ":"):
        raise ValueError("工作区路径越界")
    parts = rel.split("/")
    for part in parts:
        check_part(part)
    return tuple(parts)


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    if before.st_ino == 0 and after.st_ino == 0:
        return (
            before.st_dev == after.st_dev
            and before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
        )
    return before.st_dev == after.st_dev and before.st_ino == after.st_ino


def _check_dir(path: Path) -> os.stat_result:
    try:
        st = os.lstat(path)
    except OSError as error:
        if error.errno in _eloop_errnos():
            raise ValueError("工作区不能包含符号链接") from None
        raise
    if is_link_like(st):
        raise ValueError("工作区不能包含符号链接")
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(path))
    return st


def ensure_dirs(
    anchor: os.PathLike[str] | str,
    parts: Iterable[str],
    *,
    create: bool = True,
    mode: int = 0o700,
) -> Path:
    """从锚点目录逐级校验（并在 create=True 时按段创建）子目录链。"""
    current = Path(anchor)
    _check_dir(current)
    for part in parts:
        check_part(part)
        nxt = current / part
        if create:
            try:
                os.lstat(nxt)
            except FileNotFoundError:
                try:
                    os.mkdir(nxt, mode)
                except FileExistsError:
                    pass
        _check_dir(nxt)
        current = nxt
    return current


def lstat_regular(path: os.PathLike[str] | str, label: str = "") -> os.stat_result:
    """校验目标是不含链接或硬链接的普通文件并返回 lstat 结果。"""
    del label
    try:
        st = os.lstat(path)
    except OSError as error:
        if error.errno in _eloop_errnos():
            raise ValueError("工作区不能包含符号链接") from None
        raise
    if is_link_like(st):
        raise ValueError("工作区不能包含符号链接")
    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
        raise ValueError("工作区只接受无硬链接的普通文件")
    return st


def open_read_fd(path: os.PathLike[str] | str, label: str = "") -> int:
    """lstat 校验后安全只读打开并经 fstat 复核身份，返回文件描述符。"""
    target = Path(path)
    before = lstat_regular(target, label)
    try:
        fd = os.open(target, os.O_RDONLY | _open_extra(nonblock=True))
    except OSError as error:
        if error.errno in _eloop_errnos():
            raise ValueError("工作区不能包含符号链接") from None
        raise
    try:
        after = os.fstat(fd)
        invalid_nlink = after.st_nlink > 1 or (
            os.name != "nt" and after.st_nlink != 1
        )
        if (
            is_link_like(after)
            or not stat.S_ISREG(after.st_mode)
            or invalid_nlink
            or not _same_file(before, after)
        ):
            raise ValueError("工作区只接受无硬链接的普通文件")
    except BaseException:
        os.close(fd)
        raise
    return fd


def create_exclusive(path: os.PathLike[str] | str, mode: int = 0o600) -> int:
    """使用 O_CREAT | O_EXCL 独占创建普通文件，返回文件描述符。"""
    target = Path(path)
    check_part(target.name)
    if path_is_link_like(target):
        raise ValueError("工作区不能包含符号链接")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _open_extra()
    try:
        fd = os.open(target, flags, mode)
    except OSError as error:
        if error.errno in _eloop_errnos():
            raise ValueError("工作区不能包含符号链接") from None
        raise
    try:
        info = os.fstat(fd)
        if is_link_like(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise ValueError("工作区只接受无硬链接的普通文件")
    except BaseException:
        os.close(fd)
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    return fd


def replace_atomic(
    parent: os.PathLike[str] | str,
    name: str,
    data: bytes,
    *,
    mode: int = 0o600,
    recheck: Callable[[], object] | None = None,
) -> None:
    """在已校验父目录中写独占临时文件、复核身份并原子替换目标文件。"""
    check_part(name)
    parent_path = Path(parent)
    parent_before = _check_dir(parent_path)
    target = parent_path / name
    try:
        old_stat: os.stat_result | None = lstat_regular(target, name)
    except FileNotFoundError:
        old_stat = None

    temp_path = parent_path / f".tmp.{secrets.token_hex(8)}"
    fd = create_exclusive(temp_path, mode)
    try:
        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        parent_after = _check_dir(parent_path)
        if not _same_file(parent_before, parent_after):
            raise ValueError("写入期间工作区目录被替换")
        if recheck is not None:
            recheck()
        try:
            current: os.stat_result | None = os.lstat(target)
        except FileNotFoundError:
            current = None
        if old_stat is None:
            if current is not None:
                raise ValueError("写入期间目标文件发生变化")
        elif (
            current is None
            or is_link_like(current)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink > 1
            or not _same_file(old_stat, current)
        ):
            raise ValueError("写入期间目标文件发生变化")
        os.replace(temp_path, target)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def safe_chmod(path: os.PathLike[str] | str, mode: int) -> None:
    """仅在 POSIX 下修改权限位，Windows 下跳过以避免只读属性阻塞替换或删除。"""
    if os.name == "nt":
        return
    Path(path).chmod(mode)


def rmtree_safe(path: os.PathLike[str] | str, *, ignore_errors: bool = False) -> None:
    """递归删除目录，遇到 Windows 只读位 PermissionError 时清除只读位重试。"""
    target = Path(path)

    def _on_error(func: Callable[[str], object], failing_path: str, exc_info: object) -> None:
        err = exc_info[1] if isinstance(exc_info, tuple) else exc_info
        if isinstance(err, PermissionError):
            try:
                if not path_is_link_like(failing_path):
                    current_mode = os.lstat(failing_path).st_mode
                    os.chmod(failing_path, current_mode | stat.S_IWRITE)
                func(failing_path)
                return
            except FileNotFoundError:
                return
            except OSError:
                pass
        if not ignore_errors:
            if isinstance(err, BaseException):
                raise err
            raise PermissionError(failing_path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, ignore_errors=ignore_errors, onexc=_on_error)
    else:
        shutil.rmtree(target, ignore_errors=ignore_errors, onerror=_on_error)


def _get_thread_lock(path: Path) -> threading.Lock:
    canonical = os.path.normcase(os.path.realpath(path))
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(canonical)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[canonical] = lock
        return lock


@dataclass
class LockHandle:
    """跨平台文件锁句柄。"""

    fd: int
    thread_lock: threading.Lock | None = None
    locked: bool = True


def acquire_lock_handle(
    path: os.PathLike[str] | str,
    *,
    nonblocking: bool = False,
) -> LockHandle:
    """获取跨平台文件锁句柄；非阻塞竞争时统一抛出 BlockingIOError。"""
    target = Path(path)
    if path_is_link_like(target):
        raise ValueError("锁文件不能是符号链接")
    if os.name != "nt":
        import fcntl

        fd = os.open(target, os.O_RDWR | os.O_CREAT | _open_extra(), 0o600)
        try:
            info = os.fstat(fd)
            if is_link_like(info) or not stat.S_ISREG(info.st_mode):
                raise ValueError("锁文件必须是普通文件")
            mode = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
            fcntl.flock(fd, mode)
        except BaseException:
            os.close(fd)
            raise
        return LockHandle(fd=fd)

    import msvcrt

    thread_lock = _get_thread_lock(target)
    if not thread_lock.acquire(blocking=not nonblocking):
        raise BlockingIOError(errno.EAGAIN, "锁已被同一进程占用", str(target))
    fd = -1
    try:
        fd = os.open(target, os.O_RDWR | os.O_CREAT | _open_extra(), 0o600)
        info = os.fstat(fd)
        if is_link_like(info) or not stat.S_ISREG(info.st_mode):
            raise ValueError("锁文件必须是普通文件")
        delay = 0.005
        max_wait = 30.0
        waited = 0.0
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except PermissionError as error:
                retryable = True
                last_error = error
            except OSError as error:
                retryable = error.errno in {
                    errno.EACCES,
                    errno.EAGAIN,
                    getattr(errno, "EDEADLOCK", 36),
                }
                last_error = error
            if not retryable:
                raise last_error
            if nonblocking:
                raise BlockingIOError(
                    errno.EAGAIN, str(last_error), str(target)
                ) from last_error
            if waited >= max_wait:
                raise TimeoutError(
                    f"获取文件锁超时: {target}"
                ) from last_error
            sleep_for = min(delay, max_wait - waited)
            time.sleep(sleep_for)
            waited += sleep_for
            delay = min(0.1, delay * 1.5)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        thread_lock.release()
        raise
    return LockHandle(fd=fd, thread_lock=thread_lock)


def release_lock_handle(handle: LockHandle | None) -> None:
    """幂等释放文件锁句柄。"""
    if handle is None or not handle.locked:
        return
    handle.locked = False
    try:
        if os.name != "nt":
            import fcntl

            try:
                fcntl.flock(handle.fd, fcntl.LOCK_UN)
            finally:
                os.close(handle.fd)
        else:
            import msvcrt

            try:
                os.lseek(handle.fd, 0, os.SEEK_SET)
                msvcrt.locking(handle.fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(handle.fd)
    finally:
        if handle.thread_lock is not None:
            handle.thread_lock.release()


@contextmanager
def file_lock(path: os.PathLike[str] | str, *, nonblocking: bool = False):
    """获取并自动释放跨平台文件锁。"""
    handle = acquire_lock_handle(path, nonblocking=nonblocking)
    try:
        yield handle
    finally:
        release_lock_handle(handle)


def is_absolute_program_path(raw: str) -> bool:
    """校验程序路径为 POSIX 绝对路径或 Windows 盘符绝对路径，拒绝相对路径与 UNC 路径。"""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return False
    if raw.startswith("//") or raw.startswith("\\\\"):
        return False
    if raw.startswith("/"):
        return True
    return bool(_DRIVE_ABS_RE.match(raw))


def is_native_absolute_program_path(raw: str) -> bool:
    """按当前宿主操作系统原生规则校验可执行程序绝对路径，拒绝相对路径、UNC 与跨平台不兼容前缀。"""
    if (
        not isinstance(raw, str)
        or not raw
        or "\x00" in raw
        or "\r" in raw
        or "\n" in raw
    ):
        return False
    if raw.startswith("//") or raw.startswith("\\\\"):
        return False
    if os.name == "nt":
        return bool(_DRIVE_ABS_RE.match(raw))
    return raw.startswith("/") and not _DRIVE_ABS_RE.match(raw)


def _env_ci(name: str, default: str = "") -> str:
    if name in os.environ:
        return os.environ[name]
    upper = name.upper()
    for key, value in os.environ.items():
        if key.upper() == upper:
            return value
    return default


def minimal_subprocess_env(*, home: Path | None = None) -> dict[str, str]:
    """构造跨平台最小可信子进程环境变量。"""
    if os.name != "nt":
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        if home is not None:
            env["HOME"] = str(home)
        return env

    system_root = _env_ci("SystemRoot") or _env_ci("WINDIR") or r"C:\Windows"
    windir = _env_ci("WINDIR") or system_root
    trusted_path = os.pathsep.join(
        [
            ntpath.join(system_root, "System32"),
            system_root,
            ntpath.join(system_root, "System32", "Wbem"),
            ntpath.join(system_root, "System32", "OpenSSH"),
        ]
    )
    env = {
        "PATH": trusted_path,
        "SystemRoot": system_root,
        "windir": windir,
        "PROGRAMDATA": _env_ci("PROGRAMDATA") or r"C:\ProgramData",
        "PATHEXT": _env_ci("PATHEXT") or ".COM;.EXE;.BAT;.CMD",
        "COMSPEC": _env_ci("COMSPEC") or ntpath.join(system_root, "System32", "cmd.exe"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    if home is not None:
        home_str = str(home)
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
            env[key] = home_str
    return env
