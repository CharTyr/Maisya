import re
from pathlib import Path

from maibot_sdk.config import PluginConfigBase
from pydantic import Field, field_validator

from .sandbox import SandboxSettings
from .vault import private_directory

PROACTIVE_LEVELS = frozenset({"quiet", "care", "lively"})
USER_ID_RE = re.compile(r"^([a-z][a-z0-9_-]{0,31}):([^\s:]{1,200})$")


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
    chat_proactive_levels: dict[str, str] = Field(default_factory=dict)
    acquaintance_users: list[str] = Field(default_factory=list)
    longing_quiet_hours: int = Field(default=72, ge=12, le=720)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)

    @field_validator("chat_proactive_levels")
    @classmethod
    def _validate_chat_levels(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 128:
            raise ValueError("分会话主动程度最多设置 128 项")
        cleaned = {}
        for chat_id, level in value.items():
            if not isinstance(chat_id, str) or not 1 <= len(chat_id.strip()) <= 256:
                raise ValueError("分会话主动程度包含无效会话标识")
            if level not in PROACTIVE_LEVELS:
                raise ValueError("主动程度只能是 quiet / care / lively")
            cleaned[chat_id.strip()] = level
        return cleaned

    @field_validator("acquaintance_users")
    @classmethod
    def _validate_acquaintances(cls, value: list[str]) -> list[str]:
        if len(value) > 200:
            raise ValueError("熟人列表最多包含 200 项")
        cleaned = []
        for entry in value:
            if not isinstance(entry, str):
                raise ValueError("熟人条目须为 platform:user_id 字符串")
            raw = entry.strip()
            if ":" not in raw:
                raise ValueError("熟人条目须使用 platform:user_id 格式")
            platform, user = raw.split(":", 1)
            normalized = f"{platform.strip().lower()}:{user.strip()}"
            if not USER_ID_RE.fullmatch(normalized):
                raise ValueError("熟人条目格式无效")
            if normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned


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
