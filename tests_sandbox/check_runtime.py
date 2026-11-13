"""真 gVisor 验收；缺运行时或依赖直接失败，不降级到宿主执行。"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import shlex
import sys
import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from maisya.abilities import Profiles
from maisya.maker import AgentRuntime
from maisya.sandbox import SandboxSettings, process
from maisya.threads import ThreadStore
from maisya.vault import Vault


def emit(phase, **values):
    print(json.dumps({"phase": phase, **values}), flush=True)


def python(code):
    return "python -c " + shlex.quote(code)


async def main():
    root = Path.cwd() / "fixture-data"
    root.mkdir(mode=0o700)
    vault = Vault(root / "vault")
    store = ThreadStore(root / "state.db")
    profiles = Profiles(root / "abilities/profiles")
    routes = json.loads((await process(["ip", "-j", "route", "show"])).output)
    occupied = [
        ipaddress.ip_network(item["dst"], strict=False)
        for item in routes
        if item.get("dst") and item["dst"] != "default"
    ]
    subnet = next(
        str(network)
        for network in [
            ipaddress.ip_network(f"192.168.{value}.0/24")
            for value in range(250, 200, -1)
        ]
        if not any(network.overlaps(other) for other in occupied if other.version == 4)
    )
    settings = SandboxSettings(
        enabled=True,
        image=os.environ["MAISYA_SANDBOX_IMAGE"],
        socket=os.environ["MAISYA_SANDBOX_SOCKET"],
        timeout=180,
        subnet=subnet,
    )
    runtime = AgentRuntime(
        None,
        root,
        store,
        settings,
        vault,
        authorized=lambda row: row.get("grant", {}).get("admin") is True,
    )
    sandbox = runtime.sandbox
    Path("runtime-instance.json").write_text(json.dumps({"instance": sandbox.instance}))
    peer = None
    trap = None
    dnat_table = None
    try:
        assert await sandbox.probe(), sandbox.error
        profile = profiles.get("maker")
        row = store.create(
            profile,
            "runtime fixture",
            chat_id="fixture",
            platform="fixture",
            user_id="",
            workspace="checks",
        )
        with runtime.files.activity(row) as work:

            async def execute(code, call_id, **arguments):
                return await sandbox.execute(
                    row, work, python(code), call_id=call_id, **arguments
                )

            result = await execute(
                'import os,json; print(json.dumps({"uid":os.getuid(),"result":6*7})); open("state.txt","w").write("saved")',
                "calculation",
            )
            emit("calculation_result", result=result)
            assert result["exit_code"] == 0 and json.loads(result["output"]) == {
                "uid": 10000,
                "result": 42,
            }
            assert work.read("project/state.txt")["text"] == "saved"
            repeated = await execute(
                'raise RuntimeError("must not run twice")', "calculation"
            )
            assert repeated["repeated"] and repeated["output"] == result["output"]
            result = await sandbox.execute(
                row, work, 'node -e "console.log(6*7)" && npm --version', call_id="node"
            )
            assert result["exit_code"] == 0 and result["output"].startswith("42\n")
            emit("calculation_and_node_passed")

            sentinel = root / "host-only.txt"
            sentinel.write_text("host-only-" + os.urandom(8).hex())
            code = (
                "import os\nfrom pathlib import Path\nassert os.getuid()==10000\nassert not Path("
                + repr(str(sentinel))
                + ').exists()\nassert not Path("/var/run/docker.sock").exists()\ntry:\n Path("/etc/maisya-test").write_text("bad")\nexcept OSError: pass\nelse: raise AssertionError("writable root")\nprint("isolation_ok")'
            )
            result = await execute(code, "filesystem")
            assert result["exit_code"] == 0 and "isolation_ok" in result["output"]
            result = await sandbox.execute(
                row,
                work,
                "python -m pip install --disable-pip-version-check --no-cache-dir --target /work/cache/site Pillow==11.3.0 && PYTHONPATH=/work/cache/site python -c "
                + shlex.quote(
                    'from PIL import Image; Image.new("RGB",(4,4),(80,130,70)).save("/work/out/picture.png")'
                ),
                call_id="public-dependency",
            )
            assert result["exit_code"] == 0, result["output"]
            assert (work.path / "out/picture.png").stat().st_size > 0
            result = await execute(
                'import json,urllib.request; data=json.load(urllib.request.urlopen("https://pypi.org/pypi/packaging/json",timeout=15)); assert data["info"]["name"]=="packaging"; print("public_api_ok")',
                "public-api",
            )
            assert result["exit_code"] == 0 and "public_api_ok" in result["output"]
            artifact = runtime.files.attach(row, work, ["out/picture.png"])[0]
            assert artifact["handle"].startswith("att_")
            emit("public_dependency_api_and_image_passed")

            entry = next(
                value
                for value in sandbox.networks.values()
                if value["name"].startswith(sandbox.network)
            )
            gateway = str(ipaddress.ip_network(entry["subnet"]).network_address + 1)

            async def trap_handler(reader, writer):
                await reader.read(4096)
                writer.write(b"HTTP/1.0 200 OK\r\nContent-Length: 4\r\n\r\ntrap")
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            trap = await asyncio.start_server(trap_handler, gateway, 0)
            port = trap.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection(gateway, port)
            writer.write(b"GET / HTTP/1.0\r\n\r\n")
            await writer.drain()
            assert b"trap" in await reader.read()
            writer.close()
            await writer.wait_closed()
            peer = "maisya-peer-" + sandbox.instance
            await sandbox.docker(
                "run",
                "--detach",
                "--name",
                peer,
                "--runtime",
                settings.runtime,
                "--network",
                entry["name"],
                "--read-only",
                "--user",
                "10000:10000",
                "--cap-drop",
                "ALL",
                "--memory",
                "64m",
                "--pids-limit",
                "32",
                settings.image,
                "python",
                "-m",
                "http.server",
                "8080",
            )
            peer_info = json.loads(
                (
                    await sandbox.docker(
                        "inspect",
                        "--format",
                        "{{json .NetworkSettings.Networks}}",
                        peer,
                    )
                ).output
            )
            peer_ip = peer_info[entry["name"]]["IPAddress"]
            probe = (
                "import socket\ntargets="
                + repr(
                    [
                        (gateway, port),
                        (peer_ip, 8080),
                        ("169.254.169.254", 80),
                        ("10.0.0.1", 443),
                        ("100.100.100.200", 80),
                    ]
                )
                + '\nfor host,port in targets:\n try:\n  connection=socket.create_connection((host,port),timeout=0.6)\n except OSError: continue\n else:\n  connection.close(); raise AssertionError("forbidden target reachable")\nprint("blocked_targets_ok")'
            )
            result = await execute(probe, "blocked-targets")
            assert (
                result["exit_code"] == 0 and "blocked_targets_ok" in result["output"]
            ), result["output"]
            rebound = (
                "import socket,urllib.request\noriginal=socket.getaddrinfo\ndef resolve(host,port,*args,**kwargs):\n return original("
                + repr(gateway)
                + ',port,*args,**kwargs)\nsocket.getaddrinfo=resolve\ntry:\n urllib.request.urlopen("http://rebind.test:'
                + str(port)
                + '/",timeout=1)\nexcept OSError: print("rebind_blocked")\nelse: raise AssertionError("rebinding escaped policy")'
            )
            result = await execute(rebound, "dns-rebinding")
            assert result["exit_code"] == 0 and "rebind_blocked" in result["output"]
            result = await execute(
                'import socket\nfor host in ("::ffff:'
                + gateway
                + '","2606:4700:4700::1111"):\n try:\n  connection=socket.create_connection((host,443),timeout=0.6)\n except OSError: continue\n else: connection.close(); raise AssertionError("IPv6 bypass")\nprint("ipv6_blocked")',
                "ipv6",
            )
            assert result["exit_code"] == 0 and "ipv6_blocked" in result["output"]
            dnat_table = "ms_" + sandbox.instance + "_dnat"
            rule = (
                f"table ip {dnat_table} {{\n"
                f" chain translate {{\n"
                f"  type nat hook prerouting priority -100; policy accept;\n"
                f'  iifname "{entry["bridge"]}" ip daddr 1.1.1.1 tcp dport 8443 dnat to {gateway}:{port}\n'
                f" }}\n"
                f"}}\n"
            )
            dnat_rc = await process(["nft", "-f", "-"], stdin=rule.encode())
            assert dnat_rc.code == 0, f"nft dnat 建表失败 rc={dnat_rc.code}: {dnat_rc.output}\nrule={rule}"
            result = await execute(
                'import socket\ntry:\n socket.create_connection(("1.1.1.1",8443),timeout=1)\nexcept OSError: print("dnat_blocked")\nelse: raise AssertionError("DNAT bypass")',
                "dnat",
            )
            assert result["exit_code"] == 0 and "dnat_blocked" in result["output"]
            await process(["nft", "delete", "table", "ip", dnat_table])
            dnat_table = None
            emit("host_peer_metadata_dns_dnat_ipv6_blocked")

            result = await execute(
                'import socket\ntry:\n socket.create_connection(("1.1.1.1",443),timeout=1)\nexcept OSError: print("offline_ok")\nelse: raise AssertionError("offline sandbox reached internet")',
                "offline",
                mode="none",
            )
            assert result["exit_code"] == 0 and "offline_ok" in result["output"]
            try:
                await sandbox.execute(
                    row,
                    work,
                    "echo interrupted > state.txt; sleep 30",
                    call_id="timeout",
                    timeout=1,
                )
            except TimeoutError:
                pass
            else:
                raise AssertionError("command exceeded deadline")
            assert work.read("project/state.txt")["text"] == "saved"
            task = asyncio.create_task(
                sandbox.execute(
                    row, work, "echo cancelled > state.txt; sleep 30", call_id="cancel"
                )
            )
            await asyncio.sleep(1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert work.read("project/state.txt")["text"] == "saved"
            try:
                await execute("data=bytearray(700*1024**2); print(len(data))", "memory")
            except RuntimeError:
                pass
            else:
                raise AssertionError("memory allocation was not stopped by limit")
            assert work.read("project/state.txt")["text"] == "saved"
            emit("offline_timeout_cancel_memory_and_rollback_passed")
            slot_probe = asyncio.create_task(sandbox.slot.acquire())
            await asyncio.sleep(0)
            acquired = slot_probe.done()
            if acquired:
                sandbox.slot.release()
            else:
                slot_probe.cancel()
                await asyncio.gather(slot_probe, return_exceptions=True)
            assert acquired, "cancelled execution still occupies the sandbox slot"

            same = store.create(
                profile,
                "revisit",
                chat_id="fixture",
                platform="fixture",
                user_id="",
                workspace="checks",
            )
            assert same["thread_id"] == row["thread_id"]
            assert same["workspace_id"] == row["workspace_id"]

            other_chat = store.create(
                profile,
                "other room",
                chat_id="other-room",
                platform="fixture",
                user_id="",
                workspace="checks",
            )
            assert other_chat["workspace_id"] != row["workspace_id"]

            try:
                with runtime.files.activity(row):
                    pass
            except RuntimeError as error:
                assert "写入" in str(error)
            else:
                raise AssertionError("held workspace admitted a second writer")

            for bad_path in (
                "../outside.txt",
                "project/../../outside",
                "/etc/passwd",
                "project/..\\escape",
            ):
                try:
                    await asyncio.to_thread(work.write, bad_path, "escape")
                except (ValueError, PermissionError):
                    pass
                else:
                    raise AssertionError("traversal path accepted: " + bad_path)
            (work.path / "project" / "linked.txt").write_text("target")
            os.symlink("linked.txt", work.path / "project" / "alias.txt")
            try:
                await asyncio.to_thread(work.read, "project/alias.txt")
            except (ValueError, OSError):
                pass
            else:
                raise AssertionError("workspace symlink accepted")
            try:
                await asyncio.to_thread(work.write, "in/blocked.txt", "read-only")
            except (ValueError, PermissionError):
                pass
            else:
                raise AssertionError("write into in/ accepted")
            big = "x" * (runtime.files.max_bytes + 1)
            try:
                await asyncio.to_thread(work.write, "project/big.txt", big)
            except ValueError:
                pass
            else:
                raise AssertionError("oversize write accepted")
            os.unlink(work.path / "project" / "alias.txt")
            await asyncio.to_thread(work.commit)

        with runtime.files.activity(row) as reopened:
            assert reopened.read("project/state.txt")["text"] == "saved"
            assert reopened.read("project/linked.txt")["text"] == "target"

        emit("workspace_concurrency_reload_traversal_quota_passed")

        class FixtureHost:
            async def call_capability(self, name, **arguments):
                if name != "message.get_by_id":
                    raise AssertionError("unexpected capability " + name)
                return {
                    "message_id": arguments["message_id"],
                    "session_id": arguments["chat_id"],
                    "raw_message": [
                        {
                            "type": "file",
                            "data": {
                                "name": "source.bin",
                                "base64": base64.b64encode(
                                    b"binary-input-\x00\x01"
                                ).decode(),
                            },
                        },
                        {
                            "type": "image",
                            "data": {
                                "name": "pixel.png",
                                "base64": base64.b64encode(
                                    b"\x89PNG\r\n\x1a\nfixture"
                                ).decode(),
                            },
                        },
                        {
                            "type": "file",
                            "data": {
                                "name": "downloaded.txt",
                                "url": "https://pypi.org/pypi/packaging/json",
                            },
                        },
                    ],
                }

        runtime.host = FixtureHost()
        imported = await runtime.prepare(row, {"references": ["msg-input-1"]})
        names = {item["path"] for item in imported}
        assert any("source.bin" in name for name in names)
        assert any("pixel.png" in name for name in names)
        assert any("downloaded.txt" in name for name in names)

        work = await runtime._work(row)
        listing = {entry["path"] for entry in work.listing()["files"]}
        assert any(name.startswith("in/") for name in listing)
        assert work.read("project/state.txt")["text"] == "saved"
        attachment = runtime.files.attach(row, work, ["out/picture.png"])
        (work.path / "out" / "picture.png").write_bytes(b"modified")
        resolved = runtime.files.resolve(attachment[0]["handle"], "fixture")
        assert resolved.read_bytes() != b"modified"
        try:
            runtime.files.resolve(attachment[0]["handle"], "other-room")
        except PermissionError:
            pass
        else:
            raise AssertionError("attachment crossed sessions")
        try:
            runtime.files.attach(row, work, ["project/state.txt"])
        except (ValueError, PermissionError):
            pass
        else:
            raise AssertionError("attachment outside out/ accepted")
        await runtime.end(row["thread_id"])
        assert row["thread_id"] not in runtime.sessions

        emit("input_entities_and_immutable_attachments_passed")

        secret_value = "mcp-secret-" + os.urandom(8).hex()
        vault.set("fixture-mcp", secret_value)
        server_dir = Path(tempfile.mkdtemp(prefix="maisya-mcp-fixture-"))
        server_dir.chmod(0o755)
        server = server_dir / "mcp-server.py"
        server.write_text(
            "import json,os,sys\n"
            "for line in sys.stdin:\n"
            " r=json.loads(line)\n"
            " if 'id' not in r: continue\n"
            " m=r['method']\n"
            " if m=='initialize': result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}\n"
            " elif m=='tools/list': result={'tools':[{'name':'draw','inputSchema':{'type':'object','properties':{'prompt':{'type':'string'}},'required':['prompt'],'additionalProperties':False}}]}\n"
            " elif m=='tools/call': result={'content':[{'type':'text','text':'drawn '+r['params']['arguments']['prompt']+' env='+str(bool(os.environ.get('DRAWING_KEY')))}]}\n"
            " else: result={}\n"
            " print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)\n"
        )
        server.chmod(0o644)
        mcp_config = {
            "servers": {
                "drawing": {
                    "command": ["/usr/bin/python3", str(server)],
                    "credentials": {"DRAWING_KEY": "fixture-mcp"},
                    "tools": ["draw"],
                    "readonly": True,
                }
            },
            "templates": {},
        }
        (root / "abilities/services.json").write_text(json.dumps(mcp_config))
        mcp_profile = replace(profile, mcp=("drawing",), tools=("mcp_call",))
        mcp_row_a = store.create(
            mcp_profile,
            "mcp a",
            chat_id="fixture",
            platform="fixture",
            user_id="",
        )
        mcp_row_b = store.create(
            mcp_profile,
            "mcp b",
            chat_id="other-room",
            platform="fixture",
            user_id="",
        )
        call_args = {
            "service": "drawing",
            "tool": "draw",
            "arguments": {"prompt": "forest"},
        }
        first = await runtime.call(
            "mcp_call", call_args, {**mcp_row_a, "_tool_call_id": "m1"}
        )
        assert "drawn forest" in first["text"]
        assert secret_value not in str(first)
        second = await runtime.call(
            "mcp_call", call_args, {**mcp_row_b, "_tool_call_id": "m2"}
        )
        assert "drawn forest" in second["text"]
        session_scopes = {key[2:] for key in runtime.services.sessions}
        assert ("fixture", mcp_row_a["thread_id"]) in session_scopes
        assert ("other-room", mcp_row_b["thread_id"]) in session_scopes
        try:
            await runtime.call(
                "mcp_call",
                {**call_args, "tool": "delete_all"},
                {**mcp_row_a, "_tool_call_id": "m3"},
            )
        except (ValueError, PermissionError):
            pass
        else:
            raise AssertionError("unlisted MCP tool accepted")
        try:
            await runtime.call(
                "mcp_call",
                {**call_args, "arguments": {"unexpected": True}},
                {**mcp_row_a, "_tool_call_id": "m4"},
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid MCP arguments accepted")
        (root / "abilities/services.json").write_text(
            json.dumps({"servers": {}, "templates": {}})
        )
        try:
            await runtime.call(
                "mcp_call", call_args, {**mcp_row_a, "_tool_call_id": "m5"}
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("revoked MCP service still callable")
        emit("real_stdio_mcp_sessions_and_revocation_passed")

        clone = profiles.clone("maker", "maker-copy")
        assert clone.id == "maker-copy" and clone.bound
        package = profiles.export("maker")
        assert isinstance(package, bytes) and len(package) < 8 * 1024**2
        assert secret_value.encode() not in package
        import_profile = profiles.import_package(package, "maker-imported")
        assert import_profile.id == "maker-imported" and import_profile.bound

        sensitive_dir = profiles.path / "sensitive"
        sensitive_dir.mkdir()
        (sensitive_dir / "PROFILE.md").write_text("sensitive profile")
        (sensitive_dir / "profile.toml").write_text(
            'title="restricted-runner"\n'
            'environment="sandbox"\n'
            'access="admins"\n'
            'network="restricted"\n'
            'tools=["credential_call"]\n'
            'mcp=["drawing"]\n'
            'secrets=["fixture-mcp"]\n'
            'template="probe"\n'
        )
        sensitive_package = profiles.export("sensitive")
        assert secret_value.encode() not in sensitive_package
        import io

        names = []
        texts = []
        with zipfile.ZipFile(io.BytesIO(sensitive_package)) as archive:
            names = archive.namelist()
            for name in names:
                texts.append(archive.read(name))
        blob = b"\n".join(texts)
        assert b"fixture-mcp" in blob and secret_value.encode() not in blob
        imported_sensitive = profiles.import_package(
            sensitive_package, "sensitive-imported"
        )
        assert imported_sensitive.bound is False
        stage_marker = root / "import-executed"
        assert not stage_marker.exists()
        emit("profile_clone_export_import_passed")

        private = replace(
            profile,
            id="private-fixture",
            access="admins",
            network="restricted",
            tools=("credential_call",),
            secrets=("fixture",),
            template="probe",
        )
        secret = os.urandom(32).hex()
        vault.set("fixture", secret)
        config = {
            "servers": {},
            "templates": {
                "probe": {
                    "argv": ["/opt/maisya/templates/probe.py"],
                    "schema": {
                        "type": "object",
                        "properties": {"label": {"type": "string"}},
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                    "secrets": ["fixture"],
                    "targets": [{"address": "1.1.1.1", "port": 443}],
                }
            },
        }
        (root / "abilities/services.json").write_text(json.dumps(config))
        row = store.create(
            private,
            "private fixture",
            chat_id="fixture",
            platform="fixture",
            user_id="admin",
            grant={"admin": True},
        )
        response = await runtime.call(
            "credential_call",
            {"arguments": {"label": "confirmed"}},
            {**row, "_tool_call_id": "private"},
        )
        assert response["exit_code"] == 0, response
        payload = json.loads(response["output"])
        assert (
            payload["digest"] == hashlib.sha256(secret.encode()).hexdigest()
            and payload["uid"] == 10000
        )
        with store.connect() as db:
            records = "\n".join(
                record["result"]
                for record in db.execute("SELECT result FROM executions")
            )
        if secret in records or secret in json.dumps(response):
            raise AssertionError("private credential escaped execution boundary")
        with runtime.files.activity(row) as work:
            result = await sandbox.execute(
                row,
                work,
                python(
                    'import socket\nconnection=socket.create_connection(("1.1.1.1",443),timeout=5); connection.close()\ntry:\n socket.create_connection(("8.8.8.8",443),timeout=1)\nexcept OSError: print("restricted_ok")\nelse: raise AssertionError("unregistered target reachable")'
                ),
                call_id="restricted",
                mode="restricted",
                targets=[("1.1.1.1", 443)],
            )
            assert result["exit_code"] == 0 and "restricted_ok" in result["output"]
        emit("private_template_and_target_limits_passed")
        containers = await sandbox.docker(
            "ps",
            "--all",
            "--filter",
            "label=maisya.instance=" + sandbox.instance,
            "--format",
            "{{.Names}}",
        )
        assert not containers.output.strip()
        emit("all_runtime_checks_passed")
    finally:
        if trap:
            trap.close()
            await trap.wait_closed()
        if peer:
            await sandbox.docker("rm", "--force", peer, checked=False)
        if dnat_table:
            await process(["nft", "delete", "table", "ip", dnat_table])
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
