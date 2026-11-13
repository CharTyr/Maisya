"""从本地忽略文件读取连接配置，在独立临时目录运行真实宿主契约。"""

import base64
import hashlib
import io
import re
import shlex
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REMOTE = r"""
import base64, hashlib, io, json, os, re, shutil, subprocess, sys, tarfile, tempfile
from pathlib import Path
root = Path(sys.argv[1]).resolve()
os.umask(0o077)
stage = Path(tempfile.mkdtemp(prefix='maisya-ci-'))
assert root not in stage.parents and 'plugins' not in stage.parts
os.chdir(stage)
def snapshot():
    digest = hashlib.sha256()
    for directory in (root/'src', root/'plugins'):
        for path in sorted(directory.rglob('*')):
            if path.is_file() and path.suffix in ('.py', '.json'):
                stat = path.stat()
                digest.update(str(path.relative_to(root)).encode())
                digest.update(str((stat.st_size,stat.st_mtime_ns)).encode())
    digest.update((root/'config/bot_config.toml').read_bytes())
    return digest.hexdigest()
before = snapshot()
try:
    archive = base64.b64decode(ARCHIVE)
    assert hashlib.sha256(archive).hexdigest() == DIGEST
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as source:
        for member in source.getmembers():
            path = Path(member.name)
            assert member.isfile() and not path.is_absolute() and '..' not in path.parts
        source.extractall(stage/'candidate', filter='data')
    (stage/'home').mkdir()
    (stage/'tmp').mkdir()
    env = dict(os.environ, HOME=str(stage/'home'), TMPDIR=str(stage/'tmp'),
               PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(root))
    result = subprocess.run([str(root/'.venv/bin/python'), '-B',
        str(stage/'candidate/tests_host/test_contract.py')], cwd=stage, env=env,
        capture_output=True, text=True, timeout=120)
    output = result.stdout + result.stderr
    print(output)
    unchanged = snapshot() == before
    print(json.dumps({'host_unchanged': unchanged, 'exit_code': result.returncode,
                      'archive_sha256': DIGEST}))
    status = result.returncode if unchanged else 1
finally:
    os.chdir(stage.parent)
    shutil.rmtree(stage)
    print(json.dumps({'isolated_directory_cleaned': not stage.exists()}))
sys.exit(status)
"""


def main():
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
    paths += [
        ROOT / "tests_host/test_contract.py",
        *list((ROOT / "webui").glob("*.html")),
    ]
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
        + "\n"
        + REMOTE
    )
    result = subprocess.run(
        ssh + ["-o", "BatchMode=yes", shlex.join(["python3", "-", host])],
        input=script,
        text=True,
        capture_output=True,
        timeout=160,
        check=False,
    )
    output = ROOT / ".local-checks/host-contract.log"
    output.parent.mkdir(exist_ok=True)
    output.write_text(result.stdout + result.stderr)
    print(result.stdout)
    if result.returncode:
        print(result.stderr)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
