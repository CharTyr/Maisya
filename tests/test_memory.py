import pytest
from test_foundation import event, loaded

from maisya.abilities import Profiles
from maisya.memory import MemoryLayers
from maisya.threads import ThreadStore
from maisya.vault import Vault


def layers(tmp_path, clock=lambda: 1794499200):
    vault = Vault(tmp_path / "vault")
    profiles = Profiles(tmp_path / "abilities/profiles")
    store = ThreadStore(tmp_path / "state.db")
    return MemoryLayers(tmp_path, store, profiles, vault, clock=clock), vault


def test_memory_is_scoped_versioned_and_rejects_stale_writes(tmp_path):
    memory, _vault = layers(tmp_path)
    original = memory.read("memory", chat_id="chat/a")
    first = memory.write(
        "memory",
        "只在这里称呼玄武厅。",
        chat_id="chat/a",
        expected_version=original["version"],
    )
    assert memory.read("memory", chat_id="other")["content"] == ""
    assert memory.context("chat/a")["memory"] == first["content"]
    with pytest.raises(ValueError, match="版本"):
        memory.write(
            "memory", "stale", chat_id="chat/a", expected_version=original["version"]
        )
    restored = memory.rollback(
        "memory",
        original["version"],
        chat_id="chat/a",
        expected_version=first["version"],
    )
    assert restored["content"] == "" and restored["version"] > first["version"]
    assert len(memory.history("memory", chat_id="chat/a")) == 3
    assert memory.search("玄武厅", "other") == []
    assert memory.search("玄武厅", "chat/a") == []


def test_global_methods_reject_conversation_details_secrets_and_unsafe_paths(tmp_path):
    memory, vault = layers(tmp_path)
    vault.set("test", "test-private-token-value")
    memory.write("memory", "群名：玄武厅", chat_id="chat")
    for content in [
        "本群用这个流程",
        "联系 a@private.example",
        "test-private-token-value",
        "玄武厅的导出步骤",
    ]:
        with pytest.raises(ValueError):
            memory.write("skill", content, target="export", chat_id="chat")
    with pytest.raises(ValueError):
        memory.write("skill", "safe", target="../../outside", chat_id="chat")
    original = memory.read("profile", target="default")
    with pytest.raises(ValueError):
        memory.write(
            "profile",
            'environment="readonly"\ntools=["unknown_tool"]',
            target="default",
            document="profile.toml",
            expected_version=memory.read(
                "profile", target="default", document="profile.toml"
            )["version"],
        )
    assert memory.read("profile", target="default")["content"] == original["content"]


def test_methods_reload_and_external_edits_are_versioned(tmp_path):
    memory, _vault = layers(tmp_path)
    memory.write("skill", "导出前检查中文字体。", target="fonts")
    settings = memory.read("profile", target="default", document="profile.toml")
    memory.write(
        "profile",
        settings["content"] + '\nskills=["fonts"]\n',
        target="default",
        document="profile.toml",
        expected_version=settings["version"],
    )
    memory.write("notes", "发现字体缺失时先检查字体列表。", target="default")
    profile = memory.profiles.get("default")
    assert profile.skills == ("fonts",)
    assert "中文字体" in memory.methods(profile)
    assert "检查字体列表" in memory.methods(profile)
    path = memory.profiles.path / "default/NOTES.md"
    path.write_text("先检查字体文件是否存在。", encoding="utf-8")
    assert memory.read("notes", target="default")["content"] == "先检查字体文件是否存在。"
    assert len(memory.history("notes", target="default")) == 3


def test_journal_expires_and_never_imports_chat_results(tmp_path):
    now = [1794499200.0]
    memory, _vault = layers(tmp_path, clock=lambda: now[0])
    profile = memory.profiles.get("default")
    chat = memory.store.create(
        profile, "group trip", chat_id="chat", platform="qq", user_id=""
    )
    memory.experience({**chat, "result": {"text": "群里在聊玄武厅"}})
    assert memory.context("other")["journal"] == ""
    own = memory.store.create(
        profile, "own trip", chat_id="", platform="self", user_id="", owner="self"
    )
    memory.experience(
        {**own, "result": {"text": "我沿着海岸逛了一圈。"}, "completed_revision": 1}
    )
    assert "海岸" in memory.context("other")["journal"]
    assert memory.search("海岸", "other", kind="journal")
    now[0] += 8 * 86400
    assert memory.context("other")["journal"] == ""
    assert memory.search("海岸", "other", kind="journal") == []


