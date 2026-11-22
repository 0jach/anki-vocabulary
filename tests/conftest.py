import pytest

from config import Config
from database import Database
from generator import Flashcard


@pytest.fixture
def config(tmp_path):
    return Config(db_path=tmp_path / "queue.sqlite3", anki_wait_timeout=0)


@pytest.fixture
def db(config):
    with Database(config.db_path) as database:
        yield database


@pytest.fixture
def card():
    return Flashcard(
        job_id=1,
        headword="Recondite",
        part_of_speech="adjective",
        definition="Difficult to understand; known by very few people.",
        italian="Recondito, astruso",
        synonyms=["obscure", "abstruse", "esoteric"],
        example="The professor's recondite theories confused the students.",
        usage_note=None,
    )
