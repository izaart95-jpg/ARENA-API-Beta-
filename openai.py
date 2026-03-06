"""
OpenAI-Compatible API Server for Arena
=======================================
Fully self-contained — no arena_client import needed.

Endpoints:
  GET  /v1/models
  GET  /v1/models/{model_id}
  POST /v1/chat/completions   (streaming + non-streaming)

Usage:
  pip install fastapi uvicorn httpx
  python openai_api.py

Available model IDs:
  arena-chat       → Standard chat
  arena-search     → Chat with web search
  arena-reasoning  → Chat with extended reasoning/thinking
  arena-image      → Image generation
"""

import json
import re
import time
import uuid
import httpx

from typing import Any, Dict, List, Optional, Union, AsyncIterator
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from modula import (
    load_config,
    save_config,
    get_latest_token,
    consume_token,
    should_filter_content,
    BASE_URL,
    AUTO_TOKEN,
)


# ================================================================ #
#  Arena constants (inlined from arena_client.py)
# ================================================================ #

DEFAULT_SEARCH_MODEL   = "019c6f55-308b-71ac-95af-f023a48253cf"
DEFAULT_THINK_MODEL    = "019c2f86-74db-7cc3-baa5-6891bebb5999"
DEFAULT_IMG_MODEL      = "019abc10-e78d-7932-b725-7f1563ed8a12"
RECAPTCHA_ACTION       = "chat_submit"
MAX_RECAPTCHA_ATTEMPTS = 3


# ================================================================ #
#  Arena helpers (inlined from arena_client.py)
# ================================================================ #

def ensure_extended_config(cfg: dict) -> dict:
    defaults = {
        "search":      False,
        "reasoning":   False,
        "v2_auth":     False,
        "image":       False,
        "image_edit":  False,
        "searchmodel": DEFAULT_SEARCH_MODEL,
        "thinkmodel":  DEFAULT_THINK_MODEL,
        "imgmodel":    DEFAULT_IMG_MODEL,
    }
    changed = False
    for key, val in defaults.items():
        if key not in cfg:
            cfg[key] = val
            changed = True
    if changed:
        save_config(cfg)
    return cfg


def detect_mode(cfg: dict) -> str:
    if cfg.get("image", False):
        return "image_edit" if cfg.get("image_edit", False) else "image"
    if cfg.get("search", False):
        return "search"
    if cfg.get("reasoning", False):
        return "reasoning"
    return "chat"


def resolve_model_id(cfg: dict, mode: str) -> str:
    if mode in ["image", "image_edit"]:
        return cfg.get("imgmodel") or DEFAULT_IMG_MODEL
    if mode == "search":
        return cfg.get("searchmodel") or DEFAULT_SEARCH_MODEL
    if mode == "reasoning":
        return cfg.get("thinkmodel") or DEFAULT_THINK_MODEL
    return cfg.get("modelAId")


def build_base_headers(cfg: dict) -> dict:
    return {
        "accept":          "*/*",
        "accept-language": "en-US,en;q=0.9",
        "origin":          BASE_URL,
        "referer":         f"{BASE_URL}/c/{cfg['eval_id']}",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }


def build_chat_headers(cfg: dict) -> dict:
    h = build_base_headers(cfg)
    h["content-type"] = "application/json"
    return h


def build_search_headers(cfg: dict) -> dict:
    h = build_base_headers(cfg)
    h.update({
        "content-type":       "text/plain;charset=UTF-8",
        "priority":           "u=1, i",
        "sec-ch-ua":          '"Chromium";v="145", "Not:A-Brand";v="99"',
        "sec-ch-ua-arch":     '"x86"',
        "sec-ch-ua-bitness":  '"64"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-model":    '""',
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-dest":     "empty",
        "sec-fetch-mode":     "cors",
        "sec-fetch-site":     "same-origin",
    })
    return h


def _is_recaptcha_validation_failed(status_code: int, text: str) -> bool:
    if status_code != 403:
        return False
    try:
        body = json.loads(text)
        return isinstance(body, dict) and body.get("error") == "recaptcha validation failed"
    except Exception:
        return False


