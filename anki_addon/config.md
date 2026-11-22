Set `python_path` to the project virtual environment's Python executable, `project_path`
to the checkout, and `config_path` to your configuration file. All three must be absolute.
Anki's bundled Python does not need the application's dependencies.

Set `profile` to the exact Anki profile name to launch only for that profile (recommended).
An empty value launches on every profile open; each worker is pinned to the opening profile.
Use one intended profile per queue, otherwise different launches could distribute words
between collections. `enabled: false` disables startup processing.

Worker output: `~/.local/state/anki-vocabulary/worker.log` (or under `XDG_STATE_HOME`).
