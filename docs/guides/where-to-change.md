---
title: Where do I change...?
description: A task-first lookup for finding Astra's configuration, implementation, and tests.
---

# Where do I change...?

Start with the narrowest row matching your goal. Paths are relative to the repository root.

| I want to change... | Configuration or content | Implementation | Tests and notes |
| --- | --- | --- | --- |
| The model, endpoint, or generation settings | `app/config/assistant.yaml` → `llm` | `app/llm/ollama_stream.py` | `tests/test_http_retries.py`, orchestrator tests |
| What Astra sees in a prompt | `context`, `orchestrator`, and `beliefs` sections | `app/core/context_builder.py` | `tests/test_context_builder.py` |
| Prompt size and evidence retention | `context.prompt_budget`, `execution.profiles` | `app/core/prompt_budget.py`, `app/core/response_generator.py` | `tests/test_prompt_budget.py`, `tests/test_mcp_prompt_budget.py` |
| How restored chats regain context | `context.history_limit` | `app/memory/chat_history.py`, `app/core/context_builder.py`, session routes in `app/server.py` | `tests/test_chat_history.py`, `tests/test_demo_runtime.py` |
| Conversation summaries | `orchestrator.summary_trigger` | `app/memory/history_summarizer.py`, `app/memory/summary_store.py`, `app/core/turn_finalizer.py` | Context and orchestrator tests |
| Long-term memory retrieval | `context.injected_memory_limit`, `context.semantic_memory_*`, `context.episodic_memory_max_distance`, `integrations.memory` | `app/memory/retriever.py`, `app/memory/memory_store.py`, `app/memory/chat_history.py`, `app/storage/vector_store.py` | `tests/test_memory_store.py`, `tests/test_chat_history.py` |
| On-demand conversation history tools | `integrations.history.enabled`, `integrations.history.allow_all_sessions`, `integrations.history.trusted_session_ids`, `context.episodic_memory_max_distance` | `app/integrations/history.py`, `app/memory/chat_history.py` | `tests/test_history_integration.py`, [access scope and bounds](../architecture/memory.md#on-demand-conversation-history) |
| Image attachment handling | Composer attachment controls | `app/perception/attachments.py`, `app/http/message_input.py`, `app/server.py` | `tests/test_attachment_utils.mjs`, `tests/test_demo_runtime.py` |
| The system prompt or assistant name | `assistant` | Loaded through `app/config.py` | `tests/test_config.py` |
| Speech recognition | `stt` | `app/stt/factory.py`, `app/stt/whisper_engine.py` | Exercise a short microphone recording |
| Astra's voice | `tts` | `app/tts/factory.py`, `app/demo/speech.py` | [Speech](../services/speech.md) |
| Native inference, continuation, and recovery | `execution`, `reasoning` | `app/core/execution_loop.py`, `app/core/execution_state.py`, `app/core/task_workspace.py` | `tests/test_execution_loop.py`, `tests/test_execution_profiles.py`, `tests/test_reasoning_continuation.py` |
| Tool dispatch and authority | `integrations` | `app/core/tool_executor.py`, `app/integrations/registry.py` | `tests/test_tool_executor.py`, `tests/test_integration_registry.py` |
| MCP server settings and catalog review | MCP tab, `integrations.mcp` | `app/http/mcp.py`, `app/integrations/mcp_management.py`, `static/js/mcp-manager.mjs` | `tests/test_mcp_management.py`, `tests/test_mcp_manager.mjs`, [MCP connections](../services/mcp.md) |
| Turn streaming, request limits, and approval waits | `autonomy.approval_timeout_s` | `app/server.py`, `app/demo/approvals.py`, `static/js/demo-client.mjs` | `tests/test_demo_runtime.py`, `tests/test_demo_client.mjs` |
| Screen capture | Composer screen-share controls | `static/js/screen-capture.mjs` | `tests/test_screen_capture.mjs` |
| Web search and page reading | `integrations.web` | `app/tools/web_search.py`, `app/tools/web/`, `app/integrations/builtins.py` | `tests/test_web_search.py`, `tests/test_web_integration.py`, [web access](../architecture/web-access.md) |
| Shell command behavior | `integrations.shell` | `app/tools/bash_execution.py` | `tests/test_bash_execution.py` |
| Belief extraction or display | `beliefs` | `app/beliefs/`, `app/knowledge/` | `tests/test_beliefs.py`, `tests/test_belief_react.py`, `tests/test_knowledge.py` |
| Browser behavior or appearance | — | `static/` | Relevant `tests/*.mjs` files |
| Logging and traces | `logging` | `app/logging.py` | `tests/test_logging.py` |
| Demo startup or reset | `--port`, `--config`, and `fixtures/examples.json` | `main.py`, `app/demo/runtime.py`, `app/demo/seeds.py` | `tests/test_demo_runtime.py`, [Getting started](../getting-started.md) |
| Documentation navigation or theme | `mkdocs.yml` | `docs/`, `docs/assets/stylesheets/astra.css` | `.venv/bin/python -m mkdocs build --strict` |

## A safe change loop

1. Reproduce the behavior and identify the smallest owning boundary.
2. Add or locate a test close to that boundary.
3. Change the implementation without moving authority into model output or browser state.
4. Run the narrow test, then the broader suite if the boundary is shared.
5. Update this documentation when configuration meaning or system flow changed.

For how those boundaries connect, return to the [Project map](../project-map.md).
