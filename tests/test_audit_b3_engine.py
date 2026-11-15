"""Batch 3 (P2 + 测试/CI 盲区) TDD 测试集。"""

from __future__ import annotations

import asyncio
import base64
import errno
import json
import os
import sys
import types
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_foundation import loaded

from maisya import pathsafe, report as report_mod, workspaces as workspaces_mod
from maisya.abilities import Profile
from maisya.context_budget import resolve_context_budget
from maisya.maker import AgentRuntime
from maisya.sandbox import Sandbox, SandboxSettings
from maisya.scout import TOOL_DEFINITIONS, Scout
from maisya.threads import ThreadEngine, ThreadStore
from maisya.vault import Vault


# ---------------------------------------------------------------------------
# Item 1: read_history offset pagination & ThreadEngine chats/locks cleanup
# ---------------------------------------------------------------------------


def test_b3_read_history_event_seq_zero_paginates_with_offset(tmp_path: Path):
    store = ThreadStore(tmp_path / "state.db")
    row = store.create(Profile(), "start", chat_id="chat-a", platform="qq", user_id="u1")
    tid = row["thread_id"]
    rev = store.enqueue(tid, "start", "chat-a")
    body = (
        ("prefix_" * 40)
        + "NEEDLE_ANCHOR_HERE "
        + "".join(f"[seg_{i:03d}]" for i in range(120))
    )
    store.finish(tid, "chat-a", {"text": body}, [], rev)

    # 1) No query, event_seq=0: offset advances linearly across pages
    collected_no_q: list[str] = []
    offset = 0
    pages = 0
    while offset is not None:
        rows = store.read_history(tid, "chat-a", query="", event_seq=0, offset=offset, limit=1, max_chars=220)
        assert len(rows) == 1
        item = rows[0]
        assert item["offset"] == offset
        collected_no_q.append(item["text"])
        offset = item["next_offset"]
        pages += 1
        assert pages < 30
    assert pages > 2
    assert json.loads("".join(collected_no_q))["text"] == body

    # 2) With query and event_seq=0: page 1 starts near match, page 2 starts at next_offset
    first_page = store.read_history(tid, "chat-a", query="NEEDLE_ANCHOR_HERE", offset=0, max_chars=240)[0]
    assert first_page["next_offset"] is not None
    assert "NEEDLE_ANCHOR_HERE" in first_page["text"]
    second_page = store.read_history(
        tid,
        "chat-a",
        query="NEEDLE_ANCHOR_HERE",
        offset=first_page["next_offset"],
        max_chars=240,
    )[0]
    assert second_page["offset"] == first_page["next_offset"]
    assert second_page["text"] != first_page["text"]
    assert "seg_119" in (
        second_page["text"]
        + (
            store.read_history(
                tid,
                "chat-a",
                query="NEEDLE_ANCHOR_HERE",
                offset=second_page["next_offset"],
                max_chars=2000,
            )[0]["text"]
            if second_page["next_offset"] is not None
            else ""
        )
    )


