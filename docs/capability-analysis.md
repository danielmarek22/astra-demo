# Voice, screen context and shell decisions

Date: 2026-10-15. Status: migration implemented; see [validation](validation.md).

## Accepted scope

The user confirmed English speech and explicitly chose to migrate the existing
shell functionality, postponing its improvement to the main project. The instruction
is to reuse the base implementations wherever possible. This decision supersedes the
earlier sandbox-first proposal; that work remains a future upstream improvement.

| Capability | Current demo | Deferred |
| --- | --- | --- |
| Speech input | Migrated faster-whisper, explicit record/stop, editable transcript before Send | Always-listening and wake words |
| Speech output | New Kokoro adapter using the original TTS interface and delivery queue; stock English `af_heart`, read-aloud, opt-in replies, stop/resume | Cloning, custom voices and avatar lip-sync |
| Screen context | Original capture/resize/JPEG functions and image pipeline; browser chooser, preview, fresh frame at Send, screenshot upload/paste fallback | Background monitoring, vision events and desktop clicking/typing |
| Shell | Original native tool, executor, approval rules and command timeout; HTTP approval transport | Filesystem sandbox, output ceiling, environment restriction and process-tree cleanup |
| Existing core | Conversation, memory, beliefs, summaries, retrieval, inspection and optional web search | Planner, Relay, notebook, Mindcraft and background autonomy |

Minimal controls support testing these capabilities. On 2026-10-19 the demo adopted
Astra’s black, white and cyan palette and a simple angular A mark. Further layout
design can build on that foundation.

## Migration boundary

The repository-root `UPSTREAM.md` records the exact source map and baseline revision.
Whisper, AudioDelivery, sentence splitting, image utilities, shell execution and typed
approval contracts come from that baseline. Browser playback/recording retain their
original behavior with small cancellation guards. Only the missing Kokoro backend,
HTTP transport and lifecycle glue are new; no replacement agent or shell runner exists.

The original Whisper result’s legacy `avg_log_prob` name remains unchanged. Its value
is language probability, not transcription confidence, and the demo does not expose
it as confidence.

## Speech evidence and packaging

Kokoro’s official API emits 24 kHz audio using stock voices. The demo selects CPU
execution to avoid requiring shared GPU capacity with the LLM.
[Official Kokoro library](https://github.com/hexgrad/kokoro),
[model card](https://huggingface.co/hexgrad/Kokoro-82M),
[stock voices](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md).

Recognition retains the multilingual `small` CPU/INT8 backend. PyAV bundles audio
decoding libraries, so this STT route does not require a system FFmpeg executable.
[Official faster-whisper documentation](https://github.com/SYSTRAN/faster-whisper).
The pinned 1.2.1 backend uses an argument removed in PyAV 19; the compatible decoder
pin avoids rewriting it. [Upstream compatibility fix](https://github.com/SYSTRAN/faster-whisper/pull/1495).

The first invocation can download weights and Kokoro’s English spaCy model. Ordinary
English synthesis passed without a host `espeak-ng` install. Some pronunciation
fallbacks can require it; broader pronunciation coverage has not been established.
Speech generation, HTTP delivery, browser playback and microphone permission are
separate validation layers; see the recorded limits rather than assuming equivalence.

## Shell behavior intentionally preserved

The tool permits some read commands without approval; arguments do not have a demo
root restriction. Other commands require approval and use host `/bin/bash`. It has
an execution timeout, but no output ceiling or explicit descendant-tree termination.
It inherits the server user’s host permissions and environment. It is not an OS sandbox.

The new transport resolves only a matching, unexpired request from its originating
page/current generation. Denial, timeout, absent client identity or disconnect before
approval means no execution. Reset reports Busy during an admitted turn. Reset clears
managed records and images; it cannot undo arbitrary command effects on the host.

Future hardening should be developed in the main project, reviewed there, then ported
with its prerequisites and regression tests. The original checkout remains untouched
by this demo implementation.
