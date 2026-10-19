#!/usr/bin/env python3
"""Start the AC43 simulator.

    python3 run.py                 # uses config.toml, real I2C if available
    python3 run.py --mock          # no hardware; all boards simulated (incl. disabled ones)
    python3 run.py -c other.toml --port 9000
"""

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from ac43sim import config as config_mod
from ac43sim.controller import Controller
from ac43sim.server import serve


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="AC43 door controller simulator")
    ap.add_argument("-c", "--config", default=str(here / "config.toml"))
    ap.add_argument("--mock", action="store_true", help="simulate every board (no I2C)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = config_mod.load(args.config)
    if args.mock:
        cfg["hardware"]["mode"] = "mock"
        for b in cfg["boards"]:
            b["enabled"] = True  # demo the lock-confirmation UI too
    host = args.host or cfg["server"]["host"]
    port = args.port or cfg["server"]["port"]

    ctl = Controller(cfg)
    ctl.start()
    httpd = serve(ctl, host, port)

    def stop(*_):
        # shutdown() blocks until serve_forever returns, so call it off-thread
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.serve_forever()
    finally:
        logging.info("shutting down - releasing all relays")
        ctl.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
