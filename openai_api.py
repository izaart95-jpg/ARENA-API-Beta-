"""
Arena.ai → OpenAI-Compatible Proxy Server
==========================================
Receives OpenAI-format requests, forwards them to arena.ai, responds in
OpenAI-compatible streaming (or non-streaming) format.

Features:
  - Full OpenAI /v1/chat/completions compatibility
  - Auto-loads reCAPTCHA tokens from tokens.json (v3 preferred, v2 fallback)
  - Config via config.json (prompts on first run if missing)
  - Streaming by default; non-streaming when client sends stream:false
  - Search mode via "search": true in request extras or config
  - Reasoning mode via "reasoning": true / "think": true in request extras or config
  - Image generation via "image": true in request extras or config
  - Image edit via "image": true + "image_edit": true + "image_path": "<path>" in request
  - reCAPTCHA v3 → v2 fallback on 403
  - Token rotation & consumption after use
  - Cookie refresh (Tokenizer)

Usage:
    pip install fastapi uvicorn httpx[http2]
    python server.py

Then point any OpenAI client at:
    http://localhost:8000/v1
    api_key = "any-value"   (ignored)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL          = "https://arena.ai"
CONFIG_FILE       = "config.json"
TOKENS_FILE       = "tokens.json"
PROXY_PORT        = 8000
MAX_CAPTCHA_RETRY = 2
TOKEN_MAX_AGE     = 110   # seconds — matches arena_client.py

DEFAULT_SEARCH_MODEL = "019c6f55-308b-71ac-95af-f023a48253cf"
DEFAULT_THINK_MODEL  = "019c2f86-74db-7cc3-baa5-6891bebb5999"
DEFAULT_IMG_MODEL    = "019abc10-e78d-7932-b725-7f1563ed8a12"
DEFAULT_CHAT_MODEL   = "019c6d29-a30c-7e20-9bd0-6650af926623"   # claude-sonnet-4-6

# ─────────────────────────────────────────────────────────────────────────────
# Config helpers  (mirrors modula.py / main.py)
# ─────────────────────────────────────────────────────────────────────────────

def _default_config() -> dict:
    return {
        "auth_prod":      "",
        "auth_prod_v2":   "",
        "cf_clearance":   "",
        "cf_bm":          "",
        "eval_id":        "",
        "modelAId":       DEFAULT_CHAT_MODEL,
        "OPENPARSER":     True,
        "Tokenizer":      True,
        "AUTO_TOKEN":     True,
        "v2_auth":        False,
        "searchmodel":    DEFAULT_SEARCH_MODEL,
        "thinkmodel":     DEFAULT_THINK_MODEL,
        "imgmodel":       DEFAULT_IMG_MODEL,
    }


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            # Backfill any missing keys
            defaults = _default_config()
            changed = False
            for k, v in defaults.items():
                if k not in cfg:
                    cfg[k] = v
                    changed = True
            if changed:
                save_config(cfg)
            return cfg
        except Exception:
            pass
    return _default_config()


def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def prompt_missing_config(cfg: dict) -> dict:
    """
    Interactively fill in any required fields that are missing.
    Called once at server startup if config is incomplete.
    """
    changed = False

    # v2_auth detection
    if "v2_auth" not in cfg or cfg["v2_auth"] is None:
        print("\nAre you using a logged-in Arena account?")
        print("  1. Yes  (uses arena-auth-prod-v1.0 + v1.1)")
        print("  2. No   (uses arena-auth-prod-v1)")
        choice = input("Select (1/2): ").strip()
        cfg["v2_auth"] = (choice == "1")
        changed = True

    auth_label = "arena-auth-prod-v1.0" if cfg.get("v2_auth") else "arena-auth-prod-v1"

    if not cfg.get("auth_prod"):
        cfg["auth_prod"] = input(f"Enter {auth_label} cookie: ").strip()
        changed = True

    if cfg.get("v2_auth") and not cfg.get("auth_prod_v2"):
        cfg["auth_prod_v2"] = input("Enter arena-auth-prod-v1.1 cookie: ").strip()
        changed = True

    if not cfg.get("cf_clearance"):
        cfg["cf_clearance"] = input("Enter cf_clearance cookie: ").strip()
        changed = True

    if not cfg.get("cf_bm"):
        cfg["cf_bm"] = input("Enter __cf_bm cookie: ").strip()
        changed = True

    if not cfg.get("eval_id"):
        cfg["eval_id"] = input("Enter Evaluation ID (arena.ai/c/<id>): ").strip()
        changed = True

    if not cfg.get("modelAId"):
        cfg["modelAId"] = input(f"Enter default model ID (default: {DEFAULT_CHAT_MODEL}): ").strip() or DEFAULT_CHAT_MODEL
        changed = True

    if changed:
        save_config(cfg)
        print("✅ Config saved to", CONFIG_FILE)

    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Token helpers  (mirrors modula.py)
# ─────────────────────────────────────────────────────────────────────────────

_tokens_lock = asyncio.Lock()


def _load_tokens_raw() -> dict:
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"tokens": [], "total_count": 0, "last_updated": ""}


def _save_tokens_raw(data: dict) -> None:
    tmp = TOKENS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, TOKENS_FILE)


def get_latest_token(version: Optional[str] = "v3", max_age: int = TOKEN_MAX_AGE):
    """Return (token_str, token_dict) or (None, None)."""
    data = _load_tokens_raw()
    tokens: list[dict] = data.get("tokens", [])

    if version:
        tokens = [t for t in tokens if t.get("version") == version]

    if not tokens:
        return None, None

    tokens_sorted = sorted(tokens, key=lambda x: x.get("timestamp_utc", ""), reverse=True)
    latest = tokens_sorted[0]

    if max_age > 0:
        try:
            ts = datetime.fromisoformat(latest["timestamp_utc"].rstrip("Z")).replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > max_age:
                return None, None
        except Exception:
            pass

    return latest["token"], latest


def consume_token(token_str: str) -> bool:
    data = _load_tokens_raw()
    before = len(data.get("tokens", []))
    data["tokens"] = [t for t in data["tokens"] if t.get("token") != token_str]
    data["total_count"] = len(data["tokens"])
    data["last_updated"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_tokens_raw(data)
    return len(data["tokens"]) < before


def pick_token() -> tuple[Optional[str], Optional[dict]]:
    """Try v3 first, then v2, then any token."""
    tok, meta = get_latest_token("v3", TOKEN_MAX_AGE)
    if tok:
        return tok, meta
    tok, meta = get_latest_token("v2", TOKEN_MAX_AGE)
    if tok:
        return tok, meta
    tok, meta = get_latest_token(None, 0)   # any age
    return tok, meta


# ─────────────────────────────────────────────────────────────────────────────
# Request mode detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_mode(body: dict, cfg: dict) -> str:
    """
    Priority: image_edit > image > search > reasoning > chat.
    Checks request body extras first, then config flags.
    """
    img        = body.get("image",      cfg.get("image",      False))
    img_edit   = body.get("image_edit", cfg.get("image_edit", False))
    search     = body.get("search",     cfg.get("search",     False))
    reasoning  = body.get("reasoning",  body.get("think", cfg.get("reasoning", False)))

    if img:
        if img_edit:
            return "image_edit"
        return "image"
    if search:
        return "search"
    if reasoning:
        return "reasoning"
    return "chat"


def resolve_model(body: dict, cfg: dict, mode: str) -> str:
    """Return arena model ID for the current mode."""
    # Allow client to pass explicit arena model id via "arena_model" or "model" if it looks like a UUID
    client_model = body.get("arena_model") or body.get("model", "")
    uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)
    if client_model and uuid_pattern.match(client_model):
        return client_model

    if mode in ("image", "image_edit"):
        return cfg.get("imgmodel") or DEFAULT_IMG_MODEL
    if mode == "search":
        return cfg.get("searchmodel") or DEFAULT_SEARCH_MODEL
    if mode == "reasoning":
        return cfg.get("thinkmodel") or DEFAULT_THINK_MODEL
    return cfg.get("modelAId") or DEFAULT_CHAT_MODEL


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI message → arena prompt
# ─────────────────────────────────────────────────────────────────────────────

def messages_to_prompt(messages: list[dict]) -> str:
    """
    Flatten OpenAI-style messages into a single prompt string.
    System messages are prefixed; assistant turns are labelled.
    """
    parts = []
    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content", "")

        # content can be a list (vision / multi-part)
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            content = " ".join(text_parts)

        if role == "system":
            parts.append(f"[System]: {content}")
        elif role == "assistant":
            parts.append(f"[Assistant]: {content}")
        else:
            parts.append(content)

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Arena HTTP headers / cookies
# ─────────────────────────────────────────────────────────────────────────────

def build_cookies(cfg: dict) -> dict:
    if cfg.get("v2_auth"):
        return {
            "arena-auth-prod-v1.0":    cfg["auth_prod"],
            "arena-auth-prod-v1.1":    cfg.get("auth_prod_v2", ""),
            "cf_clearance":            cfg["cf_clearance"],
            "__cf_bm":                 cfg["cf_bm"],
            "domain_migration_completed": "true",
        }
    return {
        "arena-auth-prod-v1": cfg["auth_prod"],
        "cf_clearance":       cfg["cf_clearance"],
        "__cf_bm":            cfg["cf_bm"],
    }


def build_headers(cfg: dict, mode: str, recaptcha_token: Optional[str] = None) -> dict:
    base = {
        "accept":           "*/*",
        "accept-language":  "en-US,en;q=0.9",
        "origin":           BASE_URL,
        "referer":          f"{BASE_URL}/c/{cfg['eval_id']}",
        "user-agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }

    if mode in ("search", "image", "image_edit"):
        base["content-type"] = "text/plain;charset=UTF-8"
        base.update({
            "priority":            "u=1, i",
            "sec-ch-ua":           '"Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile":    "?0",
            "sec-ch-ua-platform":  '"Windows"',
            "sec-fetch-dest":      "empty",
            "sec-fetch-mode":      "cors",
            "sec-fetch-site":      "same-origin",
        })
    else:
        base["content-type"] = "application/json"

    if recaptcha_token:
        base["X-Recaptcha-Token"]  = recaptcha_token
        base["X-Recaptcha-Action"] = "chat_submit"

    return base


# ─────────────────────────────────────────────────────────────────────────────
# Arena payload builder
# ─────────────────────────────────────────────────────────────────────────────

def build_arena_payload(
    cfg:             dict,
    mode:            str,
    model_id:        str,
    prompt:          str,
    recaptcha_token: Optional[str],
    attachment_url:  Optional[str] = None,
    mime_type:       Optional[str] = None,
    v2_token:        Optional[str] = None,
) -> dict:
    modality_map = {
        "chat":       "chat",
        "reasoning":  "chat",
        "search":     "search",
        "image":      "image",
        "image_edit": "image",
    }

    attachments = []
    if attachment_url and mime_type:
        attachments.append({"name": "image.png", "contentType": mime_type, "url": attachment_url})

    payload: dict = {
        "id":               cfg["eval_id"],
        "modelAId":         model_id,
        "userMessageId":    str(uuid.uuid4()),
        "modelAMessageId":  str(uuid.uuid4()),
        "userMessage": {
            "content":                  prompt,
            "experimental_attachments": attachments,
            "metadata":                 {},
        },
        "modality":         modality_map.get(mode, "chat"),
    }

    if v2_token:
        payload["recaptchaV2Token"] = v2_token
    elif recaptcha_token:
        payload["recaptchaV3Token"] = recaptcha_token

    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Image upload handshake  (mirrors main.py)
# ─────────────────────────────────────────────────────────────────────────────

def _upload_image(client: httpx.Client, cfg: dict, image_bytes: bytes, mime_type: str) -> str:
    """Two-step Cloudflare upload. Returns signed URL (used as attachment_url)."""
    reserve_url = f"{BASE_URL}/c/{cfg['eval_id']}"
    headers = {
        "accept":           "*/*",
        "accept-language":  "en-US,en;q=0.9",
        "origin":           BASE_URL,
        "referer":          f"{BASE_URL}/c/{cfg['eval_id']}",
        "user-agent":       "Mozilla/5.0",
        "next-action":      "7012303914af71fce235a732cde90253f7e2986f2b",
        "content-type":     "application/json",
    }
    res = client.post(reserve_url, headers=headers, json=["image.png", mime_type])
    res.raise_for_status()

    match = re.search(r'https://[^\s"\'\\]+\.cloudflarestorage\.com[^\s"\'\\]+', res.text)
    if not match:
        raise RuntimeError("Failed to extract signed upload URL from arena response.")

    signed_url = match.group(0).replace("\\u0026", "&")

    up = client.put(signed_url, headers={"Content-Type": mime_type}, content=image_bytes)
    up.raise_for_status()
    return signed_url


def _read_image_file(path: str) -> tuple[bytes, str]:
    """Read image from disk and return (bytes, mime_type)."""
    ext = os.path.splitext(path)[1].lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png",  ".webp": "image/webp", ".gif": "image/gif"}
    mime = mime_map.get(ext, "image/png")
    with open(path, "rb") as f:
        return f.read(), mime


def _decode_b64_image(b64: str) -> tuple[bytes, str]:
    """Decode a base64 string (with or without data URI prefix)."""
    mime = "image/png"
    if b64.startswith("data:"):
        header, b64 = b64.split(",", 1)
        mime = header.split(";")[0].split(":")[1]
    return base64.b64decode(b64), mime


# ─────────────────────────────────────────────────────────────────────────────
# Stream decoder  (mirrors main.py process_stream)
# ─────────────────────────────────────────────────────────────────────────────

MARKDOWN_BLOCK_RE = re.compile(r'^```\w*\n?$')


def _decode_data(raw: str) -> str:
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return json.loads(raw)
        except Exception:
            pass
    return raw


def _should_filter(content: str) -> bool:
    if content and "[{" in content and "heartbeat" in content:
        return True
    if MARKDOWN_BLOCK_RE.match(content.strip()):
        return True
    if content.strip() == "```":
        return True
    return False


# OpenAI SSE chunk builders

def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _content_chunk(text: str, model: str = "arena-proxy") -> str:
    return _sse({
        "id":      f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object":  "chat.completion.chunk",
        "model":   model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    })


def _reasoning_chunk(text: str, model: str = "arena-proxy") -> str:
    return _sse({
        "id":      f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object":  "chat.completion.chunk",
        "model":   model,
        "choices": [{"index": 0, "delta": {"reasoning_content": text}, "finish_reason": None}],
    })


def _image_chunk(url: str, model: str = "arena-proxy") -> str:
    return _sse({
        "id":     f"img-{uuid.uuid4().hex[:8]}",
        "object": "image.generation.chunk",
        "model":  model,
        "data":   [{"url": url, "revised_prompt": None}],
    })


def _done_chunk(model: str = "arena-proxy") -> str:
    done = _sse({
        "id":      f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object":  "chat.completion.chunk",
        "model":   model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    return done + "data: [DONE]\n\n"


def _non_streaming_response(content: str, model: str = "arena-proxy") -> dict:
    return {
        "id":      f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object":  "chat.completion",
        "model":   model,
        "choices": [{
            "index":         0,
            "message":       {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _error_chunk(msg: str) -> str:
    return _sse({"error": {"message": msg, "type": "proxy_error"}})


# ─────────────────────────────────────────────────────────────────────────────
# Citation accumulator (for search mode)
# ─────────────────────────────────────────────────────────────────────────────

class CitationAccumulator:
    def __init__(self):
        self._buf = ""

    def feed(self, raw: str) -> Optional[dict]:
        try:
            outer = json.loads(raw)
        except Exception:
            return None
        if outer.get("toolCallId") != "citation-source":
            return None
        self._buf += outer.get("argsTextDelta", "")
        try:
            result = json.loads(self._buf)
            self._buf = ""
            return result
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Core streaming generator
# ─────────────────────────────────────────────────────────────────────────────

async def _arena_stream(
    cfg:       dict,
    mode:      str,
    model_id:  str,
    prompt:    str,
    do_stream: bool,
    image_source: Optional[str] = None,   # file path or base64 string
) -> AsyncIterator[str]:
    """
    Async generator that:
      1. Gets a reCAPTCHA token (v3 preferred)
      2. Optionally uploads an image (image_edit mode)
      3. Streams the arena response and emits OpenAI-compatible SSE chunks
      4. Handles reCAPTCHA 403 with v2 fallback
      5. Consumes the token on success
    """
    model_label = model_id  # used in SSE payloads

    # ── Token acquisition ────────────────────────────────────────────────────
    recaptcha_token, token_meta = pick_token()
    if not recaptcha_token:
        msg = "No reCAPTCHA tokens available. Run the harvester first."
        if do_stream:
            yield _error_chunk(msg)
            yield "data: [DONE]\n\n"
        else:
            yield json.dumps({"error": {"message": msg}})
        return

    used_v2 = recaptcha_token if token_meta and token_meta.get("version") == "v2" else None

    # ── Build cookies & URL ──────────────────────────────────────────────────
    cookies = build_cookies(cfg)
    url     = f"{BASE_URL}/nextjs-api/stream/post-to-evaluation/{cfg['eval_id']}"

    # ── Image upload (image_edit) ────────────────────────────────────────────
    attachment_url: Optional[str] = None
    mime_type:      Optional[str] = None

    if mode == "image_edit" and image_source:
        try:
            if image_source.startswith("data:") or len(image_source) > 260:
                img_bytes, mime_type = _decode_b64_image(image_source)
            else:
                if not os.path.exists(image_source):
                    raise FileNotFoundError(f"Image not found: {image_source}")
                img_bytes, mime_type = _read_image_file(image_source)

            with httpx.Client(http2=True, timeout=60, cookies=cookies) as upload_client:
                attachment_url = _upload_image(upload_client, cfg, img_bytes, mime_type)
            print(f"[proxy] Image uploaded OK")
        except Exception as e:
            msg = f"Image upload failed: {e}"
            if do_stream:
                yield _error_chunk(msg)
                yield "data: [DONE]\n\n"
            else:
                yield json.dumps({"error": {"message": msg}})
            return

    # ── Retry loop (captcha fallback) ────────────────────────────────────────
    accumulated_content = []   # for non-streaming mode
    citation_acc = CitationAccumulator() if mode == "search" else None

    for attempt in range(MAX_CAPTCHA_RETRY):
        is_v2 = used_v2 is not None
        headers = build_headers(cfg, mode, recaptcha_token if not is_v2 else None)
        payload = build_arena_payload(
            cfg, mode, model_id, prompt,
            recaptcha_token if not is_v2 else None,
            attachment_url, mime_type,
            v2_token=used_v2,
        )

        # Send
        try:
            async with httpx.AsyncClient(http2=True, timeout=None, cookies=cookies) as client:
                async with client.stream(
                    "POST", url,
                    headers=headers,
                    content=json.dumps(payload).encode() if mode in ("search", "image", "image_edit") else None,
                    json=payload if mode not in ("search", "image", "image_edit") else None,
                ) as resp:

                    # ── reCAPTCHA 403 ────────────────────────────────────────
                    if resp.status_code == 403:
                        body = await resp.aread()
                        try:
                            err = json.loads(body)
                        except Exception:
                            err = {}
                        if err.get("error") == "recaptcha validation failed" and attempt < MAX_CAPTCHA_RETRY - 1:
                            print(f"[proxy] reCAPTCHA v3 rejected (attempt {attempt+1}) — trying v2 fallback...")
                            v2_tok, v2_meta = get_latest_token("v2", TOKEN_MAX_AGE)
                            if v2_tok:
                                used_v2         = v2_tok
                                recaptcha_token = v2_tok
                                token_meta      = v2_meta
                                continue
                            # No v2 — try a fresh v3
                            fresh_v3, fresh_meta = get_latest_token("v3", TOKEN_MAX_AGE)
                            if fresh_v3 and fresh_v3 != recaptcha_token:
                                used_v2         = None
                                recaptcha_token = fresh_v3
                                token_meta      = fresh_meta
                                continue
                            # Exhausted
                            msg = "reCAPTCHA validation failed and no fallback token available."
                            if do_stream:
                                yield _error_chunk(msg)
                                yield "data: [DONE]\n\n"
                            else:
                                yield json.dumps({"error": {"message": msg}})
                            return

                        # Non-captcha 403 or out of retries
                        body_text = body.decode("utf-8", errors="replace")
                        msg = f"Arena returned {resp.status_code}: {body_text[:300]}"
                        if do_stream:
                            yield _error_chunk(msg)
                            yield "data: [DONE]\n\n"
                        else:
                            yield json.dumps({"error": {"message": msg}})
                        return

                    # ── Other non-200 ────────────────────────────────────────
                    if resp.status_code != 200:
                        body = await resp.aread()
                        msg  = f"Arena error {resp.status_code}: {body.decode('utf-8', errors='replace')[:300]}"
                        if do_stream:
                            yield _error_chunk(msg)
                            yield "data: [DONE]\n\n"
                        else:
                            yield json.dumps({"error": {"message": msg}})
                        return

                    # ── Tokenizer: refresh auth cookie ───────────────────────
                    if cfg.get("Tokenizer"):
                        cookie_key = "arena-auth-prod-v1.0" if cfg.get("v2_auth") else "arena-auth-prod-v1"
                        new_tok = resp.cookies.get(cookie_key)
                        if new_tok:
                            cfg["auth_prod"] = new_tok
                            save_config(cfg)
                            print(f"[proxy] {cookie_key} refreshed")

                    # ── Stream decode ─────────────────────────────────────────
                    async for raw_line in resp.aiter_lines():
                        if not raw_line:
                            continue

                        m = re.match(r'^([a-z0-9]+):(.*)', raw_line)
                        if not m:
                            continue

                        prefix = m.group(1)
                        data   = m.group(2).strip()

                        # Finish
                        if prefix == "ad":
                            break

                        # Image result
                        if prefix == "a2" and mode in ("image", "image_edit"):
                            try:
                                items = json.loads(data)
                                if isinstance(items, list):
                                    for item in items:
                                        if isinstance(item, dict) and item.get("type") == "image":
                                            img_url = item.get("image", "")
                                            chunk = _image_chunk(img_url, model_label)
                                            if do_stream:
                                                yield chunk
                                            else:
                                                accumulated_content.append(f"[Image]({img_url})")
                            except Exception:
                                pass
                            continue

                        # Citation (search mode)
                        if prefix == "ac" and citation_acc is not None:
                            cit = citation_acc.feed(data)
                            if cit:
                                c_list = cit if isinstance(cit, list) else [cit]
                                for c in c_list:
                                    ref = f"\n[{c.get('title','')}]({c.get('url','')})"
                                    if do_stream:
                                        yield _content_chunk(ref, model_label)
                                    else:
                                        accumulated_content.append(ref)
                            continue

                        # Reasoning token
                        if prefix == "ag" and mode == "reasoning":
                            tok = _decode_data(data)
                            if _should_filter(tok):
                                continue
                            if do_stream:
                                yield _reasoning_chunk(tok, model_label)
                            else:
                                # Include reasoning in content for non-streaming
                                accumulated_content.append(f"<think>{tok}</think>")
                            continue

                        # Regular content token
                        if prefix == "a0":
                            tok = _decode_data(data)
                            if _should_filter(tok):
                                continue
                            if do_stream:
                                yield _content_chunk(tok, model_label)
                            else:
                                accumulated_content.append(tok)
                            continue

                    # ── Emit done ────────────────────────────────────────────
                    if do_stream:
                        yield _done_chunk(model_label)
                    else:
                        full = "".join(accumulated_content)
                        yield json.dumps(_non_streaming_response(full, model_label))

                    # Consume token after successful request
                    if token_meta:
                        consume_token(recaptcha_token)
                    return   # success — exit retry loop

        except Exception as e:
            msg = f"Proxy internal error: {e}"
            if do_stream:
                yield _error_chunk(msg)
                yield "data: [DONE]\n\n"
            else:
                yield json.dumps({"error": {"message": msg}})
            return


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Arena OpenAI Proxy", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Loaded once at startup, mutated by Tokenizer
_cfg: dict = {}


@app.on_event("startup")
async def on_startup():
    global _cfg
    _cfg = load_config()
    _cfg = prompt_missing_config(_cfg)
    print(f"\n✅ Arena proxy ready on http://localhost:{PROXY_PORT}")
    print(f"   eval_id   : {_cfg.get('eval_id', '?')}")
    print(f"   v2_auth   : {_cfg.get('v2_auth', False)}")
    print(f"   chat model: {_cfg.get('modelAId', DEFAULT_CHAT_MODEL)}")
    print(f"   tokens    : {TOKENS_FILE}")
    print(f"   config    : {CONFIG_FILE}\n")


# ── /v1/models  ──────────────────────────────────────────────────────────────

@app.get("/v1/models")
async def list_models():
    """Return a minimal model list so clients don't complain."""
    models_file = "models.json"
    if os.path.exists(models_file):
        try:
            with open(models_file) as f:
                raw = json.load(f)
            return {
                "object": "list",
                "data": [
                    {"id": m["id"], "object": "model", "owned_by": "arena",
                     "display_name": m.get("publicName", m["id"])}
                    for m in raw
                ],
            }
        except Exception:
            pass
    return {
        "object": "list",
        "data": [
            {"id": _cfg.get("modelAId", DEFAULT_CHAT_MODEL),  "object": "model", "owned_by": "arena"},
            {"id": _cfg.get("searchmodel", DEFAULT_SEARCH_MODEL), "object": "model", "owned_by": "arena"},
            {"id": _cfg.get("thinkmodel",  DEFAULT_THINK_MODEL),  "object": "model", "owned_by": "arena"},
            {"id": _cfg.get("imgmodel",    DEFAULT_IMG_MODEL),    "object": "model", "owned_by": "arena"},
        ],
    }


