---
title: Documentation guide
description: Build and maintain Astra's generated documentation site.
---

# Documentation guide

The Markdown under `docs/` is canonical for architecture and development guidance. The generated `site/` directory is disposable output.

## Local preview

```bash
.venv/bin/python -m pip install -r requirements-docs.txt
.venv/bin/python -m mkdocs serve --dev-addr 127.0.0.1:8003
```

Open [the local preview](http://127.0.0.1:8003/). MkDocs watches Markdown,
configuration, and theme assets. The docs preview runs alongside the demo,
which uses port 8001 by default.

## Strict build

```bash
.venv/bin/python -m mkdocs build --strict
```

A strict build treats navigation and link warnings as failures. Run it before considering a documentation change complete.

## Writing conventions

- Explain responsibilities and invariants before listing implementation files.
- Describe the current code, not the intended future architecture, unless a section is explicitly marked planned.
- Prefer small Mermaid diagrams for flows, ownership, and lifecycle relationships.
- Link to another page rather than duplicating its explanation.
- Use admonitions sparingly for genuine warnings, invariants, and operational notes.
- Add stable pages to `nav` in `mkdocs.yml`.
- Explain important architectural reasoning on the page for the subsystem it affects.
