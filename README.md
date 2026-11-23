# Vocabulary → Anki

Capture English words and generate English/Italian Anki cards using a local Ollama
model. Built for **Arch Linux on Wayland**, with Python 3.12+.

**Select a word → press your shortcut → cards are generated and imported while Anki is open.**

## Setup

Run from this checkout, as your desktop user:

```bash
./setup.sh
```

Setup installs missing Arch packages (asking for sudo), prepares the Python environment,
installs AnkiConnect and the startup add-on, and downloads the configured local model
if needed. It disables enabled/running Ollama services and initializes the SQLite queue.
AnkiConnect is downloaded from its official upstream repository; existing installs are kept.

Open Anki in your intended profile when prompted. Setup lists that profile's decks
and lets you choose the destination, a Basic-style note type, and whether to request
AnkiWeb sync. It saves the configuration automatically. Restart Anki when setup finishes.
For sync, sign into AnkiWeb first. Desktop notifications require a notification daemon.

Bind a keyboard shortcut to the capture command printed by setup. Setup can be rerun
to change the destination. Optional arguments:

```bash
./setup.sh --model qwen2.5:3b
./setup.sh --anki-data-dir /path/to/Anki2
./setup.sh --config /path/to/config.env
```

## Use

1. Select an English word or short phrase and press your shortcut. If the application
   does not expose the selection, copy it first. A notification confirms capture.
2. If Anki is open, processing starts automatically in the background. Otherwise,
   words wait in the queue until you open Anki. Keep it open until imports finish.
3. Review the cards in your selected deck. Check definitions and translations for accuracy.

Duplicates are ignored. Capture saves immediately; AI processing runs separately.
Ollama runs on demand and stops afterward (an existing server is left running).
Only setup downloads models. Set `VOCAB_PROCESS_WHEN_ANKI_OPEN=false` in your config
to process only when Anki starts.

To try it, run this and then open Anki:

```bash
.venv/bin/python capture.py --text Recondite
```

To process an existing queue manually with Anki open:

```bash
.venv/bin/python worker.py
```

## Inspect and retry

```bash
.venv/bin/python database.py list --status pending
.venv/bin/python database.py list --status failed
.venv/bin/python database.py retry          # Reset all failed jobs
.venv/bin/python database.py retry --id 3   # Reset one job
.venv/bin/python worker.py                 # Process with Anki open
```

Failed jobs are retried on later captures or startups, up to three attempts by default.
Configuration: `~/.config/anki-vocabulary/config.env` (settings in `.env.example`).
Startup logs: `~/.local/state/anki-vocabulary/worker.log`.
Queue: `~/.local/share/anki-vocabulary/queue.sqlite3`.
