---
title: Web access
description: Structured search, isolated page snapshots, literal find, and source links in Astra Demo.
---

# Web access

Astra Demo starts with web tools disabled. Search discovers URLs and snippets.
The optional page reader fetches public pages and returns excerpts from retained
snapshots.

| Tool | Purpose | Result |
| --- | --- | --- |
| `web__search` | Discover sources when you lack a suitable URL | Bounded titles, URLs, snippets, and source IDs |
| `web__open` | Read a public URL or a retained source | An excerpt and navigation for the retained snapshot |
| `web__find` | Locate a literal phrase in an opened source | Matching line windows across the retained snapshot |

## Enable search

Configure a running SearXNG instance with JSON search enabled and add these
settings to your local demo configuration.

```yaml
integrations:
  web:
    enabled: true
    provider: searxng
    base_url: http://localhost:8080
```

Each search record contains its source ID, title, URL, snippet, retrieval time,
and truncation flag. Publication dates retain provider provenance. Treat snippets
as discovery evidence when deciding which pages to open.

### Optional LangSearch discovery

To use LangSearch, set `integrations.web.provider: langsearch` and
`integrations.web.langsearch_api_key_env: LANGSEARCH_API_KEY`, then provide the
key through the demo process's environment. Search queries go to the hosted
provider. The adapter uses the same structured search contract and the configured
result limits. The page reader still runs locally when enabled.

### Retrieved sources beneath an answer

Native user turns collect title/URL metadata from actual `web__search`, `web__open`
and `web__find` results in a fresh `RetrievedSources` collector. Successful search
records are labeled **Search result**; successful opened-page/find metadata is
labeled **Page opened**, meaning page evidence was retrieved rather than the whole
page necessarily being read. URLs from model prose, page navigation, unexecuted
multi-call batches and other conversations are not collected. Failed/denied tools
never contribute links. A web attempt with no usable source URLs produces an
explicit **No live sources were retrieved for this turn** notice.

The backend adds a Markdown **Sources retrieved** footer after generation.
It records retrieval activity. Claim citations require checking the source text. It may include sources later reduced by the prompt
guard. Existing model-written links remain unchanged. Normalized URLs are
deduplicated; page retrieval upgrades a search entry, and redirects replace the
discovery URL with the actual opened destination. Opened pages have priority over
search-only entries. The collector holds at most eight records, caps titles at
120 characters and input URLs at 2,048 characters, and bounds the rendered footer
to 6,000 characters with an omission notice when needed. Titles are escaped and
destinations are encoded after HTTP(S)/credential/control-character validation.

The footer becomes part of the committed reply and appears again when the
conversation is reopened.

## Isolated reader and snapshots

`web/reader.py` owns one lazy event-loop worker and one serial reader slot. The
total deadline includes queue wait, DNS/HTTP collection, browser startup and
extraction. A failed or timed-out operation kills the isolated worker; the next
operation starts a new one. Shutdown cancels active work, closes pipes, stops the
worker and joins its thread. Search remains independent of optional reader setup.

The browser runs inside Linux bubblewrap with private network, PID, IPC and UTS
namespaces. Its filesystem contains read-only system libraries, the selected
Python environment, Chromium binaries and the standalone worker script, plus a
private 64 MiB temporary filesystem. The project, private home, personal browser
profile and parent environment are not mounted or inherited. Chromium's own
sandbox stays enabled. Missing bubblewrap, unavailable namespace permissions,
missing dependencies or missing Chromium fail closed; there is no unsandboxed
fallback. The demo validation notes record the coverage for this port.

The browser has no external network route. Its permitted GET requests travel over
bounded stdin/stdout messages to `web/fetch.py`, not through a general-purpose
browser or the user's browser session. The parent fetcher:

- permits only credential-free public HTTP(S), on ports 80/443;
- resolves each request and redirect, rejects mixed/private/special address sets,
  and pins the connection to an approved numeric address while retaining the
  original Host header and TLS hostname verification;
- follows at most three redirects itself, never passing redirects to Chromium;
- does not forward page cookies, authorization or proxy environment settings;
- accepts only HTML/XHTML for documents and a small text/script/style/JSON set for
  supporting resources; PDF, images, media and downloads are not supported;
- requests identity encoding and rejects compressed responses rather than
  collecting an unbounded decompressed body;
- defaults to 2 MiB per response, 4 MiB across an operation, 32 requests and a
  30-second total operation deadline.

Route handlers abort extra top-level navigation, non-GET requests, unsupported
resources and WebSockets. Frames use the same broker validation. Service-worker
or auxiliary traffic missed by hooks still cannot leave the OS network namespace.
Only the parent can access the network, and its broker never accepts cookies or
arbitrary headers from the child. Contexts are recycled and explicitly closed
after every operation; cookies and local storage do not carry into the next read.
Extraction uses Crawl4AI raw Markdown without an extraction LLM or generated code.

