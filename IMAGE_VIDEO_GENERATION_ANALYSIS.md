# Image and Video Generation Support Analysis

## Current State

### What exists (limited):
1. **Vision model detection** (`is_likely_vision_model`) in `copilot_constants.py`:
   - Heuristic based on model name markers: ("vision", "claude", "gemini", "gpt-4o", "gpt-5")
   - Used for Copilot header injection (`Copilot-Vision-Request: true`)

2. **Vision headers in passthrough**:
   - In `auth_strategies.py`, vision headers are added for Copilot requests
   - No actual image generation endpoints

3. **Provider Registry** (`DEFAULT_PROVIDER_REGISTRY` in `config.py`):
   - Includes providers: openai, openrouter, groq, zai, google, cerebras, cohere, minimax, synthetic, ollama-cloud, opencode-go, opencode-zen, openai-codex, github-copilot, github-copilot-enterprise, local-llm
   - All currently configured for chat completions only

4. **Pools configuration**:
   - Limited to GPT-5.x models (code, privacy, premium, swarm pools)
   - No image or video generation models

### Missing:
- **OpenAI Image Generation API**: `/v1/images/generations` (supports gpt-image-2, gpt-image-1, dall-e-3)
- **OpenAI Image Edit API**: `/v1/images/edits` (DALL-E 2/3)
- **OpenAI Variations API**: `/v1/images/variations` (DALL-E 2)
- **OpenRouter image models**: Multiple vision/generation models available via OpenRouter
- **Anthropic Claude image generation**: Claude 3.5 Sonnet+, Claude 3 Opus with vision
- **Google Gemini image generation**: Gemini 2.5 Flash Image, Gemini 3.1 Flash Image, Gemini 3 Pro Image
- **Video generation**: OpenAI (GPT-4o to video), Pika, Runway, and others

## OpenAI Image Generation API Capabilities

### Image Generation Models:
- `gpt-image-2` (latest, cheapest, supports text extraction, auto-scaling to 1024x1024)
- `gpt-image-1` (supports text extraction, can generate variations, transparent background)
- `dall-e-3` (HD quality, supports text, verbose explanations, multiple sizes)
- `dall-e-2` (older, smaller, faster, supports variations and edits)

### Request Parameters:
- `model`: One of the above
- `prompt`: Text description
- `n`: Number of images (1-10 for DALL-E 2/3, 1 for GPT Image)
- `size`: One of "256x256", "512x512", "1024x1024", "1792x1024", "1024x1792" (DALL-E 3), or "auto" (GPT Image)
- `quality`: "standard" or "hd" (DALL-E 3), "auto" or "low" or "medium" or "high" (GPT Image)
- `response_format`: "url" or "b64_json"
- `background`: "auto", "transparent", or "opaque"
- `moderation": "low"

### Response Structure:
```json
{
  "created": 1677672188,
  "data": [
    {
      "url": "https://...",  // if response_format="url"
      "b64_json": "...",    // if response_format="b64_json"
      "revised_prompt": "..." // Optional revised prompt
    }
  ]
}
```

## OpenRouter Image Models

From recent discovery, OpenRouter hosts these image models:
- `openai/gpt-5.4-image-2` (GPT-5 Image 2)
- `openai/gpt-5-image-mini` (GPT-5 Image Mini)
- `openai/gpt-5-image` (GPT-5 Image)
- `google/gemini-3.1-flash-lite-image` (Gemini 3.1 Flash Lite Image)
- `google/gemini-3.1-flash-image` (Gemini 3.1 Flash Image)
- `google/gemini-3-pro-image` (Gemini 3 Pro Image)
- `google/gemini-3.1-flash-image-preview` (Preview)
- `google/gemini-3-pro-image-preview` (Preview)
- `google/gemini-2.5-flash-image` (Gemini 2.5 Flash Image)
- `deepseek/deepseek-v4-flash-vision-exp` (DeepSeek Vision)

## Anthropic Image Generation

Anthropic Claude models support vision through Messages API:
- Claude 3.5 Sonnet (vision)
- Claude 3 Opus (vision)
- Claude 3.5 Haiku (no vision)
- Claude 3 Sonnet (no vision)

Vision requires image inputs in the format:
```json
{
  "role": "user",
  "content": [
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,...."}},
    {"type": "text", "text": "Describe the image..."}
  ]
}
```

## Video Generation Models

### OpenAI GPT-4o to Video:
- Model: `gpt-4o` with video generation capability

### Other Providers:
- **Pika**: Short-form video generation from text
- **Runway**: Professional video generation and editing
- **Seed**: AI video generation
- **Nine**: Text-to-video generation

## Architecture Integration Plan

### 1. Provider Registry Updates (`config.py`)
Add new provider entries:
```python
"openai-image": ProviderConfig(
    "openai-image", "bearer", 
    "https://api.openai.com", "/v1/images/generations",
    auth_env="OPENAI_API_KEY", zdr_ok=False
),