def build_payload(
    cfg: dict,
    mode: str,
    model_id: str,
    prompt_text: str,
    recaptcha_token: str,
    attachment_url: str = None,
    mime_type: str = None,
    recaptcha_v2_token: str = None,
) -> dict:
    modality = "image" if mode in ["image", "image_edit"] else ("search" if mode == "search" else "chat")
    attachments = []
    if attachment_url and mime_type:
        attachments.append({"name": "image.png", "contentType": mime_type, "url": attachment_url})

    payload = {
        "id":              cfg["eval_id"],
        "modelAId":        model_id,
        "userMessageId":   str(uuid.uuid4()),
        "modelAMessageId": str(uuid.uuid4()),
        "userMessage": {
            "content":                  prompt_text,
            "experimental_attachments": attachments,
            "metadata":                 {},
        },
        "modality":         modality,
        "recaptchaV3Token": recaptcha_token,
    }
    if recaptcha_v2_token:
        payload["recaptchaV2Token"] = recaptcha_v2_token
        payload.pop("recaptchaV3Token", None)
    return payload


def _decode_data(data: str) -> str:
    if data.startswith('"') and data.endswith('"'):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            pass
    return data


class CitationAccumulator:
    def __init__(self):
        self._buffer = ""

    def feed(self, raw_data: str):
        try:
            outer = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            return None
        if outer.get("toolCallId") != "citation-source":
            return None
        self._buffer += outer.get("argsTextDelta", "")
        try:
            citation = json.loads(self._buffer)
            self._buffer = ""
            return citation
        except json.JSONDecodeError:
            return None


# ================================================================ #
#  FastAPI app
# ================================================================ #

app = FastAPI(title="Arena OpenAI-Compatible API", version="1.0.0")

ARENA_MODELS = [
    {"id": "arena-chat",      "object": "model", "created": 1700000000, "owned_by": "arena"},
    {"id": "arena-search",    "object": "model", "created": 1700000000, "owned_by": "arena"},
    {"id": "arena-reasoning", "object": "model", "created": 1700000000, "owned_by": "arena"},
    {"id": "arena-image",     "object": "model", "created": 1700000000, "owned_by": "arena"},
]
MODEL_ID_MAP = {m["id"]: m for m in ARENA_MODELS}

MODEL_MODE_MAP = {
    "arena-chat":      {"search": False, "reasoning": False, "image": False, "image_edit": False},
    "arena-search":    {"search": True,  "reasoning": False, "image": False, "image_edit": False},
    "arena-reasoning": {"search": False, "reasoning": True,  "image": False, "image_edit": False},
    "arena-image":     {"search": False, "reasoning": False, "image": True,  "image_edit": False},
}


# ================================================================ #
#  Pydantic models
#
#  KEY FIX: content accepts str OR list of content-part objects.
#  Kilo Code (and other agentic tools) send content as a list when
#  tool results / file reads are attached to a message.
# ================================================================ #

