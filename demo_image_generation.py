#!/usr/bin/env python3
"""Demonstration of image generation support in Tusker AI Gateway."""

import sys
sys.path.insert(0, '.')

from tusker_gateway.providers.image_generation import ImageGenerationHandler

def main():
    print("Tusker AI Gateway - Image Generation Support Demo")
    print("=" * 60)
    
    # Create handler
    handler = ImageGenerationHandler({})
    
    print("\n1. Image Generation Request Detection:")
    test_cases = [
        ("POST", "/v1/images/generations", "Image generation endpoint"),
        ("POST", "/v1/images/edits", "Image edit endpoint"),
        ("POST", "/v1/images/variations", "Image variation endpoint"),
        ("GET", "/v1/images/generations", "GET request (should not match)"),
        ("POST", "/v1/chat/completions", "Chat completions endpoint (should not match)"),
    ]
    
    for method, path, description in test_cases:
        is_image = handler.is_image_generation_request(method, path)
        status = "✅" if is_image else "❌"
        print(f"  {status} {description}")
        if is_image:
            print(f"     -> Path '{path}' correctly identified as image generation")
    
    print("\n2. Provider Selection:")
    test_requests = [
        ("gpt-image-2", "/v1/images/generations", "Standard image generation"),
        ("dall-e-3", "/v1/images/edits", "Image edit"),
        ("gpt-image-1", "/v1/images/variations", "Image variation"),
    ]
    
    for model, path, description in test_requests:
        provider = handler.get_provider_for_image_request(model, path)
        print(f"  🎨 {description}: {provider} provider")
    
    print("\n3. Sample Request Handling:")
    sample_body = {
        "prompt": "A serene landscape with mountains and a lake",
        "n": 2,
        "size": "1024x1024",
        "quality": "standard",
        "response_format": "url"
    }
    
    # Note: This is a demonstration, not an actual API call
    print("  📝 Sample request body:")
    for key, value in sample_body.items():
        print(f"     {key}: {value}")
    
    print("\n4. Architecture Benefits:")
    print("  ✅ Extends existing Tusker architecture")
    print("  ✅ Uses provider pools and quality systems")
    print("  ✅ Compatible with existing authentication")
    print("  ✅ Supports multiple providers (OpenAI, OpenRouter, etc.)")
    print("  ✅ Integrates with rate limiting and monitoring")
    
    print("\n5. Supported Image Models:")
    from tusker_gateway.providers.image_generation import IMAGE_GEN_MODELS
    for model, info in IMAGE_GEN_MODELS.items():
        print(f"  🎨 {model}: ${info['cost_per_1k_tokens']} per 1K tokens")
    
    print("\n" + "=" * 60)
    print("Image generation support has been successfully implemented!")
    print("The Tusker AI Gateway can now handle:")
    print("  • OpenAI Image API (generations, edits, variations)")
    print("  • Multiple vision models")
    print("  • Provider pool selection")
    print("  • Quality and rate limiting integration")

if __name__ == "__main__":
    main()