@pytest.mark.asyncio
async def test_b3_thread_engine_cleans_chats_and_locks_after_finish_stop_and_timeout(
    tmp_path: Path,
):
    entered = asyncio.Event()
    release = asyncio.Event()

    class ControlledRuntime:
        async def step(self, profile, history):
            if profile.id == "waiter":
                entered.set()
                await release.wait()
            return {"success": True, "response": "ok", "tool_calls": []}

    store = ThreadStore(tmp_path / "state.db")
    engine = ThreadEngine(store, ControlledRuntime())

    # Normal finish cleans chats & locks
    res_fast = await asyncio.wait_for(
        engine.start(
            Profile(id="fast"),
            "quick",
            chat_id="chat-1",
            platform="qq",
            user_id="u1",
            wait_seconds=2.5,
        ),
        timeout=3.0,
    )
    assert res_fast["state"] == "idle"
    assert res_fast["thread_id"] not in engine.chats
    assert res_fast["thread_id"] not in engine.locks

    # stop() cleans chats & locks
    res_wait = await asyncio.wait_for(
        engine.start(
            Profile(id="waiter"),
            "long task",
            chat_id="chat-2",
            platform="qq",
            user_id="u2",
            wait_seconds=0,
        ),
        timeout=3.0,
    )
    await asyncio.wait_for(entered.wait(), timeout=3.0)
    await asyncio.wait_for(engine.stop(res_wait["thread_id"], chat_id="chat-2"), timeout=3.0)
    assert res_wait["thread_id"] not in engine.chats
    assert res_wait["thread_id"] not in engine.locks

    # Timeout cleanup + drain leaves no stale chats / locks even if waiter times out after _finalize
    entered.clear()
    release.clear()
    row = store.create(Profile(id="waiter"), "timeout wait", chat_id="chat-3", platform="qq", user_id="u3")
    send_res = await asyncio.wait_for(
        engine.send(row["thread_id"], "go", chat_id="chat-3", wait_seconds=0.05),
        timeout=3.0,
    )
    assert send_res["state"] == "active"
    release.set()
    await asyncio.wait_for(engine.drain(), timeout=3.0)
    assert engine.chats == {}
    assert engine.locks == {}
    await engine.close()


@pytest.mark.asyncio
async def test_b3_waiter_timeout_after_idle_cleanup_drops_chats_and_locks(tmp_path: Path):
    store = ThreadStore(tmp_path / "state.db")
    gate = asyncio.Event()

    class UnresolvedIdleRuntime:
        async def step(self, profile, history):
            await gate.wait()
            return {"success": True, "response": "", "tool_calls": []}

    engine = ThreadEngine(store, UnresolvedIdleRuntime())
    row = store.create(Profile(), "init", chat_id="chat-x", platform="qq", user_id="u1")
    tid = row["thread_id"]
    wait_task = asyncio.create_task(engine.send(tid, "msg1", chat_id="chat-x", wait_seconds=0.25))
    await asyncio.sleep(0.02)
    gate.set()
    await asyncio.wait_for(wait_task, timeout=3.0)
    assert tid not in engine.chats
    assert tid not in engine.locks

    # Waiter times out on higher revision when _drive hits idle/error (so _resolve keeps waiter until _cleanup_waiter)
    row2 = store.create(Profile(), "init2", chat_id="chat-y", platform="qq", user_id="u2")
    tid2 = row2["thread_id"]
    fake_fut = asyncio.get_running_loop().create_future()
    engine.chats[tid2] = "chat-y"
    engine.locks[tid2] = asyncio.Lock()
    engine.waiters[tid2] = [(99, fake_fut)]
    engine._cleanup_waiter(tid2, fake_fut)
    assert tid2 not in engine.chats
    assert tid2 not in engine.locks

    # drain() cleaning finished tasks must also drop engine.chats and engine.locks
    row3 = store.create(Profile(), "init3", chat_id="chat-z", platform="qq", user_id="u3")
    tid3 = row3["thread_id"]
    done_fut = asyncio.get_running_loop().create_future()
    done_fut.set_result(None)
    engine.chats[tid3] = "chat-z"
    engine.locks[tid3] = asyncio.Lock()
    engine.tasks[tid3] = done_fut
    await asyncio.wait_for(engine.drain(), timeout=3.0)
    assert tid3 not in engine.chats
    assert tid3 not in engine.locks
    await engine.close()


