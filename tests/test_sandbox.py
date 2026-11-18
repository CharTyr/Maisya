import pytest
from pathlib import Path

from maisya.sandbox import NetworkPolicy, SandboxSettings


class DockerFixture:
    def __init__(self):
        import asyncio

        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.hold = False
        self.removable = True
        self.running = set()
        self.starts = 0

    async def __call__(self, arguments, **kwargs):
        import json

        from maisya.sandbox import ProcessResult

        operation = arguments[3]
        if operation == "create":
            name = arguments[arguments.index("--name") + 1]
            self.running.add(name)
            return ProcessResult(0, name)
        if operation == "start":
            self.starts += 1
            self.entered.set()
            if self.hold:
                await self.release.wait()
            return ProcessResult(0, "known-secret 42")
        if operation == "inspect":
            return ProcessResult(
                0, json.dumps({"Running": False, "OOMKilled": False, "ExitCode": 0})
            )
        if operation == "rm":
            if self.removable:
                self.running.discard(arguments[-1])
            return ProcessResult(0 if self.removable else 1, "")
        if operation == "ps":
            return ProcessResult(0, "\n".join(self.running))
        raise AssertionError(arguments)


def execution_fixture(tmp_path):
    from maisya.abilities import Profile
    from maisya.sandbox import Sandbox
    from maisya.threads import ThreadStore
    from maisya.workspaces import Workspaces

    store = ThreadStore(tmp_path / "state.db")
    row = store.create(
        Profile(environment="sandbox"),
        "calculate",
        chat_id="chat",
        platform="qq",
        user_id="",
    )
    runner = DockerFixture()
    sandbox = Sandbox(
        SandboxSettings(),
        store,
        runner=runner,
        redact=lambda value: value.replace("known-secret", "[REDACTED]"),
    )
    sandbox.ready = True
    sandbox._own = lambda work: None
    files = Workspaces(tmp_path / "workspaces", store)
    return sandbox, runner, files, row


@pytest.mark.asyncio
async def test_execution_receipt_redacts_and_does_not_replay(tmp_path):
    sandbox, runner, files, row = execution_fixture(tmp_path)
    with files.activity(row) as work:
        result = await sandbox.execute(
            row, work, "calculate", call_id="same", mode="none"
        )
        repeated = await sandbox.execute(
            row, work, "calculate", call_id="same", mode="none"
        )
    assert runner.starts == 1 and repeated["repeated"]
    assert result["output"] == "[REDACTED] 42"
    assert "known-secret" not in str(
        sandbox._receipt((row["thread_id"], row["revision"], "same"))
    )
    await sandbox.close()


@pytest.mark.asyncio
async def test_cancel_removes_container_before_rollback(tmp_path):
    import asyncio

    sandbox, runner, files, row = execution_fixture(tmp_path)
    runner.hold = True
    with files.activity(row) as work:
        work.write("project/value.txt", "before")
        work.commit()
        task = asyncio.create_task(
            sandbox.execute(row, work, "wait", call_id="cancel", mode="none")
        )
        await runner.entered.wait()
        work.write("project/value.txt", "during")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runner.running
        assert work.read("project/value.txt")["text"] == "before"
    await sandbox.close()


@pytest.mark.asyncio
async def test_unconfirmed_cleanup_retains_workspace_lock(tmp_path):
    sandbox, runner, files, row = execution_fixture(tmp_path)
    runner.removable = False
    with files.activity(row) as work:
        with pytest.raises(RuntimeError, match="清理"):
            await sandbox.execute(
                row, work, "calculate", call_id="unknown", mode="none"
            )
        assert work.quarantined and not sandbox.ready
    assert files.quarantines
    with pytest.raises((BlockingIOError, RuntimeError)), files.activity(row):
        pass
    runner.removable = True
    await sandbox.close()
    files.release_quarantines()
    assert not files.quarantines


@pytest.mark.asyncio
async def test_policy_loss_cancels_execution_and_rolls_back(tmp_path):
    import asyncio

    sandbox, runner, files, row = execution_fixture(tmp_path)
    runner.hold = True
    sandbox.networks["fixture"] = {"name": "fixture"}

    async def network(*args):
        return "fixture"

    async def broken_policy(entry):
        raise RuntimeError("网络隔离规则缺失")

    sandbox._network = network
    sandbox._check_network = broken_policy
    with files.activity(row) as work:
        work.write("project/state", "saved")
        work.commit()
        task = asyncio.create_task(sandbox.execute(row, work, "wait", call_id="policy"))
        await runner.entered.wait()
        work.write("project/state", "uncommitted")
        with pytest.raises(RuntimeError, match="规则缺失"):
            await task
        assert not runner.running
        assert work.read("project/state")["text"] == "saved"
    sandbox.networks.clear()
    await sandbox.close()


