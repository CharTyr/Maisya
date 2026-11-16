import re
from pathlib import Path
from typing import Literal

from maibot_sdk.config import PluginConfigBase
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .pathsafe import path_is_link_like, safe_chmod
from .sandbox import SandboxSettings
from .trust import normalize_admin_id
from .vault import VaultError

PROACTIVE_LEVELS = frozenset({"quiet", "care", "lively"})
UI_STYLES = frozenset({"maiwork", "millennium", "modern", "auto"})
USER_ID_RE = re.compile(r"^([a-z][a-z0-9_-]{0,31}):([^\s:]{1,200})$")


def canonical_ui_style(value: str | None) -> str:
    style = str(value or "").strip().lower()
    if style == "maiwork":
        return "modern"
    if style in {"millennium", "modern", "auto"}:
        return style
    return "millennium"


class PluginSettings(PluginConfigBase):
    enabled: bool = False
    config_version: str = "0.1.0"


class ProactiveSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sleep_window: str = ""
    proactive_level: str = Field(default="care", pattern=r"^(quiet|care|lively)$")
    daily_proactive_limit: int = Field(default=1, ge=0, le=50)
    heartbeat_enabled: bool = True
    heartbeat_interval_minutes: int = Field(default=60, ge=5, le=1440)
    heartbeat_profile: str = Field(
        default="default",
        pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$",
    )
    chat_proactive_levels: dict[str, str] = Field(default_factory=dict)
    acquaintance_users: list[str] = Field(default_factory=list)
    longing_quiet_hours: int = Field(default=0, ge=0, le=720)
    tz_offset_minutes: int = Field(default=480, ge=-720, le=840)


WEB_PROVIDER_PATTERN = (
    r"^(auto|host|google_search_plugin"
    r"|mcp(:[A-Za-z][A-Za-z0-9_-]{0,63})?"
    r"|plugin_api(:[A-Za-z][A-Za-z0-9_-]{0,63})?"
    r"|plugin:[A-Za-z0-9_.-]{1,128}"
    r"|web:[A-Za-z][A-Za-z0-9_-]{0,63})$"
)