# ---------------------------------------------------------------------------
# Item 2: image_read base64, html_render whitespace fallback, brief.html, tz_offset_minutes, render_report caption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b3_scout_tz_offset_image_read_base64_and_render_report_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    render_def = TOOL_DEFINITIONS["render_report"]["function"]
    assert "out/reports/brief.html" in render_def["description"]
    assert "out/reports/brief.html" in render_def["parameters"]["properties"]["path"]["description"]
    assert "caption" in render_def["parameters"]["properties"]

    seen_prompts: list[list[dict]] = []
    seen_vision: list[dict] = []

    class FakeHost:
        async def call_capability(self, name, **kwargs):
            if name == "llm.generate_with_tools":
                seen_prompts.append(kwargs["prompt"])
                return {"success": True, "response": "ok", "tool_calls": []}
            if name == "message.get_by_id":
                return {
                    "success": True,
                    "message": {
                        "session_id": "chat-tz",
                        "raw_message": [
                            {"type": "image", "base64": base64.b64encode(b"\x89PNG\r\n\x1a\npayload").decode()},
                            {"type": "image", "data": {"base64": base64.b64encode(b"\xff\xd8\xffjpeg").decode()}},
                        ],
                    },
                }
            if name == "llm.generate_for_image":
                seen_vision.append(kwargs)
                return {"success": True, "response": "看见一张图"}
            raise AssertionError(f"unexpected capability {name}")

    scout = Scout(FakeHost(), tz_offset_minutes=-300)
    res_step = await asyncio.wait_for(
        scout.step(Profile(), [{"role": "user", "content": "now?"}]),
        timeout=3.0,
    )
    assert res_step["response"] == "ok"
    sys_msg = seen_prompts[-1][0]["content"]
    assert "UTC-5" in sys_msg
    assert "UTC+8" not in sys_msg

    # memory.tz_offset_minutes takes effect when instance tz_offset_minutes isn't pinned
    scout_mem = Scout(FakeHost())
    scout_mem.memory = SimpleNamespace(tz_offset_minutes=330)
    await asyncio.wait_for(
        scout_mem.step(Profile(), [{"role": "user", "content": "india"}]),
        timeout=3.0,
    )
    assert "UTC+5:30" in seen_prompts[-1][0]["content"]

    img1 = await asyncio.wait_for(
        scout.call("image_read", {"message_id": "m1", "index": 0, "question": "图里是什么"}, {"chat_id": "chat-tz"}),
        timeout=3.0,
    )
    img2 = await asyncio.wait_for(
        scout.call("image_read", {"message_id": "m1", "index": 1, "question": "图里是什么"}, {"chat_id": "chat-tz"}),
        timeout=3.0,
    )
    assert img1 == "看见一张图"
    assert img2 == "看见一张图"
    assert seen_vision[0]["image_format"] == "png"
    assert seen_vision[1]["image_format"] == "jpeg"


