from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from maisya.abilities import Profile, Profiles
from maisya.context_budget import is_context_overflow_error
from maisya.maker import AgentRuntime
from maisya.memory import MemoryLayers
import maisya.pathsafe as pathsafe
from maisya.sandbox import NetworkPolicy, ProcessResult, Sandbox, SandboxSettings
from maisya.scout import Scout, validate_arguments
from maisya.threads import ThreadEngine, ThreadStore
from maisya.vault import Vault
from maisya.workspaces import Workspaces


class _RecordingDockerRunner:
    """Real command-line level fake runner recording docker/nft invocations."""

    def __init__(self, *, active_hold: asyncio.Event | None = None):
        self.calls: list[list[str]] = []
        self.create_args: list[list[str]] = []
        self.networks: dict[str, str] = {}
        self.nft_tables: dict[str, str] = {}
        self.containers: dict[str, str] = {}
        self.active_hold = active_hold
        self.entered = asyncio.Event()
        self.executed_commands: list[list[str]] = []
        self._net_seq = 0

    async def __call__(self, arguments, **kwargs):
        cmd = list(arguments)
        self.calls.append(cmd)
        if cmd[0] == "nft":
            if cmd[1:3] == ["list", "tables"]:
                return ProcessResult(
                    0,
                    "\n".join(f"table inet {name}" for name in sorted(self.nft_tables)),
                )
            if cmd[1:3] == ["-f", "-"]:
                stdin = (kwargs.get("stdin") or b"").decode("utf-8", errors="replace")
                for line in stdin.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 3 and parts[0] == "table" and parts[1] in {"inet", "bridge"}:
                        self.nft_tables[parts[2].rstrip("{")] = stdin
                return ProcessResult(0, "")
            if cmd[1:3] == ["list", "table"]:
                table = cmd[4]
                if table in self.nft_tables:
                    return ProcessResult(0, self.nft_tables[table])
                return ProcessResult(1, "no such table")
            if cmd[1:3] == ["delete", "table"]:
                self.nft_tables.pop(cmd[4], None)
                return ProcessResult(0, "")
        if cmd[0] == "ip":
            if "route" in cmd:
                return ProcessResult(0, "[]")
            if "address" in cmd:
                payload = [
                    {
                        "ifname": bridge,
                        "addr_info": [{"family": "inet", "local": "10.200.0.1"}],
                    }
                    for bridge in self.networks.values()
                ]
                return ProcessResult(0, json.dumps(payload))
        if cmd[0] == "iptables":
            return ProcessResult(0, "")
        if cmd[0] != "docker":
            return ProcessResult(0, "")

        # docker --host ... <op> ...
        op = cmd[3]
        if op == "network":
            sub = cmd[4]
            if sub == "create":
                name = cmd[-1]
                self._net_seq += 1
                bridge = f"br-{self._net_seq:012x}"
                for idx, token in enumerate(cmd):
                    if isinstance(token, str) and "com.docker.network.bridge.name=" in token:
                        bridge = token.split("=", 1)[1]
                self.networks[name] = bridge
                return ProcessResult(0, name)
            if sub == "inspect":
                name = cmd[-1]
                bridge = self.networks.get(name, f"br-{name[:12]}")
                return ProcessResult(0, bridge)
            if sub == "rm":
                for name in cmd[5:]:
                    self.networks.pop(name, None)
                return ProcessResult(0, "")
            if sub == "ls":
                return ProcessResult(0, "")
        if op == "create":
            self.create_args.append(cmd)
            name = cmd[cmd.index("--name") + 1]
            self.containers[name] = str(cmd[-1])
            self.executed_commands.append(cmd)
            return ProcessResult(0, name)
        if op == "start":
            self.entered.set()
            if self.active_hold is not None:
                await self.active_hold.wait()
            name = cmd[-1]
            return ProcessResult(0, self.containers.get(name, "ok"))
        if op == "wait":
            return ProcessResult(0, "0")
        if op == "logs":
            name = cmd[-1]
            return ProcessResult(0, self.containers.get(name, "ok"))
        if op == "inspect":
            return ProcessResult(
                0, json.dumps({"Running": False, "OOMKilled": False, "ExitCode": 0})
            )
        if op == "rm":
            container = cmd[-1]
            self.containers.pop(container, None)
            return ProcessResult(0, "")
        if op == "ps":
            if "--filter" in cmd:
                flt = cmd[cmd.index("--filter") + 1]
                if flt.startswith("name=^/") and flt.endswith("$"):
                    target = flt[len("name=^/") : -1]
                    return ProcessResult(0, target if target in self.containers else "")
            return ProcessResult(0, "\n".join(sorted(self.containers)))
        raise AssertionError(f"unexpected runner command: {cmd}")


# ---------------------------------------------------------------------------
# Item 1: ThreadStore.enqueue grant merge
# ---------------------------------------------------------------------------


