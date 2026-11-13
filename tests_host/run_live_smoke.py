"""在隔离临时实例目录中执行真实宿主 LLM + EXA 联网冒烟验证，零修改线上代码、零改线上库、零对外发消息。"""

import base64
import hashlib
import io
import re
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SMOKE_SCRIPT = r"""
import asyncio, importlib.metadata, json, sys, time
from pathlib import Path

stage = Path(sys.argv[1]).resolve()
host_root = Path(sys.argv[2]).resolve()
instance = stage / "instance"
candidate = stage / "candidate"

def log_phase(phase, **kw):
    print(json.dumps({"phase": phase, **kw}, ensure_ascii=False), flush=True)

sys.path.insert(0, str(host_root))
sys.path.insert(0, str(candidate))

from src.common.paths import get_instance_root
assert get_instance_root() == instance.resolve(), (get_instance_root(), instance)

from src.common.database.database import initialize_database, _DB_FILE
assert _DB_FILE.resolve() == (instance / "data/MaiBot.db").resolve(), _DB_FILE
log_phase("db_init_start")
initialize_database()
log_phase("db_init_done")

from maibot_sdk.context import PluginContext
from src.plugin_runtime.capabilities.core import RuntimeCoreCapabilityMixin
from src.plugin_runtime.capabilities import web as host_web
from src.plugin_runtime.runner.plugin_paths import build_plugin_paths
from maisya.abilities import Profile
from maisya.learning import Learning
from maisya.scout import Scout
from plugin import PLUGIN_ID, create_plugin


class HostCaps(RuntimeCoreCapabilityMixin):
    pass


async def run():
    from src.config.config import model_config

    live_web_assigned = bool(model_config.model_task_config.web_search.model_list)
    if not live_web_assigned and model_config.web.services:
        model_config.model_task_config.web_search.model_list = [
            next(iter(model_config.web.services))
        ]

    t0 = time.monotonic()
    caps = HostCaps()
    plugin = create_plugin()
    stats = {
        "llm_calls": 0,
        "web_search_calls": 0,
        "web_search_ok": 0,
        "timeout_ms_seen": [],
    }

    async def rpc(method, plugin_id, payload, **kwargs):
        assert method == "cap.call", method
        cap = payload["capability"]
        args = payload["args"]
        timeout_ms = kwargs.get("timeout_ms")
        if timeout_ms is not None:
            stats["timeout_ms_seen"].append((cap, timeout_ms))
        timeout_s = (timeout_ms / 1000.0) if timeout_ms else 30.0
        log_phase("rpc_start", capability=cap, timeout_ms=timeout_ms, task_name=args.get("task_name"))
        if cap == "config.get":
            return {"success": True, "value": []}
        if cap == "llm.generate_with_tools":
            stats["llm_calls"] += 1
            try:
                res = await asyncio.wait_for(
                    caps._cap_llm_generate_with_tools(plugin_id, cap, args),
                    timeout=timeout_s,
                )
            except Exception as exc:
                log_phase("rpc_error", capability=cap, error_type=type(exc).__name__)
                raise RuntimeError(str(exc)) from exc
            log_phase("rpc_done", capability=cap, success=bool(isinstance(res, dict) and res.get("success", True)), has_tools=bool(isinstance(res, dict) and res.get("tool_calls")))
            return res
        if cap in {"web.search", "web.fetch"}:
            if cap == "web.search":
                stats["web_search_calls"] += 1
            try:
                res = await asyncio.wait_for(host_web.call(plugin_id, cap, args), timeout=timeout_s)
            except Exception as exc:
                log_phase("rpc_error", capability=cap, error_type=type(exc).__name__)
                raise RuntimeError(str(exc)) from exc
            ok = bool(isinstance(res, dict) and res.get("success", True))
            if cap == "web.search" and ok and res.get("result", {}).get("results"):
                stats["web_search_ok"] += 1
            log_phase("rpc_done", capability=cap, success=ok)
            return res
        raise AssertionError(f"forbidden capability during isolated live smoke: {cap}")

    plugin._set_context(
        PluginContext(
            PLUGIN_ID,
            rpc_call=rpc,
            paths=build_plugin_paths(PLUGIN_ID, instance),
        )
    )
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "enabled_chats": ["smoke-chat"],
            "agent_max_steps": 4,
            "agent_timeout": 90,
        }
    )
    await plugin.on_load()
    log_phase("plugin_loaded")
    try:
        # 1. Real scout flow: LLM -> EXA web.search -> LLM synthesis
        res = await plugin.agent_tool(
            action="start",
            profile="scout",
            message="请先调用 web_search 搜索 Python 3.13 release date，然后用中文一两句话总结发布日期并附上至少一条搜索结果的完整 https 来源链接。",
            chat_id="smoke-chat",
            platform="qq",
            wait_seconds=30,
        )
        assert res["success"], res
        await plugin.engine.drain()
        row = plugin.threads.get(res["thread_id"], "smoke-chat")
        assert row["state"] == "idle", row
        scout_text = str(row["result"].get("text") or "").strip()
        assert scout_text, row
        assert stats["web_search_calls"] >= 1, stats
        assert stats["web_search_ok"] >= 1, stats
        assert stats["llm_calls"] <= 8, stats
        assert "http://" in scout_text or "https://" in scout_text, scout_text
        log_phase("scout_verified", web_search_calls=stats["web_search_calls"], web_search_ok=stats["web_search_ok"])

        # 2. Real maker preset planner slot routing check
        maker_profile = plugin.profiles.get("maker")
        assert maker_profile.task == "planner"
        maker_resp = await Scout(plugin.ctx).step(
            Profile(
                id="maker",
                task=maker_profile.task,
                tools=(),
                instructions="只用一句话回复确认即可，不要调用任何工具。",
            ),
            [{"role": "user", "content": "收到请简短确认"}],
        )
        assert isinstance(maker_resp, dict) and (maker_resp.get("response") or maker_resp.get("tool_calls"))
        log_phase("maker_verified", task=maker_profile.task)

        # 3. Real utils Learning._learn check on synthetic recovered trace
        learner = Learning(plugin.ctx, plugin.memory)
        with plugin.threads.connect() as db, db:
            db.execute(
                "INSERT OR IGNORE INTO learning_receipts VALUES (?, ?, 'pending')",
                (row["thread_id"], row["completed_revision"]),
            )
        await learner._learn(
            row,
            [
                {"tool": "web_search", "status": "error"},
                {"tool": "web_search", "status": "success"},
            ],
        )
        with plugin.threads.connect() as db:
            receipt = db.execute(
                "SELECT status FROM learning_receipts WHERE thread_id=? AND revision=?",
                (row["thread_id"], row["completed_revision"]),
            ).fetchone()
        learn_status = receipt[0] if receipt else "none"
        assert learn_status in {"applied", "empty", "full"}, learn_status

        log_phase(
            "live_smoke_passed",
            sdk=importlib.metadata.version("maibot-plugin-sdk"),
            instance_db_isolated=str(_DB_FILE.resolve()).startswith(str(instance.resolve())),
            live_web_assigned=live_web_assigned,
            llm_calls=stats["llm_calls"],
            web_search_calls=stats["web_search_calls"],
            web_search_ok=stats["web_search_ok"],
            timeout_ms_sample=stats["timeout_ms_seen"][:4],
            scout_preview=scout_text[:160],
            maker_planner_ok=True,
            learning_status=learn_status,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    finally:
        await plugin.on_unload()


asyncio.run(run())
"""

