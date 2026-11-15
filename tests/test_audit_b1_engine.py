from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from maisya.abilities import Profile
from maisya.maker import AgentRuntime
from maisya.sandbox import ProcessResult, Sandbox, SandboxSettings
from maisya.threads import ThreadEngine, ThreadStore
from maisya.vault import Vault
from maisya.workspaces import Workspaces


class _CancellableDockerFixture:
    def __init__(self):
        self.running = set()
        self.entered = asyncio.Event()
        self.hold = asyncio.Event()
        self.rm_started = asyncio.Event()
        self.rm_gate = asyncio.Event()
        self.rm_returns_gone = True
        self.starts = 0
        self.terminates = 0

    async def __call__(self, arguments, **kwargs):
        operation = arguments[3]
        if operation == "create":
            name = arguments[arguments.index("--name") + 1]
            self.running.add(name)
            return ProcessResult(0, name)
        if operation == "start":
            self.starts += 1
            self.entered.set()
            await self.hold.wait()
            return ProcessResult(0, "")
        if operation == "inspect":
            return ProcessResult(
                0, json.dumps({"Running": False, "OOMKilled": False, "ExitCode": 0})
            )
        if operation == "rm":
            container = arguments[-1]
            self.terminates += 1
            self.rm_started.set()
            await self.rm_gate.wait()
            if self.rm_returns_gone:
                self.running.discard(container)
            return ProcessResult(0 if self.rm_returns_gone else 1, "")
        if operation == "ps":
            return ProcessResult(0, "\n".join(self.running))
        raise AssertionError(arguments)


class _Tier1PruneRuntime:
    def __init__(self):
        self.seen_histories = []
        self.turns = 0

    async def prepare(self, row, metadata):
        if metadata.get("references"):
            return [{"path": "in/ref.txt", "message_id": metadata["references"][0], "bytes": 12}]
        return []

    async def step(self, profile, history):
        self.seen_histories.append(json.loads(json.dumps(history)))
        self.turns += 1
        if self.turns == 1:
            return {
                "success": True,
                "response": "",
                "tool_calls": [
                    {
                        "id": "call_big_1",
                        "function": {
                            "name": "web_read",
                            "arguments": json.dumps({"url": "https://example.com/big"}),
                        },
                    }
                ],
            }
        if self.turns == 2:
            # Large assistant reply (> tail_tokens=4000) pushes the old tool output outside protected tail
            return {
                "success": True,
                "response": "ACK1 " + ("z" * 16500),
                "tool_calls": [],
            }
        return {
            "success": True,
            "response": "FINAL_OK",
            "tool_calls": [],
        }

    async def call(self, name, arguments, context):
        return {"content": "OLD_TOOL " + ("a" * 22500)}


@pytest.mark.asyncio
async def test_b1_tier1_prune_history_preserves_current_pending_and_thread_inputs(
    tmp_path: Path,
):
    store = ThreadStore(tmp_path / "state.db")
    runtime = _Tier1PruneRuntime()
    engine = ThreadEngine(store, runtime, context_tokens=16384)
    profile = Profile(
        id="prune_ws",
        environment="readonly",
        tools=("web_read", "workspace_file"),
        context_tokens=16384,
    )
    res1 = await engine.start(
        profile,
        "FIRST_TURN_PROMPT",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Alice",
        workspace="ws_prune",
        wait_seconds=10,
    )
    assert res1["state"] == "idle"

    res2 = await engine.send(
        res1["thread_id"],
        "SECOND_USER_MSG_MUST_NOT_VANISH",
        chat_id="qq:group:100246809",
        input_context={"references": ["m_ref_1"]},
        wait_seconds=10,
    )
    assert res2["state"] == "idle"
    assert res2["result"]["text"] == "FINAL_OK"

    # Turn 3 prompt delivered to runtime.step must still contain current input and <thread_inputs>
    step_turn3_blob = json.dumps(runtime.seen_histories[-1], ensure_ascii=False)
    assert "SECOND_USER_MSG_MUST_NOT_VANISH" in step_turn3_blob
    assert "<thread_inputs>" in step_turn3_blob
    assert "in/ref.txt" in step_turn3_blob

    row = store.get(res1["thread_id"], "qq:group:100246809")
    db_blob = json.dumps(row["history"], ensure_ascii=False)
    assert "SECOND_USER_MSG_MUST_NOT_VANISH" in db_blob
    assert "<thread_inputs>" in db_blob
    await engine.close()


