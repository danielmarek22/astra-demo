# Milestone 1 — Separate Astra demo repository

Date: 2026-10-14

Status: historical planning record; standalone implementation started on 2026-10-15.
See [current architecture](architecture.md) and [validation](validation.md) for the delivered scope.

This replaces the runtime-profile proposal. The filename is retained so existing links continue to work. The user now prefers an independent demo project that periodically receives selected Astra features.

## Architecture decision

Build a small downstream repository derived from a known Astra commit, with its own startup, configuration, text frontend, data, tests, and release cadence. Astra remains the development home for the assistant and its richer capabilities. No Personal/Demo runtime selector or three-profile composition layer is required in Astra for this delivery.

The demo uses real conversation, storage, retrieval, memory, belief, and selected tool implementations, with fictional sample data. It does not promise immediate feature parity with Astra.

The benefit is simpler composition in both products. The cost is maintaining a downstream copy of shared code and adapting updates where the projects diverge. Repository separation makes that tradeoff explicit; it does not eliminate coupling or automatically isolate files on the same machine.

## Repository and Git workflow

Proposed repository name: `astra-demo`. Name, location, hosting, and visibility remain to be chosen before creation/publication.

Use Astra as an `upstream` remote and the demo repository as `origin`. Prefer a clean checkout preserving ancestry from an explicitly selected source commit, followed by focused demo changes. Ancestry helps comparisons and fix ports. Retain applicable licensing and attribution.

Before publishing preserved history, review what historical source/assets it would expose. A clean checkout excludes ignored local files but does not remove tracked historical material. If preserving history is inappropriate, initialize from a reviewed source snapshot and record its commit instead; shared ancestry is useful but not mandatory.

Import reviewed source, tests, dependencies, and relevant documentation from that revision. Do not copy the working directory wholesale. Private config, databases, vectors, uploads, logs, model/voice files, and local review/output directories are excluded.

Use demo `main` as its stable branch and short feature branches such as `codex/bootstrap-demo` and `codex/sync-astra-<revision>`. Demo-specific changes are not merged back into Astra. A generally useful fix can be made independently in Astra and then ported to the demo.

Current local Git inspection found:

- The checkout is now on `codex/m1-runtime-profiles` at `25377f5`; `dev` points to the same commit. Leave this existing branch alone during planning.
- Astra's local `main` points to `93053cd`, an older snapshot than `dev`. “Main project” and the branch named `main` are not interchangeable; choose the initial source revision explicitly.
- Prefer a validated baseline promoted through Astra's normal release process. If starting from current `dev`, record that exact revision and its validation instead of silently treating it as released `main`.
- Remote freshness has not been checked; these are local branch pointers.

The independent project needs a separate checkout, environment, and launch instructions. An Astra worktree alone would still belong to the same repository. No repository creation or publication is part of this planning step.

## Periodic upstream updates

Use deliberate feature/fix ports, normally from Astra's stable `main` or selected releases. Avoid automatic full-tree merges: upstream startup or presentation changes could restore the dependencies removed from the demo.

Maintain `UPSTREAM.md` with the bootstrap revision, last reviewed upstream revision, imported feature/fix commits, intentional differences, and outstanding relevant fixes. Distinguish “reviewed through revision X” from “contains everything through X.”

For each refresh:

1. Review upstream changes since the recorded review, including correctness fixes even when no new feature is wanted.
2. Select a coherent feature/fix set, including schema migrations, dependency changes, prerequisite commits, and regression tests.
3. Port on a demo update branch. Use `cherry-pick -x` for suitable focused commits; adapt coupled changes manually and record source revisions.
4. Preserve text startup, selected tools, independent paths, and the sample-data contract. Rebuild seeds through the current schema and reconcile derived indexes.
5. Run focused tests and demo acceptance, review the diff, then merge into demo `main`.

Keep unchanged core module names and layout where practical. Concentrate demo differences in composition, frontend, fixtures, config, and packaging. Avoid broad style refactors in copied core code.

Do not extract a shared `astra-core` package initially. Reconsider that boundary if repeated ports require extensive work in the same core modules. An occasional-update demo can accept some duplication; a frequently synchronized second product may warrant a shared package later.

## Demo composition

| Keep | Remove from the demo delivery |
| --- | --- |
| Text generation, streaming, failures and retries | Three.js, VRM/FBX assets, avatar manager and avatar prompt instructions |
| Conversation history, summaries, memory/retrieval | TTS/STT initialization, microphone/camera/screen capture, speech queues |
| Beliefs and useful knowledge inspection | Mindcraft and unrelated integration startup |
| Explicitly selected low-risk tools | Shell execution and autonomous event processing |
| Fictional examples, blank chat, reset | Private identities, prompts, config and data |

Propose omitting scratchpad from initial composition to keep the first showcase small; it remains an optional feature choice. Web search is optional and requires its configured service. Retain supporting internal types/stores if existing core APIs require them; removing unused product features should not become an unrelated core rewrite.

Use one demo application factory and one config schema. Unsupported capabilities are absent from composition and routes. Reuse existing optional-dependency boundaries and make narrow adaptations for text delivery. There is no selector that can switch into Personal operation.

## Configuration and state

Create the demo's checked-in template from Astra's template, with a neutral prompt, fictional identities, model/endpoint settings, and an explicit supported tool set. Astra's current template enables shell and speech input, so it needs changes. A local copy customizes supported settings without loading Astra's private `assistant.yaml`.

