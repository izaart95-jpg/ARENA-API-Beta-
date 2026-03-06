"""
Arena → OpenAI-Compatible API Server
-------------------------------------
Wraps arena_client.py logic into a FastAPI server that speaks the
OpenAI /v1/chat/completions streaming protocol so tools like opencode
can use it as a drop-in backend.

Setup:   pip install fastapi uvicorn httpx
Run:     uvicorn arena_openai_api:app --host 0.0.0.0 --port 8000

Configure opencode (or any OpenAI-compatible client):
    base_url = "http://localhost:8000/v1"
    api_key  = "any-string"   # not validated, just required by clients
    model    = "arena"        # ignored internally

Per-request overrides (extra fields in the JSON body):
    image      = true              → image generation mode
    image_edit = true              → image edit mode  (requires image=true)
    image_path = "/full/path.png"  → source image for image_edit
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
import asyncio
import httpx

from typing import Any, AsyncIterator, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# -- your existing helpers ---------------------------------------------------
from modula import (
    load_config,
    save_config,
    load_tokens,
    get_latest_token,
    consume_token,
    should_filter_content,
    BASE_URL,
    AUTO_TOKEN,
)


# ============================================================
# ✏️  USER CONFIGURATION  –  edit anything in this section
# ============================================================

# --- Server -----------------------------------------------------------------
SERVER_HOST            = "0.0.0.0"   # bind address
SERVER_PORT            = 8000        # port uvicorn listens on

# --- Model IDs --------------------------------------------------------------
DEFAULT_SEARCH_MODEL   = "019c6f55-308b-71ac-95af-f023a48253cf"  # search / web
DEFAULT_THINK_MODEL    = "019c2f86-74db-7cc3-baa5-6891bebb5999"  # reasoning
DEFAULT_IMG_MODEL      = "019abc10-e78d-7932-b725-7f1563ed8a12"  # image

# --- Image upload -----------------------------------------------------------
# The next-action token for the Arena image-upload handshake endpoint
IMAGE_UPLOAD_NEXT_ACTION = "7012303914af71fce235a732cde90253f7e2986f2b"

# --- reCAPTCHA --------------------------------------------------------------
RECAPTCHA_ACTION       = "chat_submit"
MAX_RECAPTCHA_ATTEMPTS = 2    # retries on captcha failure before giving up

# --- Markdown parser --------------------------------------------------------
# MARKParser – strip markdown from streamed output before it reaches the client.
#   True  → markdown is removed
#   False → raw output, nothing changed
MARK_PARSER_DEFAULT    = True

# CodeParser – only active when MARKParser is True.
#   True  → ONLY strip fenced code-block delimiters (``` lines);
#            bold / headings / links / etc. are left untouched.
#   False → strip ALL markdown (full mode)
CODE_PARSER_DEFAULT    = False

# ============================================================
# end of user configuration
# ============================================================

app = FastAPI(title="Arena OpenAI Bridge")


# ============================================================
# Pydantic models
# ============================================================

class Message(BaseModel):
    role: str
    # ANY type so Pydantic never wraps list items in a non-serializable object.
    # opencode sends content as a plain str OR list of {"type","text"} dicts.
    content: Any = ""

    def get_text(self) -> str:
        """Return content as a plain string, no matter what format arrived."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            parts: list[str] = []
            for part in self.content:
                if isinstance(part, dict) and part.get("type") == "text":
                    t = part.get("text") or ""
                    if t:
                        parts.append(str(t))
                elif isinstance(part, str) and part:
                    parts.append(part)
            return "\n".join(parts)
        return str(self.content) if self.content else ""


class ChatCompletionRequest(BaseModel):
    model: str = "arena"
    messages: List[Message]
    stream: Optional[bool] = True
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # ── Per-request Arena mode overrides ─────────────────────────────────────
    # These take priority over whatever flags are set in the JSON config file.
    #
    #   image generation:  { "image": true }
    #   image edit:        { "image": true, "image_edit": true,
    #                        "image_path": "/absolute/path/to/source.png" }
    image:      Optional[bool] = None
    image_edit: Optional[bool] = None
    image_path: Optional[str]  = None


