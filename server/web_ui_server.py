#!/usr/bin/env python3
"""Serve operator and robot UIs on separate ports."""
import argparse
import logging
import signal
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Optional, Tuple


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def build_server(root: Path, host: str, port: int):
    if not root.is_dir():
        raise FileNotFoundError(f"directory not found: {root}")
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadedHTTPServer((host, port), handler)
    return server


def parse_args(argv: Optional[list[str]] = None) -> Tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description="Serve static A-Frame UIs.")
    parser.add_argument("--ui", type=int, default=8000, help="port for operator UI")
    parser.add_argument("--robot", type=int, default=8001, help="port for robot viewer UI")
    parser.add_argument("--host", default="0.0.0.0", help="bind address")
    parser.add_argument("--ui-root", default="../ui", help="path to operator UI directory")
    parser.add_argument("--robot-root", default="../robot_ui", help="path to robot UI directory")
    return parser.parse_known_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    args, unknown = parse_args(argv)
    if unknown:
        logging.warning("ignored cli args: %s", unknown)

    host = args.host
    script_dir = Path(__file__).parent.resolve()
    ui_root = (script_dir / args.ui_root).resolve()
    robot_root = (script_dir / args.robot_root).resolve()

    servers = []
    threads = []

    try:
        ui_server = build_server(ui_root, host, args.ui)
        servers.append(ui_server)
        threads.append(threading.Thread(target=ui_server.serve_forever, daemon=True))
        logging.info("operator UI: http://%s:%d/ -> %s", host, args.ui, ui_root)
    except FileNotFoundError as exc:
        logging.error("operator UI directory missing: %s", exc)
        return 1

    try:
        robot_server = build_server(robot_root, host, args.robot)
        servers.append(robot_server)
        threads.append(threading.Thread(target=robot_server.serve_forever, daemon=True))
        logging.info("robot viewer UI: http://%s:%d/ -> %s", host, args.robot, robot_root)
    except FileNotFoundError as exc:
        logging.error("robot UI directory missing: %s", exc)
        return 1

    stop_event = threading.Event()

    def handle_signal(_sig, _frame):  # noqa: ANN001
        logging.info("signal received, shutting down")
        stop_event.set()
        for srv in servers:
            srv.shutdown()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    for thread in threads:
        thread.start()

    logging.info("servers running; press Ctrl+C to stop")
    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    finally:
        for srv in servers:
            srv.shutdown()
        for srv in servers:
            srv.server_close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
