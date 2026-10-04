"""FlowDesk - the beginner-friendly desktop app for the options-flow research system.

    pythonw flow_app.py            opens the app in its own window (desktop shortcut runs this)
    py flow_app.py --browser       only serves it, at http://127.0.0.1:8060 (for testing)

A tiny local server (this PC only) answers the UI's data requests from app_data.py, app_market.py
and app_research.py, which read flow.db read-only. The app never talks to IBKR or Trade Echo and
never writes anything, so it can run or crash without touching the logger, the IBKR tracker or the
Discord pings. (The tracker asks /api/focus which ticker is on screen, to stream its order book.)
"""

import json
import os
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import app_data
import app_market
import app_research

HOST, PORT = "127.0.0.1", int(os.environ.get("FLOWDESK_PORT", "8060"))
UI = Path(__file__).parent / "ui"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI), **kwargs)

    def do_GET(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return super().do_GET()
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            name = url.path[5:]
            if name == "status":
                data = app_data.status()
            elif name == "market":
                data = app_market.board()
            elif name == "ticker":
                data = app_market.ticker(q.get("t"))
            elif name == "focus":
                data = app_market.focus()
            elif name == "pings":
                data = app_data.pings(q.get("scope", "open"))
            elif name == "detail":
                data = app_data.detail(int(q["id"]))
            elif name == "learning":
                data = app_research.learning()
            elif name == "lab":
                data = app_research.lab()
            elif name == "simulate":
                num = lambda k: float(q[k]) if q.get(k) not in (None, "", "off") else None
                data = app_research.simulate(q.get("group", "all"), q.get("side", "any"), q.get("entry", "whale"),
                                             num("target"), num("stop"))
            elif name == "grid":
                data = app_research.exit_grid(q.get("group", "all"), q.get("side", "any"), q.get("entry", "whale"))
            elif name == "contest":
                data = app_research.contest(q.get("universe", "core"))
            elif name == "predictions":
                data = app_research.predictions(q.get("scope", "mine"))
            else:
                return self.send_error(404)
            body, code = json.dumps(data, default=str).encode("utf-8"), 200
        except Exception as e:  # show the problem in the app instead of a blank screen
            body, code = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode("utf-8"), 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, *args):
        pass


def serve():
    """Start the local server; returns False if one is already running (app already open)."""
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        return False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return True


def _warm_up():
    """Load the simulator's price paths and pandas in the background so the Learning and
    Predictions tabs open instantly."""
    try:
        app_research._sim_data()
        import pandas  # noqa: F401
    except Exception:
        pass


def main():
    serve()
    threading.Thread(target=_warm_up, daemon=True).start()
    url = f"http://{HOST}:{PORT}/"
    if "--browser" in sys.argv:
        print(f"FlowDesk at {url} (Ctrl+C to stop)")
        threading.Event().wait()
    import webview
    webview.create_window("FlowDesk", url, width=1380, height=900, min_size=(960, 640),
                          background_color="#0d1117")
    webview.start()


if __name__ == "__main__":
    main()
