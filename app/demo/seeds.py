"""Build sample state through canonical stores; never copy a personal database."""

import json
from datetime import datetime, timezone
from pathlib import Path

from app.beliefs import BeliefCandidateBatch, BeliefUpdateService
from app.paths import APP_ROOT

FIXTURE_PATH = APP_ROOT / "fixtures/examples.json"


def sample_manifest() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def seed_examples(orchestrator, state_dir: Path) -> dict:
    manifest = sample_manifest()
    if orchestrator.history.list_sessions():
        raise ValueError("Examples require an empty workspace")
    history = orchestrator.history
    for session in manifest["sessions"]:
        ids = [
            history.add(session["id"], message["role"], message["content"])
            for message in session["messages"]
        ]
        if orchestrator.belief_repository is not None:
            service = BeliefUpdateService(
                orchestrator.belief_repository, extractor_version="demo-seed-v1"
            )
            for belief in session.get("beliefs", []):
                index = belief["message_index"]
                service.apply(
                    owner_agent_id=orchestrator.agent_id,
                    session_id=session["id"],
                    source_message_id=ids[index],
                    user_text=session["messages"][index]["content"],
                    observed_at=datetime.now(timezone.utc),
                    timezone_name=orchestrator.timezone_name,
                    source_sender_id=orchestrator.local_human_id,
                    source_sender_display_name=orchestrator.local_human_name,
                    candidates=BeliefCandidateBatch.model_validate(
                        {
                            "operations": [
                                {
                                    "operation": "ASSERT",
                                    "subject_id": orchestrator.local_human_id,
                                    "subject_reference": "My",
                                    "predicate": belief["predicate"],
                                    "value": belief["value"],
                                    "evidence_excerpt": belief["evidence"],
                                    "visibility": "AGENT_CURRENT",
                                    "expiry_policy": "NO_AUTOMATIC_EXPIRY",
                                }
                            ]
                        }
                    ),
                    existing_beliefs=orchestrator.belief_repository.get_visible(
                        orchestrator.agent_id, session["id"]
                    ),
                )
    memory = orchestrator.memory_retriever.memory
    for item in manifest["memories"]:
        memory.add(**item)
    # add() deliberately preserves canonical history if indexing fails. Require a
    # successful reconciliation before exposing the prepared workspace.
    history.reconcile_index()
    memory.reconcile_index()
    (state_dir / "seed.json").write_text(
        json.dumps({"version": manifest["version"], "complete": True})
    )
    return manifest