def test_b2_enqueue_merges_grant_preserving_privileges_and_source(tmp_path: Path):
    store = ThreadStore(tmp_path / "state.db")
    row = store.create(
        Profile(id="scout", access="admins"),
        "initial admin direct run",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="100001",
        grant={
            "admin": True,
            "admin_direct": True,
            "scheduled_admin": True,
            "source_chat_id": "qq:private:100001",
            "source_user_id": "100001",
            "source_msg_id": "msg-admin-1",
        },
    )

    # Simulate console_send / follow-up enqueue passing partial grant
    store.enqueue(
        row["thread_id"],
        "console continuation",
        "qq:group:100246809",
        input_context={"grant": {"console_admin": True}},
    )
    after_console = store.get(row["thread_id"], "qq:group:100246809")
    assert after_console["grant"]["console_admin"] is True
    assert after_console["grant"]["admin_direct"] is True
    assert after_console["grant"]["scheduled_admin"] is True
    assert after_console["grant"]["source_chat_id"] == "qq:private:100001"

    # Follow-up with empty strings or False privilege markers (without explicit admin_direct=False) must not erase markers
    store.enqueue(
        row["thread_id"],
        "admin follow-up",
        "qq:group:100246809",
        input_context={
            "grant": {
                "source_msg_id": "msg-admin-2",
                "source_chat_id": "",
                "scheduled_admin": False,
                "admin": False,
            }
        },
    )
    after_followup = store.get(row["thread_id"], "qq:group:100246809")
    assert after_followup["grant"]["source_msg_id"] == "msg-admin-2"
    assert after_followup["grant"]["source_chat_id"] == "qq:private:100001"
    assert after_followup["grant"]["admin_direct"] is True
    assert after_followup["grant"]["scheduled_admin"] is True
    assert after_followup["grant"]["console_admin"] is True
    assert after_followup["grant"]["admin"] is True

    # Explicit admin_direct=False (#66 cross-chat switch to target/none) clears preview stickiness only
    store.enqueue(
        row["thread_id"],
        "switch to target delivery",
        "qq:group:100246809",
        input_context={"grant": {"admin_direct": False}},
    )
    after_target_switch = store.get(row["thread_id"], "qq:group:100246809")
    assert after_target_switch["grant"]["admin_direct"] is False
    assert after_target_switch["grant"]["scheduled_admin"] is True
    assert after_target_switch["grant"]["console_admin"] is True
    assert after_target_switch["grant"]["admin"] is True
    assert after_target_switch["grant"]["source_chat_id"] == "qq:private:100001"


# ---------------------------------------------------------------------------
# Item 2: ThreadEngine capacity TOCTOU, exact-revision waiters, cleanup
# ---------------------------------------------------------------------------


class _TwoStepGateRuntime:
    def __init__(self):
        self.step_entered = {}
        self.step_gates = {}
        self.call_log = []

    async def prepare(self, row, metadata):
        return []

    async def step(self, profile, history):
        last_user = next(
            (m["content"] for m in reversed(history) if m.get("role") == "user"),
            "",
        )
        tag = "rev2" if "msg-2" in str(last_user) else "rev1"
        self.call_log.append(tag)
        ev = self.step_entered.setdefault(tag, asyncio.Event())
        ev.set()
        gate = self.step_gates.setdefault(tag, asyncio.Event())
        await gate.wait()
        return {"success": True, "response": f"done_{tag}", "tool_calls": []}

    async def call(self, name, arguments, context):
        return {"ok": True}

    async def end(self, thread_id):
        return None


@pytest.mark.asyncio
async def test_b2_engine_start_capacity_toctou_leaves_no_orphan_row(tmp_path: Path):
    store = ThreadStore(tmp_path / "state.db")
    runtime = _TwoStepGateRuntime()
    engine = ThreadEngine(store, runtime, max_active=1)

    entered_get = threading.Event()
    release_get = threading.Event()
    orig_get = store.get
    get_count = 0

    def gated_get(thread_id, chat_id, *, allow_self=False):
        nonlocal get_count
        get_count += 1
        row = orig_get(thread_id, chat_id, allow_self=allow_self)
        # Call 1 is at the tail of store.create; call 2 is inside send() AFTER _release_reservation
        if get_count == 2 and row.get("processed_revision", 0) == 0:
            entered_get.set()
            assert release_get.wait(timeout=2.5)
        return row

    store.get = gated_get

    async def _run():
        t1 = asyncio.create_task(
            engine.start(
                Profile(), "first task", chat_id="chat-1", platform="qq", user_id="u1", wait_seconds=0
            )
        )
        for _ in range(100):
            if entered_get.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered_get.is_set()

        # Concurrent second start while slot 1 is inside send() before tasks[tid] exists
        t2 = asyncio.create_task(
            engine.start(
                Profile(), "second task", chat_id="chat-2", platform="qq", user_id="u2", wait_seconds=0
            )
        )
        await asyncio.sleep(0.05)

        release_get.set()
        results = await asyncio.gather(t1, t2, return_exceptions=True)
        assert not isinstance(results[0], BaseException)
        assert isinstance(results[1], (ValueError, RuntimeError))
        runtime.step_gates.setdefault("rev1", asyncio.Event()).set()
        await engine.drain()

        with store.connect() as db:
            rows = db.execute("SELECT thread_id, state FROM agent_threads").fetchall()
        assert len(rows) == 1

    try:
        await asyncio.wait_for(_run(), timeout=3.0)
    finally:
        release_get.set()
        store.get = orig_get


@pytest.mark.asyncio
async def test_b2_engine_waiters_match_exact_revision_on_reused_workspace(
    tmp_path: Path,
):
    store = ThreadStore(tmp_path / "state.db")
    runtime = _TwoStepGateRuntime()
    engine = ThreadEngine(store, runtime, max_active=2)
    profile = Profile(id="maker", environment="readonly", tools=("workspace_file",))

    async def _run():
        t1 = asyncio.create_task(
            engine.start(
                profile,
                "msg-1",
                chat_id="chat-1",
                platform="qq",
                user_id="u1",
                workspace="shared_ws",
                wait_seconds=2.5,
            )
        )
        await runtime.step_entered.setdefault("rev1", asyncio.Event()).wait()

        # Caller 2 reuses the active persistent workspace thread without steering rev1
        t2 = asyncio.create_task(
            engine.start(
                profile,
                "msg-2",
                chat_id="chat-1",
                platform="qq",
                user_id="u1",
                workspace="shared_ws",
                wait_seconds=2.5,
            )
        )
        # Wait until rev 2 is actually enqueued before finishing rev 1
        for _ in range(100):
            with store.connect() as db:
                row = db.execute(
                    "SELECT revision FROM agent_threads WHERE chat_id='chat-1'"
                ).fetchone()
            if row and row["revision"] >= 2:
                break
            await asyncio.sleep(0.01)
        assert row and row["revision"] >= 2

        runtime.step_gates["rev1"].set()
        res1 = await t1
        assert res1["result"]["text"] == "done_rev1"

        # rev2 must now run and return done_rev2 to t2 (NOT rev1's done_rev1!)
        await runtime.step_entered.setdefault("rev2", asyncio.Event()).wait()
        runtime.step_gates.setdefault("rev2", asyncio.Event()).set()
        res2 = await t2
        assert res2["result"]["text"] == "done_rev2"
        await engine.drain()

    await asyncio.wait_for(_run(), timeout=3.0)