# ── /v1/chat/completions  ─────────────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body: dict = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # ── Extract messages & prompt ─────────────────────────────────────────
    messages: list[dict] = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="'messages' field is required")

    prompt = messages_to_prompt(messages)

    # ── Mode & model ──────────────────────────────────────────────────────
    mode     = detect_mode(body, _cfg)
    model_id = resolve_model(body, _cfg, mode)

    # ── Stream flag — default True unless client explicitly says False ────
    do_stream: bool = body.get("stream", True)
    if do_stream is None:
        do_stream = True
    do_stream = bool(do_stream)

    # ── Image source for image_edit ───────────────────────────────────────
    image_source: Optional[str] = None
    if mode == "image_edit":
        image_source = body.get("image_path") or body.get("image_base64")
        if not image_source:
            # Try to extract from last message's content list (base64 block)
            for msg in reversed(messages):
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "image_url":
                            url_data = block.get("image_url", {}).get("url", "")
                            if url_data.startswith("data:"):
                                image_source = url_data
                                break
                        if isinstance(block, dict) and block.get("type") == "image_path":
                            image_source = block.get("path", "")
                            break
                if image_source:
                    break

        if not image_source:
            raise HTTPException(
                status_code=400,
                detail="image_edit mode requires 'image_path' (file path) or 'image_base64' in the request body, "
                       "or an image_url/image_path block in the last message content."
            )

    print(f"[proxy] mode={mode} model={model_id} stream={do_stream} prompt_len={len(prompt)}")

    # ── Dispatch ──────────────────────────────────────────────────────────
    gen = _arena_stream(_cfg, mode, model_id, prompt, do_stream, image_source)

    if do_stream:
        return StreamingResponse(
            gen,
            media_type="text/event-stream",
            headers={
                "Cache-Control":  "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        # Collect the single JSON blob emitted by the generator
        result_json = None
        async for chunk in gen:
            result_json = chunk
        if result_json is None:
            raise HTTPException(status_code=500, detail="No response from arena")
        try:
            return JSONResponse(content=json.loads(result_json))
        except Exception:
            return JSONResponse(content={"error": {"message": result_json}})


# ── /v1/images/generations  ──────────────────────────────────────────────────

@app.post("/v1/images/generations")
async def image_generations(request: Request):
    """Dedicated image generation endpoint."""
    try:
        body: dict = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    prompt = body.get("prompt", "")
    if not prompt:
        raise HTTPException(status_code=400, detail="'prompt' is required")

    # Force image mode
    body["image"]      = True
    body["image_edit"] = False
    mode     = "image"
    model_id = resolve_model(body, _cfg, mode)
    do_stream = bool(body.get("stream", False))   # image gen defaults non-streaming

    gen = _arena_stream(_cfg, mode, model_id, prompt, do_stream, None)

    if do_stream:
        return StreamingResponse(gen, media_type="text/event-stream")

    result_json = None
    async for chunk in gen:
        result_json = chunk
    if result_json is None:
        raise HTTPException(status_code=500, detail="No response from arena")
    try:
        return JSONResponse(content=json.loads(result_json))
    except Exception:
        return JSONResponse(content={"error": {"message": result_json}})


# ── /v1/images/edits  ────────────────────────────────────────────────────────

@app.post("/v1/images/edits")
async def image_edits(request: Request):
    """Dedicated image edit endpoint (multipart or JSON)."""
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        prompt       = form.get("prompt", "")
        image_field  = form.get("image")
        image_source = None
        if image_field and hasattr(image_field, "read"):
            raw  = await image_field.read()
            mime = image_field.content_type or "image/png"
            image_source = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        elif isinstance(image_field, str):
            image_source = image_field
    else:
        try:
            body: dict = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid body")
        prompt       = body.get("prompt", "")
        image_source = body.get("image_path") or body.get("image_base64") or body.get("image")

    if not prompt:
        raise HTTPException(status_code=400, detail="'prompt' is required")
    if not image_source:
        raise HTTPException(status_code=400, detail="'image' / 'image_path' / 'image_base64' is required")

    model_id = _cfg.get("imgmodel") or DEFAULT_IMG_MODEL
    gen = _arena_stream(_cfg, "image_edit", model_id, prompt, False, image_source)

    result_json = None
    async for chunk in gen:
        result_json = chunk
    if result_json is None:
        raise HTTPException(status_code=500, detail="No response from arena")
    try:
        return JSONResponse(content=json.loads(result_json))
    except Exception:
        return JSONResponse(content={"error": {"message": result_json}})


# ── Health / info ─────────────────────────────────────────────────────────────

@app.get("/")
@app.get("/health")
async def health():
    tokens_data = _load_tokens_raw()
    tokens      = tokens_data.get("tokens", [])
    now         = datetime.now(timezone.utc)
    fresh = sum(
        1 for t in tokens
        if (now - datetime.fromisoformat(t["timestamp_utc"].rstrip("Z")).replace(tzinfo=timezone.utc)).total_seconds() < 120
        if "timestamp_utc" in t
    )
    return {
        "status":        "ok",
        "proxy":         "arena → openai",
        "eval_id":       _cfg.get("eval_id"),
        "mode_defaults": {
            "chat":     _cfg.get("modelAId"),
            "search":   _cfg.get("searchmodel"),
            "think":    _cfg.get("thinkmodel"),
            "image":    _cfg.get("imgmodel"),
        },
        "tokens": {
            "total":   len(tokens),
            "fresh_2m": fresh,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Arena → OpenAI Proxy")
    print(f"  Base URL : {BASE_URL}")
    print(f"  Listening: http://0.0.0.0:{PROXY_PORT}/v1")
    print(f"  Config   : {CONFIG_FILE}")
    print(f"  Tokens   : {TOKENS_FILE}")
    print("=" * 60)

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=PROXY_PORT,
        log_level="info",
        reload=False,
    )
