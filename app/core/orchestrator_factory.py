"""Compose the standalone demo from Astra's retained core services."""

import logging
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Config
from app.llm.ollama_stream import OllamaClient
from app.core.orchestrator import Orchestrator
from app.storage.database import Database
from app.storage.vector_store import VectorStore
from app.memory.chat_history import ChatHistoryStore
from app.memory.memory_store import MemoryStore
from app.memory.summary_store import SummaryStore
from app.core.context_builder import ContextBuilder
from app.memory.history_summarizer import HistorySummarizer
from app.memory.memory_policy import SimpleMemoryPolicy
from app.memory.action_handler import MemoryActionHandler
from app.memory.retriever import MemoryRetriever
from app.core.tool_executor import ToolExecutor
from app.core.turn_finalizer import TurnFinalizer
from app.integrations import (
    BeliefIntegration,
    IntegrationRegistry,
    MemoryIntegration,
    WebIntegration,
)
from app.integrations.builtins import ShellIntegration
from app.tools.bash_execution import BashExecutionTool
from app.tools.web_search import SearXNGClient, WebSearchTool
from app.tools.search_summarizer import SearchResultSummarizer
from app.beliefs import (
    BeliefCandidateExtractor,
    BeliefContextProvider,
    BeliefRepository,
    BeliefSnapshotFormatter,
    BeliefSnapshotService,
    BeliefUpdateService,
    ConversationalBeliefObserver,
    BeliefTurnPreparer,
    REACT_TOOL_BELIEF_VERSION,
)
from app.paths import DATA_DIR, owned_state_path

logger = logging.getLogger("orchestrator_factory")


def _build_ollama_options(raw_generation: dict | None) -> dict:
    generation = raw_generation if isinstance(raw_generation, dict) else {}
    options: dict[str, object] = {}

    field_map = {
        "temperature": "temperature",
        "top_p": "top_p",
        "top_k": "top_k",
        "min_p": "min_p",
        "max_tokens": "num_predict",
        "num_predict": "num_predict",
        "rep_pen": "repeat_penalty",
        "repeat_penalty": "repeat_penalty",
        "num_ctx": "num_ctx",
        "num_gpu_layers": "num_gpu",
    }

    for source_key, target_key in field_map.items():
        if source_key not in generation:
            continue
        options[target_key] = generation[source_key]

    return options


@dataclass(frozen=True)
class BeliefComponents:
    """Belief wiring without taking ownership of the shared database."""

    repository: BeliefRepository | None = None
    context_provider: BeliefContextProvider | None = None
    completion_observers: list[ConversationalBeliefObserver] = field(
        default_factory=list
    )
    integration: BeliefIntegration | None = None
    turn_preparer: BeliefTurnPreparer | None = None


def _build_belief_components(
    *,
    config,
    llm,
    db,
    history_store,
    agent_id: str,
    native_late_routing_enabled: bool = True,
) -> BeliefComponents:
    """Build storage/context and exactly one configured conversational producer."""
    if not config.beliefs.get("enabled", True):
        return BeliefComponents()

    processing_mode = config.beliefs.get("processing_mode", "disabled")
    if processing_mode == "react_tool" and not native_late_routing_enabled:
        raise ValueError(
            "beliefs.processing_mode=react_tool requires native late-routing/tool infrastructure"
        )

    local_human = getattr(config, "local_human", None) or {
        "id": "local-human",
        "display_name": "You",
    }
    repository = BeliefRepository(
        db,
        legacy_local_human_id=local_human["id"],
        legacy_local_human_name=local_human["display_name"],
    )
    snapshot_service = BeliefSnapshotService(
        repository,
        max_beliefs=config.beliefs["max_existing_beliefs"],
    )
    context_provider = BeliefContextProvider(
        owner_agent_id=agent_id,
        snapshot_service=snapshot_service,
        formatter=BeliefSnapshotFormatter(
            max_chars=config.beliefs["max_snapshot_chars"],
        ),
    )
    preparer = BeliefTurnPreparer(
        snapshot_service=snapshot_service,
        history_store=history_store,
    )
    observers = []
    belief_integration = None
    if processing_mode == "observer":
        extractor = BeliefCandidateExtractor(
            llm,
            max_candidates=config.beliefs["max_candidates"],
            max_context_chars=config.beliefs["max_disambiguating_context_chars"],
            max_tokens=config.beliefs["max_generation_tokens"],
            timeout_s=config.beliefs["timeout_s"],
        )
        observers.append(
            ConversationalBeliefObserver(
                extractor=extractor,
                update_service=BeliefUpdateService(
                    repository,
                    max_expiry_days=config.beliefs["max_expiry_days"],
                ),
                snapshot_service=snapshot_service,
                history_store=history_store,
                preparer=preparer,
            )
        )
    elif processing_mode == "react_tool":
        belief_integration = BeliefIntegration(
            update_service=BeliefUpdateService(
                repository,
                extractor_version=REACT_TOOL_BELIEF_VERSION,
                max_expiry_days=config.beliefs["max_expiry_days"],
            ),
            max_candidates=config.beliefs["max_candidates"],
        )
    elif processing_mode != "disabled":
        raise ValueError(f"Unsupported belief processing mode: {processing_mode}")
    return BeliefComponents(
        repository=repository,
        context_provider=context_provider,
        completion_observers=observers,
        integration=belief_integration,
        turn_preparer=preparer,
    )


