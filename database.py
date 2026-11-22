"""SQLite queue. Short transactions keep capture independent of inference."""

import argparse
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from config import load_config


@dataclass(frozen=True)
class Job:
    id: int
    word: str
    normalized_word: str
    context: str | None
    attempt_count: int


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create privately before sqlite opens it (including when called outside main).
        path.touch(mode=0o600, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=2, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.migrate()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def migrate(self) -> None:
        with self.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)"
            )
            for migration in sorted(files("migrations").iterdir(), key=lambda p: p.name):
                if not migration.name.endswith(".sql"):
                    continue
                if connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE name=?", (migration.name,)
                ).fetchone():
                    continue
                # Current migrations contain plain DDL, no triggers/embedded semicolons.
                for statement in migration.read_text().split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations VALUES (?)", (migration.name,))

    def enqueue(self, word: str, normalized: str, context: str | None = None) -> bool:
        cursor = self.connection.execute(
            "INSERT INTO vocabulary_queue(word, normalized_word, context) VALUES (?, ?, ?) "
            "ON CONFLICT(normalized_word) DO NOTHING",
            (word, normalized, context),
        )
        return cursor.rowcount == 1

    def recover(self) -> int:
        """Only call while holding the worker lock: all processing rows are then orphaned."""
        return self.connection.execute(
            "UPDATE vocabulary_queue SET status='failed', last_error='Worker interrupted', "
            "attempt_count=max(attempt_count-1, 0), "
            "processed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE status='processing'"
        ).rowcount

    def eligible(self, max_attempts: int) -> list[int]:
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT id FROM vocabulary_queue WHERE status IN ('pending', 'failed') "
                "AND attempt_count < ? ORDER BY id",
                (max_attempts,),
            )
        ]

    def claim(self, ids: list[int], max_attempts: int) -> list[Job]:
        jobs = []
        with self.transaction() as connection:
            for job_id in ids:
                row = connection.execute(
                    "UPDATE vocabulary_queue SET status='processing', "
                    "attempt_count=attempt_count+1, last_error=NULL, processed_at=NULL "
                    "WHERE id=? AND status IN ('pending','failed') AND attempt_count < ? "
                    "RETURNING id, word, normalized_word, context, attempt_count",
                    (job_id, max_attempts),
                ).fetchone()
                if row:
                    jobs.append(Job(**dict(row)))
        return jobs

    def imported(self, job_id: int, note_id: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE vocabulary_queue SET status='imported', anki_note_id=?, last_error=NULL, "
                "processed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?",
                (note_id, job_id),
            )
            connection.execute("UPDATE queue_state SET value='1' WHERE key='sync_pending'")

    def failed(self, job_id: int, error: str) -> None:
        self.connection.execute(
            "UPDATE vocabulary_queue SET status='failed', last_error=?, "
            "processed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE id=? AND status='processing'",
            (error[:2000], job_id),
        )

    def sync_pending(self) -> bool:
        return (
            self.connection.execute(
                "SELECT value FROM queue_state WHERE key='sync_pending'"
            ).fetchone()[0]
            == "1"
        )

    def synced(self) -> None:
        self.connection.execute("UPDATE queue_state SET value='0' WHERE key='sync_pending'")

    def inspect(self, status: str | None = None) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM vocabulary_queue WHERE (? IS NULL OR status=?) ORDER BY id",
                (status, status),
            )
        ]

    def retry(self, job_id: int | None = None) -> int:
        return self.connection.execute(
            "UPDATE vocabulary_queue SET status='pending', attempt_count=0, last_error=NULL, "
            "processed_at=NULL WHERE status='failed' AND (? IS NULL OR id=?)",
            (job_id, job_id),
        ).rowcount


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize, inspect, or retry the SQLite queue")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    listing = sub.add_parser("list")
    listing.add_argument("--status", choices=["pending", "processing", "imported", "failed"])
    retry = sub.add_parser("retry")
    retry.add_argument("--id", type=int, help="Omit to reset all failed jobs")
    args = parser.parse_args()
    try:
        with Database(load_config(args.config).db_path) as db:
            if args.command == "list":
                print(json.dumps(db.inspect(args.status), indent=2, ensure_ascii=False))
            elif args.command == "retry":
                print(f"Queued {db.retry(args.id)} failed job(s)")
            else:
                print(f"Queue initialized: {db.path}")
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Queue error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
