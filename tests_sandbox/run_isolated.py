"""在独立 Docker/containerd 实例中验收候选沙箱，不修改线上运行时配置。"""

import base64
import hashlib
import io
import re
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REMOTE = r"""
import base64, hashlib, io, json, os, re, shutil, signal, socket, subprocess, sys, tarfile, tempfile, time, urllib.request
from pathlib import Path
host = Path(sys.argv[1]).resolve()
os.umask(0o077)
stage = Path(tempfile.mkdtemp(prefix='maisya-sandbox-ci-',dir='/var/tmp'))
stage.chmod(0o755)
assert host not in stage.parents and 'plugins' not in stage.parts
environment = {'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','HOME':str(stage),'DOCKER_CONFIG':str(stage/'client'),'PYTHONDONTWRITEBYTECODE':'1'}
processes = []
logs = []
status = 1
def emit(phase, **values):
 print(json.dumps({'phase':phase,**values}),flush=True)
def command(arguments, timeout=120, checked=True):
 result = subprocess.run(arguments,cwd=stage,env=environment,capture_output=True,text=True,timeout=timeout)
 if checked and result.returncode:
  raise RuntimeError(str(arguments[0])+' failed: '+result.stderr[-1400:])
 return result
def snapshot():
 digest = hashlib.sha256()
 for directory in (host/'src',host/'plugins'):
  for path in sorted(directory.rglob('*')):
   if path.is_file() and path.suffix in {'.py','.json'}:
    info = path.stat()
    digest.update(str(path.relative_to(host)).encode())
    digest.update(str((info.st_size,info.st_mtime_ns)).encode())
 digest.update((host/'config/bot_config.toml').read_bytes())
 return digest.hexdigest()
def start(arguments, name):
 output = (stage/(name+'.log')).open('wb')
 logs.append(output)
 process = subprocess.Popen(arguments,cwd=stage,env=environment,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,preexec_fn=lambda: (os.setsid(),os.umask(0o022)))
 processes.append(process)
 return process
def wait_socket(path, process, ready):
 for attempt in range(60):
  if process.poll() is not None:
   raise RuntimeError('isolated daemon exited before ready')
  if Path(path).exists() and ready(): return
  time.sleep(0.5)
 raise RuntimeError('isolated daemon startup timed out')
before = snapshot()
original_tables = command(['nft','list','tables']).stdout
original_docker = command(['docker','info','--format','{{json .Runtimes}} {{.DefaultRuntime}}']).stdout
backup = Path('/root/backups')/stage.name
backup.mkdir(mode=0o700,parents=True,exist_ok=False)
(backup/'firewall-before.nft').write_text(command(['nft','list','ruleset']).stdout)
if shutil.which('iptables-save'):
 (backup/'firewall-before.iptables').write_text(command(['iptables-save']).stdout)
try:
 assert os.geteuid() == 0
 assert shutil.disk_usage(stage).free > 5*1024**3
 assert command(['findmnt','-n','-o','FSTYPE','-T',str(stage)]).stdout.strip() not in {'tmpfs','ramfs'}
 assert Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() == '1'
 for binary in ('docker','dockerd','containerd','nft','mount','umount','ip','zstd'):
  assert shutil.which(binary), binary+' missing'
 archive = base64.b64decode(ARCHIVE)
 assert hashlib.sha256(archive).hexdigest() == DIGEST
 with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as source:
  for member in source.getmembers():
   assert member.isfile() and not Path(member.name).is_absolute() and '..' not in Path(member.name).parts
  source.extractall(stage/'candidate',filter='data')
 root = 'https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/'
 bundle = stage/'gvisor.tar.zstd'
 with urllib.request.urlopen(root+'gvisor.tar.zstd',timeout=300) as incoming,bundle.open('wb') as output:
  shutil.copyfileobj(incoming,output)
 with urllib.request.urlopen(root+'gvisor.tar.zstd.sha512',timeout=30) as incoming:
  expected = incoming.read().decode().split()[0]
 assert hashlib.sha512(bundle.read_bytes()).hexdigest() == expected
 command(['tar','-I','zstd','-xf',str(bundle)],timeout=60)
 runsc = stage/'runsc'
 bindir = stage/'gvisor-bin'
 assert runsc.exists() and bindir.is_dir()
 emit('runsc_verified',sha256=hashlib.sha256(runsc.read_bytes()).hexdigest())
 containerd_socket = stage/'containerd.sock'
 configuration = stage/'containerd.toml'
 configuration.write_text('version = 2\nroot = '+json.dumps(str(stage/'containerd-root'))+'\nstate = '+json.dumps(str(stage/'containerd-state'))+'\ndisabled_plugins = ["io.containerd.grpc.v1.cri"]\n[grpc]\naddress = '+json.dumps(str(containerd_socket))+'\n')
 containerd = start(['containerd','--config',str(configuration)],'containerd')
 wait_socket(containerd_socket,containerd,lambda:True)
 docker_socket = stage/'docker.sock'
 daemon_config = stage/'daemon.json'
 daemon_config.write_text('{}')
 daemon = start(['dockerd','--config-file',str(daemon_config),'--host','unix://'+str(docker_socket),'--data-root',str(stage/'docker-root'),'--exec-root',str(stage/'docker-exec'),'--pidfile',str(stage/'docker.pid'),'--containerd',str(containerd_socket),'--containerd-namespace','maisya-ci','--containerd-plugins-namespace','maisya-ci-plugins','--bridge','none','--iptables=false','--ip6tables=false','--ip-forward=false','--ip-masq=false','--add-runtime','runsc='+str(runsc)],'docker')
 docker = ['docker','--host','unix://'+str(docker_socket)]
 wait_socket(docker_socket,daemon,lambda: command(docker+['info'],timeout=3,checked=False).returncode==0)
 emit('isolated_daemons_ready')
 seeds = []
 for tag in ('python:3.13-slim-bookworm','node:24-bookworm-slim'):
  command(docker+['pull',tag],timeout=240)
  digest = json.loads(command(docker+['image','inspect','--format','{{json .RepoDigests}}',tag]).stdout)[0]
  seed = command(docker+['create',digest]).stdout.strip()
  seeds.append(seed)
  emit('official_image_ready',image=digest)
 python_seed,node_seed = seeds
 for source,target in [('/usr/local/bin/node','/usr/local/bin/node'),('/usr/local/lib/node_modules','/usr/local/lib/node_modules'),('/usr/lib/x86_64-linux-gnu/libstdc++.so.6','/usr/lib/x86_64-linux-gnu/libstdc++.so.6'),('/usr/lib/x86_64-linux-gnu/libgcc_s.so.1','/usr/lib/x86_64-linux-gnu/libgcc_s.so.1')]:
  temporary = stage/('asset-'+str(len(list(stage.glob('asset-*')))))
  command(docker+['cp','--follow-link',node_seed+':'+source,str(temporary)])
  command(docker+['cp',str(temporary),python_seed+':'+target])
 bin_dir = stage/'bin-links'
 bin_dir.mkdir()
 (bin_dir/'npm').symlink_to('../lib/node_modules/npm/bin/npm-cli.js')
 (bin_dir/'npx').symlink_to('../lib/node_modules/npm/bin/npx-cli.js')
 command(docker+['cp',str(bin_dir)+'/.',python_seed+':/usr/local/bin'])
 templates = stage/'maisya/templates'
 templates.mkdir(parents=True)
 fixture = templates/'probe.py'
 fixture.write_text('#!/usr/local/bin/python\nimport hashlib,json,os,stat,sys\nfrom pathlib import Path\narguments=json.loads(sys.argv[1])\npath=Path("/run/secrets/fixture")\nassert stat.S_IMODE(path.stat().st_mode)==0o400\nassert not list(Path("/work/project").iterdir())\nprint(json.dumps({"label":arguments["label"],"digest":hashlib.sha256(path.read_bytes()).hexdigest(),"uid":os.getuid()}))\n')
 fixture.chmod(0o755)
 command(['chmod','-R','0755',str(stage/'maisya')])
 command(docker+['cp',str(stage/'maisya'),python_seed+':/opt/'])
 workdir = stage/'work'
 for sub in ('project','in','out','cache'):
  (workdir/sub).mkdir(parents=True)
 command(['chmod','-R','0755',str(workdir)])
 command(docker+['cp',str(workdir),python_seed+':/work'])
 image = command(docker+['commit',python_seed]).stdout.strip()
 command(docker+['rm',*seeds])
 emit('test_image_ready',image=image)
 env = dict(environment,PYTHONPATH=str(stage/'candidate'),MAISYA_SANDBOX_IMAGE=image,MAISYA_SANDBOX_SOCKET='unix://'+str(docker_socket))
 result = subprocess.run([str(host/'.venv/bin/python'),'-B',str(stage/'candidate/tests_sandbox/check_runtime.py')],cwd=stage,env=env,capture_output=True,text=True,timeout=420)
 print(result.stdout,flush=True)
 if result.returncode: print(result.stderr[-12000:],flush=True)
 status = result.returncode
finally:
 marker = stage/'runtime-instance.json'
 if marker.exists():
  instance = json.loads(marker.read_text())['instance']
  if re.fullmatch(r'[a-f0-9]{8}',instance):
   network_cleared = False
   try:
    ids=command(docker+['ps','--all','--quiet'],checked=False).stdout.split()
    if ids: command(docker+['rm','--force',*ids],checked=False)
    networks=command(docker+['network','ls','--filter','label=maisya.instance='+instance,'--quiet'],checked=False).stdout.split()
    for network in networks: command(docker+['network','rm',network],checked=False)
    remaining_containers=command(docker+['ps','--all','--quiet'],checked=False)
    remaining_networks=command(docker+['network','ls','--filter','label=maisya.instance='+instance,'--quiet'],checked=False)
    network_cleared=remaining_containers.returncode==0 and remaining_networks.returncode==0 and not remaining_containers.stdout.strip() and not remaining_networks.stdout.strip()
   except (OSError,subprocess.TimeoutExpired): pass
   if network_cleared:
    for index in range(8):
     bridge='br-ms-'+instance[:6]+str(index)
     chain='MS'+instance+str(index)
     if shutil.which('iptables'):
      for hook in ('DOCKER-USER','FORWARD'):
       for direction in ('-i','-o'): command(['iptables','-w','5','-D',hook,direction,bridge,'-j',chain],checked=False)
      command(['iptables','-w','5','-F',chain],checked=False)
      command(['iptables','-w','5','-X',chain],checked=False)
     for family in ('inet','bridge'): command(['nft','delete','table',family,'ms_'+instance+'_'+str(index)],checked=False)
    command(['nft','delete','table','ip','ms_'+instance+'_dnat'],checked=False)
   else:
    emit('cleanup_blocked',reason='containers_or_networks_unconfirmed_fences_retained')
 for process in reversed(processes):
  if process.poll() is None:
   os.killpg(process.pid,signal.SIGTERM)
   try: process.wait(timeout=15)
   except subprocess.TimeoutExpired:
    os.killpg(process.pid,signal.SIGKILL)
    process.wait(timeout=5)
 for output in logs: output.close()
 unchanged = snapshot() == before
 same_docker = command(['docker','info','--format','{{json .Runtimes}} {{.DefaultRuntime}}']).stdout == original_docker
 same_tables = command(['nft','list','tables']).stdout == original_tables
 emit('production_checks',host_unchanged=unchanged,original_runtime_unchanged=same_docker,table_names_restored=same_tables)
 if not unchanged or not same_docker or not same_tables: status=1
 if status:
  for log in ('docker.log','containerd.log'):
   path=stage/log
   if path.exists(): print(log+':\n'+path.read_text(errors='replace')[-5000:])
 # gVisor 在 runtime exec-root pin 的 null-netns 属正常残留，daemon 退出后惰性卸载
 for attempt in range(20):
  mounted = command(['findmnt','-rn','-o','TARGET']).stdout.splitlines()
  leftovers=[path for path in mounted if path.startswith(str(stage)+'/')]
  if not leftovers: break
  for target in leftovers: command(['umount','-l',target],checked=False)
  time.sleep(0.5)
 mounted = command(['findmnt','-rn','-o','TARGET']).stdout.splitlines()
 leftovers=[path for path in mounted if path.startswith(str(stage)+'/')]
 if leftovers:
  emit('cleanup_blocked',mounts_remaining=len(leftovers))
  status=1
 else:
  shutil.rmtree(stage)
  emit('isolated_directory_cleaned',cleaned=not stage.exists())
  if status == 0 and backup.exists():
   shutil.rmtree(backup)
   emit('backup_cleaned',cleaned=not backup.exists())
sys.exit(status)
"""