@pytest.mark.asyncio
async def test_admin_shape_is_live_and_recall_keeps_chat_memory_scoped(tmp_path):
    plugin = await loaded(tmp_path, permissions=["qq:Alice"])
    prompts = []

    async def capability(name, **arguments):
        prompts.append(arguments)
        return {"response": "已完成", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        denied = await plugin.shape_tool(
            action="file",
            op="write",
            kind="profile",
            target="default",
            content="先检查来源。",
            chat_id="chat",
            platform="qq",
            user_id="Alice",
        )
        assert not denied["success"]
        await plugin.after_receive(message=event())
        source = event()["message_id"]
        saved = plugin.memory.write(
            "notes",
            "先核实来源再回答。",
            target="default",
            expected_version=0,
        )
        assert saved["version"] == 1
        await plugin.agent_tool(
            action="start", message="查证", chat_id="chat", platform="qq"
        )
        assert "先核实来源再回答" in str(prompts[-1]["prompt"])
        saved = await plugin.shape_tool(
            action="file",
            op="write",
            kind="memory",
            content="群名：玄武厅",
            source_msg_id=source,
            chat_id="chat",
            platform="qq",
            expected_version=0,
        )
        assert saved["success"], saved
        found = await plugin.recall_tool(
            query="玄武厅", kind="memory", chat_id="chat", platform="qq"
        )
        assert found["success"] and found["memories"]
        assert plugin.memory.search("玄武厅", "other") == []
        readonly = await plugin.shape_tool(
            action="file",
            op="write",
            kind="memory",
            content="forged",
            source_msg_id="forged",
            chat_id="chat",
            platform="qq",
            expected_version=1,
        )
        assert not readonly["success"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_learning_extracts_only_generic_statuses_after_actual_recovery(tmp_path):
    import json

    from maisya.learning import Learning

    memory, _vault = layers(tmp_path)
    row = memory.store.create(
        memory.profiles.get("default"),
        "玄武厅 private input",
        chat_id="chat",
        platform="qq",
        user_id="Alice",
    )
    row.update(result={"text": "成功"}, completed_revision=1)
    requests = []

    class Host:
        async def call_capability(self, name, **arguments):
            requests.append(arguments)
            assert arguments["max_tokens"] == 500
            assert "玄武厅" not in str(arguments) and "Alice" not in str(arguments)
            return {
                "response": json.dumps(
                    {"note": "参数校验失败后先核对输入结构，再重试。"}
                )
            }

    learning = Learning(Host(), memory)
    try:
        await learning.schedule(row)
        await learning.drain()
        assert not requests
        with memory.store.connect() as db, db:
            memory.store.event(
                db,
                row["thread_id"],
                "tools",
                1,
                {
                    "calls": [
                        {
                            "id": "bad",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query":"玄武厅"}',
                            },
                        }
                    ],
                    "results": [{"call_id": "bad", "value": {"error": "failed"}}],
                },
            )
            memory.store.event(
                db,
                row["thread_id"],
                "tools",
                1,
                {
                    "calls": [
                        {
                            "id": "good",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query":"玄武厅"}',
                            },
                        }
                    ],
                    "results": [
                        {"call_id": "good", "value": {"text": "private output"}}
                    ],
                },
            )
        await learning.schedule(row)
        await learning.drain()
        notes = memory.read("notes", target="default")
        assert "输入结构" in notes["content"] and notes["version"] == 1
        await learning.schedule(row)
        await learning.drain()
        assert len(requests) == 1
    finally:
        await learning.close()


