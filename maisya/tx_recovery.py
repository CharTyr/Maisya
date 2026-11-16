"""多写端原子事务日志、进程内锁与崩溃恢复（B5）。"""

from __future__ import annotations

from contextlib import closing
import json
import logging
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid

from .pathsafe import path_is_link_like
from .vault import atomic_private_write

logger = logging.getLogger(__name__)

_TX_LOCKS_GUARD = threading.Lock()
_TX_LOCKS: dict[str, threading.RLock] = {}
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def tx_lock(data_root: Path | str) -> threading.RLock:
    """返回针对指定 data_root 的进程内可重入事务写锁（锁内禁止等待 LLM/网络）。"""
    key = str(Path(data_root).resolve()) if data_root else "__default__"
    with _TX_LOCKS_GUARD:
        lock = _TX_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _TX_LOCKS[key] = lock
        return lock


def _journal_root(data_root: Path | str) -> Path:
    root = Path(data_root) / "tx_journal"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _safe_label(label: str) -> str:
    return _SAFE_LABEL_RE.sub("_", str(label or "item"))[:48] or "item"


class TxJournal:
    """持久化两阶段（prepared -> committed / rolling_back）恢复日志，绝不落任何明文密钥。"""

    def __init__(
        self,
        data_root: Path | str,
        *,
        kind: str,
        tx_id: str | None = None,
        _manifest: dict | None = None,
    ):
        self.data_root = Path(data_root)
        self.root = _journal_root(self.data_root)
        self.tx_id = tx_id or f"tx_{uuid.uuid4().hex[:16]}"
        self.dir = self.root / self.tx_id
        self.backups_dir = self.dir / "backups"
        self.manifest_path = self.dir / "manifest.json"
        if _manifest is not None:
            self._manifest = dict(_manifest)
            return
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.backups_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._manifest = {
            "tx_id": self.tx_id,
            "kind": str(kind or "tx"),
            "status": "prepared",
            "created_at": time.time(),
            "operations": [],
            "errors": [],
        }
        self._save_manifest()

    @classmethod
    def open_existing(cls, manifest_path: Path, data_root: Path | str) -> TxJournal:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cls(
            data_root,
            kind=str(data.get("kind") or "tx"),
            tx_id=str(data.get("tx_id") or manifest_path.parent.name),
            _manifest=data,
        )

    def _save_manifest(self) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_private_write(
            self.manifest_path,
            json.dumps(self._manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def backup_file(self, label: str, target_path: Path | str) -> None:
        path = Path(target_path)
        idx = len(self._manifest["operations"])
        if path.exists():
            if path.is_dir() or path_is_link_like(path):
                raise ValueError(f"事务文件备份目标无效：{path}")
            rel = f"backups/{idx:03d}_{_safe_label(label)}.bin"
            atomic_private_write(self.dir / rel, path.read_bytes())
            self._manifest["operations"].append(
                {
                    "op": "file",
                    "target": str(path),
                    "absent": False,
                    "backup_rel": rel,
                }
            )
        else:
            self._manifest["operations"].append(
                {
                    "op": "file",
                    "target": str(path),
                    "absent": True,
                }
            )
        self._save_manifest()

    def backup_tree(self, label: str, target_dir: Path | str) -> None:
        path = Path(target_dir)
        idx = len(self._manifest["operations"])
        if path.exists():
            if not path.is_dir() or path_is_link_like(path):
                raise ValueError(f"事务目录备份目标无效：{path}")
            rel = f"backups/{idx:03d}_{_safe_label(label)}_dir"
            shutil.copytree(path, self.dir / rel, symlinks=False)
            self._manifest["operations"].append(
                {
                    "op": "tree",
                    "target": str(path),
                    "absent": False,
                    "backup_rel": rel,
                }
            )
        else:
            self._manifest["operations"].append(
                {
                    "op": "tree",
                    "target": str(path),
                    "absent": True,
                }
            )
        self._save_manifest()

    def record_created_path(self, target_path: Path | str) -> None:
        self._manifest["operations"].append(
            {
                "op": "created_path",
                "target": str(Path(target_path)),
            }
        )
        self._save_manifest()

    def record_skill_overwrite(self, skill_id: str, prev_version: int) -> None:
        self._manifest["operations"].append(
            {
                "op": "skill_overwrite",
                "skill_id": str(skill_id),
                "prev_version": int(prev_version),
            }
        )
        self._save_manifest()

    def record_skill_create(self, skill_id: str) -> None:
        self._manifest["operations"].append(
            {
                "op": "skill_create",
                "skill_id": str(skill_id),
            }
        )
        self._save_manifest()

    def record_created_intent(self, intent_id: str) -> None:
        clean_id = str(intent_id or "").strip()
        if not clean_id:
            return
        self._manifest["operations"].append(
            {
                "op": "intent_create",
                "intent_id": clean_id,
            }
        )
        self._save_manifest()

    def commit(self) -> None:
        self._manifest["status"] = "committed"
        self._manifest["committed_at"] = time.time()
        shutil.rmtree(self.dir, ignore_errors=True)

    def rollback(
        self,
        *,
        store_path: Path | str | None = None,
        memory=None,
    ) -> list[str]:
        self._manifest["status"] = "rolling_back"
        self._save_manifest()
        errors: list[str] = []
        rolled_ops: list[str] = []
        db_file = Path(store_path) if store_path else (
            Path(memory.store.path) if memory is not None and getattr(memory, "store", None) else None
        )

        for entry in reversed(self._manifest.get("operations") or ()):
            op = entry.get("op")
            try:
                if op == "file":
                    target = Path(entry["target"])
                    if entry.get("absent"):
                        if target.is_dir():
                            raise IsADirectoryError(f"无法按文件移除目录：{target}")
                        target.unlink(missing_ok=True)
                    else:
                        backup_rel = str(entry.get("backup_rel") or "")
                        backup_path = self.dir / backup_rel
                        if not backup_rel or not backup_path.is_file():
                            raise FileNotFoundError(f"缺少事务文件快照：{backup_path}")
                        if target.is_dir():
                            raise IsADirectoryError(f"文件还原目标已是目录：{target}")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        atomic_private_write(target, backup_path.read_bytes())
                    rolled_ops.append(f"file:{target.name}")
                elif op == "tree":
                    target = Path(entry["target"])
                    if target.exists():
                        if target.is_dir():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    if not entry.get("absent"):
                        backup_rel = str(entry.get("backup_rel") or "")
                        backup_path = self.dir / backup_rel
                        if not backup_rel or not backup_path.is_dir():
                            raise FileNotFoundError(f"缺少事务目录快照：{backup_path}")
                        shutil.copytree(backup_path, target, symlinks=False)
                    rolled_ops.append(f"tree:{target.name}")
                elif op == "created_path":
                    target = Path(entry["target"])
                    if target.exists():
                        if target.is_dir():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    rolled_ops.append(f"created_path:{target.name}")
                elif op == "skill_create":
                    sid = str(entry.get("skill_id") or "")
                    asset = f"skill:{sid}:SKILL.md"
                    conn_ctx = (
                        memory.store.connect()
                        if memory is not None and getattr(memory, "store", None)
                        else (closing(sqlite3.connect(db_file, timeout=3)) if db_file and db_file.exists() else None)
                    )
                    if conn_ctx is not None:
                        with conn_ctx as db, db:
                            db.execute(
                                "DELETE FROM memory_versions WHERE kind='skill' AND target=?",
                                (sid,),
                            )
                            db.execute(
                                "DELETE FROM memory_search WHERE asset=?",
                                (asset,),
                            )
                    rolled_ops.append(f"skill_create:{sid}")
                elif op == "skill_overwrite":
                    sid = str(entry.get("skill_id") or "")
                    prev_ver = int(entry.get("prev_version") or 1)
                    if memory is not None:
                        memory.rollback(
                            "skill",
                            prev_ver,
                            target=sid,
                            document="SKILL.md",
                            allow_custom=True,
                        )
                    elif db_file and db_file.exists():
                        with closing(sqlite3.connect(db_file, timeout=3)) as db, db:
                            db.execute(
                                "DELETE FROM memory_versions WHERE kind='skill' AND target=? AND version>?",
                                (sid, prev_ver),
                            )
                    rolled_ops.append(f"skill_overwrite:{sid}@{prev_ver}")
                elif op == "intent_create":
                    iid = str(entry.get("intent_id") or "")
                    if iid and db_file and db_file.exists():
                        with closing(sqlite3.connect(db_file, timeout=3)) as db, db:
                            db.execute("DELETE FROM intents WHERE intent_id=?", (iid,))
                            if (
                                db.execute(
                                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delegations'"
                                ).fetchone()
                                is not None
                            ):
                                db.execute(
                                    "DELETE FROM delegations WHERE subject_kind='intent' AND subject_id=?",
                                    (iid,),
                                )
                    rolled_ops.append(f"intent_create:{iid}")
            except Exception as exc:
                errors.append(f"{op}:{exc}")

        if errors:
            self._manifest["status"] = "rollback_failed"
            self._manifest["errors"] = list(errors)
            self._save_manifest()
            logger.error(
                "事务回滚未完全成功 tx_id=%s kind=%s errors=%s",
                self.tx_id,
                self._manifest.get("kind"),
                errors,
            )
            raise RuntimeError("事务回滚未完全成功：" + "; ".join(errors))

        shutil.rmtree(self.dir, ignore_errors=True)
        return rolled_ops


def recover_pending_transactions(
    data_root: Path | str,
    *,
    store_path: Path | str | None = None,
    memory=None,
) -> list[dict]:
    """启动时清理并回滚上次进程中断遗留的未提交事务（幂等且不吞失败）。"""
    root_path = Path(data_root)
    journal_dir = root_path / "tx_journal"
    if not journal_dir.is_dir():
        return []
    recovered: list[dict] = []
    with tx_lock(root_path):
        for entry in sorted(journal_dir.iterdir()):
            if not entry.is_dir():
                continue
            manifest = entry / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                tx = TxJournal.open_existing(manifest, root_path)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.error("读取未完成事务日志失败 path=%s: %s", manifest, exc)
                continue
            status = str(tx._manifest.get("status") or "")
            if status == "committed":
                shutil.rmtree(entry, ignore_errors=True)
                continue
            if status in {"prepared", "rolling_back"}:
                ops = tx.rollback(store_path=store_path, memory=memory)
                recovered.append(
                    {
                        "tx_id": tx.tx_id,
                        "kind": tx._manifest.get("kind"),
                        "rolled_back": ops,
                    }
                )
    return recovered