@pytest.mark.asyncio
async def test_b2_engine_cleans_chats_locks_and_expired_waiters(tmp_path: Path):
    store = ThreadStore(tmp_path / "state.db")
    runtime = _TwoStepGateRuntime()
    engine = ThreadEngine(store, runtime, max_active=2)

    async def _run():
        # Timeout waiter on rev1 then let rev1 complete
        try:
            timed = await engine.start(
                Profile(), "slow", chat_id="chat-clean", platform="qq", user_id="u1", wait_seconds=0.05
            )
            tid = timed["thread_id"]
            assert timed["state"] == "active"
        finally:
            runtime.step_gates.setdefault("rev1", asyncio.Event()).set()
        await engine.drain()

        assert tid not in engine.chats
        assert tid not in engine.locks
        assert tid not in engine.waiters

        # Stopped thread also cleans chats/locks/waiters
        runtime.step_gates["rev1"] = asyncio.Event()
        runtime.step_entered["rev1"] = asyncio.Event()
        running = await engine.start(
            Profile(), "to stop", chat_id="chat-stop", platform="qq", user_id="u1", wait_seconds=0
        )
        stop_tid = running["thread_id"]
        await runtime.step_entered["rev1"].wait()
        await engine.stop(stop_tid, chat_id="chat-stop")
        assert stop_tid not in engine.chats
        assert stop_tid not in engine.locks
        assert stop_tid not in engine.waiters

    await asyncio.wait_for(_run(), timeout=3.0)


# ---------------------------------------------------------------------------
# Item 3: Step-scoped execution receipts & non-caching of interrupted / {}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b2_sandbox_step_scoped_receipt_and_interrupted_retry(tmp_path: Path):
    store = ThreadStore(tmp_path / "state.db")
    files = Workspaces(tmp_path / "workspaces", store)
    row = store.create(
        Profile(id="maker", environment="sandbox"),
        "multi step run",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Alice",
    )
    runner = _RecordingDockerRunner()
    sandbox = Sandbox(
        SandboxSettings(), store, runner=runner, redact=lambda v: v, authorized=lambda _r: True
    )
    sandbox.ready = True
    sandbox._own = lambda work: None

    async def _run():
        with files.activity(row) as work:
            work.write("project/a.txt", "hi")
            work.commit()

            r0 = await sandbox.execute(
                row, work, "echo STEP0", call_id="call_0", mode="none", step_index=0
            )
            r1 = await sandbox.execute(
                row, work, "echo STEP1", call_id="call_0", mode="none", step_index=1
            )
            assert r0["output"].strip().endswith("STEP0")
            assert r1["output"].strip().endswith("STEP1")
            assert r1.get("repeated") is not True
            assert len(runner.executed_commands) == 2

            # Same step_index + call_id STILL deduplicates as repeated
            r0_again = await sandbox.execute(
                row, work, "echo STEP0", call_id="call_0", mode="none", step_index=0
            )
            assert r0_again.get("repeated") is True
            assert len(runner.executed_commands) == 2

            # Seed interrupted + empty {} receipt -> MUST re-execute instead of short-circuiting {}
            sandbox._receipt(
                (row["thread_id"], row["revision"], "step:2:call_0"),
                container="c-int",
                status="interrupted",
                result={},
            )
            r2 = await sandbox.execute(
                row, work, "echo STEP2", call_id="call_0", mode="none", step_index=2
            )
            assert r2.get("repeated") is not True
            assert r2["output"].strip().endswith("STEP2")
            assert len(runner.executed_commands) == 3

            # Seed uncertain receipt -> anti-replay protection MUST STILL block re-execution
            sandbox._receipt(
                (row["thread_id"], row["revision"], "step:3:call_0"),
                container="c-unc",
                status="uncertain",
                result={"error": "container status unknown"},
            )
            r3 = await sandbox.execute(
                row, work, "echo STEP3", call_id="call_0", mode="none", step_index=3
            )
            assert r3["status"] == "uncertain"
            assert r3.get("repeated") is True
            assert len(runner.executed_commands) == 3

    await asyncio.wait_for(_run(), timeout=3.0)