@pytest.mark.asyncio
async def test_b1_drive_cancel_before_try_does_not_leak_slots_or_hang_waiters(
    tmp_path: Path,
):
    store = ThreadStore(tmp_path / "state.db")
    orig_get = store.get
    loop = asyncio.get_running_loop()
    first_get_entered = asyncio.Event()
    release_first_get = threading.Event()
    calls = 0

    def slow_first_get(*args, **kwargs):
        nonlocal calls
        if "allow_self" in kwargs and calls == 0:
            calls += 1
            loop.call_soon_threadsafe(first_get_entered.set)
            release_first_get.wait(timeout=2.0)
        return orig_get(*args, **kwargs)

    store.get = slow_first_get

    class _SimpleRuntime:
        async def step(self, profile, history):
            return {"success": True, "response": "OK:" + history[-1]["content"], "tool_calls": []}

    engine = ThreadEngine(store, _SimpleRuntime())
    try:
        started = await engine.start(
            Profile(),
            "initial message",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="Alice",
            background=True,
            wait_seconds=0,
        )
        tid = started["thread_id"]
        await asyncio.wait_for(first_get_entered.wait(), timeout=3.0)
        drive_task = engine.tasks[tid]
        # Cancel _drive task while it is on the very first store.get before async with
        drive_task.cancel()
        release_first_get.set()

        await asyncio.wait_for(
            asyncio.gather(drive_task, return_exceptions=True), timeout=3.0
        )
        assert drive_task.cancelled()
        # _drive may restart to finish pending active revision 1, and drain() must complete without leaking slots
        await asyncio.wait_for(engine.drain(), timeout=3.0)
        assert tid not in engine.tasks
        assert tid not in engine.background_tasks
        first_done = store.get(tid, "qq:group:100246809")
        assert first_done["state"] == "idle"
        assert "initial message" in first_done["result"]["text"]

        resumed = await asyncio.wait_for(
            engine.send(tid, "followup", chat_id="qq:group:100246809", wait_seconds=5),
            timeout=3.0,
        )
        assert resumed["state"] == "idle"
        assert "followup" in resumed["result"]["text"]

        # Also test cancelling via stop() during initial store.get (never restarts, zero slot leak)
        calls = 0
        release_first_get.clear()
        first_get_entered.clear()
        started2 = await engine.start(
            Profile(),
            "stop during first get",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="Alice",
            background=True,
            wait_seconds=0,
        )
        tid2 = started2["thread_id"]
        await asyncio.wait_for(first_get_entered.wait(), timeout=3.0)
        stop_task = asyncio.create_task(
            engine.stop(tid2, chat_id="qq:group:100246809")
        )
        await asyncio.sleep(0.01)
        release_first_get.set()
        stopped = await asyncio.wait_for(stop_task, timeout=3.0)
        assert stopped["state"] == "closed"
        assert tid2 not in engine.tasks
        assert tid2 not in engine.background_tasks
    finally:
        release_first_get.set()
        await asyncio.wait_for(engine.close(), timeout=3.0)


@pytest.mark.asyncio
async def test_b1_drive_finally_end_failure_and_double_cancel_never_leak_slots(
    tmp_path: Path,
):
    store = ThreadStore(tmp_path / "state.db")
    end_entered = asyncio.Event()
    end_gate = asyncio.Event()
    end_finished = asyncio.Event()
    mode = "raise"

    class _EndRuntime:
        def __init__(self):
            self.step_calls = 0

        async def step(self, profile, history):
            self.step_calls += 1
            return {
                "success": True,
                "response": f"step_{self.step_calls}:{history[-1]['content']}",
                "tool_calls": [],
            }

        async def end(self, thread_id):
            end_entered.set()
            if mode == "raise":
                raise RuntimeError("umount failed")
            await end_gate.wait()
            end_finished.set()

    rt = _EndRuntime()
    engine = ThreadEngine(store, rt, max_active=1)
    try:
        # Case A: runtime.end raises RuntimeError -> slot & waiter must NOT leak
        res_a = await asyncio.wait_for(
            engine.start(
                Profile(),
                "first",
                chat_id="qq:group:100246809",
                platform="qq",
                user_id="Alice",
                wait_seconds=3,
            ),
            timeout=3.0,
        )
        assert res_a["state"] == "idle"
        assert engine.tasks == {}
        assert engine.background_tasks == set()

        # Concurrency slot (max_active=1) must be free for the next thread
        end_entered.clear()
        mode = "slow"
        started_b = await asyncio.wait_for(
            engine.start(
                Profile(),
                "second",
                chat_id="qq:group:100246809",
                platform="qq",
                user_id="Alice",
                workspace="ws_b",
                background=True,
                wait_seconds=0,
            ),
            timeout=3.0,
        )
        tid_b = started_b["thread_id"]
        await asyncio.wait_for(end_entered.wait(), timeout=3.0)

        # Case B: while runtime.end is running, hit task with double cancel AND steer send
        drive_task = engine.tasks[tid_b]
        drive_task.cancel()
        await asyncio.sleep(0)
        drive_task.cancel()
        steered_future = asyncio.create_task(
            engine.send(
                tid_b,
                "steered_during_end",
                chat_id="qq:group:100246809",
                steer=True,
                wait_seconds=5,
            )
        )
        await asyncio.sleep(0)
        end_gate.set()

        steered_res = await asyncio.wait_for(steered_future, timeout=3.0)
        assert end_finished.is_set()
        assert steered_res["state"] == "idle"
        assert "steered_during_end" in steered_res["result"]["text"]
        assert engine.tasks == {}
        assert engine.background_tasks == set()
    finally:
        end_gate.set()
        await asyncio.wait_for(engine.close(), timeout=3.0)


