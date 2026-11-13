"""分层文件、版本历史与按会话隔离的检索。"""

import fcntl
import hashlib
import os
import re
import stat
import time
from contextlib import contextmanager
from datetime import UTC, datetime

from .vault import atomic_private_write, private_directory


class MemoryLayers:
    def __init__(self, root, store, profiles, vault, *, clock=time.time):
        self.root, self.store, self.profiles, self.vault, self.clock = (
            root,
            store,
            profiles,
            vault,
            clock,
        )
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
        private_directory(root / "memory/self")
        for kind in ("journal", "heartbeat"):
            path = self._locate(kind)[1]
            if not path.exists():
                atomic_private_write(path, b"")

    @contextmanager
    def _lock(self):
        descriptor = os.open(
            self.root / ".memory.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "r+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _locate(self, kind, target="", chat_id="", document=""):
        if kind in {"profile", "notes", "skill"}:
            if not isinstance(target, str) or not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_-]{0,63}", target
            ):
                raise ValueError("能力或技能编号无效")
            if kind == "profile":
                document = document or "PROFILE.md"
                if document not in {"PROFILE.md", "profile.toml"}:
                    raise ValueError("档案文档无效")
                directory = self.profiles.path / target
                if not directory.is_dir():
                    raise ValueError("能力档案不存在，请先克隆创建")
            elif kind == "notes":
                self.profiles.get(target)
                directory, document = self.profiles.path / target, "NOTES.md"
            else:
                directory, document = (
                    self.root / "abilities/skills" / target,
                    "SKILL.md",
                )
        elif kind == "memory":
            if not isinstance(chat_id, str) or not chat_id or len(chat_id) > 256:
                raise ValueError("会话记忆需要原会话")
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
        private_directory(directory)
        return f"{kind}:{target}:{document}", directory / document, document, target

    @staticmethod
    def _bytes(path):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return b""
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > 65536
            ):
                raise ValueError("记忆文件无效或过大")
            return source.read(65537)

    def _guard(self, kind, content, chat_id, document=""):
        if not isinstance(content, str):
            raise TypeError("记忆内容必须为文本")
        maximum = {
            "memory": 2200,
            "journal": 1500,
            "notes": 2200,
            "heartbeat": 2200,
        }.get(kind, 16000)
        if len(content) > maximum or "\x00" in content:
            raise ValueError("记忆内容超过长度限制")
        if self.vault.contains_secret(content.encode()) or re.search(
            r"-----BEGIN .*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b",
            content,
        ):
            raise ValueError("内容包含凭据，已拒绝保存")
        if kind == "memory" or kind == "profile" and document == "profile.toml":
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

    def _sync(self, db, kind, target, chat_id, document):
        asset, path, document, target = self._locate(kind, target, chat_id, document)
        content = self._bytes(path).decode("utf-8")
        self._guard(kind, content, chat_id, document)
        old = db.execute(
            "SELECT * FROM memory_versions WHERE asset=? ORDER BY version DESC LIMIT 1",
            (asset,),
        ).fetchone()
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

    def read(self, kind, *, target="", chat_id="", document="", version=None):
        with self._lock(), self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            current, asset, _path = self._sync(db, kind, target, chat_id, document)
            if version is not None:
                old = db.execute(
                    "SELECT content,version FROM memory_versions WHERE asset=? AND version=?",
                    (asset, version),
                ).fetchone()
                if not old:
                    raise ValueError("历史版本不存在")
                current.update(dict(old))
            return current

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
            )

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
    ):
        if kind == "journal" and content and not content.startswith("## "):
            content = (
                "## "
                + datetime.fromtimestamp(self.clock(), UTC).date().isoformat()
                + "\n"
                + content
            )
        self._guard(kind, content, chat_id, document)
        previous = None
        path = None
        try:
            with self.store.connect() as db, db:
                db.execute("BEGIN IMMEDIATE")
                current, asset, path = self._sync(db, kind, target, chat_id, document)
                if expected_version is not None and (
                    type(expected_version) is not int
                    or expected_version != current["version"]
                ):
                    raise ValueError("版本已变化，请重新读取再修改")
                previous = current["content"].encode()
                atomic_private_write(path, content.encode())
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

    def rollback(self, kind, version, **options):
        if type(version) is not int or version < 0:
            raise ValueError("历史版本无效")
        old = self.read(
            kind,
            version=version,
            **{
                key: value
                for key, value in options.items()
                if key != "expected_version"
            },
        )
        return self.write(kind, old["content"], **options, author="rollback")

    def _recent_journal(self, content):
        blocks = re.findall(
            r"^## (\d{4}-\d{2}-\d{2})\n(.*?)(?=^## |\Z)",
            content,
            re.MULTILINE | re.DOTALL,
        )
        kept = []
        for day, text in blocks:
            when = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
            if self.clock() - 7 * 86400 < when <= self.clock():
                kept.append("## " + day + "\n" + text.strip())
        while len("\n\n".join(kept)) > 1500 and len(kept) > 1:
            kept.pop(0)
        return "\n\n".join(kept)[:1500]

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
        pieces = [self.read("notes", target=profile.id)["content"]]
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
                if any(parent.is_symlink() for parent in path.parents):
                    raise ValueError("技能路径不能经过链接")
                content = self._bytes(path).decode("utf-8")
                self._guard("skill", content, "")
            if not content.strip():
                raise ValueError("档案引用的技能为空或不存在")
            pieces.append(content)
        return "\n\n".join(pieces)[:16000]

    def search(self, query, chat_id, *, kind="all", scope="available", limit=5):
        if (
            kind not in {"all", "memory", "journal"}
            or scope not in {"chat", "self", "available"}
            or not isinstance(query, str)
            or len(query) > 500
            or type(limit) is not int
            or not 1 <= limit <= 10
        ):
            raise ValueError("记忆检索参数无效")
        self.context(chat_id)
        tokens = re.findall(r"\w+", query)[:12]
        if query and not tokens:
            return []
        match = " AND ".join('"' + token + '"*' for token in tokens)
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT v.* FROM memory_versions v JOIN (SELECT asset,MAX(version) AS version FROM memory_versions GROUP BY asset) latest ON v.asset=latest.asset AND v.version=latest.version WHERE (v.kind="memory" AND v.chat_id=? OR v.kind="journal") ORDER BY v.updated DESC',
                (chat_id,),
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
            found.append(
                {
                    "kind": row["kind"],
                    "content": content[:2200],
                    "version": row["version"],
                }
            )
        return found[:limit]

    def experience(self, row):
        with self._lock():
            self._experience(row)

    def _experience(self, row):
        if (
            row["owner"] != "self"
            or row["profile_snapshot"]["access"] != "everyone"
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
        text = row["result"]["text"][:500]
        self._guard("journal", text, "")
        day = datetime.fromtimestamp(self.clock(), UTC).date().isoformat()
        content = self._recent_journal(old["content"] + "\n\n## " + day + "\n" + text)
        self._write(
            "journal", content, expected_version=old["version"], author="self_activity"
        )
        with self.store.connect() as db, db:
            db.execute(
                "INSERT OR IGNORE INTO experience_receipts VALUES (?,?)",
                (row["thread_id"], revision),
            )