def _write_profile(profiles_root: Path, profile: Profile) -> None:
    d = profiles_root / profile.id
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        f'title = {json.dumps(profile.title)}',
        f'task = {json.dumps(profile.task)}',
        f'environment = {json.dumps(profile.environment)}',
        f'network = {json.dumps(profile.network)}',
        f'access = {json.dumps(profile.access)}',
        f'tools = {json.dumps(list(profile.tools))}',
        f'mcp = {json.dumps(list(profile.mcp))}',
        f'apis = {json.dumps(list(profile.apis))}',
        f'secrets = {json.dumps(list(profile.secrets))}',
        f'template = {json.dumps(profile.template)}',
        f'sandbox_targets = {json.dumps(list(profile.sandbox_targets))}',
    ]
    if profile.max_steps is not None:
        lines.append(f"max_steps = {int(profile.max_steps)}")
    if profile.context_tokens is not None:
        lines.append(f"context_tokens = {int(profile.context_tokens)}")
    (d / "profile.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "PROFILE.md").write_text(profile.instructions or "# test\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_b2_drive_injects_step_index_isolating_call0_for_sandbox_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = Vault(tmp_path / "secrets.vault")
    store = ThreadStore(tmp_path / "state.db")
    profiles = Profiles(tmp_path / "data/profiles")
    memory = MemoryLayers(tmp_path / "data", store, profiles, vault)

    mcp_calls: list[dict] = []
    (tmp_path / "data/abilities").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data/abilities/services.json").write_text(
        json.dumps(
            {
                "servers": {
                    "docs": {
                        "command": [sys.executable, "-c", "pass"],
                        "tools": ["search"],
                        "readonly": True,
                        "access": "everyone",
                        "description": "docs search",
                    }
                },
                "apis": {},
                "templates": {},
            }
        ),
        encoding="utf-8",
    )

    class _FakeSession:
        tools = {"search": {"inputSchema": {"type": "object"}}}

        async def request(self, method, params):
            args = dict(params.get("arguments") or {})
            mcp_calls.append(args)
            return {
                "content": [
                    {"type": "text", "text": json.dumps({"ok": True, "q": args.get("q")})}
                ]
            }

        async def close(self):
            pass

    async def fake_session(service, context, config):
        return _FakeSession()

    rt = AgentRuntime(
        None,
        tmp_path / "data",
        store,
        SandboxSettings(enabled=True),
        vault,
        authorized=lambda row: True,
    )
    rt.memory = memory
    rt.files.volume = None
    rt.services._session = fake_session
    rt.sandbox.ready = True
    rt.sandbox._own = lambda work: None
    runner = _RecordingDockerRunner()
    rt.docker_runner.cli_runner = runner

    profile = Profile(
        id="step_iso",
        environment="sandbox",
        network="none",
        tools=("sandbox_exec", "mcp_call"),
        mcp=("docs",),
    )
    _write_profile(profiles.path, profile)

    step_turn = 0

    async def fake_step(self, prof, history):
        nonlocal step_turn
        step_turn += 1
        if step_turn == 1:
            return {
                "success": True,
                "response": "",
                "tool_calls": [
                    {
                        "id": "call_0",
                        "function": {
                            "name": "sandbox_exec",
                            "arguments": json.dumps({"command": "echo S0"}),
                        },
                    },
                    {
                        "id": "call_m",
                        "function": {
                            "name": "mcp_call",
                            "arguments": json.dumps(
                                {"service": "docs", "tool": "search", "arguments": {"q": "first"}}
                            ),
                        },
                    },
                ],
            }
        if step_turn == 2:
            return {
                "success": True,
                "response": "",
                "tool_calls": [
                    {
                        "id": "call_0",
                        "function": {
                            "name": "sandbox_exec",
                            "arguments": json.dumps({"command": "echo S1"}),
                        },
                    },
                    {
                        "id": "call_m",
                        "function": {
                            "name": "mcp_call",
                            "arguments": json.dumps(
                                {"service": "docs", "tool": "search", "arguments": {"q": "second"}}
                            ),
                        },
                    },
                ],
            }
        return {"success": True, "response": "ALL_STEPS_OK", "tool_calls": []}

    monkeypatch.setattr("maisya.maker.Scout.step", fake_step)
    engine = ThreadEngine(store, rt, max_active=2)

    async def _run():
        started = await engine.start(
            profile,
            "run two steps with identical call_0",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="Alice",
            wait_seconds=2.5,
        )
        assert started["result"].get("text") == "ALL_STEPS_OK", started
        assert len(runner.executed_commands) == 2
        assert [c["q"] for c in mcp_calls] == ["first", "second"]
        await engine.drain()
        await rt.close()

    await asyncio.wait_for(_run(), timeout=3.0)


# ---------------------------------------------------------------------------
# Item 4: Restricted --add-host, network cap eviction, async .resolv.conf
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b2_sandbox_restricted_add_host_no_dns_network_eviction_and_to_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ThreadStore(tmp_path / "state.db")
    files = Workspaces(tmp_path / "workspaces", store)
    row = store.create(
        Profile(id="maker", environment="sandbox"),
        "network test",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Alice",
    )
    runner = _RecordingDockerRunner()
    sandbox = Sandbox(
        SandboxSettings(dns=("1.1.1.1",)),
        store,
        runner=runner,
        redact=lambda v: v,
        authorized=lambda _r: True,
    )
    sandbox.ready = True
    sandbox._own = lambda work: None

    # Record threads used when creating .resolv.conf under public mode
    loop_tid = threading.get_ident()
    write_tids: list[int] = []
    orig_write_text = Path.write_text

    def tracked_write_text(self: Path, data: str, *args, **kwargs):
        if self.name.startswith("resolv.") or ".resolv" in self.name:
            write_tids.append(threading.get_ident())
        return orig_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", tracked_write_text)

    async def _run():
        with files.activity(row) as work:
            work.write("project/x.txt", "1")
            work.commit()

            # 1) restricted mode: injects --add-host for domain targets, MUST NOT inject --dns
            res_restricted = await sandbox.execute(
                row,
                work,
                "curl https://api.example.com",
                call_id="c-rest",
                mode="restricted",
                targets=[("93.184.216.34", 443)],
                host_mappings={"api.example.com": "93.184.216.34"},
            )
            assert res_restricted["exit_code"] == 0
            restricted_cmd = runner.create_args[-1]
            assert "--add-host" in restricted_cmd
            idx = restricted_cmd.index("--add-host")
            assert restricted_cmd[idx + 1] == "api.example.com:93.184.216.34"
            assert "--dns" not in restricted_cmd
            assert not any("resolv" in arg for arg in restricted_cmd)

            # 2) public mode: still injects --dns and writes .resolv.conf off the event loop
            await sandbox.execute(
                row,
                work,
                "curl https://example.org",
                call_id="c-pub",
                mode="public",
            )
            public_cmd = runner.create_args[-1]
            assert "--dns" in public_cmd
            assert write_tids and all(tid != loop_tid for tid in write_tids)

            # 3) >8 sequential distinct targets reclaim idle network tables cleanly
            for i in range(1, 11):
                res = await sandbox.execute(
                    row,
                    work,
                    f"echo net{i}",
                    call_id=f"c-net-{i}",
                    mode="restricted",
                    targets=[(f"93.184.216.{i}", 443)],
                )
                assert res["exit_code"] == 0
            assert len(sandbox.networks) <= 8

    await asyncio.wait_for(_run(), timeout=3.0)