REMOTE = r"""
import base64, hashlib, io, json, os, shutil, subprocess, sys, tarfile, tempfile, time
from pathlib import Path

root = Path(sys.argv[1]).resolve()
os.umask(0o077)
stage = Path(tempfile.mkdtemp(prefix='maisya-live-smoke-'))
assert root not in stage.parents and 'plugins' not in stage.parts
os.chdir(stage)

def snapshot():
    digest = hashlib.sha256()
    for directory in (root / 'src', root / 'plugins'):
        for path in sorted(directory.rglob('*')):
            if path.is_file() and path.suffix in ('.py', '.json', '.toml'):
                stat = path.stat()
                digest.update(str(path.relative_to(root)).encode())
                digest.update(str((stat.st_size, stat.st_mtime_ns)).encode())
    for cfg in sorted((root / 'config').glob('*.toml')):
        digest.update(cfg.name.encode())
        digest.update(cfg.read_bytes())
    return digest.hexdigest()

before = snapshot()
status = 1
try:
    archive = base64.b64decode(ARCHIVE)
    assert hashlib.sha256(archive).hexdigest() == DIGEST
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as source:
        for member in source.getmembers():
            path = Path(member.name)
            assert member.isfile() and not path.is_absolute() and '..' not in path.parts
        source.extractall(stage / 'candidate', filter='data')
    instance = stage / 'instance'
    (instance / 'data/plugins/chartyr.maisya').mkdir(parents=True)
    shutil.copytree(root / 'config', instance / 'config')
    if (root / '.env').is_file():
        shutil.copy2(root / '.env', instance / '.env')
    (stage / 'home').mkdir()
    (stage / 'tmp').mkdir()
    (stage / 'smoke.py').write_text(SMOKE_SCRIPT)
    env = dict(
        os.environ,
        HOME=str(stage / 'home'),
        TMPDIR=str(stage / 'tmp'),
        MAIBOT_INSTANCE_DIR=str(instance),
        PYTHONDONTWRITEBYTECODE='1',
        PYTHONUNBUFFERED='1',
        PYTHONPATH=str(root),
    )
    proc = subprocess.Popen(
        [str(root / '.venv/bin/python'), '-u', '-B', str(stage / 'smoke.py'), str(stage), str(root)],
        cwd=stage,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 180
    while True:
        line = proc.stdout.readline()
        if line:
            print(line, end='', flush=True)
        elif proc.poll() is not None:
            break
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            print(json.dumps({'phase': 'smoke_timed_out'}), flush=True)
            break
    rc = proc.returncode if proc.returncode is not None else 1
    unchanged = snapshot() == before
    print(json.dumps({'host_unchanged': unchanged, 'exit_code': rc}), flush=True)
    status = rc if unchanged else 1
finally:
    os.chdir(stage.parent)
    shutil.rmtree(stage)
    print(json.dumps({'isolated_directory_cleaned': not stage.exists()}), flush=True)
sys.exit(status)
"""


