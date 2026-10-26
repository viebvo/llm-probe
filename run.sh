#!/usr/bin/env bash
# Starts the web UI (or a batch run: --run, --configure, see --help). The first start creates a virtual environment in .venv and
# installs the dependencies into it; they are updated whenever requirements.txt changes. Needs Python >= 3.10.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")"

if [ ! -x .venv/bin/python ]; then
    PY=""
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    done
    if [ -z "$PY" ]; then
        echo "LLM Probe needs Python 3.10 or newer, but none was found." >&2
        echo "Install it (Debian/Ubuntu: sudo apt install python3 python3-venv; macOS: https://www.python.org/downloads/)" >&2
        echo "and start this script again." >&2
        exit 1
    fi
    echo "First start: creating the Python environment in .venv ..."
    if ! "$PY" -m venv .venv; then
        rm -rf .venv
        echo "Could not create the environment. On Debian/Ubuntu install the venv module first:" >&2
        echo "    sudo apt install python3-venv" >&2
        exit 1
    fi
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
    echo "Installing the dependencies (needs internet access, takes a minute) ..."
    if ! .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt; then
        echo "Installing the dependencies failed - check the internet connection and start this script again." >&2
        exit 1
    fi
    touch .venv/.installed
fi
exec .venv/bin/python -m llm_probe "$@"
