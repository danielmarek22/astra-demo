from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
import yaml

from app.core.session_ids import validate_session_id
from app.paths import DEFAULT_CONFIG_PATH, TEMPLATE_CONFIG_PATH


class _StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


_IDENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _stripped_nonempty(value: str, *, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be empty")
    return stripped


class _LLMGenerationConfig(_StrictConfigModel):
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0)
    min_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, gt=0)
    num_predict: int | None = Field(default=None, gt=0)
    rep_pen: float | None = Field(default=None, gt=0.0)
    repeat_penalty: float | None = Field(default=None, gt=0.0)
    num_ctx: int | None = Field(default=None, gt=0)
    num_gpu_layers: int | None = Field(default=None, ge=-1)

    @model_validator(mode="after")
    def validate_aliases(self):
        if self.max_tokens is not None and self.num_predict is not None:
            raise ValueError("max_tokens and num_predict are aliases; configure only one")
        if self.rep_pen is not None and self.repeat_penalty is not None:
            raise ValueError("rep_pen and repeat_penalty are aliases; configure only one")
        return self


class _LLMThinkingConfig(_StrictConfigModel):
    enabled: bool = False
    level: Literal["low", "medium", "high"] | None = None
    generation: _LLMGenerationConfig = Field(default_factory=_LLMGenerationConfig)


class _LLMConfig(_StrictConfigModel):
    backend: Literal["ollama"] = "ollama"
    host: str = "http://localhost:11434"
    model: str = "gemma4:12b-it-qat"
    timeout_s: float = Field(default=30.0, gt=0.0, le=3600.0)
    max_retries: int = Field(default=2, ge=0, le=20)
    retry_backoff_s: float = Field(default=0.25, ge=0.0, le=60.0)
    generation: _LLMGenerationConfig = Field(default_factory=_LLMGenerationConfig)
    thinking: _LLMThinkingConfig = Field(default_factory=_LLMThinkingConfig)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        value = _stripped_nonempty(value, field_name="llm.host")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("llm.host must be an HTTP(S) origin without credentials or a path")
        return value.rstrip("/")

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        return _stripped_nonempty(value, field_name="llm.model")


class _IdentityConfig(_StrictConfigModel):
    id: str
    display_name: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        value = value.strip()
        if not _IDENTITY_ID_RE.fullmatch(value):
            raise ValueError(
                "must be 1-128 characters using letters, digits, '.', '_', ':', or '-', "
                "and must begin with a letter or digit"
            )
        return value

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        value = _stripped_nonempty(value, field_name="display_name")
        if len(value) > 128:
            raise ValueError("display_name must be at most 128 characters")
        return value


class _LocalHumanConfig(_IdentityConfig):
    id: str = "local-human"
    display_name: str = "You"


class _AssistantPersonalityConfig(_StrictConfigModel):
    default_emotion: str = "neutral"

    @field_validator("default_emotion")
    @classmethod
    def validate_default_emotion(cls, value: str) -> str:
        return _stripped_nonempty(value, field_name="assistant.personality.default_emotion")


class _AvatarControlsConfig(_StrictConfigModel):
    default_outfit: str = "default"
    expressions: list[str] | None = None

    @field_validator("default_outfit")
    @classmethod
    def validate_default_outfit(cls, value: str) -> str:
        return _stripped_nonempty(value, field_name="assistant.avatar_controls.default_outfit")

    @field_validator("expressions")
    @classmethod
    def validate_expressions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("assistant.avatar_controls.expressions must not be empty")
        normalized: list[str] = []
        seen: set[str] = set()
        for expression in value:
            expression = _stripped_nonempty(
                expression,
                field_name="assistant.avatar_controls.expressions item",
            ).lower()
            if len(expression) > 64:
                raise ValueError("avatar expression names must be at most 64 characters")
            if expression in seen:
                raise ValueError(f"duplicate avatar expression: {expression!r}")
            seen.add(expression)
            normalized.append(expression)
        return normalized


class _AssistantConfig(_IdentityConfig):
    id: str = "default-agent"
    display_name: str = "Astra"
    system_prompt: str = "You are Astra, a local personal assistant."
    personality: _AssistantPersonalityConfig = Field(
        default_factory=_AssistantPersonalityConfig
    )
    avatar_controls: _AvatarControlsConfig = Field(default_factory=_AvatarControlsConfig)

    @field_validator("system_prompt")
    @classmethod
    def validate_system_prompt(cls, value: str) -> str:
        value = _stripped_nonempty(value, field_name="assistant.system_prompt")
        if len(value) > 100_000:
            raise ValueError("assistant.system_prompt must be at most 100000 characters")
        return value