def test_concurrent_edits_have_one_winner_and_one_version_conflict(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    memory, _vault = layers(tmp_path)

    def edit(content):
        try:
            return memory.write("memory", content, chat_id="chat", expected_version=0)[
                "version"
            ]
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(str(result) for result in executor.map(edit, ["甲", "乙"])) == [
            "1",
            "conflict",
        ]
    assert len(memory.history("memory", chat_id="chat")) == 2


@pytest.mark.asyncio
async def test_skipped_tool_is_not_counted_as_recovery(tmp_path):
    from maisya.learning import Learning

    memory, _vault = layers(tmp_path)
    row = memory.store.create(
        memory.profiles.get("default"), "check", chat_id="chat", platform="qq", user_id=""
    )
    row.update(result={"text": "done"}, completed_revision=1)

    class Host:
        async def call_capability(self, *arguments, **options):
            raise AssertionError("跳过工具不能触发提炼")

    with memory.store.connect() as db, db:
        for call_id, value in [
            ("failed", {"error": "failed"}),
            ("skipped", {"skipped": True}),
        ]:
            memory.store.event(
                db,
                row["thread_id"],
                "tools",
                1,
                {
                    "calls": [{"id": call_id, "function": {"name": "web_search"}}],
                    "results": [{"call_id": call_id, "value": value}],
                },
            )
    learning = Learning(Host(), memory)
    try:
        await learning.schedule(row)
        await learning.drain()
    finally:
        await learning.close()


@pytest.mark.asyncio
async def test_recovery_learning_and_self_journal_are_connected_to_real_thread_loop(
    tmp_path,
):
    import json

    plugin = await loaded(tmp_path)
    turns = [0]
    searches = [0]
    extraction = []

    async def capability(name, **arguments):
        if name == "web.search":
            searches[0] += 1
            if searches[0] == 1:
                return {"success": False}
            return {"success": True, "result": {"text": "verified"}}
        assert name == "llm.generate_with_tools"
        if arguments.get("max_tokens") == 500:
            extraction.append(arguments)
            return {
                "response": json.dumps({"note": "工具失败后先核对参数结构，再重试。"})
            }
        turns[0] += 1
        if turns[0] <= 2:
            return {
                "tool_calls": [
                    {
                        "id": "search-" + str(turns[0]),
                        "function": {
                            "name": "web_search",
                            "arguments": {"query": "private fixture query"},
                        },
                    }
                ]
            }
        return {"response": "我今天去海边散步了。", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        result = await plugin.agent_tool(
            action="start", message="check", chat_id="chat", platform="qq"
        )
        assert result["success"] and result["state"] == "idle"
        await plugin.runtime.learning.drain()
        assert len(extraction) == 1 and "private fixture query" not in str(extraction)
        assert "核对参数" in plugin.memory.read("notes", target="default")["content"]
        assert plugin.memory.context("chat")["journal"] == ""
        await plugin.engine.start(
            plugin.profiles.get("default"),
            "自己的散步",
            chat_id="",
            platform="self",
            owner="self",
            user_id="",
        )
        assert "海边" in plugin.memory.context("chat")["journal"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_memory_reaches_planner_agent_and_native_replyer_without_cross_chat_data(
    tmp_path,
):
    plugin = await loaded(tmp_path)
    prompts = []

    async def capability(name, **arguments):
        prompts.append(arguments["prompt"])
        return {"response": "done", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        await plugin.after_receive(message=event())
        plugin.memory.write("memory", "群名：玄武厅", chat_id="chat")
        plugin.memory.write("memory", "另一个会话独有的秘密称呼", chat_id="other")
        hook = await plugin.before_planner(
            session_id="chat",
            tool_definitions=[
                {"type": "function", "function": {"name": "maisya_agent"}}
            ],
        )
        assert "玄武厅" in str(hook) and "另一个会话独有" not in str(hook)
        await plugin.agent_tool(
            action="start", message="hello", chat_id="chat", platform="qq"
        )
        assert "玄武厅" in str(prompts) and "另一个会话独有" not in str(prompts)
        reply = await plugin.before_replyer(
            session_id="chat", request_type="maisaka.replyer", extra_prompt="original"
        )
        assert "玄武厅" in str(reply) and "另一个会话独有" not in str(reply)
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_profile_correction_applies_to_existing_thread_and_restricted_access_is_live(
    tmp_path,
):
    plugin = await loaded(tmp_path, permissions=["qq:Alice"])
    prompts = []

    async def capability(name, **arguments):
        prompts.append(arguments["prompt"])
        return {"response": "done", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        first = await plugin.agent_tool(
            action="start", message="first", chat_id="chat", platform="qq"
        )
        plugin.memory.write(
            "profile",
            "先用原始来源交叉核实，再给出答案。",
            target="default",
            expected_version=0,
        )
        resumed = await plugin.agent_tool(
            action="send",
            thread_id=first["thread_id"],
            message="again",
            chat_id="chat",
            platform="qq",
        )
        assert resumed["state"] == "idle" and "交叉核实" in str(prompts[-1])
        settings = plugin.memory.read(
            "profile", target="default", document="profile.toml"
        )
        plugin.memory.write(
            "profile",
            settings["content"].replace('access = "everyone"', 'access = "admins"'),
            target="default",
            document="profile.toml",
            expected_version=settings["version"],
        )
        recalled = await plugin.recall_tool(
            kind="threads", chat_id="chat", platform="qq"
        )
        assert recalled["threads"] == []
        denied = await plugin.agent_tool(
            action="send",
            thread_id=first["thread_id"],
            message="private",
            chat_id="chat",
            platform="qq",
        )
        assert not denied["success"]
    finally:
        await plugin.on_unload()
