#!/usr/bin/env bash
# macOS / Linux desktop: double-click to start the web UI on this computer and open it in the browser.
cd "$(dirname "$0")"
(sleep 4 && { open "http://127.0.0.1:8080/" 2>/dev/null || xdg-open "http://127.0.0.1:8080/" 2>/dev/null; }) &
exec ./run.sh --server --host 127.0.0.1 --port 8080