def validate_demo_config(config: Config) -> None:
    for name in ("mindcraft", "scratchpad"):
        if config.integrations.get(name, {}).get("enabled", False):
            raise ValueError(f"{name} is not supported by the standalone demo")
    if config.autonomy.get("enabled", False):
        raise ValueError("Autonomous event processing is not supported by the demo")


def build_orchestrator(
    config: Config | None = None, *, state_dir: Path | None = None
) -> Orchestrator:
    config = config or Config()
    validate_demo_config(config)
    state_dir = owned_state_path(state_dir or DATA_DIR)
    with ExitStack() as resources:
        llm = OllamaClient(
            model=config.llm["model"],
            host=config.llm["host"],
            options=_build_ollama_options(config.llm.get("generation")),
            thinking_enabled=config.llm.get("thinking", {}).get("enabled", False),
            thinking_level=config.llm.get("thinking", {}).get("level"),
            thinking_options=_build_ollama_options(
                config.llm.get("thinking", {}).get("generation")
            ),
            timeout_s=config.llm.get("timeout_s", 30),
            max_retries=config.llm.get("max_retries", 2),
            retry_backoff_s=config.llm.get("retry_backoff_s", 0.25),
        )
        resources.callback(llm.close)
        # Loading a demo or restoring examples makes no generation requests.
        db = Database(
            path=str(state_dir / "assistant.db"),
            legacy_local_human_id=config.local_human["id"],
            legacy_local_human_name=config.local_human["display_name"],
        )
        resources.callback(db.close)
        vectors = VectorStore(path=str(state_dir / "vectors"))
        resources.callback(vectors.close)
        agent_id = config.assistant["id"]
        history = ChatHistoryStore(
            db,
            vectors,
            uploads_root=str(state_dir / "uploads"),
            local_human_id=config.local_human["id"],
            local_human_name=config.local_human["display_name"],
            local_assistant_id=agent_id,
            local_assistant_name=config.assistant["display_name"],
            episodic_max_distance=config.context["episodic_memory_max_distance"],
        )
        memory = MemoryStore(
            db,
            vectors,
            max_distance=config.context["semantic_memory_max_distance"],
            fallback_max_distance=config.context[
                "semantic_memory_fallback_max_distance"
            ],
            fallback_limit=config.context["semantic_memory_fallback_limit"],
        )
        summaries = SummaryStore(db)
        beliefs = _build_belief_components(
            config=config,
            llm=llm,
            db=db,
            history_store=history,
            agent_id=agent_id,
        )
        integrations = []
        if config.integrations["memory"]["enabled"]:
            integrations.append(
                MemoryIntegration(MemoryActionHandler(memory, SimpleMemoryPolicy()))
            )
        if beliefs.integration:
            integrations.append(beliefs.integration)
        web = config.integrations["web"]
        if web["enabled"]:
            client = SearXNGClient(
                base_url=web["base_url"],
                timeout=web["timeout"],
                max_retries=web["max_retries"],
                retry_backoff_s=web["retry_backoff_s"],
            )
            integrations.append(
                WebIntegration(
                    WebSearchTool(
                        client=client,
                        summarizer=SearchResultSummarizer(llm),
                        max_results=web["max_results"],
                    )
                )
            )
        shell = config.integrations["shell"]
        if shell["enabled"]:
            integrations.append(ShellIntegration(BashExecutionTool(timeout=shell["timeout"])))
        registry = IntegrationRegistry(integrations)
        resources.callback(registry.close)
        orchestrator = Orchestrator(
            llm=llm,
            context_builder=ContextBuilder(
                system_prompt=config.assistant["system_prompt"],
                history_store=history,
                summary_store=summaries,
                history_limit=config.context["history_limit"],
            ),
            history_store=history,
            summary_store=summaries,
            tool_executor=ToolExecutor(registry),
            memory_retriever=MemoryRetriever(
                memory_store=memory,
                history_store=history,
                semantic_limit=config.context["injected_memory_limit"],
            ),
            turn_finalizer=TurnFinalizer(
                history_store=history,
                summary_store=summaries,
                summarizer=HistorySummarizer(
                    llm,
                    timeout_s=config.orchestrator["summary_timeout_s"],
                    num_predict=config.orchestrator["summary_num_predict"],
                ),
                summary_trigger=config.orchestrator["summary_trigger"],
                completion_observers=beliefs.completion_observers,
            ),
            allowed_expressions=set(),
            gesture_catalog={},
            late_routing_enabled=True,
            integration_context_limit=config.context["integration_context_limit"],
            agent_id=agent_id,
            timezone_name=config.beliefs["timezone"],
            belief_context_provider=beliefs.context_provider,
            belief_repository=beliefs.repository,
            belief_processing_mode=config.beliefs["processing_mode"],
            belief_turn_preparer=beliefs.turn_preparer
            if config.beliefs["processing_mode"] == "react_tool"
            else None,
            local_human_id=config.local_human["id"],
            local_human_name=config.local_human["display_name"],
            local_assistant_name=config.assistant["display_name"],
            generation_deadline_s=config.orchestrator["generation_deadline_s"],
            recovery_deadline_s=config.orchestrator["recovery_deadline_s"],
            recovery_num_predict=config.orchestrator["recovery_num_predict"],
            max_late_routing_steps=config.autonomy["max_tool_steps"],
            database=db,
            vector_store=vectors,
        )
        resources.pop_all()
        return orchestrator