@pytest.mark.asyncio
async def test_recovery_cleans_only_persisted_owned_resources(tmp_path):
    from maisya.sandbox import Sandbox

    sandbox, runner, _files, row = execution_fixture(tmp_path)
    container = "maisya-" + sandbox.instance + "-0123456789ab"
    runner.running.add(container)
    sandbox._resource(
        "container",
        container,
        {"socket": sandbox.settings.socket, "thread_id": row["thread_id"]},
    )
    replacement = Sandbox(sandbox.settings, sandbox.store, runner=runner)
    await replacement._recover_resources()
    assert not runner.running
    assert replacement._resources() == []
    await replacement.close()


def test_public_policy_checks_actual_destinations_and_host_addresses():
    policy = NetworkPolicy("public", host_addresses=["93.184.216.34"], dns=["1.1.1.1"])
    assert policy.permits("8.8.8.8", 443, "tcp")
    assert policy.permits("8.8.8.8", 8443, "tcp")
    assert policy.permits("1.1.1.1", 53, "udp")
    for address in [
        "127.0.0.1",
        "10.0.0.1",
        "172.17.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.100.100.200",
        "93.184.216.34",
        "::1",
        "::ffff:127.0.0.1",
        "2606:4700:4700::1111",
    ]:
        assert not policy.permits(address, 443, "tcp"), address
    assert not policy.permits("8.8.8.8", 53, "udp")
    assert not policy.permits("8.8.8.8", 53, "tcp")
    assert not policy.permits("8.8.8.8", 443, "udp")
    assert not NetworkPolicy("none").permits("8.8.8.8", 443, "tcp")


def test_restricted_policy_does_not_inherit_public_access():
    policy = NetworkPolicy("restricted", targets=[("10.1.1.2", 22)])
    assert policy.permits("10.1.1.2", 22, "tcp")
    assert not policy.permits("10.1.1.2", 443, "tcp")
    assert not policy.permits("8.8.8.8", 443, "tcp")


def test_network_rules_cover_host_prerouting_forward_and_peers():
    policy = NetworkPolicy("public", host_addresses=["93.184.216.34"])
    rules = policy.nft_rules("ms_test123", "br-ms-test123", "172.30.253.0/24")
    for text in [
        "hook prerouting",
        "hook input",
        "hook forward",
        "93.184.216.34",
        "ipv6",
        "bridge",
        "ct state",
        "drop",
    ]:
        assert text in rules
    assert "flush ruleset" not in rules
    with pytest.raises(ValueError):
        policy.nft_rules("bad; flush ruleset", "br-safe", "172.30.253.0/24")


def test_runtime_configuration_allows_tags_and_requires_local_socket():
    tagged = SandboxSettings(enabled=True, image="python:latest")
    assert tagged.enabled
    assert tagged.image == "python:latest"
    assert tagged.runtime == ""
    assert not tagged.privileged_sandbox_mode

    with pytest.raises(ValueError):
        SandboxSettings(
            enabled=True, image="python:latest", privileged_sandbox_mode=True
        )
    with pytest.raises(ValueError):
        SandboxSettings(
            enabled=True,
            image="image@sha256:" + "0" * 64,
            runtime="runc",
            privileged_sandbox_mode=True,
        )
    with pytest.raises(ValueError):
        SandboxSettings(
            enabled=True, image="image@sha256:" + "0" * 64, socket="tcp://host:2375"
        )
    config = SandboxSettings()
    assert not config.enabled
    assert config.image == "maisya-sandbox:latest"
    assert config.runtime == ""


@pytest.mark.asyncio
async def test_process_timeout_and_output_limit_terminate_the_process():
    import sys

    from maisya.sandbox import process

    with pytest.raises(TimeoutError):
        await process(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.05
        )
    with pytest.raises(RuntimeError, match="输出"):
        await process([sys.executable, "-c", 'print("x"*100000)'], output_bytes=1000)


class ProbeFixture:
    """只回答 probe 关心的 docker 子命令；可切换运行时列表与镜像是否存在。"""

    def __init__(self, *, runtimes=None, image_present=True, cgroup="2"):
        import json

        self.runtimes = runtimes if runtimes is not None else {"runc": {}}
        self.image_present = image_present
        self.cgroup = cgroup
        self._json = json

    async def __call__(self, arguments, **kwargs):
        from maisya.sandbox import ProcessResult

        operation = arguments[3]
        if operation == "info":
            return ProcessResult(
                0,
                self._json.dumps(
                    {"Runtimes": self.runtimes, "CgroupVersion": self.cgroup}
                ),
            )
        if operation == "image":
            return ProcessResult(0 if self.image_present else 1, "")
        if operation == "ps":
            return ProcessResult(0, "")
        raise AssertionError(arguments)


