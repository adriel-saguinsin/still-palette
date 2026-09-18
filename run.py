"""Entry point: start Still Palette and open it in the browser.

Safe to double-click on Windows as well as run from a terminal.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import traceback
import webbrowser

HOST = "127.0.0.1"
DEFAULT_PORT = 5111
PORT_ATTEMPTS = 20


def port_in_use(host: str, port: int) -> bool:
    """Is something already listening here?

    Worth checking explicitly. The dev server sets SO_REUSEADDR, so on Windows a
    second instance binds the same port quite happily while the *original*
    process carries on answering requests. Restarting then appears to do
    nothing -- and the older instance serves its cached templates against the
    newer static files, which breaks the page in confusing ways.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


def find_port(host: str, first: int) -> int | None:
    for port in range(first, first + PORT_ATTEMPTS):
        if not port_in_use(host, port):
            return port
    return None


def pause_if_console() -> None:
    """Stop a double-clicked window vanishing before the message is read."""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\n  Press Enter to close this window...")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Still Palette")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open a browser window"
    )
    args = parser.parse_args()

    if args.port is not None:
        # An explicit port is a request, not a suggestion: say so rather than
        # quietly starting somewhere the user is not looking.
        if port_in_use(HOST, args.port):
            print(
                f"\n  Port {args.port} is already in use -- most likely an older\n"
                f"  Still Palette still running. Stop it, or pick another port.\n",
                file=sys.stderr,
            )
            return 1
        port = args.port
    else:
        # No port asked for, so move aside rather than refusing to start. A
        # stale instance holding the default must not stop this one launching,
        # least of all on a double-click where the error scrolls past unseen.
        port = find_port(HOST, DEFAULT_PORT)
        if port is None:
            print(
                f"\n  Could not find a free port between {DEFAULT_PORT} and\n"
                f"  {DEFAULT_PORT + PORT_ATTEMPTS - 1}. Close some programs and retry.\n",
                file=sys.stderr,
            )
            return 1

    from stillpalette.server import create_app

    url = f"http://{HOST}:{port}/"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    note = "" if port == DEFAULT_PORT else f"  (port {DEFAULT_PORT} was busy)\n"
    print(f"\n  Still Palette  ->  {url}\n{note}  Ctrl+C to stop.\n")

    # Bound to loopback only: this is a local tool, not a public server.
    create_app().run(host=HOST, port=port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 0
    except Exception:
        traceback.print_exc()
        print("\n  Still Palette could not start.", file=sys.stderr)
        code = 1
    if code:
        pause_if_console()
    sys.exit(code)
