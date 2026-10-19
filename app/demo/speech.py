"""Demo HTTP/lifecycle adapter around migrated speech engines and AudioDelivery."""

import threading
import uuid
from pathlib import Path

from app.paths import STATE_ROOT, owned_state_path
from app.stt.factory import build_stt_engine
from app.tts.base import TTS
from app.tts.delivery import AudioDelivery
from app.tts.factory import build_tts_engine


class LazyTTS(TTS):
    def __init__(self, settings):
        self.settings = settings
        self.engine = None

    def synthesize(self, text: str, output_path: Path) -> None:
        if self.engine is None:
            self.engine = build_tts_engine(self.settings)
        if self.engine is None:
            raise RuntimeError("Speech output is disabled")
        self.engine.synthesize(text, output_path)


class SpeechServices:
    def __init__(self, config):
        self.config = config
        self.delivery = AudioDelivery(
            LazyTTS(config.tts), queue_size=2, timeout=120, shutdown_timeout=5
        )
        self.stt = None
        self.stt_lock = threading.Lock()
        self.staging = owned_state_path(STATE_ROOT / "speech-staging")

    def start(self):
        self.delivery.start()

    async def close(self):
        await self.delivery.close()

    def transcribe(self, audio: bytes):
        if not self.stt_lock.acquire(blocking=False):
            raise RuntimeError("Speech recognition is busy")
        try:
            if self.stt is None:
                self.stt = build_stt_engine(self.config.stt)
            if self.stt is None:
                raise RuntimeError("Speech input is disabled")
            return self.stt.transcribe(audio)
        finally:
            self.stt_lock.release()

    async def synthesize(self, text: str, session_id: str) -> bytes:
        self.staging.mkdir(parents=True, exist_ok=True)
        target = owned_state_path(self.staging / (uuid.uuid4().hex + ".wav"))
        try:
            await self.delivery.synthesize(text, target, session_id)
            return target.read_bytes()
        finally:
            target.unlink(missing_ok=True)
