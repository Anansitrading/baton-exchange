"""Gemini API wrapper for HyperVisa sessions."""

import os
import time
from typing import Any

from google import genai
from google.genai import types

DEFAULT_MODEL = "gemini-3-flash-preview"

# Minimum text tokens before video compression kicks in.
# Below 100K, raw text is fine. Above 100K, compress to video.
VIDEO_MIN_THRESHOLD = 100_000

# Token budget before swarm mode triggers.
# With ~15-22x video compression and 1M context window,
# chunking is unnecessary until codebases exceed 2M text tokens.
SWARM_THRESHOLD = 2_000_000


def get_api_key() -> str:
    key = (os.environ.get("GEMINI_API_KEY")
           or os.environ.get("GOOGLE_API_KEY")
           or "AIzaSyD-GaPz3XqpGj1UUMxPw-Sqhc-wmHE9VLA")
    return key


def make_client(api_key: str | None = None) -> genai.Client:
    return genai.Client(api_key=api_key or get_api_key())


def _generation_config(temperature: float = 1.1) -> types.GenerateContentConfig:
    """Build the standard HyperVisa generation config.

    Settings: temp=1.1, media_resolution=high, thinking=high,
    grounding with Google Search enabled.
    """
    return types.GenerateContentConfig(
        temperature=temperature,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.HIGH,
        ),
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )


def upload_video(client: genai.Client, video_path: str,
                 display_name: str = "hypervisa") -> types.File:
    """Upload a video file to Gemini File API. Waits for processing."""
    uploaded = client.files.upload(
        file=video_path,
        config=types.UploadFileConfig(
            mime_type="video/mp4",
            display_name=display_name,
        ),
    )

    # Wait for file to become ACTIVE
    while uploaded.state and uploaded.state.name == "PROCESSING":
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)

    if uploaded.state and uploaded.state.name == "FAILED":
        raise RuntimeError(f"File processing failed: {uploaded.error}")

    return uploaded


def count_tokens(client: genai.Client, model: str,
                 contents: list) -> int:
    """Count tokens for the given contents (uploaded files, text, etc.)."""
    resp = client.models.count_tokens(model=model, contents=contents)
    return resp.total_tokens


def count_text_tokens(client: genai.Client, model: str, text: str) -> int:
    """Count actual tokens for raw text using the Gemini API.

    Uses client.models.count_tokens per the Gemini cookbook rather than
    the inaccurate len(text)//4 heuristic.
    """
    resp = client.models.count_tokens(model=model, contents=text)
    return resp.total_tokens


def extract_telemetry(resp: types.GenerateContentResponse) -> dict[str, Any]:
    """Extract full telemetry from a Gemini response for dev inspection."""
    telemetry: dict[str, Any] = {}

    # Model and response metadata
    raw = resp.to_json_dict() if hasattr(resp, "to_json_dict") else {}
    telemetry["model_version"] = raw.get("model_version", "")
    telemetry["response_id"] = raw.get("response_id", "")

    # Usage metadata
    usage = raw.get("usage_metadata", {})
    telemetry["usage"] = {
        "prompt_tokens": usage.get("prompt_token_count", 0),
        "candidates_tokens": usage.get("candidates_token_count", 0),
        "thoughts_tokens": usage.get("thoughts_token_count", 0),
        "total_tokens": usage.get("total_token_count", 0),
        "cached_tokens": usage.get("cached_content_token_count", 0),
        "prompt_tokens_details": usage.get("prompt_tokens_details", []),
    }

    # Candidate details
    candidates = raw.get("candidates", [])
    if candidates:
        cand = candidates[0]
        telemetry["finish_reason"] = cand.get("finish_reason", "")
        # Extract thinking parts vs output parts
        parts = cand.get("content", {}).get("parts", [])
        thinking_parts = []
        output_parts = []
        for p in parts:
            if p.get("thought"):
                thinking_parts.append(p.get("text", ""))
            elif "thought_signature" in p and "text" in p:
                # Output part with thought signature (the actual model output)
                output_parts.append(p.get("text", ""))
            elif "text" in p:
                output_parts.append(p.get("text", ""))
        telemetry["thinking_text"] = "\n".join(thinking_parts) if thinking_parts else None
        telemetry["has_thinking"] = bool(thinking_parts) or usage.get("thoughts_token_count", 0) > 0
        # Grounding metadata
        grounding = cand.get("grounding_metadata")
        if grounding:
            telemetry["grounding"] = grounding

    # Server timing from HTTP headers
    headers = raw.get("sdk_http_response", {}).get("headers", {})
    if headers.get("server-timing"):
        telemetry["server_timing"] = headers["server-timing"]

    return telemetry


def _query_generate(client: genai.Client, model: str,
                    contents: list, config: types.GenerateContentConfig,
                    ) -> tuple[str, dict[str, Any]]:
    """Core generate call that returns (text, telemetry)."""
    t0 = time.time()
    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )
    elapsed_ms = int((time.time() - t0) * 1000)
    telemetry = extract_telemetry(resp)
    telemetry["latency_ms"] = elapsed_ms
    return resp.text or "", telemetry


def query_single(client: genai.Client, model: str,
                 file_ref: types.File, prompt: str,
                 system: str | None = None,
                 temperature: float = 1.1,
                 with_telemetry: bool = False) -> str | tuple[str, dict]:
    """Query Gemini with a single uploaded video + prompt."""
    config = _generation_config(temperature)
    if system:
        config.system_instruction = system
    contents = [file_ref, prompt]
    if with_telemetry:
        return _query_generate(client, model, contents, config)
    resp = client.models.generate_content(
        model=model, contents=contents, config=config,
    )
    return resp.text or ""