@pytest.mark.asyncio
async def test_b2_sandbox_refuses_eviction_when_all_8_networks_active(tmp_path: Path):
    store = ThreadStore(tmp_path / "state.db")
    files = Workspaces(tmp_path / "workspaces", store)
    row = store.create(
        Profile(id="maker", environment="sandbox"),
        "8 concurrent active networks",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Alice",
    )
    hold = asyncio.Event()
    runner = _RecordingDockerRunner(active_hold=hold)
    sandbox = Sandbox(
        SandboxSettings(max_concurrent=8),
        store,
        runner=runner,
        redact=lambda v: v,
        authorized=lambda _r: True,
    )
    sandbox.slot = asyncio.Semaphore(10)
    sandbox.ready = True
    sandbox._own = lambda work: None

    async def _run():
        with files.activity(row) as work:
            work.write("project/x.txt", "1")
            work.commit()

            tasks = [
                asyncio.create_task(
                    sandbox.execute(
                        row,
                        work,
                        "sleep 10",
                        call_id=f"active-{i}",
                        mode="restricted",
                        targets=[(f"93.184.216.{i}", 443)],
                    )
                )
                for i in range(1, 9)
            ]
            # Wait until all 8 networks are occupied by running containers
            for _ in range(100):
                if len(runner.containers) == 8:
                    break
                await asyncio.sleep(0.01)
            assert len(runner.containers) == 8

            try:
                with pytest.raises(RuntimeError, match="网络策略过多"):
                    await sandbox.execute(
                        row,
                        work,
                        "echo 9th",
                        call_id="active-9",
                        mode="restricted",
                        targets=[("93.184.216.9", 443)],
                    )
            finally:
                hold.set()
                await asyncio.gather(*tasks)

    await asyncio.wait_for(_run(), timeout=3.0)


@pytest.mark.asyncio
async def test_b2_maker_resolves_restricted_domain_targets_to_add_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ThreadStore(tmp_path / "state.db")
    vault = Vault(tmp_path / "secrets.vault")
    profile = Profile(
        id="restricted_maker",
        access="admins",
        environment="sandbox",
        network="restricted",
        tools=("sandbox_exec",),
        sandbox_targets=("api.example.com:443",),
    )
    rt = AgentRuntime(
        None,
        tmp_path / "data",
        store,
        SandboxSettings(enabled=True),
        vault,
        authorized=lambda row: True,
    )
    rt.sandbox.ready = True
    rt.sandbox._own = lambda work: None
    rt.files.volume = None
    runner = _RecordingDockerRunner()
    rt.docker_runner.cli_runner = runner
    recorded_targets: list[tuple[str, ...]] = []
    orig_execute = rt.docker_runner.execute

    async def spy_execute(req):
        recorded_targets.append(tuple(req.targets))
        return await orig_execute(req)

    rt.docker_runner.execute = spy_execute

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    row = store.create(
        profile,
        "fetch api",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Admin",
        grant={"admin": True, "source_msg_id": "m-admin"},
    )
    ctx = {**row, "profile_snapshot": asdict(profile), "_tool_call_id": "c-1", "_step_index": 0}
    res = await asyncio.wait_for(
        rt.call("sandbox_exec", {"command": "echo hi"}, ctx), timeout=3.0
    )
    assert res["exit_code"] == 0
    assert runner.create_args and recorded_targets == [("api.example.com:443",)]
    await rt.close()


# ---------------------------------------------------------------------------
# Item 5: Tier-2 compaction, Scout schema/overflow, Maker resolve_profile & attachments
# ---------------------------------------------------------------------------


def test_b2_tier2_compaction_truncates_tool_arguments_and_force_false_tail(tmp_path: Path):
    store = ThreadStore(tmp_path / "state.db")
    profile = Profile(id="maker")
    row = store.create(profile, "initial", chat_id="qq:group:100246809", platform="qq", user_id="u1")

    huge_args = json.dumps({"action": "write", "path": "out/big.txt", "content": "A" * 12000})
    block = [
        {
            "role": "assistant",
            "content": "writing large file",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "workspace_file", "arguments": huge_args},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "B" * 4000},
    ]
    truncated = store._truncate_tool_block(block, target_chars=700, archive_seq=42)
    encoded_len = len(json.dumps(truncated, ensure_ascii=False))
    assert encoded_len <= 700, f"truncated tool block still too large: {encoded_len}"
    assert truncated[0]["tool_calls"][0]["id"] == "call_1"
    assert truncated[1]["tool_call_id"] == "call_1"
    parsed_args = json.loads(truncated[0]["tool_calls"][0]["function"]["arguments"])
    assert isinstance(parsed_args, dict)
    assert "42" in json.dumps(parsed_args, ensure_ascii=False)

    # Single oversized trailing user message MUST shrink even when force=False
    big_user_history = [{"role": "user", "content": "X" * 30000}]
    store.enqueue(row["thread_id"], "initial", "qq:group:100246809")
    store.checkpoint(
        row["thread_id"],
        big_user_history,
        row["revision"],
        "tools",
        {},
    )
    store.compact(
        row["thread_id"],
        "qq:group:100246809",
        max_tokens=2000,
        tail_tokens=400,
        force=False,
    )
    compacted = store.get(row["thread_id"], "qq:group:100246809")["history"]
    assert len(json.dumps(compacted, ensure_ascii=False)) < 4000
    assert "event_seq=" in compacted[-1]["content"]


