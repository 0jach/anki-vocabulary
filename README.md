# Local vocabulary → Anki

Select an English word or short phrase, press **Mod+Shift+A**, then open Anki when
you are ready. Capture saves to a local SQLite queue and shows a notification.
An Anki startup add-on launches a separate worker, which generates English/Italian
cards with a local Ollama model and imports them through AnkiConnect.

**Python 3.12+, Linux, niri/Wayland. No MySQL, external AI API, or background worker.**
SQLite replaces MySQL in the original plan: there is no database service or database
password to maintain. The only runtime Python dependencies are Pydantic and python-dotenv.

## 1. Install desktop prerequisites on Arch Linux

```bash
sudo pacman -Syu python python-pip wl-clipboard libnotify ollama anki
```

Use your existing notification daemon (for example, mako or swaync); `notify-send`
requires a running notification daemon. Install/configure one if your niri session
does not already provide notifications. SQLite is included with Python; the
optional `sqlite` package provides its command-line client.

The official Arch packages include [Anki](https://archlinux.org/packages/extra/x86_64/anki/),
[Ollama](https://archlinux.org/packages/extra/x86_64/ollama/),
[wl-clipboard](https://archlinux.org/packages/extra/x86_64/wl-clipboard/), and
[libnotify](https://archlinux.org/packages/extra/x86_64/libnotify/).
GPU acceleration depends on your hardware and installed Ollama backend. CPU inference
works but may require a longer generation timeout.

## 2. Set up Python and configuration

From this checkout, in Bash:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
mkdir -p ~/.config/anki-vocabulary
cp .env.example ~/.config/anki-vocabulary/config.env
chmod 600 ~/.config/anki-vocabulary/config.env
```

With `uv` installed, the equivalent environment setup is
`uv sync --extra dev --python 3.12`.

Edit `~/.config/anki-vocabulary/config.env`. Set the intended Anki profile, destination
deck, note type, field mapping, model, and optional API key. Defaults use the existing
`Basic` note type's `Front` and `Back` fields. Localized/custom Basic-style types work
with the corresponding names, as long as they have exactly two fields and the mapped
front is the first field. Templates and styling are never modified.

Configuration lookup is `--config PATH`, then `VOCAB_CONFIG`, then
`$XDG_CONFIG_HOME/anki-vocabulary/config.env` (default `~/.config/...`). Process
environment variables override file values. A checkout `.env` is only read if
explicitly selected, for example `python capture.py --config .env`.
Use `~` or absolute paths; shell variables and `${...}` are not expanded inside the file.
If no configuration file is present, defaults apply. An explicitly selected missing
file is an error.

## 3. Initialize the SQLite queue

```bash
.venv/bin/python database.py init
.venv/bin/python database.py list
```

The default database is `~/.local/share/anki-vocabulary/queue.sqlite3`.
`VOCAB_DB_PATH` changes it. Every command applies missing, versioned migrations
automatically; `init` is a useful explicit installation check. No server or credentials
are needed. Keep the queue on a **local filesystem**, not a network share.

The queue has `id`, `word`, `normalized_word`, nullable `context`, UTC `created_at`,
`status` (`pending`, `processing`, `imported`, `failed`), `attempt_count`, `last_error`,
`anki_note_id`, and `processed_at`. SQL values are parameterized. A unique normalized
word prevents duplicate capture across **all statuses**, including imported/failed
words. Re-capturing a failed word reports “Already captured”; use the retry command
below rather than creating another row.

## 4. Prepare a local Ollama model, without a permanent service

Disable a previously installed system service if present:

```bash
sudo systemctl disable --now ollama.service
```

If you installed a user service, also run
`systemctl --user disable --now ollama.service`. “Unit does not exist” means that
service was not installed. Remove any `ollama serve` desktop autostart entry too.
The application never changes services itself.

Download the model **once, explicitly**, under your desktop user account. In one
terminal start a temporary setup server:

```bash
OLLAMA_NO_CLOUD=1 ollama serve
```

In a second terminal:

```bash
ollama pull qwen2.5:3b
ollama list
```

Then stop the setup server with Ctrl+C. The application never pulls models. Configure
`VOCAB_OLLAMA_MODEL` to a local model you have already installed; the default is a
small starting point, not a guarantee of dictionary accuracy. Models downloaded as
root or by a system service may live in another account's model directory. Pull as
your desktop user or explicitly set `OLLAMA_MODELS` in the environment inherited by
Anki. Arbitrary Ollama settings in `config.env` are not exported to the child server.

For each nonempty run the worker probes `VOCAB_OLLAMA_URL`. If an existing local
server answers, it reuses it and **never terminates it**. Otherwise it starts
`ollama serve` on a dynamically selected, isolated `127.0.0.1` port and waits for
readiness. It refuses missing/cloud models. The private server sets
`OLLAMA_NO_CLOUD=1`; all application HTTP endpoints must be loopback addresses,
and HTTP redirects and proxies are disabled.

Generation uses Ollama's JSON-schema `format`, validates the response with Pydantic,
and sets `keep_alive=0` to release the model after each generation request. Only
the owned subprocess group is shut down on exit, exceptions, or Ctrl+C/SIGTERM.
A Linux parent-death signal also terminates the owned server if the worker is
abruptly killed. The worker does not shut down an existing server or other applications.
See [Ollama structured generation](https://docs.ollama.com/api/generate) and
[local-only configuration](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features).

## 5. Add the niri shortcut

Merge the binding from [examples/niri.kdl](examples/niri.kdl) into the `binds` block
in `~/.config/niri/config.kdl`. Replace the paths with your checkout and virtual
environment paths:

```kdl
binds {
    Mod+Shift+A hotkey-overlay-title="Capture English vocabulary" {
        spawn "/home/YOUR_USER/Github/anki-vocabulary/.venv/bin/python" "/home/YOUR_USER/Github/anki-vocabulary/capture.py";
    }
}
```

Use absolute paths: `spawn` does not expand `~` or shell variables. You do not need
to activate a virtual environment in niri. See niri's
[default configuration](https://github.com/niri-wm/niri/blob/main/resources/default-config.kdl)
for binding syntax. Run `niri validate` after editing.

Capture tries `wl-paste --primary --no-newline`, then `wl-paste --no-newline` if the
primary selection is missing/empty. If the application does not expose primary
selection, copy the word before pressing the shortcut. An invalid nonempty primary
selection is rejected rather than silently capturing an unrelated clipboard value.

Accepted input: 1–8 English words, at most 120 characters after normalization,
including internal apostrophes/hyphens. Case and whitespace are normalized for
deduplication; common smart quotes and hyphens are handled. Digits, URLs, code,
emoji, and non-English alphabets are rejected. Surrounding common punctuation is
removed. This is a syntax check, not a dictionary-membership test.

## 6. Install and configure AnkiConnect

In Anki: **Tools → Add-ons → Get Add-ons**, enter **2055492159**, then restart.
See the [AnkiConnect installation guide](https://github.com/FooSoft/anki-connect).

Keep its listener local (`webBindAddress: "127.0.0.1"`, `webBindPort: 8765`). If
you set `apiKey`, set the same `VOCAB_ANKI_API_KEY` in your private config file.
The client uses the supported version-6 compatibility API for installed AnkiConnect
versions; it does not access Anki's internal SQLite collection.

Create the configured destination deck (default **English Vocabulary**) yourself.
Verify the configured note type and its field names. The worker reports missing
deck/model/field configuration before starting inference.

For automatic sync, first sign into AnkiWeb and complete an ordinary manual sync,
then set `VOCAB_ANKI_SYNC=true`. The worker requests sync after imports/reconciliation;
it never handles your AnkiWeb credentials. AnkiConnect's legacy `sync` action may
acknowledge a scheduled sync before it completes: check Anki for account/network
errors. A failed HTTP sync request leaves a durable flag so the next startup can
request it again without regenerating cards. Completion after an accepted request
is handled by Anki, not tracked by this MVP.

## 7. Install the startup add-on

In Anki open **Tools → Add-ons → View Files**. Close Anki. Copy the supplied folder
to that directory as `anki_vocabulary_startup`, for example with the default Linux
data location:

```bash
cp -r anki_addon ~/.local/share/Anki2/addons21/anki_vocabulary_startup
```

The folder must directly contain `__init__.py`, `config.json`, and the manifest.
Edit the copied `config.json`, or select the add-on's **Config** action after restart:

```json
{
  "enabled": true,
  "python_path": "/home/YOUR_USER/Github/anki-vocabulary/.venv/bin/python",
  "project_path": "/home/YOUR_USER/Github/anki-vocabulary",
  "config_path": "/home/YOUR_USER/.config/anki-vocabulary/config.env",
  "profile": "User 1"
}
```

Use your exact intended profile name. Use **one profile per queue**. The startup
worker also checks the active profile before importing; switching profiles during
processing causes remaining operations to fail safely for retry. It does not switch
profiles automatically. The hook runs whenever a profile opens, including switches;
it launches `Popen` and returns immediately, leaving Anki's UI free during inference.
See [Anki's hook definitions](https://github.com/ankitects/anki/blob/main/qt/tools/genhooks_gui.py).

The worker takes a nonblocking lock next to the queue database. A second worker
exits harmlessly. The next run recovers orphaned `processing` rows; interrupted
attempts do not consume the retry budget. It takes a snapshot of eligible jobs and
processes it in bounded batches (default 8), one structured generation per batch,
using one Ollama server session. This bounds small-model context/output size;
increase `VOCAB_BATCH_SIZE` up to 50 if your model handles it. New captures during
a run wait for the next profile opening. With no eligible jobs or outstanding sync
request, the worker exits before contacting either AnkiConnect or Ollama.

## 8. Run an end-to-end smoke test

First leave automatic sync disabled, and use your intended test profile/deck.

1. Close Anki and stop the temporary setup Ollama server.
2. Queue a word without relying on the clipboard:

   ```bash
   .venv/bin/python capture.py --text Recondite
   .venv/bin/python database.py list --status pending
   ```

3. Repeat capture with `recondite`: it should report “Already captured”. Test the
   niri shortcut with a different selected word.
4. Open Anki. After local inference finishes, inspect the destination deck and run:

   ```bash
   .venv/bin/python database.py list --status imported
   tail -n 30 ~/.local/state/anki-vocabulary/worker.log
   pgrep -af 'ollama serve'
   ```

   Expect an Anki note ID for each imported job and no application-owned Ollama
   server after completion. If you had another server running, it remains running.
   Logs respect `XDG_STATE_HOME` when configured.
5. Confirm Front contains the headword and Back contains definition, Italian,
   synonyms if appropriate, example, part of speech, and optional usage note.
   Review linguistic accuracy. Restart Anki: no additional notes should appear.
6. Enable sync only after verifying ordinary AnkiWeb synchronization works, then
   capture another word and reopen Anki. Confirm sync in Anki itself.

For an immediate manual run while Anki is open:

```bash
.venv/bin/python worker.py
```

No service is installed by this project. Normal inference happens only on profile
opening; this explicit command is the troubleshooting exception.

## 9. Inspect failures and retry

```bash
.venv/bin/python database.py list --status failed
.venv/bin/python database.py retry --id 3
# Or reset every failed entry:
.venv/bin/python database.py retry
# With Anki open, run now, or wait for the next Anki startup:
.venv/bin/python worker.py
```

Failures are retried automatically on subsequent startups up to
`VOCAB_MAX_ATTEMPTS` (default 3). There is no timer/backoff loop or repeated retry
of the same failed job during a single run. `retry` resets its budget. An unavailable
AnkiConnect/configuration preflight does not consume job attempts. Invalid batch
output fails that batch without importing any of its generated cards; separate
successful imports remain imported.

Each created note gets a deterministic `vocab_key::<sha256>` tag. Before import the
worker checks that identity and normalized existing fronts in the selected note
type across all decks. It also disallows Anki's native first-field duplicates.
If `addNote` succeeds but its response or the queue update is lost, a retry finds
the tagged note, stores its ID, and avoids a second import. Existing equivalent
fronts are reused without changing them. This compares spelling/case/whitespace,
not semantic equivalence: different inflections/spellings are separate words.
Preserve the identity tag when editing imported notes; deleting both identity and
changing the front removes the information needed to recognize them on recovery.

Worker output is JSON lines. Startup logs go to
`~/.local/state/anki-vocabulary/worker.log`; direct CLI output goes to stderr.
Rotate/remove that log periodically if desired. Empty startup is normal.
`ollama ... not installed`: pull the configured model as the desktop user.
`AnkiConnect ...`: verify Anki is open, the add-on is installed, the profile matches,
and the endpoint/key are correct. A very slow machine may need higher
`VOCAB_GENERATION_TIMEOUT` or a smaller batch/model. Keep Anki open until processing
finishes; closing it may leave retryable failures.

Back up the queue with SQLite's backup API or with all project processes stopped;
copying only the database file while WAL writes are active can omit recent data.

## 10. Optional WordNet grounding

The `Grounding` protocol keeps dictionary hints modular. Nothing is downloaded by
the application. To opt in, install the extra and fetch the corpus explicitly:

```bash
.venv/bin/python -m pip install -e '.[wordnet]'
.venv/bin/python -m nltk.downloader wordnet
```

Set `VOCAB_WORDNET=true`. Missing dependencies/corpus then produce an actionable
failed-job message. WordNet hints support English definitions; the local model still
provides Italian translations. Model output is validated for structure and identity,
but cannot be automatically guaranteed factually correct. There is no fine-tuning.

## Development and verification

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Tests use temporary real SQLite databases, mock service failures, loopback fake HTTP
services, and a fake executable to test the real owned-process lifecycle and Linux
parent-death cleanup. They cover valid/invalid capture, fallback, concurrent
duplicates, migrations/transactions, database failure, structured-output rejection,
missing models, Ollama reuse/start/stop, unavailable AnkiConnect, duplicate notes,
failed/ambiguous imports, interruption/retries, persistent sync requests, startup
add-on launch, and the complete HTTP pipeline. They require no model downloads,
running Anki/Ollama, Wayland session, or AnkiWeb account. Real model quality and the
desktop/AnkiWeb integration require the manual smoke test above.

## Project files

| File/directory | Responsibility |
| --- | --- |
| `capture.py` | Clipboard, normalization, queue insertion, notification |
| `database.py`, `migrations/` | SQLite queue, migrations, inspection/retry CLI |
| `config.py`, `.env.example` | Validated local configuration |
| `generator.py`, `ollama_process.py` | Structured cards and temporary Ollama lifecycle |
| `grounding.py` | Optional WordNet adapter |
| `anki_client.py`, `http_client.py` | AnkiConnect, duplicate checks, local JSON HTTP |
| `worker.py` | Locking, batching, recovery, imports, sync requests |
| `anki_addon/` | Nonblocking `profile_did_open` integration |
| `log_utils.py` | JSON logging |
| `examples/niri.kdl` | Example Mod+Shift+A binding |
| `tests/` | Automated service-isolated checks |

Console commands `vocab-capture`, `vocab-worker`, and `vocab-queue` are also installed
in the virtual environment. Run them by absolute path from desktop shortcuts.
