"""python -m solution.dispatcher [--provider URL] [--port 8200] [--db dispatcher.db] ..."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .app import Dispatcher, Settings, SimTimeFilter
from .config import DEFAULT_FIXTURES


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m solution.dispatcher",
                                description="Durable campaign dispatcher (Assignment B)")
    p.add_argument("--provider", default="http://127.0.0.1:8099", help="provider base URL")
    p.add_argument("--host", default="127.0.0.1", help="webhook listen host")
    p.add_argument("--port", type=int, default=8200, help="webhook listen port")
    p.add_argument("--callback-url", default=None,
                   help="URL the provider should call (default http://HOST:PORT/webhooks/telephony)")
    p.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES, help="campaigns.json")
    p.add_argument("--db", type=Path, default=Path("dispatcher.db"), help="SQLite state file")
    p.add_argument("--out", type=Path, default=Path("final_state.csv"), help="output CSV")
    p.add_argument("--log-level", default="INFO")
    a = p.parse_args(argv)

    # No --reset on purpose: the same command is correct for the first start and for every
    # restart (state is resumed if it belongs to the running provider, else started fresh).
    settings = Settings(provider_url=a.provider, host=a.host, port=a.port, callback_url=a.callback_url,
                        fixtures=a.fixtures, db_path=a.db, out_path=a.out)
    dispatcher = Dispatcher(settings)
    logging.basicConfig(level=a.log_level.upper(), stream=sys.stdout,
                        format="%(asctime)s [sim %(sim)s] %(levelname)-7s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(SimTimeFilter(dispatcher.clock))
    return asyncio.run(dispatcher.run())


if __name__ == "__main__":
    sys.exit(main())