class Settings(PluginConfigBase):
    model_config = ConfigDict(validate_assignment=True, extra="ignore")

    plugin: PluginSettings = Field(default_factory=PluginSettings)
    admins: list[str] = Field(default_factory=list)
    permission_mode: Literal["ask_risky", "autonomous", "always_ask"] = "ask_risky"
    enabled_chats: list[str] = Field(default_factory=list)
    default_model: str = ""
    agent_max_steps: int = Field(default=16, ge=1, le=64)
    agent_timeout: int = Field(default=180, ge=10, le=900)
    agent_max_active: int = Field(default=8, ge=1, le=32)
    thread_idle_seconds: int = Field(default=900, ge=0, le=86400)
    thread_context_tokens: int = Field(default=262144, ge=16384, le=2000000)
    thread_context_chars: int = Field(default=48000, ge=24000, le=2000000)
    web_provider: str = Field(
        default="auto",
        pattern=WEB_PROVIDER_PATTERN,
    )
    ui_style: str = Field(
        default="millennium", pattern=r"^(maiwork|millennium|modern|auto)$"
    )
    sleep_window: str = Field(
        default="",
        pattern=r"^$|^\d{1,2}(?::\d{2})?-\d{1,2}(?::\d{2})?$",
    )
    proactive_level: str = Field(default="care", pattern=r"^(quiet|care|lively)$")
    daily_proactive_limit: int = Field(default=1, ge=0, le=50)
    heartbeat_enabled: bool = True
    heartbeat_interval_minutes: int = Field(default=60, ge=5, le=1440)
    heartbeat_profile: str = Field(
        default="default",
        pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$",
    )
    chat_proactive_levels: dict[str, str] = Field(default_factory=dict)
    acquaintance_users: list[str] = Field(default_factory=list)
    longing_quiet_hours: int = Field(default=0, ge=0, le=720)
    tz_offset_minutes: int = Field(default=480, ge=-720, le=840)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)

    @field_validator("admins")
    @classmethod
    def _validate_admins(cls, value: list[str]) -> list[str]:
        if len(value) > 200:
            raise ValueError("管理员列表最多包含 200 项")
        cleaned: list[str] = []
        seen: set[str] = set()
        for entry in value:
            normalized = normalize_admin_id(entry)
            if not normalized:
                raise ValueError(f"管理员条目格式无效：{entry!r}")
            if normalized not in seen:
                seen.add(normalized)
                cleaned.append(normalized)
        return cleaned

    @field_validator("default_model")
    @classmethod
    def _validate_default_model(cls, value: str) -> str:
        text = str(value or "").strip()
        if len(text) > 256:
            raise ValueError("default_model 长度不能超过 256 个字符")
        return text

    def proactive_settings(self) -> ProactiveSettings:
        return ProactiveSettings(
            sleep_window=self.sleep_window,
            proactive_level=self.proactive_level,
            daily_proactive_limit=self.daily_proactive_limit,
            heartbeat_interval_minutes=self.heartbeat_interval_minutes,
            heartbeat_profile=self.heartbeat_profile,
            chat_proactive_levels=dict(self.chat_proactive_levels),
            acquaintance_users=list(self.acquaintance_users),
            longing_quiet_hours=self.longing_quiet_hours,
            tz_offset_minutes=self.tz_offset_minutes,
        )

    @field_validator("sleep_window")
    @classmethod
    def _validate_sleep_window(cls, value: str) -> str:
        if not value:
            return ""
        start_raw, end_raw = value.split("-", 1)
        for endpoint in (start_raw, end_raw):
            if ":" in endpoint:
                hour_str, minute_str = endpoint.split(":", 1)
                hour = int(hour_str)
                minute = int(minute_str)
            else:
                hour = int(endpoint)
                minute = 0
            if not (0 <= hour <= 24) or not (0 <= minute <= 59):
                raise ValueError(
                    "sleep_window 小时需在 0..24 之间、分钟需在 0..59 之间"
                )
            if hour == 24 and minute != 0:
                raise ValueError("sleep_window 为 24 时分钟只能为 00")
        return value

    @field_validator("tz_offset_minutes", mode="before")
    @classmethod
    def _reject_bool_tz_offset(cls, value):
        if isinstance(value, bool):
            raise ValueError("tz_offset_minutes 必须是整数分钟偏移，不能为布尔值")
        return value

    @field_validator("longing_quiet_hours")
    @classmethod
    def _validate_longing_quiet_hours(cls, value: int) -> int:
        if value != 0 and not (12 <= value <= 720):
            raise ValueError("久违问候安静阈值须为 0（关闭）或 12 到 720 小时")
        return value

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


def _ensure_bounded_private_dir(root: Path, relative: str | None = None) -> Path:
    """仅在 data/maisya 及其子目录边界内检查符号链接并收紧权限，不向上检查宿主祖先目录。"""
    candidates = [root]
    if relative:
        current = root
        for part in Path(relative).parts:
            current = current / part
            candidates.append(current)
    for candidate in candidates:
        if path_is_link_like(candidate):
            raise VaultError("保险箱路径不能经过符号链接")
        candidate.mkdir(mode=0o700, exist_ok=True)
        if path_is_link_like(candidate):
            raise VaultError("保险箱路径不能经过符号链接")
        safe_chmod(candidate, 0o700)
    return candidates[-1]


def initialize_data(sdk_data_dir: Path) -> Path:
    sdk_data_dir = Path(sdk_data_dir).absolute()
    if (
        sdk_data_dir.parent.name != "plugins"
        or sdk_data_dir.parent.parent.name != "data"
    ):
        raise ValueError("宿主 SDK data_dir 应为 <宿主根>/data/plugins/<plugin_id>")
    data_root = sdk_data_dir.parent.parent
    data_root.mkdir(parents=True, exist_ok=True)
    root = _ensure_bounded_private_dir(data_root / "maisya")
    for relative in (
        "vault",
        "abilities/profiles",
        "abilities/skills",
        "memory/self",
        "memory/chats",
        "workspaces",
    ):
        _ensure_bounded_private_dir(root, relative)
    return root
