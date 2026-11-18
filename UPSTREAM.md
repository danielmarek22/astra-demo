# Astra source provenance

The demo starts from Astra commit `25377f547e60a0039e283c4cda162537e70e03a3`
(`dev`, 2026-10-15). This is a development baseline, not a released version.
The original MIT license and copyright remain in LICENSE.

This checkout has independent files and Git storage. Its `upstream` fetch remote
points to the original local checkout; pushing through that remote is disabled.
The public `main` branch starts with a fresh demo snapshot, without inherited Astra
commits. This preserves the source attribution above without publishing historical
configuration or deleted assets. The original license and retained vendor licenses
remain included.

The local `codex/bootstrap-demo` branch preserves the development history for reference.
The local `codex/publish-demo` branch tracks public `origin/main`. Publish only the
demo branch explicitly; do not push all branches or mirror this local repository.

## Intentional differences

- One standalone application; no Personal/Demo/Relay selector.
- Demo-owned disposable state, neutral config, fictional fixtures, and reset.
- On-demand English speech and explicit screen context; no avatar, Mindcraft,
  scratchpad or autonomous processing.
- Existing orchestration, tool authority, summaries, memories, and beliefs retained.
- Some internal contracts still support upstream features to keep future ports small.

## Speech, screen and shell migration — 2026-10-15

Source for that migration: the same baseline commit above.
The user selected migration of existing shell behavior, with hardening deferred to
the main project. This supersedes the earlier sandbox-first proposal.

| Area | Reused source | Necessary demo adaptation |
| --- | --- | --- |
| Shell | `app/tools/bash_execution.py`, `app/integrations/builtins.py`, typed approval contracts and core tool routing, unchanged | Register the existing integration; carry its callback over NDJSON/HTTP with tab/generation identity and timeout/disconnect denial |
| Recognition | `app/stt/factory.py`, `app/stt/whisper_engine.py`, unchanged | Lazy application ownership and raw-audio HTTP endpoint; return editable transcript |
| Synthesis | `app/tts/base.py`, `delivery.py`, `sentence_splitter.py`, existing engine adapters, unchanged | New Kokoro adapter plus factory/config branch; queued synthesis of committed answers with transient managed files |
| Speech text | Original server `_prepare_tts_text` and its regexes | Extracted to `app/tts/text.py` |
| Images | Existing attachment validation, storage, context building, Ollama image transport and browser attachment utilities | Original batch validator extracted to `app/http/message_input.py`; generation-aware image routes |
| Screen | Original `captureVideoFrame` and `stopStream` functions | Small lifecycle owner and explicit per-send preview/inclusion controls |
| Browser audio | Original AudioManager and MicrophoneRecorder | Stop/cancel and late-result guards for reset/session changes; existing playback recovery retained |
| Regressions | Original audio delivery, sentence splitter, audio input, synthesis timeout, audio playback, microphone and attachment tests | Additional HTTP approval, media reset, capture cleanup and cancellation cases |

The host shell classification, approval policy, execution flags, timeout, environment,
output behavior and filesystem access are unchanged. There is no new sandbox runner.
The decoder is pinned to `av==16.1.0` because faster-whisper 1.2.1 still calls an
argument removed in PyAV 19. This preserves the source backend instead of patching it.

## Updates

Review upstream fixes and features explicitly on a demo feature branch. Port coherent
sets including their dependencies, migrations and tests; use `cherry-pick -x` where
suitable. Cherry-picking copies a selected change without adding its source history
as parents. Avoid whole-tree merges with Astra or pushing local archive branches.
Record reviewed and imported revisions separately, then run retained regressions and
the demo acceptance checks.

| Reviewed through | Imported after baseline | Validation |
| --- | --- | --- |
| `25377f547e60a0039e283c4cda162537e70e03a3` | None | See docs/validation.md |
| `16e458d9fadc03fd4cb0cbc369966e7f9cccf577` | Selected changes from `9798140` through `16e458d` | See docs/validation.md |

## Agent runtime refresh — 2026-11-18

Ported the execution loop and atomic response handling (`2373c73`, `0199f1f`),
native thinking continuation (`72fa0b1`), prompt and reasoning controls
(`5409e30`, `431177a`, `145b53f`, `768949c`, `2af44bb`, `0f01fbe`),
structured web tools, the isolated reader and source footer (`9798140`,
`c88ee72`, `86e06a9`, `cc7f92b`), conversation history tools (`35d15be`),
and MCP (`260b675` through `16e458d`).

The demo composes these services in its own HTTP runtime. It retains its model,
local state and media adapters. The template uses a 32K model context window to
fit native continuations. The MCP tab writes a per-workspace copy of its settings.

The demo documentation reuses the main Astra site's MkDocs layout, diagram
viewer, and shared runtime guides. Startup, workspace lifecycle, and service
guides describe the standalone deployment. The docs preview uses port 8003 so
it can run alongside the main Astra docs preview.
