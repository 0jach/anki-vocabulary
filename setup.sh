#!/usr/bin/env bash
# Bootstrap as the desktop user; sudo is used only for package/service operations.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if (( EUID == 0 )); then
    echo "Run this as your desktop user, without sudo. Setup asks for sudo when needed." >&2
    exit 1
fi
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: ./setup.sh [--config PATH] [--anki-data-dir PATH] [--model NAME]"
    echo "Installs missing Arch packages, sets up Python/add-ons, selects a deck, and downloads the model."
    exit 0
fi
missing=()
for pair in python:python wl-paste:wl-clipboard notify-send:libnotify ollama:ollama anki:anki; do
    if ! command -v "${pair%%:*}" >/dev/null 2>&1; then
        missing+=("${pair#*:}")
    fi
done
if ! command -v uv >/dev/null 2>&1 && ! python -m pip --version >/dev/null 2>&1; then
    missing+=(python-pip)
fi
if (( ${#missing[@]} )); then
    command -v pacman >/dev/null 2>&1 || { echo "This installer requires Arch Linux (pacman)." >&2; exit 1; }
    echo "Missing packages: ${missing[*]}"
    read -r -p "Install them with sudo pacman -Syu --needed? [Y/n] " answer
    [[ "$answer" =~ ^([yY]([eE][sS])?)?$ ]] || exit 1
    sudo pacman -Syu --needed "${missing[@]}"
fi
python -c 'import sys; sys.exit("Python 3.12+ is required" if sys.version_info < (3, 12) else 0)'
if command -v uv >/dev/null 2>&1; then
    if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
        uv venv --python python "$project_dir/.venv"
    fi
    uv pip install --python "$project_dir/.venv/bin/python" -e "$project_dir"
else
    python -m venv "$project_dir/.venv"
    "$project_dir/.venv/bin/python" -m pip install -e "$project_dir"
fi
exec "$project_dir/.venv/bin/python" "$project_dir/setup_app.py" "$@"