class _GPTSoVITSConfig(_StrictConfigModel):
    api_url: str = "http://127.0.0.1:9880/tts"
    ref_audio_path: str = ""
    prompt_text: str = ""
    text_lang: str = "en"
    prompt_lang: str = "en"

    @field_validator("api_url")
    @classmethod
    def validate_api_url(cls, value: str) -> str:
        value = _stripped_nonempty(value, field_name="tts.gpt_sovits.api_url")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("must be an HTTP(S) URL without credentials or a fragment")
        return value

    @field_validator("ref_audio_path", "prompt_text")
    @classmethod
    def strip_optional_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("text_lang", "prompt_lang")
    @classmethod
    def validate_language(cls, value: str) -> str:
        return _stripped_nonempty(value, field_name="GPT-SoVITS language")


class _PiperConfig(_StrictConfigModel):
    model_path: str = "models/piper/en_US-amy-medium.onnx"
    use_cuda: bool = False

    @field_validator("model_path")
    @classmethod
    def validate_model_path(cls, value: str) -> str:
        return _stripped_nonempty(value, field_name="tts.piper.model_path")


class _KokoroConfig(_StrictConfigModel):
    voice: str = Field(default="af_heart", pattern=r"^[ab][fm]_[a-z_]+$")
    lang_code: Literal["a", "b"] = "a"
    device: Literal["cpu", "cuda"] = "cpu"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)

    @model_validator(mode="after")
    def validate_voice_language(self):
        if not self.voice.startswith(self.lang_code):
            raise ValueError("Kokoro stock voice must match lang_code")
        return self


class _TTSConfig(_StrictConfigModel):
    enabled: bool = True
    engine: Literal["kokoro", "pocket_tts", "piper", "gpt_sovits"] = "pocket_tts"
    kokoro: _KokoroConfig = Field(default_factory=_KokoroConfig)
    gpt_sovits: _GPTSoVITSConfig = Field(default_factory=_GPTSoVITSConfig)
    piper: _PiperConfig = Field(default_factory=_PiperConfig)

    @model_validator(mode="before")
    @classmethod
    def reject_removed_layouts(cls, value):
        if not isinstance(value, dict):
            return value
        if "qwen3" in value or value.get("engine") == "qwen3":
            raise ValueError(
                "tts.qwen3 is not supported because no Qwen TTS engine is implemented; "
                "choose pocket_tts, piper, or gpt_sovits"
            )
        legacy_selector = next(
            (name for name in ("provider", "backend") if name in value),
            None,
        )
        if legacy_selector is not None:
            raise ValueError(f"tts.{legacy_selector} was replaced by tts.engine")
        legacy_flat_fields = sorted(
            set(value)
            & {
                "api_url",
                "ref_audio_path",
                "prompt_text",
                "text_lang",
                "prompt_lang",
                "model_path",
                "use_cuda",
            }
        )
        if legacy_flat_fields:
            raise ValueError(
                "flat TTS settings were replaced by tts.gpt_sovits or tts.piper; "
                f"move: {', '.join(legacy_flat_fields)}"
            )
        return value

    @field_validator("engine", mode="before")
    @classmethod
    def normalize_engine(cls, value):
        aliases = {
            "pocket-tts": "pocket_tts",
            "pocket": "pocket_tts",
            "gpt-sovits": "gpt_sovits",
            "sovits": "gpt_sovits",
        }
        return aliases.get(value, value)

    @model_validator(mode="after")
    def validate_selected_engine(self):
        if self.engine == "gpt_sovits" and not self.gpt_sovits.ref_audio_path:
            raise ValueError(
                "tts.engine=gpt_sovits requires tts.gpt_sovits.ref_audio_path"
            )
        return self


class _VADParametersConfig(_StrictConfigModel):
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    neg_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    min_speech_duration_ms: int = Field(default=0, ge=0)
    max_speech_duration_s: float | None = Field(default=None, gt=0.0)
    min_silence_duration_ms: int = Field(default=300, ge=0)
    speech_pad_ms: int = Field(default=400, ge=0)

    @model_validator(mode="after")
    def validate_thresholds(self):
        if self.neg_threshold is not None and self.neg_threshold > self.threshold:
            raise ValueError("neg_threshold must not exceed threshold")
        return self


