"""Exercise real SQLite, beliefs, seed/reset, and HTTP streaming with a fake model/index."""

import asyncio
import base64
import io
from types import SimpleNamespace
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.config import Config
from app.core import orchestrator_factory
from app.demo.runtime import DemoRuntime
from app.paths import STATE_ROOT, owned_state_path
from app.server import create_app
from tests.test_memory_store import FakeVectorStore


class Vectors(FakeVectorStore):
    def close(self):
        pass


class Model:
    def __init__(self, **kwargs):
        self.calls = []
        self.started = threading.Event()
        self.release = None
        self.fail = False
        self.command = None

    def close(self):
        pass

    def chat_buffered(self, messages, **kwargs):
        self.started.set()
        if self.release and not self.release.wait(5):
            raise TimeoutError("test model was not released")
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("Test model is unavailable")
        if self.command:
            command, self.command = self.command, None
            return {
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "shell__execute",
                            "arguments": {"command": command},
                        }
                    }
                ],
            }
        return {"content": "A practical plan from the demo model.", "tool_calls": []}

    def resolve_think_value(self, value=None):
        return False


class Speech:
    def __init__(self, config):
        self.started = threading.Event()
        self.release = None
        self.fail = False
        self.texts = []

    def start(self):
        pass

    async def close(self):
        pass

    def transcribe(self, audio):
        self.started.set()
        if self.release and not self.release.wait(5):
            raise TimeoutError("test did not release recognition")
        return SimpleNamespace(text="Editable English transcript", language="en")

    async def synthesize(self, text, session_id):
        self.texts.append(text)
        self.started.set()
        if self.release:
            await asyncio.to_thread(self.release.wait, 5)
        if self.fail:
            raise RuntimeError("speech unavailable")
        return b"RIFF-demo-wave"


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("app/config/assistant-template.yaml")
        self.patches = [
            patch.object(orchestrator_factory, "OllamaClient", Model),
            patch.object(
                orchestrator_factory, "VectorStore", side_effect=lambda **kw: Vectors()
            ),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.runtime = DemoRuntime(self.config)
        self.runtime.replace()
        self.addCleanup(self.runtime.close)

    def test_fictional_seed_correction_and_index_counts(self):
        current = self.runtime.current
        orch = current.orchestrator
        self.assertEqual(len(orch.history.list_sessions()), 3)
        beliefs = orch.belief_repository.get_visible(
            orch.agent_id, "sample-preferences"
        )
        self.assertEqual([b.value for b in beliefs], ["peppermint tea"])
        records, total = orch.belief_repository.list_for_inspection(orch.agent_id)
        self.assertEqual(total, 1)
        self.assertEqual(records[0]["revision"], 2)
        self.assertEqual(records[0]["value_json"], '"peppermint tea"')
        self.assertEqual(len(orch.history.collection.ids), 9)
        self.assertEqual(len(orch.memory_retriever.memory.collection.ids), 1)
        self.assertEqual(orch.llm.calls, [])
        self.assertTrue(
            json.loads((current.directory / "seed.json").read_text())["complete"]
        )
        names = [t["function"]["name"] for t in orch.tool_executor.get_native_tools()]
        self.assertTrue(
            all(name.startswith(("memory__", "beliefs__", "shell__")) for name in names)
        )
        self.assertIsNone(orch.autonomy_runtime)
        self.assertNotIn("avatar", orch.context_builder.system_prompt.lower())

    def test_repeated_reset_discards_demo_changes_and_preserves_external_sentinel(self):
        with tempfile.TemporaryDirectory() as external:
            sentinel = Path(external) / "untouched.txt"
            sentinel.write_text("outside demo")
            for _ in range(2):
                old = self.runtime.current
                old.orchestrator.history.add(
                    "extra-session", "user", "Disposable change"
                )
                old.orchestrator.memory_retriever.memory.add("Disposable memory")
                self.runtime.replace()
                self.assertNotEqual(old.id, self.runtime.current.id)
                self.assertFalse(old.directory.exists())
                self.assertEqual(
                    len(self.runtime.current.orchestrator.history.list_sessions()), 3
                )
                self.assertEqual(
                    len(
                        self.runtime.current.orchestrator.memory_retriever.memory.get_all()
                    ),
                    1,
                )
                self.assertEqual(sentinel.read_text(), "outside demo")

    def test_preparation_failure_leaves_old_workspace_usable(self):
        previous = self.runtime.current
        with patch(
            "app.demo.runtime.seed_examples", side_effect=RuntimeError("broken fixture")
        ):
            with self.assertRaisesRegex(RuntimeError, "broken fixture"):
                self.runtime.replace()
        self.assertIs(self.runtime.current, previous)
        self.assertTrue(previous.directory.exists())
        self.assertEqual(len(previous.orchestrator.history.list_sessions()), 3)

    def test_activation_failure_exposes_no_old_runtime_as_current(self):
        previous = self.runtime.current
        try:
            with patch.object(
                previous.orchestrator, "close", side_effect=RuntimeError("close failed")
            ):
                with self.assertRaisesRegex(RuntimeError, "close failed"):
                    self.runtime.replace()
            self.assertIsNone(self.runtime.current)
            self.assertTrue(previous.directory.exists())
        finally:
            previous.orchestrator.close()
            self.runtime._remove(previous.directory)

    def test_unsupported_capabilities_cannot_be_enabled_by_configuration(self):
        for name in ("mindcraft", "scratchpad"):
            with self.subTest(capability=name):
                config = Config("app/config/assistant-template.yaml")
                config.integrations[name]["enabled"] = True
                with self.assertRaisesRegex(ValueError, "not supported"):
                    orchestrator_factory.validate_demo_config(config)
        for section in ("autonomy",):
            config = Config("app/config/assistant-template.yaml")
            getattr(config, section)["enabled"] = True
            with self.assertRaisesRegex(ValueError, "not supported"):
                orchestrator_factory.validate_demo_config(config)

    def test_paths_refuse_external_targets_and_symlink_escapes(self):
        with self.assertRaises(ValueError):
            owned_state_path("/tmp/not-demo-owned")
        with self.assertRaises(ValueError):
            owned_state_path(STATE_ROOT / ".." / "outside")
        with tempfile.TemporaryDirectory() as external:
            link = self.runtime.current.directory / "escape"
            link.symlink_to(external, target_is_directory=True)
            try:
                with self.assertRaises(ValueError):
                    owned_state_path(link / "sentinel")
                with self.assertRaises(ValueError):
                    self.runtime._remove(self.runtime.current.directory)
            finally:
                link.unlink()
        with self.assertRaises(ValueError):
            self.runtime._remove(STATE_ROOT)


class DemoHTTPTests(unittest.TestCase):
    def setUp(self):
        config = Config("app/config/assistant-template.yaml")
        self.models = []

        def build_model(**kwargs):
            model = Model(**kwargs)
            self.models.append(model)
            return model

        for item in [
            patch.object(orchestrator_factory, "OllamaClient", side_effect=build_model),
            patch.object(
                orchestrator_factory, "VectorStore", side_effect=lambda **kw: Vectors()
            ),
            patch("app.server.setup_logging_from_config"),
        ]:
            item.start()
            self.addCleanup(item.stop)
        self.app = create_app(config, speech_builder=Speech)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.generation = self.client.get("/api/status").json()["generation"]
        self.headers = {"X-Demo-Generation": self.generation}

    def test_stream_persists_and_reset_fences_stale_clients(self):
        response = self.client.post(
            "/api/sessions/sample-workshop/messages",
            headers=self.headers,
            json={"text": "Please make a plan."},
        )
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertTrue(any(e["type"] == "user_message_accepted" for e in events))
        self.assertTrue(
            any(e["type"] == "speech" and e["is_final"] for e in events), events
        )
        self.assertEqual(events[-1]["type"], "done")
        history = self.client.get(
            "/api/sessions/sample-workshop", headers=self.headers
        ).json()
        self.assertEqual(history["messages"][-1]["role"], "assistant")
        self.assertEqual(history["messages"][-2]["content"], "Please make a plan.")
        reset = self.client.post("/api/reset", headers=self.headers)
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(
            self.client.post("/api/reset", headers=self.headers).status_code, 409
        )
        self.assertEqual(
            self.client.get(
                "/api/knowledge/memories", headers=self.headers
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                "/api/sessions/sample-workshop/messages",
                headers=self.headers,
                json={"text": "stale"},
            ).status_code,
            409,
        )
        new_headers = {"X-Demo-Generation": reset.json()["generation"]}
        self.assertEqual(
            len(
                self.client.get(
                    "/api/sessions/sample-workshop", headers=new_headers
                ).json()["messages"]
            ),
            3,
        )

    def test_busy_turn_blocks_reset_and_reads_until_finalization(self):
        model = self.models[-1]
        model.release = threading.Event()
        results = []
        thread = threading.Thread(
            target=lambda: results.append(
                self.client.post(
                    "/api/sessions/sample-workshop/messages",
                    headers=self.headers,
                    json={"text": "Block for test"},
                )
            )
        )
        thread.start()
        try:
            self.assertTrue(model.started.wait(3))
            self.assertTrue(self.client.get("/api/status").json()["busy"])
            self.assertEqual(
                self.client.post("/api/reset", headers=self.headers).status_code, 409
            )
            self.assertEqual(
                self.client.post("/api/sessions", headers=self.headers).status_code, 409
            )
            self.assertEqual(
                self.client.get(
                    "/api/knowledge/memories", headers=self.headers
                ).status_code,
                409,
            )
        finally:
            model.release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status_code, 200)
        self.assertFalse(self.client.get("/api/status").json()["busy"])

    def test_manual_retry_reuses_accepted_message(self):
        model = self.models[-1]
        model.fail = True
        response = self.client.post(
            "/api/sessions/sample-workshop/messages",
            headers=self.headers,
            json={"text": "A message to retry"},
        )
        events = [json.loads(line) for line in response.text.splitlines()]
        failed = next(e for e in events if e["type"] == "turn_failure")
        self.assertEqual(len(model.calls), 1)
        model.fail = False
        retried = self.client.post(
            "/api/sessions/sample-workshop/messages",
            headers=self.headers,
            json={"retry_message_id": failed["user_message_id"]},
        )
        self.assertTrue(
            any(
                e["type"] == "speech" and e["is_final"]
                for e in map(json.loads, retried.text.splitlines())
            )
        )
        rows = self.client.get(
            "/api/sessions/sample-workshop", headers=self.headers
        ).json()["messages"]
        self.assertEqual(sum(r["content"] == "A message to retry" for r in rows), 1)
        self.assertIsNone(rows[-2]["retry_error"])
        self.assertEqual(rows[-1]["role"], "assistant")

    def test_reset_failure_retains_generation_and_releases_gate(self):
        with patch(
            "app.demo.runtime.seed_examples",
            side_effect=RuntimeError("failed preparation"),
        ):
            response = self.client.post("/api/reset", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            self.client.get("/api/status").json()["generation"], self.generation
        )
        self.assertEqual(
            self.client.get("/api/sessions", headers=self.headers).status_code, 200
        )

    def test_new_blank_conversation_is_listed_and_survives_page_refresh(self):
        created = self.client.post("/api/sessions", headers=self.headers).json()[
            "session_id"
        ]
        sessions = self.client.get("/api/sessions", headers=self.headers).json()[
            "sessions"
        ]
        row = next(row for row in sessions if row["session_id"] == created)
        self.assertEqual(row["message_count"], 0)
        self.assertEqual(
            self.client.get("/api/sessions/" + created, headers=self.headers).json()[
                "messages"
            ],
            [],
        )

    def test_removed_routes_stay_absent_and_mutations_require_generation(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        for path in [
            "/api/autonomy",
            "/api/admin/reflect",
            "/api/audio",
            "/static/animations/Idle/Idle_5.fbx",
            "/static/../.demo-state/assistant.db",
        ]:
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.post("/api/reset").status_code, 422)
        self.assertEqual(
            self.client.post(
                "/api/sessions",
                headers={**self.headers, "Origin": "https://example.com"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/sessions/sample-workshop/messages",
                headers=self.headers,
                json={
                    "text": "hi",
                    "attachments": [
                        {
                            "name": "broken.png",
                            "mimeType": "image/png",
                            "data": "invalid",
                        }
                    ],
                },
            ).status_code,
            422,
        )
        memories = self.client.get("/api/knowledge/memories", headers=self.headers)
        self.assertEqual(memories.json()["total"], 1)

    def test_images_use_base_validation_storage_and_current_generation_url(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (3, 2), "blue").save(buffer, format="PNG")
        image = {
            "name": "sample.png",
            "mimeType": "image/png",
            "data": base64.b64encode(buffer.getvalue()).decode(),
        }
        response = self.client.post(
            "/api/sessions/sample-workshop/messages",
            headers=self.headers,
            json={"attachments": [image]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        rows = self.client.get(
            "/api/sessions/sample-workshop", headers=self.headers
        ).json()["messages"]
        stored = rows[-2]["attachments"][0]
        self.assertNotIn("storage_path", stored)
        self.assertTrue(stored["url"].startswith("/api/attachments/" + self.generation))
        retrieved = self.client.get(stored["url"])
        self.assertEqual(retrieved.content, buffer.getvalue())
        self.assertEqual(retrieved.headers["content-type"], "image/png")
        self.assertTrue(
            any(message.get("images") for message in self.models[-1].calls[-1])
        )
        self.client.post("/api/reset", headers=self.headers)
        self.assertEqual(self.client.get(stored["url"]).status_code, 409)

    def test_speech_is_separate_from_committed_text_and_failure_preserves_answer(self):
        before = self.client.get(
            "/api/sessions/sample-workshop", headers=self.headers
        ).json()["messages"]
        answer = next(row for row in before if row["role"] == "assistant")
        route = "/api/sessions/sample-workshop/speech"
        self.assertEqual(
            self.client.post(
                "/api/transcribe", headers=self.headers, content=b""
            ).status_code,
            422,
        )
        transcript = self.client.post(
            "/api/transcribe", headers=self.headers, content=b"controlled-audio"
        )
        self.assertEqual(
            transcript.json(), {"text": "Editable English transcript", "language": "en"}
        )
        self.assertEqual(
            self.client.post(
                route, headers=self.headers, json={"message_id": answer["id"]}
            ).content,
            b"RIFF-demo-wave",
        )
        self.app.state.speech.fail = True
        self.assertEqual(
            self.client.post(
                route, headers=self.headers, json={"message_id": answer["id"]}
            ).status_code,
            503,
        )
        self.assertEqual(
            self.client.post(
                route, headers=self.headers, json={"message_id": before[0]["id"]}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                "/api/sessions/sample-workshop", headers=self.headers
            ).json()["messages"],
            before,
        )
        self.assertEqual(self.models[-1].calls, [])


class DemoCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model = None

        def build_model(**kwargs):
            self.model = Model(**kwargs)
            return self.model

        for item in [
            patch.object(orchestrator_factory, "OllamaClient", side_effect=build_model),
            patch.object(
                orchestrator_factory, "VectorStore", side_effect=lambda **kw: Vectors()
            ),
            patch("app.server.setup_logging_from_config"),
        ]:
            item.start()
            self.addCleanup(item.stop)
        self.app = create_app(
            Config("app/config/assistant-template.yaml"), speech_builder=Speech
        )
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://demo"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.generation = (await self.client.get("/api/status")).json()["generation"]
        self.headers = {"X-Demo-Generation": self.generation}

    async def test_disconnected_turn_keeps_gate_until_durable_completion(self):
        self.model.release = threading.Event()
        task = asyncio.create_task(
            self.client.post(
                "/api/sessions/sample-workshop/messages",
                headers=self.headers,
                json={"text": "Survive disconnection"},
            )
        )
        try:
            self.assertTrue(await asyncio.to_thread(self.model.started.wait, 3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue((await self.client.get("/api/status")).json()["busy"])
            self.assertEqual(
                (
                    await self.client.post("/api/reset", headers=self.headers)
                ).status_code,
                409,
            )
        finally:
            self.model.release.set()
            await asyncio.wait_for(asyncio.gather(*self.app.state.turn_tasks), 5)
        rows = (
            await self.client.get("/api/sessions/sample-workshop", headers=self.headers)
        ).json()["messages"]
        self.assertEqual([r["role"] for r in rows[-2:]], ["user", "assistant"])
        self.assertEqual(rows[-2]["content"], "Survive disconnection")

    async def test_disconnected_reset_owns_preparation_until_activation(self):
        from app.demo.seeds import seed_examples

        started, release = threading.Event(), threading.Event()

        def slow_seed(*args):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test did not release seed")
            return seed_examples(*args)

        with patch("app.demo.runtime.seed_examples", side_effect=slow_seed):
            task = asyncio.create_task(
                self.client.post("/api/reset", headers=self.headers)
            )
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue((await self.client.get("/api/status")).json()["busy"])
                self.assertEqual(
                    (
                        await self.client.get("/api/sessions", headers=self.headers)
                    ).status_code,
                    409,
                )
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(*self.app.state.turn_tasks), 5)
        status = (await self.client.get("/api/status")).json()
        self.assertFalse(status["busy"])
        self.assertNotEqual(status["generation"], self.generation)

    async def wait_for_approval(self):
        for _ in range(100):
            if self.app.state.approvals.pending:
                return next(iter(self.app.state.approvals.pending))
            await asyncio.sleep(0.01)
        self.fail("No approval was requested")

    async def test_shell_uses_original_executor_and_binds_approval_to_its_tab(self):
        self.model.command = "printf demo-shell"
        headers = {**self.headers, "X-Demo-Client": "origin-tab"}
        with patch("app.tools.bash_execution.subprocess.run") as execute:
            execute.return_value = SimpleNamespace(
                stdout="demo-shell", stderr="", returncode=0
            )
            task = asyncio.create_task(
                self.client.post(
                    "/api/sessions/sample-workshop/messages",
                    headers=headers,
                    json={"text": "Run the controlled command"},
                )
            )
            try:
                request_id = await self.wait_for_approval()
                self.assertFalse(execute.called)
                wrong = await self.client.post(
                    "/api/approvals/" + request_id,
                    headers={**headers, "X-Demo-Client": "another-tab"},
                    json={"approved": True},
                )
                self.assertEqual(wrong.status_code, 409)
                reply = await self.client.post(
                    "/api/approvals/" + request_id,
                    headers=headers,
                    json={"approved": True},
                )
                self.assertEqual(reply.status_code, 200)
                response = await asyncio.wait_for(task, 5)
                self.assertIn("tool_approval_request", response.text)
                self.assertEqual(execute.call_args.kwargs["shell"], True)
                self.assertEqual(execute.call_args.kwargs["timeout"], 15.0)
                self.assertEqual(
                    (
                        await self.client.post(
                            "/api/approvals/" + request_id,
                            headers=headers,
                            json={"approved": True},
                        )
                    ).status_code,
                    409,
                )
            finally:
                for pending in list(self.app.state.approvals.pending.values()):
                    if not pending.future.done():
                        pending.future.set_result(False)
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)

    async def test_disconnected_approval_denies_command_and_releases_gate(self):
        self.model.command = "printf should-not-run"
        with patch("app.tools.bash_execution.subprocess.run") as execute:
            task = asyncio.create_task(
                self.client.post(
                    "/api/sessions/sample-workshop/messages",
                    headers={**self.headers, "X-Demo-Client": "closing-tab"},
                    json={"text": "Ask for approval"},
                )
            )
            await self.wait_for_approval()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(asyncio.gather(*self.app.state.turn_tasks), 5)
            execute.assert_not_called()
            self.assertFalse(self.app.state.approvals.pending)
            self.assertFalse((await self.client.get("/api/status")).json()["busy"])

    async def test_reset_discards_late_speech_and_recognition(self):
        for route, payload in [
            ("/api/transcribe", {"content": b"controlled-audio"}),
            ("/api/sessions/sample-workshop/speech", {"json": {"message_id": 2}}),
        ]:
            speech = self.app.state.speech
            speech.started.clear()
            speech.release = threading.Event()
            headers = {
                "X-Demo-Generation": (await self.client.get("/api/status")).json()[
                    "generation"
                ]
            }
            if "speech" in route:
                rows = (
                    await self.client.get(
                        "/api/sessions/sample-workshop", headers=headers
                    )
                ).json()["messages"]
                payload = {
                    "json": {
                        "message_id": next(
                            row["id"] for row in rows if row["role"] == "assistant"
                        )
                    }
                }
            task = asyncio.create_task(
                self.client.post(route, headers=headers, **payload)
            )
            try:
                self.assertTrue(await asyncio.to_thread(speech.started.wait, 3))
                reset = await self.client.post("/api/reset", headers=headers)
                self.assertEqual(reset.status_code, 200)
            finally:
                speech.release.set()
            self.assertEqual((await asyncio.wait_for(task, 5)).status_code, 409)


if __name__ == "__main__":
    unittest.main()
