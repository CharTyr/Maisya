"""分层文件、版本历史与按会话隔离的检索。"""

import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from .abilities import _ensure_managed_dir, _has_relative_link, is_autonomous_snapshot
from .pathsafe import (
    check_part,
    ensure_dirs,
    file_lock,
    has_dir_fd,
    open_read_fd,
    path_is_link_like,
    rmtree_safe,
)
from .vault import atomic_private_write

PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
SKILL_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_SKILL_ARCHIVE_MAX_BYTES = 8 * 1024 * 1024
_SKILL_ARCHIVE_MAX_UNPACKED = 16 * 1024 * 1024
_SKILL_FILE_MAX_BYTES = 2 * 1024 * 1024
_SKILL_MAX_ENTRIES = 512
_SKILL_MAX_FILES = 64
_JOURNAL_BLOCK_RE = (
    r"^##[ \t]+(\d{4}-\d{2}-\d{2})[ \t]*(?:\n|\Z)"
    r"(.*?)(?=^##[ \t]+\d{4}-\d{2}-\d{2}[ \t]*(?:\n|\Z)|\Z)"
)
_SKILL_MAX_BUNDLE = 20
_SECRET_RE = re.compile(
    r"-----BEGIN .*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b"
)
_COMPANION_SEG_RE = re.compile(r"[A-Za-z0-9._\u4e00-\u9fff-]{1,80}")
_ARCHIVE_JUNK_PARTS = frozenset(
    {
        "__MACOSX",
        ".git",
        ".svn",
        ".hg",
        "__pycache__",
        "node_modules",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
_ARCHIVE_JUNK_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


def _validate_tz_offset_minutes(tz_offset_minutes: int) -> int:
    if type(tz_offset_minutes) is not int or not -720 <= tz_offset_minutes <= 840:
        raise ValueError("时区偏移分钟数无效")
    return tz_offset_minutes


def _slug_skill_id(raw: str) -> str:
    text = str(raw or "").strip()
    for suffix in (
        ".tar.gz",
        ".tar.bz2",
        ".tar.xz",
        ".tgz",
        ".zip",
        ".skill",
        ".md",
        ".txt",
    ):
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)]
            break
    for tail in ("-main", "-master", "-head", "-HEAD"):
        if len(text) > len(tail) and text.endswith(tail):
            text = text[: -len(tail)]
            break
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-_")
    if not cleaned:
        return ""
    if not cleaned[0].isalpha():
        cleaned = f"skill-{cleaned}"
    cleaned = cleaned[:64].rstrip("-_")
    return cleaned if SKILL_ID_RE.fullmatch(cleaned) else ""


def _normalize_archive_path(raw_name: str) -> str:
    if not isinstance(raw_name, str) or "\x00" in raw_name or "\\" in raw_name:
        raise ValueError("技能压缩包包含非法路径")
    stripped = raw_name.strip()
    while stripped.startswith("./"):
        stripped = stripped[2:]
    stripped = stripped.rstrip("/")
    if not stripped or stripped == ".":
        return ""
    if stripped.startswith("/"):
        raise ValueError("技能压缩包不支持绝对路径")
    parts = stripped.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("技能压缩包包含越界或空路径段")
    return "/".join(parts)


def _is_archive_junk(rel_path: str) -> bool:
    parts = rel_path.split("/")
    if any(part in _ARCHIVE_JUNK_PARTS for part in parts):
        return True
    base = parts[-1]
    return base in _ARCHIVE_JUNK_FILES or base.startswith("._")


def _extract_skill_archive_entries(
    raw_bytes: bytes, *, filename: str = ""
) -> dict[str, bytes]:
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise TypeError("技能包内容必须为字节")
    raw = bytes(raw_bytes)
    if not raw or len(raw) > _SKILL_ARCHIVE_MAX_BYTES:
        raise ValueError("技能包为空或超过 8 MiB 上限")

    lower_name = str(filename or "").strip().lower()
    is_zip_ext = lower_name.endswith((".zip", ".skill"))
    is_tar_ext = lower_name.endswith(
        (".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tar.xz")
    )
    is_md_ext = lower_name.endswith((".md", ".txt"))

    mode = ""
    if is_zip_ext or zipfile.is_zipfile(io.BytesIO(raw)):
        mode = "zip"
    elif is_tar_ext:
        mode = "tar"
    elif is_md_ext:
        mode = "md"
    else:
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
                tf.getmembers()
            mode = "tar"
        except (tarfile.TarError, EOFError, OSError):
            if not lower_name:
                raise ValueError(
                    "无法识别技能包格式，请上传 .zip、.skill、.tar.gz、.tgz 或 SKILL.md"
                ) from None
            raise ValueError(
                "不支持的技能包格式，请上传 .zip、.skill、.tar.gz、.tgz 或 SKILL.md"
            ) from None

    if mode == "md":
        return {"SKILL.md": raw}

    entries: dict[str, bytes] = {}
    total_unpacked = 0

    if mode == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = archive.infolist()
                if len(infos) > _SKILL_MAX_ENTRIES:
                    raise ValueError("技能压缩包内条目过多")
                for info in infos:
                    if (info.external_attr >> 16) & 0o170000 not in {
                        0,
                        0o100000,
                        0o040000,
                    }:
                        raise ValueError("技能压缩包不接受符号链接或特殊文件")
                    rel = _normalize_archive_path(info.filename)
                    if not rel or info.is_dir() or _is_archive_junk(rel):
                        continue
                    if rel in entries:
                        raise ValueError(f"技能压缩包存在重复路径: {rel}")
                    if info.file_size > _SKILL_FILE_MAX_BYTES:
                        raise ValueError(f"技能文件过大: {rel}")
                    with archive.open(info, "r") as stream:
                        data = stream.read(_SKILL_FILE_MAX_BYTES + 1)
                    if len(data) > _SKILL_FILE_MAX_BYTES:
                        raise ValueError(f"技能文件过大: {rel}")
                    total_unpacked += len(data)
                    if total_unpacked > _SKILL_ARCHIVE_MAX_UNPACKED or len(entries) >= 256:
                        raise ValueError("技能压缩包解压总量超过预算")
                    entries[rel] = data
        except zipfile.BadZipFile:
            raise ValueError("ZIP 技能压缩包已损坏或格式无效") from None
        return entries

    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
            count = 0
            for member in archive:
                count += 1
                if count > _SKILL_MAX_ENTRIES:
                    raise ValueError("技能压缩包内条目过多")
                if member.type in (tarfile.XHDTYPE, tarfile.XGLTYPE):
                    continue
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ValueError("技能压缩包不接受符号链接或特殊文件")
                rel = _normalize_archive_path(member.name)
                if not rel or member.isdir() or not member.isfile() or _is_archive_junk(rel):
                    continue
                if rel in entries:
                    raise ValueError(f"技能压缩包存在重复路径: {rel}")
                if member.size > _SKILL_FILE_MAX_BYTES:
                    raise ValueError(f"技能文件过大: {rel}")
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                with stream:
                    data = stream.read(_SKILL_FILE_MAX_BYTES + 1)
                if len(data) > _SKILL_FILE_MAX_BYTES:
                    raise ValueError(f"技能文件过大: {rel}")
                total_unpacked += len(data)
                if total_unpacked > _SKILL_ARCHIVE_MAX_UNPACKED or len(entries) >= 256:
                    raise ValueError("技能压缩包解压总量超过预算")
                entries[rel] = data
    except (tarfile.TarError, EOFError, OSError):
        raise ValueError("TAR 技能压缩包已损坏或格式无效") from None
    return entries


def _unquote_yaml(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            parsed = json.loads(value)
            if isinstance(parsed, str):
                return parsed.strip()
        except ValueError:
            return value[1:-1].strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].strip()
    return value


def parse_skill_meta(skill_id: str, content: str) -> dict:
    """提取 SKILL.md 的 frontmatter 元信息与摘要。"""
    text = str(content or "").replace("\r\n", "\n")
    fm_match = re.match(r"^\s*---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)", text)
    fields: dict[str, str] = {}
    if fm_match:
        for line in fm_match.group(1).splitlines():
            pair = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$", line)
            if pair:
                fields[pair.group(1).lower()] = _unquote_yaml(pair.group(2))
        body = text[fm_match.end() :]
    else:
        body = text

    heading_match = re.search(r"^\s*#{1,3}\s+(.+?)\s*$", body, flags=re.MULTILINE)
    heading = heading_match.group(1).strip() if heading_match else ""
    name = fields.get("name") or skill_id
    title = fields.get("title") or heading or name or skill_id
    description = fields.get("description") or ""
    if not description:
        for raw_line in body.splitlines():
            clean = raw_line.strip()
            if (
                not clean
                or clean.startswith("#")
                or clean == "---"
                or clean.startswith("<!--")
            ):
                continue
            clean = re.sub(r"^(?:[-*+]|\d+\.)\s+", "", clean)
            clean = clean.replace("**", "").strip()
            if clean:
                description = clean[:160]
                break
    return {
        "name": name,
        "title": title,
        "description": description[:160],
        "has_frontmatter": bool(fm_match),
    }


