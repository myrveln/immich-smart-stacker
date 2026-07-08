#!/usr/bin/env python3
"""Compatibility entrypoint for Immich Smart Stacker."""

import sys
import time
import shutil
import subprocess
import logging

import imagehash
import requests

from immich_smart_stacker import Asset, ImmichClient, SmartStacker, logger, unstack_all
from immich_smart_stacker.cli import main as package_main


def main():
    """Delegate execution to package CLI while allowing test monkeypatching."""
    return package_main(
        immich_client_cls=ImmichClient,
        stacker_cls=SmartStacker,
        unstack_fn=unstack_all,
        logger_override=logger,
    )


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
