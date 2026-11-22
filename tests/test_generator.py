import json
import os
import signal
import subprocess
from unittest.mock import Mock

import pytest

import generator
from database import Job
from generator import OllamaSession, generate_cards
from http_client import ServiceError


def test_structured_generation(config, card, monkeypatch):
    request = Mock(
        return_value={
            "done": True,
            "response": json.dumps(
                {
                    "cards": [card.model_dump()],
                }
            ),
        }
    )
    monkeypatch.setattr(generator, "request_json", request)
    session = OllamaSession(config)
    jobs = [Job(1, "Recondite", "recondite", None, 1)]
    assert generate_cards(session, jobs) == [card]
    payload = request.call_args.args[1]
    assert payload["format"]["type"] == "object"
    assert payload["stream"] is False
    assert payload["keep_alive"] == 0


@pytest.mark.parametrize(
    "change", ["invalid_json", "extra", "missing", "wrong_id", "headword", "type"]
)
def test_invalid_output(config, card, monkeypatch, change):
    data = card.model_dump()
    if change == "extra":
        data["unwanted"] = "x"
    elif change == "missing":
        del data["italian"]
    elif change == "wrong_id":
        data["job_id"] = 2
    elif change == "headword":
        data["headword"] = "different"
    elif change == "type":
        data["synonyms"] = "not a list"
    response = "not json" if change == "invalid_json" else json.dumps({"cards": [data]})
    monkeypatch.setattr(
        generator, "request_json", Mock(return_value={"done": True, "response": response})
    )
    with pytest.raises(ServiceError):
        generate_cards(OllamaSession(config), [Job(1, "Recondite", "recondite", None, 1)])


def test_truncated_generation(config, monkeypatch):
    monkeypatch.setattr(generator, "request_json", Mock(return_value={"done": False}))
    with pytest.raises(ServiceError, match="incomplete"):
        generate_cards(OllamaSession(config), [Job(1, "word", "word", None, 1)])


def test_reuse_server_never_stops_it(config, monkeypatch):
    monkeypatch.setattr(
        generator,
        "request_json",
        Mock(
            return_value={
                "models": [{"name": config.ollama_model}],
            }
        ),
    )
    popen = Mock()
    kill = Mock()
    monkeypatch.setattr(generator.subprocess, "Popen", popen)
    monkeypatch.setattr(generator.os, "killpg", kill)
    with OllamaSession(config) as session:
        assert session.process is None
    popen.assert_not_called()
    kill.assert_not_called()


def test_missing_model_no_download(config, monkeypatch):
    request = Mock(return_value={"models": []})
    monkeypatch.setattr(generator, "request_json", request)
    with pytest.raises(ServiceError, match="not installed"), OllamaSession(config):
        pass
    assert all(call.args[0].endswith("/api/tags") for call in request.call_args_list)


def test_cloud_model_refused(config, monkeypatch):
    monkeypatch.setattr(
        generator,
        "request_json",
        Mock(
            return_value={
                "models": [{"name": config.ollama_model, "remote_host": "https://ollama.com"}],
            }
        ),
    )
    with pytest.raises(ServiceError, match="cloud"), OllamaSession(config):
        pass


def test_startup_failure_cleans_owned_process(config, monkeypatch):
    monkeypatch.setattr(generator, "request_json", Mock(side_effect=ServiceError("offline")))
    child = Mock(pid=12345)
    child.poll.return_value = 1
    monkeypatch.setattr(generator.subprocess, "Popen", Mock(return_value=child))
    kill = Mock()
    monkeypatch.setattr(generator.os, "killpg", kill)
    session = OllamaSession(config)
    with pytest.raises(ServiceError, match="exited"), session:
        pass
    kill.assert_called_once_with(12345, signal.SIGTERM)
    assert session.process is None
    assert session.log is None


def test_shutdown_escalates(config, monkeypatch):
    session = OllamaSession(config)
    child = Mock(pid=12345)
    child.wait.side_effect = [subprocess.TimeoutExpired("ollama", 5), 0]
    session.process = child
    kill = Mock()
    monkeypatch.setattr(generator.os, "killpg", kill)
    session.close()
    assert [call.args[1] for call in kill.call_args_list] == [signal.SIGTERM, signal.SIGKILL]


def test_startup_timeout_stops_child(config, monkeypatch):
    monkeypatch.setattr(generator, "request_json", Mock(side_effect=ServiceError("offline")))
    monkeypatch.setattr(generator.time, "monotonic", Mock(side_effect=[0, 0, 100]))
    monkeypatch.setattr(generator.time, "sleep", Mock())
    child = Mock(pid=12345)
    child.poll.return_value = None
    monkeypatch.setattr(generator.subprocess, "Popen", Mock(return_value=child))
    kill = Mock()
    monkeypatch.setattr(generator.os, "killpg", kill)
    with pytest.raises(ServiceError, match="timed out"), OllamaSession(config):
        pass
    kill.assert_called_once_with(12345, signal.SIGTERM)


@pytest.fixture
def fake_ollama(tmp_path):
    """Real child executable implementing just the local /api/tags health check."""
    import sys

    path = tmp_path / "fake-ollama"
    path.write_text(f"""#!{sys.executable}
import http.server, json, os
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({{"models": [{{"name": "qwen2.5:3b"}}]}}).encode())
    def log_message(self, *args): pass
host, port = os.environ["OLLAMA_HOST"].split(":")
http.server.HTTPServer((host, int(port)), Handler).serve_forever()
""")
    path.chmod(0o700)
    return str(path)


def test_real_subprocess_lifecycle(config, fake_ollama, monkeypatch):
    # Force initial default probe offline without depending on host-installed services.
    original = generator.request_json

    def request(url, *args, **kwargs):
        if url.startswith(config.ollama_url):
            raise ServiceError("offline")
        return original(url, *args, **kwargs)

    monkeypatch.setattr(generator, "request_json", request)
    config.ollama_binary = fake_ollama
    with pytest.raises(KeyboardInterrupt), OllamaSession(config) as session:
        child = session.process
        assert child.poll() is None
        assert session.url != config.ollama_url
        raise KeyboardInterrupt()
    assert child.poll() is not None
    assert session.process is None


def test_parent_death_kills_owned_server(config, fake_ollama, tmp_path):
    import sys
    import time

    # A wrapper process starts the actual Linux prctl launcher, then is abruptly killed.
    pidfile = tmp_path / "child.pid"
    launcher = generator.Path(generator.__file__).with_name("ollama_process.py")
    script = """
import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, sys.argv[1], str(os.getpid()), sys.argv[2], "serve"],
                         start_new_session=True)
pidfile = Path(sys.argv[3])
pidfile.with_suffix('.tmp').write_text(str(child.pid))
pidfile.with_suffix('.tmp').rename(pidfile)
time.sleep(30)
"""
    env = {**os.environ, "OLLAMA_HOST": "127.0.0.1:0"}
    parent = subprocess.Popen(
        [sys.executable, "-c", script, str(launcher), fake_ollama, str(pidfile)], env=env
    )
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child_pid = int(pidfile.read_text())
        # Wait until exec completed so we exercise PDEATHSIG, not just the race check.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if fake_ollama.encode() in generator.Path(f"/proc/{child_pid}/cmdline").read_bytes():
                break
            time.sleep(0.02)
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stat = generator.Path(f"/proc/{child_pid}/stat")
            if not stat.exists() or stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("Ollama child survived abrupt worker death")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if pidfile.exists():
            try:
                os.killpg(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
