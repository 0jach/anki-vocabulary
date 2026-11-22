from unittest.mock import MagicMock, Mock

import pytest

import anki_client
import worker
from anki_client import AnkiClient, field_word, identity_tag
from database import Database
from http_client import ServiceError
from worker import WorkerBusy, run, worker_lock


def test_actual_client_unavailable(config, monkeypatch):
    request = Mock(side_effect=ServiceError("connection refused"))
    monkeypatch.setattr(anki_client, "request_json", request)
    with pytest.raises(ServiceError, match="connection refused"):
        AnkiClient(config).prepare()
    request.assert_called_once()


def test_anki_api_key_and_errors(config, monkeypatch):
    config.anki_api_key = "test-key"
    request = Mock(return_value={"result": None, "error": "model unavailable"})
    monkeypatch.setattr(anki_client, "request_json", request)
    with pytest.raises(ServiceError, match="model unavailable"):
        AnkiClient(config).call("modelFieldNames", modelName="Basic")
    assert request.call_args.args[1]["key"] == "test-key"


def test_existing_formatted_front(config):
    client = AnkiClient(config)
    client.call = Mock(
        side_effect=[
            6,
            ["Front", "Back"],
            [config.anki_deck],
            [99],
            [
                {
                    "noteId": 99,
                    "fields": {"Front": {"value": "<b>RECONDITE</b>"}},
                }
            ],
            [],
        ]
    )
    client.prepare()
    assert client.find_existing("recondite") == 99
    assert field_word("take&nbsp;into<br>account") == "take into account"


def test_duplicate_tag_skips_add(config, card):
    client = AnkiClient(config)
    client.call = Mock(return_value=[321])
    assert client.import_card(card, "recondite") == 321
    assert client.call.call_count == 1


def test_import_content_escaped(config, card):
    client = AnkiClient(config)
    card.definition = "A <script> and & example"
    client.call = Mock(side_effect=[[], 123])
    assert client.import_card(card, "recondite") == 123
    note = client.call.call_args.kwargs["note"]
    assert "&lt;script&gt;" in note["fields"]["Back"]
    assert identity_tag("recondite") in note["tags"]
    assert note["options"]["allowDuplicate"] is False
    assert "css" not in note


def test_lost_import_response_reconciles(config, card):
    client = AnkiClient(config)
    client.call = Mock(side_effect=[[], ServiceError("timeout"), [123]])
    assert client.import_card(card, "recondite") == 123


def test_failed_import(config, card):
    client = AnkiClient(config)
    client.call = Mock(side_effect=[[], ServiceError("cannot add"), []])
    with pytest.raises(ServiceError, match="cannot add"):
        client.import_card(card, "recondite")


def test_anki_unavailable_no_attempts(config, db, monkeypatch):
    db.enqueue("word", "word")
    client = Mock()
    client.prepare.side_effect = ServiceError("offline")
    monkeypatch.setattr(worker, "AnkiClient", Mock(return_value=client))
    ollama = Mock()
    monkeypatch.setattr(worker, "OllamaSession", ollama)
    with pytest.raises(ServiceError):
        run(config)
    ollama.assert_not_called()
    assert db.inspect()[0]["attempt_count"] == 0


def test_wrong_profile(config):
    config.anki_profile = "English"
    client = AnkiClient(config)
    client.call = Mock(return_value="Other")
    with pytest.raises(ServiceError, match="profile"):
        client.find_existing("word")


def test_empty_queue_contacts_no_services(config, monkeypatch):
    anki = Mock()
    ollama = Mock()
    monkeypatch.setattr(worker, "AnkiClient", anki)
    monkeypatch.setattr(worker, "OllamaSession", ollama)
    assert run(config) == 0
    anki.assert_not_called()
    ollama.assert_not_called()


@pytest.fixture
def services(monkeypatch, card):
    client = Mock()
    client.find_existing.return_value = None
    client.import_card.return_value = 123
    monkeypatch.setattr(worker, "AnkiClient", Mock(return_value=client))
    session = MagicMock()
    monkeypatch.setattr(worker, "OllamaSession", Mock(return_value=session))
    generate = Mock(return_value=[card])
    monkeypatch.setattr(worker, "generate_cards", generate)
    return client, session, generate