"openrouter-vision": ProviderConfig(
    "openrouter-vision", "bearer",
    "https://openrouter.ai/api/v1", "/chat/completions",
    auth_env="OPENROUTER_API_KEY", zdr_ok=False
),

"anthropic-vision": ProviderConfig(
    "anthropic-vision", "anthropic",
    "https://api.anthropic.com", "/v1/messages",
    auth_env="ANTHROPIC_API_KEY", zdr_ok=False
),

"google-vision": ProviderConfig(
    "google-vision", "bearer",
    "https://generativelanguage.googleapis.com", "/v1beta/openai/chat/completions",
    auth_env="GEMINI_API_KEY", zdr_ok=False
),

"video": ProviderConfig(
    "video", "bearer",
    "https://api.pika.video", "/v1/generate",  # Example
    auth_env="PIKA_API_KEY", zdr_ok=False
),
```

### 2. Route Resolution Updates (`routing.py`)
Add image and video generation route detection:
```python
IMAGE_GEN_MARKERS = ("/images/generations", "/v1/images/generations")
VIDEO_GEN_MARKERS = ("/videos", "/v1/video", "/generate")

def resolve_route(model: str | None, body: dict[str, Any]) -> Route:
    # Existing logic...
    
    # Check if this is an image generation request
    if any(marker in str(body.get("url", "")) or marker in str(model or "") for marker in IMAGE_GEN_MARKERS):
        return Route(kind="image_generation", provider="openai-image", model=model)
    
    # Check if this is a video generation request
    if any(marker in str(body.get("url", "")) or marker in str(model or "") for marker in VIDEO_GEN_MARKERS):
        return Route(kind="video_generation", provider="video", model=model)
    
    # Existing pool/swarm/passthrough logic...
```

### 3. Handler Implementation (`endpoints.py`)
Add new endpoint handlers:

#### Image Generation Handler:
```python
async def images_handler(request: web.Request) -> web.Response:
    """POST /v1/images/* — Image generation endpoint."""
    try:
        body = await request.json()
        route = resolve_route(body.get("model"), body)
        
        if route.kind == "image_generation":
            return await _handle_image_generation(request, body, route)
        
        # Fall through to existing handlers for other types
        # ...
    except Exception as exc:
        return web.json_response(openai_error(str(exc), code="provider_error", error_type="provider_error"), status=502)