# ============================================================
# Config / mode helpers
# ============================================================

def _ensure_config(cfg: dict) -> dict:
    defaults = {
        "v2_auth":    False,
        "search":     False,
        "reasoning":  False,
        "image":      False,
        "image_edit": False,
        "searchmodel": DEFAULT_SEARCH_MODEL,
        "thinkmodel":  DEFAULT_THINK_MODEL,
        "imgmodel":    DEFAULT_IMG_MODEL,
        "MARKParser":  MARK_PARSER_DEFAULT,
        "CodeParser":  CODE_PARSER_DEFAULT,
    }
    changed = any(k not in cfg for k in defaults)
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    if changed:
        save_config(cfg)
    return cfg


def _detect_mode(cfg: dict,
                 override_image: bool | None = None,
                 override_image_edit: bool | None = None) -> str:
    """
    Detect mode from config, with optional per-request overrides.
    Request-level override > config file flag.
    """
    use_image      = override_image      if override_image      is not None else cfg.get("image", False)
    use_image_edit = override_image_edit if override_image_edit is not None else cfg.get("image_edit", False)

    if use_image:
        return "image_edit" if use_image_edit else "image"
    if cfg.get("search"):    return "search"
    if cfg.get("reasoning"): return "reasoning"
    return "chat"


def _resolve_model(cfg: dict, mode: str) -> str:
    if mode in ("image", "image_edit"): return cfg.get("imgmodel") or DEFAULT_IMG_MODEL
    if mode == "search":                return cfg.get("searchmodel") or DEFAULT_SEARCH_MODEL
    if mode == "reasoning":             return cfg.get("thinkmodel") or DEFAULT_THINK_MODEL
    return cfg.get("modelAId", "")


# ============================================================
# Header builders
# ============================================================

def _base_headers(cfg: dict) -> dict:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/c/{cfg['eval_id']}",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }


def _chat_headers(cfg: dict) -> dict:
    h = _base_headers(cfg)
    h["content-type"] = "application/json"
    return h


