# Runtime and data ownership

`main.py` launches one loopback HTTP server on port 8001. `app/server.py` constructs
`DemoRuntime`, which owns a disposable runtime generation. There is one demo factory;
no selector can switch to the original Personal runtime.

## Retained services

`app/core/orchestrator_factory.py` composes the existing Ollama client, SQLite stores,
Chroma collections, conversation orchestration, history summarization, semantic and
episodic retrieval, belief context, and validated native tools. The default tool set
contains memory writes, eligible belief updates, and the existing shell integration. Optional SearXNG web search uses
the retained web implementation when explicitly configured.

Speech uses the original Whisper adapter, original AudioDelivery queue, original
browser recording/playback modules, and a new Kokoro TTS adapter. Engines load lazily
and remain owned by the application across resets. Screen sharing uses the original
frame-capture functions and attachment pipeline, triggered only by explicit Send.
Camera, avatar, Minecraft, notebook and autonomous events remain absent.

## State layout

Each generation lives at `.demo-state/<random-generation-id>/` with its own SQLite
database, vector indexes, image upload root, ownership marker and completed seed
manifest. The source fixtures are reviewable JSON in `fixtures/examples.json`.
SQLite remains canonical; indexes are derived through the production store APIs.

All fixture identities and projects are fictional. Beliefs are populated through the
same evidence and source checks used by live tools. Corrections update a belief's
current row and increment its revision; the original reports remain in conversation
history. The current schema does not archive every historical belief value.

No seed operation asks the language model to generate text. Embedding initialization
and seed indexing can download/run the configured local embedding model.

The web server serves only `static/`. It never mounts `.demo-state`, configuration,
or the original checkout. The browser uses relative HTTP URLs, and its selected
conversation is stored under a demo-specific, generation-aware session key.

## Reset and concurrency

One shared gate serializes access across tabs, conversations, knowledge reads and
reset. A turn owns the gate until all persistence and post-processing finish. An
accepted turn continues after its browser disconnects. Reset owns its worker even
if its initiating browser disconnects. Shutdown waits for admitted workers before
closing stores.

Reset prepares a replacement generation and its indexes before closing the old
runtime. A preparation error retains the old workspace. Once activation succeeds,
closed old state is removed. An activation error makes the workspace unavailable.
Browser mutations carry the expected generation ID, so late requests cannot change
the replacement workspace. Logs remain under `.demo-state/logs` across resets.

Managed state paths reject parent traversal, external roots and symlinks. Deletion
requires a generation directory with its own matching ownership marker and never
accepts a browser-supplied filesystem path. A local user who deliberately changes the
source or filesystem still controls their machine; repository separation is not an
operating-system sandbox.

Multiple server workers are unsupported. Only the single-process launcher is part
of the demo's lifecycle contract.

## Media and approval transport

`POST /api/transcribe` accepts bounded audio (8 MiB), runs the migrated Whisper
backend and returns text for review. It does not create a chat turn. `POST
/api/sessions/{id}/speech` synthesizes a selected committed assistant message after
the original speech-text filter. Tool traces/thinking are not passed as speech.
Audio is prepared by the original bounded AudioDelivery queue outside the chat gate;
its deadline/quarantine behavior remains intact. Temporary files live in managed
`.demo-state/speech-staging` and are removed after delivery or abandoned-job cleanup.
There is no static mount of generated media. Both endpoints reject results from an
old generation. Browser session/reset changes stop media and reject late callbacks.

Image input reuses original validation (8 images, 10 MiB aggregate, dimension/pixel
limits), SQLite attachment persistence, and the actual multimodal context. Image GET
routes validate generation and stored-file ownership; raw storage paths are not exposed.
No separate vision-summary model call is added. Unsupported model fallback is reported.

A turn waiting for shell approval retains the chat gate. Its HTTP approval-response
route deliberately does not acquire that gate. The small broker binds one request ID
to generation, conversation and originating page, enforces the existing configured
approval timeout, and denies on disconnect. The base executor still selects which
commands need approval and applies its 15-second execution timeout.

Shell processes retain host access and can read/change files beyond demo state under
the original policy. Demo state ownership and repository independence do not constrain
shell commands. Reset cannot undo their host effects. An OS execution boundary,
output ceiling and descendant-process cleanup are deferred to main-project work.