```

#### Image Generation Implementation:
```python
async def _handle_image_generation(request: web.Request, body: dict[str, Any], route: Route) -> web.Response:
    """Handle image generation request."""
    provider = route.provider
    model = route.model
    
    # Extract API key and headers
    api_key = _resolve_api_key(request)
    
    # Build request to upstream provider
    upstream_url = f"{PROVIDER_ENDPOINTS[provider]['base_url']}{PROVIDER_ENDPOINTS[provider]['chat_path']}"
    
    # Special handling for OpenAI image generation
    if provider == "openai-image":
        # OpenAI Image API has different parameter names than chat completions
        # Map OpenAI-style parameters to the actual request
        request_body = {
            "model": model or body.get("model", "gpt-image-2"),
            "prompt": body.get("prompt", ""),
            "n": body.get("n", 1),
            "size": body.get("size", "1024x1024"),
            "quality": body.get("quality", "standard"),
            "response_format": body.get("response_format", "url"),
            "background": body.get("background", "auto"),
        }
        
        # Add optional parameters
        if "extra_body" in body:
            request_body.update(body["extra_body"])
    
    # For other providers, adapt the format
    else:
        # Convert to provider-specific format
        request_body = _adapt_image_request(body, provider, model)
    
    # Make request to upstream provider
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {api_key}"}
        if body.get("extra_headers"):
            headers.update(body["extra_headers"])
        
        async with session.post(upstream_url, json=request_body, headers=headers) as resp:
            if resp.status == 200:
                response_data = await resp.json()
                # Transform response if needed
                return web.json_response(_transform_image_response(response_data, provider))
            else:
                error_text = await resp.text()
                raise GatewayError(f"Image generation failed: {error_text}", code="provider_error", error_type="provider_error")
```

### 4. Pool Configuration Updates (`config.py`)
Add image and video generation pools:
```python
if "image" not in pools:
    pools["image"] = PoolConfig(
        name="image",
        models=[
            {"provider": "openai-image", "model": "gpt-image-2"},
            {"provider": "openai-image", "model": "dall-e-3", "heavyweight": True},
            {"provider": "openrouter-vision", "model": "openai/gpt-5.4-image-2"},
            {"provider": "google-vision", "model": "gemini-3.1-flash-image"},
        ],
        zdr=False,
    )

if "video" not in pools:
    pools["video"] = PoolConfig(
        name="video",
        models=[
            {"provider": "video", "model": "gpt-4o-to-video"},
            {"provider": "video", "model": "pika", "heavyweight": True},
        ],
        zdr=False,
    )
```

### 5. Vision Model Pool Aliases
Add virtual role aliases for image generation:
```python
POOL_ALIASES.update({
    "hermes-vision": "image",
    "hermes-video": "video",
})
```

### 6. Authentication Strategy Updates (`auth_strategies.py`)
Add image generation auth:
```python
def _vision_auth_strategy(request: web.Request, model: str, provider: str, api_key: str) -> dict[str, str]:
    """Authentication strategy for vision/image generation requests."""
    headers = {"Authorization": f"Bearer {api_key}"}
    
    # Add provider-specific headers
    if provider == "github-copilot":
        is_vision = is_likely_vision_model(model)
        if is_vision:
            headers["Copilot-Vision-Request"] = "true"
    
    # Add image generation specific headers
    if provider == "openai-image":
        headers["Content-Type"] = "application/json"
    
    return headers
```

### 7. Quality and Metrics Updates
- Update quality tracking to include image generation metrics
- Add image-specific metrics (generation time, token usage)
- Update dashboard to show image/video generation metrics

### 8. Tests
Create comprehensive tests for image/video generation:
- Test OpenAI image generation endpoint
- Test OpenRouter vision models
- Test Anthropic vision generation
- Test video generation endpoints
- Test error handling and rate limiting
- Test authentication for image generation

## Implementation Priority

1. **Phase 1**: OpenAI Image Generation (easiest - well-documented API)
2. **Phase 2**: OpenRouter Vision Models (leveraging existing discovery)
3. **Phase 3**: Anthropic and Google Vision (existing vision support, add image generation)
4. **Phase 4**: Video Generation (more complex API requirements)

## Key Implementation Challenges

1. **Parameter Mapping**: Different providers use different parameter names and structures
2. **Response Transformation**: Need to convert provider-specific responses to consistent format
3. **Authentication**: Different auth mechanisms (Bearer, API Key, OAuth, Anthropic-specific)
4. **Rate Limiting**: Need separate rate limiting for image/video generation
5. **Cost Tracking**: Different pricing models (per image vs per token)

## Next Steps

1. Implement provider registry updates
2. Add route resolution for image/video generation
3. Create base handler framework
4. Implement OpenAI image generation support
5. Add tests and validation
6. Extend to other providers

This implementation will enable Tusker to support image generation and video generation across multiple providers while maintaining consistency with the existing architecture.