def _bundle_paths(root: Path = ROOT) -> list[Path]:
    candidates = (
        list((root / "maisya").glob("*.py"))
        + [root / "maisya/viz.css"]
        + list((root / "presets/profiles").rglob("*"))
        + list((root / "presets/skills").rglob("*"))
        + list((root / "presets/memory").rglob("*"))
        + [root / "tests_sandbox/check_runtime.py"]
    )
    return [p for p in candidates if p.is_file() and not p.is_symlink()]


def main():
    if "--execute" not in sys.argv:
        print(
            "隔离验收计划：仅在 /var/tmp/maisya-sandbox-ci-* 放候选代码、经校验的 runsc 和官方镜像；启动独立 Docker/containerd，不修改现有 daemon 配置；临时创建专用 bridge/nft 表及测试容器；结束后清理并复核线上源码、原运行时和防火墙表名。"
        )
        return
    local = (ROOT / "AGENTS.local.md").read_text()
    ssh = shlex.split(re.search(r"ssh [^\n`]+", local).group())
    ssh[ssh.index("-i") + 1] = str(Path(ssh[ssh.index("-i") + 1]).expanduser())
    host = re.search(r"宿主目录：`([^`]+)`", local).group(1)
    payload = io.BytesIO()
    paths = _bundle_paths(ROOT)
    with tarfile.open(fileobj=payload, mode="w:gz") as target:
        for path in paths:
            if path.is_file() and not path.is_symlink():
                target.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    archive = payload.getvalue()
    script = (
        "ARCHIVE="
        + repr(base64.b64encode(archive).decode())
        + "\nDIGEST="
        + repr(hashlib.sha256(archive).hexdigest())
        + "\n"
        + REMOTE
    )
    result = subprocess.run(
        ssh + ["-o", "BatchMode=yes", shlex.join(["python3", "-", host])],
        input=script,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    log = ROOT / ".local-checks/runtime-contract.log"
    log.write_text(result.stdout + result.stderr)
    for line in result.stdout.splitlines():
        if line.startswith("{"):
            print(line)
    if result.returncode:
        print("隔离验收失败，详细日志保存在 .local-checks/runtime-contract.log")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