@pytest.mark.asyncio
async def test_b2_tier2_compact_skips_llm_summary_on_noop_and_scout_fast_overflow(
    tmp_path: Path,
):
    store = ThreadStore(tmp_path / "state.db")
    profile = Profile(
        id="scout",
        model="primary-model",
        fallback_models=("fb-1", "fb-2"),
    )
    row = store.create(profile, "tiny prompt", chat_id="qq:group:100246809", platform="qq", user_id="u1")

    seen_models: list[str] = []

    class _OverflowHost:
        async def call_capability(self, name, **arguments):
            if name == "llm.generate_with_tools":
                seen_models.append(arguments.get("model", ""))
                raise RuntimeError("This model's maximum context length is 16384 tokens")
            raise AssertionError(name)

    scout = Scout(_OverflowHost())
    with pytest.raises(RuntimeError, match="maximum context length"):
        await asyncio.wait_for(
            scout.step(profile, [{"role": "user", "content": "hello"}]), timeout=3.0
        )
    # Must re-raise immediately after primary model instead of exhausting all fallbacks
    assert len(seen_models) == 1

    # digest_feed schema accepts query + total_candidates
    validate_arguments(
        "digest_feed",
        {"action": "collect", "query": "LLM inference", "total_candidates": 42},
    )

    # _run_tier2_compact must NOT burn summarize_compaction LLM call when compaction makes no reduction
    summarize_calls = 0

    class _DummyRuntime:
        async def summarize_compaction(self, *args, **kwargs):
            nonlocal summarize_calls
            summarize_calls += 1
            return "summary"

        async def prepare(self, r, m):
            return []

        async def step(self, p, h):
            return {"success": True, "response": "ok", "tool_calls": []}

        async def call(self, n, a, c):
            return {}

        async def end(self, tid):
            return None

    engine = ThreadEngine(store, _DummyRuntime(), context_tokens=16384)
    tiny_history = [{"role": "user", "content": "short"}]
    context_index, out_history = await asyncio.wait_for(
        engine._run_tier2_compact(
            row["thread_id"],
            "qq:group:100246809",
            profile,
            tiny_history,
            row["processed_revision"],
            budget={
                "max_tokens": 16384,
                "trigger_tokens": 8000,
                "prune_tokens": 6000,
                "tail_tokens": 2000,
            },
            overhead_tokens=7950,
            force=False,
        ),
        timeout=3.0,
    )
    assert out_history == tiny_history
    assert context_index == ""
    assert summarize_calls == 0


@pytest.mark.asyncio
async def test_b2_maker_unified_resolve_profile_and_attachment_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = Vault(tmp_path / "secrets.vault")
    store = ThreadStore(tmp_path / "state.db")
    profiles = Profiles(tmp_path / "data/profiles")
    memory = MemoryLayers(tmp_path / "data", store, profiles, vault)

    # Save an admins-only live profile, start thread with max_steps=8, sandbox_targets=()
    p_v1 = Profile(
        id="live_editor",
        access="admins",
        environment="sandbox",
        network="restricted",
        tools=("workspace_file", "sandbox_exec", "result_attach"),
        max_steps=8,
        sandbox_targets=(),
    )
    _write_profile(profiles.path, p_v1)

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    rt = AgentRuntime(
        None,
        tmp_path / "data",
        store,
        SandboxSettings(enabled=True),
        vault,
        authorized=lambda row: True,
    )
    rt.memory = memory
    rt.sandbox.ready = True
    rt.sandbox._own = lambda work: None
    rt.files.volume = None
    runner = _RecordingDockerRunner()
    rt.docker_runner.cli_runner = runner
    recorded_targets: list[tuple[str, ...]] = []
    orig_execute = rt.docker_runner.execute

    async def spy_execute(req):
        recorded_targets.append(tuple(req.targets))
        return await orig_execute(req)

    rt.docker_runner.execute = spy_execute

    # Update live profile on disk BEFORE running step/call
    p_v2 = Profile(
        id="live_editor",
        access="admins",
        environment="sandbox",
        network="restricted",
        tools=("workspace_file", "sandbox_exec", "result_attach"),
        max_steps=2,
        sandbox_targets=("api.example.com:443",),
    )
    _write_profile(profiles.path, p_v2)

    row = store.create(
        p_v1,
        "test live profile + attachments",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Admin",
        workspace="live_ws",
        grant={"admin": True, "source_msg_id": "m-admin"},
    )

    resolved_in_drive: list[int | None] = []
    step_count = 0

    async def fake_step(self, prof, history):
        nonlocal step_count
        step_count += 1
        resolved_in_drive.append(prof.max_steps)
        if step_count == 1:
            return {
                "success": True,
                "response": "",
                "tool_calls": [
                    {
                        "id": "call-sb",
                        "function": {
                            "name": "sandbox_exec",
                            "arguments": json.dumps({"command": "echo live_target"}),
                        },
                    }
                ],
            }
        return {"success": True, "response": "done_step2", "tool_calls": []}

    monkeypatch.setattr("maisya.maker.Scout.step", fake_step)
    engine = ThreadEngine(store, rt, max_active=2)

    res = await asyncio.wait_for(
        engine.send(
            row["thread_id"],
            "go",
            chat_id="qq:group:100246809",
            wait_seconds=2.5,
        ),
        timeout=3.0,
    )
    # step and call MUST see live max_steps=2 and sandbox_targets=("api.example.com:443",)
    assert resolved_in_drive == [2, 2]
    assert runner.create_args and recorded_targets == [("api.example.com:443",)]
    assert res["result"]["text"] == "done_step2"

    # Non-escalation: if live profile drops sandbox_targets & tightens to admins, stale snapshot cannot call it
    p_v3 = Profile(
        id="live_editor",
        access="admins",
        environment="sandbox",
        network="restricted",
        tools=("workspace_file", "sandbox_exec", "result_attach"),
        max_steps=2,
        sandbox_targets=(),
    )
    _write_profile(profiles.path, p_v3)
    bad_ctx = {
        **row,
        "profile_snapshot": asdict(p_v2),
        "_tool_call_id": "c-stale",
        "_step_index": 0,
    }
    res_removed = await rt.call("sandbox_exec", {"command": "echo hi"}, bad_ctx)
    assert res_removed["exit_code"] == 0
    assert recorded_targets[-1] == ()

    # Also verify tightening access="everyone" -> "admins" on live profile rejects unprivileged row
    p_orig_everyone = Profile(
        id="live_tighten",
        access="everyone",
        environment="readonly",
        tools=("workspace_file",),
    )
    _write_profile(
        profiles.path,
        Profile(
            id="live_tighten",
            access="admins",
            environment="readonly",
            tools=("workspace_file",),
        ),
    )
    tight_rt = AgentRuntime(
        None,
        tmp_path / "data",
        store,
        SandboxSettings(enabled=False),
        vault,
        authorized=lambda r: False,
    )
    tight_rt.memory = memory
    non_admin_row = store.create(
        p_orig_everyone,
        "tighten check",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Bob",
    )
    with pytest.raises(PermissionError):
        await tight_rt.call(
            "workspace_file",
            {"action": "list"},
            {**non_admin_row, "profile_snapshot": asdict(p_orig_everyone), "_tool_call_id": "c-t"},
        )
    await tight_rt.close()

    # Release tight_rt session lock? Wait — rt holds its own session lock on row["thread_id"], release first
    await rt.end(row["thread_id"])
    # Attachment retry limit (once per turn -> 2nd failure stops immediately), merge, & turn reset
    with rt.files.activity(row) as work:
        work.write("out/one.txt", "1")
        work.write("out/two.txt", "2")
        work.commit()

    # 1st bad result_attach raises specific error (preserved by _drive!)
    with pytest.raises((ValueError, OSError)):
        await rt.call("result_attach", {"paths": ["out/missing.txt"]}, {**row, "_tool_call_id": "ra-1"})
    # 2nd bad result_attach immediately returns retry-exhausted message (does NOT require a 3rd call!)
    second_fail = await rt.call(
        "result_attach", {"paths": ["out/missing.txt"]}, {**row, "_tool_call_id": "ra-2"}
    )
    assert "本轮附件已尝试修正一次" in str(second_fail.get("error", ""))

    # begin_turn resets attempts + stale attachments, then multiple result_attach calls merge cleanly
    rt.begin_turn(row["thread_id"])
    r_a = await rt.call("result_attach", {"paths": ["out/one.txt"]}, {**row, "_tool_call_id": "ra-3"})
    r_b = await rt.call("result_attach", {"paths": ["out/two.txt"]}, {**row, "_tool_call_id": "ra-4"})
    assert len(r_a["attachments"]) == 1
    assert len(r_b["attachments"]) == 1
    assert len(rt.attachments[row["thread_id"]]) == 2

    # Next turn clear removes stale attachments from previous turn
    rt.begin_turn(row["thread_id"])
    assert row["thread_id"] not in rt.attachments
    assert row["thread_id"] not in rt.attachment_attempts
    await engine.drain()
    await rt.close()


