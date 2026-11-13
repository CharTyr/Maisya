import json
import sys
from dataclasses import replace

import pytest
from test_foundation import loaded


@pytest.mark.asyncio
async def test_real_stdio_mcp_keeps_keys_and_sessions_out_of_model(tmp_path):
    plugin = await loaded(tmp_path)
    secret = "mcp-key-do-not-publish-12345"
    plugin.vault.set("drawing", secret)
    server = tmp_path / "server.py"
    server.write_text("""import json, os, sys
count = 0
for line in sys.stdin:
 request = json.loads(line)
 if 'id' not in request: continue
 method = request['method']
 if method == 'initialize': result = {'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}
 elif method == 'tools/list': result = {'tools':[{'name':'draw','inputSchema':{'type':'object','properties':{'prompt':{'type':'string'}},'required':['prompt'],'additionalProperties':False}}]}
 elif method == 'tools/call':
  count += 1
  result = {'content':[{'type':'text','text':str(count)+' '+os.environ['DRAWING_KEY']}]}
 else: result = {}
 print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}), flush=True)
""")
    config = {
        "servers": {
            "drawing": {
                "command": [sys.executable, str(server)],
                "credentials": {"DRAWING_KEY": "drawing"},
                "tools": ["draw"],
                "readonly": True,
            }
        },
        "templates": {},
    }
    (plugin.data_root / "abilities/services.json").write_text(json.dumps(config))
    from maisya.services import Services

    plugin.runtime.services = Services(
        plugin.data_root / "abilities/services.json", plugin.vault
    )
    profile = replace(
        plugin.profiles.get("scout"), mcp=("drawing",), tools=("mcp_call",)
    )
    row = plugin.threads.create(
        profile, "draw", chat_id="chat", platform="qq", user_id=""
    )
    arguments = {"service": "drawing", "tool": "draw", "arguments": {"prompt": "山"}}
    try:
        first = await plugin.runtime.call(
            "mcp_call", arguments, {**row, "_tool_call_id": "a"}
        )
        assert secret not in str(first) and "REDACTED" in str(first)
        other = plugin.threads.create(
            profile, "draw", chat_id="other", platform="qq", user_id=""
        )
        second = await plugin.runtime.call(
            "mcp_call", arguments, {**other, "_tool_call_id": "b"}
        )
        assert first == second
        with pytest.raises((ValueError, PermissionError)):
            await plugin.runtime.call(
                "mcp_call",
                {**arguments, "tool": "delete_all"},
                {**row, "_tool_call_id": "c"},
            )
        with pytest.raises(ValueError):
            await plugin.runtime.call(
                "mcp_call",
                {**arguments, "arguments": {"unexpected": True}},
                {**row, "_tool_call_id": "d"},
            )
        (plugin.data_root / "abilities/services.json").write_text(
            json.dumps({"servers": {}, "templates": {}})
        )
        with pytest.raises(PermissionError):
            await plugin.runtime.call(
                "mcp_call", arguments, {**row, "_tool_call_id": "e"}
            )
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_file_ref_input_uses_registered_real_stdio_reader_and_never_inlines_binary(
    tmp_path,
):
    import base64

    plugin = await loaded(tmp_path)
    plugin.runtime.sandbox.ready = True
    server = tmp_path / "reader.py"
    server.write_text("""import json,sys,base64
for line in sys.stdin:
 request=json.loads(line)
 if 'id' not in request:continue
 method=request['method']
 if method=='initialize':result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}
 elif method=='tools/list':result={'tools':[{'name':'read_file','inputSchema':{'type':'object','properties':{'message_id':{'type':'string'},'file_ref':{'type':'string'}},'required':['message_id','file_ref'],'additionalProperties':False}}]}
 else:
  assert request['params']['arguments']['file_ref']=='opaque-platform-reference'
  result={'content':[{'type':'resource','resource':{'uri':'file:///input.csv','mimeType':'text/csv','blob':base64.b64encode(b'a,b\\n1,2').decode()}}]}
 print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
""")
    configuration = {
        "servers": {
            "files": {
                "command": [sys.executable, str(server)],
                "tools": ["read_file"],
                "readonly": True,
            }
        },
        "templates": {},
        "inputs": {"qq": {"service": "files", "tool": "read_file"}},
    }
    plugin.runtime.services.path.write_text(json.dumps(configuration))
    profile_file = plugin.profiles.path / "maker/profile.toml"
    profile_file.write_text(
        profile_file.read_text()
        .replace('"result_attach"', '"result_attach", "mcp_call"')
        .replace("mcp = []", 'mcp = ["files"]')
    )
    prompts = []

    async def capability(name, **arguments):
        if name == "message.get_by_id":
            return {
                "message_id": "file-message",
                "session_id": "chat",
                "raw_message": [
                    {
                        "type": "file",
                        "data": {
                            "name": "input.csv",
                            "file_ref": "opaque-platform-reference",
                        },
                    }
                ],
            }
        prompts.append(arguments["prompt"])
        return {"response": "read", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        result = await plugin.agent_tool(
            action="start",
            profile="maker",
            workspace="file-reader",
            message="read file",
            ref_msg_ids=["file-message"],
            chat_id="chat",
            platform="qq",
        )
        assert result["success"] and "error" not in result["result"], result
        row = plugin.threads.get(result["thread_id"], "chat")
        with plugin.runtime.files.activity(row) as work:
            files = work.listing()["files"]
            assert any(file["path"].endswith("input.csv") for file in files)
        assert base64.b64encode(b"a,b\n1,2").decode() not in str(prompts)
        assert not result["result"].get("attachments")
    finally:
        await plugin.on_unload()
