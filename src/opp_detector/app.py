import argparse
import logging
import signal
import sys
import threading
import webbrowser

from opp_detector.server import DEFAULT_PORT, DetectorController, Server
from opp_detector.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(prog="opp-detector", description="Local web UI for the opp detector")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page in a browser")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        store = Store()
    except RuntimeError as e:
        sys.exit(str(e))
    detector = DetectorController(store)

    try:
        server = Server(store, detector, args.port)
    except OSError as e:
        sys.exit(f"Couldn't listen on 127.0.0.1:{args.port}: {e.strerror or e} (try another --port)")

    # make `kill` shut us down as cleanly as ctrl-c does
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    print(f"opp-detector running at {server.url} (data in {store.dir}); ctrl-c to quit", flush=True)
    if not args.no_browser:
        # the socket is already listening, so the browser can't get there too early
        threading.Thread(target=webbrowser.open, args=[server.url], daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        # the detector process ignores SIGINT: it's on us to stop it
        detector.stop()
        server.server_close()

def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    main()
