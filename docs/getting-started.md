---
title: Getting started
description: Install Astra Demo, start the local server, and explore the sample workspace.
---

# Getting started

## Prerequisites

Use Python 3.12 and a running Ollama service. The demo template uses
`gemma4:12b-it-qat` with a 32K context window. Choose a model that supports native
tools for history and belief workflows, and vision for attached images.

## Install runtime dependencies

```bash
git clone https://github.com/danielmarek22/astra-demo.git
cd astra-demo
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
ollama pull gemma4:12b-it-qat
```

The first start may download the local CPU embedding model. Speech weights load
the first time you use [dictation or playback](services/speech.md).

## Run the demo

```bash
.venv/bin/python main.py
```

Open [Astra Demo](http://127.0.0.1:8001/). The server creates a fresh workspace
from three fictional examples. Loading the examples uses local embeddings.
Text generation begins when you send a message.

- Continue the workshop plan or ask about the observatory decision from a new
  conversation.
- Open **Knowledge** to inspect saved memories, belief records, and the current
  belief context.
- Select **Share screen** or **Attach image** to include an image with your next
  message.
- Select **Restore sample workspace** to rebuild the examples. The new workspace
  replaces demo history, images, indexes, and changes made in the MCP tab.

## Configure the demo

The checked-in `app/config/assistant-template.yaml` is the default. Copy it to
`app/config/assistant.yaml` for local settings, or pass a configuration file
explicitly.

```bash
.venv/bin/python main.py --port 8004 --config app/config/assistant.yaml
```

| Setting | Purpose |
| --- | --- |
| `llm.host`, `llm.model`, `llm.generation` | Ollama origin, model, and generation options |
| `execution.default_profile`, `execution.profiles` | Standard and deep execution limits |
| `reasoning.enabled` | Bounded continuation with a turn-local workspace |
| `context.prompt_budget` | Opt-in model context budget and output and image reserves |
| `integrations.history` | On-demand history and eligible archive access |
| `integrations.web`, `integrations.mcp` | Optional web and external tool integrations |
| `stt.enabled`, `tts.enabled` | Dictation and playback controls |

Run one demo process. The default server binds to loopback. A separate demo port
can be useful when another local app already uses port 8001.

## Optional services

- [Web access](architecture/web-access.md) covers SearXNG, LangSearch, and the
  optional isolated reader.
- [MCP connections](services/mcp.md) covers installing the client, connecting a
  server, and reviewing tools.
- [Service catalog](services/index.md) lists runtime dependencies and defaults.

## Preview these docs

Run MkDocs in a separate terminal and open the
[documentation site](http://127.0.0.1:8003/).

```bash
.venv/bin/python -m pip install -r requirements-docs.txt
.venv/bin/python -m mkdocs serve --dev-addr 127.0.0.1:8003
```
