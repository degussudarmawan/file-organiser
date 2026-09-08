#!/usr/bin/env python3
"""Start the local web UI.

This is a personal tool that moves files on your own machine, so it binds to
127.0.0.1 by default and there is no auth. Don't put it on 0.0.0.0 unless you
have thought hard about who else is on the network.
"""

import argparse
import sys

from organiser.paths import state_dir
from organiser.server import create_app
from organiser.undo_log import undo_log_path

# macOS hands port 5000 to AirPlay Receiver, so default one above it.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5001


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run.py", description="Run the local file-organising agent UI."
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"interface to bind (default {DEFAULT_HOST}; local-only by design)",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--debug", action="store_true", help="Flask debug mode with auto-reload"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    app = create_app()
    manager = app.extensions["organiser_manager"]
    available, detail = manager.delegate_status()

    classifier_line = (
        f"classifier: available ({detail})"
        if available
        else f"classifier: UNAVAILABLE — rules-only ({detail})"
    )

    if args.host != DEFAULT_HOST:
        print(f"!  binding to {args.host} — this tool has no auth and moves files.")

    print("File-organiser agent")
    print(f"   UI:        http://{args.host}:{args.port}")
    print(f"   {classifier_line}")
    print(f"   undo log:  {undo_log_path()}")
    print(f"   state dir: {state_dir()}")
    print("   Ctrl-C to stop.\n")

    try:
        app.run(host=args.host, port=args.port, debug=args.debug)
    except KeyboardInterrupt:
        print("\nStopped.")
    except OSError as exc:
        # Nearly always "address already in use" — say which port.
        print(f"could not bind {args.host}:{args.port} — {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
