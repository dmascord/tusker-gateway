# Semantic Cache

The semantic response cache is opt-in with
`TUSKER_SEMANTIC_CACHE_ENABLED=true`. It is intentionally narrower than the
exact response cache:

- Requests must be authenticated, non-streaming, text-only, and free of tool
  definitions, tool choices, and tool history.
- With the default `TUSKER_SEMANTIC_CACHE_REQUIRE_DETERMINISTIC=true`, callers
  must send `temperature: 0`, with `top_p: 1` when present and no non-zero
  presence/frequency penalties, `n > 1`, or `logit_bias`.
- The `privacy` pool is excluded by default because its ZDR contract does not
  allow persistent response storage.
- Entries are scoped by a one-way caller/API-key fingerprint, pool, requested
  model, concrete provider/model, and forwarded generation options. The raw
  API key and prompt are not stored in metadata or logs.
- Responses containing native or normalized tool calls are never stored or
  replayed. Old unsafe entries live in a different versioned Chroma collection.

Runtime safety controls:

- `TUSKER_SEMANTIC_CACHE_LOCAL_FILES_ONLY=true` prevents startup from fetching
  a model from Hugging Face. The Docker image bakes the pinned
  `sentence-transformers/all-MiniLM-L6-v2` model revision, and the revision is
  part of the versioned Chroma collection name.
- `TUSKER_SEMANTIC_CACHE_MODEL_REVISION` pins the model snapshot. The shipped
  image uses revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`.
- The production image pins ChromaDB `1.5.9` and sentence-transformers
  `6.0.0`; update those versions together with the image-level validation.
- `TUSKER_SEMANTIC_CACHE_MAX_ENTRIES`, `...MAX_INPUT_CHARS`, and
  `...MAX_RESPONSE_BYTES` bound persistent growth and work per request.
- Embedding and Chroma operations run in a bounded worker pool with
  `TUSKER_SEMANTIC_CACHE_OPERATION_TIMEOUT_SECS`; cache failures fall through
  to the provider and do not fail the chat request.
- Budget preflight runs before either cache, and cache hits are recorded
  against the caller budget.

The production deployment keeps the flag off until a canary is explicitly
approved. A canary should use deterministic, non-sensitive requests and watch
semantic hit/miss, skip, error, and latency metrics before broad enablement.
