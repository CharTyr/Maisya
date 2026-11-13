from pathlib import Path

from maibot_sdk.config import PluginConfigBase
from pydantic import Field

from .sandbox import SandboxSettings
from .vault import private_directory


class PluginSettings(PluginConfigBase):
    enabled: bool = False
    config_version: str = "0.1.0"


class Settings(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    enabled_chats: list[str] = Field(default_factory=list)
    agent_max_steps: int = Field(default=16, ge=1, le=64)
    agent_timeout: int = Field(default=180, ge=10, le=900)
    agent_max_active: int = Field(default=8, ge=1, le=32)
    thread_idle_seconds: int = Field(default=900, ge=0, le=86400)
    thread_context_chars: int = Field(default=48000, ge=24000, le=256000)
    sleep_window: str = Field(default="", pattern=r"^$|^\d{1,2}-\d{1,2}$")
    proactive_level: str = Field(default="care", pattern=r"^(quiet|care|lively)$")
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)


def initialize_data(sdk_data_dir: Path) -> Path:
    sdk_data_dir = Path(sdk_data_dir).absolute()
    if (
        sdk_data_dir.parent.name != "plugins"
        or sdk_data_dir.parent.parent.name != "data"
    ):
        raise ValueError("宿主 SDK data_dir 应为 <宿主根>/data/plugins/<plugin_id>")
    root = private_directory(sdk_data_dir.parent.parent / "maisya")
    for relative in (
        "vault",
        "abilities/profiles",
        "abilities/skills",
        "memory/self",
        "memory/chats",
        "workspaces",
    ):
        private_directory(root / relative)
    return root
