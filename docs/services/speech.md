---
title: Speech
description: English dictation and speech playback in Astra Demo.
---

# Speech

The demo uses Faster-Whisper for speech recognition and Kokoro for English
speech output. Both run on CPU with the checked-in template.

## Dictate a message

Select **Dictate** and grant microphone access. Select **Stop recording** and
review the editable transcript before sending it. A transcription can finish
without creating a chat turn.

The first transcription may download the Whisper `small` model. The default
settings use CPU, INT8 computation, and voice activity detection. Set
`stt.enabled: false` to turn off dictation.

## Read a reply

Select **Read aloud** on a saved reply, or enable **Speak replies · English** for
new replies. Use **Stop voice** or **Resume voice** to control playback.

The first synthesis may download Kokoro weights, the stock `af_heart` voice, and
the English spaCy model. English fallback pronunciation can require
`espeak-ng`. Change the voice under `tts.kokoro` or set `tts.enabled: false`
to turn off speech output.

The speech endpoint takes a committed reply and returns audio to the requesting
browser. Text replies remain available if synthesis fails. See
[Runtime lifecycle](../architecture/runtime.md#speech-delivery-deadlines) for the
queue and timeout behavior.
