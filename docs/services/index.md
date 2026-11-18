---
title: Service catalog
description: The local processes, models, and stores Astra depends on or can integrate with.
---

# Service catalog

The demo uses local models and stores, with optional services for web access and external tools.

| Service | Role | Required? | Default connection or location |
| --- | --- | --- | --- |
| Ollama | Text, tool-routing, and multimodal inference | Yes for the current backend | `http://localhost:11434` |
| SQLite | Canonical chats, summaries, beliefs, memories, and metadata | Built in | Local database file managed by Astra |
| Chroma | Semantic and episodic retrieval indexes | Used by memory features | Local persistent store |
| Faster-Whisper | Speech-to-text | When speech input is enabled | In-process model |
| Kokoro | English speech synthesis | When speech output is enabled | In-process model with stock English voice |
| SearXNG | Search results for Astra's web integration | Optional | `http://localhost:8080` |
| Crawl4AI reader | Isolated page snapshots for `web__open` and `web__find` | Optional, disabled by default | Separate Python environment and Chromium |
| MCP servers | Manually enabled external tools | Optional, disabled by default | Configured stdio command or Streamable HTTP endpoint |

## Typical startup order

1. Start Ollama and ensure the configured model is available.
2. Start SearXNG if the local web search provider is enabled.
3. Start [Astra Demo](../getting-started.md).
4. Open the browser UI and verify one plain text turn before testing media or tools.

Connect and refresh MCP servers in the **MCP** tab.

## Runbooks

- [Speech](speech.md) covers dictation, playback, and the local model defaults.
- [MCP connections](mcp.md) covers server setup and tool review.
- [Web access](../architecture/web-access.md) — understand and configure search and isolated page reading.

!!! note "Machine-local settings"
    Keep endpoints, model names, reference audio paths, ports, and personal profiles in `app/config/assistant.yaml` or the external service's local configuration. The committed template should remain a safe example.
