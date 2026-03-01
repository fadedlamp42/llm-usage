"""load and validate config.yaml for brightness-monitor.

looks for config.yaml in the project root (next to pyproject.toml).
missing keys fall back to built-in defaults.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

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
    enabled: bool = True
    relay_url: str = "http://127.0.0.1:8393"


@dataclass
class Config:
    window: str = "five_hour"
    poll_interval: int = 60
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
        log.info("no config.yaml found, using defaults")
        return Config()

    with open(config_path) as handle:
        raw = yaml.safe_load(handle) or {}

    log.debug("loaded config from %(path)s", {"path": config_path})

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

    config = Config(
        **{
            key: raw[key]
            for key in Config.__dataclass_fields__
            if key in raw and key not in ("output", "sttts")
        }
    )
    config.output = output
    config.sttts = sttts

    return config
