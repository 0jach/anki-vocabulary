"""Interactive desktop installation; runtime commands never install or download anything."""

import argparse
import io
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

from dotenv import dotenv_values

from anki_client import AnkiClient
from config import Config, load_config
from database import Database
from generator import OllamaSession
from http_client import ServiceError
from worker import interrupted, worker_lock

PROJECT = Path(__file__).resolve().parent
MAX_DOWNLOAD = 20 * 1024 * 1024


def choose(label: str, values: list[str], default: str | None = None) -> str:
    if not values:
        raise RuntimeError(f"No {label.lower()} available in this Anki profile")
    print(f"\n{label}:")
    for index, value in enumerate(values, 1):
        print(f"  {index}. {value}")
    default_index = values.index(default) + 1 if default in values else None
    while True:
        reply = input(
            f"Choose 1–{len(values)}" + (f" [{default_index}]" if default_index else "") + ": "
        ).strip()
        if not reply and default_index:
            return values[default_index - 1]
        if reply.isascii() and reply.isdigit() and 1 <= int(reply) <= len(values):
            return values[int(reply) - 1]
        print("Enter a number from the list.")


def yes_no(prompt: str, default: bool = False) -> bool:
    while True:
        reply = input(prompt + (" [Y/n] " if default else " [y/N] ")).strip().lower()
        if not reply:
            return default
        if reply in {"y", "yes", "n", "no"}:
            return reply in {"y", "yes"}
        print("Enter y or n.")


