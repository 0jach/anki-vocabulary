"""Fast clipboard capture. This module never imports the worker, LLM, or Anki client."""

import argparse
import logging
import os
import re
import sqlite3
import subprocess
import sys
import unicodedata
from pathlib import Path

from config import load_config
from database import Database
from log_utils import setup_logging

logger = logging.getLogger(__name__)
WORD = r"[A-Za-z]+(?:['-][A-Za-z]+)*"
SELECTION = re.compile(rf"{WORD}(?: {WORD}){{0,7}}\Z")


def normalize_selection(value: str) -> tuple[str, str]:
    if len(value) > 512 or any(
        unicodedata.category(c) == "Cc" and c not in "\t\n\r" for c in value
    ):
        raise ValueError("Select an English word or a phrase of up to eight words")
    value = unicodedata.normalize("NFKC", value).translate(
        str.maketrans(
            {
                "’": "'",
                "‘": "'",
                "‐": "-",
                "‑": "-",
                "–": "-",
            }
        )
    )
    word = " ".join(value.split()).strip('"“”.,!?;:()[]{}')
    word = word.strip()
    if not word or len(word) > 120 or not SELECTION.fullmatch(word):
        raise ValueError("Select an English word or a phrase of up to eight words")
    return word, word.casefold()


def read_selection() -> str:
    for command in (
        ["wl-paste", "--primary", "--no-newline"],
        ["wl-paste", "--no-newline"],
    ):
        try:
            result = subprocess.run(command, capture_output=True, timeout=1, check=False)
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0 and result.stdout.strip():
            if len(result.stdout) > 2048:
                raise ValueError("Selection is too long")
            return result.stdout.decode("utf-8", errors="strict")
    raise ValueError("No selection or clipboard text available")


def notify(message: str, *, error: bool = False) -> None:
    try:
        subprocess.run(
            [
                "notify-send",
                "--app-name=Anki Vocabulary",
                "--expire-time=2500",
                "--urgency=" + ("critical" if error else "normal"),
                "--",
                "Anki Vocabulary",
                message,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("notification_unavailable")


def launch_worker(config_path: str | None) -> None:
    """Hand off without waiting for Anki, the worker lock, or local inference."""
    command = [sys.executable, str(Path(__file__).with_name("worker.py")), "--if-anki-open"]
    if config_path:
        command.extend(["--config", str(Path(config_path).expanduser().resolve())])
    log_dir = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    log_dir = log_dir / "anki-vocabulary"
    try:
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(log_dir / "worker.log", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as log:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    except OSError as exc:
        # Capture already committed. Startup processing remains available.
        logger.warning("worker_launch_failed", extra={"details": {"error": str(exc)}})


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture vocabulary and process it when Anki is available"
    )
    parser.add_argument("--config")
    parser.add_argument("--text", help="Explicit text instead of reading the Wayland clipboard")
    parser.add_argument("--context", help="Optional sentence for disambiguating meaning")
    args = parser.parse_args()
    setup_logging()
    try:
        config = load_config(args.config)
        word, normalized = normalize_selection(
            args.text if args.text is not None else read_selection()
        )
        if args.context and len(args.context) > 2000:
            raise ValueError("Context must be at most 2000 characters")
        with Database(config.db_path) as db:
            inserted = db.enqueue(word, normalized, args.context)
        if inserted and config.process_when_anki_open:
            launch_worker(args.config)
        notify(f"Queued: {word}" if inserted else f"Already captured: {word}")
        logger.info("capture_saved" if inserted else "capture_duplicate")
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        logger.error("capture_failed", extra={"details": {"error": str(exc)}})
        notify(f"Capture failed: {exc}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