# ---------------------------------------------------------------------------
# Item 6: Workspaces locking, usage FileNotFoundError tolerance, cache/ quota, native path check
# ---------------------------------------------------------------------------


def test_b2_workspaces_cache_quota_checkpoint_lock_usage_and_pathsafe(tmp_path: Path):
    store = ThreadStore(tmp_path / "state.db")
    files = Workspaces(tmp_path / "workspaces", store, max_bytes=200, max_files=3)
    profile = Profile(id="maker", environment="readonly", tools=("workspace_file",))
    row = store.create(
        profile,
        "cache quota",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Alice",
        workspace="quota_ws",
    )

    with files.activity(row) as work:
        work.write_bytes("cache/a.bin", b"a" * 120)
        with pytest.raises(ValueError):
            work.write_bytes("cache/b.bin", b"b" * 120)
        work.write("out/ok.txt", "ok")
        work.commit()

    # Concurrent read_checkpoint_outputs while commit rotates versions must never raise FileNotFoundError
    errors: list[BaseException] = []
    stop_flag = threading.Event()

    def writer():
        try:
            for i in range(15):
                with files.activity(row) as work:
                    work.write("out/ok.txt", f"v{i}")
                    work.commit()
        except BaseException as exc:
            errors.append(exc)
        finally:
            stop_flag.set()

    def reader():
        try:
            while not stop_flag.is_set():
                outs = files.read_checkpoint_outputs(row)
                assert any(name == "ok.txt" for name, _data in outs)
        except BaseException as exc:
            errors.append(exc)

    t_w = threading.Thread(target=writer)
    t_r = threading.Thread(target=reader)
    t_w.start()
    t_r.start()
    t_w.join(timeout=5.0)
    t_r.join(timeout=5.0)
    assert not errors, errors

    # usage() must tolerate files deleted mid-scan (FileNotFoundError)
    ephemeral = files.root / "temp_gone.txt"
    ephemeral.write_text("hi", encoding="utf-8")
    real_lstat = Path.lstat
    deleted_once = False

    def flaky_lstat(self: Path):
        nonlocal deleted_once
        if self == ephemeral and not deleted_once:
            deleted_once = True
            ephemeral.unlink(missing_ok=True)
        return real_lstat(self)

    orig_lstat = Path.lstat
    Path.lstat = flaky_lstat
    try:
        total_bytes = files.usage()
        assert total_bytes >= 0
    finally:
        Path.lstat = orig_lstat

    # delete_thread must acquire workspace capacity lock during workspace deletion
    implicit_row = store.create(
        profile,
        "implicit delete lock check",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Alice",
    )
    with files.activity(implicit_row) as work:
        work.write("project/temp.txt", "temp")
        work.commit()

    cap_lock = files.root / ".capacity.lock"
    store.delete_thread(implicit_row["thread_id"], workspace_root=files.root)
    assert cap_lock.exists()

    # pathsafe contract: syntactic is_absolute_program_path preserved; is_native_absolute_program_path OS-aware
    is_native = getattr(pathsafe, "is_native_absolute_program_path", None)
    assert callable(is_native), "missing pathsafe.is_native_absolute_program_path"
    assert pathsafe.is_absolute_program_path("/usr/bin/python3") is True
    assert pathsafe.is_absolute_program_path(r"C:\Python313\python.exe") is True
    if os.name == "nt":
        assert is_native(r"C:\Python313\python.exe") is True
        assert is_native("/usr/bin/python3") is False
    else:
        assert is_native("/usr/bin/python3") is True
        assert is_native(r"C:\Python313\python.exe") is False
    assert is_native(r"\\evil\share\a.exe") is False
    assert is_native("//evil/share/a.exe") is False
    assert is_native("python3") is False


