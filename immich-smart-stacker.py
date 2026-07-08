#!/usr/bin/env python3
"""Compatibility entrypoint for Immich Smart Stacker."""

import logging
import shutil
import subprocess
import sys
import time

import imagehash
import requests

from immich_smart_stacker import Asset, ImmichClient, SmartStacker, logger, unstack_all
from immich_smart_stacker.cli import (
    _format_datetime_utc,
    _load_state_json,
    _load_watermark,
    _parse_datetime_arg,
    _save_state_json,
    _save_watermark,
    main as package_main,
)


def main():
    """Delegate execution to package CLI while allowing test monkeypatching."""
    return package_main(
        immich_client_cls=ImmichClient,
        stacker_cls=SmartStacker,
        unstack_fn=unstack_all,
        logger_override=logger,
    )


if __name__ == '__main__':
    sys.exit(main())
