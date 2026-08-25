"""Video generation provider support for Tusker AI Gateway.

Supports:
- OpenAI Sora (POST /v1/videos) when OPENAI_API_KEY is set.
- OpenRouter Video (POST /api/v1/videos) when the model is provider-prefixed.

Both APIs are asynchronous: POST returns a job object with an id and initial
status; the caller polls until the job reaches "completed"/"succeeded" or
"failed", then fetches the MP4 bytes from the content endpoint.

By default the gateway waits up to 5 minutes for the video to finish so
clients receive the final file in one round trip (the final MP4 is inlined
as base64 under ``b64_json`` on the job object). Set ``wait=false`` on the
request to return the initial job JSON immediately.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Dict, Optional

import aiohttp

from tusker_gateway.errors import GatewayError

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 5.0  # seconds
DEFAULT_MAX_WAIT = 300.0    # seconds


class VideoHandler:
    """Dispatch video generation requests to OpenAI or OpenRouter."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def is_video_request(self, path: str, body: Dict[str, Any]) -> bool:
        return path in ("/v1/videos", "/v1/video/generations")

    def get_provider_for_video_request(self, model: str) -> str:
        if "/" in model:
            return "openrouter"
        # Z.ai's own video models (CogVideoX, Vidu). Slugs are unambiguous.
        if model.lower().startswith(("cogvideox-", "vidu", "viduq1")):
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
        if provider == "zai":
            return await self._call_zai(
                model, body, api_key, extra_headers,
                wait, poll_interval, max_wait,
            )
        return await self._call_openai(
            model, body, api_key, extra_headers,
            wait, poll_interval, max_wait,
        )


    @staticmethod
    def _strip_provider_prefix(model: str) -> str:
        """Strip a leading 'openrouter/' prefix before sending upstream.

        The gateway accepts both 'model' and 'openrouter/model' request ids so
        clients can pin the routing provider; OpenRouter itself does not
        understand its own prefix, so it must be removed before the upstream
        call. Mirrors routing.resolve_route's slash-form partition.
        """
        if model.startswith(("openrouter/", "openrouter::")):
            return model.split("/", 1)[1] if "/" in model else model.split("::", 1)[1]
        return model
    @staticmethod
    def _normalise_request(model: str, body: Dict[str, Any]) -> Dict[str, Any]:
        prompt = body.get("prompt", "")
        if not prompt:
            raise GatewayError(
                "video request missing required 'prompt'", code="bad_request"
            )
        payload: Dict[str, Any] = {"model": model, "prompt": prompt}
        for key in ("size", "seconds", "input_reference"):
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
        url = "https://api.openai.com/v1/videos"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        payload = self._normalise_request(model, body)
        timeout = aiohttp.ClientTimeout(total=max_wait + 60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
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
            async with session.get(url, headers=headers) as resp:
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
            url, headers={"Authorization": f"Bearer {api_key}"}
        ) as resp:
            if resp.status >= 400:
                err_text = (await resp.text())[:300]
                logger.warning(
                    "OpenAI video content fetch failed: %s %s", resp.status, err_text
                )
                return job
            video_bytes = await resp.read()
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
        if not api_key:
            raise GatewayError(
                "OpenRouter API key required for video", code="missing_api_key"
            )
        url = "https://openrouter.ai/api/v1/videos"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        payload = self._normalise_request(self._strip_provider_prefix(model), body)
        timeout = aiohttp.ClientTimeout(total=max_wait + 60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:500]
                    logger.warning(
                        "OpenRouter video create failed: %s %s", resp.status, err_text
                    )
                    raise GatewayError(
                        f"OpenRouter video error {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                job = await resp.json()
            job_id = job.get("id") or job.get("video_id")
            if not job_id:
                raise GatewayError(
                    f"OpenRouter video response missing id: {job}",
                    code="upstream_error",
                )
            if not wait:
                return job
            if job.get("status") not in ("completed", "succeeded"):
                job = await self._poll_openrouter(
                    session, job_id, api_key, poll_interval, max_wait
                )
            return await self._finalise_openrouter(session, job_id, api_key, job)

    async def _poll_openrouter(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        api_key: str,
        poll_interval: float,
        max_wait: float,
    ) -> Dict[str, Any]:
        url = f"https://openrouter.ai/api/v1/videos/{job_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        deadline = asyncio.get_event_loop().time() + max_wait
        backoff = poll_interval
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise GatewayError(
                    f"OpenRouter video job {job_id} did not finish within {max_wait}s",
                    code="timeout",
                )
            async with session.get(url, headers=headers) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:300]
                    raise GatewayError(
                        f"OpenRouter video poll {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                job = await resp.json()
            status = job.get("status")
            if status in ("completed", "succeeded"):
                return job
            if status in ("failed", "cancelled"):
                raise GatewayError(
                    f"OpenRouter video job {job_id} status={status}: "
                    f"{job.get('error') or job}",
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
        url = f"https://openrouter.ai/api/v1/videos/{job_id}/content"
        async with session.get(
            url, headers={"Authorization": f"Bearer {api_key}"}
        ) as resp:
            if resp.status >= 400:
                err_text = (await resp.text())[:300]
                logger.warning(
                    "OpenRouter video content fetch failed: %s %s",
                    resp.status,
                    err_text,
                )
                return job
            video_bytes = await resp.read()
        job["b64_json"] = base64.b64encode(video_bytes).decode("ascii")
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

        Z.ai's video API differs from OpenAI/OpenRouter:
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
        prompt = body.get("prompt", "")
        if not prompt:
            raise GatewayError(
                "video request missing required 'prompt'", code="bad_request"
            )
        payload: Dict[str, Any] = {"model": model, "prompt": prompt}
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
            async with session.post(url, headers=headers, json=payload) as resp:
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
            async with session.get(url, headers=headers) as resp:
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
        """Inline the MP4 bytes from the signed URL returned by Z.ai.

        Z.ai does not expose a /content endpoint - the result contains the
        URL in ``video_result[0].url``. We follow it once to inline the
        bytes under ``b64_json`` so the caller gets the same shape as
        OpenAI/OpenRouter results.
        """
        results = job.get("video_result") or []
        if not results:
            return job
        url = results[0].get("url")
        if not url:
            return job
        try:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:300]
                    logger.warning(
                        "Z.AI video content fetch failed: %s %s",
                        resp.status,
                        err_text,
                    )
                    return job
                video_bytes = await resp.read()
        except aiohttp.ClientError as exc:
            logger.warning("Z.AI video content fetch error: %s", exc)
            return job
        job["b64_json"] = base64.b64encode(video_bytes).decode("ascii")
        return job


def get_video_handler(config: Dict[str, Any]) -> VideoHandler:
    return VideoHandler(config)