@pytest.mark.asyncio
async def test_b2_engine_auto_drain_fifo_fg_priority_drain_and_cleanup(tmp_path: Path):
    store = ThreadStore(tmp_path / "state.db")
    gates: dict[str, asyncio.Event] = {}
    entered: dict[str, asyncio.Event] = {}
    order: list[str] = []

    class _LaneRuntime:
        async def prepare(self, row, metadata):
            return []

        async def step(self, profile, history):
            tag = next(
                (str(m["content"]) for m in reversed(history) if m.get("role") == "user"),
                "",
            )
            order.append(tag)
            entered.setdefault(tag, asyncio.Event()).set()
            gate = gates.get(tag)
            if gate is not None:
                await gate.wait()
            return {"success": True, "response": f"ok:{tag}", "tool_calls": []}

        async def call(self, name, arguments, context):
            return {}

        async def end(self, thread_id):
            return None

    async def _run():
        # 1) Background lane full (max_bg=2 with max_active=4):
        #    queued fg behind a queued bg must NOT be starved when bg lane is full!
        for tag in ("bg1", "bg2", "fg1", "fg2"):
            gates[tag] = asyncio.Event()
        engine = ThreadEngine(store, _LaneRuntime(), max_active=4)
        try:
            for tag, is_bg in (("bg1", True), ("bg2", True), ("fg1", False), ("fg2", False)):
                await engine.start(
                    Profile(), tag, chat_id=f"c-{tag}", platform="qq", user_id="u1", wait_seconds=0, background=is_bg
                )
                await entered.setdefault(tag, asyncio.Event()).wait()

            # Defer a background thread (bg lane 2/2 full) AND a foreground thread (total 4/4 full), both auto_drain=True
            bg3 = await engine.start(
                Profile(),
                "bg3",
                chat_id="c-bg3",
                platform="qq",
                user_id="u1",
                wait_seconds=0,
                background=True,
                defer_if_full=True,
                auto_drain=True,
            )
            assert bg3["state"] == "active"
            assert store.get(bg3["thread_id"], "c-bg3")["completed_revision"] == 0

            fg3 = await engine.start(
                Profile(),
                "fg3",
                chat_id="c-fg3",
                platform="qq",
                user_id="u1",
                wait_seconds=0,
                background=False,
                defer_if_full=True,
                auto_drain=True,
            )
            assert fg3["state"] == "active"
            assert store.get(fg3["thread_id"], "c-fg3")["completed_revision"] == 0

            # Default auto_drain=False keeps manual kick_queued deferred behavior intact
            manual = await engine.start(
                Profile(),
                "manual_bg",
                chat_id="c-man",
                platform="qq",
                user_id="u1",
                wait_seconds=0,
                background=True,
                defer_if_full=True,
            )
            assert manual["state"] == "active"
            assert store.get(manual["thread_id"], "c-man")["completed_revision"] == 0

            # Enqueue stop-cancelled auto_drain entry to verify stop() cleans _deferred_queue
            to_stop = await engine.start(
                Profile(),
                "to_stop",
                chat_id="c-stop",
                platform="qq",
                user_id="u1",
                wait_seconds=0,
                background=False,
                defer_if_full=True,
                auto_drain=True,
            )
            await engine.stop(to_stop["thread_id"], chat_id="c-stop")

            # Release fg1 ONLY while bg1+bg2 still fill bg lane -> fg3 must self-drain even though bg3 is ahead in FIFO!
            gates["fg1"].set()
            await entered.setdefault("fg3", asyncio.Event()).wait()
            assert "bg3" not in entered
            assert store.get(manual["thread_id"], "c-man")["completed_revision"] == 0

            # Release remaining slots -> bg3 auto-drains, engine.drain() waits for all auto-drained tasks, manual stays untouched
            gates["bg1"].set()
            gates["bg2"].set()
            gates["fg2"].set()
            await engine.drain()

            assert store.get(fg3["thread_id"], "c-fg3")["completed_revision"] == 1
            assert store.get(bg3["thread_id"], "c-bg3")["completed_revision"] == 1
            assert store.get(manual["thread_id"], "c-man")["completed_revision"] == 0
            assert await engine.kick_queued(manual["thread_id"], "c-man", background=True) is True
            await engine.drain()
            assert store.get(manual["thread_id"], "c-man")["completed_revision"] == 1
            assert store.get(to_stop["thread_id"], "c-stop")["state"] == "idle"
        finally:
            await engine.close()

    await asyncio.wait_for(_run(), timeout=3.0)


@pytest.mark.asyncio
async def test_b2_drive_preserves_safe_tool_error_with_redaction(tmp_path: Path):
    store = ThreadStore(tmp_path / "state.db")
    seen_tool_contents: list[str] = []

    class _ErrRuntime:
        async def prepare(self, row, metadata):
            return []

        async def step(self, profile, history):
            for msg in history:
                if msg.get("role") == "tool":
                    seen_tool_contents.append(msg["content"])
            if not any(m.get("role") == "tool" for m in history):
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "call-err",
                            "function": {
                                "name": "html_render",
                                "arguments": json.dumps({"html_path": "out/x.html"}),
                            },
                        }
                    ],
                }
            return {"success": True, "response": "handled", "tool_calls": []}

        async def call(self, name, arguments, context):
            raise ValueError("HTML 输出超限 (>262144 bytes) secret=SECRET_TOK_123")

        async def end(self, thread_id):
            return None

    engine = ThreadEngine(
        store,
        _ErrRuntime(),
        redact=lambda val: str(val).replace("SECRET_TOK_123", "[REDACTED]"),
    )
    res = await asyncio.wait_for(
        engine.start(
            Profile(tools=("html_render",)),
            "render card",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="u1",
            wait_seconds=2.5,
        ),
        timeout=3.0,
    )
    assert res["result"]["text"] == "handled"
    assert seen_tool_contents
    assert "HTML 输出超限" in seen_tool_contents[0]
    assert "SECRET_TOK_123" not in seen_tool_contents[0]
    assert "[REDACTED]" in seen_tool_contents[0]
    await engine.drain()

