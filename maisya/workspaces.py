"""线程工作区、原子检查点与会话内不可变附件。"""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .vault import atomic_private_write, private_directory


async def completed_thread(function, *arguments, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *arguments, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


@contextmanager
def parent_fd(root, relative, *, create=False):
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or "\x00" in relative
    ):
        raise ValueError("工作区路径无效")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"..", "."} for part in relative.split("/")):
        raise ValueError("工作区路径越界")
    if not path.parts or path.parts[0] not in {"project", "in", "out", "cache"}:
        raise ValueError("路径必须位于 project、in、out 或 cache")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor, path.name
    finally:
        os.close(descriptor)


def regular_open(root, relative):
    try:
        with parent_fd(root, relative) as (directory, name):
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
    except OSError as error:
        if error.errno in {40, 62}:
            raise ValueError("工作区不能包含符号链接") from None
        raise
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise ValueError("工作区只接受无硬链接的普通文件")
    return os.fdopen(descriptor, "rb")


def inventory(root, *, roots=("project", "in", "out"), max_bytes, max_files):
    files, total = [], 0
    for name in roots:
        start = root / name
        if start.is_symlink():
            raise ValueError("工作区目录不能是符号链接")
        if not start.exists():
            continue
        for directory, children, names in os.walk(start, followlinks=False):
            children[:] = [
                child
                for child in children
                if child not in {"node_modules", ".venv", "__pycache__", ".cache"}
            ]
            for child in children:
                if (Path(directory) / child).is_symlink():
                    raise ValueError("工作区目录不能包含符号链接")
            for child in names:
                relative = (Path(directory) / child).relative_to(root).as_posix()
                with regular_open(root, relative) as source:
                    info = os.fstat(source.fileno())
                    total += info.st_size
                files.append((relative, info.st_size))
                if total > max_bytes or len(files) > max_files:
                    raise ValueError("工作区超过文件数或容量配额")
    return files, total


def copy_files(source, destination, files):
    for relative, _size in files:
        with regular_open(source, relative) as incoming:
            with parent_fd(destination, relative, create=True) as (directory, name):
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
            with os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(incoming, output, 65536)
                output.flush()
                os.fsync(output.fileno())


class WorkingCopy:
    def __init__(self, owner, row, path, durable):
        self.owner, self.row, self.path, self.durable = owner, row, path, durable
        self.quarantined = False

    def read(self, path, offset=0):
        if type(offset) is not int or offset < 0:
            raise ValueError("文件游标无效")
        with regular_open(self.path, path) as source:
            source.seek(offset)
            content = source.read(12000)
            size = os.fstat(source.fileno()).st_size
        return {
            "text": content.decode("utf-8", errors="replace"),
            "next_offset": offset + len(content)
            if offset + len(content) < size
            else None,
            "bytes": size,
        }

    def write(self, path, content):
        if not isinstance(content, str):
            raise ValueError("写入内容格式无效")
        return self.write_bytes(path, content.encode())

    def write_bytes(self, path, data):
        if path.startswith("in/"):
            raise PermissionError("引用输入只读")
        if not isinstance(data, (bytes, bytearray)) or len(data) > self.owner.max_bytes:
            raise ValueError("写入内容过大或格式无效")
        raw = bytes(data)
        files, used = self.owner.inventory(self.path)
        previous = next((size for name, size in files if name == path), 0)
        if used - previous + len(raw) > self.owner.max_bytes:
            raise ValueError("工作区超过容量配额")
        if (
            previous == 0
            and path not in dict(files)
            and len(files) >= self.owner.max_files
        ):
            raise ValueError("工作区超过文件数配额")
        with parent_fd(self.path, path, create=True) as (directory, name):
            temporary = ".write-" + uuid.uuid4().hex
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                if name in os.listdir(directory):
                    with regular_open(self.path, path):
                        pass
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return {"path": path, "bytes": len(raw)}

    def listing(self):
        files, size = self.owner.inventory(self.path)
        return {
            "files": [{"path": path, "bytes": length} for path, length in files[:200]],
            "total_files": len(files),
            "bytes": size,
        }

    def commit(self):
        files, _used = self.owner.inventory(self.path)
        with self.owner.capacity_lock():
            total = self.owner.usage()
            incoming = sum(size for _path, size in files)
            if total + incoming > self.owner.total_bytes:
                raise ValueError("全部工作区和附件已达到容量配额")
            version = "v_" + uuid.uuid4().hex
            destination = private_directory(self.durable / version)
            try:
                copy_files(self.path, destination, files)
                with self.owner.store.connect() as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute(
                        "SELECT state FROM agent_threads WHERE thread_id=?",
                        (self.row["thread_id"],),
                    ).fetchone()
                    if not current or current["state"] == "closed":
                        raise RuntimeError("线程已停止，文件检查点未提交")
                    atomic_private_write(
                        self.durable / "current.json",
                        json.dumps({"version": version}).encode(),
                    )
            except BaseException:
                shutil.rmtree(destination)
                raise
            for old in self.durable.glob("v_*"):
                if old != destination:
                    shutil.rmtree(old)
        return version

    def rollback(self):
        for name in ("project", "in", "out", "cache"):
            target = self.path / name
            if target.is_symlink():
                target.unlink()
            elif target.exists():
                shutil.rmtree(target)
            private_directory(target)
        pointer = self.durable / "current.json"
        if pointer.exists():
            version = json.loads(pointer.read_text())["version"]
            if not re.fullmatch(r"v_[a-f0-9]{32}", version):
                raise ValueError("检查点编号无效")
            files, _size = self.owner.inventory(self.durable / version)
            copy_files(self.durable / version, self.path, files)


