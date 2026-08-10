"""Entry point: wire config, tracker and web server, then serve.

    python -m cycletime.main [--config config.json] [--camera 0] [--port 8000]
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from dataclasses import replace
from pathlib import Path

import uvicorn

from . import config as config_mod
from .api import create_app
from .tracker import Tracker


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="cycletime", description="Conveyor cycle-time tracker")
    p.add_argument("--config", type=Path, default=config_mod.DEFAULT_CONFIG_PATH,
                   help="path to config.json")
    p.add_argument("--camera", type=int, default=None, help="override camera index")
    p.add_argument("--host", default=None, help="override bind host")
    p.add_argument("--port", type=int, default=None, help="override port")
    p.add_argument("--log-level", default="info",
                   choices=["debug", "info", "warning", "error"])
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("cycletime")

    cfg = config_mod.load(args.config)
    if args.camera is not None:
        cfg = replace(cfg, camera=replace(cfg.camera, index=args.camera))
    if args.host or args.port:
        cfg = replace(cfg, server=replace(
            cfg.server,
            host=args.host or cfg.server.host,
            port=args.port or cfg.server.port,
        ))

    tracker = Tracker(cfg, config_path=args.config)
    tracker.start()

    app = create_app(tracker)
    server = uvicorn.Server(uvicorn.Config(
        app, host=cfg.server.host, port=cfg.server.port,
        log_level=args.log_level, access_log=False,
    ))

    def shutdown(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        server.should_exit = True

    # systemd sends SIGTERM on stop; Ctrl-C sends SIGINT. Both must release the
    # camera cleanly or the next start finds the device busy.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    log.info("dashboard: http://%s:%d/  |  setup: http://%s:%d/setup",
             cfg.server.host, cfg.server.port, cfg.server.host, cfg.server.port)
    try:
        server.run()
    finally:
        tracker.stop()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
