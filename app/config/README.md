# Demo configuration

The server uses the checked-in `assistant-template.yaml` unless a local ignored
`assistant.yaml` exists, or `main.py --config` specifies a file.

Copy the complete template to customize the Ollama host/model. All template identities
and seed records are fictional. Native tools must be supported by the selected model.

The retained typed schema recognizes upstream configuration sections for easier ports.
The template enables shell, Whisper speech input, and stock English Kokoro output.
`tts.enabled` and `stt.enabled` disable speech independently. Demo composition rejects
enabled Mindcraft, scratchpad and autonomous event processing. Logs must resolve under this checkout's `.demo-state`.
