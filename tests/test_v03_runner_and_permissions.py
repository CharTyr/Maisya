import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from maisya.config import Settings
from maisya.sandbox import ProcessResult, SandboxSettings
from maisya.trust import Message, MessageStore, normalize_admins
from maisya.vault import Vault, VaultError


def _python_cmd(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_settings_extra_ignore_admins_permission_mode_and_ui_style():
    from maisya.config import ProactiveSettings, canonical_ui_style

    cfg = Settings(
        legacy_obsolete_flag=True,
        old_nested_removed={"a": 1},
        admins=[" 10001 ", "QQ:AliceCase", "qq:private:10002", "qq:AliceCase"],
        permission_mode="ask_risky",
        default_model="gpt-5-mini",
        ui_style="millennium",
    )
    assert cfg.admins == ["qq:10001", "qq:AliceCase", "qq:10002"]
    assert cfg.permission_mode == "ask_risky"
    assert cfg.default_model == "gpt-5-mini"
    assert cfg.ui_style == "millennium"
    assert not hasattr(cfg, "legacy_obsolete_flag")

    assert canonical_ui_style("maiwork") == "modern"
    assert canonical_ui_style("millennium") == "millennium"
    assert Settings(ui_style="maiwork").ui_style == "maiwork"
    assert Settings(ui_style="modern").ui_style == "modern"

    proactive = cfg.proactive_settings()
    assert isinstance(proactive, ProactiveSettings)
    assert proactive.proactive_level == cfg.proactive_level

    for mode in ("ask_risky", "autonomous", "always_ask"):
        assert Settings(permission_mode=mode).permission_mode == mode

    with pytest.raises(ValueError):
        Settings(permission_mode="unsafe_all")

    with pytest.raises(ValueError):
        Settings(admins=["qq:group:100246809"])

    with pytest.raises(ValueError):
        Settings(admins=["not-a-valid-id"])

    import tomllib

    example_path = Path(__file__).resolve().parents[1] / "config.example.toml"
    parsed_example = tomllib.loads(example_path.read_text(encoding="utf-8"))
    example_cfg = Settings(**parsed_example)
    assert example_cfg.permission_mode == "ask_risky"
    assert example_cfg.ui_style == "millennium"
    assert example_cfg.admins == []
    assert example_cfg.sandbox.image == "maisya-sandbox:latest"
    assert example_cfg.sandbox.runtime == ""


def test_sandbox_settings_lowered_defaults_and_boundary():
    default_sb = SandboxSettings()
    assert not default_sb.enabled
    assert default_sb.image == "maisya-sandbox:latest"
    assert default_sb.runtime == ""
    assert default_sb.privileged_sandbox_mode is False
    assert default_sb.network_mode == "none"
    assert default_sb.readonly_rootfs is True

    tag_sb = SandboxSettings(
        enabled=True,
        image="python:3.12-slim",
        runtime="",
        network_mode="bridge",
        readonly_rootfs=False,
    )
    assert tag_sb.enabled
    assert tag_sb.image == "python:3.12-slim"
    assert tag_sb.network_mode == "bridge"
    assert tag_sb.readonly_rootfs is False

    digest_sb = SandboxSettings(
        enabled=True,
        image="ghcr.io/example/sandbox@sha256:" + "a" * 64,
        runtime="runc",
    )
    assert digest_sb.runtime == "runc"

    with pytest.raises(ValueError):
        SandboxSettings(enabled=True, image="")

    with pytest.raises(ValueError):
        SandboxSettings(enabled=True, image="python:latest", socket="tcp://host:2375")

    with pytest.raises(ValueError):
        SandboxSettings(enabled=True, image="python:latest", privileged_sandbox_mode=True)


def test_dual_source_admins_anti_lockout_and_admin_dm(tmp_path: Path):
    from maisya.trust import (
        admin_dm_allowed,
        merge_admins,
        validate_admin_update,
    )

    merged = merge_admins(
        ["QQBOT:AbCdEf_123", "telegram:private:AliceOrg"],
        ["10001", "qq:BobCase", "qq:BobCase", "qq:group:100246809"],
    )
    assert merged == {
        "qqbot:AbCdEf_123",
        "telegram:AliceOrg",
        "qq:10001",
        "qq:BobCase",
    }
    assert normalize_admins(["10001", "qq:CaseCheck"]) == {"qq:10001", "qq:CaseCheck"}

    # Anti-lockout: cannot leave effective admins empty
    with pytest.raises(ValueError):
        validate_admin_update([], actor="qq:10001", host_admins=[])

    # Anti-lockout: verified actor cannot remove themselves unless retained by host_admins
    with pytest.raises(ValueError):
        validate_admin_update(["qq:20002"], actor="qq:10001", host_admins=[])

    # Clearing settings.admins is allowed if host_admins preserves actor
    assert (
        validate_admin_update([], actor="qq:10001", host_admins=["qq:10001"]) == []
    )
    updated = validate_admin_update(
        ["10001", "qqbot:OpenIdMixed"], actor="qq:10001", host_admins=[]
    )
    assert updated == ["qq:10001", "qqbot:OpenIdMixed"]

    store = MessageStore(tmp_path / "maisya.db")
    admins = {"qq:10001", "qqbot:OpenIdMixed"}

    # Private DM from verified admin passes even without enabled_chats
    store.record(Message("m-dm-admin", "s-dm", "qq", "10001", False, group_id=""))
    assert admin_dm_allowed(store, "m-dm-admin", "s-dm", "qq", admins)

    # Group message from admin does NOT count as private DM bypass
    store.record(
        Message("m-grp-admin", "s-grp", "qq", "10001", False, group_id="100246809")
    )
    assert not admin_dm_allowed(store, "m-grp-admin", "s-grp", "qq", admins)

    # Non-admin DM and plugin-relayed DM are denied
    store.record(Message("m-dm-user", "s-dm-u", "qq", "20002", False, group_id=""))
    assert not admin_dm_allowed(store, "m-dm-user", "s-dm-u", "qq", admins)
    store.record(Message("m-dm-relay", "s-dm-r", "qq", "10001", True, group_id=""))
    assert not admin_dm_allowed(store, "m-dm-relay", "s-dm-r", "qq", admins)

    # Plugin disabled blocks admin DM too
    assert not admin_dm_allowed(
        store, "m-dm-admin", "s-dm", "qq", admins, plugin_enabled=False
    )


def test_permission_grants_once_session_always_and_revoke(tmp_path: Path):
    from maisya.trust import PermissionGrantStore

    db_path = tmp_path / "maisya.db"
    grants = PermissionGrantStore(db_path)

    # once tier: single consumption
    grants.grant(
        scope="qq:group:100246809",
        session_id="th-1",
        action_key="exec:destructive_shell",
        tier="once",
        actor="qq:10001",
    )
    assert grants.check_or_consume(
        scope="qq:group:100246809",
        session_id="th-1",
        action_key="exec:destructive_shell",
    )
    assert not grants.check_or_consume(
        scope="qq:group:100246809",
        session_id="th-1",
        action_key="exec:destructive_shell",
    )

    # session tier: persists within matching session_id only
    grants.grant(
        scope="qq:group:100246809",
        session_id="th-1",
        action_key="exec:external_write",
        tier="session",
        actor="qq:10001",
    )
    assert grants.check_or_consume(
        scope="qq:group:100246809",
        session_id="th-1",
        action_key="exec:external_write",
    )
    assert grants.check_or_consume(
        scope="qq:group:100246809",
        session_id="th-1",
        action_key="exec:external_write",
    )
    assert not grants.check_or_consume(
        scope="qq:group:100246809",
        session_id="th-2",
        action_key="exec:external_write",
    )

    # always tier: persists across reopen and revocable
    grant_key = grants.grant(
        scope="*",
        action_key="secret:gh_token",
        tier="always",
        actor="qq:10001",
        note="Allow gh_token",
    )
    reopened = PermissionGrantStore(db_path)
    assert reopened.check_or_consume(
        scope="qq:private:10001",
        session_id="th-9",
        action_key="secret:gh_token",
    )
    rows = reopened.list_grants()
    assert any(row["grant_key"] == grant_key for row in rows)
    assert reopened.revoke(grant_key)
    assert not reopened.check_or_consume(
        scope="qq:private:10001",
        session_id="th-9",
        action_key="secret:gh_token",
    )


def test_vault_meta_catalog_and_secret_injection_redaction(tmp_path: Path):
    from maisya.vault import redact_secrets

    secret = "sk-test-vault-secret-" + "z" * 16
    vault = Vault(tmp_path / "vault")
    vault.set(
        "openai_api",
        secret,
        label="OpenAI Key",
        domain="api.openai.com",
        fields=["api_key"],
    )

    meta_file = vault.path / "vault_meta.json"
    assert meta_file.exists()
    if os.name != "nt":
        assert stat.S_IMODE(meta_file.stat().st_mode) == 0o600
    raw_meta = meta_file.read_text(encoding="utf-8")
    assert secret not in raw_meta

    records = vault.list_meta()
    assert len(records) == 1
    assert records[0]["handle"] == "openai_api"
    assert records[0]["label"] == "OpenAI Key"
    assert records[0]["domain"] == "api.openai.com"
    assert records[0]["fields"] == ["api_key"]
    assert isinstance(records[0]["updated_at"], float)

    # Metadata must never accept plaintext secret values
    with pytest.raises(VaultError):
        vault.set_meta("openai_api", label=f"leak {secret}")

    # Inject env secrets + redact
    env = vault.inject_env_secrets(["openai_api"], env={"LC_ALL": "C.UTF-8"})
    assert env["MAISYA_SECRET_OPENAI_API"] == secret
    assert env["LC_ALL"] == "C.UTF-8"

    redacted = redact_secrets(f"stdout={secret} stderr=ok", [secret])
    assert secret not in redacted
    assert "[REDACTED]" in redacted

    # Delete removes both secret and catalog record
    vault.delete("openai_api")
    assert vault.list_meta() == []
    parsed_meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert "openai_api" not in parsed_meta


def test_host_workspace_runner_admin_env_risk_gate_and_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from maisya.runner import ApprovalRequired, ExecutionRequest, HostWorkspaceRunner
    from maisya.trust import PermissionGrantStore

    monkeypatch.setenv("HOST_SUPER_SECRET", "host-env-must-not-leak-999")
    ws = tmp_path / "workspace"
    ws.mkdir()
    vault = Vault(tmp_path / "vault")
    api_secret = "tok-live-secret-abcdef12345"
    vault.set("api_token", api_secret, label="API Token", domain="api.example.com")
    grants = PermissionGrantStore(tmp_path / "maisya.db")
    runner = HostWorkspaceRunner(vault=vault, grants=grants)

    # 1. Non-admin is hard denied regardless of permission_mode
    denied = runner.run(
        ExecutionRequest(
            command=_python_cmd("print('hi')"),
            workspace=ws,
            is_admin=False,
            permission_mode="autonomous",
        )
    )
    assert denied.status == "denied"

    # 2. Admin safe workspace command executes with clean env (no host secret leaked)
    safe_res = runner.run(
        ExecutionRequest(
            command=_python_cmd(
                "import os, pathlib; print(os.environ.get('HOST_SUPER_SECRET', 'MISSING')); print(pathlib.Path.cwd())"
            ),
            workspace=ws,
            is_admin=True,
            scope="qq:private:10001",
            session_id="th-1",
            permission_mode="ask_risky",
        )
    )
    assert safe_res.status == "ok"
    assert safe_res.exit_code == 0
    assert "MISSING" in safe_res.stdout
    assert "host-env-must-not-leak-999" not in safe_res.stdout
    assert str(ws.resolve()) in safe_res.stdout

    # 3. Destructive shell & path escape are intercepted before running under ask_risky
    victim = ws / "keep.txt"
    victim.write_text("important", encoding="utf-8")
    risky_rm = runner.run(
        ExecutionRequest(
            command="rm -rf keep.txt",
            shell=True,
            workspace=ws,
            is_admin=True,
            scope="qq:private:10001",
            session_id="th-1",
            permission_mode="ask_risky",
        )
    )
    assert risky_rm.status == "approval_required"
    assert isinstance(risky_rm.approval_required, ApprovalRequired)
    assert risky_rm.approval_required.risk_level == "risky"
    assert victim.exists()

    risky_escape = runner.run(
        ExecutionRequest(
            command=_python_cmd("print(open('../outside.txt').read())"),
            workspace=ws,
            is_admin=True,
            scope="qq:private:10001",
            session_id="th-1",
            permission_mode="ask_risky",
        )
    )
    assert risky_escape.status == "approval_required"
    assert risky_escape.approval_required is not None

    risky_reset = runner.run(
        ExecutionRequest(
            command=["git", "reset", "--hard", "HEAD~1"],
            workspace=ws,
            is_admin=True,
            scope="qq:private:10001",
            session_id="th-1",
            permission_mode="ask_risky",
        )
    )
    assert risky_reset.status == "approval_required"

    # 4. Reading /run/secrets or injecting secret needs confirmation in ask_risky
    secret_req = ExecutionRequest(
        command=_python_cmd(
            "import os, sys; val = os.environ['MAISYA_SECRET_API_TOKEN']; print('OUT:' + val); print('ERR:' + val, file=sys.stderr)"
        ),
        workspace=ws,
        is_admin=True,
        scope="qq:private:10001",
        session_id="th-1",
        permission_mode="ask_risky",
        secrets=("api_token",),
    )
    need_confirm = runner.run(secret_req)
    assert need_confirm.status == "approval_required"
    assert need_confirm.approval_required is not None

    # Grant once -> executes once and redacts secret from BOTH stdout and stderr
    grants.grant(
        scope="qq:private:10001",
        session_id="th-1",
        action_key=need_confirm.approval_required.action_key,
        tier="once",
        actor="qq:10001",
    )
    granted_res = runner.run(secret_req)
    assert granted_res.status == "ok"
    assert api_secret not in granted_res.stdout
    assert api_secret not in granted_res.stderr
    assert "[REDACTED]" in granted_res.stdout
    assert "[REDACTED]" in granted_res.stderr

    # Subsequent call requires confirmation again because grant was "once"
    need_confirm_again = runner.run(secret_req)
    assert need_confirm_again.status == "approval_required"

    # 5. always_ask intercepts even low-risk commands until granted
    low_req = ExecutionRequest(
        command=_python_cmd("print('low-ok')"),
        workspace=ws,
        is_admin=True,
        scope="qq:private:10001",
        session_id="th-2",
        permission_mode="always_ask",
    )
    low_confirm = runner.run(low_req)
    assert low_confirm.status == "approval_required"
    grants.grant(
        scope="qq:private:10001",
        session_id="th-2",
        action_key=low_confirm.approval_required.action_key,
        tier="session",
        actor="qq:10001",
    )
    assert runner.run(low_req).status == "ok"

    # 6. autonomous runs risky workspace commands without prompt while still redacting secrets
    auto_res = runner.run(
        ExecutionRequest(
            command=_python_cmd("import os; print(os.environ['MAISYA_SECRET_API_TOKEN'])"),
            workspace=ws,
            is_admin=True,
            scope="qq:private:10001",
            session_id="th-3",
            permission_mode="autonomous",
            secrets=("api_token",),
        )
    )
    assert auto_res.status == "ok"
    assert api_secret not in auto_res.stdout
    assert "[REDACTED]" in auto_res.stdout

    # 7. Timeout enforces deadline cleanly
    timed_out = runner.run(
        ExecutionRequest(
            command=_python_cmd("import time; time.sleep(5)"),
            workspace=ws,
            is_admin=True,
            permission_mode="autonomous",
            timeout=0.05,
        )
    )
    assert timed_out.status == "timeout"


@pytest.mark.asyncio
async def test_docker_runner_tags_optional_runtime_network_and_secret_redaction(
    tmp_path: Path,
):
    from maisya.runner import ExecutionRequest
    from maisya.sandbox import DockerRunner
    from maisya.trust import PermissionGrantStore

    recorded_cmds: list[list[str]] = []
    secret_val = "docker-secret-token-987654"
    vault = Vault(tmp_path / "vault")
    vault.set("svc_key", secret_val)
    grants = PermissionGrantStore(tmp_path / "maisya.db")
    ws = tmp_path / "work"
    ws.mkdir()

    async def fake_cli(arguments: list[str], **kwargs):
        recorded_cmds.append(list(arguments))
        op = arguments[3]
        if op == "create":
            name = arguments[arguments.index("--name") + 1]
            return ProcessResult(0, name)
        if op == "start":
            return ProcessResult(0, f"leaked={secret_val}", f"err={secret_val}")
        if op == "inspect":
            return ProcessResult(
                0, json.dumps({"Running": False, "OOMKilled": False, "ExitCode": 0})
            )
        if op == "rm":
            return ProcessResult(0, "")
        raise AssertionError(arguments)

    # Default runtime="" must NOT pass --runtime=runsc; tag image is accepted
    settings = SandboxSettings(
        enabled=True,
        image="maisya-sandbox:latest",
        runtime="",
        network_mode="none",
        readonly_rootfs=True,
    )
    docker_runner = DockerRunner(
        settings, vault=vault, grants=grants, cli_runner=fake_cli
    )

    # Non-admin cannot inject vault secrets
    non_admin_secret = await docker_runner.execute(
        ExecutionRequest(
            command=["python3", "-c", "print(1)"],
            workspace=ws,
            is_admin=False,
            secrets=("svc_key",),
        )
    )
    assert non_admin_secret.status == "denied"

    # Admin running without secrets in ask_risky works seamlessly with lowered defaults
    res = await docker_runner.execute(
        ExecutionRequest(
            command=["python3", "app.py"],
            workspace=ws,
            is_admin=True,
            permission_mode="ask_risky",
        )
    )
    assert res.status == "ok"
    create_cmd = next(cmd for cmd in recorded_cmds if cmd[3] == "create")
    assert "--runtime" not in create_cmd
    assert "--network" in create_cmd
    assert create_cmd[create_cmd.index("--network") + 1] == "none"
    assert "--read-only" in create_cmd
    assert "maisya-sandbox:latest" in create_cmd
    assert any("/work/project" in part for part in create_cmd)

    # Non-empty runtime + bridge + writable rootfs + granted secret mount + redaction
    recorded_cmds.clear()
    bridge_runner = DockerRunner(
        SandboxSettings(
            enabled=True,
            image="python:3.12-slim",
            runtime="runc",
            network_mode="bridge",
            readonly_rootfs=False,
        ),
        vault=vault,
        grants=grants,
        cli_runner=fake_cli,
    )
    secret_req = ExecutionRequest(
        command=["cat", "/run/secrets/svc_key"],
        workspace=ws,
        is_admin=True,
        scope="qq:private:10001",
        session_id="th-d1",
        permission_mode="ask_risky",
        secrets=("svc_key",),
    )
    confirm = await bridge_runner.execute(secret_req)
    assert confirm.status == "approval_required"
    assert confirm.approval_required is not None
    grants.grant(
        scope="qq:private:10001",
        session_id="th-d1",
        action_key=confirm.approval_required.action_key,
        tier="always",
        actor="qq:10001",
    )
    granted_docker_res = await bridge_runner.execute(secret_req)
    assert granted_docker_res.status == "ok"
    assert secret_val not in granted_docker_res.stdout
    assert secret_val not in granted_docker_res.stderr
    assert "[REDACTED]" in granted_docker_res.stdout

    create_bridge = next(cmd for cmd in recorded_cmds if cmd[3] == "create")
    assert create_bridge[create_bridge.index("--runtime") + 1] == "runc"
    assert create_bridge[create_bridge.index("--network") + 1] == "bridge"
    assert "--read-only" not in create_bridge
    assert any("/run/secrets/svc_key" in arg and "readonly" in arg for arg in create_bridge)


@pytest.mark.asyncio
async def test_host_runner_async_execute_truncation_cross_chat_and_secret_path(
    tmp_path: Path,
):
    from maisya.runner import ExecutionRequest, HostWorkspaceRunner

    ws = tmp_path / "ws"
    ws.mkdir()
    vault = Vault(tmp_path / "vault")
    sec = "async-secret-value-abcdef"
    vault.set("s1", sec)
    runner = HostWorkspaceRunner(vault=vault)

    # /run/secrets read triggers risky approval_required
    sec_path_res = await runner.execute(
        ExecutionRequest(
            command=["cat", "/run/secrets/s1"],
            workspace=ws,
            is_admin=True,
            permission_mode="ask_risky",
        )
    )
    assert sec_path_res.status == "approval_required"

    # risk_hint="cross_chat" triggers risky approval_required
    cross_res = await runner.execute(
        ExecutionRequest(
            command=_python_cmd("print('cross')"),
            workspace=ws,
            is_admin=True,
            permission_mode="ask_risky",
            risk_hint="cross_chat",
        )
    )
    assert cross_res.status == "approval_required"
    assert cross_res.approval_required is not None
    assert cross_res.approval_required.category == "cross_chat"

    # Async execute with output truncation & secret redaction
    res = await runner.execute(
        ExecutionRequest(
            command=_python_cmd(
                "import os; print(os.environ['MAISYA_SECRET_S1'] + ' ' + 'X' * 5000)"
            ),
            workspace=ws,
            is_admin=True,
            permission_mode="autonomous",
            secrets=("s1",),
            output_bytes=1024,
        )
    )
    assert res.status == "ok"
    assert res.truncated is True
    assert sec not in res.stdout
    assert "[REDACTED]" in res.stdout