def _flatten_content(content: Any) -> str:
    """
    Convert any content shape to a plain string.
      - str  → returned as-is
      - list → each part's "text" field is joined with newlines
               (non-text parts like images are silently skipped)
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text", "")
            elif hasattr(part, "text"):
                text = part.text or ""
            else:
                text = str(part)
            if text:
                parts.append(text)
        return "\n".join(parts)
    return str(content)


class ChatMessage(BaseModel):
    role:    str
    # Accept str or list — Kilo Code sends lists for agentic messages
    content: Union[str, List[Any]]
    name:    Optional[str] = None

    def text(self) -> str:
        """Always return content as a flat string."""
        return _flatten_content(self.content)


class ChatCompletionRequest(BaseModel):
    model:             str
    messages:          List[ChatMessage]
    stream:            Optional[bool]  = False
    temperature:       Optional[float] = None
    max_tokens:        Optional[int]   = None
    n:                 Optional[int]   = 1
    stop:              Optional[Union[str, List[str]]] = None
    top_p:             Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty:  Optional[float] = None
    user:              Optional[str]   = None


# ================================================================ #
#  Prompt construction
# ================================================================ #

def messages_to_prompt(messages: List[ChatMessage]) -> str:
    """
    Flatten the full conversation history into a single prompt string
    for the Arena API, preserving system instructions and prior turns.
    """
    parts = []
    for msg in messages:
        role    = msg.role.capitalize()
        content = msg.text()
        if content.strip():
            parts.append(f"{role}: {content}")
    parts.append("Assistant:")
    return "\n\n".join(parts)


# ================================================================ #
#  Config helper
# ================================================================ #

def get_patched_config(model_alias: str) -> dict:
    cfg = load_config()
    cfg = ensure_extended_config(cfg)
    cfg.update(MODEL_MODE_MAP.get(model_alias, MODEL_MODE_MAP["arena-chat"]))
    return cfg


def _count_tokens_approx(text: str) -> int:
    return max(1, len(text) // 4)


# ================================================================ #
#  SSE helpers
# ================================================================ #

def _make_chunk(
    completion_id: str,
    created_ts:    int,
    model_name:    str,
    delta:         dict,
    finish_reason: Optional[str] = None,
) -> str:
    return f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created_ts, 'model': model_name, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish_reason}]})}\n\n"


def _make_done() -> str:
    return "data: [DONE]\n\n"


# ================================================================ #
#  Core async streaming generator
# ================================================================ #

async def arena_stream(
    cfg:               dict,
    mode:              str,
    model_id:          str,
    prompt_text:       str,
    recaptcha_token:   str,
    completion_id:     str,
    created_ts:        int,
    openai_model_name: str,
) -> AsyncIterator[str]:
    cookies = {
        "arena-auth-prod-v1.0": cfg["auth_prod"],
        "cf_clearance":          cfg["cf_clearance"],
        "__cf_bm":               cfg["cf_bm"],
    }
    if cfg.get("v2_auth"):
        cookies["domain_migration_completed"] = "true"
        cookies["arena-auth-prod-v1.1"]       = cfg.get("auth_prod_v2", "")

    url     = f"{BASE_URL}/nextjs-api/stream/post-to-evaluation/{cfg['eval_id']}"
    headers = build_search_headers(cfg) if mode in ["search", "image", "image_edit"] else build_chat_headers(cfg)
    if recaptcha_token:
        headers["X-Recaptcha-Token"]  = recaptcha_token
        headers["X-Recaptcha-Action"] = RECAPTCHA_ACTION

    payload      = build_payload(cfg, mode, model_id, prompt_text, recaptcha_token)
    citation_acc = CitationAccumulator() if mode == "search" else None

    # OpenAI spec: emit role delta first
    yield _make_chunk(completion_id, created_ts, openai_model_name, {"role": "assistant", "content": ""})

    async with httpx.AsyncClient(http2=True, timeout=None, cookies=cookies) as client:
        for attempt in range(MAX_RECAPTCHA_ATTEMPTS):
            body_bytes = json.dumps(payload).encode("utf-8")

            async with client.stream("POST", url, headers=headers, content=body_bytes) as response:

                if response.status_code != 200:
                    error_body = ""
                    async for chunk in response.aiter_bytes():
                        error_body += chunk.decode("utf-8", errors="replace")

                    if _is_recaptcha_validation_failed(response.status_code, error_body):
                        if attempt < MAX_RECAPTCHA_ATTEMPTS - 1:
                            v2_token, _ = get_latest_token(version="v2", max_age_seconds=110)
                            if v2_token:
                                payload["recaptchaV2Token"] = v2_token
                                payload.pop("recaptchaV3Token", None)
                                headers.pop("X-Recaptcha-Token", None)
                                headers.pop("X-Recaptcha-Action", None)
                                consume_token(v2_token)
                                continue
                            fresh_v3, _ = get_latest_token(version="v3", max_age_seconds=110)
                            if fresh_v3 and fresh_v3 != recaptcha_token:
                                payload["recaptchaV3Token"] = fresh_v3
                                payload.pop("recaptchaV2Token", None)
                                headers["X-Recaptcha-Token"]  = fresh_v3
                                headers["X-Recaptcha-Action"] = RECAPTCHA_ACTION
                                continue

                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"Arena API error ({response.status_code}): {error_body[:500]}",
                    )

                async for raw_line in response.aiter_lines():
                    if not raw_line:
                        continue
                    m = re.match(r'^([a-z0-9]+):(.*)', raw_line)
                    if not m:
                        continue
                    prefix = m.group(1)
                    data   = m.group(2).strip()

                    if prefix == "ad":
                        yield _make_chunk(completion_id, created_ts, openai_model_name, {}, finish_reason="stop")
                        yield _make_done()
                        return

                    if prefix == "a0":
                        token = _decode_data(data)
                        if token and not should_filter_content(token):
                            yield _make_chunk(completion_id, created_ts, openai_model_name, {"content": token})
                        continue

                    if prefix == "ag" and mode == "reasoning":
                        token = _decode_data(data)
                        if token and not should_filter_content(token):
                            yield _make_chunk(completion_id, created_ts, openai_model_name, {"content": token})
                        continue

                    if prefix == "ac" and citation_acc is not None:
                        citation = citation_acc.feed(data)
                        if citation is not None:
                            for c in (citation if isinstance(citation, list) else [citation]):
                                ref = f"\n\n> [{c.get('title','')}]({c.get('url','')})"
                                yield _make_chunk(completion_id, created_ts, openai_model_name, {"content": ref})
                        continue

                    if prefix == "a2" and mode in ["image", "image_edit"]:
                        try:
                            items = json.loads(data)
                            if isinstance(items, list):
                                for item in items:
                                    if isinstance(item, dict) and item.get("type") == "image":
                                        yield _make_chunk(completion_id, created_ts, openai_model_name, {"content": item.get("image", "")})
                        except (json.JSONDecodeError, TypeError):
                            pass
                        continue

                # Stream ended without "ad"
                yield _make_chunk(completion_id, created_ts, openai_model_name, {}, finish_reason="stop")
                yield _make_done()
                return


# ================================================================ #
#  Non-streaming collector
# ================================================================ #

async def collect_full_response(gen: AsyncIterator[str]) -> str:
    content = []
    async for line in gen:
        line = line.strip()
        if not line or line == "data: [DONE]":
            continue
        if line.startswith("data: "):
            try:
                chunk = json.loads(line[6:])
                token = chunk["choices"][0]["delta"].get("content", "")
                if token:
                    content.append(token)
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
    return "".join(content)


# ================================================================ #
#  Routes
# ================================================================ #

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": ARENA_MODELS}


@app.get("/v1/models/{model_id}")
async def retrieve_model(model_id: str):
    model = MODEL_ID_MAP.get(model_id)
    if not model:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found.")
    return model


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    if request.model not in MODEL_MODE_MAP:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{request.model}' not found. Available: {list(MODEL_MODE_MAP.keys())}",
        )

    cfg      = get_patched_config(request.model)
    mode     = detect_mode(cfg)
    model_id = resolve_model_id(cfg, mode)

    # Build flat prompt from all messages (handles str and list content)
    prompt_text = messages_to_prompt(request.messages)

    recaptcha_token, used_token_data = get_latest_token(version="v3", max_age_seconds=110)
    if not recaptcha_token:
        recaptcha_token, used_token_data = get_latest_token(version=None, max_age_seconds=0)
    if not recaptcha_token:
        raise HTTPException(status_code=503, detail="No reCAPTCHA tokens available. Run the harvester first.")

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created_ts    = int(time.time())

    stream_gen = arena_stream(
        cfg               = cfg,
        mode              = mode,
        model_id          = model_id,
        prompt_text       = prompt_text,
        recaptcha_token   = recaptcha_token,
        completion_id     = completion_id,
        created_ts        = created_ts,
        openai_model_name = request.model,
    )

    # ── Streaming ──
    if request.stream:
        async def event_stream():
            try:
                async for chunk in stream_gen:
                    yield chunk
            finally:
                if used_token_data and recaptcha_token:
                    consume_token(recaptcha_token)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    # ── Non-streaming ──
    try:
        full_content = await collect_full_response(stream_gen)
    finally:
        if used_token_data and recaptcha_token:
            consume_token(recaptcha_token)

    prompt_str        = " ".join(msg.text() for msg in request.messages)
    prompt_tokens     = _count_tokens_approx(prompt_str)
    completion_tokens = _count_tokens_approx(full_content)

    return JSONResponse(content={
        "id":      completion_id,
        "object":  "chat.completion",
        "created": created_ts,
        "model":   request.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": full_content}, "logprobs": None, "finish_reason": "stop"}],
        "usage":   {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
        "system_fingerprint": None,
    })


# ================================================================ #
#  Entrypoint
# ================================================================ #

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")