"""Video generation provider support for Tusker AI Gateway.

Supports OpenAI Sora, OpenRouter video models, Google Veo, MiniMax H3, and
Z.AI video generation. These upstream APIs are asynchronous: create returns a
job or operation which is polled until completion. OpenAI/OpenRouter/Google
content is downloaded only from fixed credential origins; MiniMax and Z.AI
return signed result URLs without making the gateway fetch another host.
"""

import asyncio
import base64
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import aiohttp

from tusker_gateway.errors import GatewayError

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 5.0  # seconds
DEFAULT_MAX_WAIT = 300.0    # seconds
MAX_VIDEO_BYTES = 32 * 1024 * 1024


async def _read_video_bytes(resp: aiohttp.ClientResponse) -> bytearray:
    """Read one video response without allowing unbounded memory growth."""
    content_length = resp.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_VIDEO_BYTES:
                raise GatewayError(
                    "Generated video exceeds the gateway response-size limit",
                    code="upstream_error",
                )
        except ValueError:
            pass

    data = bytearray()
    async for chunk in resp.content.iter_chunked(1024 * 1024):
        if len(data) + len(chunk) > MAX_VIDEO_BYTES:
            raise GatewayError(
                "Generated video exceeds the gateway response-size limit",
                code="upstream_error",
            )
        data.extend(chunk)
    return data

_OPENROUTER_HOST = "openrouter.ai"
_GOOGLE_CREDENTIAL_HOST = "generativelanguage.googleapis.com"


