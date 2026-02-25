"""load and validate config.yaml for brightness-monitor.

looks for config.yaml in the project root (next to pyproject.toml).
missing keys fall back to built-in defaults.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
class Config:
    window: str = "five_hour"
    poll_interval: int = 60
    min_brightness: float = 0.0
    fade_speed: int = 0
    pulse_threshold: float = 10.0
    pulse_period: float = 3.0
    readout: ReadoutConfig = field(default_factory=ReadoutConfig)


def load_config(path: Optional[Path] = None) -> Config:
    """load config from yaml file, falling back to defaults for missing keys."""
    config_path = path or DEFAULT_CONFIG_PATH

    if not config_path.exists():
        log.info("no config.yaml found, using defaults")
        return Config()

    with open(config_path) as handle:
        raw = yaml.safe_load(handle) or {}

    log.debug("loaded config from %(path)s", {"path": config_path})

    readout_raw = raw.pop("readout", {}) or {}
    readout = ReadoutConfig(
        **{
            key: readout_raw[key]
            for key in ReadoutConfig.__dataclass_fields__
            if key in readout_raw
        }
    )

    config = Config(
        **{
            key: raw[key]
            for key in Config.__dataclass_fields__
            if key in raw and key != "readout"
        }
    )
    config.readout = readout

    return config