@pytest.mark.asyncio
async def test_b1_tool_error_passthrough_preserves_safe_reason_and_scrubs_paths(
    tmp_path: Path,
):
    store = ThreadStore(tmp_path / "state.db")

    class _ErrorToolRuntime:
        def __init__(self):
            self.histories = []
            self.round = 0

        async def step(self, profile, history):
            self.histories.append(json.loads(json.dumps(history)))
            self.round += 1
            if self.round == 1:
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {"id": "c_html", "function": {"name": "web_read", "arguments": '{"url":"https://e.com/1"}'}},
                        {"id": "c_path", "function": {"name": "web_read", "arguments": '{"url":"https://e.com/2"}'}},
                        {"id": "c_os", "function": {"name": "web_read", "arguments": '{"url":"https://e.com/3"}'}},
                    ],
                }
            return {"success": True, "response": "done", "tool_calls": []}

        async def call(self, name, arguments, context):
            cid = context.get("_tool_call_id")
            if cid == "c_html":
                raise ValueError("HTML 源文件超过 512KB 上限")
            if cid == "c_path":
                raise RuntimeError("读取 /Users/secret/private.txt 失败")
            raise FileNotFoundError(2, "No such file or directory", "/etc/shadow")

    rt = _ErrorToolRuntime()
    engine = ThreadEngine(store, rt)
    res = await engine.start(
        Profile(tools=("web_read",)),
        "check tool errors",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Alice",
        wait_seconds=5,
    )
    assert res["state"] == "idle"
    tool_msgs = [m["content"] for m in rt.histories[-1] if m["role"] == "tool"]
    assert len(tool_msgs) == 3
    assert "HTML 源文件超过 512KB 上限" in tool_msgs[0]
    assert "/Users/secret/private.txt" not in tool_msgs[1]
    assert "失败" in tool_msgs[1]
    assert "/etc/shadow" not in tool_msgs[2]
    await engine.close()


def test_b1_store_search_cross_chat_and_alias_reads_history_without_permission_error(
    tmp_path: Path,
):
    store = ThreadStore(tmp_path / "state.db")
    store.chat_aliases = lambda cid, plat: (
        {cid, "legacy_grp_alias"} if cid == "qq:group:100246809" else {cid}
    )
    foreign = store.create(
        Profile(),
        "investigate alpha_cross_chat_marker",
        chat_id="qq:group:999888777",
        platform="qq",
        user_id="Bob",
    )
    store.finish(
        foreign["thread_id"],
        "qq:group:999888777",
        {"text": "found alpha_cross_chat_marker"},
        [{"role": "assistant", "content": "found alpha_cross_chat_marker"}],
        foreign["revision"],
    )
    legacy = store.create(
        Profile(),
        "investigate alpha_alias_marker",
        chat_id="legacy_grp_alias",
        platform="qq",
        user_id="Alice",
    )
    store.finish(
        legacy["thread_id"],
        "legacy_grp_alias",
        {"text": "found alpha_alias_marker"},
        [{"role": "assistant", "content": "found alpha_alias_marker"}],
        legacy["revision"],
    )

    with pytest.raises(PermissionError):
        store.read_history(foreign["thread_id"], "qq:group:100246809")

    all_hits = store.search("alpha_cross_chat_marker", "qq:group:100246809", all_chats=True)
    assert len(all_hits) == 1
    assert all_hits[0]["thread_id"] == foreign["thread_id"]
    assert all_hits[0]["matches"]

    alias_hits = store.search(
        "alpha_alias_marker",
        "qq:group:100246809",
        chat_ids=["qq:group:100246809", "legacy_grp_alias"],
    )
    assert len(alias_hits) == 1
    assert alias_hits[0]["thread_id"] == legacy["thread_id"]
    assert alias_hits[0]["matches"]

    direct_any = store.read_history(
        foreign["thread_id"], "qq:group:100246809", query="alpha_cross_chat_marker", allow_any_chat=True
    )
    assert direct_any


