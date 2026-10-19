# Speech output

`KokoroTTS` implements the existing `TTS.synthesize(text, output_path)` interface.
The demo selects stock English `af_heart` on CPU, producing 24 kHz PCM WAV output.
The original AudioDelivery worker owns queueing, deadline, quarantine and cleanup.
Speech text preparation is extracted from the base server. Existing engine adapters
remain for upstream compatibility; optional backend dependencies are not installed
by the demo. The UI exposes one stock voice, not custom/cloned voices.
