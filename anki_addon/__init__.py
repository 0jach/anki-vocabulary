"""Install this directory into Anki's add-ons folder; no third-party dependencies here."""

import json
import os
import subprocess
from pathlib import Path

from aqt import gui_hooks, mw


def on_profile_open() -> None:
    config = mw.addonManager.getConfig(__name__) or {}
    if not config.get("enabled", True):
        return
    profile = config.get("profile", "")
    if profile and mw.pm.name != profile:
        return
    # Popen only; the subprocess handles readiness, locks, logging, and all slow work.
    try:
        python = Path(config["python_path"]).expanduser()
        project = Path(config["project_path"]).expanduser()
        env_file = Path(config["config_path"]).expanduser()
        if not python.is_absolute() or not python.is_file():
            raise ValueError("Set python_path to the project's absolute .venv/bin/python path")
        if not project.is_absolute() or not (project / "worker.py").is_file():
            raise ValueError("Set project_path to the absolute project checkout path")
        if not env_file.is_absolute() or not env_file.is_file():
            raise ValueError("Set config_path to an existing absolute config.env path")
        log_dir = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        log_dir = log_dir / "anki-vocabulary"
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Pin to the opening profile even if the environment file has no profile guard.
        env = os.environ.copy()
        env["VOCAB_ANKI_PROFILE"] = mw.pm.name
        # Anki's bundled Python/Qt environment must not leak into the project's interpreter.
        for key in ("PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH", "LD_PRELOAD"):
            env.pop(key, None)
        fd = os.open(log_dir / "worker.log", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as log:
            subprocess.Popen(
                [str(python), str(project / "worker.py"), "--config", str(env_file)],
                cwd=project,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"event": "vocabulary_addon_launch_failed", "error": str(exc)}))


gui_hooks.profile_did_open.append(on_profile_open)