class Workspaces:
    def __init__(
        self,
        root,
        store,
        *,
        max_bytes=128 * 1024**2,
        max_files=8192,
        total_bytes=1024**3,
        volume=None,
        contains_secret=lambda content: False,
    ):
        self.root = private_directory(Path(root))
        self.store, self.max_bytes, self.max_files, self.total_bytes = (
            store,
            max_bytes,
            max_files,
            total_bytes,
        )
        self.volume = volume
        self.contains_secret = contains_secret
        self.runtime = private_directory(self.root / "runtime")
        self.artifacts = private_directory(self.root / "attachments")
        self.quarantines = {}
        with store.connect() as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS attachments (handle TEXT PRIMARY KEY, chat_id TEXT, thread_id TEXT, revision INTEGER, name TEXT, kind TEXT, size INTEGER, digest TEXT, created REAL)"
            )

    def inventory(self, path):
        return inventory(path, max_bytes=self.max_bytes, max_files=self.max_files)

    def read_checkpoint_outputs(self, row):
        workspace_id = (
            row.get("workspace_id")
            or "ws_" + hashlib.sha256(row["thread_id"].encode()).hexdigest()[:24]
        )
        if not workspace_id.startswith("ws_") or not workspace_id[3:].isalnum():
            return []
        durable = self.root / workspace_id
        pointer = durable / "current.json"
        if not pointer.is_file() or pointer.is_symlink():
            return []
        try:
            version = json.loads(pointer.read_text()).get("version", "")
        except (OSError, ValueError):
            return []
        if not re.fullmatch(r"v_[a-f0-9]{32}", version):
            return []
        source = durable / version
        if not source.is_dir() or source.is_symlink():
            return []
        out_files, _size = inventory(
            source, roots=("out",), max_bytes=self.max_bytes, max_files=self.max_files
        )
        items = []
        total_out = 0
        budget = min(self.max_bytes, 24 * 1024**2)
        for rel, size in sorted(out_files):
            if len(items) >= 32:
                break
            if size <= 0 or size > 6 * 1024**2 or total_out + size > budget:
                continue
            with regular_open(source, rel) as handle:
                data = handle.read(6 * 1024**2 + 1)
            if len(data) > 6 * 1024**2 or self.contains_secret(data):
                continue
            subpath = rel[4:] if rel.startswith("out/") else rel
            if subpath:
                items.append((subpath, data))
                total_out += len(data)
        return items

    def usage(self):
        return sum(
            path.lstat().st_size
            for path in self.root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and self.runtime not in path.parents
        )

    @contextmanager
    def capacity_lock(self):
        with (self.root / ".capacity.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def activity(self, row):
        workspace_id = (
            row.get("workspace_id")
            or "ws_" + hashlib.sha256(row["thread_id"].encode()).hexdigest()[:24]
        )
        if not workspace_id.startswith("ws_") or not workspace_id[3:].isalnum():
            raise ValueError("内部工作区编号无效")
        durable = private_directory(self.root / workspace_id)
        lock = (durable / ".write.lock").open("a+b")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("工作区正由原线程写入") from None
        temporary = Path(tempfile.mkdtemp(prefix="activity-", dir=self.runtime))
        volume = self._volume(temporary)
        work = WorkingCopy(self, row, temporary, durable)
        mounted = False
        try:
            volume.__enter__()
            mounted = True
            for name in ("project", "in", "out", "cache"):
                private_directory(temporary / name)
            pointer = durable / "current.json"
            current = None
            if pointer.exists():
                current = json.loads(pointer.read_text())["version"]
                if not re.fullmatch(r"v_[a-f0-9]{32}", current):
                    raise ValueError("检查点编号无效")
                source = durable / current
                files, _size = self.inventory(source)
                copy_files(source, temporary, files)
            with self.capacity_lock():
                for abandoned in durable.glob("v_*"):
                    if abandoned.name != current:
                        if abandoned.is_symlink() or not re.fullmatch(
                            r"v_[a-f0-9]{32}", abandoned.name
                        ):
                            raise ValueError("检查点路径无效")
                        shutil.rmtree(abandoned)
            yield work
        finally:
            if work.quarantined:
                self.quarantines[workspace_id] = lock, volume, temporary
            else:
                if mounted:
                    volume.__exit__(None, None, None)
                shutil.rmtree(temporary)
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()

    def release_quarantines(self):
        for key, (lock, volume, path) in list(self.quarantines.items()):
            volume.__exit__(None, None, None)
            shutil.rmtree(path)
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
            self.quarantines.pop(key)

    @contextmanager
    def _volume(self, path):
        if self.volume:
            with self.volume(path):
                yield
        else:
            yield

    def attach(self, row, work, paths):
        if not isinstance(paths, list) or not 1 <= len(paths) <= 4:
            raise ValueError("一次最多声明四个附件")
        result = []
        for path in paths:
            if not isinstance(path, str) or not path.startswith("out/"):
                raise ValueError("附件必须位于 out 目录")
            handle = "att_" + uuid.uuid4().hex
            destination = self.artifacts / handle
            with self.capacity_lock(), regular_open(work.path, path) as source:
                size = os.fstat(source.fileno()).st_size
                if (
                    size <= 0
                    or size > self.max_bytes
                    or self.usage() + size > self.total_bytes
                ):
                    raise ValueError("附件为空或超过配额")
                if self.contains_secret(source.read(self.max_bytes + 1)):
                    raise ValueError("附件包含已登记凭据，已拒绝导出")
                source.seek(0)
                with destination.open("xb") as output:
                    shutil.copyfileobj(source, output, 65536)
                    output.flush()
                    os.fsync(output.fileno())
                destination.chmod(0o600)
                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                name = PurePosixPath(path).name
                kind = (
                    "image"
                    if destination.stat().st_size
                    and Path(name).suffix.lower()
                    in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
                    else "file"
                )
                try:
                    with self.store.connect() as db, db:
                        db.execute(
                            "INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                handle,
                                row["chat_id"],
                                row["thread_id"],
                                row.get("revision", row.get("latest_revision", 0)),
                                name,
                                kind,
                                size,
                                digest,
                                time.time(),
                            ),
                        )
                except BaseException:
                    destination.unlink()
                    raise
            result.append({"handle": handle, "name": name, "kind": kind, "bytes": size})
        return result

    def resolve(self, handle, chat_id):
        if (
            not isinstance(handle, str)
            or not handle.startswith("att_")
            or not handle[4:].isalnum()
        ):
            raise ValueError("附件句柄无效")
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM attachments WHERE handle=?", (handle,)
            ).fetchone()
        if not row or row["chat_id"] != chat_id:
            raise PermissionError("附件不属于本会话")
        path = self.artifacts / handle
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != row["size"]
            or hashlib.sha256(path.read_bytes()).hexdigest() != row["digest"]
        ):
            raise ValueError("附件已损坏")
        return path