@pytest.mark.asyncio
async def test_b3_html_render_whitespace_fallback_and_render_report_caption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ThreadStore(tmp_path / "state.db")
    vault = Vault(tmp_path / "secrets.vault")
    rendered_html: list[str] = []

    class FakeHost:
        async def call_capability(self, name, **kwargs):
            if name == "render.html2png":
                rendered_html.append(kwargs["html"])
                return {"success": True, "image_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n123").decode()}
            raise AssertionError(name)

    rt = AgentRuntime(
        FakeHost(),
        tmp_path / "data",
        store,
        SandboxSettings(enabled=False),
        vault,
        authorized=lambda row: True,
    )
    profile = Profile(
        id="maker",
        environment="readonly",
        tools=("workspace_file", "html_render", "render_report"),
    )
    row = store.create(profile, "render", chat_id="chat-r", platform="qq", user_id="u1", workspace="ws1")
    ctx = {**row, "profile_snapshot": asdict(profile), "_tool_call_id": "c1", "_step_index": 0}
    await rt.call("workspace_file", {"action": "write", "path": "out/src.html", "content": "<h1>From File</h1>"}, ctx)

    # whitespace html falls back cleanly to html_path
    out_img = await asyncio.wait_for(
        rt.call("html_render", {"html": "   \n\t ", "html_path": "out/src.html"}, {**ctx, "_tool_call_id": "c2"}),
        timeout=3.0,
    )
    assert out_img["path"].startswith("out/")
    assert rendered_html == ["<h1>From File</h1>"]

    # render_report preserves both subtitle & caption cleanly through AgentRuntime.call and report_artifact
    seen_specs: list[dict] = []
    orig_validate = report_mod.validate

    def spy_validate(spec, *, profile="report"):
        seen_specs.append(dict(spec))
        return orig_validate(spec, profile=profile)

    monkeypatch.setattr(report_mod, "validate", spy_validate)
    rep = await asyncio.wait_for(
        rt.call(
            "render_report",
            {
                "path": "out/reports/brief.html",
                "title": "主标题",
                "subtitle": "副标题",
                "caption": "底部备注 2026",
                "source_text": "正文内容，年份 2026",
                "blocks": [{"type": "text", "text": "正文内容"}],
            },
            {**ctx, "_tool_call_id": "c3"},
        ),
        timeout=3.0,
    )
    assert rep["path"] == "out/reports/brief.html"
    assert seen_specs[-1]["subtitle"] == "副标题"
    assert seen_specs[-1]["caption"] == "底部备注 2026"

    # Also verify standalone render_report_artifact accepts caption + subtitle, reads full HTML & checks caption numbers/types
    await rt.end(row["thread_id"])
    with rt.files.activity(row) as work:
        cap_only = report_mod.render_report_artifact(
            work,
            {
                "path": "out/reports/cap_only.html",
                "title": "标题",
                "caption": "唯一说明",
                "blocks": [{"type": "text", "text": "段落"}],
            },
        )
        assert cap_only["path"] == "out/reports/cap_only.html"
        assert "唯一说明" in (work.path / "out/reports/cap_only.html").read_text(encoding="utf-8")
        with pytest.raises(ValueError, match="caption"):
            report_mod.render_report_artifact(
                work,
                {
                    "title": "标题",
                    "subtitle": "副标题",
                    "caption": 123,
                    "blocks": [{"type": "text", "text": "段落"}],
                },
            )
    await rt.close()


# ---------------------------------------------------------------------------
# Item 3: context_budget specificity & estimate_tools_tokens / overhead integration
# ---------------------------------------------------------------------------


def test_b3_context_budget_family_window_prefers_model_basename_and_word_boundaries():
    catalog = {
        "models": [
            {"name": "gw-deepseek", "identifier": "custom-o3/deepseek-r1"},
            {"name": "gw-unknown", "identifier": "custom-o3/my-private-model"},
            {"name": "real-o3", "identifier": "openai/o3-mini"},
            {"name": "video3", "identifier": "acme/video3-large"},
            {"name": "qwen3coder", "identifier": "qwen/qwen3-coder-480b"},
        ],
        "tasks": {},
    }
    b_ds = resolve_context_budget(Profile(model_name="gw-deepseek"), model_catalog=catalog)
    assert b_ds["family_window"] == 128_000
    assert b_ds["window_tokens"] == 128_000

    b_unk = resolve_context_budget(Profile(model_name="gw-unknown"), default_context_tokens=262_144, model_catalog=catalog)
    assert b_unk["family_window"] is None
    assert b_unk["window_tokens"] == 262_144

    b_o3 = resolve_context_budget(Profile(model_name="real-o3"), model_catalog=catalog)
    assert b_o3["family_window"] == 200_000

    b_vid = resolve_context_budget(Profile(model_name="video3"), default_context_tokens=262_144, model_catalog=catalog)
    assert b_vid["family_window"] is None

    b_qc = resolve_context_budget(Profile(model_name="qwen3coder"), model_catalog=catalog)
    assert b_qc["family_window"] == 262_144


