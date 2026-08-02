"""Serve the dashboard locally.

    python scripts/serve_dashboard.py        # http://127.0.0.1:4280

With no apiBase configured in dashboard/config.js the page reads the sample
documents in dashboard/data, so this needs neither Azure nor a running Function
App. Set PORT to override the port.
"""

from __future__ import annotations

import functools
import http.server
import os
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"


def main() -> None:
    port = int(os.environ.get("PORT", "4280"))
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(DASHBOARD_DIR)
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Dashboard on http://127.0.0.1:{port} (serving {DASHBOARD_DIR})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