@pytest.mark.asyncio
async def test_b1_sandbox_execute_double_cancel_quarantines_and_finishes_terminate(
    tmp_path: Path,
):
    store = ThreadStore(tmp_path / "state.db")
    row = store.create(
        Profile(environment="sandbox"),
        "run",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="",
    )
    runner = _CancellableDockerFixture()
    sandbox = Sandbox(
        SandboxSettings(),
        store,
        runner=runner,
        redact=lambda value: value,
    )
    sandbox.ready = True
    sandbox._own = lambda work: None
    files = Workspaces(tmp_path / "workspaces", store)

    with files.activity(row) as work:
        work.write("project/f.txt", "before")
        work.commit()
        task = asyncio.create_task(
            sandbox.execute(row, work, "sleep 10", call_id="c1", mode="none")
        )
        try:
            await asyncio.wait_for(runner.entered.wait(), timeout=2.0)

            # First cancel hits execute while container is running
            task.cancel()
            await asyncio.wait_for(runner.rm_started.wait(), timeout=2.0)
            # Immediately quarantined while container termination is in-flight
            assert work.quarantined is True

            # Second cancel hits while _terminate is still waiting for rm_gate
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done(), "second cancel must NOT abandon _terminate"
            assert work.quarantined is True

            runner.rm_gate.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2.0)
            assert not runner.running
            assert row["thread_id"] not in sandbox.containers
            assert work.read("project/f.txt")["text"] == "before"
            assert work.quarantined is False
        finally:
            runner.hold.set()
            runner.rm_gate.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)

    # Case B: _terminate fails (container remains running) under double cancel -> stays quarantined & refuses next exec
    runner2 = _CancellableDockerFixture()
    runner2.rm_returns_gone = False
    sandbox2 = Sandbox(SandboxSettings(), store, runner=runner2, redact=lambda v: v)
    sandbox2.ready = True
    sandbox2._own = lambda work: None
    with files.activity(row) as work2:
        task2 = asyncio.create_task(
            sandbox2.execute(row, work2, "sleep 10", call_id="c2", mode="none")
        )
        try:
            await asyncio.wait_for(runner2.entered.wait(), timeout=2.0)
            task2.cancel()
            await asyncio.wait_for(runner2.rm_started.wait(), timeout=2.0)
            task2.cancel()
            await asyncio.sleep(0.02)
            assert not task2.done()
            runner2.rm_gate.set()
            with pytest.raises(RuntimeError, match="容器清理未确认"):
                await asyncio.wait_for(task2, timeout=2.0)
            assert work2.quarantined is True
            assert sandbox2.ready is False
            assert any(r["kind"] == "container" for r in sandbox2._resources())
        finally:
            runner2.hold.set()
            runner2.rm_gate.set()
            if not task2.done():
                await asyncio.gather(task2, return_exceptions=True)
    await sandbox.close()
    runner2.rm_returns_gone = True
    await sandbox2.close()


