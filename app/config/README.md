# Demo configuration

The server uses the checked-in `assistant-template.yaml` unless a local ignored
`assistant.yaml` exists, or `main.py --config` specifies a file.

Copy the complete template to customize the Ollama host/model. All template identities
and seed records are fictional. Native tools must be supported by the selected model.

The retained typed schema recognizes upstream configuration sections for easier ports.
The template enables shell, Whisper speech input, and stock English Kokoro output.
`tts.enabled` and `stt.enabled` disable speech independently. Demo composition rejects
enabled Mindcraft, scratchpad and autonomous event processing. Logs must resolve under this checkout's `.demo-state`.

The default `standard` execution profile inherits the existing model and
orchestrator limits. Set `execution.default_profile: deep` and add overrides under
`execution.profiles.deep` to change the inference budget, generation options, or
recovery limits. Set `reasoning.enabled: true` to expose the bounded continuation
tool and its turn-local workspace. Model thinking is configured independently
under `llm.thinking`.

The full prompt budget is opt-in under `context.prompt_budget`. Size its context
window and completion and media reserves for the configured model, particularly if
you use screen captures or multiple images. Native tool continuations also check
that protected context fits when the full budget is off.

History search and read are enabled in the template. They use eligible conversations
in the current demo workspace, including the fictional samples. The optional
`integrations.history.trusted_session_ids` and `allow_all_sessions` settings use
Astra's archive authorization checks.

Web and MCP start disabled. Web settings come from `integrations.web` and
`integrations.web.reader`. Launch-time MCP settings come from `integrations.mcp`.
The MCP tab edits a copy inside the active workspace, which is recreated on reset.
