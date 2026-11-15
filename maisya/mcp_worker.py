"""受信 MCP 启动器：先限制资源、降权，再执行管理员登记的程序。"""

import json
import os
import sys
from pathlib import Path


def main():
    command, memory_mib, uid, group_name = json.loads(sys.argv[1])
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) for part in command)
    ):
        raise ValueError("MCP 启动参数无效")
    if sys.platform.startswith("linux"):
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2, 16 * 1024**2))
        if os.geteuid() == 0:
            base = Path("/sys/fs/cgroup") / group_name
            leaf = base / str(uid)
            leaf.mkdir(exist_ok=True)
            (leaf / "memory.max").write_text(
                str(memory_mib * 1024**2), encoding="utf-8"
            )
            (leaf / "pids.max").write_text("64", encoding="utf-8")
            (leaf / "cgroup.procs").write_text(str(os.getpid()), encoding="utf-8")
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            # Only apply per-UID RLIMIT_NPROC after switching to the isolated worker UID;
            # non-root shares UID with the host and RLIMIT_AS breaks V8/Go virtual mappings.
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
