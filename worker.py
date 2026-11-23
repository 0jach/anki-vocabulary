"""Finite queue processing on startup or capture; no daemon or polling service."""

import argparse
import fcntl
import logging
import os
import signal
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager

from anki_client import AnkiClient
from config import Config, load_config
from database import Database, Job
from generator import OllamaSession, generate_cards
from grounding import WordNetGrounding
from http_client import ServiceError
from log_utils import setup_logging

logger = logging.getLogger(__name__)


class WorkerBusy(RuntimeError):
    pass


@contextmanager
def worker_lock(config: Config, *, wait: bool = False) -> Iterator[None]:
    config.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Never unlink the lock: replacing its inode would let another worker bypass flock.
    fd = os.open(str(config.db_path) + ".worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise WorkerBusy("Another worker owns this queue") from exc
        yield
    finally:
        os.close(fd)


def process_batch(
    config: Config,
    db: Database,
    anki: AnkiClient,
    jobs: list[Job],
    session: Callable[[], OllamaSession],
) -> bool:
    """Reconcile before generation, avoiding inference after ambiguous successful imports."""
    remaining: list[Job] = []
    failed = False
    for job in jobs:
        try:
            note_id = anki.find_existing(job.normalized_word)
            if note_id is not None:
                db.imported(job.id, note_id)
                logger.info("note_reconciled", extra={"details": {"job_id": job.id}})
            else:
                remaining.append(job)
        except Exception as exc:
            db.failed(job.id, str(exc))
            failed = True
            logger.error("reconciliation_failed", extra={"details": {"job_id": job.id}})
    if not remaining:
        return failed
    try:
        # Enter lazily: no Ollama process when every job already exists in Anki.
        ollama = session()
        cards = generate_cards(ollama, remaining, WordNetGrounding() if config.wordnet else None)
    except Exception as exc:
        for job in remaining:
            db.failed(job.id, str(exc))
        logger.error("generation_failed", extra={"details": {"error": str(exc)}})
        return True
    for job, card in zip(remaining, cards, strict=True):
        try:
            note_id = anki.import_card(card, job.normalized_word)
            db.imported(job.id, note_id)
            logger.info("note_imported", extra={"details": {"job_id": job.id, "note_id": note_id}})
        except Exception as exc:
            # Preserve the stable Anki tag even if this database write also fails.
            db.failed(job.id, str(exc))
            failed = True
            logger.error("import_failed", extra={"details": {"job_id": job.id, "error": str(exc)}})
    return failed


def run(config: Config, *, if_anki_open: bool = False) -> int:
    # Capture-triggered processes wait behind the active batch, then take their own
    # snapshot. A capture arriving during processing cannot miss the worker's exit.
    with worker_lock(config, wait=if_anki_open), Database(config.db_path) as db:
        recovered = db.recover()
        if recovered:
            logger.warning("interrupted_jobs_recovered", extra={"details": {"count": recovered}})
        ids = db.eligible(config.max_attempts)
        if not ids and not (config.anki_sync and db.sync_pending()):
            logger.info("queue_empty")
            return 0
        anki = AnkiClient(
            config.model_copy(update={"anki_wait_timeout": 0.0}) if if_anki_open else config
        )
        if if_anki_open:
            try:
                anki.call("version", _timeout=0.5)
            except ServiceError:
                logger.info("anki_unavailable_queue_preserved")
                return 0
        # A failed readiness check consumes no attempts and never starts Ollama.
        anki.prepare()
        failed = False
        with ExitStack() as stack:
            current_session: OllamaSession | None = None

            def get_session() -> OllamaSession:
                nonlocal current_session
                if current_session is None:
                    current_session = stack.enter_context(OllamaSession(config))
                return current_session

            try:
                for offset in range(0, len(ids), config.batch_size):
                    jobs = db.claim(ids[offset : offset + config.batch_size], config.max_attempts)
                    if jobs:
                        failed = process_batch(config, db, anki, jobs, get_session) or failed
            finally:
                # Signals, exceptions, and DB failures must not strand processing rows.
                # If SQLite itself is unavailable, the next locked run recovers them.
                db.recover()
        if config.anki_sync and db.sync_pending():
            anki.sync()
            db.synced()
            logger.info("anki_sync_requested")
        return 1 if failed else 0


def interrupted(signum: int, _frame: object) -> None:
    # BaseException bypasses per-job Exception handlers and unwinds owned subprocesses.
    raise KeyboardInterrupt(f"Worker interrupted by signal {signum}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate and import the current vocabulary queue")
    parser.add_argument("--config")
    parser.add_argument(
        "--if-anki-open",
        action="store_true",
        help="Exit quietly if AnkiConnect is unavailable; wait behind an existing worker",
    )
    args = parser.parse_args()
    os.umask(0o077)
    setup_logging()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        return run(load_config(args.config), if_anki_open=args.if_anki_open)
    except WorkerBusy:
        logger.info("worker_already_running")
        return 0
    except KeyboardInterrupt:
        logger.warning("worker_interrupted")
        return 130
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        logger.error("worker_failed", extra={"details": {"error": str(exc)}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
