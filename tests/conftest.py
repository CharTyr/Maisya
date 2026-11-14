"""跨平台测试共享夹具与符号链接辅助函数。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def safe_symlink(
    link_path: str | os.PathLike[str],
    target_path: str | os.PathLike[str],
) -> Path:
    """创建符号链接；仅在 Windows 缺少 SeCreateSymbolicLinkPrivilege (WinError 1314) 时跳过。"""
    link = Path(link_path)
    target = Path(target_path)
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as err:
        if os.name == "nt" and getattr(err, "winerror", None) == 1314:
            pytest.skip(
                "Windows requires SeCreateSymbolicLinkPrivilege (WinError 1314)"
            )
        raise
    return link


@pytest.fixture
def simulate_no_dir_fd(monkeypatch):
    """移除 os.O_NOFOLLOW/os.O_DIRECTORY 并清空 os.supports_dir_fd 以模拟无 dir_fd 运行时。"""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    monkeypatch.setattr(os, "supports_dir_fd", set(), raising=False)