def probe_sandbox(tmp_path, *, settings=None, **fixture):
    from maisya.sandbox import Sandbox
    from maisya.threads import ThreadStore

    store = ThreadStore(tmp_path / "state.db")
    return Sandbox(
        settings or SandboxSettings(),
        store,
        runner=ProbeFixture(**fixture),
        redact=lambda value: value,
    )


@pytest.mark.asyncio
async def test_probe_missing_runtime_lists_available(tmp_path, monkeypatch):
    monkeypatch.setattr("maisya.sandbox.shutil.which", lambda name: "/usr/bin/docker")
    sandbox = probe_sandbox(
        tmp_path,
        settings=SandboxSettings(enabled=True, runtime="runsc"),
        runtimes={"runc": {}, "io.containerd.runc.v2": {}},
    )
    assert await sandbox.probe() is False
    assert "runsc" in sandbox.error
    assert "未安装" in sandbox.error
    assert "runc" in sandbox.error
    assert "runtime" in sandbox.error


@pytest.mark.asyncio
async def test_probe_missing_image_points_at_build_script(tmp_path, monkeypatch):
    monkeypatch.setattr("maisya.sandbox.shutil.which", lambda name: "/usr/bin/docker")
    sandbox = probe_sandbox(
        tmp_path, settings=SandboxSettings(enabled=True), image_present=False
    )
    assert await sandbox.probe() is False
    assert "maisya-sandbox:latest" in sandbox.error
    assert "不存在" in sandbox.error
    assert "build_image.sh" in sandbox.error


@pytest.mark.asyncio
async def test_probe_ready_when_runtime_and_image_available(tmp_path, monkeypatch):
    monkeypatch.setattr("maisya.sandbox.shutil.which", lambda name: "/usr/bin/docker")
    sandbox = probe_sandbox(tmp_path, settings=SandboxSettings(enabled=True))
    assert await sandbox.probe() is True
    assert sandbox.error == ""


class FailingCreateRunner:
    """create 阶段直接失败，用来确认 chown 发生在容器创建之前。"""

    def __init__(self):
        self.commands = []

    async def __call__(self, arguments, **kwargs):
        from maisya.sandbox import ProcessResult

        self.commands.append(list(arguments))
        return ProcessResult(1, "", "boom")


@pytest.mark.asyncio
async def test_docker_runner_hands_workspace_to_container_uid(tmp_path, monkeypatch):
    """容器以非 root 10000:10000 运行，执行前必须先把宿主工作区交给同一 uid。"""
    from maisya.runner import ExecutionRequest
    from maisya.sandbox import DockerRunner

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hi", encoding="utf-8")

    chowned = []

    def fake_chown(target, uid, gid, *, follow_symlinks=True):
        chowned.append((Path(target).name, uid, gid))

    monkeypatch.setattr("maisya.sandbox.os.chown", fake_chown)
    runner = FailingCreateRunner()
    docker = DockerRunner(SandboxSettings(enabled=True), cli_runner=runner)
    result = await docker.execute(
        ExecutionRequest(command=["true"], workspace=workspace)
    )

    assert result.status == "error"
    assert any(name == "project" and uid == 10000 and gid == 10000 for name, uid, gid in chowned)
    assert any(name == "note.txt" for name, _uid, _gid in chowned)
    assert any("create" in cmd for cmd in runner.commands)


@pytest.mark.asyncio
async def test_docker_runner_tolerates_chown_failure_like_docker_desktop(tmp_path, monkeypatch):
    """宿主与容器不共享 uid 映射（如 Docker Desktop）时，chown 失败不应阻断执行。"""

    from maisya.runner import ExecutionRequest
    from maisya.sandbox import DockerRunner

    workspace = tmp_path / "project"
    workspace.mkdir()

    def denied(target, uid, gid, *, follow_symlinks=True):
        raise PermissionError("denied")

    monkeypatch.setattr("maisya.sandbox.os.chown", denied)
    runner = FailingCreateRunner()
    result = await DockerRunner(
        SandboxSettings(enabled=True), cli_runner=runner
    ).execute(ExecutionRequest(command=["true"], workspace=workspace))

    assert any("create" in cmd for cmd in runner.commands)
    assert "10000:10000" not in result.error
