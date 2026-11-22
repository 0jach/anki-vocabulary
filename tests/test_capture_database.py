import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

import capture
from capture import normalize_selection, read_selection
from database import Database


@pytest.mark.parametrize(
    "text,normalized",
    [
        (" Recondite\n", "recondite"),
        ("take\tinto account", "take into account"),
        ("well‑known", "well-known"),
        ("don’t", "don't"),
        ("“Obscure,”", "obscure"),
        ("ice cream", "ice cream"),
        ("Mother-in-law", "mother-in-law"),
    ],
)
def test_valid_capture(text, normalized):
    assert normalize_selection(text)[1] == normalized


@pytest.mark.parametrize(
    "text",
    [
        "",
        "  ",
        "123",
        "hello@example.com",
        "https://example.com",
        "word; rm -rf /",
        "un\x00safe",
        "日本語",
        "one two three four five six seven eight nine",
        "a" * 121,
        "x" * 513,
        "hello🙂",
        "two+two",
    ],
)
def test_invalid_capture(text):
    with pytest.raises(ValueError):
        normalize_selection(text)


def test_primary_clipboard():
    run = Mock(return_value=subprocess.CompletedProcess([], 0, b"word", b""))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(capture.subprocess, "run", run)
        assert read_selection() == "word"
    assert run.call_args.args[0] == ["wl-paste", "--primary", "--no-newline"]


def test_clipboard_fallback(monkeypatch):
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 1, b"", b""),
            subprocess.CompletedProcess([], 0, b"phrase", b""),
        ]
    )
    monkeypatch.setattr(capture.subprocess, "run", run)
    assert read_selection() == "phrase"
    assert run.call_count == 2
    assert run.call_args.args[0] == ["wl-paste", "--no-newline"]


def test_empty_clipboards(monkeypatch):
    monkeypatch.setattr(
        capture.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
    )
    with pytest.raises(ValueError, match="No selection"):
        read_selection()


def test_capture_db_failure(config, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["capture.py", "--text", "word"])
    monkeypatch.setattr(capture, "load_config", lambda _: config)
    monkeypatch.setattr(
        capture, "Database", Mock(side_effect=sqlite3.OperationalError("disk full"))
    )
    notification = Mock()
    monkeypatch.setattr(capture, "notify", notification)
    assert capture.main() == 1
    assert notification.call_args.kwargs["error"] is True


def test_capture_command(config, db, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["capture.py", "--text", "Recondite", "--context", "A theory"])
    monkeypatch.setattr(capture, "load_config", lambda _: config)
    notification = Mock()
    monkeypatch.setattr(capture, "notify", notification)
    assert capture.main() == 0
    assert capture.main() == 0
    assert len(db.inspect()) == 1
    assert db.inspect()[0]["context"] == "A theory"
    assert "Already captured" in notification.call_args.args[0]


def test_migration_idempotent_and_parameters(db):
    db.migrate()
    assert db.enqueue("don't", "don't")
    assert not db.enqueue("Don't", "don't")
    assert len(db.inspect()) == 1
    assert db.inspect()[0]["created_at"].endswith("Z")
    assert db.connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1


def test_concurrent_capture(config, db):
    def insert(_):
        with Database(config.db_path) as connection:
            return connection.enqueue("word", "word")

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sum(executor.map(insert, range(8))) == 1


def test_retry_and_recovery(db):
    db.enqueue("word", "word")
    job = db.claim(db.eligible(3), 3)[0]
    assert job.attempt_count == 1
    assert not db.claim([job.id], 3)
    assert db.recover() == 1
    assert db.inspect()[0]["last_error"] == "Worker interrupted"
    for _ in range(3):
        job = db.claim(db.eligible(3), 3)[0]
        db.failed(job.id, "offline")
    assert db.eligible(3) == []
    assert db.retry(job.id) == 1
    assert db.inspect()[0]["attempt_count"] == 0
    assert db.eligible(3) == [job.id]


def test_import_and_sync_state(db):
    db.enqueue("word", "word")
    job = db.claim(db.eligible(3), 3)[0]
    db.imported(job.id, 123)
    row = db.inspect()[0]
    assert row["status"] == "imported"
    assert row["anki_note_id"] == 123
    assert row["processed_at"]
    assert db.sync_pending()
    db.synced()
    assert not db.sync_pending()
    assert not db.enqueue("WORD", "word")
    assert db.retry() == 0


def test_transaction_rolls_back(db):
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute("INSERT INTO vocabulary_queue(word,normalized_word) VALUES ('x','x')")
        raise RuntimeError("interrupted")
    assert db.inspect() == []


def test_interrupted_final_attempt_remains_retryable(db):
    db.enqueue("word", "word")
    job = db.claim(db.eligible(1), 1)[0]
    assert job.attempt_count == 1
    assert db.eligible(1) == []
    db.recover()
    assert db.eligible(1) == [job.id]
