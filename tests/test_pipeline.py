"""Real SQLite and HTTP clients, with fake Ollama/Anki services on loopback."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from database import Database
from worker import run


def test_end_to_end_http_pipeline(config, card):
    notes = {}
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def reply(self, data):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def do_GET(self):
            self.reply({"models": [{"name": config.ollama_model}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/generate":
                calls.append("generate")
                records = json.loads(body["prompt"])
                cards = [
                    card.model_copy(
                        update={"job_id": record["job_id"], "headword": record["word"]}
                    ).model_dump()
                    for record in records
                ]
                self.reply({"done": True, "response": json.dumps({"cards": cards})})
                return
            action, params = body["action"], body["params"]
            calls.append(action)
            if action == "version":
                result = 6
            elif action == "getActiveProfile":
                result = "English"
            elif action == "modelFieldNames":
                result = ["Front", "Back"]
            elif action == "deckNames":
                result = [config.anki_deck]
            elif action == "findNotes":
                query = params["query"]
                result = [
                    nid
                    for nid, note in notes.items()
                    if not query.startswith("tag:") or query[4:] in note["tags"]
                ]
            elif action == "notesInfo":
                result = [
                    {
                        "noteId": nid,
                        "fields": {
                            key: {"value": value} for key, value in notes[nid]["fields"].items()
                        },
                    }
                    for nid in params["notes"]
                ]
            elif action == "addNote":
                result = len(notes) + 100
                notes[result] = params["note"]
            elif action == "sync":
                result = None
            else:
                raise AssertionError(action)
            self.reply({"result": result, "error": None})

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config.anki_url = config.ollama_url = f"http://127.0.0.1:{server.server_port}"
    config.anki_profile = "English"
    config.anki_sync = True
    try:
        with Database(config.db_path) as db:
            db.enqueue("Recondite", "recondite")
            db.enqueue("obscure", "obscure")
        assert run(config) == 0
        assert len(notes) == 2
        assert calls.count("generate") == 1
        assert calls.count("sync") == 1
        with Database(config.db_path) as db:
            assert all(row["status"] == "imported" for row in db.inspect())
            # Simulate losing queue confirmation of an already imported note.
            db.connection.execute(
                "UPDATE vocabulary_queue SET status='processing', anki_note_id=NULL WHERE id=1"
            )
        assert run(config) == 0
        assert len(notes) == 2
        assert calls.count("generate") == 1
        assert calls.count("addNote") == 2
        previous = len(calls)
        assert run(config) == 0
        assert len(calls) == previous  # Fully idle startup performs no HTTP requests.
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