def main():
    if "--execute" not in sys.argv:
        print(
            "隔离实模联网冒烟计划：在宿主 /tmp/maisya-live-smoke-* 中以独立 MAIBOT_INSTANCE_DIR 运行候选插件，仅调用只读 llm.generate_with_tools 与 web.search 真实链路，禁止任何 send.*/proactive.trigger 与写入原 data/，结束后验证宿主未改并清理临时目录。"
        )
        return
    local = (ROOT / "AGENTS.local.md").read_text()
    ssh = shlex.split(re.search(r"ssh [^\n`]+", local).group())
    ssh[ssh.index("-i") + 1] = str(Path(ssh[ssh.index("-i") + 1]).expanduser())
    host = re.search(r"宿主目录：`([^`]+)`", local).group(1)
    archive = io.BytesIO()
    paths = [
        ROOT / name
        for name in ("plugin.py", "__init__.py", "_manifest.json", "webui.json")
    ]
    paths += list((ROOT / "maisya").glob("*.py"))
    paths += [path for path in (ROOT / "presets").rglob("*") if path.is_file()]
    paths += list((ROOT / "webui").glob("*.html"))
    with tarfile.open(fileobj=archive, mode="w:gz") as target:
        for path in paths:
            if path.is_symlink():
                raise ValueError("候选代码不能包含符号链接")
            target.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    payload = archive.getvalue()
    script = (
        "ARCHIVE="
        + repr(base64.b64encode(payload).decode())
        + "\nDIGEST="
        + repr(hashlib.sha256(payload).hexdigest())
        + "\nSMOKE_SCRIPT="
        + repr(SMOKE_SCRIPT)
        + "\n"
        + REMOTE
    )
    result = subprocess.run(
        ssh + ["-o", "BatchMode=yes", shlex.join(["python3", "-", host])],
        input=script,
        text=True,
        capture_output=True,
        timeout=240,
        check=False,
    )
    log = ROOT / ".local-checks/live-smoke.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout + result.stderr)
    print(result.stdout)
    if result.returncode:
        print("实模联网冒烟失败，详细日志保存在 .local-checks/live-smoke.log")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