class _STTConfig(_StrictConfigModel):
    enabled: bool = True
    model_size: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    vad_filter: bool = True
    vad_parameters: _VADParametersConfig = Field(default_factory=_VADParametersConfig)

    @field_validator("model_size", "device", "compute_type")
    @classmethod
    def validate_runtime_name(cls, value: str) -> str:
        value = _stripped_nonempty(value, field_name="STT runtime value")
        if len(value) > 256:
            raise ValueError("STT runtime values must be at most 256 characters")
        return value


_LOG_LEVEL_ALIASES = {
    "WARN": "WARNING",
    "FATAL": "CRITICAL",
}
_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}


class _LoggingConfig(_StrictConfigModel):
    level: str = "INFO"
    console_level: str = "INFO"
    file_level: str = "INFO"
    dir: str = "logs"
    file_name: str = "assistant.log"
    max_bytes: int = Field(default=10_000_000, gt=0, le=10_000_000_000)
    backup_count: int = Field(default=5, ge=0, le=1000)
    trace_enabled: bool = True
    trace_level: str = "DEBUG"
    trace_file_name: str = "trace.log"
    trace_max_bytes: int = Field(default=10_000_000, gt=0, le=10_000_000_000)
    trace_backup_count: int = Field(default=5, ge=0, le=1000)
    trace_max_string_chars: int | None = Field(default=None, gt=0, le=10_000_000)
    trace_max_event_chars: int | None = Field(default=None, ge=1024, le=100_000_000)
    trace_include_thinking: bool = False

    @field_validator("level", "console_level", "file_level", "trace_level")
    @classmethod
    def validate_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        normalized = _LOG_LEVEL_ALIASES.get(normalized, normalized)
        if normalized not in _LOG_LEVELS:
            raise ValueError(
                "must be one of: CRITICAL, ERROR, WARNING, INFO, DEBUG, NOTSET"
            )
        return normalized

    @field_validator("dir")
    @classmethod
    def validate_log_dir(cls, value: str) -> str:
        value = _stripped_nonempty(value, field_name="logging.dir")
        if "\x00" in value or len(value) > 4096:
            raise ValueError("logging.dir must be a valid path of at most 4096 characters")
        return value

    @field_validator("file_name", "trace_file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        value = _stripped_nonempty(value, field_name="logging file name")
        if (
            value in {".", ".."}
            or Path(value).name != value
            or any(ord(character) < 32 for character in value)
            or len(value) > 255
        ):
            raise ValueError("must be a plain file name without directories")
        return value

    @model_validator(mode="after")
    def validate_distinct_files(self):
        if self.trace_enabled and self.file_name == self.trace_file_name:
            raise ValueError(
                "logging.file_name and logging.trace_file_name must be different"
            )
        return self


class _TelemetryConfig(_StrictConfigModel):
    mode: Literal["none", "minimal", "full", "extended"] = "none"
    file_name: str = "model-telemetry.jsonl"

    @field_validator("file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        value = _stripped_nonempty(value, field_name="telemetry.file_name")
        if Path(value).name != value:
            raise ValueError("telemetry.file_name must be a file name without directories")
        return value


class _EnabledIntegrationConfig(_StrictConfigModel):
    enabled: bool


class _WebCacheConfig(_StrictConfigModel):
    ttl_s: float = Field(default=600.0, gt=0, le=3600)
    max_document_bytes: int = Field(default=524288, ge=1024, le=1048576)
    max_session_bytes: int = Field(default=4194304, ge=262144, le=16777216)
    max_total_bytes: int = Field(default=16777216, ge=262144, le=67108864)
    max_session_entries: int = Field(default=64, ge=50, le=256)
    max_total_entries: int = Field(default=256, ge=50, le=1024)
    max_document_lines: int = Field(default=10000, ge=1, le=20000)

    @model_validator(mode="after")
    def validate_limits(self):
        if not self.max_document_bytes <= self.max_session_bytes <= self.max_total_bytes:
            raise ValueError("web cache requires document bytes <= session bytes <= total bytes")
        if self.max_session_entries > self.max_total_entries:
            raise ValueError("web cache requires session entries <= total entries")
        return self


class _WebReaderConfig(_StrictConfigModel):
    enabled: bool = False
    python_executable: str | None = None
    browsers_path: str | None = None
    total_timeout_s: float = Field(default=30.0, gt=0, le=60)
    max_response_bytes: int = Field(default=2097152, ge=1024, le=8388608)
    max_total_bytes: int = Field(default=4194304, ge=1024, le=16777216)
    max_requests: int = Field(default=32, ge=1, le=64)
    max_redirects: int = Field(default=3, ge=0, le=5)
    max_links: int = Field(default=20, ge=0, le=30)
    max_memory_mb: int = Field(default=1536, ge=512, le=4096)
    max_cpu_percent: int = Field(default=100, ge=10, le=400)
    max_tasks: int = Field(default=256, ge=64, le=512)
    window_lines: int = Field(default=40, ge=1, le=100)
    max_output_chars: int = Field(default=8000, ge=1024, le=16000)
    find_max_matches: int = Field(default=5, ge=1, le=10)
    find_context_lines: int = Field(default=3, ge=0, le=5)
    find_text_max_chars: int = Field(default=200, ge=1, le=500)

    @field_validator("python_executable", "browsers_path")
    @classmethod
    def validate_path(cls, value):
        if value is not None and not Path(value).is_absolute():
            raise ValueError("web reader paths must be absolute")
        return value

    @model_validator(mode="after")
    def validate_limits(self):
        if self.max_response_bytes > self.max_total_bytes:
            raise ValueError("web reader requires response bytes <= total bytes")
        return self


class _WebIntegrationConfig(_EnabledIntegrationConfig):
    enabled: bool = False
    provider: Literal["searxng", "langsearch"] = "searxng"
    langsearch_api_key_env: str = "LANGSEARCH_API_KEY"
    base_url: str = "http://localhost:8080"
    timeout: float = Field(default=10.0, gt=0.0, le=300.0)
    max_retries: int = Field(default=2, ge=0, le=20)
    retry_backoff_s: float = Field(default=0.25, ge=0.0, le=60.0)
    max_results: int = Field(default=5, gt=0, le=50)
    total_timeout_s: float = Field(default=15.0, gt=0.0, le=300.0)
    max_retry_after_s: float = Field(default=2.0, ge=0.0, le=30.0)
    max_response_bytes: int = Field(default=1048576, ge=1024, le=8388608)
    query_max_chars: int = Field(default=512, ge=1, le=2000)
    title_max_chars: int = Field(default=240, ge=1, le=1000)
    url_max_chars: int = Field(default=2048, ge=128, le=4096)
    snippet_max_chars: int = Field(default=1000, ge=1, le=4000)
    max_output_chars: int = Field(default=8000, ge=1024, le=32000)
    cache: _WebCacheConfig = Field(default_factory=_WebCacheConfig)
    reader: _WebReaderConfig = Field(default_factory=_WebReaderConfig)

    @field_validator("langsearch_api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value):
            raise ValueError("integrations.web.langsearch_api_key_env must be an environment variable name")
        return value

    @model_validator(mode="after")
    def validate_output_budget(self):
        if self.max_output_chars < self.query_max_chars + 512:
            raise ValueError("web.max_output_chars must reserve at least query_max_chars + 512 characters for metadata")
        return self

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = _stripped_nonempty(value, field_name="integrations.web.base_url")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "integrations.web.base_url must be an HTTP(S) URL without credentials, "
                "a query, or a fragment"
            )
        return value.rstrip("/")


class _MemoryIntegrationConfig(_EnabledIntegrationConfig):
    enabled: bool = True


class _HistoryIntegrationConfig(_EnabledIntegrationConfig):
    enabled: bool = True
    allow_all_sessions: bool = False
    trusted_session_ids: list[str] = Field(default_factory=list, max_length=256)

    @field_validator("trusted_session_ids")
    @classmethod
    def validate_trusted_session_ids(cls, value: list[str]) -> list[str]:
        for session_id in value:
            validate_session_id(session_id)
        if len(value) != len(set(value)):
            raise ValueError("trusted_session_ids must not contain duplicates")
        return value


class _ScratchpadIntegrationConfig(_EnabledIntegrationConfig):
    enabled: bool = True
    recall_limit: int = Field(default=3, ge=1, le=20)
    max_distance: float = Field(default=0.70, ge=0.0, le=2.0)
    query_max_chars: int = Field(default=2000, ge=1, le=2000)
    context_max_chars: int = Field(default=3000, ge=300, le=12000)


class _ShellIntegrationConfig(_EnabledIntegrationConfig):
    enabled: bool = True
    timeout: int = Field(default=15, gt=0, le=3600)


_MindcraftAutonomousEvent = Literal[
    "spawned",
    "disconnected",
    "damage_taken",
    "critical_health",
    "died",
    "respawned",
    "player_joined",
    "player_left",
    "player_spoke",
    "delegation_rejected",
]


class _MindcraftIntegrationConfig(_EnabledIntegrationConfig):
    enabled: bool = False
    url: str = "http://localhost:8081"
    agent_name: str | None = None
    connect_timeout: float = Field(default=3.0, gt=0.0, le=300.0)
    reconnect_delay_s: float = Field(default=2.0, gt=0.0, le=3600.0)
    reconnect_max_delay_s: float = Field(default=30.0, gt=0.0, le=3600.0)
    context_enabled: bool = True
    recent_output_limit: int = Field(default=3, gt=0, le=1000)
    events_enabled: bool = True
    ambient_session_id: str | None = None
    autonomous_events: list[_MindcraftAutonomousEvent] = Field(
        default_factory=lambda: ["critical_health", "died", "disconnected"]
    )
    attachment_dir: str = "static/uploads/events/mindcraft"

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = _stripped_nonempty(value, field_name="integrations.mindcraft.url")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "integrations.mindcraft.url must be an HTTP(S) URL without credentials, "
                "a query, or a fragment"
            )
        return value.rstrip("/")

    @field_validator("agent_name")
    @classmethod
    def normalize_agent_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > 128:
            raise ValueError("agent_name must be at most 128 characters")
        return value

    @field_validator("ambient_session_id")
    @classmethod
    def validate_ambient_session_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        return validate_session_id(value)

    @field_validator("autonomous_events")
    @classmethod
    def validate_autonomous_events(
        cls,
        value: list[_MindcraftAutonomousEvent],
    ) -> list[_MindcraftAutonomousEvent]:
        if len(value) != len(set(value)):
            raise ValueError("autonomous_events must not contain duplicates")
        return value

    @field_validator("attachment_dir")
    @classmethod
    def validate_attachment_dir(cls, value: str) -> str:
        value = _stripped_nonempty(
            value,
            field_name="integrations.mindcraft.attachment_dir",
        )
        if "\x00" in value or len(value) > 4096:
            raise ValueError("attachment_dir must be a valid path of at most 4096 characters")
        return value

    @model_validator(mode="after")
    def validate_reconnect_delays(self):
        if self.reconnect_max_delay_s < self.reconnect_delay_s:
            raise ValueError("reconnect_max_delay_s must be at least reconnect_delay_s")
        return self


class MCPServerConfig(_StrictConfigModel):
    """Local configuration only; environment mappings contain variable names."""

    enabled: bool = False
    description: str = Field(default="", max_length=512)
    transport: Literal["stdio", "streamable_http"]
    command: str | None = Field(default=None, min_length=1, max_length=1024)
    args: list[str] = Field(default_factory=list, max_length=128)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict, max_length=32)
    url: str | None = Field(default=None, max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict, max_length=32)
    enabled_tools: list[str] = Field(default_factory=list, max_length=256)
    tool_fingerprints: dict[str, str] = Field(default_factory=dict, max_length=256)
    tool_approvals: dict[str, bool] = Field(default_factory=dict, max_length=256)
    approval_required: bool = True
    connect_timeout_s: float = Field(default=5.0, gt=0, le=120, allow_inf_nan=False)
    catalog_timeout_s: float = Field(default=15.0, gt=0, le=120, allow_inf_nan=False)
    shutdown_timeout_s: float = Field(default=5.0, gt=0, le=30, allow_inf_nan=False)
    max_catalog_pages: int = Field(default=32, gt=0, le=128)
    max_catalog_tools: int = Field(default=256, gt=0, le=2048)
    max_catalog_bytes: int = Field(default=1048576, ge=1024, le=8388608)
    call_timeout_s: float = Field(default=30.0, gt=0, le=300, allow_inf_nan=False)
    max_message_bytes: int = Field(default=1048576, ge=1024, le=8388608)
    max_schema_bytes: int = Field(default=65536, ge=1024, le=262144)
    max_argument_bytes: int = Field(default=16384, ge=512, le=65536)
    max_result_chars: int = Field(default=16384, ge=2048, le=131072)

    @field_validator("command", "cwd", "url")
    @classmethod
    def validate_transport_text(cls, value):
        if value is not None and (
            not value or value != value.strip() or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("MCP transport values must be nonempty and contain no control characters")
        return value

    @field_validator("args")
    @classmethod
    def validate_args(cls, value):
        if any(len(arg) > 8192 or "\x00" in arg for arg in value):
            raise ValueError("MCP arguments must be bounded and contain no NUL characters")
        return value

    @field_validator("env", "headers")
    @classmethod
    def validate_environment_references(cls, value, info):
        env_name = r"[A-Za-z_][A-Za-z0-9_]*"
        for key, reference in value.items():
            if not re.fullmatch(env_name, reference) or len(reference) > 128:
                raise ValueError("MCP secrets must reference environment variable names")
            if info.field_name == "env":
                if not re.fullmatch(env_name, key) or len(key) > 128:
                    raise ValueError("Invalid MCP child environment variable name")
            elif (
                not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", key)
                or key.lower() in {"host", "content-length", "content-type", "connection", "transfer-encoding"}
                or key.lower().startswith("mcp-")
            ):
                raise ValueError("Invalid or transport-owned MCP header name")
        if info.field_name == "headers" and len({key.lower() for key in value}) != len(value):
            raise ValueError("Duplicate MCP header names")
        return value

    @field_validator("enabled_tools")
    @classmethod
    def validate_enabled_tools(cls, value):
        if len(value) != len(set(value)) or any(
            not name or len(name) > 128 or "*" in name
            or any(char.isspace() or ord(char) < 32 for char in name) for name in value
        ):
            raise ValueError("MCP enabled tools must be unique exact tool names, without wildcards")
        return value

    @field_validator("tool_fingerprints")
    @classmethod
    def validate_tool_fingerprints(cls, value):
        cls.validate_enabled_tools(list(value))
        if any(not re.fullmatch(r"[0-9a-f]{64}", fingerprint) for fingerprint in value.values()):
            raise ValueError("MCP tool fingerprints must be lowercase SHA-256 values")
        return value

    @field_validator("tool_approvals")
    @classmethod
    def validate_tool_approvals(cls, value):
        cls.validate_enabled_tools(list(value))
        return value

    @model_validator(mode="after")
    def validate_transport(self):
        if not set(self.tool_fingerprints) <= set(self.enabled_tools):
            raise ValueError("MCP tool fingerprints must refer to explicitly enabled tools")
        if self.transport == "stdio":
            if not self.command or self.url is not None or self.headers:
                raise ValueError("MCP stdio requires command and forbids HTTP settings")
            if self.cwd is not None and not Path(self.cwd).is_absolute():
                raise ValueError("MCP working directory must be absolute")
        else:
            if self.command is not None or self.args or self.cwd is not None or self.env:
                raise ValueError("MCP HTTP forbids subprocess settings")
            try:
                parsed = urlsplit(self.url or "")
                port = parsed.port
                invalid = (
                    parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or port == 0
                    or any(char.isspace() for char in self.url or "")
                )
            except ValueError:
                invalid = True
            if invalid:
                raise ValueError("MCP requires an HTTP(S) URL without credentials, query, or fragment")
        return self


class MCPConfig(_StrictConfigModel):
    enabled: bool = False
    servers: dict[str, MCPServerConfig] = Field(default_factory=dict, max_length=32)
    queue_timeout_s: float = Field(default=5.0, gt=0, le=120, allow_inf_nan=False)

    @field_validator("servers")
    @classmethod
    def validate_server_ids(cls, value):
        if any(not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", key) or len(key) > 48 for key in value):
            raise ValueError("MCP server IDs must be bounded lowercase identifiers")
        return value


class _IntegrationsConfig(_StrictConfigModel):
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    web: _WebIntegrationConfig = Field(default_factory=_WebIntegrationConfig)
    memory: _MemoryIntegrationConfig = Field(default_factory=_MemoryIntegrationConfig)
    scratchpad: _ScratchpadIntegrationConfig = Field(default_factory=_ScratchpadIntegrationConfig)
    history: _HistoryIntegrationConfig = Field(default_factory=_HistoryIntegrationConfig)
    shell: _ShellIntegrationConfig = Field(default_factory=_ShellIntegrationConfig)
    mindcraft: _MindcraftIntegrationConfig = Field(
        default_factory=_MindcraftIntegrationConfig
    )


class _OrchestratorConfig(_StrictConfigModel):
    summary_trigger: int = Field(default=10, gt=0)
    summary_timeout_s: float = Field(default=300.0, gt=0.0, le=3600.0)
    summary_num_predict: int = Field(default=384, gt=0)
    generation_deadline_s: float = Field(default=600.0, gt=0)
    recovery_deadline_s: float = Field(default=180.0, gt=0)
    recovery_num_predict: int = Field(default=192, gt=0)
    recovery_observation_chars: int = Field(default=1200, gt=0)


class _WorkspaceConfig(_StrictConfigModel):
    max_bytes: int = Field(default=8192, ge=1024)
    max_text_chars: int = Field(default=512, gt=0)
    max_items: int = Field(default=8, gt=0)
    max_observations: int = Field(default=8, gt=0)
    observation_chars: int = Field(default=192, gt=0)
    goal_chars: int = Field(default=512, gt=0)


class _ExecutionProfileConfig(_StrictConfigModel):
    # Missing overrides preserve the existing llm/orchestrator/autonomy settings.
    max_inference_steps: int | None = Field(default=None, gt=0)
    generation_deadline_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    recovery_deadline_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    recovery_num_predict: int | None = Field(default=None, gt=0)
    recovery_observation_chars: int | None = Field(default=None, gt=0)
    correction_num_predict: int | None = Field(default=None, gt=0)
    context_window_tokens: int | None = Field(default=None, ge=1024, le=262144)
    num_predict: int | None = Field(default=None, gt=0)

    max_continuations: int | None = Field(default=None, ge=0)
    max_continuation_attempts: int | None = Field(default=None, gt=0)
    max_tool_dispatches: int | None = Field(default=None, gt=0)
    workspace: _WorkspaceConfig | None = None

    @model_validator(mode="after")
    def validate_context(self):
        if (self.context_window_tokens is not None and self.num_predict is not None
                and self.num_predict >= self.context_window_tokens):
            raise ValueError("num_predict must leave input space in context_window_tokens")
        return self


class _ExecutionProfilesConfig(_StrictConfigModel):
    standard: _ExecutionProfileConfig = Field(default_factory=_ExecutionProfileConfig)
    deep: _ExecutionProfileConfig = Field(default_factory=_ExecutionProfileConfig)


class _ExecutionConfig(_StrictConfigModel):
    default_profile: Literal["standard", "deep"] = "standard"
    profiles: _ExecutionProfilesConfig = Field(default_factory=_ExecutionProfilesConfig)


class _ReasoningConfig(_StrictConfigModel):
    # Additional runtime reasoning steps; independent of llm.thinking.
    enabled: bool = False


class _PromptBudgetConfig(_StrictConfigModel):
    enabled: bool = False
    context_window_tokens: int = Field(default=32768, ge=1024, le=262144)
    completion_reserve_tokens: int = Field(default=2048, ge=1, le=32768)
    safety_margin_tokens: int = Field(default=1024, ge=128, le=8192)
    media_reserve_tokens: int = Field(default=8192, ge=1024, le=65536)
    text_bytes_per_token: float = Field(default=1.0, ge=1.0, le=4.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_reserves(self):
        if self.completion_reserve_tokens + self.safety_margin_tokens >= self.context_window_tokens:
            raise ValueError("prompt budget reserves must leave positive input space")
        return self


class _ContextConfig(_StrictConfigModel):
    history_limit: int = Field(default=6, gt=0)
    injected_memory_limit: int = Field(default=5, gt=0)
    semantic_memory_max_distance: float = Field(default=0.70, ge=0.0, le=2.0)
    semantic_memory_fallback_max_distance: float = Field(default=0.85, ge=0.0, le=2.0)
    semantic_memory_fallback_limit: int = Field(default=2, gt=0)
    episodic_memory_max_distance: float = Field(default=0.70, ge=0.0, le=2.0)
    integration_context_limit: int = Field(default=4000, gt=0)
    image_summary_timeout_s: float = Field(default=15.0, gt=0.0, le=120.0)
    prompt_budget: _PromptBudgetConfig = Field(default_factory=_PromptBudgetConfig)

    @model_validator(mode="after")
    def validate_semantic_memory_fallback(self):
        if self.semantic_memory_fallback_max_distance < self.semantic_memory_max_distance:
            raise ValueError(
                "semantic_memory_fallback_max_distance must be at least "
                "semantic_memory_max_distance"
            )
        return self


class _BeliefsConfig(_StrictConfigModel):
    enabled: bool = True
    processing_mode: Literal["disabled", "observer", "react_tool"] = "disabled"
    timezone: str = "UTC"
    max_candidates: int = Field(default=4, gt=0)
    max_existing_beliefs: int = Field(default=24, gt=0)
    max_snapshot_chars: int = Field(default=2000, gt=0)
    max_disambiguating_context_chars: int = Field(default=1000, ge=0)
    max_generation_tokens: int = Field(default=384, gt=0)
    timeout_s: float = Field(default=30.0, gt=0)
    max_expiry_days: int = Field(default=90, gt=0)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_enabled_mode(self):
        if not self.enabled and self.processing_mode != "disabled":
            raise ValueError(
                "beliefs.enabled=false requires beliefs.processing_mode=disabled"
            )
        return self


class _AutonomyConfig(_StrictConfigModel):
    enabled: bool = False
    max_chain_events: int = Field(default=20, gt=0)
    max_chain_age_s: float = Field(default=1800.0, gt=0)
    max_queue_size: int = Field(default=256, gt=0)
    max_tool_steps: int = Field(default=5, gt=0)
    global_llm_concurrency: int = Field(default=1, gt=0)
    approval_timeout_s: float = Field(default=300.0, gt=0)
    recent_context_limit: int = Field(default=4000, gt=0)


class _NativeAudioConfig(_StrictConfigModel):
    payload_field: Literal["images", "audios"] = "images"
    prompt_text: str = "Please answer the user's spoken audio."
    display_text: str = "Voice message"
    convert_to_wav: bool = True
    sample_rate: int = Field(default=16000, ge=8000, le=192000)


class _VoiceInputConfig(_StrictConfigModel):
    path: Literal["stt", "native_audio"] = "stt"
    native_audio: _NativeAudioConfig = Field(default_factory=_NativeAudioConfig)


class _VisionWatchdogConfig(_StrictConfigModel):
    model: str = "HuggingFaceTB/SmolVLM-256M-Instruct"
    device: str = "auto"
    torch_dtype: str = "auto"
    attn_implementation: str = "auto"
    max_new_tokens: int = Field(default=8, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)


class _RuntimeConfigSections(_StrictConfigModel):
    llm: _LLMConfig
    assistant: _AssistantConfig
    local_human: _LocalHumanConfig
    tts: _TTSConfig
    stt: _STTConfig
    logging: _LoggingConfig
    telemetry: _TelemetryConfig
    integrations: _IntegrationsConfig
    orchestrator: _OrchestratorConfig
    execution: _ExecutionConfig
    reasoning: _ReasoningConfig
    context: _ContextConfig
    beliefs: _BeliefsConfig
    autonomy: _AutonomyConfig
    voice_input: _VoiceInputConfig
    vision_watchdog: _VisionWatchdogConfig

    @model_validator(mode="after")
    def validate_page_reader_activation(self):
        if self.integrations.web.reader.enabled:
            if not self.integrations.web.enabled or not self.context.prompt_budget.enabled:
                raise ValueError("web reader activation requires integrations.web.enabled and context.prompt_budget.enabled")
            if self.llm.generation.num_ctx is None:
                raise ValueError("web reader activation requires an explicit llm.generation.num_ctx validated for this model")
        return self


def _format_config_error(exc: ValidationError) -> ValueError:
    issues = []
    for error in exc.errors(include_url=False, include_input=False)[:12]:
        location = ".".join(str(part) for part in error["loc"])
        issues.append(f"{location}: {error['msg']} [{error['type']}]")
    return ValueError("Invalid configuration: " + "; ".join(issues))


class Config:
    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else TEMPLATE_CONFIG_PATH
        self.path = Path(path).expanduser().resolve()
        with self.path.open("r") as f:
            raw = yaml.safe_load(f)
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("Invalid configuration: document root must be a mapping")
        if "tools" in raw:
            raise ValueError(
                "Invalid configuration: tools.web was removed; move web search settings "
                "to integrations.web and remove the tools section"
            )
        unknown_sections = sorted(set(raw) - _RuntimeConfigSections.model_fields.keys(), key=repr)
        if unknown_sections:
            raise ValueError(
                "Invalid configuration: unsupported top-level sections: "
                + ", ".join(repr(name) for name in unknown_sections)
            )
        for section_name, section_value in raw.items():
            if not isinstance(section_value, dict):
                raise ValueError(
                    f"Invalid configuration: {section_name} must be a mapping"
                )
        runtime_sections = {
            section_name: raw.get(section_name, {})
            for section_name in _RuntimeConfigSections.model_fields
        }
        runtime_sections["beliefs"] = self._load_beliefs_config(runtime_sections["beliefs"])
        try:
            validated = _RuntimeConfigSections.model_validate(runtime_sections)
        except ValidationError as exc:
            raise _format_config_error(exc) from exc

        normalized = validated.model_dump(mode="python", exclude_none=True)
        self.raw = raw
        for section_name, section_value in normalized.items():
            setattr(self, section_name, section_value)
            self.raw[section_name] = section_value

    @staticmethod
    def _load_beliefs_config(raw_beliefs: dict | None) -> dict:
        config = {}
        if isinstance(raw_beliefs, dict):
            if "extraction_enabled" in raw_beliefs:
                raise ValueError(
                    "beliefs.extraction_enabled was replaced by beliefs.processing_mode; "
                    "valid values are: disabled, observer, react_tool"
                )
            config.update(raw_beliefs)
        valid_modes = {"disabled", "observer", "react_tool"}
        mode = config.get("processing_mode", "disabled")
        if mode not in valid_modes:
            raise ValueError(
                "beliefs.processing_mode must be one of: disabled, observer, react_tool"
            )
        return config