@pytest.mark.asyncio
async def test_b3_threads_overhead_tokens_includes_tools_schema_methods_and_memory_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ThreadStore(tmp_path / "state.db")
    profile = Profile(
        id="scout",
        context_tokens=8_192,
        tools=tuple(k for k in TOOL_DEFINITIONS if k not in {"thread_read", "ask_user", "thread_later"})[:8],
    )
    row = store.create(profile, "go", chat_id="chat-b", platform="qq", user_id="u1")
    tid = row["thread_id"]
    rev1 = store.enqueue(tid, "first", "chat-b")
    # Build two moderate turns that only cross trigger_tokens once real tools schema + methods + memory_context are counted
    # window=8192, reserve=2048 -> effective=6144 -> trigger=4608 tokens.
    # ~2420 history tokens + ~1253 tools + ~750 methods + ~400 memory_context = ~4823 > 4608 (whereas old 8*120=960 gives ~3380 < 4608!)
    hist = [
        {"role": "user", "content": "q1 " + ("abcd " * 480)},
        {"role": "assistant", "content": "a1 " + ("abcd " * 480)},
        {"role": "user", "content": "q2 " + ("abcd " * 480)},
        {"role": "assistant", "content": "a2 " + ("abcd " * 480)},
    ]
    store.finish(tid, "chat-b", {"text": "done"}, hist, rev1)

    compact_overheads: list[int] = []
    orig_compact = ThreadEngine._run_tier2_compact

    async def spy_compact(self, *args, **kwargs):
        compact_overheads.append(int(kwargs.get("overhead_tokens", 0)))
        return await orig_compact(self, *args, **kwargs)

    monkeypatch.setattr(ThreadEngine, "_run_tier2_compact", spy_compact)

    class RuntimeWithMemory:
        def __init__(self):
            self.memory = SimpleNamespace(methods=lambda prof: "method_line " * 250)

        def before(self, context):
            return "mem_ctx " * 200

        async def summarize_compaction(self, turns, *, previous_handoff="", profile=None):
            return "## 目标与约束\ncompact ok"

        async def step(self, prof, req):
            return {"success": True, "response": "ok", "tool_calls": []}

    engine = ThreadEngine(store, RuntimeWithMemory())
    res = await asyncio.wait_for(
        engine.send(tid, "next", chat_id="chat-b", wait_seconds=2.5),
        timeout=3.0,
    )
    assert res["state"] == "idle"
    assert compact_overheads, "Tier-2 compaction must trigger when tool schemas + <methods> + <memory_context> push prompt over trigger_tokens"
    assert compact_overheads[0] > 2_000
    await engine.close()


# ---------------------------------------------------------------------------
# Item 4: Windows LK_NBLCK retry loop & _eloop_errnos
# ---------------------------------------------------------------------------


def test_b3_pathsafe_windows_lk_nblck_retry_and_eloop_errnos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    eloop = getattr(errno, "ELOOP", None)
    actual_eloop = pathsafe._eloop_errnos()
    assert {40, 62} <= actual_eloop
    if isinstance(eloop, int):
        assert eloop in actual_eloop
    assert workspaces_mod._eloop_errnos is pathsafe._eloop_errnos

    lock_file = tmp_path / ".win.lock"
    lock_file.write_bytes(b"\x00")
    attempts: list[tuple[int, int]] = []
    sleeps: list[float] = []
    fake_msvcrt = types.ModuleType("msvcrt")
    fake_msvcrt.LK_LOCK = 1
    fake_msvcrt.LK_NBLCK = 2
    fake_msvcrt.LK_UNLCK = 3
    remaining_failures = {"count": 2}

    def fake_locking(fd, mode, nbytes):
        attempts.append((mode, nbytes))
        if mode == fake_msvcrt.LK_LOCK:
            raise AssertionError("blocking lock must not use msvcrt.LK_LOCK")
        if mode == fake_msvcrt.LK_NBLCK and remaining_failures["count"] > 0:
            remaining_failures["count"] -= 1
            if remaining_failures["count"] == 1:
                raise PermissionError(13, "sharing violation")
            raise OSError(errno.EACCES, "locked")

    fake_msvcrt.locking = fake_locking
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr("maisya.pathsafe.Path", type(tmp_path))
    monkeypatch.setattr("maisya.pathsafe.os.name", "nt")
    try:
        monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

        with pathsafe.file_lock(lock_file, nonblocking=False):
            pass

        assert attempts.count((fake_msvcrt.LK_NBLCK, 1)) == 3
        assert attempts.count((fake_msvcrt.LK_UNLCK, 1)) == 1
        assert (fake_msvcrt.LK_LOCK, 1) not in attempts
        assert len(sleeps) == 2

        # nonblocking=True fails immediately without sleeping
        remaining_failures["count"] = 1
        sleeps.clear()
        with pytest.raises(BlockingIOError):
            with pathsafe.file_lock(lock_file, nonblocking=True):
                pass
        assert sleeps == []
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# Item 5 & 6: Dockerfile UID 10000, Sandbox._own guards, _bundle_paths
# ---------------------------------------------------------------------------


