"""Command line entry point.

stockhold init-db                  create tables
stockhold expire                   expire stale reservations once
stockhold expire --every 30        keep sweeping every 30 seconds
stockhold serve --port 8000        run the HTTP API with uvicorn
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Sequence
from typing import Optional

from .config import Settings
from .db import Base, make_engine, make_session_factory, write_transaction
from .services import expire_stale

log = logging.getLogger("stockhold")


def run_expiry_once(settings: Settings, batch_size: int = 500) -> int:
    """Sweep until no stale reservations remain. Returns the total expired."""
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    total = 0
    try:
        while True:
            with write_transaction(factory) as session:
                count = expire_stale(session, batch_size=batch_size)
            total += count
            if count < batch_size:
                return total
    finally:
        engine.dispose()


def _cmd_init_db(settings: Settings, _args: argparse.Namespace) -> int:
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    print("tables created")
    return 0


def _cmd_expire(settings: Settings, args: argparse.Namespace) -> int:
    if not args.every:
        print(f"expired {run_expiry_once(settings, args.batch_size)} reservation(s)")
        return 0
    log.info("sweeping every %ss", args.every)
    try:
        while True:
            count = run_expiry_once(settings, args.batch_size)
            if count:
                log.info("expired %d reservation(s)", count)
            time.sleep(args.every)
    except KeyboardInterrupt:
        return 0


def _cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(settings), host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stockhold", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create database tables").set_defaults(func=_cmd_init_db)

    expire = sub.add_parser("expire", help="expire stale reservations")
    expire.add_argument("--every", type=float, default=0, help="repeat every N seconds")
    expire.add_argument("--batch-size", type=int, default=500)
    expire.set_defaults(func=_cmd_expire)

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(Settings.from_env(), args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
