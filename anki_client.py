"""AnkiConnect only: existing note types and styling are preserved."""

import hashlib
import html
import time
from html.parser import HTMLParser
from typing import Any

from capture import normalize_selection
from config import Config
from generator import Flashcard
from http_client import ServiceError, request_json


def identity_tag(normalized: str) -> str:
    return "vocab_key::" + hashlib.sha256(normalized.encode()).hexdigest()


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"br", "div", "p"}:
            self.parts.append(" ")


def field_word(value: str) -> str | None:
    parser = PlainText()
    parser.feed(value)
    try:
        return normalize_selection("".join(parser.parts))[1]
    except ValueError:
        return None


def quote_search(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class AnkiClient:
    def __init__(self, config: Config):
        self.config = config
        self.existing: dict[str, int] = {}

    def call(self, action: str, **params: Any) -> Any:
        payload = {"action": action, "version": 6, "params": params}
        if self.config.anki_api_key:
            payload["key"] = self.config.anki_api_key
        response = request_json(self.config.anki_url, payload, timeout=10)
        if "result" not in response or "error" not in response:
            raise ServiceError("Malformed AnkiConnect response")
        if response["error"] is not None:
            raise ServiceError(f"AnkiConnect {action}: {response['error']}")
        return response["result"]

    def ensure_profile(self) -> None:
        if self.config.anki_profile and self.call("getActiveProfile") != self.config.anki_profile:
            raise ServiceError("Anki active profile differs from VOCAB_ANKI_PROFILE")

    def prepare(self) -> None:
        deadline = time.monotonic() + self.config.anki_wait_timeout
        while True:
            try:
                if self.call("version") < 6:
                    raise ServiceError("AnkiConnect version 6 or newer is required")
                break
            except ServiceError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
        self.ensure_profile()
        fields = self.call("modelFieldNames", modelName=self.config.anki_note_type)
        expected = {self.config.anki_front_field, self.config.anki_back_field}
        if not expected.issubset(fields) or fields[0] != self.config.anki_front_field:
            raise ServiceError(
                "Mapped front must be the note type's first field; check field mapping"
            )
        if len(fields) != 2:
            raise ServiceError("MVP supports Basic-style note types with exactly two fields")
        if self.config.anki_deck not in self.call("deckNames"):
            raise ServiceError(f"Create destination deck {self.config.anki_deck!r} in Anki first")
        # Index existing fronts across all decks for the selected model. No wildcard query
        # involving clipboard text, and formatting/case/whitespace variants compare equally.
        ids = self.call("findNotes", query=f"note:{quote_search(self.config.anki_note_type)}")
        for start in range(0, len(ids), 250):
            for note in self.call("notesInfo", notes=ids[start : start + 250]):
                normalized = field_word(note["fields"][self.config.anki_front_field]["value"])
                if normalized:
                    self.existing[normalized] = note["noteId"]

    def find_existing(self, normalized: str) -> int | None:
        self.ensure_profile()
        # Stable tags survive edits to the front, field/deck/model configuration changes,
        # and ambiguous addNote HTTP timeouts.
        ids = self.call("findNotes", query=f"tag:{identity_tag(normalized)}")
        return ids[0] if ids else self.existing.get(normalized)

    def import_card(self, card: Flashcard, normalized: str) -> int:
        existing = self.find_existing(normalized)
        if existing is not None:
            return existing
        esc = html.escape
        lines = [
            f"<b>Part of speech:</b> {esc(card.part_of_speech)}",
            f"<b>Definition:</b> {esc(card.definition)}",
            f"<b>Italian:</b> {esc(card.italian)}",
        ]
        if card.synonyms:
            lines.append(f"<b>Synonyms:</b> {esc(', '.join(card.synonyms))}")
        lines.append(f"<b>Example:</b> {esc(card.example)}")
        if card.usage_note:
            lines.append(f"<b>Usage:</b> {esc(card.usage_note)}")
        note = {
            "deckName": self.config.anki_deck,
            "modelName": self.config.anki_note_type,
            "fields": {
                self.config.anki_front_field: esc(card.headword),
                self.config.anki_back_field: "<br>".join(lines),
            },
            "tags": list(dict.fromkeys([*self.config.anki_tags, identity_tag(normalized)])),
            "options": {"allowDuplicate": False, "duplicateScope": "collection"},
        }
        try:
            note_id = self.call("addNote", note=note)
            if not isinstance(note_id, int) or isinstance(note_id, bool) or note_id <= 0:
                raise ServiceError("AnkiConnect did not return a valid note ID")
        except ServiceError:
            # addNote may have succeeded despite a lost HTTP response.
            existing = self.find_existing(normalized)
            if existing is None:
                raise
            note_id = existing
        self.existing[normalized] = note_id
        return note_id

    def sync(self) -> None:
        self.ensure_profile()
        self.call("sync")
