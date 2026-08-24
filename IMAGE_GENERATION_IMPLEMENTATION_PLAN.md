# Image Generation Implementation Plan

## Summary
The Tusker AI Gateway currently only supports chat completions via provider pools and passthrough routing. It has some vision model detection via `is_likely_vision_model()` but no dedicated image generation endpoints.

OpenAI launched GPT Image models (gpt-image-2, gpt-image-1, gpt-image-1-mini) with dedicated Image API endpoints (`/v1/images/generations`, `/v1/images/edits`, `/v1/images/variations`). This implementation will add image generation support to the gateway.

## Implementation Phases

### Phase 1: OpenAI Image Generation (Current Focus)
**Goal**: Add support for OpenAI Image API (`/v1/images/generations`, `/v1/images/edits`, `/v1/images/variations`)
**Priority**: HIGH - Well-documented API, high demand

**Changes Needed**:
1. **Provider Registry Updates** (`config.py`)
   - Add "openai-image" provider for generations
   - Add "openai-edit" provider for edits  
   - Add "openai-variation" provider for variations
   - Set correct base URLs and chat_paths

2. **Route Resolution** (`routing.py`)
   - Detect image generation routes from URL paths or model parameters
   - Return appropriate Route kind ("image_generation", "image_edit", "image_variation")

3. **Handler Implementation** (`endpoints.py`)
   - Create `images_handler()` for `/v1/images/*` routes
   - Implement `_handle_image_generation()`, `_handle_image_edit()`, `_handle_image_variation()`
   - Add request validation specific to image generation

4. **Pool Configuration** (`config.py`)
   - Add "image" pool with OpenAI image models (gpt-image-2, gpt-image-1, gpt-image-1-mini)
   - Add "vision" virtual role alias

5. **Authentication** (`auth_strategies.py`, `copilot_exchange.py`)
   - Update vision detection for image generation models
   - Ensure proper headers for vision requests

6. **Quality and Metrics**
   - Add image generation metrics
   - Update quality tracking for image models

### Phase 2: OpenRouter Vision Models
**Goal**: Add support for OpenRouter-hosted vision models
**Priority**: MEDIUM - Good selection available, leverages existing discovery

**Changes**:
1. **Provider Registry** - Add openrouter-vision provider
2. **Route Resolution** - Detect OpenRouter vision models  
3. **Handler Implementation** - Support OpenRouter image generation API format
4. **Pool Configuration** - Add vision models to image pool

### Phase 3: Anthropic Vision Models
**Goal**: Add full support for Anthropic Claude vision models (Claude 3.5 Sonnet+, Claude 3 Opus)
**Priority**: MEDIUM - Already has vision detection, needs image generation

**Changes**:
1. **Anthropic Adapter** (`anthropic_adapter.py`)
   - Add image generation tool support to Anthropic Messages API
   - Handle image inputs and outputs in Anthropic format
2. **Provider Registry** - Add anthropic-vision provider
3. **Route Resolution** - Detect Anthropic vision routes

### Phase 4: Video Generation
**Goal**: Add video generation support (OpenAI GPT-4o to video, Pika, Runway, etc.)
**Priority**: LOW - More complex, may be implemented by users separately

## Technical Architecture

### Image Generation Flow

1. **Request Routing**
   - Client sends request to `/v1/images/generations`, `/v1/images/edits`, or `/v1/images/variations`
   - `resolve_route()` detects image generation type and provider
   - Route returns appropriate kind ("image_generation", "image_edit", "image_variation")

2. **Provider Selection**
   - For image generation, use dedicated image pools (not chat pools)
   - Quality-based ranking with custom metrics for image generation
   - Support session stickiness for consistent model selection

3. **Request Transformation**
   - Map OpenAI Image API parameters to provider-specific format
   - Handle different response formats (URL vs base64)
   - Transform provider responses to standard gateway format

4. **Response Processing**
   - Convert provider responses to standard format
   - Extract image data, metadata, usage statistics
   - Include quality metrics for image generation

### Key Design Decisions

1. **Separate Pools for Image Generation**
   - Image generation has different QoS requirements (cost, latency, memory)
   - Separate pools prevent chat generation models from being used for images
   - Easier to manage and optimize for different use cases

2. **Hybrid Routing**
   - Virtual role "hermes-vision" routes to image pool
   - Provider-prefixed models (e.g., "openai-image::gpt-image-2") for direct passthrough
   - Maintain backward compatibility with existing chat models

3. **Provider-Agnostic Handler Base**
   - Common image generation handler with provider-specific adapters
   - Simplify adding new providers
   - Consistent error handling and metrics collection

## Implementation Timeline

### Week 1: Phase 1 Setup
- [ ] Provider registry updates for OpenAI image endpoints
- [ ] Route resolution for image generation detection
- [ ] Pool configuration for image pool
- [ ] Basic test infrastructure

### Week 2: Phase 1 Core
- [ ] Images handler implementation
- [ ] OpenAI API request transformation
- [ ] Response processing and transformation
- [ ] Basic unit tests

### Week 3: Phase 1 Validation
- [ ] Integration tests with OpenAI mock
- [ ] Error handling and validation
- [ ] Quality and metrics integration
- [ ] Documentation and examples

### Week 4: Phase 2 Preparation
- [ ] OpenRouter provider configuration
- [ ] Additional pool entries for vision models
- [ ] Test infrastructure updates

## Files to Modify

### Core Changes
1. `tusker_gateway/config.py` - Provider registry, pool configuration
2. `tusker_gateway/routing.py` - Route resolution for image generation
3. `tusker_gateway/endpoints.py` - Images handler and helper methods
4. `tusker_gateway/auth_strategies.py` - Vision header injection

### Optional Extensions
5. `tusker_gateway/pools.py` - Image generation-specific pool logic
6. `tusker_gateway/quality.py` - Image generation quality metrics
7. `tusker_gateway/anthropic_adapter.py` - Anthropic vision support (Phase 3)

## Testing Strategy

### Unit Tests
- Provider configuration validation
- Route resolution for different image endpoints
- Request/response transformation
- Error handling (400, 429, 500)

### Integration Tests
- Mock OpenAI Image API calls
- Provider fallback behavior
- Quality ranking for image models
- Session stickiness

### End-to-End Tests
- Full gateway image generation flow
- Multiple provider scenarios
- Rate limiting and error recovery

## Security Considerations

1. **API Key Protection**
   - Image generation keys stored separately from chat keys
   - Different environment variable names

2. **Usage Monitoring**
   - Separate metrics for image generation
   - Cost tracking per image (different pricing model)

3. **Content Filtering**
   - Image generation has different safety requirements
   - Separate guardrails configuration

## Deployment Considerations

1. **Configuration**
   - Add TUSKER_IMAGE_API_KEY environment variable
   - Image pool configuration in TUSKER_POOL_IMAGE
   - Optional provider override via PROVIDER_REGISTRY_JSON

2. **Monitoring**
   - Add image generation metrics to dashboard
   - Separate rate limits for image generation
   - Cost tracking and alerting

## Next Steps

1. **Immediate** - Start Phase 1 with OpenAI Image Generation
2. **Short-term** - Add OpenRouter vision support (Phase 2)  
3. **Medium-term** - Anthropic vision support (Phase 3)
4. **Long-term** - Video generation support (Phase 4)

This implementation will enable Tusker to support image generation while maintaining the existing architecture and backward compatibility.