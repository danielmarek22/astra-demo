# Validation

## Agent runtime refresh — 2026-11-18

- The Python regressions and seven JavaScript test files passed. Critical lint,
  the existing strict type-check scope, and the strict MkDocs build passed.
- The standalone launcher used cached local embeddings, real SQLite and vector
  stores, and loopback model and search fixtures. A turn searched and read the
  seeded history from a new conversation. A web-assisted turn replayed the latest
  native thinking, streamed a source link, and restored the same committed answer.
- A local MCP echo server was added, connected, and refreshed through the demo API.
  Its reviewed tool ran exactly once after approval in the turn stream. Reset
  restored launch settings and rejected requests carrying the old workspace ID.
- Headless Firefox loaded the MCP tab, saved a server, enabled the integration,
  and restored the sample workspace at a 390-pixel viewport.
- Live Ollama inference, hosted web services, and the isolated reader worker were
  outside these smoke checks. The tests use local protocol fixtures for those
  integration boundaries.

## Standalone and media migration — 2026-10-15

The standalone demo and speech/screen/shell migration were implemented on
`codex/bootstrap-demo`. The implementation was committed on 2026-10-19, and public
`main` is prepared as one fresh demo snapshot without inherited Astra commits.
The checks below record the local demo environment and restored fictional samples.

### Repository independence

- Demo Git/common directory: `/home/bsh/astra-demo/.git`.
- Original Git/common directory: `/home/bsh/local_ai_assistant/.git`.
- No object alternates, worktree sharing, source symlinks or shared tracked-file inodes.
- Demo branch: `codex/bootstrap-demo`; original branch: `dev`.
- Original HEAD, all refs, remotes, working-tree status, symbolic branch and all
  **300 tracked file hashes** match the pre-migration snapshot.
- Base shell executor/integration, Whisper adapter/factory, TTS interface/delivery/
  sentence splitter, audio-input and attachment utilities match the recorded source.
- Upstream pushing remains disabled. The public snapshot preserves the source license
  and provenance without publishing the original commit history; the development
  branch remains local. Historical validation statements below describe their check dates.

### Deterministic checks

| Check | Result |
| --- | --- |
| Python unit/integration suite, demo Python 3.12 environment | **584 passed** |
| Browser module tests | **6 test files passed** |
| Critical Python lint (`E9,F63,F7,F82`) | Passed |
| Existing strict type-check scope | Passed, 4 source files |
| MkDocs strict build | Passed |
| Installed dependency consistency | Passed |
| Whitespace/diff check | Passed |

The suite covers the original core services plus restored speech delivery, text
splitting, audio input, synthesis timeout and shell regressions. Added integration
checks exercise real SQLite/service composition with controlled model/vector/speech
substitutes: image validation/storage/retrieval, speech failure preserving committed
answers, editable transcription without a turn, reset during speech/transcription,
approval identity/expiry/disconnection, duplicate approval rejection and the original
executor's command flags/timeout. Browser tests preserve original recording/playback/
attachment regressions and add stop/cancellation/late-permission cleanup checks.

The HTTP test client required local thread/event-loop communication outside the tool
sandbox. Starlette emits an informational httpx TestClient deprecation warning.

### Live speech, image and shell checks

All migration observations used this demo's independent environment and server.
No original Astra server was launched or modified.

- The new Kokoro adapter generated **24 kHz PCM WAV** using stock English `af_heart`.
  The unchanged Whisper `small` CPU/INT8/VAD backend transcribed the generated phrase:
  “Hello, I can help you with your computer, remember decisions, and explain what I
  am doing.” This was controlled synthetic audio, not a microphone recording.
- The live `/api/transcribe` endpoint returned that English transcript in **4.41 s**
  including first engine setup in the running server.
- The live committed-answer speech endpoint returned **666,044 bytes**, `audio/wav`,
  in **7.28 s**, including first Kokoro setup in that server. These are single smoke
  timings, not benchmarks or latency promises.
- A real native **`shell__execute`** call requested approval for exactly
  `printf ASTRA_DEMO_SHELL_OK`. The HTTP reply approved that request and the persisted
  tool trace contained the actual successful output. No other commands were approved.
- A generated image contained a **blue circle** with no descriptive label. The real
  model answered “The filled shape in the image is blue and it is a circle.” No
  image-unavailable event occurred. Retrieving the saved image returned identical bytes.
- Reset changed generation, made the old image URL return **409**, and restored
  exactly **three sample conversations**.

The speech setup caught a decoder incompatibility: faster-whisper 1.2.1 calls an
`av.open` argument removed in PyAV 19. The demo pins compatible `av==16.1.0` instead
of modifying the migrated Whisper implementation. See the
[upstream fix](https://github.com/SYSTRAN/faster-whisper/pull/1495).
Kokoro downloaded its official English spaCy model on first use. Ordinary English
synthesis passed without a system espeak install; unusual pronunciation fallbacks
have not been validated.

### Browser validation boundary

Current browser automation was blocked by the browser URL policy when binding the
localhost tab. No workaround was attempted. The new controls therefore have not had
a live visual review, microphone permission test, screen chooser test, or audible
browser-playback/hidden-tab check. Passing synthesis, HTTP delivery and browser module
tests does not establish audible playback in the user's browser.

Earlier bootstrap checks verified text/memory recall and a native `memory__write`,
then restored samples. The previous text-only layout was inspected at 1280×720 and
390×844.

### Limits and deferred work

The shell implementation deliberately retains Astra's host execution policy. It is
not a filesystem sandbox: some read commands run without approval, approved commands
have the server user's host access, output is not byte-bounded, and the timeout does
not explicitly terminate all descendants. Reset clears managed demo state, not host
command effects. Shell hardening is deferred to the main project for later migration.

Media engines load lazily and may need network downloads on first use. The original
TTS worker bounds queue/deadline and quarantines a stalled engine; it cannot forcibly
stop a native synthesis thread. Timed-out recognition can finish in its owned worker;
its result is discarded after reset and overlapping recognition is rejected.

Live model-driven belief mutation and optional SearXNG search were not exercised.
Only one server worker is supported. Logs remain under `.demo-state/logs` across resets.
Further layout/design work, Relay, Planner and application hosting remain separate work.

## Frontend palette pass — 2026-10-19

The demo now follows Astra’s near-black background, white text and original `#00c3ff`
cyan accent. Shared CSS variables cover navigation, conversation, Knowledge, speech/
capture controls and command approval. A simple angular A replaces the starburst in
the sidebar, welcome screen and favicon. This pass also returns large headings to
the interface’s sans-serif family. Behavior and service composition are unchanged.

All six browser-module test files pass. Theme variable references and the SVG parse
were checked, and the principal text/button color pairs exceed 4.5:1 contrast
(the weakest checked pair is secondary text on the raised surface at 5.06:1).
Whitespace checks pass. The original Astra stylesheet checksum is unchanged.
No live visual review is claimed; the previously recorded browser-policy limitation
still applies. Refresh the demo page to load the updated static assets.
