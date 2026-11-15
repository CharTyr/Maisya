import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from maisya.abilities import Profiles
from maisya.maker import AgentRuntime
from maisya.memory import MemoryLayers
from maisya.sandbox import SandboxSettings
from maisya.services import Services, validate_endpoint_targets
from maisya.threads import ThreadEngine, ThreadStore
from maisya.vault import Vault


class _Harness:
    def __init__(self, tmp_path: Path):
        self.data_root = tmp_path / "data"
        self.data_root.mkdir(parents=True, exist_ok=True)
        (self.data_root / "abilities").mkdir(parents=True, exist_ok=True)
        self.vault = Vault(self.data_root / "vault")
        self.store = ThreadStore(self.data_root / "maisya.db")
        self.profiles = Profiles(
            self.data_root / "profiles", contains_secret=self.vault.contains_secret
        )

        async def _call_capability(name, **arguments):
            return {"response": "ok", "tool_calls": []}

        self.ctx = SimpleNamespace(call_capability=_call_capability)
        self.runtime = AgentRuntime(
            self.ctx,
            self.data_root,
            self.store,
            SandboxSettings(enabled=False),
            self.vault,
            authorized=lambda context: True,
        )
        self.runtime.memory = MemoryLayers(
            self.data_root, self.store, self.profiles, self.vault
        )
        self.engine = ThreadEngine(
            self.store,
            self.runtime,
            redact=self.vault.redact,
        )

    async def close(self):
        await self.runtime.services.close()
        await self.runtime.sandbox.close()


@pytest.mark.asyncio
async def test_real_stdio_mcp_keeps_keys_and_sessions_out_of_model(tmp_path):
    h = _Harness(tmp_path)
    secret = "mcp-key-do-not-publish-12345"
    h.vault.set("drawing", secret)
    server = tmp_path / "server.py"
    server.write_text(
        """import json, os, sys
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
""",
        encoding="utf-8",
    )
    config = {
        "servers": {
            "drawing": {
                "command": [sys.executable, str(server)],
                "credentials": {"DRAWING_KEY": "drawing"},
                "tools": ["draw"],
                "readonly": True,
            }
        },
    }
    (h.data_root / "abilities/services.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    h.runtime.services = Services(h.data_root / "abilities/services.json", h.vault)
    profile_file = h.profiles.path / "scout/profile.toml"
    profile_text = "\n".join(
        line
        for line in profile_file.read_text(encoding="utf-8").splitlines()
        if not line.startswith(("tools =", "mcp ="))
    )
    profile_file.write_text(
        profile_text + '\ntools = ["mcp_call"]\nmcp = ["drawing"]\n',
        encoding="utf-8",
    )
    profile = h.profiles.get("scout")
    row = h.store.create(profile, "draw", chat_id="chat", platform="qq", user_id="")
    arguments = {"service": "drawing", "tool": "draw", "arguments": {"prompt": "山"}}
    try:
        first = await h.runtime.call(
            "mcp_call", arguments, {**row, "_tool_call_id": "a"}
        )
        assert secret not in str(first) and "REDACTED" in str(first)
        other = h.store.create(
            profile, "draw", chat_id="other", platform="qq", user_id=""
        )
        second = await h.runtime.call(
            "mcp_call", arguments, {**other, "_tool_call_id": "b"}
        )
        assert first == second
        with pytest.raises((ValueError, PermissionError)):
            await h.runtime.call(
                "mcp_call",
                {**arguments, "tool": "delete_all"},
                {**row, "_tool_call_id": "c"},
            )
        with pytest.raises(ValueError):
            await h.runtime.call(
                "mcp_call",
                {**arguments, "arguments": {"unexpected": True}},
                {**row, "_tool_call_id": "d"},
            )
        # Upgrade compatibility: legacy "templates" field on disk is discarded, not executed
        (h.data_root / "abilities/services.json").write_text(
            json.dumps({"servers": {}, "templates": {"legacy": {}}}),
            encoding="utf-8",
        )
        loaded_cfg = h.runtime.services.load()
        assert "templates" not in loaded_cfg
        assert not hasattr(h.runtime.services, "credential")
        with pytest.raises(PermissionError):
            await h.runtime.call(
                "mcp_call", arguments, {**row, "_tool_call_id": "e"}
            )
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_file_ref_input_uses_registered_real_stdio_reader_and_never_inlines_binary(
    tmp_path,
):
    h = _Harness(tmp_path)
    h.runtime.sandbox.ready = True
    server = tmp_path / "reader.py"
    server.write_text(
        """import json,sys,base64
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
""",
        encoding="utf-8",
    )
    configuration = {
        "servers": {
            "files": {
                "command": [sys.executable, str(server)],
                "tools": ["read_file"],
                "readonly": True,
            }
        },
        "inputs": {"qq": {"service": "files", "tool": "read_file"}},
    }
    h.runtime.services.path.write_text(json.dumps(configuration), encoding="utf-8")
    profile_file = h.profiles.path / "maker/profile.toml"
    profile_file.write_text(
        profile_file.read_text(encoding="utf-8")
        .replace('"result_attach"', '"result_attach", "mcp_call"')
        .replace("mcp = []", 'mcp = ["files"]'),
        encoding="utf-8",
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

    h.ctx.call_capability = capability
    try:
        profile = h.profiles.get("maker")
        result = await h.engine.start(
            profile,
            "read file",
            chat_id="chat",
            platform="qq",
            user_id="",
            workspace="file-reader",
            references=["file-message"],
            wait_seconds=10,
        )
        assert "error" not in result.get("result", {}), result
        row = h.store.get(result["thread_id"], "chat")
        with h.runtime.files.activity(row) as work:
            files = work.listing()["files"]
            assert any(file["path"].endswith("input.csv") for file in files)
        assert base64.b64encode(b"a,b\n1,2").decode() not in str(prompts)
        assert not result.get("result", {}).get("attachments")
    finally:
        await h.close()


def test_endpoint_target_ssrf_allowlist(tmp_path):
    assert validate_endpoint_targets(["93.184.216.34:443"]) == [("93.184.216.34", 443)]
    for blocked in ("127.0.0.1:8080", "169.254.169.254:80", "224.0.0.1:80"):
        with pytest.raises(ValueError):
            validate_endpoint_targets([blocked])
