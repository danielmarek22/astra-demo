# Astra Demo

A standalone showcase of Astra’s local agent runtime, conversation history, memory,
beliefs, speech, and screen context. Continue fictional projects, inspect saved
knowledge, and restore the sample workspace whenever you want a clean start.

The demo runs independently from the original Astra checkout. Its database, vector
indexes, generated files, and logs live under this repository's `.demo-state/`.

## Run locally

Use Python 3.12 and an Ollama service with the configured model available.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001). The checked-in template works as
the default configuration. To select a different model or Ollama origin, copy
`app/config/assistant-template.yaml` to the ignored `app/config/assistant.yaml`
and edit its `llm` section. The model must support native tools for the memory
and belief demonstrations. The default is `gemma4:12b-it-qat` with a 32K context
window to accommodate tool schemas and native continuations.

```bash
.venv/bin/python main.py --port 8004 --config app/config/assistant.yaml
```

The first start may download the CPU embedding model `all-MiniLM-L6-v2`.
The first speech use downloads Kokoro weights, its stock `af_heart` voice, the
English spaCy model, and/or the Whisper `small` model. Speech runs on CPU by default.
English is the demo language. Standard English synthesis was verified without a
system espeak installation; uncommon pronunciation fallbacks can require `espeak-ng`.
Loading examples uses local embeddings but makes no text-generation calls. Model
inference begins when you send a message. Model and dependency downloads require
network access; ordinary conversation can run locally once those are available.

## Explore

- Continue the fictional community workshop plan.
- Start a new conversation and ask Astra to find the first decision about buying
  equipment in the observatory sample. The history tools can search and read earlier
  exchanges from eligible conversations in this demo run.
- Inspect a corrected beverage preference: the current belief is peppermint tea,
  at revision 2. The sample conversation contains the earlier coffee report.
- Open **Knowledge** for saved memories, belief records, and the current belief context.
- Use **Try again** on a failed turn to retry its original message.
- Select **Dictate**, then **Stop recording**. Review the transcript before Send.
- Select **Read aloud**, or enable **Speak replies · English**. Use Stop/Resume voice
  to control playback. Typed chat remains available if a speech engine fails.
- Select **Share screen**, choose the source in the browser, and review its preview.
  A fresh frame is included only when you Send with the inclusion checkbox selected.
  **Attach image** or paste a screenshot when screen sharing is unavailable. Images
  are kept with the conversation until reset/server shutdown. A vision-capable model
  is required; the demo reports when the backend falls back to text-only input.
- Ask for a shell action. Astra’s existing command policy allows certain read commands
  directly and requests approval for others. Review the exact command and approve
  once or deny. Missing, expired, or disconnected approval is denied.
- Use **Restore sample workspace** to discard demonstration changes.

## Optional tools

- **Web search and sources.** Enable `integrations.web.enabled` in your local YAML
  and point `base_url` to a SearXNG instance with JSON search enabled. An explicitly
  selected LangSearch provider sends queries to LangSearch. Web-assisted answers
  include links to the sources retrieved during the turn.
- **Page reading.** The optional local reader can open a public page and find exact
  text in its saved snapshot. See [reader setup](docs/architecture.md#optional-web-reader)
  for dependencies and model-context settings.
- **MCP.** Install with `.venv/bin/python -m pip install -r requirements-integrations.txt`.
  In **MCP**, enable the integration, add a server, connect, refresh its catalog, and
  review the tools to enable. Tools require approval by default. Settings changed in
  the tab last for this workspace. Restore or restart loads the launch YAML again.

The new execution loop is active on every native-tool turn. To try bounded reasoning
continuation or configure prompt limits, see [demo configuration](app/config/README.md).

Each server start prepares a fresh workspace. Browser refresh keeps the current
server run. Reset is shared across tabs, and stale requests are rejected until the
tab reloads its workspace. While a turn or reset is active, other operations return
a visible busy response. A disconnected browser does not stop an already accepted
turn; reopen its conversation to see the saved outcome.

The shell is migrated unchanged from Astra: it runs with the server user’s host
permissions, has a 15-second command timeout, and is **not a filesystem sandbox**.
Reset clears demo chat/images/indexes, not arbitrary effects of approved shell commands.
Shell isolation, output limits, and process-tree cleanup are deferred to upstream work.
Disable it with `integrations.shell.enabled: false` if needed.

Run a single server process/worker. The launcher binds to loopback on port 8001 by
default. Run the original Astra on another port if you want both open at once.

## Documentation site

Read the [published documentation](https://danielmarek22.github.io/astra-demo/).
The demo application runs locally with Ollama.

To preview the docs locally, run these commands from the repository root.

```bash
.venv/bin/python -m pip install -r requirements-docs.txt
.venv/bin/python -m mkdocs serve --dev-addr 127.0.0.1:8003
```

Open the [local documentation site](http://127.0.0.1:8003/) or read the
[getting-started guide](docs/getting-started.md).

## Development

```bash
.venv/bin/python -m pip install -r requirements-test.txt -r requirements-docs.txt
.venv/bin/python -m unittest discover -s tests -v --buffer
node --test tests/*.mjs
.venv/bin/python -m ruff check --select E9,F63,F7,F82 app tests main.py
.venv/bin/python -m mypy --config-file mypy.ini --python-version 3.12
.venv/bin/python -m mkdocs build --strict
```

The standalone runtime uses the shared Astra execution, history, web, and MCP
services with demo-owned HTTP, speech, and reset adapters.

See [architecture](docs/architecture.md), [development notes](docs/development.md),
[validation](docs/validation.md), and [upstream provenance](UPSTREAM.md).
The original MIT license and copyright remain in [LICENSE](LICENSE).