class MemoryLayers:
    def __init__(
        self,
        root,
        store,
        profiles,
        vault,
        *,
        clock=time.time,
        tz_offset_minutes: int = 480,
    ):
        self.tz_offset_minutes = _validate_tz_offset_minutes(tz_offset_minutes)
        self.root, self.store, self.profiles, self.vault, self.clock = (
            root,
            store,
            profiles,
            vault,
            clock,
        )
        self.chat_aliases = None
        self._r_lock = threading.RLock()
        self._lock_depth = threading.local()
        with store.connect() as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS memory_versions (asset TEXT,version INTEGER,kind TEXT,target TEXT,chat_id TEXT,document TEXT,content TEXT,author TEXT,updated REAL,PRIMARY KEY(asset,version))"
            )
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(asset UNINDEXED,content)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS learning_receipts (thread_id TEXT,revision INTEGER,status TEXT,PRIMARY KEY(thread_id,revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS experience_receipts (thread_id TEXT,revision INTEGER,PRIMARY KEY(thread_id,revision))"
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS skill_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    profile_id TEXT NOT NULL DEFAULT 'default',
                    skill_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    content TEXT NOT NULL,
                    signals TEXT NOT NULL DEFAULT '[]',
                    rationale TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
        _ensure_managed_dir(root, root / "memory/self")
        heartbeat_preset = PRESETS_DIR / "memory/HEARTBEAT.md"
        for kind in ("journal", "heartbeat"):
            path = self._locate(kind)[1]
            if not path.exists() and not path_is_link_like(path):
                initial = (
                    heartbeat_preset.read_bytes()
                    if kind == "heartbeat" and heartbeat_preset.is_file()
                    else b""
                )
                atomic_private_write(path, initial)
        _HISTORICAL_HEARTBEAT_HASHES = frozenset(
            {
                "135cfc4e35db5f453945125b1c2cea5ff596c73937d57e24b41d6cd4ec8b6ffd",
            }
        )

        def _sync_unedited_preset(
            asset: str,
            destination: Path,
            preset_bytes: bytes,
            *,
            max_bytes: int = 131072,
            known_hashes: frozenset[str] | None = None,
        ) -> bool:
            if not destination.is_file() or path_is_link_like(destination):
                return False
            with store.connect() as db:
                edited = db.execute(
                    "SELECT 1 FROM memory_versions WHERE asset=? AND version >= 1 LIMIT 1",
                    (asset,),
                ).fetchone()
            if edited:
                return False
            try:
                current_bytes = self._bytes(destination, max_bytes=max_bytes)
            except ValueError:
                return False
            if known_hashes is not None:
                if (
                    hashlib.sha256(current_bytes.replace(b"\r\n", b"\n")).hexdigest()
                    not in known_hashes
                ):
                    return False
            if current_bytes == preset_bytes:
                return False
            atomic_private_write(destination, preset_bytes)
            preset_text = preset_bytes.decode("utf-8", errors="replace")
            with store.connect() as db, db:
                has_v0 = db.execute(
                    "SELECT 1 FROM memory_versions WHERE asset=? AND version=0 LIMIT 1",
                    (asset,),
                ).fetchone()
                if has_v0 is not None:
                    db.execute(
                        "UPDATE memory_versions SET content=?, updated=? WHERE asset=? AND version=0",
                        (preset_text, self.clock(), asset),
                    )
                    db.execute("DELETE FROM memory_search WHERE asset=?", (asset,))
                    db.execute(
                        "INSERT INTO memory_search VALUES (?,?)",
                        (asset, preset_text),
                    )
            return True

        if heartbeat_preset.is_file() and not path_is_link_like(heartbeat_preset):
            _sync_unedited_preset(
                "heartbeat:self:HEARTBEAT.md",
                self._locate("heartbeat")[1],
                heartbeat_preset.read_bytes(),
                known_hashes=_HISTORICAL_HEARTBEAT_HASHES,
            )

        skills_presets = PRESETS_DIR / "skills"
        if skills_presets.is_dir() and not path_is_link_like(skills_presets):
            for source_dir in sorted(skills_presets.iterdir()):
                if not source_dir.is_dir() or path_is_link_like(source_dir):
                    continue
                skill_file = source_dir / "SKILL.md"
                if not skill_file.is_file() or path_is_link_like(skill_file):
                    continue
                destination_dir = _ensure_managed_dir(
                    root, root / "abilities/skills" / source_dir.name
                )
                destination = destination_dir / "SKILL.md"
                preset_bytes = skill_file.read_bytes()
                skill_asset = f"skill:{source_dir.name}:SKILL.md"
                if not destination.exists() and not path_is_link_like(destination):
                    atomic_private_write(destination, preset_bytes)
                else:
                    _sync_unedited_preset(
                        skill_asset,
                        destination,
                        preset_bytes,
                        max_bytes=_SKILL_FILE_MAX_BYTES,
                    )
                with store.connect() as db:
                    skill_edited = bool(
                        db.execute(
                            "SELECT 1 FROM memory_versions WHERE asset=? AND version >= 1 LIMIT 1",
                            (skill_asset,),
                        ).fetchone()
                    )
                for extra in sorted(source_dir.rglob("*")):
                    if (
                        extra.name == ".gitkeep"
                        or extra == skill_file
                        or extra.is_dir()
                        or path_is_link_like(extra)
                    ):
                        continue
                    rel = extra.relative_to(source_dir)
                    if _has_relative_link(source_dir, rel):
                        continue
                    extra_dst = destination_dir / rel
                    extra_parent = _ensure_managed_dir(destination_dir, extra_dst.parent)
                    extra_target = extra_parent / extra_dst.name
                    if path_is_link_like(extra_target):
                        continue
                    extra_bytes = extra.read_bytes()
                    if not extra_target.exists():
                        atomic_private_write(extra_target, extra_bytes)
                    elif not skill_edited and extra_target.is_file():
                        try:
                            if (
                                self._bytes(
                                    extra_target, max_bytes=_SKILL_FILE_MAX_BYTES
                                )
                                != extra_bytes
                            ):
                                atomic_private_write(extra_target, extra_bytes)
                        except ValueError:
                            pass
        profiles_presets = PRESETS_DIR / "profiles"
        if profiles_presets.is_dir() and not path_is_link_like(profiles_presets):
            for source_dir in sorted(profiles_presets.iterdir()):
                if not source_dir.is_dir() or path_is_link_like(source_dir):
                    continue
                profile_preset = source_dir / "PROFILE.md"
                if not profile_preset.is_file() or path_is_link_like(profile_preset):
                    continue
                _sync_unedited_preset(
                    f"profile:{source_dir.name}:PROFILE.md",
                    self.profiles.path / source_dir.name / "PROFILE.md",
                    profile_preset.read_bytes(),
                )

    @staticmethod
    def heartbeat_parts(content: str) -> tuple[str, str]:
        cleaned = re.sub(
            r"<!--[\s\S]*?-->",
            "",
            str(content or "").replace("\r\n", "\n"),
        )
        match = re.search(r"^##\s*自主探索方向\s*$", cleaned, flags=re.MULTILINE)
        if match is None:
            return "", cleaned.strip()
        before = re.sub(
            r"^##\s*气质与分寸\s*$", "", cleaned[: match.start()], flags=re.MULTILINE
        ).strip()
        after = cleaned[match.end() :]
        next_heading = re.search(r"^##\s+", after, flags=re.MULTILINE)
        if next_heading is not None:
            after = after[: next_heading.start()]
        return before, after.strip()

    @contextmanager
    def _lock(self):
        with self._r_lock:
            depth = getattr(self._lock_depth, "count", 0)
            self._lock_depth.count = depth + 1
            try:
                if depth == 0:
                    with file_lock(self.root / ".memory.lock"):
                        yield
                else:
                    yield
            finally:
                self._lock_depth.count = depth

    def _memory_path_for(self, chat_id: str) -> tuple[str, Path]:
        target = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:32]
        return target, self.root / "memory/chats" / target / "MEMORY.md"

    def _has_memory_data(self, chat_id: str, *, db=None) -> bool:
        if not isinstance(chat_id, str) or not chat_id or len(chat_id) > 256:
            return False
        target, path = self._memory_path_for(chat_id)
        if path.is_file() and not path_is_link_like(path):
            try:
                if path.stat().st_size > 0:
                    return True
            except OSError:
                pass
        asset = f"memory:{target}:MEMORY.md"
        if db is not None:
            row = db.execute(
                "SELECT content FROM memory_versions WHERE asset=? ORDER BY version DESC LIMIT 1",
                (asset,),
            ).fetchone()
        else:
            with self.store.connect() as conn:
                row = conn.execute(
                    "SELECT content FROM memory_versions WHERE asset=? ORDER BY version DESC LIMIT 1",
                    (asset,),
                ).fetchone()
        return bool(row and str(row["content"] or ""))

    def _memory_alias_ids(self, chat_id: str) -> list[str]:
        if not isinstance(chat_id, str) or not chat_id or len(chat_id) > 256:
            return []
        candidates = [chat_id]
        if callable(self.chat_aliases):
            try:
                aliases = sorted(set(self.chat_aliases(chat_id)) - {chat_id})
            except (TypeError, ValueError):
                aliases = []
            for alias in aliases:
                if isinstance(alias, str) and alias and len(alias) <= 256:
                    candidates.append(alias)
        return candidates

    def _resolve_memory_chat_id(self, chat_id: str, *, db=None) -> str:
        candidates = self._memory_alias_ids(chat_id)
        if not candidates:
            return chat_id
        existing = [cid for cid in candidates if self._has_memory_data(cid, db=db)]
        if not existing:
            return chat_id
        if len(existing) == 1:
            return existing[0]

        asset_to_cid: dict[str, str] = {}
        for cid in existing:
            target, _ = self._memory_path_for(cid)
            asset_to_cid.setdefault(f"memory:{target}:MEMORY.md", cid)
        assets = list(asset_to_cid.keys())
        placeholders = ",".join("?" for _ in assets)
        sql = (
            "SELECT asset FROM memory_versions "
            f"WHERE kind='memory' AND asset IN ({placeholders}) "
            "ORDER BY updated DESC, version DESC LIMIT 1"
        )
        if db is not None:
            row = db.execute(sql, assets).fetchone()
        else:
            with self.store.connect() as conn:
                row = conn.execute(sql, assets).fetchone()
        if row is not None and row["asset"] in asset_to_cid:
            return asset_to_cid[row["asset"]]

        def _mtime_key(cid: str) -> tuple[int, int]:
            _, path = self._memory_path_for(cid)
            mtime_ns = -1
            if path.is_file() and not path_is_link_like(path):
                try:
                    mtime_ns = path.stat().st_mtime_ns
                except OSError:
                    mtime_ns = -1
            return (mtime_ns, 1 if cid == chat_id else 0)

        return max(existing, key=_mtime_key)

    def _locate(
        self, kind, target="", chat_id="", document="", *, db=None, resolve_alias=True
    ):
        if kind in {"profile", "notes", "role_memory", "skill"}:
            if not isinstance(target, str) or not SKILL_ID_RE.fullmatch(target):
                raise ValueError("角色或技能编号无效")
            if kind == "profile":
                document = document or "PROFILE.md"
                if document not in {"PROFILE.md", "profile.toml"}:
                    raise ValueError("角色文档无效")
                directory = self.profiles.path / target
                if not directory.is_dir():
                    raise ValueError("角色不存在，请先克隆创建")
            elif kind == "notes":
                self.profiles.get(target)
                directory, document = self.profiles.path / target, "NOTES.md"
            elif kind == "role_memory":
                self.profiles.get(target)
                directory, document = self.profiles.path / target, "MEMORY.md"
            else:
                directory, document = (
                    self.root / "abilities/skills" / target,
                    "SKILL.md",
                )
        elif kind == "memory":
            if not isinstance(chat_id, str) or not chat_id or len(chat_id) > 256:
                raise ValueError("会话记忆需要原会话")
            if resolve_alias:
                chat_id = self._resolve_memory_chat_id(chat_id, db=db)
            target = hashlib.sha256(chat_id.encode()).hexdigest()[:32]
            directory, document = self.root / "memory/chats" / target, "MEMORY.md"
        elif kind in {"journal", "heartbeat"}:
            target, document = (
                "self",
                "JOURNAL.md" if kind == "journal" else "HEARTBEAT.md",
            )
            directory = self.root / "memory/self"
        else:
            raise ValueError("未知记忆层")
        base_root = (
            self.profiles.path
            if kind in {"profile", "notes", "role_memory"}
            else self.root
        )
        _ensure_managed_dir(base_root, directory)
        return f"{kind}:{target}:{document}", directory / document, document, target

    @staticmethod
    def _bytes(path, *, max_bytes: int = 131072):
        if has_dir_fd():
            try:
                descriptor = os.open(
                    path,
                    os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                )
            except FileNotFoundError:
                return b""
        else:
            try:
                descriptor = open_read_fd(path)
            except FileNotFoundError:
                return b""
            except ValueError:
                raise ValueError("记忆文件无效或过大") from None
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            invalid_nlink = info.st_nlink > 1 or (
                os.name != "nt" and info.st_nlink != 1
            )
            if (
                not stat.S_ISREG(info.st_mode)
                or invalid_nlink
                or info.st_size > max_bytes
            ):
                raise ValueError("记忆文件无效或过大")
            data = source.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("记忆文件无效或过大")
            return data

    LIMITS = {
        "memory": 16000,
        "role_memory": 8000,
        "journal": 8000,
        "notes": 16000,
        "heartbeat": 16000,
    }

    def _guard(self, kind, content, chat_id, document="", *, allow_custom=False):
        if not isinstance(content, str):
            raise TypeError("记忆内容必须为文本")
        maximum = self.LIMITS.get(kind, 65536)
        if len(content) > maximum or "\x00" in content:
            raise ValueError("记忆内容超过长度限制")
        if self.vault.contains_secret(content.encode()) or re.search(
            r"-----BEGIN .*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b",
            content,
        ):
            raise ValueError("内容包含凭据，已拒绝保存")
        if (
            allow_custom
            or kind in {"memory", "role_memory"}
            or kind == "profile"
            and document == "profile.toml"
        ):
            return
        if re.search(
            r"本群|群名|群号|群友称呼|业务抬头|资产代号|工作区代号|服务器地址|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b\d{8,}\b|\b(?:\d{1,3}\.){3}\d{1,3}\b|/(?:root|Users|home)/",
            content,
        ):
            raise ValueError("具体会话或个人信息应写入本会话 MEMORY.md")
        if chat_id:
            with self.store.connect() as db:
                identifiers = [
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT user_id FROM agent_threads WHERE chat_id=?",
                        (chat_id,),
                    )
                ]
                identifiers += [
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM agent_workspaces WHERE chat_id=?", (chat_id,)
                    )
                ]
            if any(
                isinstance(identifier, str)
                and len(identifier) >= 3
                and identifier in content
                for identifier in [chat_id, *identifiers]
            ):
                raise ValueError("内容包含会话、发言人或工作区标识")
            path = self._locate("memory", chat_id=chat_id)[1]
            private = self._bytes(path).decode("utf-8")
            fragments = re.split(r"[\n:：=，,。；;]", private)
            if any(
                2 <= len(fragment.strip()) <= 80 and fragment.strip() in content
                for fragment in fragments
            ):
                raise ValueError("内容包含本会话信息，应写入本会话 MEMORY.md")

    def _record(
        self, db, asset, version, kind, target, chat_id, document, content, author
    ):
        db.execute(
            "INSERT INTO memory_versions VALUES (?,?,?,?,?,?,?,?,?)",
            (
                asset,
                version,
                kind,
                target,
                chat_id if kind == "memory" else "",
                document,
                content,
                author,
                self.clock(),
            ),
        )
        db.execute("DELETE FROM memory_search WHERE asset=?", (asset,))
        db.execute("INSERT INTO memory_search VALUES (?,?)", (asset, content))

    def _sync(
        self,
        db,
        kind,
        target,
        chat_id,
        document,
        *,
        allow_custom=False,
        resolve_alias=True,
    ):
        if kind == "memory" and isinstance(chat_id, str) and chat_id and resolve_alias:
            chat_id = self._resolve_memory_chat_id(chat_id, db=db)
        asset, path, document, target = self._locate(
            kind, target, chat_id, document, db=db, resolve_alias=resolve_alias
        )
        max_bytes = _SKILL_FILE_MAX_BYTES if kind == "skill" else 131072
        content = self._bytes(path, max_bytes=max_bytes).decode("utf-8")
        old = db.execute(
            "SELECT * FROM memory_versions WHERE asset=? ORDER BY version DESC LIMIT 1",
            (asset,),
        ).fetchone()
        self._guard(
            kind,
            content,
            chat_id,
            document,
            allow_custom=allow_custom or (old is not None and old["content"] == content),
        )
        if old is None or old["content"] != content:
            version = old["version"] + 1 if old else 0
            self._record(
                db,
                asset,
                version,
                kind,
                target,
                chat_id,
                document,
                content,
                "external" if old else "initial",
            )
        else:
            version = old["version"]
        return (
            {
                "kind": kind,
                "target": target,
                "document": document,
                "content": content,
                "version": version,
            },
            asset,
            path,
        )

    @staticmethod
    def _merge_memory_contents(contents: list[str], limit: int) -> str:
        seen_lines: set[str] = set()
        merged_lines: list[str] = []
        for raw in contents:
            text = str(raw or "").strip()
            if not text:
                continue
            block_added = False
            for line in text.splitlines():
                norm = line.strip()
                if not norm:
                    if merged_lines and merged_lines[-1] != "":
                        merged_lines.append("")
                    continue
                if norm in seen_lines:
                    continue
                seen_lines.add(norm)
                merged_lines.append(line.rstrip())
                block_added = True
            if block_added and merged_lines and merged_lines[-1] != "":
                merged_lines.append("")
        merged = "\n".join(merged_lines).strip()
        if len(merged) > limit:
            merged = merged[:limit].rstrip()
        return merged

    def _alias_recency_key(self, db, cid: str, asset: str) -> tuple[float, int]:
        row = db.execute(
            "SELECT updated FROM memory_versions WHERE asset=? ORDER BY version DESC LIMIT 1",
            (asset,),
        ).fetchone()
        updated = float(row["updated"]) if row and row["updated"] is not None else -1.0
        _, path = self._memory_path_for(cid)
        mtime_ns = -1
        if path.is_file() and not path_is_link_like(path):
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                mtime_ns = -1
        return (updated, mtime_ns)

    def read(
        self,
        kind,
        *,
        target="",
        chat_id="",
        document="",
        version=None,
        allow_custom=False,
    ):
        with self._lock(), self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            current, asset, _path = self._sync(
                db, kind, target, chat_id, document, allow_custom=allow_custom
            )
            if version is not None:
                old = db.execute(
                    "SELECT content,version FROM memory_versions WHERE asset=? AND version=?",
                    (asset, version),
                ).fetchone()
                if not old:
                    raise ValueError("历史版本不存在")
                current.update(dict(old))
                return current
            if kind == "memory" and isinstance(chat_id, str) and chat_id:
                existing = [
                    cid
                    for cid in self._memory_alias_ids(chat_id)
                    if self._has_memory_data(cid, db=db)
                ]
                if len(existing) > 1:
                    other_entries: list[tuple[tuple[float, int], str]] = []
                    for cid in existing:
                        other_target, _ = self._memory_path_for(cid)
                        other_asset = f"memory:{other_target}:MEMORY.md"
                        if other_asset == asset:
                            continue
                        other_doc, synced_asset, _ = self._sync(
                            db,
                            "memory",
                            "",
                            cid,
                            "",
                            allow_custom=allow_custom,
                            resolve_alias=False,
                        )
                        if other_doc.get("content"):
                            other_entries.append(
                                (
                                    self._alias_recency_key(db, cid, synced_asset),
                                    other_doc["content"],
                                )
                            )
                    if other_entries:
                        other_entries.sort(key=lambda item: item[0], reverse=True)
                        merged = self._merge_memory_contents(
                            [current["content"], *(text for _, text in other_entries)],
                            self.LIMITS["memory"],
                        )
                        current = {**current, "content": merged}
            return current

    def preset_skills(self) -> set[str]:
        skills_presets = PRESETS_DIR / "skills"
        if not skills_presets.is_dir():
            return set()
        return {
            p.name
            for p in skills_presets.iterdir()
            if p.is_dir()
            and not path_is_link_like(p)
            and (p / "SKILL.md").is_file()
            and not path_is_link_like(p / "SKILL.md")
        }

    def skills(self):
        root = self.root / "abilities/skills"
        if not root.is_dir():
            return []
        return sorted(
            p.name for p in root.iterdir() if p.is_dir() and not path_is_link_like(p)
        )

    def skill_briefs(self, skill_ids, *, profile_id: str = "") -> list[dict]:
        briefs = []
        for skill_id in skill_ids or ():
            if not isinstance(skill_id, str) or not SKILL_ID_RE.fullmatch(skill_id):
                continue
            candidates = [self.root / "abilities/skills" / skill_id / "SKILL.md"]
            if profile_id and SKILL_ID_RE.fullmatch(profile_id):
                candidates.append(
                    self.profiles.path / profile_id / "preset/skills" / skill_id / "SKILL.md"
                )
            raw_text = ""
            for cand in candidates:
                raw = self._bytes(cand, max_bytes=_SKILL_FILE_MAX_BYTES)
                if raw:
                    raw_text = raw.decode("utf-8", errors="replace")
                    break
            meta = parse_skill_meta(skill_id, raw_text) if raw_text else {
                "title": skill_id,
                "description": "",
            }
            summary = self.vault.redact(
                (meta.get("description") or meta.get("title") or skill_id)[:72]
            )
            briefs.append({"id": skill_id, "summary": summary})
        return briefs

    def create_skill(
        self,
        target: str,
        *,
        base: str = "",
        title: str = "",
        description: str = "",
        content: str = "",
        allow_custom: bool = True,
    ):
        if not isinstance(target, str) or not SKILL_ID_RE.fullmatch(target):
            raise ValueError("技能编号无效")
        skill_path = self.root / "abilities/skills" / target / "SKILL.md"
        if path_is_link_like(skill_path):
            raise ValueError("技能路径无效")
        if skill_path.exists() and self._bytes(skill_path).strip():
            raise ValueError("同名技能已存在")
        if content and content.strip():
            body = content
        elif base and base.strip():
            if not isinstance(base, str) or not SKILL_ID_RE.fullmatch(base):
                raise ValueError("源技能编号无效")
            src_path = self.root / "abilities/skills" / base / "SKILL.md"
            if path_is_link_like(src_path) or not src_path.is_file():
                raise ValueError("源技能不存在")
            src = self.read("skill", target=base)["content"]
            fm_match = re.match(r"^(\s*---\r?\n)([\s\S]*?)(\r?\n---(?:\r?\n|$))", src)
            if fm_match:
                fm_body = fm_match.group(2)
                if re.search(r"(?m)^\s*name\s*:", fm_body):
                    fm_body = re.sub(
                        r"(?m)^\s*name\s*:.*$", f"name: {target}", fm_body, count=1
                    )
                else:
                    fm_body = f"name: {target}\n{fm_body}"
                body = fm_match.group(1) + fm_body + fm_match.group(3) + src[fm_match.end() :]
            else:
                desc = (description or f"{title or target}（复制自 {base}）").strip()
                body = (
                    f"---\nname: {target}\n"
                    f"description: {json.dumps(desc, ensure_ascii=False)}\n---\n\n{src}"
                )
        else:
            heading = (title or target).strip()
            desc = (description or f"{heading}：说明本技能处理的场景、步骤与核验边界。").strip()
            body = (
                f"---\nname: {target}\n"
                f"description: {json.dumps(desc, ensure_ascii=False)}\n---\n\n"
                f"# {heading}\n\n"
                "## 适用场景\n- 说明在什么需求或信号下启用本技能。\n\n"
                "## 执行步骤\n1. 先核实输入材料或调用只读检索工具。\n"
                "2. 按步骤产出结构化结论或工作区文件。\n\n"
                "## 质量边界\n- 仅引用真实工具返回的数据与链接，不编造结论。\n"
            )
        if base and base.strip():
            companions = self.read_skill_companions(base)
            dest_root = _ensure_managed_dir(
                self.root, self.root / "abilities/skills" / target
            )
            for rel_posix, raw_data in companions:
                dest_file = dest_root / rel_posix
                _ensure_managed_dir(dest_root, dest_file.parent)
                atomic_private_write(dest_file, raw_data)
        current = self.read("skill", target=target)
        return self.write(
            "skill",
            body,
            target=target,
            expected_version=current["version"],
            allow_custom=allow_custom,
        )

    @staticmethod
    def _validate_companion_rel(rel_path: str) -> str:
        if not isinstance(rel_path, str) or "\\" in rel_path or "\x00" in rel_path:
            raise ValueError("技能附属文件路径无效")
        clean = rel_path.strip("/")
        parts = clean.split("/")
        if (
            not clean
            or len(parts) > 6
            or clean.upper() == "SKILL.MD"
            or any(
                part in {"", ".", ".."}
                or part.startswith(".")
                or not _COMPANION_SEG_RE.fullmatch(part)
                for part in parts
            )
        ):
            raise ValueError(f"技能附属文件路径不合法: {rel_path}")
        for part in parts:
            check_part(part)
        return "/".join(parts)

    def _clear_skill_companions(self, skill_dir: Path):
        if not skill_dir.is_dir() or path_is_link_like(skill_dir):
            return
        for child in list(skill_dir.iterdir()):
            if child.name == "SKILL.md":
                continue
            if path_is_link_like(child) or child.is_file():
                child.unlink()
            elif child.is_dir():
                rmtree_safe(child)

    def _collect_skill_companions(self, directory: Path) -> list[tuple[str, bytes]]:
        if not directory.is_dir() or path_is_link_like(directory):
            return []
        collected: list[tuple[str, bytes]] = []
        total = 0
        for root_dir, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".")
                and d not in _ARCHIVE_JUNK_PARTS
                and not path_is_link_like(Path(root_dir) / d)
            )
            for name in sorted(files):
                if name.startswith(".") or name in _ARCHIVE_JUNK_FILES:
                    continue
                full = Path(root_dir) / name
                rel = full.relative_to(directory).as_posix()
                if rel == "SKILL.md":
                    continue
                if path_is_link_like(full):
                    continue
                try:
                    rel_clean = self._validate_companion_rel(rel)
                except ValueError:
                    continue
                if has_dir_fd():
                    fd = os.open(
                        full,
                        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                    )
                else:
                    try:
                        fd = open_read_fd(full)
                    except (ValueError, OSError):
                        continue
                with os.fdopen(fd, "rb") as src:
                    info = os.fstat(src.fileno())
                    invalid_nlink = info.st_nlink > 1 or (
                        os.name != "nt" and info.st_nlink != 1
                    )
                    if not stat.S_ISREG(info.st_mode) or invalid_nlink:
                        continue
                    if info.st_size > _SKILL_FILE_MAX_BYTES:
                        continue
                    data = src.read(_SKILL_FILE_MAX_BYTES + 1)
                if len(data) > _SKILL_FILE_MAX_BYTES:
                    continue
                total += len(data)
                if len(collected) >= _SKILL_MAX_FILES or total > 4 * 1024 * 1024:
                    break
                collected.append((rel_clean, data))
        return collected

    def _resolve_preset_skill_dir(
        self, target: str, *, profile_id: str = "", rel_path: str = ""
    ) -> Path | None:
        if profile_id and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", profile_id):
            preset_dir = self.profiles.path / profile_id / "preset/skills" / target
            if path_is_link_like(preset_dir) or (
                preset_dir.exists()
                and _has_relative_link(self.profiles.path, preset_dir)
            ):
                raise ValueError("技能路径不能经过链接")
            if (preset_dir / "SKILL.md").is_file() or (rel_path and preset_dir.is_dir()):
                if (preset_dir / "SKILL.md").exists() and _has_relative_link(
                    self.profiles.path, preset_dir / "SKILL.md"
                ):
                    raise ValueError("技能路径不能经过链接")
                return preset_dir
            return None
        if not self.profiles.path.is_dir():
            return None
        fallback_match: Path | None = None
        for prof_dir in sorted(self.profiles.path.iterdir()):
            if (
                path_is_link_like(prof_dir)
                or not prof_dir.is_dir()
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", prof_dir.name)
            ):
                continue
            cand = prof_dir / "preset/skills" / target
            if path_is_link_like(cand) or (
                cand.exists() and _has_relative_link(self.profiles.path, cand)
            ):
                raise ValueError("技能路径不能经过链接")
            if not (cand / "SKILL.md").is_file() and not (rel_path and cand.is_dir()):
                continue
            if (cand / "SKILL.md").exists() and _has_relative_link(
                self.profiles.path, cand / "SKILL.md"
            ):
                raise ValueError("技能路径不能经过链接")
            if rel_path and (cand / rel_path).exists():
                return cand
            if fallback_match is None:
                fallback_match = cand
        return fallback_match

    def read_skill_companions(
        self, target: str, *, profile_id: str = ""
    ) -> list[tuple[str, bytes]]:
        if not isinstance(target, str) or not SKILL_ID_RE.fullmatch(target):
            raise ValueError("技能编号无效")
        skill_dir = self.root / "abilities/skills" / target
        if not (skill_dir / "SKILL.md").is_file():
            preset_dir = self._resolve_preset_skill_dir(
                target, profile_id=profile_id
            )
            if preset_dir is not None:
                skill_dir = preset_dir
        return self._collect_skill_companions(skill_dir)

    def skill_files(self, target: str, *, profile_id: str = "") -> list[dict]:
        return [
            {"path": rel, "bytes": len(data)}
            for rel, data in self.read_skill_companions(target, profile_id=profile_id)
        ]

    def read_skill_file(
        self, target: str, rel_path: str, *, profile_id: str = ""
    ) -> dict:
        if not isinstance(target, str) or not SKILL_ID_RE.fullmatch(target):
            raise ValueError("技能编号无效")
        rel = self._validate_companion_rel(rel_path)
        skill_dir = self.root / "abilities/skills" / target
        if path_is_link_like(skill_dir):
            raise ValueError("技能不存在")
        if skill_dir.is_dir() and (
            (skill_dir / "SKILL.md").is_file() or (skill_dir / rel).exists()
        ):
            if _has_relative_link(self.root, skill_dir):
                raise ValueError("技能不存在")
        else:
            if skill_dir.is_dir() and _has_relative_link(self.root, skill_dir):
                raise ValueError("技能不存在")
            preset_dir = self._resolve_preset_skill_dir(
                target, profile_id=profile_id, rel_path=rel
            )
            if preset_dir is None or path_is_link_like(preset_dir) or not preset_dir.is_dir():
                raise ValueError("技能不存在")
            skill_dir = preset_dir
        parts = rel.split("/")
        if has_dir_fd():
            cur_fd = os.open(skill_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for seg in parts[:-1]:
                    next_fd = os.open(
                        seg, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cur_fd
                    )
                    os.close(cur_fd)
                    cur_fd = next_fd
                file_fd = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=cur_fd,
                )
            except FileNotFoundError:
                raise ValueError("技能附属文件不存在") from None
            except OSError:
                raise ValueError("无法读取技能附属文件") from None
            finally:
                os.close(cur_fd)
        else:
            try:
                parent = ensure_dirs(skill_dir, parts[:-1], create=False)
                file_fd = open_read_fd(parent / parts[-1], rel)
                try:
                    ensure_dirs(skill_dir, parts[:-1], create=False)
                except BaseException:
                    os.close(file_fd)
                    raise
            except FileNotFoundError:
                raise ValueError("技能附属文件不存在") from None
            except ValueError:
                raise ValueError("无法读取技能附属文件") from None
            except OSError:
                raise ValueError("无法读取技能附属文件") from None
        with os.fdopen(file_fd, "rb") as src:
            info = os.fstat(src.fileno())
            invalid_nlink = info.st_nlink > 1 or (
                os.name != "nt" and info.st_nlink != 1
            )
            if not stat.S_ISREG(info.st_mode) or invalid_nlink:
                raise ValueError("技能附属文件无效")
            sample = src.read(32768)
            size = info.st_size
        if self.vault.contains_secret(sample) or _SECRET_RE.search(
            sample.decode("utf-8", errors="ignore")
        ):
            raise ValueError("技能附属文件包含凭据，已拒绝读取")
        is_binary = b"\x00" in sample
        content = ""
        if not is_binary:
            try:
                content = sample.decode("utf-8")
            except UnicodeDecodeError:
                is_binary = True
        return {
            "target": target,
            "path": rel,
            "bytes": size,
            "is_binary": is_binary,
            "content": content,
            "truncated": size > len(sample),
        }

    def _guard_companion_bytes(self, rel: str, data: bytes):
        if len(data) > _SKILL_FILE_MAX_BYTES:
            raise ValueError(f"技能附属文件过大: {rel}")
        if self.vault.contains_secret(data):
            raise ValueError(f"技能附属文件包含已登记凭据，已拒绝导入: {rel}")
        try:
            as_text = data.decode("utf-8")
        except UnicodeDecodeError:
            as_text = ""
        if as_text and _SECRET_RE.search(as_text):
            raise ValueError(f"技能附属文件包含密钥特征，已拒绝导入: {rel}")

    def import_skill_archive(
        self,
        raw_bytes: bytes,
        *,
        target: str = "",
        filename: str = "",
        overwrite: bool = True,
    ) -> dict:
        entries = _extract_skill_archive_entries(raw_bytes, filename=filename)
        skill_md_entries: dict[str, str] = {}
        for rel in sorted(entries):
            p = PurePosixPath(rel)
            if p.name.lower() == "skill.md":
                parent = "" if len(p.parts) == 1 else p.parent.as_posix()
                skill_md_entries.setdefault(parent, rel)

        if not skill_md_entries:
            if len(entries) == 1:
                only_rel, only_bytes = next(iter(entries.items()))
                if only_rel.lower().endswith((".md", ".txt")):
                    entries = {"SKILL.md": only_bytes}
                    skill_md_entries = {"": "SKILL.md"}
                else:
                    raise ValueError("压缩包内未找到 SKILL.md")
            else:
                raise ValueError("压缩包内未找到 SKILL.md")

        # Keep only non-nested top skill roots; nested examples/*/SKILL.md remain companions.
        sorted_roots = sorted(
            skill_md_entries.keys(),
            key=lambda r: (0 if not r else len(PurePosixPath(r).parts), r),
        )
        top_roots: list[str] = []
        for root_dir in sorted_roots:
            if any(
                anc == "" or root_dir == anc or root_dir.startswith(anc + "/")
                for anc in top_roots
            ):
                continue
            top_roots.append(root_dir)

        if len(top_roots) > _SKILL_MAX_BUNDLE:
            raise ValueError(f"单个压缩包最多批量导入 {_SKILL_MAX_BUNDLE} 个技能")

        bundles: list[tuple[str, str, dict[str, bytes]]] = []
        requested_target = target.strip()
        if requested_target and not SKILL_ID_RE.fullmatch(requested_target):
            raise ValueError("技能编号无效")

        for root_dir in top_roots:
            md_rel = skill_md_entries[root_dir]
            try:
                md_text = entries[md_rel].decode("utf-8").lstrip("\ufeff")
            except UnicodeDecodeError:
                raise ValueError(f"SKILL.md 必须为 UTF-8 文本: {md_rel}") from None
            if not md_text.strip():
                raise ValueError(f"SKILL.md 内容为空: {md_rel}")
            meta = parse_skill_meta(
                PurePosixPath(root_dir).name if root_dir else "", md_text
            )
            prefix = f"{root_dir}/" if root_dir else ""
            companions: dict[str, bytes] = {}
            total_comp = 0
            for rel, data in entries.items():
                if rel == md_rel:
                    continue
                if prefix and not rel.startswith(prefix):
                    continue
                sub_rel = rel[len(prefix) :] if prefix else rel
                clean_sub = self._validate_companion_rel(sub_rel)
                self._guard_companion_bytes(clean_sub, data)
                companions[clean_sub] = data
                total_comp += len(data)
                if len(companions) > _SKILL_MAX_FILES or total_comp > 4 * 1024 * 1024:
                    raise ValueError("单个技能附属文件数量或大小超过预算")

            folder_slug = _slug_skill_id(PurePosixPath(root_dir).name) if root_dir else ""
            meta_slug = _slug_skill_id(meta.get("name", ""))
            file_slug = _slug_skill_id(filename)
            auto_id = folder_slug or meta_slug or file_slug

            if len(top_roots) == 1:
                skill_id = requested_target or auto_id
                if not skill_id:
                    raise ValueError("无法识别技能编号，请先填写「导入技能编号」")
            else:
                skill_id = auto_id
                if not skill_id:
                    raise ValueError(f"无法为压缩包内目录 {root_dir} 生成有效技能编号")
                if requested_target and requested_target not in {
                    skill_id,
                    PurePosixPath(root_dir).name if root_dir else "",
                }:
                    continue

            self._guard("skill", md_text, "", allow_custom=True)
            bundles.append((skill_id, md_text, companions))

        if requested_target and len(top_roots) > 1 and not bundles:
            raise ValueError(
                "压缩包内包含多个技能，请留空技能编号以批量导入，或填写包内对应的技能目录名"
            )

        seen_ids: set[str] = set()
        for skill_id, _, _ in bundles:
            if skill_id in seen_ids:
                raise ValueError(f"压缩包内存在重复技能编号: {skill_id}")
            seen_ids.add(skill_id)

        imported: list[dict] = []
        with self._lock():
            skills_root = _ensure_managed_dir(self.root, self.root / "abilities/skills")
            for skill_id, md_text, companions in bundles:
                dest_dir = skills_root / skill_id
                if path_is_link_like(dest_dir):
                    raise ValueError("技能路径无效")
                if not overwrite and dest_dir.exists() and self._bytes(dest_dir / "SKILL.md"):
                    raise ValueError(f"同名技能已存在: {skill_id}")
                stage = Path(tempfile.mkdtemp(prefix="skill-stage-", dir=skills_root))
                try:
                    for rel_sub, comp_bytes in companions.items():
                        target_file = stage / rel_sub
                        _ensure_managed_dir(stage, target_file.parent)
                        atomic_private_write(target_file, comp_bytes)
                    _ensure_managed_dir(skills_root, dest_dir)
                    self._clear_skill_companions(dest_dir)
                    for rel_sub, comp_bytes in companions.items():
                        target_file = dest_dir / rel_sub
                        _ensure_managed_dir(dest_dir, target_file.parent)
                        atomic_private_write(target_file, comp_bytes)
                finally:
                    if stage.exists():
                        rmtree_safe(stage)
                saved = self._write(
                    "skill",
                    md_text,
                    target=skill_id,
                    author="import",
                    allow_custom=True,
                )
                imported.append(
                    {
                        "id": skill_id,
                        "version": saved["version"],
                        "companions": len(companions),
                    }
                )
        return {
            "id": imported[0]["id"],
            "count": len(imported),
            "imported": imported,
        }

    def export_skill_archive(self, target: str) -> bytes:
        if not isinstance(target, str) or not SKILL_ID_RE.fullmatch(target):
            raise ValueError("技能编号无效")
        skill_dir = self.root / "abilities/skills" / target
        skill_file = skill_dir / "SKILL.md"
        if (
            path_is_link_like(skill_dir)
            or path_is_link_like(skill_file)
            or not skill_file.is_file()
        ):
            raise ValueError("技能不存在")
        main_bytes = self._bytes(skill_file, max_bytes=_SKILL_FILE_MAX_BYTES)
        if self.vault.contains_secret(main_bytes) or _SECRET_RE.search(
            main_bytes.decode("utf-8", errors="ignore")
        ):
            raise ValueError("技能包含凭据，禁止导出")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{target}/SKILL.md", main_bytes)
            for rel_sub, data in self.read_skill_companions(target):
                if self.vault.contains_secret(data):
                    raise ValueError("技能附属文件含凭据，禁止导出")
                archive.writestr(f"{target}/{rel_sub}", data)
        if buffer.tell() > _SKILL_ARCHIVE_MAX_BYTES:
            raise ValueError("技能包超过 8 MiB 导出上限")
        return buffer.getvalue()

    def reset_skill(self, target: str):
        if not isinstance(target, str) or not SKILL_ID_RE.fullmatch(target):
            raise ValueError("技能编号无效")
        preset_file = PRESETS_DIR / "skills" / target / "SKILL.md"
        if path_is_link_like(preset_file) or not preset_file.is_file():
            raise ValueError("仅内置预设技能支持重置")
        with self._lock():
            self._clear_skill_companions(self.root / "abilities/skills" / target)
        current = self.read("skill", target=target)
        return self.write(
            "skill",
            preset_file.read_text(encoding="utf-8"),
            target=target,
            expected_version=current["version"],
            author="reset",
            allow_custom=True,
        )

    def delete_skill(self, target: str):
        if not isinstance(target, str) or not SKILL_ID_RE.fullmatch(target):
            raise ValueError("技能编号无效")
        if target in self.preset_skills():
            raise ValueError("预设技能不可删除，可点击「重置预设」恢复默认内容")
        directory = self.root / "abilities/skills" / target
        if path_is_link_like(directory) or not directory.exists():
            raise ValueError("技能不存在")
        with self._lock(), self.store.connect() as db, db:
            rmtree_safe(directory)
            asset = f"skill:{target}:SKILL.md"
            db.execute("DELETE FROM memory_search WHERE asset=?", (asset,))
            db.execute("DELETE FROM memory_versions WHERE asset=?", (asset,))
        return {"deleted": True, "skill": target}

    def patch_skill(
        self,
        target: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
        author: str = "agent",
    ) -> dict:
        if not isinstance(target, str) or not SKILL_ID_RE.fullmatch(target):
            raise ValueError("技能编号无效")
        if not isinstance(old_string, str) or not old_string:
            raise ValueError("补丁替换需要非空 old_string")
        if not isinstance(new_string, str):
            raise ValueError("补丁替换需要字符串 new_string")
        with self._lock():
            cur = self.read("skill", target=target)
            body = str(cur.get("content") or "")
            count = body.count(old_string)
            if count == 0:
                raise ValueError("目标技能中未找到待替换文本片段，请先读取最新 SKILL.md")
            if count > 1 and not replace_all:
                raise ValueError(f"待替换片段在 SKILL.md 中出现 {count} 次，请提供更精确上下文或指定 replace_all")
            updated = (
                body.replace(old_string, new_string)
                if replace_all
                else body.replace(old_string, new_string, 1)
            )
            return self._write(
                "skill",
                updated,
                target=target,
                expected_version=cur.get("version"),
                author=author,
                allow_custom=True,
            )

    def write_skill_file(
        self,
        target: str,
        rel_path: str,
        content: str,
        *,
        author: str = "agent",
    ) -> dict:
        if not isinstance(target, str) or not SKILL_ID_RE.fullmatch(target):
            raise ValueError("技能编号无效")
        clean_rel = self._validate_companion_rel(rel_path)
        first_seg = clean_rel.split("/", 1)[0]
        if first_seg not in {"references", "templates", "scripts", "assets"}:
            raise ValueError("附属文件必须位于 references/、templates/、scripts/ 或 assets/ 目录下")
        if not isinstance(content, str):
            raise ValueError("附属文件内容必须为文本")
        raw_bytes = content.encode("utf-8")
        self._guard_companion_bytes(clean_rel, raw_bytes)
        with self._lock():
            if target not in set(self.skills()):
                raise ValueError(f"目标技能不存在: {target}")
            cur = self.read("skill", target=target)
            skills_root = _ensure_managed_dir(self.root, self.root / "abilities/skills")
            skill_dir = _ensure_managed_dir(skills_root, skills_root / target)
            if not (skill_dir / "SKILL.md").is_file() and cur.get("content"):
                atomic_private_write(skill_dir / "SKILL.md", str(cur["content"]).encode("utf-8"))
            existing_comps = dict(self.read_skill_companions(target))
            if clean_rel not in existing_comps and len(existing_comps) >= _SKILL_MAX_FILES:
                raise ValueError("该技能附属文件数量已达上限")
            target_file = skill_dir / clean_rel
            _ensure_managed_dir(skill_dir, target_file.parent)
            atomic_private_write(target_file, raw_bytes)
        return {"ok": True, "skill": target, "file_path": clean_rel, "bytes": len(raw_bytes)}

    def remove_skill_file(self, target: str, rel_path: str) -> dict:
        if not isinstance(target, str) or not SKILL_ID_RE.fullmatch(target):
            raise ValueError("技能编号无效")
        clean_rel = self._validate_companion_rel(rel_path)
        with self._lock():
            skill_dir = self.root / "abilities/skills" / target
            if path_is_link_like(skill_dir) or not skill_dir.is_dir():
                raise ValueError("目标技能本地附属目录不存在")
            target_file = skill_dir / clean_rel
            if path_is_link_like(target_file) or not target_file.is_file():
                raise ValueError(f"附属文件不存在: {clean_rel}")
            target_file.unlink()
        return {"ok": True, "skill": target, "removed": clean_rel}

    def seed_profile(self, target: str) -> dict[str, int]:
        versions = {
            "PROFILE.md": self.read(
                "profile", target=target, document="PROFILE.md", allow_custom=True
            )["version"],
            "profile.toml": self.read(
                "profile", target=target, document="profile.toml", allow_custom=True
            )["version"],
        }
        notes_path = self.profiles.path / target / "NOTES.md"
        if notes_path.is_file() and not path_is_link_like(notes_path):
            versions["NOTES.md"] = self.read(
                "notes", target=target, allow_custom=True
            )["version"]
        role_mem_path = self.profiles.path / target / "MEMORY.md"
        if role_mem_path.is_file() and not path_is_link_like(role_mem_path):
            versions["MEMORY.md"] = self.read(
                "role_memory", target=target, allow_custom=True
            )["version"]
        return versions

    def purge_profile(self, target: str) -> None:
        if not isinstance(target, str) or not target:
            return
        with self._lock(), self.store.connect() as db, db:
            assets = {
                f"profile:{target}:PROFILE.md",
                f"profile:{target}:profile.toml",
                f"notes:{target}:NOTES.md",
                f"role_memory:{target}:MEMORY.md",
            }
            rows = db.execute(
                "SELECT DISTINCT asset FROM memory_versions WHERE kind IN ('profile', 'notes', 'role_memory') AND target=?",
                (target,),
            ).fetchall()
            assets.update(row[0] for row in rows)
            for asset in assets:
                db.execute("DELETE FROM memory_search WHERE asset=?", (asset,))
                db.execute("DELETE FROM memory_versions WHERE asset=?", (asset,))

    def save_skill_proposal(
        self,
        *,
        thread_id: str,
        revision: int = 0,
        profile_id: str = "default",
        skill_id: str,
        title: str,
        description: str,
        content: str,
        signals: list[str] | None = None,
        rationale: str = "",
    ) -> dict:
        clean_id = _slug_skill_id(skill_id) or f"workflow-{hashlib.sha1(str(thread_id).encode()).hexdigest()[:8]}"
        self._guard("skill", content, "", allow_custom=True)
        now = float(self.clock())
        prop_id = f"sp_{hashlib.sha1(f'{thread_id}:{revision}:{clean_id}'.encode('utf-8')).hexdigest()[:12]}"
        sig_json = json.dumps(list(signals or []), ensure_ascii=False)
        with self.store.connect() as db, db:
            db.execute(
                """INSERT INTO skill_proposals (
                    proposal_id, thread_id, revision, profile_id, skill_id, title,
                    description, content, signals, rationale, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    skill_id=excluded.skill_id,
                    title=excluded.title,
                    description=excluded.description,
                    content=excluded.content,
                    signals=excluded.signals,
                    rationale=excluded.rationale,
                    status='pending',
                    updated_at=excluded.updated_at""",
                (
                    prop_id,
                    str(thread_id or ""),
                    int(revision or 0),
                    str(profile_id or "default"),
                    clean_id,
                    str(title or clean_id)[:80],
                    str(description or "")[:200],
                    content,
                    sig_json,
                    str(rationale or "")[:240],
                    now,
                    now,
                ),
            )
        return self.get_skill_proposal(prop_id) or {"proposal_id": prop_id, "skill_id": clean_id}

    def get_skill_proposal(self, proposal_id: str) -> dict | None:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM skill_proposals WHERE proposal_id=?",
                (str(proposal_id or "").strip(),),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["signals"] = json.loads(d.get("signals") or "[]")
        except json.JSONDecodeError:
            d["signals"] = []
        return d

    def list_skill_proposals(
        self, *, status: str = "pending", limit: int = 20, db=None
    ) -> list[dict]:
        sql = "SELECT * FROM skill_proposals WHERE status=? ORDER BY updated_at DESC LIMIT ?"
        params = (str(status or "pending"), int(limit))
        try:
            if db is not None:
                rows = db.execute(sql, params).fetchall()
            else:
                with self.store.connect() as conn:
                    rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["signals"] = json.loads(d.get("signals") or "[]")
            except json.JSONDecodeError:
                d["signals"] = []
            out.append(d)
        return out

    def resolve_skill_proposal(self, proposal_id: str, status: str = "created") -> bool:
        if status not in {"created", "dismissed", "pending"}:
            raise ValueError("草稿状态无效")
        now = float(self.clock())
        with self.store.connect() as db, db:
            cur = db.execute(
                "UPDATE skill_proposals SET status=?, updated_at=? WHERE proposal_id=?",
                (status, now, str(proposal_id or "").strip()),
            )
            return bool(cur.rowcount)

    def write(
        self,
        kind,
        content,
        *,
        target="",
        chat_id="",
        document="",
        expected_version=None,
        author="admin",
        allow_custom=False,
    ):
        with self._lock():
            return self._write(
                kind,
                content,
                target=target,
                chat_id=chat_id,
                document=document,
                expected_version=expected_version,
                author=author,
                allow_custom=allow_custom,
            )

    def _journal_tz(self, tz_offset_minutes: int | None = None) -> timezone:
        offset = (
            self.tz_offset_minutes
            if tz_offset_minutes is None
            else _validate_tz_offset_minutes(tz_offset_minutes)
        )
        return timezone(timedelta(minutes=offset))

    def _write(
        self,
        kind,
        content,
        *,
        target="",
        chat_id="",
        document="",
        expected_version=None,
        author="admin",
        allow_custom=False,
    ):
        if kind == "memory" and isinstance(chat_id, str) and chat_id:
            chat_id = self._resolve_memory_chat_id(chat_id)
        if kind == "journal" and content and not content.lstrip("\r\n ").startswith("## "):
            content = (
                "## "
                + datetime.fromtimestamp(self.clock(), self._journal_tz())
                .date()
                .isoformat()
                + "\n"
                + content
            )
        self._guard(kind, content, chat_id, document, allow_custom=allow_custom)
        previous = None
        path = None
        try:
            with self.store.connect() as db, db:
                db.execute("BEGIN IMMEDIATE")
                current, asset, path = self._sync(
                    db, kind, target, chat_id, document, allow_custom=allow_custom
                )
                if expected_version is not None and (
                    type(expected_version) is not int
                    or expected_version != current["version"]
                ):
                    raise ValueError("版本已变化，请重新读取再修改")
                previous = current["content"].encode("utf-8")
                atomic_private_write(path, content.encode("utf-8"))
                if kind == "profile":
                    self.profiles.get(target)
                version = current["version"] + 1
                self._record(
                    db,
                    asset,
                    version,
                    kind,
                    current["target"],
                    chat_id,
                    current["document"],
                    content,
                    author,
                )
                return {**current, "content": content, "version": version}
        except BaseException:
            if previous is not None and path is not None:
                atomic_private_write(path, previous)
            raise

    def history(self, kind, *, target="", chat_id="", document=""):
        self.read(kind, target=target, chat_id=chat_id, document=document)
        asset = self._locate(kind, target, chat_id, document)[0]
        with self.store.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT version,author,updated FROM memory_versions WHERE asset=? ORDER BY version DESC LIMIT 30",
                    (asset,),
                )
            ]

    def _reanchor_expired_journal(
        self, content: str, *, tz_offset_minutes: int | None = None
    ) -> str:
        if not str(content or "").strip():
            return content
        tz = self._journal_tz(tz_offset_minutes)
        now = self.clock()
        today = datetime.fromtimestamp(now, tz).date().isoformat()
        normalized = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.findall(
            _JOURNAL_BLOCK_RE,
            normalized,
            re.MULTILINE | re.DOTALL,
        )
        expired_entries: list[str] = []
        for day, text in blocks:
            try:
                when = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz).timestamp()
            except ValueError:
                continue
            if when <= now - 7 * 86400:
                for entry in self._split_journal_entries(text.strip()):
                    if entry:
                        body = entry[2:].strip() if entry.startswith("- ") else entry
                        expired_entries.append(f"- [回溯 {day}] {body}")
        recent = self._recent_journal(content, tz_offset_minutes=tz_offset_minutes)
        if not expired_entries:
            return recent or content
        reanchored = f"## {today}\n" + "\n".join(expired_entries)
        combined = f"{recent}\n\n{reanchored}".strip() if recent else reanchored
        return self._recent_journal(combined, tz_offset_minutes=tz_offset_minutes)

    def rollback(self, kind, version, **options):
        if type(version) is not int or version < 0:
            raise ValueError("历史版本无效")
        old = self.read(
            kind,
            version=version,
            **{
                key: value
                for key, value in options.items()
                if key not in {"expected_version", "allow_custom"}
            },
        )
        content = old["content"]
        if kind == "journal":
            content = self._reanchor_expired_journal(content)
        write_options = {"allow_custom": True, **options}
        return self.write(kind, content, **write_options, author="rollback")

    @staticmethod
    def _split_journal_entries(text: str) -> list[str]:
        body = str(text or "").strip()
        if not body:
            return []
        lines = body.splitlines()
        if any(line.lstrip().startswith("- ") for line in lines):
            entries: list[str] = []
            for raw_line in lines:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                if raw_line.lstrip().startswith("- "):
                    entries.append("- " + raw_line.lstrip()[2:].strip())
                elif entries:
                    entries[-1] = entries[-1] + "\n" + raw_line.rstrip()
                else:
                    entries.append("- " + stripped)
            return entries
        return [line.strip() for line in lines if line.strip()]

    def _recent_journal(self, content, *, tz_offset_minutes: int | None = None):
        tz = self._journal_tz(tz_offset_minutes)
        normalized = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.findall(
            _JOURNAL_BLOCK_RE,
            normalized,
            re.MULTILINE | re.DOTALL,
        )
        now = self.clock()
        by_day: dict[str, list[str]] = {}
        day_blocks_count: dict[str, int] = {}
        raw_day_body: dict[str, str] = {}
        raw_day_entry_count: dict[str, int] = {}
        for day, text in blocks:
            try:
                when = (
                    datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz).timestamp()
                )
            except ValueError:
                continue
            if now - 7 * 86400 < when <= now + 86400:
                cleaned_body = text.strip()
                if not cleaned_body:
                    continue
                day_blocks_count[day] = day_blocks_count.get(day, 0) + 1
                raw_day_body.setdefault(day, cleaned_body)
                entries = self._split_journal_entries(cleaned_body)
                if not entries:
                    continue
                raw_day_entry_count.setdefault(day, len(entries))
                by_day.setdefault(day, []).extend(entries)

        days: list[tuple[str, list[str]]] = []
        for day in sorted(by_day):
            entries = by_day[day]
            if day_blocks_count[day] > 1:
                entries = [
                    entry if entry.startswith("- ") else f"- {entry}"
                    for entry in entries
                ]
            days.append((day, entries))

        def _render(items: list[tuple[str, list[str]]]) -> str:
            rendered_blocks = []
            for day, entries in items:
                if not entries:
                    continue
                if (
                    day_blocks_count[day] == 1
                    and len(entries) == raw_day_entry_count[day]
                    and not any(entry.startswith("- ") for entry in entries)
                ):
                    rendered_blocks.append(f"## {day}\n{raw_day_body[day]}")
                else:
                    rendered_blocks.append(f"## {day}\n" + "\n".join(entries))
            return "\n\n".join(rendered_blocks)

        limit = self.LIMITS["journal"]
        while len(_render(days)) > limit and days:
            if len(days[0][1]) > 1:
                days[0][1].pop(0)
            elif len(days) > 1:
                days.pop(0)
            else:
                break
        return _render(days)[:limit]

    def context(self, chat_id):
        memory = self.read("memory", chat_id=chat_id)["content"] if chat_id else ""
        journal = self.read("journal")
        recent = self._recent_journal(journal["content"])
        if recent != journal["content"]:
            try:
                self.write(
                    "journal",
                    recent,
                    expected_version=journal["version"],
                    author="expiry",
                )
            except ValueError:
                recent = self._recent_journal(self.read("journal")["content"])
        return {
            "memory": memory,
            "journal": recent,
            "heartbeat": self.read("heartbeat")["content"],
        }

    def methods(self, profile):
        pieces: list[str] = []
        role_mem = self.read("role_memory", target=profile.id)["content"].strip()
        if role_mem:
            pieces.append(f"### 角色长期记忆\n{role_mem}")
        notes_txt = self.read("notes", target=profile.id)["content"]
        pieces.append(notes_txt)
        for skill in profile.skills:
            content = self.read("skill", target=skill)["content"]
            if not content.strip():
                path = (
                    self.profiles.path
                    / profile.id
                    / "preset/skills"
                    / skill
                    / "SKILL.md"
                )
                if _has_relative_link(self.profiles.path, path):
                    raise ValueError("技能路径不能经过链接")
                content = self._bytes(path, max_bytes=_SKILL_FILE_MAX_BYTES).decode("utf-8")
                self._guard("skill", content, "", allow_custom=True)
            if not content.strip():
                raise ValueError("角色引用的技能为空或不存在")
            companions = self.skill_files(skill, profile_id=profile.id)
            if companions:
                listed = ", ".join(c["path"] for c in companions[:20])
                content = (
                    content.rstrip()
                    + f"\n\n[技能附属文件（工作区只读路径 in/skills/{skill}/）：{listed}]"
                )
            pieces.append(content)
        return "\n\n".join(pieces)[:16000]

    def search(
        self,
        query,
        chat_id,
        *,
        kind="all",
        scope="available",
        limit=5,
        all_chats=False,
        chat_ids=None,
    ):
        if (
            kind not in {"all", "memory", "journal"}
            or scope not in {"chat", "self", "available"}
            or not isinstance(query, str)
            or len(query) > 500
            or type(limit) is not int
            or not 1 <= limit <= 10
        ):
            raise ValueError("记忆检索参数无效")
        self.context("" if all_chats else chat_id)
        tokens = re.findall(r"\w+", query)[:12]
        if query and not tokens:
            return []
        match = " AND ".join('"' + token + '"*' for token in tokens)
        alias_ids: list[str] = []
        if not all_chats:
            raw_candidates = [chat_id, *(chat_ids or ())]
            if callable(self.chat_aliases) and chat_id:
                try:
                    raw_candidates.extend(self.chat_aliases(chat_id))
                except (TypeError, ValueError):
                    pass
            alias_ids = sorted(
                {str(cid) for cid in raw_candidates if isinstance(cid, str) and cid}
            )
            for cid in alias_ids:
                if self._has_memory_data(cid):
                    with self._lock(), self.store.connect() as sync_db, sync_db:
                        sync_db.execute("BEGIN IMMEDIATE")
                        self._sync(
                            sync_db,
                            "memory",
                            "",
                            cid,
                            "",
                            allow_custom=True,
                            resolve_alias=False,
                        )
        with self.store.connect() as db:
            if all_chats:
                rows = db.execute(
                    'SELECT v.* FROM memory_versions v JOIN (SELECT asset,MAX(version) AS version FROM memory_versions GROUP BY asset) latest ON v.asset=latest.asset AND v.version=latest.version WHERE (v.kind="memory" OR v.kind="journal") ORDER BY v.updated DESC'
                ).fetchall()
            elif len(alias_ids) > 1:
                placeholders = ",".join("?" for _ in alias_ids)
                rows = db.execute(
                    f'SELECT v.* FROM memory_versions v JOIN (SELECT asset,MAX(version) AS version FROM memory_versions GROUP BY asset) latest ON v.asset=latest.asset AND v.version=latest.version WHERE (v.kind="memory" AND v.chat_id IN ({placeholders}) OR v.kind="journal") ORDER BY v.updated DESC',
                    alias_ids,
                ).fetchall()
            else:
                target_cid = alias_ids[0] if alias_ids else chat_id
                rows = db.execute(
                    'SELECT v.* FROM memory_versions v JOIN (SELECT asset,MAX(version) AS version FROM memory_versions GROUP BY asset) latest ON v.asset=latest.asset AND v.version=latest.version WHERE (v.kind="memory" AND v.chat_id=? OR v.kind="journal") ORDER BY v.updated DESC',
                    (target_cid,),
                ).fetchall()
            matches = (
                {
                    row["asset"]
                    for row in db.execute(
                        "SELECT asset FROM memory_search WHERE memory_search MATCH ?",
                        (match,),
                    )
                }
                if match
                else set()
            )
        found = []
        for row in rows:
            if (
                kind != "all"
                and row["kind"] != kind
                or scope == "self"
                and row["kind"] != "journal"
                or scope == "chat"
                and row["kind"] != "memory"
            ):
                continue
            content = row["content"]
            if (
                not content
                or tokens
                and row["asset"] not in matches
                and not all(token.casefold() in content.casefold() for token in tokens)
            ):
                continue
            entry = {
                "kind": row["kind"],
                "content": content[:2200],
                "version": row["version"],
            }
            if row["kind"] == "memory" and row["chat_id"]:
                entry["chat_id"] = row["chat_id"]
            found.append(entry)
        return found[:limit]

    def experience(self, row):
        with self._lock():
            self._experience(row)

    def _experience(self, row):
        if (
            row.get("owner") != "self"
            or not is_autonomous_snapshot(row.get("profile_snapshot"))
            or not row.get("result", {}).get("text")
        ):
            return
        revision = row.get("completed_revision", 0)
        with self.store.connect() as db, db:
            if db.execute(
                "SELECT 1 FROM experience_receipts WHERE thread_id=? AND revision=?",
                (row["thread_id"], revision),
            ).fetchone():
                return
            old, _asset, _path = self._sync(db, "journal", "", "", "")
        text = " ".join(str(row["result"]["text"][:500]).split())
        if not text:
            return
        self._guard("journal", text, "")
        day = (
            datetime.fromtimestamp(self.clock(), self._journal_tz()).date().isoformat()
        )
        entry = text if text.startswith("- ") else f"- {text}"
        seed = (old["content"] + "\n\n## " + day + "\n" + entry).strip()
        content = self._recent_journal(seed)
        self._write(
            "journal", content, expected_version=old["version"], author="self_activity"
        )
        with self.store.connect() as db, db:
            db.execute(
                "INSERT OR IGNORE INTO experience_receipts VALUES (?,?)",
                (row["thread_id"], revision),
            )