def test_worker_imports_and_syncs(config, db, services):
    client, session, generate = services
    config.anki_sync = True
    db.enqueue("Recondite", "recondite")
    assert run(config) == 0
    assert db.inspect()[0]["anki_note_id"] == 123
    assert db.inspect()[0]["status"] == "imported"
    assert not db.sync_pending()
    client.sync.assert_called_once()
    session.__exit__.assert_called_once()


def test_worker_recovers_without_llm(config, db, services):
    client, session, generate = services
    db.enqueue("Recondite", "recondite")
    db.claim(db.eligible(3), 3)  # Simulate the prior worker's abrupt exit.
    client.find_existing.return_value = 123
    assert run(config) == 0
    assert db.inspect()[0]["status"] == "imported"
    generate.assert_not_called()
    session.__enter__.assert_not_called()


def test_database_failure_after_successful_import(config, db, services, monkeypatch):
    client, _, generate = services
    db.enqueue("Recondite", "recondite")
    original = Database.imported
    monkeypatch.setattr(Database, "imported", Mock(side_effect=RuntimeError("disk full")))
    assert run(config) == 1
    assert db.inspect()[0]["status"] == "failed"
    monkeypatch.setattr(Database, "imported", original)
    client.find_existing.return_value = 123
    assert run(config) == 0
    assert db.inspect()[0]["anki_note_id"] == 123
    client.import_card.assert_called_once()
    generate.assert_called_once()


def test_retry_limits(config, db, services):
    _, _, generate = services
    db.enqueue("Recondite", "recondite")
    generate.side_effect = ServiceError("bad JSON")
    for _ in range(config.max_attempts):
        assert run(config) == 1
    assert db.inspect()[0]["attempt_count"] == config.max_attempts
    assert run(config) == 0
    assert generate.call_count == config.max_attempts
    assert db.retry() == 1
    generate.side_effect = None
    assert run(config) == 0
    assert db.inspect()[0]["status"] == "imported"


def test_interruption_recovers_and_shuts_down(config, db, services):
    _, session, generate = services
    db.enqueue("Recondite", "recondite")
    generate.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run(config)
    session.__exit__.assert_called_once()
    assert db.inspect()[0]["status"] == "failed"
    assert db.inspect()[0]["last_error"] == "Worker interrupted"


def test_sync_failure_persisted_without_llm_rerun(config, db, services):
    client, _, generate = services
    config.anki_sync = True
    db.enqueue("Recondite", "recondite")
    client.sync.side_effect = ServiceError("sync failed")
    with pytest.raises(ServiceError):
        run(config)
    assert db.inspect()[0]["status"] == "imported"
    assert db.sync_pending()
    client.sync.side_effect = None
    assert run(config) == 0
    assert not db.sync_pending()
    generate.assert_called_once()


def test_concurrent_worker_lock(config):
    with worker_lock(config), pytest.raises(WorkerBusy), worker_lock(config):
        pass
    # Kernel releases the lock; persistent lock file is harmless.
    with worker_lock(config):
        pass


def test_finite_snapshot_and_bounded_batches(config, db, services):
    client, _, generate = services
    config.batch_size = 1
    db.enqueue("Recondite", "recondite")
    db.enqueue("obscure", "obscure")

    def output(session, jobs, grounding):
        from generator import Flashcard

        db.enqueue("later", "later")  # Arrival during inference waits for next startup.
        return [
            Flashcard(
                job_id=j.id,
                headword=j.word,
                part_of_speech="adjective",
                definition="Not widely known",
                italian="oscuro",
                synonyms=[],
                example="An obscure text.",
                usage_note=None,
            )
            for j in jobs
        ]

    generate.side_effect = output
    assert run(config) == 0
    assert [row["status"] for row in db.inspect()] == ["imported", "imported", "pending"]
    assert generate.call_count == 2