def _search_headers(cfg: dict) -> dict:
    h = _base_headers(cfg)
    h.update({
        "content-type": "text/plain;charset=UTF-8",
        "priority": "u=1, i",
        "sec-ch-ua": '"Chromium";v="145", "Not:A-Brand";v="99"',
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    return h


# ============================================================
# Image upload helpers
# ============================================================

_MIME_MAP = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
    ".gif":  "image/gif",
}


def _load_image_from_path(file_path: str) -> tuple[bytes, str]:
    """Read an image from disk. Returns (image_bytes, mime_type)."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Image not found: {file_path}")
    ext  = os.path.splitext(file_path)[1].lower()
    mime = _MIME_MAP.get(ext, "image/png")
    with open(file_path, "rb") as f:
        return f.read(), mime


def _upload_image_handshake(client: httpx.Client,
                             cfg: dict,
                             image_bytes: bytes,
                             mime_type: str) -> str:
    """
    Execute the Arena 2-step image upload handshake (synchronous – run in
    executor alongside _do_request). Returns the signed Cloudflare URL used
    as the attachment URL in the payload.

    Step 1: POST to the eval page with next-action header → get a signed URL.
    Step 2: PUT image bytes to that signed URL.
    """
    reserve_url = f"{BASE_URL}/c/{cfg['eval_id']}"
    headers = _base_headers(cfg)
    headers["next-action"]  = IMAGE_UPLOAD_NEXT_ACTION
    headers["content-type"] = "application/json"

    res = client.post(
        reserve_url,
        headers=headers,
        content=json.dumps(["image.png", mime_type]).encode(),
    )
    res.raise_for_status()

    match = re.search(
        r'https://[^\s"\'\\]+\.cloudflarestorage\.com[^\s"\'\\]+', res.text
    )
    if not match:
        raise RuntimeError("Failed to extract signed URL from upload handshake response.")
    signed_url = match.group(0).replace("\\u0026", "&")

    upload_res = client.put(
        signed_url,
        headers={"Content-Type": mime_type},
        content=image_bytes,
    )
    upload_res.raise_for_status()
    return signed_url


# ============================================================
# Payload builder
# ============================================================

def _build_payload(cfg: dict, mode: str, model_id: str,
                   prompt: str, recaptcha_token: str,
                   attachment_url: str | None = None,
                   mime_type: str | None = None) -> dict:
    modality = (
        "image"  if mode in ("image", "image_edit")
        else "search" if mode == "search"
        else "chat"
    )
    attachments = []
    if attachment_url and mime_type:
        attachments.append({
            "name":        "image.png",
            "contentType": mime_type,
            "url":         attachment_url,
        })
    return {
        "id":              cfg["eval_id"],
        "modelAId":        model_id,
        "userMessageId":   str(uuid.uuid4()),
        "modelAMessageId": str(uuid.uuid4()),
        "userMessage": {
            "content":                  prompt,
            "experimental_attachments": attachments,
            "metadata":                 {},
        },
        "modality":         modality,
        "recaptchaV3Token": recaptcha_token,
    }


# ============================================================
# reCAPTCHA
# ============================================================

def _is_recaptcha_failure(status: int, body: str) -> bool:
    if status != 403:
        return False
    try:
        return json.loads(body).get("error") == "recaptcha validation failed"
    except Exception:
        return False


def _get_token() -> str:
    token, _ = get_latest_token(version="v3", max_age_seconds=110)
    if not token:
        token, _ = get_latest_token(version=None, max_age_seconds=0)
    return token or ""


# ============================================================
# SSE / chunk helpers
# ============================================================

def _decode_token(raw: str) -> str:
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return raw


def _openai_chunk(content: str, finish: bool = False) -> str:
    if finish:
        obj = {
            "id": "chatcmpl-arena", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    else:
        obj = {
            "id": "chatcmpl-arena", "object": "chat.completion.chunk",
            "choices": [{"index": 0,
                         "delta": {"role": "assistant", "content": content},
                         "finish_reason": None}],
        }
    return f"data: {json.dumps(obj)}\n\n"


def _reasoning_chunk(token: str) -> str:
    """Reasoning/thinking tokens – non-standard delta field, compatible with
    clients that support reasoning_content (e.g. opencode with reasoning mode)."""
    obj = {
        "id": "chatcmpl-arena", "object": "chat.completion.chunk",
        "choices": [{"index": 0,
                     "delta": {"role": "assistant", "reasoning_content": token},
                     "finish_reason": None}],
    }
    return f"data: {json.dumps(obj)}\n\n"


def _citation_chunk(citation: dict) -> str:
    obj = {
        "id": "chatcmpl-arena", "object": "chat.completion.chunk",
        "choices": [{"index": 0,
                     "delta": {"citations": [citation]},
                     "finish_reason": None}],
    }
    return f"data: {json.dumps(obj)}\n\n"


def _image_chunk(image_url: str, mime_type: str = "image/png") -> str:
    """Image generation result – OpenAI image-response envelope."""
    obj = {"data": [{"url": image_url, "revised_prompt": None}]}
    return f"data: {json.dumps(obj)}\n\n"


# ============================================================
# Citation accumulator  (search mode)
# ============================================================

class _CitationAccumulator:
    """Reassembles streamed citation JSON fragments arriving on the ac prefix."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, raw_data: str) -> dict | None:
        try:
            outer = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            return None
        if outer.get("toolCallId") != "citation-source":
            return None
        self._buf += outer.get("argsTextDelta", "")
        try:
            citation  = json.loads(self._buf)
            self._buf = ""
            return citation
        except json.JSONDecodeError:
            return None


# ============================================================
# Markdown / CodeFence parser
# ============================================================

class _StreamMarkdownStripper:
    """
    Stateful, token-by-token markdown stripper.

    Modes
    -----
    MARKParser=True,  CodeParser=False  →  strip ALL markdown syntax.
    MARKParser=True,  CodeParser=True   →  strip ONLY fenced code-block
                                           delimiters (``` lines).
    MARKParser=False                    →  pass-through, nothing changed.
    """

    _HEADING     = re.compile(r'^#{1,6}\s+', re.M)
    _BOLD_ITALIC = re.compile(r'\*{1,3}([^*\n]+)\*{1,3}')
    _UNDER_BI    = re.compile(r'_{1,3}([^_\n]+)_{1,3}')
    _STRIKE      = re.compile(r'~~([^~\n]+)~~')
    _INLINE_CODE = re.compile(r'`{1,2}([^`]+)`{1,2}')
    _BLOCKQUOTE  = re.compile(r'^\s*>\s?', re.M)
    _HR          = re.compile(r'^[-*_]{3,}\s*$', re.M)
    _IMAGE       = re.compile(r'!\[([^\]]*)\]\([^)]*\)')
    _LINK        = re.compile(r'\[([^\]]+)\]\([^)]+\)')
    _REF_LINK    = re.compile(r'\[([^\]]+)\]\[[^\]]*\]')
    _LINK_DEF    = re.compile(r'^\[[^\]]+\]:\s+\S+.*$', re.M)
    _UL          = re.compile(r'^\s*[-*+]\s+', re.M)
    _OL          = re.compile(r'^\s*\d+\.\s+', re.M)
    _TABLE_SEP   = re.compile(r'^\|?[\s:|-]+\|[\s:|-]*\|?\s*$', re.M)
    _TABLE_PIPE  = re.compile(r'\|')
    _MULTI_BLANK = re.compile(r'\n{3,}')

    def __init__(self, mark_parser: bool, code_parser: bool) -> None:
        self.mark_parser = mark_parser
        self.code_parser = code_parser and mark_parser
        self._buf        = ""
        self._in_fence   = False

    def feed(self, token: str) -> str:
        if not self.mark_parser:
            return token
        self._buf += token
        if "\n" in self._buf:
            safe, self._buf = self._buf.rsplit("\n", 1)
            return self._process(safe + "\n")
        return ""

    def flush(self) -> str:
        if not self.mark_parser or not self._buf:
            return ""
        out, self._buf = self._process(self._buf), ""
        return out

    def _process(self, text: str) -> str:
        return self._strip_fences_only(text) if self.code_parser else self._strip_all(text)

    def _strip_fences_only(self, text: str) -> str:
        out = []
        for line in text.split("\n"):
            if line.strip().startswith("```"):
                self._in_fence = not self._in_fence
                continue
            out.append(line)
        return "\n".join(out)

    def _strip_all(self, text: str) -> str:
        result_lines = []
        for line in text.split("\n"):
            if line.strip().startswith("```"):
                self._in_fence = not self._in_fence
                continue
            result_lines.append(line)
        text = "\n".join(result_lines)
        text = self._INLINE_CODE.sub(r'\1', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = self._HEADING.sub('', text)
        text = self._BOLD_ITALIC.sub(r'\1', text)
        text = self._UNDER_BI.sub(r'\1', text)
        text = self._STRIKE.sub(r'\1', text)
        text = self._BLOCKQUOTE.sub('', text)
        text = self._HR.sub('', text)
        text = self._IMAGE.sub(r'\1', text)
        text = self._LINK.sub(r'\1', text)
        text = self._REF_LINK.sub(r'\1', text)
        text = self._LINK_DEF.sub('', text)
        text = self._UL.sub('', text)
        text = self._OL.sub('', text)
        text = self._TABLE_SEP.sub('', text)
        text = self._TABLE_PIPE.sub(' ', text)
        text = self._MULTI_BLANK.sub('\n\n', text)
        return text


# ============================================================
# Core streaming generator
# ============================================================

async def _stream_arena(
    prompt: str,
    override_image:      bool | None = None,
    override_image_edit: bool | None = None,
    image_path:          str  | None = None,
) -> AsyncIterator[str]:

    cfg      = load_config()
    cfg      = _ensure_config(cfg)
    mode     = _detect_mode(cfg, override_image, override_image_edit)
    model_id = _resolve_model(cfg, mode)

    parser = _StreamMarkdownStripper(
        mark_parser=cfg.get("MARKParser", MARK_PARSER_DEFAULT),
        code_parser=cfg.get("CodeParser", CODE_PARSER_DEFAULT),
    )
    citation_acc = _CitationAccumulator() if mode == "search" else None

    recaptcha_token = _get_token()

    auth_key = "arena-auth-prod-v1.0" if cfg.get("v2_auth") else "arena-auth-prod-v1"
    cookies: dict = {
        auth_key:       cfg["auth_prod"],
        "cf_clearance": cfg["cf_clearance"],
        "__cf_bm":      cfg["cf_bm"],
    }
    if cfg.get("v2_auth"):
        cookies["domain_migration_completed"] = "true"
        cookies["arena-auth-prod-v1.1"]       = cfg.get("auth_prod_v2", "")

    url     = f"{BASE_URL}/nextjs-api/stream/post-to-evaluation/{cfg['eval_id']}"
    headers = (
        _search_headers(cfg)
        if mode in ("search", "image", "image_edit")
        else _chat_headers(cfg)
    )
    if recaptcha_token:
        headers["X-Recaptcha-Token"]  = recaptcha_token
        headers["X-Recaptcha-Action"] = RECAPTCHA_ACTION

    loop = asyncio.get_event_loop()

    def _do_request() -> list:
        chunks: list = []

        with httpx.Client(http2=True, timeout=None, cookies=cookies) as client:

            # ── Image upload handshake (image_edit only) ─────────────────────
            attachment_url: str | None = None
            attach_mime:    str | None = None

            if mode == "image_edit":
                if not image_path:
                    chunks.append("__ERROR__:image_edit requires image_path in request body.")
                    return chunks
                try:
                    img_bytes, attach_mime = _load_image_from_path(image_path)
                    attachment_url = _upload_image_handshake(
                        client, cfg, img_bytes, attach_mime
                    )
                except Exception as exc:
                    chunks.append(f"__ERROR__:Image upload failed: {exc}")
                    return chunks

            payload = _build_payload(
                cfg, mode, model_id, prompt, recaptcha_token,
                attachment_url, attach_mime,
            )

            for attempt in range(MAX_RECAPTCHA_ATTEMPTS):
                req_headers = dict(headers)
                body_bytes  = json.dumps(payload).encode("utf-8")

                # search / image modes require text/plain content-type
                if mode not in ("search", "image", "image_edit"):
                    req_headers["content-type"] = "application/json"

                with client.stream("POST", url,
                                   headers=req_headers,
                                   content=body_bytes) as response:

                    if response.status_code != 200:
                        error_body = (b"".join(response.iter_bytes())
                                      .decode("utf-8", errors="replace"))

                        if _is_recaptcha_failure(response.status_code, error_body):
                            if attempt < MAX_RECAPTCHA_ATTEMPTS - 1:
                                v2, _ = get_latest_token(version="v2", max_age_seconds=110)
                                if v2:
                                    payload["recaptchaV2Token"] = v2
                                    payload.pop("recaptchaV3Token", None)
                                    req_headers.pop("X-Recaptcha-Token", None)
                                    req_headers.pop("X-Recaptcha-Action", None)
                                    headers.pop("X-Recaptcha-Token", None)
                                    headers.pop("X-Recaptcha-Action", None)
                                    consume_token(v2)
                                    continue
                                fv3, _ = get_latest_token(version="v3", max_age_seconds=110)
                                if fv3 and fv3 != recaptcha_token:
                                    payload["recaptchaV3Token"] = fv3
                                    payload.pop("recaptchaV2Token", None)
                                    headers["X-Recaptcha-Token"]  = fv3
                                    headers["X-Recaptcha-Action"] = RECAPTCHA_ACTION
                                    continue
                            chunks.append(f"__ERROR__:reCAPTCHA failed: {error_body[:200]}")
                            return chunks

                        chunks.append(f"__ERROR__:Arena {response.status_code}: {error_body[:200]}")
                        return chunks

                    # ── Refresh auth cookie if Tokenizer flag is on ──────────
                    if cfg.get("Tokenizer"):
                        new_tok = response.cookies.get(auth_key)
                        if new_tok:
                            cfg["auth_prod"] = new_tok
                            save_config(cfg)

                    for line in response.iter_lines():
                        chunks.append(line)
                    return chunks

        return chunks

    raw_lines: list = await loop.run_in_executor(None, _do_request)

    if recaptcha_token:
        consume_token(recaptcha_token)

    for raw_line in raw_lines:
        if not raw_line:
            continue

        # Hard error injected by _do_request
        if raw_line.startswith("__ERROR__:"):
            yield _openai_chunk(raw_line[len("__ERROR__:"):])
            break

        m = re.match(r'^([a-z0-9]+):(.*)', raw_line)
        if not m:
            continue
        prefix, data = m.group(1), m.group(2).strip()

        # ad → stream finished
        if prefix == "ad":
            break

        # a0 → main text token
        if prefix == "a0":
            token = _decode_token(data)
            if token and not should_filter_content(token):
                cleaned = parser.feed(token)
                if cleaned:
                    yield _openai_chunk(cleaned)
            continue

        # ag → reasoning / thinking token
        if prefix == "ag":
            if mode == "reasoning":
                token = _decode_token(data)
                if token and not should_filter_content(token):
                    yield _reasoning_chunk(token)
            continue

        # ac → citation fragment (search mode)
        if prefix == "ac":
            if mode == "search" and citation_acc is not None:
                citation = citation_acc.feed(data)
                if citation is not None:
                    yield _citation_chunk(citation)
            continue

        # a2 → image generation result
        if prefix == "a2":
            if mode in ("image", "image_edit"):
                try:
                    items = json.loads(data)
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict) and item.get("type") == "image":
                                img_url  = item.get("image", "")
                                img_mime = item.get("mimeType", "image/png")
                                if img_url:
                                    yield _image_chunk(img_url, img_mime)
                except (json.JSONDecodeError, TypeError):
                    pass
            continue

    # Flush any remaining buffered markdown
    tail = parser.flush()
    if tail:
        yield _openai_chunk(tail)

    yield _openai_chunk("", finish=True)
    yield "data: [DONE]\n\n"


# ============================================================
# Routes
# ============================================================

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{
            "id": "arena", "object": "model",
            "created": int(time.time()), "owned_by": "arena",
        }],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """
    OpenAI-compatible streaming endpoint.

    Chat / search / reasoning (mode set in config):
        POST { "model": "arena", "messages": [...] }

    Image generation:
        POST { ..., "image": true }

    Image edit:
        POST { ..., "image": true, "image_edit": true,
                    "image_path": "/absolute/path/to/source.png" }

    Markdown stripping is controlled by MARKParser / CodeParser in the config.
    """
    prompt = ""
    for msg in reversed(request.messages):
        if msg.role == "user":
            prompt = msg.get_text()
            break

    if not prompt:
        raise HTTPException(status_code=400, detail="No user message found.")

    if request.image_edit and not request.image_path:
        raise HTTPException(
            status_code=400,
            detail="image_edit=true requires image_path to be provided.",
        )
    if request.image_path and not os.path.exists(request.image_path):
        raise HTTPException(
            status_code=400,
            detail=f"image_path not found on server: {request.image_path}",
        )

    return StreamingResponse(
        _stream_arena(
            prompt,
            override_image=request.image,
            override_image_edit=request.image_edit,
            image_path=request.image_path,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# Dev entrypoint
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("arena_openai_api:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)