"""CLI entrypoint for brightness-monitor.

parses arguments, configures logging, loads config, and hands off
to the daemon loop. this is the only module that touches argparse.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from prism.logging import configure_logging, get_logger

from brightness_monitor.config import load_config
from brightness_monitor.daemon import run_daemon

logger = get_logger()


def main():
    parser = argparse.ArgumentParser(
        description="sync MacBook keyboard brightness to Claude API usage",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="path to config.yaml (default: config.yaml in project root)",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="OAuth token (overrides env var and Keychain lookup)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log what would happen without touching brightness",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="enable debug logging",
    )

    args = parser.parse_args()

    configure_logging(level=logging.DEBUG if args.verbose else logging.INFO)

    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)

    kb = config.output.keyboard
    logger.info(
        "config loaded",
        window=config.window,
        poll_interval=config.poll_interval,
        speech=config.output.speech,
        keyboard=kb.enabled,
    )
    if kb.enabled:
        logger.info(
            "keyboard config",
            fade=kb.fade_speed,
            pulse_threshold=kb.pulse_threshold,
            readout_every=kb.readout.every_percent,
            readout_threshold=kb.readout.threshold,
        )

    run_daemon(
        config=config,
        dry_run=args.dry_run,
        token_override=args.token,
    )


if __name__ == "__main__":
    main()