def write_json(path: Path, value: dict) -> None:
    """Replace atomically so an Anki restart never sees a partial configuration."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".setup-")
    try:
        with os.fdopen(fd, "w") as target:
            json.dump(value, target, indent=2, ensure_ascii=False)
            target.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save_config(path: Path, config: Config) -> None:
    # Preserve unknown settings and existing secrets; update only this application's keys.
    values = dict(dotenv_values(path, interpolate=False)) if path.exists() else {}
    for field in Config.model_fields:
        value = getattr(config, field)
        if value is None:
            values.pop(f"VOCAB_{field.upper()}", None)
        else:
            values[f"VOCAB_{field.upper()}"] = (
                " ".join(value)
                if isinstance(value, list)
                else str(value).lower()
                if isinstance(value, bool)
                else str(value)
            )
    text = "".join(
        f"{key}='" + value.replace("\\", "\\\\").replace("'", "\\'") + "'\n"
        for key, value in values.items()
        if value is not None
    )
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".setup-")
    try:
        with os.fdopen(fd, "w") as target:
            target.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def download(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "anki-vocabulary-setup"})
    with urlopen(request, timeout=60) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise RuntimeError("AnkiConnect download exceeded size limit")
    return data


def install_ankiconnect(addons: Path) -> bool:
    """Install upstream plugin source at an immutable revision; keep existing installs."""
    target = addons / "2055492159"
    if target.exists():
        if not (target / "__init__.py").is_file():
            raise RuntimeError(f"Incomplete AnkiConnect installation at {target}")
        meta_path = target / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        was_disabled = meta.get("disabled", False)
        if was_disabled:
            meta["disabled"] = False
            write_json(meta_path, meta)
        return bool(was_disabled)
    print("Downloading AnkiConnect from github.com/FooSoft/anki-connect…")
    commit = json.loads(
        download("https://api.github.com/repos/FooSoft/anki-connect/commits/master")
    )
    revision = commit.get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("Upstream returned an invalid AnkiConnect revision")
    archive_data = download(f"https://codeload.github.com/FooSoft/anki-connect/zip/{revision}")
    addons.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=addons, prefix=".ankiconnect-") as stage:
        plugin = Path(stage) / "plugin"
        plugin.mkdir()
        total = 0
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            for item in archive.infolist():
                parts = PurePosixPath(item.filename).parts
                if len(parts) < 3 or parts[1] != "plugin":
                    continue
                if ".." in parts or item.filename.startswith("/") or "\\" in item.filename:
                    raise RuntimeError("Unsafe path in AnkiConnect archive")
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("Symlinks are not allowed in the add-on archive")
                total += item.file_size
                if total > MAX_DOWNLOAD * 2:
                    raise RuntimeError("Unpacked AnkiConnect archive exceeded size limit")
                destination = plugin.joinpath(*parts[2:])
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(item))
            # Retain the upstream license when it is outside plugin/.
            for name in archive.namelist():
                if len(PurePosixPath(name).parts) == 2 and Path(name).name in {
                    "LICENSE",
                    "LICENSE.txt",
                }:
                    (plugin / Path(name).name).write_bytes(archive.read(name))
        if not (plugin / "__init__.py").is_file() or not (plugin / "config.json").is_file():
            raise RuntimeError("Downloaded archive is missing AnkiConnect plugin files")
        write_json(plugin / "manifest.json", {"name": "AnkiConnect", "package": "2055492159"})
        (plugin / "upstream_revision.txt").write_text(revision + "\n")
        plugin.rename(target)
    return True


def startup_settings(config_path: Path, enabled: bool, profile: str = "") -> dict:
    return {
        "enabled": enabled,
        "python_path": str(PROJECT / ".venv/bin/python"),
        "project_path": str(PROJECT),
        "config_path": str(config_path),
        "profile": profile,
    }


def install_startup(addons: Path, settings: dict) -> None:
    target = addons / "anki_vocabulary_startup"
    source = PROJECT / "anki_addon"
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_file() and path.name != "config.json":
            shutil.copy2(path, target / path.name)
    write_json(target / "config.json", settings)
    meta_path = target / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta.update(config=settings, disabled=False)
    write_json(meta_path, meta)


def stop_permanent_servers() -> None:
    if not shutil.which("systemctl"):
        return
    for scope in ([], ["--user"]):
        base = ["systemctl", *scope]
        installed = subprocess.run(
            [*base, "show", "--property=LoadState", "--value", "ollama.service"],
            capture_output=True,
            text=True,
            check=False,
        )
        if installed.returncode or installed.stdout.strip() != "loaded":
            continue
        enabled = subprocess.run([*base, "is-enabled", "--quiet", "ollama.service"], check=False)
        active = subprocess.run([*base, "is-active", "--quiet", "ollama.service"], check=False)
        if enabled.returncode == 0 or active.returncode == 0:
            command = [*base, "disable", "--now", "ollama.service"]
            if not scope:
                command.insert(0, "sudo")
            print("Disabling permanent Ollama service:", shlex.join(command))
            subprocess.run(command, check=True)


def connect_anki(config: Config, addons: Path) -> AnkiClient:
    # Reuse an existing local installation's credentials without asking users to copy secrets.
    settings = {}
    connect_dir = addons / "2055492159"
    for filename in ("config.json", "meta.json"):
        path = connect_dir / filename
        if path.is_file():
            stored = json.loads(path.read_text())
            settings.update(stored.get("config", {}) if filename == "meta.json" else stored)
    updates = {}
    if not config.anki_api_key and settings.get("apiKey"):
        updates["anki_api_key"] = settings["apiKey"]
    if config.anki_url == Config().anki_url and settings.get("webBindPort"):
        updates["anki_url"] = f"http://127.0.0.1:{int(settings['webBindPort'])}"
    if updates:
        # The caller persists this instance after the user selects a destination.
        validated = Config.model_validate({**config.model_dump(), **updates})
        config.anki_api_key = validated.anki_api_key
        config.anki_url = validated.anki_url
    client = AnkiClient(config)
    try:
        if client.call("version") >= 6:
            return client
        raise RuntimeError("Update AnkiConnect to support API version 6")
    except ServiceError as exc:
        if "api key" in str(exc).lower():
            raise RuntimeError("Check VOCAB_ANKI_API_KEY in your configuration") from exc
    install_ankiconnect(addons)
    print("\nAnkiConnect is installed. Close Anki completely, reopen it, and open your profile.")
    while True:
        input("Press Enter once Anki has reopened (Ctrl+C to cancel): ")
        try:
            if client.call("version") >= 6:
                return client
        except ServiceError as exc:
            print(f"AnkiConnect is not ready: {exc}")
            print("If you use an API key, set VOCAB_ANKI_API_KEY in the config and rerun setup.")


def select_destination(client: AnkiClient, config: Config) -> Config:
    profile = client.call("getActiveProfile")
    if not isinstance(profile, str) or not profile:
        raise RuntimeError("Open your intended Anki profile and rerun setup")
    print(f"\nActive Anki profile: {profile}. Decks are read from this profile.")
    deck = choose("Destination deck", sorted(client.call("deckNames")), config.anki_deck)
    models = []
    fields_by_model = {}
    for model in sorted(client.call("modelNames")):
        fields = client.call("modelFieldNames", modelName=model)
        if len(fields) == 2:
            templates = client.call("modelTemplates", modelName=model)
            if any("{{cloze:" in template.get("Front", "") for template in templates.values()):
                continue
            models.append(model)
            fields_by_model[model] = fields
    model = choose("Basic-style note type", models, config.anki_note_type)
    front, back = fields_by_model[model]
    print(f"Using {front} for the word and {back} for the definition.")
    sync = yes_no("Request AnkiWeb sync after imports? Sign into Anki first.", config.anki_sync)
    return Config.model_validate(
        {
            **config.model_dump(),
            "anki_profile": profile,
            "anki_deck": deck,
            "anki_note_type": model,
            "anki_front_field": front,
            "anki_back_field": back,
            "anki_sync": sync,
        }
    )


def prepare_model(config: Config) -> None:
    print(f"\nPreparing local model {config.ollama_model} (a missing model will be downloaded)…")
    with OllamaSession(config, require_model=False) as session:
        model = config.ollama_model
        names = {item.get("name") for item in session._models()}
        if model not in names and (model + ":latest") not in names:
            env = {**os.environ, "OLLAMA_HOST": session.url, "OLLAMA_NO_CLOUD": "1"}
            child = subprocess.Popen([config.ollama_binary, "pull", model], env=env)
            try:
                if child.wait() != 0:
                    raise RuntimeError("Model download failed; rerun setup to resume")
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
        # Verify a successful download really installed a local model.
        installed = next(
            (item for item in session._models() if item.get("name") in {model, model + ":latest"}),
            None,
        )
        if installed is None or installed.get("remote_host") or installed.get("remote_model"):
            raise RuntimeError("The requested model is not installed locally")


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive Arch Wayland vocabulary setup")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--anki-data-dir", type=Path, help="Anki base directory containing addons21"
    )
    parser.add_argument("--model", help="Local Ollama model to install (default: configured model)")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.exit(1, "Run setup as your desktop user, without sudo.\n")
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    config_path = (
        (
            args.config
            or Path(os.environ.get("VOCAB_CONFIG", str(config_root / "anki-vocabulary/config.env")))
        )
        .expanduser()
        .resolve()
    )
    data_root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    addons = (args.anki_data_dir or data_root / "Anki2").expanduser().resolve() / "addons21"
    try:
        if not (PROJECT / ".venv/bin/python").is_file():
            raise RuntimeError("Run ./setup.sh to create the project's virtual environment first")
        if not config_path.exists():
            save_config(config_path, Config())
        config = load_config(config_path)
        if args.model:
            config = Config.model_validate({**config.model_dump(), "ollama_model": args.model})
        with worker_lock(config):
            # Keep automatic processing off until setup has completed successfully.
            install_startup(addons, startup_settings(config_path, False))
            client = connect_anki(config, addons)
            config = select_destination(client, config)
            # Check for profile switches before saving or processing the existing queue.
            client.config = config
            client.prepare()
            save_config(config_path, config)
            with Database(config.db_path):
                pass
            stop_permanent_servers()
            prepare_model(config)
            install_startup(addons, startup_settings(config_path, True, config.anki_profile))
        print(f"\nSetup complete. Destination: {config.anki_profile} → {config.anki_deck}")
        print("Restart Anki to enable automatic processing of your pending words.")
        command = [
            str(PROJECT / ".venv/bin/python"),
            str(PROJECT / "capture.py"),
            "--config",
            str(config_path),
        ]
        print("Bind a keyboard shortcut to:\n" + shlex.join(command))
        return 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        sqlite3.Error,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        print("Fix the problem and rerun ./setup.sh; your queue is preserved.", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled; rerun ./setup.sh to finish.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
