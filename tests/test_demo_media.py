import asyncio
import threading
import unittest
from unittest.mock import patch

from app.demo.approvals import ApprovalBroker


class ApprovalTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_and_absent_client_deny_without_leaking_approval(self):
        broker = ApprovalBroker(timeout_s=0.01)
        queue = asyncio.Queue()
        result = await broker.request(
            {"detail": "printf controlled"},
            generation="g",
            session_id="s",
            client_id="tab",
            queue=queue,
            disconnected=threading.Event(),
        )
        self.assertFalse(result)
        self.assertFalse(broker.pending)
        self.assertEqual((await queue.get())["type"], "tool_approval_request")
        self.assertEqual((await queue.get())["type"], "tool_approval_resolved")
        self.assertFalse(
            await broker.request(
                {},
                generation="g",
                session_id="s",
                client_id="",
                queue=queue,
                disconnected=threading.Event(),
            )
        )
        self.assertTrue(queue.empty())


class KokoroAdapterTests(unittest.TestCase):
    def test_factory_preserves_existing_tts_contract_without_loading_models(self):
        from app.tts.factory import build_tts_engine

        with patch("app.tts.kokoro_tts.KokoroTTS") as engine:
            self.assertIs(
                build_tts_engine(
                    {
                        "engine": "kokoro",
                        "kokoro": {
                            "voice": "af_heart",
                            "device": "cpu",
                            "lang_code": "a",
                            "speed": 1.0,
                        },
                    }
                ),
                engine.return_value,
            )
            engine.assert_called_once_with(
                voice="af_heart", device="cpu", lang_code="a", speed=1.0
            )
            self.assertIsNone(build_tts_engine({"enabled": False, "engine": "kokoro"}))
