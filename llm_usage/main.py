"""CLI entrypoint for llm-usage.

parses arguments, configures logging, loads config, and hands off
to the daemon loop. this is the only module that touches argparse.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from prism.logging import configure_logging, get_logger

from llm_usage.config import load_config
from llm_usage.daemon import run_daemon

logger = get_logger()


def main():
    parser = argparse.ArgumentParser(
        description="track LLM CLI usage windows with keyboard + voice feedback",
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
        help="provider token override (supports claude oauth or codex bearer token)",
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
        provider=config.provider.name,
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
