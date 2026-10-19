"""Stock-voice Kokoro adapter implementing Astra's existing TTS interface."""

from pathlib import Path

from app.tts.base import TTS


class KokoroTTS(TTS):
    def __init__(self, voice="af_heart", lang_code="a", device="cpu", speed=1.0):
        from kokoro import KPipeline

        self.voice = voice
        self.speed = speed
        self.pipeline = KPipeline(
            lang_code=lang_code, device=device, repo_id="hexgrad/Kokoro-82M"
        )

    def synthesize(self, text: str, output_path: Path) -> None:
        import numpy as np
        import soundfile as sf

        chunks = []
        for _, _, audio in self.pipeline(text, voice=self.voice, speed=self.speed):
            if audio is not None:
                chunks.append(np.asarray(audio, dtype=np.float32))
        if not chunks:
            raise ValueError("Kokoro returned no audio")
        sf.write(str(output_path), np.concatenate(chunks), 24000, subtype="PCM_16")
