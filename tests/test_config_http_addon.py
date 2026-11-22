import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from config import Config, load_config
from http_client import ServiceError, request_json


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765",
        "http://example.com",
        "http://127.0.0.1@evil.com",
        "http://127.0.0.1:8765/api",
        "http://127.0.0.1:8765?x=1",
    ],
)
def test_remote_urls_rejected(url):
    with pytest.raises(ValidationError):
        Config(anki_url=url)


def test_env_config(tmp_path, monkeypatch):
    path = tmp_path / "config.env"
    path.write_text("VOCAB_ANKI_DECK=Italian\nVOCAB_ANKI_TAGS=one two\nVOCAB_ANKI_SYNC=true\n")
    monkeypatch.setenv("VOCAB_ANKI_DECK", "English")
    config = load_config(path)
    assert config.anki_deck == "English"
    assert config.anki_tags == ["one", "two"]
    assert config.anki_sync is True
    with pytest.raises(ValueError):
        load_config(tmp_path / "missing")


@pytest.fixture
def local_server():
    state = {"body": {"ok": True}, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", "http://example.com/")
            self.end_headers()
            self.wfile.write(json.dumps(state["body"]).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_bypasses_proxies(local_server, monkeypatch):
    url, _ = local_server
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    assert request_json(url) == {"ok": True}


def test_http_refuses_redirect(local_server):
    url, state = local_server
    state["status"] = 302
    with pytest.raises(ServiceError, match="HTTP 302"):
        request_json(url)


def test_http_invalid_response(local_server):
    url, state = local_server
    state["body"] = []
    with pytest.raises(ServiceError, match="non-object"):
        request_json(url)


def test_addon_launch_is_nonblocking(tmp_path, monkeypatch):
    import sys

    project = tmp_path / "project"
    project.mkdir()
    (project / "worker.py").touch()
    env_file = tmp_path / "config.env"
    env_file.touch()
    config = {
        "python_path": sys.executable,
        "project_path": str(project),
        "config_path": str(env_file),
        "profile": "English",
    }
    aqt = ModuleType("aqt")
    aqt.gui_hooks = SimpleNamespace(profile_did_open=[])
    aqt.mw = SimpleNamespace(
        addonManager=SimpleNamespace(getConfig=lambda _: config), pm=SimpleNamespace(name="English")
    )
    monkeypatch.setitem(sys.modules, "aqt", aqt)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("PYTHONHOME", "/wrong/anki/python")
    spec = importlib.util.spec_from_file_location(
        "vocabulary_addon", Path(__file__).parents[1] / "anki_addon/__init__.py"
    )
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    spawn = Mock()
    monkeypatch.setattr(addon.subprocess, "Popen", spawn)
    assert aqt.gui_hooks.profile_did_open == [addon.on_profile_open]
    addon.on_profile_open()
    spawn.assert_called_once()
    assert spawn.call_args.kwargs["env"]["VOCAB_ANKI_PROFILE"] == "English"
    assert "PYTHONHOME" not in spawn.call_args.kwargs["env"]
    spawn.return_value.wait.assert_not_called()
    config["profile"] = "Other"
    addon.on_profile_open()
    spawn.assert_called_once()