The worker, browser and driver run in one transient systemd user scope. Before
importing Crawl4AI, the worker reports its cgroup membership and waits for the
parent to verify actual kernel limits: 1536 MiB memory, zero swap, group OOM kill,
100% CPU quota (one CPU) and 256 tasks by default. Missing systemd user-session
support, cgroup v2 controllers or verifiable limits fail closed. RSS sampling every
50 ms remains an auxiliary early-stop mechanism, not the hard boundary; summed
RSS can double-count shared pages. These controls do not constrain the parent
fetcher, Astra or Ollama/GPU memory. The reader waits for DOM
content plus 250 ms, not arbitrary late JavaScript completion. Heavy/delayed pages,
sites requiring compressed responses, authenticated content and anti-bot pages
may fail explicitly. Crawl4AI's anti-bot heuristics can also reject very short
legitimate pages; unsuccessful extraction is never accepted as page evidence.

`web/sources.py` retains only bounded in-memory evidence owned by the invoking
conversation. Search IDs resolve to retained URL references; a fetched `page_…`
ID identifies an immutable snapshot with retrieval time, final URL, raw Markdown
lines and bounded extracted links. Reopening a retained URL reuses its snapshot;
expired or evicted content must be fetched again and receives a new snapshot ID.
Snapshot line numbers are zero-based, with exclusive `end_line` and `next_line`
for paging. Literal find is Unicode-case-insensitive, not regex or semantic search.
Opening without `start_line` always returns the first window, including when a
URL has a `#fragment`; a fragment does not select a snapshot section. Use find
with the returned source ID for a specific passage, or open the snapshot ID with
`start_line: next_line` to continue reading. Tool descriptions and trusted routing
instructions explicitly explain this distinction and preserve source attribution
when comparing findings. Each invocation uses the turn’s normal tool allowance.

Tool descriptions lead with their intended use: search discovers sources when a
suitable URL is not known; open initially inspects a supplied URL and pages for
broad reading or surrounding context; find locates a specific fact, option, error
or section that is missing from the visible excerpt. Find explicitly searches the
entire cached snapshot, including lines open has not shown, without another page
download. Its parameter descriptions ask for an opened source ID and a distinctive
literal word/phrase. These are model-facing guidance changes, not forced dispatch
or a semantic-search feature; schemas, permissions and limits remain unchanged.

Defaults are a 10-minute TTL, 512 KiB extracted UTF-8 text and 10,000 lines per
document, 64 entries/4 MiB per conversation and 256 entries/16 MiB globally.
Byte accounting includes conservative serialized/object/line overhead, not just
Markdown. Per-conversation and global LRU eviction enforce their own caps; expiry
is checked on access. Expired-ID tombstones are bounded too. Open defaults to 40
lines; find returns at most five matching windows. Both cap the entire escaped
JSON observation at 8,000 characters, mark truncation and preserve whole URLs.
They never silently slice a source URL to fit metadata. Long individual lines
can be visibly clipped; find can locate a later passage without dumping the page.

Search references and snapshots are scoped to the invoking conversation.
Reset and shutdown clear the cache. Source links on committed replies can be
opened in the browser during the current workspace.

## Install and enable the reader

The reader needs Linux bubblewrap, a systemd user session with cgroup v2, and
Playwright Chromium in a separate Python environment.

```bash
python3.12 -m venv .venv-web
.venv-web/bin/python -m pip install -r requirements-web.txt
PLAYWRIGHT_BROWSERS_PATH="$PWD/.venv-web/browsers" .venv-web/bin/python -m playwright install chromium
.venv/bin/python -m pip install psutil==7.2.2
```

Enable `integrations.web.enabled`, `integrations.web.reader.enabled`, and
`context.prompt_budget.enabled` in your local YAML. In
`integrations.web.reader`, set `python_executable` and `browsers_path` to the
absolute paths for `.venv-web/bin/python` and `.venv-web/browsers`. Set
`llm.generation.num_ctx` explicitly to the context size for your model.

## Prompt budgets and recovery

The prompt guard reserves space for completion and estimates the input before
generation. It can drop optional history, older window text, and navigation
lists to fit a request. It preserves whole URLs and marks shortened evidence.
Current-turn find results and recent open windows take priority over older
excerpts. A protected latest native continuation can produce an explicit
context-size failure if it cannot fit.

See [Context construction](context-building.md#per-request-prompt-budget) for
the reduction order and [Runtime lifecycle](runtime.md#buffered-inference-and-recovery)
for the separate recovery request. The demo's [validation notes](../validation.md)
record the tested model and reader boundaries.