All writable paths resolve inside demo-owned state: SQLite, vectors, logs/traces, and any generated files. Source paths in `app/paths.py`, the factory, server, and maintenance commands must consistently resolve to the new project's state. This needs a small storage boundary, not runtime profile switching.

No absolute references or fallback paths to the Astra checkout are allowed. Cleanup accepts no arbitrary browser-supplied path and refuses targets outside the managed root, including symlink escapes. Read-only model caches and the Ollama service may be shared; requests still carry only demo context.

The browser uses same-origin HTTP/WebSocket connections and demo-specific storage keys. Astra currently hardcodes WebSocket port 8000 and uses common browser history/preference keys. Copying these unchanged could connect the demo to Astra or restore unrelated state. Run concurrent applications on separate ports.

Static serving contains only demo assets and permitted demo-owned files. Verify copied URLs, absolute paths, and filesystem links; repository separation alone does not enforce that boundary.

## Text frontend

Replace the entry point with a simple responsive interface: streamed chat, examples/history, new chat, connection/error status, and reset. Reuse `NetworkClient`, `MessageRenderer`, and `ChatHistoryStore` where useful. The existing `UIManager` initializes media controls, so reuse/extract only the text components needed.

No avatar/media imports, asset requests, permission prompts, or render loop should remain. Retain acknowledgements, supported retries, history restoration, and knowledge inspection where retained services support it. Text completion must not construct or wait for speech services.

## Fictional seed and reset

Track small, versioned text fixtures and generate the database locally using the current schema and service APIs. Do not ship a copied Personal database or binary Chroma state.

Suggested examples:

1. A fictional community workshop planning conversation, ready to continue.
2. Another fictional project conversation plus an attributed memory record, demonstrating recall.
3. A reported preference and correction with corresponding belief records, demonstrating attribution and effective state.

Label examples as sample data. Seeded assistant text is not evidence of a live inference or successful tool call. Preserve valid participant/message references and summary checkpoints; derive vectors from canonical records. Seeding adds no text-generation calls, although local embeddings may be needed. Record seed version/completion and prevent duplicate insertion or serving partially prepared state.

Proposed lifetime: a fresh seeded workspace on each server start, retained for that run. Browser refresh does not reset it. Reset restores the examples and removes demonstration changes; new blank chats are also available.

Reuse existing resource owners. Serialize reset across tabs and writers; return a visible busy result during active work. Prepare replacement state, close old resources under the reset boundary, activate a new runtime generation, and reject stale requests/callbacks. Preparation failure retains the old usable state; activation failure produces a visible unavailable state. Delete only closed, owned demo state. Measure reset duration before promising a specific latency.

## Implementation increments

| Step | Deliverable | Suggested commit message |
| --- | --- | --- |
| 1 | Select/review source baseline; create independent repository; record provenance/update policy and configure its own checks. | `chore: bootstrap Astra demo from reviewed upstream source` |
| 2 | One demo startup/config/storage boundary, media-free text streaming, selected tools and matching routes. | `feat: compose a standalone text-only demo runtime` |
| 3 | Text frontend, correct origin/storage handling, history, errors and retained knowledge views. | `feat: add the demo chat interface` |
| 4 | Versioned fixtures, fresh startup, reliable reset and regression coverage. | `feat: add seeded examples and demo reset` |
| 5 | Remove unused assets/dependencies/docs after inspecting imports; verify clean installation/live showcase; document launch and refresh. | `docs: document and validate the standalone demo` |

Keep increments independently reviewable. Repository creation and publication are separate actions. Before publication, review the included tree/history, licenses, provenance, and absence of private artifacts.

## Acceptance and validation

- Clean installation/launch works without Personal config, avatar assets, or speech engines. Adjust dependencies from the actual retained import graph; not every ML dependency is speech-only.
- Retained-core regressions pass, including authorization, summary checkpoints, and terminal-state handling where retained.
- Live browser loads no Three.js/VRM/FBX/audio assets, requests no media permissions, and connects to its own server on a different port.
- Examples restore and continue; actual recall and selected tool results are observable. Distinguish deterministic tests from empirical model outcomes.
- Reset restores SQLite, vectors, in-memory state, and browser/session context consistently. Cover busy work, multiple tabs, failure, repeated reset, and late callbacks.
- Temporary external sentinel files/stores remain untouched and inaccessible through API, static serving, reset, or maintenance. Never run destructive tests on actual Personal data.
- Rehearse a small relevant upstream port to validate provenance recording, adaptation, and tests.
- Run retained Python/JavaScript suites, relevant lint/type checks, documentation checks, and whitespace checks. Perform live browser/model smoke checks and record limitations.

## Roadmap implications

The current roadmap and FI-01 still describe Personal/Demo/Relay runtime profiles. Once this revised proposal is adopted, update those documents to describe the separate demo and its maintenance model. Do not claim the old runtime-profile acceptance criteria were fulfilled.

Relay remains a separate architectural decision for Astra: it uses the avatar/media frontend intentionally omitted from this demo. Keep Relay as outstanding roadmap work and agree its placement separately. Do not implement it in the new demo as an automatic continuation. The M4 Planner and M5 execution sandbox remain outside this delivery.

## Original planning scope

Only this plan was revised. No repository, remote, branch, application code, config, data, or running service was created or changed by this planning step. Existing untracked files and the current branch were left in place. Implementation and live validation remain future work.