def test_b3_dockerfile_10000_uid_and_sandbox_own_handles_existing_erofs_and_perm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dockerfile = (Path(__file__).resolve().parent.parent / "sandbox/Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "65532" not in dockerfile
    assert "ARG " in dockerfile
    assert "10000" in dockerfile
    assert "USER 10000:10000" in dockerfile
    assert "groupadd" in dockerfile and "useradd" in dockerfile
    assert "@sha256:0000" not in dockerfile

    store = ThreadStore(tmp_path / "state.db")
    sb = Sandbox(SandboxSettings(enabled=False), store)
    work_dir = tmp_path / "ws"
    (work_dir / "project").mkdir(parents=True)
    file_path = work_dir / "project" / "a.txt"
    file_path.write_text("hi", encoding="utf-8")
    work = SimpleNamespace(path=work_dir)

    # 1) Skip os.chown if lstat already reports 10000:10000
    orig_lstat = os.lstat
    chown_calls: list[Path] = []
    monkeypatch.setattr(
        "maisya.sandbox.os.lstat",
        lambda p: SimpleNamespace(st_mode=orig_lstat(p).st_mode, st_uid=10000, st_gid=10000),
    )
    monkeypatch.setattr(
        "maisya.sandbox.os.chown",
        lambda p, u, g, **kw: chown_calls.append(Path(p)),
    )
    sb._own(work)
    assert chown_calls == []

    # 2) PermissionError raises controlled PermissionError without leaking host path
    monkeypatch.setattr(
        "maisya.sandbox.os.lstat",
        lambda p: SimpleNamespace(st_mode=orig_lstat(p).st_mode, st_uid=1001, st_gid=1001),
    )

    def raise_perm(p, u, g, **kw):
        raise PermissionError(errno.EPERM, "Operation not permitted", str(p))

    monkeypatch.setattr("maisya.sandbox.os.chown", raise_perm)
    with pytest.raises(PermissionError) as exc_info:
        sb._own(work)
    msg = str(exc_info.value)
    assert "10000:10000" in msg
    assert str(tmp_path) not in msg

    # 3) Read-only filesystem EROFS is handled gracefully without aborting
    monkeypatch.setattr(
        "maisya.sandbox.os.chown",
        lambda p, u, g, **kw: (_ for _ in ()).throw(OSError(errno.EROFS, "Read-only file system")),
    )
    sb._own(work)


def test_b3_run_isolated_bundle_paths_includes_viz_skills_and_memory():
    from tests_sandbox import run_isolated

    root = Path(__file__).resolve().parent.parent
    bundled = {p.resolve() for p in run_isolated._bundle_paths(root) if p.is_file()}
    assert (root / "maisya/viz.css").resolve() in bundled
    assert (root / "tests_sandbox/check_runtime.py").resolve() in bundled
    assert any(p == (root / "maisya/threads.py").resolve() for p in bundled)
    assert any((root / "presets/profiles") in p.parents for p in bundled)
    assert any((root / "presets/skills") in p.parents for p in bundled)
    assert any((root / "presets/memory") in p.parents for p in bundled)