def query_youtube(client: genai.Client, model: str,
                  youtube_url: str, prompt: str,
                  system: str | None = None,
                  temperature: float = 1.1,
                  with_telemetry: bool = False) -> str | tuple[str, dict]:
    """Query Gemini with a YouTube URL (native video understanding)."""
    config = _generation_config(temperature)
    if system:
        config.system_instruction = system
    video_part = types.Part(
        file_data=types.FileData(
            file_uri=youtube_url,
            mime_type="video/*",
        )
    )
    contents = [video_part, prompt]
    if with_telemetry:
        return _query_generate(client, model, contents, config)
    resp = client.models.generate_content(
        model=model, contents=contents, config=config,
    )
    return resp.text or ""


def _streaming_config(temperature: float = 1.1) -> types.GenerateContentConfig:
    """Generation config for streaming with include_thoughts enabled."""
    return types.GenerateContentConfig(
        temperature=temperature,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.HIGH,
            include_thoughts=True,
        ),
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )


def stream_generate(client: genai.Client, model: str,
                    contents: list, system: str | None = None,
                    temperature: float = 1.1):
    """Core streaming generator. Yields (event_type, data) tuples.

    Event types: 'thinking', 'answer', 'done'.
    """
    config = _streaming_config(temperature)
    if system:
        config.system_instruction = system
    t0 = time.time()
    accumulated_answer = ""
    accumulated_thinking = ""
    last_usage = {}

    last_grounding = None
    last_model_version = ""
    last_finish_reason = ""

    for chunk in client.models.generate_content_stream(
        model=model, contents=contents, config=config,
    ):
        if not chunk.candidates:
            continue
        cand = chunk.candidates[0]
        if not cand.content or not cand.content.parts:
            # Even without parts, check for grounding metadata
            raw = chunk.to_json_dict() if hasattr(chunk, "to_json_dict") else {}
            candidates = raw.get("candidates", [])
            if candidates:
                gm_data = candidates[0].get("grounding_metadata")
                if gm_data:
                    last_grounding = gm_data
                fr = candidates[0].get("finish_reason")
                if fr:
                    last_finish_reason = fr
            continue
        for part in cand.content.parts:
            if not part.text:
                continue
            if part.thought:
                accumulated_thinking += part.text
                yield ("thinking", part.text)
            else:
                accumulated_answer += part.text
                yield ("answer", part.text)

        # Capture metadata from each chunk (accumulates)
        raw = chunk.to_json_dict() if hasattr(chunk, "to_json_dict") else {}
        usage_meta = raw.get("usage_metadata")
        if usage_meta:
            last_usage = usage_meta
        mv = raw.get("model_version")
        if mv:
            last_model_version = mv
        # Grounding metadata typically arrives in the final chunk
        candidates = raw.get("candidates", [])
        if candidates:
            gm_data = candidates[0].get("grounding_metadata")
            if gm_data:
                last_grounding = gm_data
            fr = candidates[0].get("finish_reason")
            if fr:
                last_finish_reason = fr

    elapsed_ms = int((time.time() - t0) * 1000)

    # Build final telemetry from accumulated data
    telemetry: dict[str, Any] = {
        "latency_ms": elapsed_ms,
        "has_thinking": bool(accumulated_thinking),
        "thinking_text": accumulated_thinking or None,
        "model_version": last_model_version,
        "finish_reason": last_finish_reason,
        "usage": {
            "prompt_tokens": last_usage.get("prompt_token_count", 0),
            "candidates_tokens": last_usage.get("candidates_token_count", 0),
            "thoughts_tokens": last_usage.get("thoughts_token_count", 0),
            "total_tokens": last_usage.get("total_token_count", 0),
            "cached_tokens": last_usage.get("cached_content_token_count", 0),
            "prompt_tokens_details": last_usage.get("prompt_tokens_details", []),
        },
    }
    if last_grounding:
        telemetry["grounding"] = last_grounding
    yield ("done", {"answer": accumulated_answer, "telemetry": telemetry})


def query_single_stream(client: genai.Client, model: str,
                        file_ref: types.File, prompt: str,
                        system: str | None = None,
                        temperature: float = 1.1):
    """Stream query with a single uploaded video + prompt."""
    yield from stream_generate(client, model, [file_ref, prompt],
                               system=system, temperature=temperature)


def query_youtube_stream(client: genai.Client, model: str,
                         youtube_url: str, prompt: str,
                         system: str | None = None,
                         temperature: float = 1.1):
    """Stream query with a YouTube URL."""
    video_part = types.Part(
        file_data=types.FileData(file_uri=youtube_url, mime_type="video/*")
    )
    yield from stream_generate(client, model, [video_part, prompt],
                               system=system, temperature=temperature)


def query_with_parts(client: genai.Client, model: str,
                     parts: list, prompt: str,
                     system: str | None = None,
                     temperature: float = 1.1,
                     with_telemetry: bool = False) -> str | tuple[str, dict]:
    """Query Gemini with arbitrary parts + prompt."""
    config = _generation_config(temperature)
    if system:
        config.system_instruction = system
    contents = parts + [prompt]
    if with_telemetry:
        return _query_generate(client, model, contents, config)
    resp = client.models.generate_content(
        model=model, contents=contents, config=config,
    )
    return resp.text or ""