def _is_origin(url: str, origin: str) -> bool:
    """Match exactly one HTTPS origin before attaching provider credentials."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() == origin
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
    )

VIDEO_PROVIDER_PINS = frozenset({"openai", "openrouter", "google", "minimax", "zai"})

class VideoHandler:
    """Dispatch video generation requests to supported upstream providers."""

    def __init__(
        self,
        config: Dict[str, Any],
        capability_registry: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.capability_registry = capability_registry

    def is_video_request(self, path: str, body: Dict[str, Any]) -> bool:
        return path in ("/v1/videos", "/v1/video/generations")

    def get_provider_for_video_request(
        self,
        model: str,
        capability_registry: Optional[Any] = None,
    ) -> str:
        pinned_provider, _ = self._split_gateway_pin(model)
        if pinned_provider is not None:
            return pinned_provider
        if "/" in model:
            return "openrouter"

        registry = (
            capability_registry
            if capability_registry is not None
            else self.capability_registry
        )
        if registry is not None:
            try:
                snap = registry.snapshot
                if any(snap.capabilities.values()):
                    from tusker_gateway.providers.capabilities import Capability

                    entry = snap.lookup(Capability.VIDEO_GENERATIONS, model)
                    if entry is not None:
                        return entry.provider
            except Exception as exc:  # noqa: BLE001
                logger.debug("capability registry lookup failed, falling back: %s", exc)

        model_lower = model.lower()
        if model_lower.startswith("veo-"):
            return "google"
        if model_lower.startswith("minimax-h3"):
            return "minimax"
        # Z.ai's own video models (CogVideoX, Vidu). Slugs are unambiguous.
        if model_lower.startswith(("cogvideox-", "vidu", "viduq1")):
            return "zai"
        return "openai"

    async def handle_request(
        self,
        model: str,
        body: Dict[str, Any],
        api_key: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        wait: bool = True,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_wait: float = DEFAULT_MAX_WAIT,
    ) -> Dict[str, Any]:
        provider = self.get_provider_for_video_request(model)
        if provider == "openrouter":
            return await self._call_openrouter(
                model, body, api_key, extra_headers,
                wait, poll_interval, max_wait,
            )
        self._require_prompt(body)
        if provider == "google":
            return await self._call_google(
                model, body, api_key, extra_headers,
                wait, poll_interval, max_wait,
            )
        if provider == "minimax":
            return await self._call_minimax(
                model, body, api_key, extra_headers,
                wait, poll_interval, max_wait,
            )
        if provider == "zai":
            return await self._call_zai(
                model, body, api_key, extra_headers,
                wait, poll_interval, max_wait,
            )
        if provider == "openai":
            return await self._call_openai(
                model, body, api_key, extra_headers,
                wait, poll_interval, max_wait,
            )
        raise GatewayError(
            f"Video generation is not supported for provider '{provider}'",
            code="unsupported_provider",
        )

    @staticmethod
    def _split_gateway_pin(model: str) -> tuple[Optional[str], str]:
        """Return a known gateway provider pin and the untouched upstream id."""
        if "::" in model:
            provider, _, upstream_model = model.partition("::")
            if provider.lower() in VIDEO_PROVIDER_PINS and upstream_model:
                return provider.lower(), upstream_model
        if "/" in model:
            provider, _, upstream_model = model.partition("/")
            if provider.lower() == "openrouter" and upstream_model:
                return "openrouter", upstream_model
        return None, model

    @classmethod
    def _strip_provider_prefix(cls, model: str) -> str:
        """Strip one known gateway pin while preserving upstream namespaces."""
        _, upstream_model = cls._split_gateway_pin(model)
        return upstream_model

    @staticmethod
    def _require_prompt(body: Dict[str, Any]) -> str:
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise GatewayError(
                "video request missing required 'prompt'", code="bad_request"
            )
        return prompt

    @classmethod
    def _normalise_request(cls, model: str, body: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "prompt": cls._require_prompt(body),
        }
        for key in ("size", "seconds"):
            if key in body and body[key] is not None:
                payload[key] = body[key]
        return payload

    # ----- OpenAI -----

    async def _call_openai(
        self,
        model: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
        wait: bool = True,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_wait: float = DEFAULT_MAX_WAIT,
    ) -> Dict[str, Any]:
        if not api_key:
            raise GatewayError(
                "OpenAI API key required for video", code="missing_api_key"
            )
        if body.get("input_reference") is not None:
            raise GatewayError(
                "OpenAI input_reference requires a multipart file upload, which "
                "cannot be represented by this JSON video request",
                code="unsupported_parameter",
            )
        url = "https://api.openai.com/v1/videos"
        headers = {"Authorization": f"Bearer {api_key}"}
        if extra_headers:
            headers.update(
                key_value
                for key_value in extra_headers.items()
                if key_value[0].lower() != "content-type"
            )
        payload = self._normalise_request(
            self._strip_provider_prefix(model), body
        )
        form = aiohttp.FormData()
        for key in ("model", "prompt", "size", "seconds"):
            value = payload.get(key)
            if value is not None:
                form.add_field(key, str(value), content_type="text/plain")
        timeout = aiohttp.ClientTimeout(total=max_wait + 60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, headers=headers, data=form, allow_redirects=False
            ) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:500]
                    logger.warning(
                        "OpenAI video create failed: %s %s", resp.status, err_text
                    )
                    raise GatewayError(
                        f"OpenAI video error {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                job = await resp.json()
            job_id = job.get("id")
            if not job_id:
                raise GatewayError(
                    f"OpenAI video response missing id: {job}",
                    code="upstream_error",
                )
            if not wait:
                return job
            if job.get("status") != "completed":
                job = await self._poll_openai(
                    session, job_id, api_key, poll_interval, max_wait
                )
            return await self._finalise_openai(session, job_id, api_key, job)

    async def _poll_openai(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        api_key: str,
        poll_interval: float,
        max_wait: float,
    ) -> Dict[str, Any]:
        url = f"https://api.openai.com/v1/videos/{job_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        deadline = asyncio.get_event_loop().time() + max_wait
        backoff = poll_interval
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise GatewayError(
                    f"OpenAI video job {job_id} did not finish within {max_wait}s",
                    code="timeout",
                )
            async with session.get(
                url, headers=headers, allow_redirects=False
            ) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:300]
                    raise GatewayError(
                        f"OpenAI video poll {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                job = await resp.json()
            status = job.get("status")
            if status == "completed":
                return job
            if status == "failed":
                raise GatewayError(
                    f"OpenAI video job {job_id} failed: {job.get('error') or job}",
                    code="upstream_error",
                )
            await asyncio.sleep(min(backoff, remaining))
            backoff = min(backoff * 1.5, 30.0)

    async def _finalise_openai(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        api_key: str,
        job: Dict[str, Any],
    ) -> Dict[str, Any]:
        url = f"https://api.openai.com/v1/videos/{job_id}/content"
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            allow_redirects=False,
        ) as resp:
            if resp.status >= 400:
                err_text = (await resp.text())[:300]
                logger.warning(
                    "OpenAI video content fetch failed: %s %s", resp.status, err_text
                )
                return job
            video_bytes = await _read_video_bytes(resp)
        job["b64_json"] = base64.b64encode(video_bytes).decode("ascii")
        return job

    # ----- OpenRouter -----

    async def _call_openrouter(
        self,
        model: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
        wait: bool = True,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_wait: float = DEFAULT_MAX_WAIT,
    ) -> Dict[str, Any]:
        """Create, poll, and download an OpenRouter video generation."""
        if not api_key:
            raise GatewayError(
                "OpenRouter API key required for video", code="missing_api_key"
            )
        payload = dict(body)
        payload["model"] = self._strip_provider_prefix(model)
        payload["prompt"] = self._require_prompt(body)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        timeout = aiohttp.ClientTimeout(total=max_wait + 60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://openrouter.ai/api/v1/videos",
                headers=headers,
                json=payload,
                allow_redirects=False,
            ) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:500]
                    raise GatewayError(
                        f"OpenRouter video error {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                job = await resp.json()
            job_id = job.get("id")
            if not isinstance(job_id, str) or not job_id:
                raise GatewayError(
                    f"OpenRouter video response missing id: {job}",
                    code="upstream_error",
                )
            if not wait:
                return job
            if job.get("status") != "completed":
                job = await self._poll_openrouter(
                    session, job, api_key, poll_interval, max_wait
                )
            return await self._finalise_openrouter(session, job_id, api_key, job)

    async def _poll_openrouter(
        self,
        session: aiohttp.ClientSession,
        job: Dict[str, Any],
        api_key: str,
        poll_interval: float,
        max_wait: float,
    ) -> Dict[str, Any]:
        job_id = str(job["id"])
        polling_url = job.get("polling_url")
        if not isinstance(polling_url, str) or not _is_origin(
            polling_url, _OPENROUTER_HOST
        ):
            polling_url = f"https://openrouter.ai/api/v1/videos/{job_id}"
        deadline = asyncio.get_event_loop().time() + max_wait
        backoff = poll_interval
        headers = {"Authorization": f"Bearer {api_key}"}
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise GatewayError(
                    f"OpenRouter video job {job_id} did not finish within {max_wait}s",
                    code="timeout",
                )
            async with session.get(
                polling_url, headers=headers, allow_redirects=False
            ) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:300]
                    raise GatewayError(
                        f"OpenRouter video poll {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                job = await resp.json()
            status = job.get("status")
            if status in {"completed", "succeeded"}:
                return job
            if status in {"failed", "cancelled", "expired"}:
                raise GatewayError(
                    f"OpenRouter video job {job_id} {status}: {job.get('error') or job}",
                    code="upstream_error",
                )
            await asyncio.sleep(min(backoff, remaining))
            backoff = min(backoff * 1.5, 30.0)

    async def _finalise_openrouter(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        api_key: str,
        job: Dict[str, Any],
    ) -> Dict[str, Any]:
        content_url = (
            f"https://openrouter.ai/api/v1/videos/{job_id}/content?index=0"
        )
        async with session.get(
            content_url,
            headers={"Authorization": f"Bearer {api_key}"},
            allow_redirects=False,
        ) as resp:
            if resp.status >= 400:
                err_text = (await resp.text())[:300]
                raise GatewayError(
                    f"OpenRouter video download {resp.status}: {err_text}",
                    code="upstream_error",
                )
            video_bytes = await _read_video_bytes(resp)
        job["b64_json"] = base64.b64encode(video_bytes).decode("ascii")
        return job

    # ----- Google Veo -----

    async def _call_google(
        self,
        model: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
        wait: bool = True,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_wait: float = DEFAULT_MAX_WAIT,
    ) -> Dict[str, Any]:
        if not api_key:
            raise GatewayError(
                "Google API key required for video", code="missing_api_key"
            )
        upstream_model = self._strip_provider_prefix(model)
        if upstream_model.startswith("models/"):
            upstream_model = upstream_model[len("models/"):]
        prompt = self._require_prompt(body)
        parameters: Dict[str, Any] = {}
        parameter_fields = {
            "aspect_ratio": "aspectRatio",
            "duration_seconds": "durationSeconds",
            "resolution": "resolution",
            "negative_prompt": "negativePrompt",
            "person_generation": "personGeneration",
        }
        for request_key, google_key in parameter_fields.items():
            value = body.get(request_key)
            if value is not None:
                parameters[google_key] = value
        payload: Dict[str, Any] = {"instances": [{"prompt": prompt}]}
        if parameters:
            payload["parameters"] = parameters

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{upstream_model}:predictLongRunning"
        )
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        timeout = aiohttp.ClientTimeout(total=max_wait + 60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, headers=headers, json=payload, allow_redirects=False
            ) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:500]
                    logger.warning(
                        "Google Veo create failed: %s %s", resp.status, err_text
                    )
                    raise GatewayError(
                        f"Google Veo error {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                operation = await resp.json()
            operation_name = operation.get("name")
            if not isinstance(operation_name, str) or not operation_name:
                raise GatewayError(
                    f"Google Veo response missing operation name: {operation}",
                    code="upstream_error",
                )
            self._raise_google_operation_error(operation_name, operation)
            if not wait:
                return self._google_job(upstream_model, operation)
            if not operation.get("done"):
                operation = await self._poll_google(
                    session, operation_name, api_key, poll_interval, max_wait
                )
            return await self._finalise_google(
                session, upstream_model, operation_name, api_key, operation
            )

    async def _poll_google(
        self,
        session: aiohttp.ClientSession,
        operation_name: str,
        api_key: str,
        poll_interval: float,
        max_wait: float,
    ) -> Dict[str, Any]:
        path = operation_name.lstrip("/")
        if path.startswith("v1beta/"):
            path = path[len("v1beta/"):]
        url = f"https://generativelanguage.googleapis.com/v1beta/{path}"
        headers = {"x-goog-api-key": api_key}
        deadline = asyncio.get_event_loop().time() + max_wait
        backoff = poll_interval
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise GatewayError(
                    f"Google Veo operation {operation_name} did not finish "
                    f"within {max_wait}s",
                    code="timeout",
                )
            async with session.get(
                url, headers=headers, allow_redirects=False
            ) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:300]
                    raise GatewayError(
                        f"Google Veo poll {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                operation = await resp.json()
            if operation.get("done"):
                self._raise_google_operation_error(operation_name, operation)
                return operation
            await asyncio.sleep(min(backoff, remaining))
            backoff = min(backoff * 1.5, 30.0)

    async def _finalise_google(
        self,
        session: aiohttp.ClientSession,
        model: str,
        operation_name: str,
        api_key: str,
        operation: Dict[str, Any],
    ) -> Dict[str, Any]:
        self._raise_google_operation_error(operation_name, operation)
        response = operation.get("response") or {}
        generate_response = response.get("generateVideoResponse") or {}
        samples = generate_response.get("generatedSamples") or []
        video_uri: Optional[str] = None
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            video = sample.get("video") or {}
            if isinstance(video, dict) and isinstance(video.get("uri"), str):
                video_uri = video["uri"]
                break
        if not video_uri:
            raise GatewayError(
                f"Google Veo operation {operation_name} completed without a "
                "generated video URI",
                code="upstream_error",
            )
        if not _is_origin(video_uri, _GOOGLE_CREDENTIAL_HOST):
            raise GatewayError(
                "Google Veo returned an untrusted video download URL",
                code="upstream_error",
            )
        async with session.get(
            video_uri,
            headers={"x-goog-api-key": api_key},
            allow_redirects=False,
        ) as resp:
            if resp.status >= 400:
                err_text = (await resp.text())[:300]
                raise GatewayError(
                    f"Google Veo video download {resp.status}: {err_text}",
                    code="upstream_error",
                )
            video_bytes = await _read_video_bytes(resp)
        job = self._google_job(model, operation)
        job["video"] = {"uri": video_uri}
        job["b64_json"] = base64.b64encode(video_bytes).decode("ascii")
        return job

    @staticmethod
    def _google_job(model: str, operation: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": operation.get("name"),
            "model": model,
            "provider": "google",
            "status": "completed" if operation.get("done") else "processing",
            "operation": operation,
        }

    @staticmethod
    def _raise_google_operation_error(
        operation_name: str,
        operation: Dict[str, Any],
    ) -> None:
        error = operation.get("error")
        if error:
            raise GatewayError(
                f"Google Veo operation {operation_name} failed: {error}",
                code="upstream_error",
            )

    # ----- MiniMax H3 -----

    async def _call_minimax(
        self,
        model: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
        wait: bool = True,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_wait: float = DEFAULT_MAX_WAIT,
    ) -> Dict[str, Any]:
        """Create a MiniMax H3 task and optionally poll it to completion."""
        if not api_key:
            raise GatewayError(
                "MiniMax API key required for video", code="missing_api_key"
            )

        upstream_model = self._strip_provider_prefix(model)
        payload = self._minimax_payload(upstream_model, body)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(
                key_value
                for key_value in extra_headers.items()
                if key_value[0].lower() not in {
                    "authorization", "content-length", "content-type", "host"
                }
            )

        timeout = aiohttp.ClientTimeout(total=max_wait + 60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://api.minimax.io/v2/video_generation",
                headers=headers,
                json=payload,
                allow_redirects=False,
            ) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:500]
                    logger.warning(
                        "MiniMax video create failed: %s %s", resp.status, err_text
                    )
                    raise GatewayError(
                        f"MiniMax video error {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                response = await resp.json()
            self._raise_minimax_response_error("create", response)
            task = response.get("task")
            task_id = response.get("task_id")
            if not task_id and isinstance(task, dict):
                task_id = task.get("id") or task.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise GatewayError(
                    f"MiniMax video response missing task_id: {response}",
                    code="upstream_error",
                )

            job = self._minimax_job(upstream_model, task_id, response)
            if not wait:
                return job
            if job["status"] != "completed":
                response = await self._poll_minimax(
                    session, task_id, api_key, poll_interval, max_wait
                )
                job = self._minimax_job(upstream_model, task_id, response)
            return self._finalise_minimax(job)

    @classmethod
    def _minimax_payload(
        cls, model: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        content: list[Dict[str, Any]] = [
            {"type": "text", "text": cls._require_prompt(body)}
        ]
        references = body.get("content")
        if references is not None:
            if not isinstance(references, list):
                raise GatewayError(
                    "MiniMax video content must be an array",
                    code="unsupported_parameter",
                )
            for reference in references:
                content.append(cls._minimax_reference(reference))

        input_reference = body.get("input_reference")
        if input_reference is not None:
            if isinstance(input_reference, str):
                input_reference = {"url": input_reference}
            if not isinstance(input_reference, dict):
                raise GatewayError(
                    "MiniMax input_reference must contain a URL",
                    code="unsupported_parameter",
                )
            image_value = input_reference.get("image_url", input_reference.get("url"))
            if isinstance(image_value, str):
                image_value = {"url": image_value}
            content.append(
                cls._minimax_reference({
                    "type": "image_url",
                    "image_url": image_value,
                    "role": input_reference.get("role", "first_frame"),
                })
            )

        duration = body.get("duration", body.get("seconds", 6))
        if isinstance(duration, str) and duration.isdigit():
            duration = int(duration)
        if not isinstance(duration, int) or isinstance(duration, bool) or not 4 <= duration <= 15:
            raise GatewayError(
                "MiniMax video duration must be an integer from 4 to 15",
                code="unsupported_parameter",
            )

        resolution = body.get("resolution")
        if resolution is None:
            resolution = "2K" if str(body.get("size", "")).startswith("2") else "768P"
        if resolution not in {"768P", "2K"}:
            raise GatewayError(
                "MiniMax video resolution must be '768P' or '2K'",
                code="unsupported_parameter",
            )

        payload: Dict[str, Any] = {
            "model": model,
            "content": content,
            "duration": duration,
            "resolution": resolution,
        }
        ratio = body.get("ratio") or cls._minimax_ratio_from_size(body.get("size"))
        has_frame_reference = any(
            item.get("type") == "image_url"
            and item.get("role") in {"first_frame", "last_frame"}
            for item in content
        )
        if ratio is not None and not has_frame_reference:
            payload["ratio"] = ratio
        elif ratio is None and not has_frame_reference:
            payload["ratio"] = "16:9"
        return payload

    @staticmethod
    def _minimax_reference(reference: Any) -> Dict[str, Any]:
        if not isinstance(reference, dict):
            raise GatewayError(
                "MiniMax video references must be objects",
                code="unsupported_parameter",
            )
        reference_type = reference.get("type")
        field_by_type = {
            "image_url": "image_url",
            "video_url": "video_url",
            "audio_url": "audio_url",
        }
        field = field_by_type.get(reference_type)
        if field is None:
            raise GatewayError(
                f"MiniMax video content type '{reference_type}' is not supported",
                code="unsupported_parameter",
            )
        value = reference.get(field)
        if isinstance(value, str):
            value = {"url": value}
        if not isinstance(value, dict) or not isinstance(value.get("url"), str) or not value["url"]:
            raise GatewayError(
                f"MiniMax {reference_type} must contain a URL",
                code="unsupported_parameter",
            )
        block: Dict[str, Any] = {"type": reference_type, field: {"url": value["url"]}}
        role = reference.get("role")
        allowed_roles = {
            "image_url": {"first_frame", "last_frame", "reference_image"},
            "video_url": {"reference_video"},
            "audio_url": {"reference_audio"},
        }
        if role not in allowed_roles[reference_type]:
            raise GatewayError(
                f"MiniMax {reference_type} has an unsupported role",
                code="unsupported_parameter",
            )
        block["role"] = role
        return block

    @staticmethod
    def _minimax_ratio_from_size(size: Any) -> Optional[str]:
        if not isinstance(size, str) or "x" not in size:
            return None
        width_text, _, height_text = size.lower().partition("x")
        try:
            width, height = int(width_text), int(height_text)
        except ValueError:
            return None
        ratios = {
            (16, 9): "16:9", (9, 16): "9:16", (4, 3): "4:3",
            (3, 4): "3:4", (1, 1): "1:1",
        }
        from math import gcd
        divisor = gcd(width, height)
        return ratios.get((width // divisor, height // divisor)) if divisor else None

    async def _poll_minimax(
        self,
        session: aiohttp.ClientSession,
        task_id: str,
        api_key: str,
        poll_interval: float,
        max_wait: float,
    ) -> Dict[str, Any]:
        url = f"https://api.minimax.io/v2/query/video_generation/{task_id}"
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        deadline = asyncio.get_event_loop().time() + max_wait
        backoff = poll_interval
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise GatewayError(
                    f"MiniMax video task {task_id} did not finish within {max_wait}s",
                    code="timeout",
                )
            async with session.get(url, headers=headers, allow_redirects=False) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:300]
                    raise GatewayError(
                        f"MiniMax video poll {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                response = await resp.json()
            self._raise_minimax_response_error(f"task {task_id}", response)
            task = response.get("task")
            status = str(task.get("status", "") if isinstance(task, dict) else "").lower()
            if status in {"success", "succeeded", "completed"}:
                return response
            if status in {"fail", "failed", "failure", "cancelled", "canceled"}:
                error = task.get("error") if isinstance(task, dict) else response
                raise GatewayError(
                    f"MiniMax video task {task_id} failed: {error or response}",
                    code="upstream_error",
                )
            await asyncio.sleep(min(backoff, remaining))
            backoff = min(backoff * 1.5, 30.0)

    @staticmethod
    def _raise_minimax_response_error(context: str, response: Dict[str, Any]) -> None:
        base_resp = response.get("base_resp")
        if isinstance(base_resp, dict) and base_resp.get("status_code") not in (None, 0, "0"):
            raise GatewayError(
                f"MiniMax video {context} failed: {base_resp.get('status_msg') or base_resp}",
                code="upstream_error",
            )

    @staticmethod
    def _minimax_job(model: str, task_id: str, response: Dict[str, Any]) -> Dict[str, Any]:
        task = response.get("task")
        task = task if isinstance(task, dict) else {}
        upstream_status = str(task.get("status") or response.get("status") or "queued")
        status = "completed" if upstream_status.lower() in {
            "success", "succeeded", "completed"
        } else "processing" if upstream_status.lower() not in {"queued", "created"} else "queued"
        return {
            "id": str(task.get("id") or task.get("task_id") or task_id),
            "task_id": task_id,
            "model": model,
            "provider": "minimax",
            "status": status,
            "task": task or response,
        }

    @staticmethod
    def _finalise_minimax(job: Dict[str, Any]) -> Dict[str, Any]:
        """Expose the signed HTTPS result URL without fetching it server-side."""
        task = job.get("task")
        content = task.get("content") if isinstance(task, dict) else None
        url = content.get("url") if isinstance(content, dict) else None
        if url is None:
            return job
        if not isinstance(url, str):
            raise GatewayError(
                "MiniMax returned an invalid video URL", code="upstream_error"
            )
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise GatewayError(
                "MiniMax returned an invalid video URL", code="upstream_error"
            )
        if (
            parts.scheme != "https"
            or not parts.hostname
            or port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
        ):
            raise GatewayError(
                "MiniMax returned an unsafe video URL", code="upstream_error"
            )
        job["video"] = {"url": url}
        job["url"] = url
        return job

    # ----- Z.ai (CogVideoX, Vidu) -----

    async def _call_zai(
        self,
        model: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
        wait: bool = True,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_wait: float = DEFAULT_MAX_WAIT,
    ) -> Dict[str, Any]:
        """Create an async video job on Z.ai and optionally wait for completion.

        Z.ai's video API differs from OpenAI:
        - Create: POST /paas/v4/videos/generations (returns id, not full job).
        - Poll:   GET  /paas/v4/async-result/{id}
          (returns {task_status: PROCESSING|SUCCESS|FAIL, video_result: [{url}]})
        - Content is fetched from the URL in ``video_result[].url`` (signed,
          expires in ~30 days) rather than from a /content endpoint.
        """
        if not api_key:
            raise GatewayError(
                "Z.AI API key required for video", code="missing_api_key"
            )
        url = "https://api.z.ai/api/paas/v4/videos/generations"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en-US,en",
        }
        if extra_headers:
            headers.update(extra_headers)
        # Z.ai takes the prompt and optional image_url array. Forward all
        # body fields except those that don't exist on the upstream schema.
        prompt = self._require_prompt(body)
        payload: Dict[str, Any] = {
            "model": self._strip_provider_prefix(model),
            "prompt": prompt,
        }
        for key in (
            "image_url",
            "quality",
            "with_audio",
            "size",
            "fps",
            "duration",
            "style",
            "aspect_ratio",
            "movement_amplitude",
            "user_id",
        ):
            if key in body and body[key] is not None:
                payload[key] = body[key]
        timeout = aiohttp.ClientTimeout(total=max_wait + 60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, headers=headers, json=payload, allow_redirects=False
            ) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:500]
                    logger.warning(
                        "Z.AI video create failed: %s %s", resp.status, err_text
                    )
                    raise GatewayError(
                        f"Z.AI video error {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                job = await resp.json()
            job_id = job.get("id")
            if not job_id:
                raise GatewayError(
                    f"Z.AI video response missing id: {job}",
                    code="upstream_error",
                )
            if not wait:
                return job
            if (job.get("task_status") or job.get("status")) != "SUCCESS":
                job = await self._poll_zai(
                    session, job_id, api_key, poll_interval, max_wait
                )
            return await self._finalise_zai(session, job_id, api_key, job)

    async def _poll_zai(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        api_key: str,
        poll_interval: float,
        max_wait: float,
    ) -> Dict[str, Any]:
        url = f"https://api.z.ai/api/paas/v4/async-result/{job_id}"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept-Language": "en-US,en",
        }
        deadline = asyncio.get_event_loop().time() + max_wait
        backoff = poll_interval
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise GatewayError(
                    f"Z.AI video job {job_id} did not finish within {max_wait}s",
                    code="timeout",
                )
            async with session.get(
                url, headers=headers, allow_redirects=False
            ) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:300]
                    raise GatewayError(
                        f"Z.AI video poll {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                job = await resp.json()
            status = job.get("task_status") or job.get("status")
            if status == "SUCCESS":
                return job
            if status == "FAIL":
                raise GatewayError(
                    f"Z.AI video job {job_id} failed: "
                    f"{job.get('error') or job}",
                    code="upstream_error",
                )
            await asyncio.sleep(min(backoff, remaining))
            backoff = min(backoff * 1.5, 30.0)

    async def _finalise_zai(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        api_key: str,
        job: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Return Z.AI's signed result URL without fetching it server-side."""
        results = job.get("video_result") or []
        if not results or not isinstance(results[0], dict):
            return job
        url = results[0].get("url")
        if not isinstance(url, str):
            return job
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise GatewayError("Z.AI returned an invalid video URL", code="upstream_error")
        if (
            parts.scheme != "https"
            or not parts.hostname
            or port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
        ):
            raise GatewayError("Z.AI returned an unsafe video URL", code="upstream_error")
        return job


def get_video_handler(
    config: Dict[str, Any],
    capability_registry: Optional[Any] = None,
) -> VideoHandler:
    return VideoHandler(config, capability_registry=capability_registry)