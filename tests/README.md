# Demo tests

Run `.venv/bin/python -m unittest discover -s tests -v --buffer` and
`node --test tests/*.mjs` from the repository root.

`test_demo_runtime.py` covers actual SQLite/service composition with deterministic
model and index substitutes: fictional seed preparation, revisioned belief correction,
reset isolation, failure, busy operations, stale generations, HTTP turn persistence, migrated shell approval transport, images, speech failure
and rejection of late media after reset.
Retained core suites cover authority, context, summaries, canonical/vector consistency
and the internal upstream contracts still present in this project.

Browser tests cover event framing and UTF-8, interrupted requests, busy responses,
the reachable local module graph, original playback recovery/recording/attachment
regressions, and added cancellation/capture cleanup. Avatar and retired original-server
tests remain removed.

See `docs/validation.md` for actual results and live-check limits.
