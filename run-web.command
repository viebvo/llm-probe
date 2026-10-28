#!/usr/bin/env bash
# macOS / Linux desktop: double-click to start the web UI on this computer and open it in the browser.
cd "$(dirname "$0")"
exec ./run.sh --server --host 127.0.0.1 --port 8080 --open-browser