def test_b1_sandbox_volume_failed_mount_does_not_leak_resource_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ThreadStore(tmp_path / "state.db")
    sandbox = Sandbox(SandboxSettings(), store, redact=lambda v: v)
    sandbox.ready = True
    mount_dir = tmp_path / "workspaces/runtime/work-1"
    mount_dir.mkdir(parents=True)

    calls = []

    def fail_mount(cmd, **kwargs):
        calls.append(list(cmd))
        raise subprocess.CalledProcessError(32, cmd)

    monkeypatch.setattr(subprocess, "run", fail_mount)
    with pytest.raises(subprocess.CalledProcessError):
        with sandbox.volume(mount_dir):
            pass
    assert sandbox._resources() == []

    # When mount succeeds but _resource fails, umount rollback must run and leave no resource record
    calls.clear()

    def ok_mount_or_umount(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", ok_mount_or_umount)
    monkeypatch.setattr(
        sandbox,
        "_resource",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db failed")),
    )
    with pytest.raises(RuntimeError, match="db failed"):
        with sandbox.volume(mount_dir):
            pass
    assert any(c[0] == "mount" for c in calls)
    assert any(c[0] == "umount" for c in calls)
    assert sandbox._resources() == []


def test_b1_alias_migration_updates_attachments_chat_id(tmp_path: Path):
    store = ThreadStore(tmp_path / "state.db")
    files = Workspaces(tmp_path / "workspaces", store)
    profile = Profile(
        id="maker",
        environment="readonly",
        tools=("workspace_file", "result_attach"),
    )
    old_row = store.create(
        profile,
        "create attachment under old alias",
        chat_id="legacy-alias-1",
        platform="qq",
        user_id="Alice",
        workspace="ws_shared",
    )
    with files.activity(old_row) as work:
        work.write("out/report.txt", "hello")
        work.commit()
        attached = files.attach(old_row, work, ["out/report.txt"])
    store.finish(
        old_row["thread_id"],
        "legacy-alias-1",
        {"text": "ok", "attachments": attached},
        [{"role": "assistant", "content": "ok"}],
        old_row["revision"],
    )

    store.chat_aliases = lambda cid, plat: (
        {cid, "legacy-alias-1"} if cid == "qq:group:100246809" else {cid}
    )
    migrated = store.create(
        profile,
        "continue under canonical chat",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Alice",
        workspace="ws_shared",
    )
    assert migrated["chat_id"] == "qq:group:100246809"
    resolved = files.resolve(attached[0]["handle"], "qq:group:100246809")
    assert resolved.read_text() == "hello"


@pytest.mark.asyncio
async def test_b1_maker_blocks_private_from_threads_into_group_even_with_admin_direct(
    tmp_path: Path,
):
    store = ThreadStore(tmp_path / "state.db")
    vault = Vault(tmp_path / "secrets.vault")
    rt = AgentRuntime(
        None,
        tmp_path / "data",
        store,
        SandboxSettings(enabled=False),
        vault,
        authorized=lambda row: True,
    )
    ws_profile = Profile(
        id="maker",
        environment="readonly",
        tools=("workspace_file", "result_attach"),
    )
    priv = store.create(
        ws_profile,
        "private exploration",
        chat_id="qq:private:Alice",
        platform="qq",
        user_id="Alice",
    )
    store.finish(
        priv["thread_id"],
        "qq:private:Alice",
        {"text": "PRIVATE_SECRET_INFO"},
        [{"role": "assistant", "content": "PRIVATE_SECRET_INFO"}],
        priv["revision"],
    )
    grp = store.create(
        ws_profile,
        "group task",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Alice",
        grant={
            "admin_direct": True,
            "scheduled_admin": True,
            "source_chat_id": "qq:private:Alice",
            "source_platform": "qq",
        },
    )
    with pytest.raises(PermissionError, match="私聊线程结果默认不可直接注入群聊线程"):
        await rt.prepare(grp, {"from_threads": [priv["thread_id"]]})

    # Also test MD5 session_ids resolved via session_routes
    with store.connect() as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS session_routes ("
            "session_id TEXT PRIMARY KEY, platform TEXT, chat_type TEXT, "
            "group_id TEXT, user_id TEXT, account_id TEXT)"
        )
        db.execute(
            "INSERT OR REPLACE INTO session_routes VALUES (?,?,?,?,?,?)",
            ("deadbeef000000000000000000000001", "qq", "private", "", "Alice", ""),
        )
        db.execute(
            "INSERT OR REPLACE INTO session_routes VALUES (?,?,?,?,?,?)",
            ("deadbeef000000000000000000000002", "qq", "group", "100246809", "", ""),
        )
    priv_md5 = store.create(
        ws_profile,
        "private md5",
        chat_id="deadbeef000000000000000000000001",
        platform="qq",
        user_id="Alice",
    )
    store.finish(
        priv_md5["thread_id"],
        "deadbeef000000000000000000000001",
        {"text": "PRIVATE_MD5_SECRET"},
        [{"role": "assistant", "content": "PRIVATE_MD5_SECRET"}],
        priv_md5["revision"],
    )
    grp_md5 = store.create(
        ws_profile,
        "group md5",
        chat_id="deadbeef000000000000000000000002",
        platform="qq",
        user_id="Alice",
        grant={"admin_direct": True, "source_chat_id": "qq:private:Alice"},
    )
    with pytest.raises(PermissionError, match="私聊线程结果默认不可直接注入群聊线程"):
        await rt.prepare(grp_md5, {"from_threads": [priv_md5["thread_id"]]})
    await rt.close()


@pytest.mark.asyncio
async def test_b1_maker_enforces_listed_profile_allowlist_on_from_threads(
    tmp_path: Path,
):
    store = ThreadStore(tmp_path / "state.db")
    vault = Vault(tmp_path / "secrets.vault")
    rt = AgentRuntime(
        None,
        tmp_path / "data",
        store,
        SandboxSettings(enabled=False),
        vault,
        authorized=lambda row: True,
    )
    listed_profile = Profile(
        id="listed_tool",
        access="listed",
        allow_chats=("qq:group:100246809",),
        allow_users=("qq:Alice",),
        environment="readonly",
        tools=("workspace_file", "result_attach"),
    )
    public_profile = Profile(
        id="maker",
        environment="readonly",
        tools=("workspace_file", "result_attach"),
    )
    prev = store.create(
        listed_profile,
        "alice only listed work",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Alice",
    )
    store.finish(
        prev["thread_id"],
        "qq:group:100246809",
        {"text": "ALICE_LISTED_SECRET"},
        [{"role": "assistant", "content": "ALICE_LISTED_SECRET"}],
        prev["revision"],
    )

    # Unlisted user Bob in same group MUST be rejected
    bob_row = store.create(
        public_profile,
        "bob tries to read alice listed thread",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Bob",
    )
    with pytest.raises(PermissionError):
        await rt.prepare(bob_row, {"from_threads": [prev["thread_id"]]})

    # Wrong chat with admin_direct MUST still be rejected by allow_chats
    wrong_chat_row = store.create(
        public_profile,
        "alice in unlisted group",
        chat_id="qq:group:555666777",
        platform="qq",
        user_id="Alice",
        grant={"admin_direct": True},
    )
    with pytest.raises(PermissionError):
        await rt.prepare(wrong_chat_row, {"from_threads": [prev["thread_id"]]})

    # Listed user Alice in listed group succeeds
    alice_row = store.create(
        public_profile,
        "alice reads own listed thread",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Alice",
    )
    imported = await rt.prepare(alice_row, {"from_threads": [prev["thread_id"]]})
    assert any(item.get("kind") == "result_text" for item in imported)
    await rt.close()


@pytest.mark.asyncio
async def test_b1_maker_offloads_available_and_sync_digest_calls_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ThreadStore(tmp_path / "state.db")
    vault = Vault(tmp_path / "secrets.vault")
    rt = AgentRuntime(
        None,
        tmp_path / "data",
        store,
        SandboxSettings(enabled=False),
        vault,
        authorized=lambda row: True,
    )
    loop_tid = threading.get_ident()
    seen_tids = []

    orig_available = rt.available

    def tracked_available(profile):
        seen_tids.append(("available", threading.get_ident()))
        return orig_available(profile)

    rt.available = tracked_available

    async def fake_step(self, profile, history):
        return {"success": True, "response": "ok", "tool_calls": []}

    monkeypatch.setattr("maisya.maker.Scout.step", fake_step)
    profile = Profile(id="maker", environment="readonly", tools=("workspace_file", "digest_feed"))
    await rt.step(profile, [{"role": "user", "content": "hi"}])

    row = store.create(
        profile,
        "run digest",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="Alice",
    )
    ctx = {**row, "profile_snapshot": asdict(profile), "_tool_call_id": "call_1"}

    def sync_digest_tool(work, arguments, **kwargs):
        seen_tids.append(("digest", threading.get_ident()))
        return {"ok": True}

    import maisya.digest as digest_mod

    monkeypatch.setattr(digest_mod, "run_digest_tool", sync_digest_tool)
    res = await rt.call("digest_feed", {"action": "status"}, ctx)
    assert res == {"ok": True}

    avail_tids = [tid for tag, tid in seen_tids if tag == "available"]
    digest_tids = [tid for tag, tid in seen_tids if tag == "digest"]
    assert len(avail_tids) >= 2 and all(tid != loop_tid for tid in avail_tids)
    assert digest_tids and all(tid != loop_tid for tid in digest_tids)
    await rt.close()
