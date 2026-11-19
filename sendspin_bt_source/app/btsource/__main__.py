"""Entry point: python -m btsource --options /data/options.json --data-dir /data"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from . import __version__
from .config import Options
from .ha import Notifier
from .service import Service


def main() -> int:
    parser = argparse.ArgumentParser(prog="btsource")
    parser.add_argument("--options", default="/data/options.json")
    parser.add_argument("--data-dir", default="/data")
    args = parser.parse_args()

    try:
        options = Options.load(args.options)
    except ValueError as err:
        print(f"Konfigurationsfehler: {err}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, options.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    if options.log_level != "debug":
        for noisy in ("aiosendspin", "zeroconf", "aiohttp"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger(__name__).info("Sendspin Bluetooth Source %s", __version__)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    async def runner() -> None:
        service = Service(options, data_dir, notifier=Notifier(options.device_name))
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await service.run()

    try:
        asyncio.run(runner())
    except RuntimeError as err:
        logging.getLogger(__name__).error("%s", err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
