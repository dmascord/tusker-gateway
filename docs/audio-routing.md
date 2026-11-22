# Audio routing (OpenAI-compatible)

The gateway speaks the OpenAI audio contract in both directions. This matters
for clients that use voice to control models: the same session can send audio
in chat, get a transcript, and ask for spoken output.

## Endpoints

| Endpoint | Direction | Request | Response |
|---|---|---|---|
| `POST /v1/audio/speech` | text → speech | JSON (`model`, `input`, `voice`, optional `response_format`, …) | binary `audio/*` |
| `POST /v1/audio/transcriptions` | speech → text | `multipart/form-data` (`file`, `model`, optional `language`, `prompt`, `response_format`, `temperature`) | JSON `{"text": ...}` |
| `POST /v1/chat/completions` | audio input | `input_audio` / `audio_url` content parts | normal assistant turn |

All three require the `inference:audio` scope where key-scoped policies apply,
and pass through the shared media preflight (rate limit, budget, guardrails).

## Provider selection

### TTS (`providers/tts.py`)
Explicit `provider::model` / `provider/model` pins win (`openai`, `groq`,
`openrouter`, `workers-ai`, `xiaomi`). Otherwise the capability registry's
`tts_speech` table is consulted, then a bare model defaults to OpenAI.

### STT (`providers/stt.py`)
The same shape: `groq::whisper-large-v3-turbo`, `openai/whisper-1`, or an
`openrouter/...` pin selects the upstream; otherwise `speech_transcription`
capability entries decide. Bare models default to OpenAI. Uploads are capped
at 25 MiB (the OpenAI limit). Upstream bodies are never echoed to clients.

Groq's catalog is probed for both speech-output models (`output_modalities`
contains `speech`) and Whisper transcription models, producing entries for
both capabilities from one read-only request.

### Chat audio input
`_required_input_modalities()` already recognises `audio_url`, `input_audio`
and `audio` content parts as an `audio` requirement, so pool selection applies
the same evidence gates as for images. `estimate_prompt_tokens()` adds
`TUSKER_AUDIO_TOKEN_ESTIMATE` (default 1000) per audio part; audio payloads are
not decoded for accounting. Audio content parts are forwarded unchanged.

Audio-only generators (e.g. `google/lyria-3-*`, whose catalog advertises
`output_modalities: ["text","audio"]`) are excluded from chat pools — they are
media endpoints, not assistant backends.

## Capability evidence

Successful media requests record `output_audio` / `tts_speech` (TTS) and
`input_audio` / `speech_transcription` (STT) through the shared media
capability recorder. Modality qualification can also probe `input_audio`
(`python -m tusker_gateway.modality_qualification --input-modality audio
--include-unadvertised`) to promote discovered audio-input routes.

## History

`/v1/audio/speech` originally shipped in `e93588b`, but its router line was
removed in `e3f0b5b` while `/v1/embeddings` was added, leaving the handler as
dead code (live requests 404'd). The 2026-11-23 work restored it, added the
STT endpoint, and added audio token accounting. See
`docs/incidents/2026-11-23-audio-routing.md`.

## Verification (live, 2026-11-23)

- `POST /v1/audio/speech` with `groq::canopylabs/orpheus-v1-english`,
  `response_format: wav` → HTTP 200, 88 KB RIFF/WAVE payload.
- `POST /v1/audio/transcriptions` with a 0.6 s WAV and
  `groq::whisper-large-v3-turbo` → HTTP 200, `{"text": " you"}`.
- Chat with an `input_audio` part via `hermes-code` → HTTP 200; the model
  identified the test signal as "a short, high-pitched pure electronic tone
  (a sine wave)". Direct passthrough to an OpenRouter audio model also 200.
- Offline suite: 2217 passed, 8 skipped.
