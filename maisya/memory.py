"""分层文件、版本历史与按会话隔离的检索。"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .vault import atomic_private_write, private_directory

PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
SKILL_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")


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
    text = str(content or "")
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
        heartbeat_preset = PRESETS_DIR / "memory/HEARTBEAT.md"
        for kind in ("journal", "heartbeat"):
            path = self._locate(kind)[1]
            if not path.exists() and not path.is_symlink():
                initial = (
                    heartbeat_preset.read_bytes()
                    if kind == "heartbeat" and heartbeat_preset.is_file()
                    else b""
                )
                atomic_private_write(path, initial)
        skills_presets = PRESETS_DIR / "skills"
        if skills_presets.is_dir():
            for source_dir in sorted(skills_presets.iterdir()):
                if not source_dir.is_dir() or source_dir.is_symlink():
                    continue
                skill_file = source_dir / "SKILL.md"
                if not skill_file.is_file() or skill_file.is_symlink():
                    continue
                destination_dir = private_directory(root / "abilities/skills" / source_dir.name)
                destination = destination_dir / "SKILL.md"
                if not destination.exists() and not destination.is_symlink():
                    atomic_private_write(destination, skill_file.read_bytes())

    @staticmethod
    def heartbeat_parts(content: str) -> tuple[str, str]:
        cleaned = re.sub(r"<!--[\s\S]*?-->", "", str(content or ""))
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
                or info.st_size > 131072
            ):
                raise ValueError("记忆文件无效或过大")
            return source.read(131073)

    LIMITS = {
        "memory": 16000,
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
        if allow_custom or kind == "memory" or kind == "profile" and document == "profile.toml":
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

    def _sync(self, db, kind, target, chat_id, document, *, allow_custom=False):
        asset, path, document, target = self._locate(kind, target, chat_id, document)
        content = self._bytes(path).decode("utf-8")
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

    def preset_skills(self) -> set[str]:
        skills_presets = PRESETS_DIR / "skills"
        if not skills_presets.is_dir():
            return set()
        return {
            p.name
            for p in skills_presets.iterdir()
            if p.is_dir()
            and not p.is_symlink()
            and (p / "SKILL.md").is_file()
            and not (p / "SKILL.md").is_symlink()
        }

    def skills(self):
        root = self.root / "abilities/skills"
        if not root.is_dir():
            return []
        return sorted(
            p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()
        )

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
        if skill_path.is_symlink():
            raise ValueError("技能路径无效")
        if skill_path.exists() and self._bytes(skill_path).strip():
            raise ValueError("同名技能已存在")
        if content and content.strip():
            body = content
        elif base and base.strip():
            if not isinstance(base, str) or not SKILL_ID_RE.fullmatch(base):
                raise ValueError("源技能编号无效")
            src_path = self.root / "abilities/skills" / base / "SKILL.md"
            if src_path.is_symlink() or not src_path.is_file():
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
        current = self.read("skill", target=target)
        return self.write(
            "skill",
            body,
            target=target,
            expected_version=current["version"],
            allow_custom=allow_custom,
        )

    def reset_skill(self, target: str):
        if not isinstance(target, str) or not SKILL_ID_RE.fullmatch(target):
            raise ValueError("技能编号无效")
        preset_file = PRESETS_DIR / "skills" / target / "SKILL.md"
        if preset_file.is_symlink() or not preset_file.is_file():
            raise ValueError("仅内置预设技能支持重置")
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
        if directory.is_symlink() or not directory.exists():
            raise ValueError("技能不存在")
        with self._lock(), self.store.connect() as db, db:
            shutil.rmtree(directory)
            asset = f"skill:{target}:SKILL.md"
            db.execute("DELETE FROM memory_search WHERE asset=?", (asset,))
            db.execute("DELETE FROM memory_versions WHERE asset=?", (asset,))
        return {"deleted": True, "skill": target}

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
        if kind == "journal" and content and not content.startswith("## "):
            content = (
                "## "
                + datetime.fromtimestamp(self.clock(), UTC).date().isoformat()
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
                if key not in {"expected_version", "allow_custom"}
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
        while len("\n\n".join(kept)) > self.LIMITS["journal"] and len(kept) > 1:
            kept.pop(0)
        return "\n\n".join(kept)[: self.LIMITS["journal"]]

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
                self._guard("skill", content, "", allow_custom=True)
            if not content.strip():
                raise ValueError("角色引用的技能为空或不存在")
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
