"""load and validate config.yaml for llm-usage.

looks for config.yaml in the project root (next to pyproject.toml).
missing keys fall back to built-in defaults.

config now includes a provider section so usage sources are pluggable
(for example claude oauth API vs codex usage API).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from prism.logging import get_logger

logger = get_logger()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass
class ReadoutConfig:
    every_percent: float = 5.0
    threshold: float = 100.0
    granularity: str = "ones"
    blink_on: float = 0.12
    blink_off: float = 0.12
    fade_speed: int = 2
    digit_pause: float = 0.5
    end_pause: float = 1.0


@dataclass
class KeyboardConfig:
    enabled: bool = True
    min_brightness: float = 0.0
    fade_speed: int = 0
    pulse_threshold: float = 10.0
    pulse_period: float = 3.0
    readout: ReadoutConfig = field(default_factory=ReadoutConfig)


@dataclass
class OutputConfig:
    speech: bool = True
    keyboard: KeyboardConfig = field(default_factory=KeyboardConfig)


@dataclass
class StttsConfig:
    """sttts mic coordination — speech waits indefinitely for mic idle.

    when enabled, all speech is held until the mic stops capturing.
    no timeout; speech is never forced through during active mic.
    """

    enabled: bool = True
    relay_url: str = "http://127.0.0.1:8393"


@dataclass
class CodexProviderConfig:
    auth_file: str = "~/.codex/auth.json"
    fallback_auth_files: list[str] = field(
        default_factory=lambda: ["~/.local/share/opencode/auth.json"]
    )
    usage_url: str = "https://chatgpt.com/backend-api/wham/usage"
    refresh_url: str = "https://auth.openai.com/oauth/token"
    refresh_client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    request_timeout_seconds: int = 10
    sessions_root: str = "~/.codex/sessions"
    max_staleness_seconds: int = 1800


@dataclass
class ProviderConfig:
    name: str = "claude"
    codex: CodexProviderConfig = field(default_factory=CodexProviderConfig)


@dataclass
class LimitWarningsConfig:
    """time-based warnings when projected to exceed 100% utilization.

    when the burn rate projects that utilization will hit the limit
    before the window resets, these minute thresholds trigger voice
    readouts as the estimated time-to-limit crosses each value.
    only active when on track to exceed — otherwise the standard
    percentage-based readout system handles announcements.
    """

    minute_thresholds: list[float] = field(
        default_factory=lambda: [60, 30, 15, 5],
    )
    """descending list of minutes-to-limit that trigger a readout.
    when the estimated minutes until 100% drops below each value
    for the first time, a voice announcement fires."""


@dataclass
class Config:
    window: str = "five_hour"
    poll_interval: int = 60
    accounts: list[str] = field(default_factory=list)
    switch_threshold: float = 90.0
    status_port: int = 8387
    limit_warnings: LimitWarningsConfig = field(default_factory=LimitWarningsConfig)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    sttts: StttsConfig = field(default_factory=StttsConfig)


def _parse_nested_dataclass(dataclass_type, raw: dict):
    """parse a dict into a dataclass, ignoring unknown keys."""
    return dataclass_type(
        **{key: raw[key] for key in dataclass_type.__dataclass_fields__ if key in raw}
    )


def load_config(path: Path | None = None) -> Config:
    """load config from yaml file, falling back to defaults for missing keys."""
    config_path = path or DEFAULT_CONFIG_PATH

    if not config_path.exists():
        logger.info("no config.yaml found, using defaults")
        return Config()

    with open(config_path) as handle:
        raw = yaml.safe_load(handle) or {}

    logger.debug("loaded config", path=str(config_path))

    # parse output section
    output_raw = raw.pop("output", {}) or {}

    # keyboard is nested: output.keyboard.{enabled, min_brightness, ...readout}
    keyboard_raw = output_raw.pop("keyboard", {}) or {}
    readout_raw = keyboard_raw.pop("readout", {}) or {}

    readout = _parse_nested_dataclass(ReadoutConfig, readout_raw)

    keyboard = _parse_nested_dataclass(KeyboardConfig, keyboard_raw)
    keyboard.readout = readout

    # speech is a simple bool at output.speech
    output = OutputConfig(
        speech=output_raw.get("speech", True),
        keyboard=keyboard,
    )

    # parse sttts section
    sttts_raw = raw.pop("sttts", {}) or {}
    sttts = _parse_nested_dataclass(StttsConfig, sttts_raw)

    # parse provider section
    provider_raw = raw.pop("provider", {}) or {}
    codex_raw = provider_raw.pop("codex", {}) or {}
    provider = _parse_nested_dataclass(ProviderConfig, provider_raw)
    provider.codex = _parse_nested_dataclass(CodexProviderConfig, codex_raw)

    # parse limit_warnings section
    limit_warnings_raw = raw.pop("limit_warnings", {}) or {}
    limit_warnings = _parse_nested_dataclass(LimitWarningsConfig, limit_warnings_raw)

    config = Config(
        **{
            key: raw[key]
            for key in Config.__dataclass_fields__
            if key in raw and key not in ("output", "sttts", "provider", "limit_warnings")
        }
    )
    config.provider = provider
    config.output = output
    config.sttts = sttts
    config.limit_warnings = limit_warnings

    return config
