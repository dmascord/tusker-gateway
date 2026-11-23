## 2026-11-23 — Standards-based audio routing (voice input + output)

### Why

A user relies on voice to control models, so both audio input and output
matter. The gateway advertised an OpenAI-compatible TTS handler but did not
actually expose it.

### Root cause

`e93588b` registered `app.router.add_post("/v1/audio/speech", tts_handler)`.
`e3f0b5b` removed that line while adding `/v1/embeddings`. The `tts_handler`
function and `app["tts_handler"]` remained, so live requests to
`/v1/audio/speech` returned 404. There was no STT endpoint at all, and prompt
token estimation had no audio accounting (audio parts were stringified as raw
base64 JSON).

### Fix

- `app.py` / `identity.py`: restored `/v1/audio/speech`, added
  `/v1/audio/transcriptions`, both with `inference:audio` scope.
- `providers/stt.py` (new): `STTHandler` with provider pinning, capability
  lookup, 25 MiB upload cap, multipart forwarding to OpenAI / Groq /
  OpenRouter transcription endpoints, and sanitized errors.
- `providers/capabilities.py`: added `SPEECH_TRANSCRIPTION`; OpenAI static
  Whisper models; Groq catalog discovery now yields both `tts_speech` and
  speech-transcription entries from one probe (old `_discover_groq_tts` kept
  as a TTS-only wrapper for compatibility).
- `endpoints.py`: `transcription_handler` parses multipart, runs the shared
  media preflight/budget/capability recording, and returns upstream JSON.
- `context_limits.py`: audio content parts are counted explicitly with
  `TUSKER_AUDIO_TOKEN_ESTIMATE` (default 1000) instead of being stringified.
- `config_runtime.py`: rebuild path also installs `stt_handler`.

Nine new tests in `tests/test_audio_routing.py`; full offline suite 2217 passed.

### Deployed and verified

Source `0e7abc3492ed28a59a3f9588a0bcb35cf05fc1af`, image
`sha256:17fc0f8439a093798b50dd2b8b2ad6d368e47dd27cbb6952f3e10d273fd46615`;
provenance OK and smoke 200/200. Live checks:

- TTS: Groq Orpheus, `response_format: wav` → 200, 88 KB RIFF/WAVE.
- STT: 0.6 s sine WAV, Groq Whisper turbo → 200 `{"text": " you"}`.
- Chat audio via `hermes-code` → 200; assistant identified the sine tone
  correctly. 14 audio-capable routes exist in `code`, 0 in `privacy`.

### Notes

- Groq Orpheus only accepts `response_format: wav`; the existing TTS
  normalizer still injects another default when the client omits the field,
  so callers must set it explicitly for that provider.
- Audio-modality evidence is mostly `advertised` in catalogs; routes whose
  catalog advertises audio are selectable. Running the audio modality probe
  (`--input-modality audio --include-unadvertised`) can promote more.
