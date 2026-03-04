"""
reCAPTCHA Token Harvester — Camoufox Edition
=============================================
Identical feature set to arena_token.py but runs on Camoufox (Firefox-based,
anti-detect) instead of Playwright Chromium.

Key differences vs arena_token.py
──────────────────────────────────
• Uses camoufox async API instead of playwright.chromium
• BrowserForge fingerprint generation — prompted at startup
• All JS runs in the ISOLATED world by default (invisible to the page).
  Scripts that MUST modify the DOM (blocker, v2/v3 harvesters) are prefixed
  with "mw:" to run in the main world — only where absolutely required.
• No Chromium-specific args / extension loading (Camoufox handles stealth)
• STEALTH_SCRIPT is handled natively by Camoufox — removed
• CUS_PROFILE / EXTENSIONS not applicable to Camoufox — removed

Usage:
    pip install "camoufox[geoip]" browserforge fastapi uvicorn
    camoufox fetch                  # download Firefox binary once
    python camoufox.py

Then open http://localhost:5000

Tokens are saved to tokens.json (compatible with modula.py / main.py).
"""

import asyncio
import json
import os
import random
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from camoufox.async_api import AsyncCamoufox
from browserforge.fingerprints import FingerprintGenerator, Screen
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

# ============================================================
# CONFIGURATION — edit these
# ============================================================

N = 1          # number of windows
SERVER_PORT = 5000
AUTO_LOGIN = True

# ============================================================
# 5_GAIN — Auto-navigate to a specific arena.ai conversation
# ============================================================
# Requires AUTO_LOGIN=True.
# When True, you will be prompted for an eval_id at startup.
# After login/ready flow, the window navigates to:
#   https://arena.ai/c/<eval_id>
# instead of staying on https://arena.ai

FIVE_GAIN = False   # set to True to enable (AUTO_LOGIN must also be True)

# ============================================================
# TUNING — Reload page after each v2/v3 token is harvested
# ============================================================
# When True (default): after every v2 or v3 token the page reloads,
# re-runs the blocker, re-marks ready, and re-injects the active harvester
# script so harvesting continues automatically.
# When False: tokens are stored normally with no page reload.

TUNING = True

# ============================================================
# HARD_TUNING — Nuke and rebuild the browser context on each reload
# ============================================================
# Requires TUNING=True. Raises error if TUNING=False.
#
# When True: instead of a simple page.reload(), HARD_TUNING performs a full
# context wipe and rebuild on every token cycle:
#   1. Save the 4 essential cookies to RAM:
#        arena-auth-prod-v1.0, arena-auth-prod-v1.1, __cf_bm, cf_clearance
#   2. Close the existing Camoufox browser context
#   3. Delete the profile directory from disk entirely
#   4. Launch a brand-new Camoufox context with a fresh BrowserForge fingerprint
#   5. Re-inject the saved cookies into the fresh context
#   6. Navigate to arena.ai (or arena.ai/c/<eval_id> if FIVE_GAIN=True)
#   7. Run blocker -> ready signal -> re-inject active harvester script
#
# Effect: each harvest cycle starts with a completely clean browser fingerprint
# (new Firefox fingerprint, no cached data, no stale cookies beyond the 4 saved
# ones, fresh localStorage). This makes each token request look like a fresh
# browser session to reCAPTCHA.

HARD_TUNING = False   # set to True to enable (TUNING must also be True)

# ============================================================
# TOKENS FILE — output compatible with modula.py / main.py
# ============================================================

TOKENS_FILE = "tokens.json"
CONFIG_FILE  = "config.json"

# ============================================================
# COOKIE INJECTION — edit these when COOKIES=True
# ============================================================

COOKIES = False
# When COOKIES=True the harvester will inject auth cookies into each context.

COOKIE_V1 = ""
# Paste the full value for arena-auth-prod-v1.0 here.

COOKIE_V2 = ""
# Paste the full value for arena-auth-prod-v1.1 here.

# ============================================================
# NOTE: AUTO_LOGIN=True is incompatible with COOKIES=True.
#
# When AUTO_LOGIN=True the harvester signs in to arena.ai for each browser
# window using the credentials you enter in the terminal at startup.
#
# Login flow per window:
#   1. Navigate to arena.ai
#   2. Run initial v2 script
#   3. POST to /nextjs-api/sign-in/email with email + password
#   4. Extract arena-auth-prod-v1.0 and arena-auth-prod-v1.1 from context cookies
#      and persist them to config.json
#   5. Reload page, run blocker script, continue as normal
# ============================================================

PROFILES_DIR = Path("harvester_profiles")

# ── Startup validation ─────────────────────────────────────────

if COOKIES:
    if not COOKIE_V1 or not COOKIE_V1.strip():
        raise RuntimeError(
            "COOKIES=True but COOKIE_V1 is empty.\n"
            "Set COOKIE_V1 to the value for the arena-auth-prod-v1.0 cookie."
        )
    if not COOKIE_V2 or not COOKIE_V2.strip():
        raise RuntimeError(
            "COOKIES=True but COOKIE_V2 is empty.\n"
            "Set COOKIE_V2 to the value for the arena-auth-prod-v1.1 cookie."
        )

if AUTO_LOGIN and COOKIES:
    raise RuntimeError(
        "AUTO_LOGIN=True and COOKIES=True cannot be used together.\n"
        "AUTO_LOGIN manages auth cookies itself via the sign-in API.\n"
        "Set either AUTO_LOGIN=False (to use manual cookies) or COOKIES=False (to use auto-login)."
    )

if FIVE_GAIN and not AUTO_LOGIN:
    raise RuntimeError(
        "FIVE_GAIN=True requires AUTO_LOGIN=True.\n"
        "Enable AUTO_LOGIN or set FIVE_GAIN=False."
    )

if HARD_TUNING and not TUNING:
    raise RuntimeError(
        "HARD_TUNING=True requires TUNING=True.\n"
        "HARD_TUNING is an enhancement of TUNING — enable TUNING first."
    )

# ── Collect AUTO_LOGIN credentials once at startup ────────────
_AUTO_LOGIN_EMAIL    = ""
_AUTO_LOGIN_PASSWORD = ""
_EVAL_ID             = ""

if AUTO_LOGIN:
    print("\n" + "=" * 55)
    print("  AUTO_LOGIN enabled — enter arena.ai credentials")
    print("  These will be used to sign in each browser window.")
    print("=" * 55)
    _AUTO_LOGIN_EMAIL    = input("  Email    : ").strip()
    _AUTO_LOGIN_PASSWORD = input("  Password : ").strip()
    if not _AUTO_LOGIN_EMAIL or not _AUTO_LOGIN_PASSWORD:
        raise RuntimeError("AUTO_LOGIN=True but email or password was left blank.")

if FIVE_GAIN:
    print("\n" + "=" * 55)
    print("  5_GAIN enabled — enter the arena.ai eval/conversation ID")
    print("  Windows will navigate to https://arena.ai/c/<eval_id>")
    print("=" * 55)
    _EVAL_ID = input("  Eval ID  : ").strip()
    if not _EVAL_ID:
        raise RuntimeError("FIVE_GAIN=True but eval_id was left blank.")
    print(f"  Target URL: https://arena.ai/c/{_EVAL_ID}")

# ── BrowserForge fingerprint configuration — prompted at startup ───────────────
#
# Camoufox uses BrowserForge to generate realistic Firefox fingerprints.
# You will be asked at startup what OS/screen profile you want.
# HARD_TUNING re-generates a fresh fingerprint of the SAME configuration on every
# cycle, rotating fingerprint values while keeping the OS/screen constraints.

_FP_OS: tuple = ("windows", "macos", "linux")
_FP_SCREEN: Optional[Screen] = None

print("\n" + "=" * 55)
print("  BrowserForge Fingerprint Configuration")
print("  Camoufox generates realistic Firefox fingerprints.")
print("=" * 55)
print("  Target OS:")
print("    1) Windows")
print("    2) macOS")
print("    3) Linux")
print("    4) Random (all three)")
_os_choice = input("  Choice [1-4, default 4]: ").strip() or "4"
if _os_choice == "1":
    _FP_OS = ("windows",)
elif _os_choice == "2":
    _FP_OS = ("macos",)
elif _os_choice == "3":
    _FP_OS = ("linux",)
else:
    _FP_OS = ("windows", "macos", "linux")

print("\n  Screen resolution constraints:")
print("    1) Desktop HD      (1280x720  - 1920x1080)")
print("    2) Desktop FHD+    (1920x1080 - 2560x1440)")
print("    3) Laptop          (1280x800  - 1600x900)")
print("    4) No constraint   (let BrowserForge decide)")
_screen_choice = input("  Choice [1-4, default 4]: ").strip() or "4"
if _screen_choice == "1":
    _FP_SCREEN = Screen(min_width=1280, max_width=1920, min_height=720,  max_height=1080)
elif _screen_choice == "2":
    _FP_SCREEN = Screen(min_width=1920, max_width=2560, min_height=1080, max_height=1440)
elif _screen_choice == "3":
    _FP_SCREEN = Screen(min_width=1280, max_width=1600, min_height=800,  max_height=900)
else:
    _FP_SCREEN = Screen(max_width=1920, max_height=1080)

# Shared generator — call _generate_fingerprint() to get a fresh fingerprint
_FP_GEN = FingerprintGenerator(
    os=_FP_OS,
    screen=_FP_SCREEN,
    browser="firefox",   # Camoufox is Firefox-based; must match to avoid UA mismatch
    mock_webrtc=True,
)

def _generate_fingerprint():
    """Return a fresh BrowserForge fingerprint for the configured OS/screen profile."""
    return _FP_GEN.generate()


# ─────────────────────────────────────────────────────────────
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global state ──────────────────────────────────────────────
_windows: dict[int, dict] = {}
_tokens_lock = asyncio.Lock()

# Per-window Camoufox manager handles (needed to close/reopen for HARD_TUNING)
_camoufox_managers: dict[int, AsyncCamoufox] = {}


# ── Token file helpers ────────────────────────────────────────

def _load_tokens_file() -> dict:
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"tokens": [], "total_count": 0, "last_updated": ""}


def _save_tokens_file(tokens_data: dict) -> None:
    tmp = TOKENS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tokens_data, f, indent=2)
    os.replace(tmp, TOKENS_FILE)


def _append_token(data: dict) -> int:
    """Append a new token entry to tokens.json. Must be called inside _tokens_lock."""
    tokens_data = _load_tokens_file()

    now_utc   = datetime.utcnow()
    raw_token = data.get("token", "")

    entry = {
        "token":           raw_token,
        "version":         data.get("version", "v3"),
        "action":          data.get("action", ""),
        "source_url":      data.get("source_url", ""),
        "window_id":       data.get("window_id", -1),
        "harvest_number":  data.get("harvest_number", 0),
        "timestamp_utc":   now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timestamp_local": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "token_preview":   (raw_token[:40] + "...") if len(raw_token) > 40 else raw_token,
    }

    tokens_data["tokens"].append(entry)
    tokens_data["total_count"]  = len(tokens_data["tokens"])
    tokens_data["last_updated"] = entry["timestamp_utc"]

    _save_tokens_file(tokens_data)
    return tokens_data["total_count"]


# ── Injected scripts ──────────────────────────────────────────
#
# Camoufox runs page.evaluate() in an ISOLATED world by default.
# This means the page cannot detect or intercept isolated-world JS.
#
# Scripts that only READ the DOM or make fetch() calls work fine in isolated world.
# Scripts that must WRITE to window.* or inject DOM elements need main world.
# Main world is activated per-script by prefixing the code with "mw:".
#
# Prefix guide used below:
#   (no prefix)  = isolated world  — safe, undetectable
#   "mw:"        = main world      — detectable but necessary for DOM modification
#
# main_world_eval=True must be set in AsyncCamoufox() for mw: to work.

# Initial v2 — reads DOM + makes fetch. Main world needed because grecaptcha
# is a page-owned object and we call render() on it (DOM write).
INITIAL_V2_SCRIPT = """
mw:(function() {
  'use strict';

  const CONFIG = {
    SITE_KEY: '6Led_uYrAAAAAKjxDIF58fgFtX3t8loNAK85bW9I',
    TIMEOUT: 60000
  };

  console.log('INITIAL v2 reCAPTCHA Token Generator');

  async function getV2Token() {
    await waitForGrecaptcha(window);
    const g = window.grecaptcha && window.grecaptcha.enterprise;
    if (!g || typeof g.render !== 'function') throw new Error('NO_GRECAPTCHA_V2');

    let settled = false;
    const done = (fn, arg) => { if (settled) return; settled = true; fn(arg); };

    return new Promise((resolve, reject) => {
      try {
        const el = document.createElement('div');
        el.style.cssText = 'position:fixed;left:-9999px;top:-9999px;width:1px;height:1px;';
        document.body.appendChild(el);

        const timer = setTimeout(() => done(reject, 'V2_TIMEOUT'), CONFIG.TIMEOUT);

        const wid = g.render(el, {
          sitekey: CONFIG.SITE_KEY,
          size: 'invisible',
          callback: function(tok) {
            clearTimeout(timer);
            done(resolve, tok);
          },
          'error-callback': function() {
            clearTimeout(timer);
            done(reject, 'V2_ERROR');
          }
        });

        if (typeof g.execute === 'function') g.execute(wid);
      } catch (e) {
        done(reject, String(e));
      }
    });
  }

  async function waitForGrecaptcha(w) {
    const start = Date.now();
    while (Date.now() - start < 60000) {
      if (w.grecaptcha && w.grecaptcha.enterprise && w.grecaptcha.enterprise.render) return true;
      await new Promise(function(r) { setTimeout(r, 100); });
    }
    throw new Error('Timeout waiting for grecaptcha');
  }

  (async function() {
    try {
      const token = await getV2Token();
      console.log('INITIAL token received, length:', token.length);
      fetch('http://localhost:5000/api', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token: token,
          version: 'v2_initial',
          action: 'initial_page_load',
          source_url: window.location.href
        })
      }).catch(function(err) { console.log('Store failed:', err); });
    } catch (error) {
      console.error('Initial token failed:', error);
    }
  })();
})()
"""

# Blocker — overrides window.fetch to strip forceLowRecaptchaScore.
# MUST run in main world (mw:) to override the real window.fetch.
BLOCKER_SCRIPT = """
mw:(function() {
    console.log('Installing forceLowRecaptchaScore blocker...');

    var originalFetch = window.fetch;
    window.fetch = function() {
        var args = Array.prototype.slice.call(arguments);
        var url = args[0];
        var options = args[1] || {};

        if (options.body && typeof options.body === 'string') {
            try {
                var body = JSON.parse(options.body);

                var deepClean = function(obj) {
                    if (!obj || typeof obj !== 'object') return obj;
                    if (Array.isArray(obj)) return obj.map(deepClean);
                    var cleaned = {};
                    for (var key in obj) {
                        if (!Object.prototype.hasOwnProperty.call(obj, key)) continue;
                        if (key === 'forceLowRecaptchaScore') {
                            console.log('REMOVED forceLowRecaptchaScore');
                            continue;
                        }
                        cleaned[key] = deepClean(obj[key]);
                    }
                    return cleaned;
                };

                options = Object.assign({}, options, { body: JSON.stringify(deepClean(body)) });
                args[1] = options;
            } catch (e) {}
        }

        return originalFetch.apply(this, args);
    };

    console.log('Blocker installed!');
})()
"""

# On-demand invisible v2 — triggered from dashboard.
# Main world because it calls grecaptcha.enterprise.render() (DOM write).
ON_DEMAND_V2_SCRIPT = """
mw:(function() {
  'use strict';

  var CONFIG = {
    SITE_KEY: '6Led_uYrAAAAAKjxDIF58fgFtX3t8loNAK85bW9I',
    TIMEOUT: 60000
  };

  function loadRecaptchaScript() {
    return new Promise(function(resolve, reject) {
      if (document.querySelector('script[src*="recaptcha/enterprise.js"]')) { resolve(); return; }
      var script = document.createElement('script');
      script.src = 'https://www.google.com/recaptcha/enterprise.js?render=' + CONFIG.SITE_KEY;
      script.async = true;
      script.defer = true;
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }

  function waitForGrecaptcha() {
    return new Promise(function(resolve, reject) {
      var start = Date.now();
      (function check() {
        if (window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.render) {
          resolve();
        } else if (Date.now() - start > 30000) {
          reject(new Error('Timeout'));
        } else {
          setTimeout(check, 100);
        }
      })();
    });
  }

  function getV2Token() {
    var g = window.grecaptcha.enterprise;
    var settled = false;
    function done(fn, arg) { if (settled) return; settled = true; fn(arg); }

    return new Promise(function(resolve, reject) {
      try {
        var el = document.createElement('div');
        el.style.cssText = 'position:fixed;left:-9999px;top:-9999px;width:1px;height:1px;';
        document.body.appendChild(el);

        var timer = setTimeout(function() { done(reject, 'V2_TIMEOUT'); }, CONFIG.TIMEOUT);

        var wid = g.render(el, {
          sitekey: CONFIG.SITE_KEY,
          size: 'invisible',
          callback: function(tok) { clearTimeout(timer); done(resolve, tok); },
          'error-callback': function() { clearTimeout(timer); done(reject, 'V2_ERROR'); }
        });

        if (typeof g.execute === 'function') g.execute(wid);
      } catch (e) {
        done(reject, String(e));
      }
    });
  }

  (async function() {
    try {
      if (!window.grecaptcha || !window.grecaptcha.enterprise) {
        await loadRecaptchaScript();
      }
      await waitForGrecaptcha();
      var token = await getV2Token();
      console.log('ON-DEMAND token received');
      fetch('http://localhost:5000/api', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token: token,
          version: 'v2_ondemand',
          action: 'manual_trigger',
          source_url: window.location.href
        })
      }).then(function(r) { return r.json(); }).then(function(data) {
        console.log('Stored. Total:', data.total_count);
      }).catch(function(err) { console.log('Store failed:', err); });
    } catch (error) {
      console.error('On-demand token failed:', error);
    }
  })();
})()
"""

# V2 Harvester — checkbox or invisible continuous loop.
# Main world (mw:) — injects visible DOM elements (panel, checkbox widget).
V2_SCRIPT = r"""
mw:(() => {
    const SERVER_URL   = "http://localhost:5000/api";
    const V2_SITEKEY   = "6Ld7ePYrAAAAAB34ovoFoDau1fqCJ6IyOjFEQaMn";
    const FORCE_MODE   = "checkbox";
    const INV_MIN_INTERVAL = 80;
    const INV_MAX_INTERVAL = 100;
    const INV_RETRY    = 15;

    let v2Count = 0;
    let invisibleErrors = 0;
    let currentMode = FORCE_MODE === "auto" ? "invisible" : FORCE_MODE;
    let currentTimeoutId = null;
    let widgetCounter = 0;
    let panelCreated = false;

    function getRandomInterval(min, max) {
        const arr = new Uint32Array(1);
        crypto.getRandomValues(arr);
        return min + (arr[0] / (0xFFFFFFFF + 1)) * (max - min);
    }

    function sendToken(token, mode) {
        v2Count++;
        invisibleErrors = 0;
        console.log(`[v2-${mode} #${v2Count}] Token generated (${token.length} chars)`);
        updateCount();
        if (panelCreated) updateStatus(`Token #${v2Count} captured! Sending...`);
        return fetch(SERVER_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                token,
                version: "v2",
                action: mode === "invisible" ? "invisible_auto" : "checkbox_challenge",
                harvest_number: v2Count,
                source_url: window.location.href,
                _reload_after: true
            })
        }).then(r => r.json()).then(data => {
            console.log(`[v2-${mode} #${v2Count}] Stored. Total: ${data.total_count}`);
            if (panelCreated) updateStatus(`Token #${v2Count} stored! Reloading...`);
        }).catch(err => console.error(`[v2-${mode} #${v2Count}] Store failed:`, err));
    }

    function harvestInvisible() {
        const g = window.grecaptcha && window.grecaptcha.enterprise;
        if (!g || typeof g.render !== 'function') {
            currentTimeoutId = setTimeout(harvestInvisible, 2000);
            return;
        }
        widgetCounter++;
        const el = document.createElement('div');
        el.id = `__v2_inv_widget_${widgetCounter}`;
        el.style.cssText = 'position:fixed;left:-9999px;top:-9999px;width:1px;height:1px;overflow:hidden;';
        document.body.appendChild(el);
        let settled = false;
        const timer = setTimeout(() => {
            if (!settled) { settled = true; el.remove(); handleInvisibleFailure(); }
        }, 60000);
        try {
            const wid = g.render(el, {
                sitekey: V2_SITEKEY,
                size: 'invisible',
                callback: (token) => {
                    if (settled) return;
                    settled = true; clearTimeout(timer); el.remove();
                    sendToken(token, "invisible").then(() => {
                        const next = getRandomInterval(INV_MIN_INTERVAL, INV_MAX_INTERVAL);
                        currentTimeoutId = setTimeout(harvestInvisible, next * 1000);
                    });
                },
                'error-callback': () => {
                    if (settled) return;
                    settled = true; clearTimeout(timer); el.remove(); handleInvisibleFailure();
                }
            });
            if (typeof g.execute === 'function') g.execute(wid);
        } catch (e) {
            if (!settled) { settled = true; clearTimeout(timer); el.remove(); handleInvisibleFailure(); }
        }
    }

    function handleInvisibleFailure() {
        invisibleErrors++;
        if (FORCE_MODE === "invisible") {
            const backoff = Math.min(INV_RETRY * Math.pow(1.5, invisibleErrors - 1), 300);
            currentTimeoutId = setTimeout(harvestInvisible, backoff * 1000);
        } else if (FORCE_MODE === "auto" && invisibleErrors >= 2) {
            currentMode = "checkbox"; startCheckboxMode();
        } else {
            const backoff = Math.min(INV_RETRY * Math.pow(1.5, invisibleErrors - 1), 60);
            currentTimeoutId = setTimeout(harvestInvisible, backoff * 1000);
        }
    }

    function createPanel() {
        if (panelCreated) return;
        panelCreated = true;
        let panel = document.getElementById('__v2_harvest_panel');
        if (panel) return;
        panel = document.createElement('div');
        panel.id = '__v2_harvest_panel';
        panel.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:999999;background:#1a1a2e;border:2px solid #16213e;border-radius:12px;padding:12px 16px;box-shadow:0 4px 20px rgba(0,0,0,0.4);font-family:system-ui,sans-serif;min-width:320px;';
        const header = document.createElement('div');
        header.style.cssText = 'color:#e0e0e0;font-size:13px;margin-bottom:8px;font-weight:600;';
        header.innerHTML = 'v2 Harvester (checkbox) <span id="__v2_count" style="color:#4ade80;float:right;">0 tokens</span>';
        panel.appendChild(header);
        const status = document.createElement('div');
        status.id = '__v2_status';
        status.style.cssText = 'color:#9ca3af;font-size:11px;margin-bottom:10px;';
        status.textContent = 'Click the checkbox below to harvest a v2 token';
        panel.appendChild(status);
        const container = document.createElement('div');
        container.id = '__v2_checkbox_container';
        container.style.cssText = 'display:flex;justify-content:center;';
        panel.appendChild(container);
        const closeBtn = document.createElement('div');
        closeBtn.style.cssText = 'color:#6b7280;font-size:11px;margin-top:8px;cursor:pointer;text-align:center;';
        closeBtn.textContent = 'stop: window.__STOP_V2_HARVEST__()';
        closeBtn.onclick = () => window.__STOP_V2_HARVEST__();
        panel.appendChild(closeBtn);
        document.body.appendChild(panel);
    }

    function updateStatus(msg) { const el = document.getElementById('__v2_status'); if (el) el.textContent = msg; }
    function updateCount()  { const el = document.getElementById('__v2_count');  if (el) el.textContent = `${v2Count} token${v2Count !== 1 ? 's' : ''}`; }

    function startCheckboxMode() { createPanel(); renderCheckbox(); }

    function renderCheckbox() {
        const g = window.grecaptcha && window.grecaptcha.enterprise;
        if (!g || typeof g.render !== 'function') {
            updateStatus('Waiting for grecaptcha.enterprise...');
            setTimeout(renderCheckbox, 1000);
            return;
        }
        const panel = document.getElementById('__v2_harvest_panel');
        if (!panel) return;
        const old = document.getElementById('__v2_checkbox_container');
        if (old) old.remove();
        const container = document.createElement('div');
        container.id = '__v2_checkbox_container';
        container.style.cssText = 'display:flex;justify-content:center;';
        panel.insertBefore(container, panel.lastElementChild);
        updateStatus('Click the checkbox below to harvest a v2 token');
        const timeout = setTimeout(() => { updateStatus('Widget expired. Rendering fresh...'); renderCheckbox(); }, 60000);
        try {
            g.render(container, {
                sitekey: V2_SITEKEY,
                callback: (token) => {
                    clearTimeout(timeout);
                    sendToken(token, "checkbox").then(() => {});
                },
                'error-callback':   () => { clearTimeout(timeout); updateStatus('Challenge failed. New widget in 5s...');  setTimeout(renderCheckbox, 5000); },
                'expired-callback': () => { clearTimeout(timeout); updateStatus('Token expired. New widget in 3s...');     setTimeout(renderCheckbox, 3000); },
                theme: document.documentElement.classList.contains('dark') ? 'dark' : 'light',
            });
        } catch (e) {
            clearTimeout(timeout);
            updateStatus(`Error: ${e.message}. Retry in 10s...`);
            setTimeout(renderCheckbox, 10000);
        }
    }

    window.__STOP_V2_HARVEST__ = () => {
        if (currentTimeoutId) { clearTimeout(currentTimeoutId); currentTimeoutId = null; }
        const panel = document.getElementById('__v2_harvest_panel');
        if (panel) panel.remove();
        panelCreated = false;
        console.log(`[v2] Stopped. Tokens: ${v2Count}`);
    };
    window.__V2_SWITCH_INVISIBLE__ = () => { window.__STOP_V2_HARVEST__(); currentMode = "invisible"; invisibleErrors = 0; harvestInvisible(); };
    window.__V2_SWITCH_CHECKBOX__  = () => { window.__STOP_V2_HARVEST__(); currentMode = "checkbox";  startCheckboxMode(); };

    console.log(`v2 Harvester started (mode: ${FORCE_MODE})`);
    if (FORCE_MODE === "checkbox") {
        currentMode = "checkbox";
        if (window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.ready) {
            window.grecaptcha.enterprise.ready(() => startCheckboxMode());
        } else { startCheckboxMode(); }
    } else {
        currentMode = "invisible";
        if (window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.ready) {
            window.grecaptcha.enterprise.ready(() => harvestInvisible());
        } else { harvestInvisible(); }
    }
})()
"""

# V3 Harvester — continuous execute() loop.
# Main world (mw:) — attaches __STOP_HARVEST__ and __RECAPTCHA_TOKEN__ to window.
V3_SCRIPT = r"""
mw:(() => {
    const SERVER_URL   = "http://localhost:5000/api";
    const SITE_KEY     = "6Led_uYrAAAAAKjxDIF58fgFtX3t8loNAK85bW9I";
    const ACTION       = "chat_submit";
    const MIN_INTERVAL = 12;
    const MAX_INTERVAL = 18;

    let tokenCount = 0;
    let currentTimeoutId = null;

    function getRandomInterval() {
        const arr = new Uint32Array(1);
        crypto.getRandomValues(arr);
        return MIN_INTERVAL + (arr[0] / (0xFFFFFFFF + 1)) * (MAX_INTERVAL - MIN_INTERVAL);
    }

    function harvest() {
        grecaptcha.enterprise.ready(() => {
            grecaptcha.enterprise.execute(SITE_KEY, { action: ACTION })
                .then(token => {
                    tokenCount++;
                    console.log(`[v3 #${tokenCount}] Token generated (${token.length} chars)`);
                    return fetch(SERVER_URL, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            token,
                            version: "v3",
                            action: ACTION,
                            harvest_number: tokenCount,
                            source_url: window.location.href,
                            _reload_after: true
                        })
                    }).then(res => res.json()).then(data => {
                        console.log(`[v3 #${tokenCount}] Stored. Total: ${data.total_count}`);
                        window.__RECAPTCHA_TOKEN__ = token;
                        scheduleNext();
                    });
                }).catch(err => { console.error("[v3] Error:", err); scheduleNext(); });
        });
    }

    function scheduleNext() {
        const next = getRandomInterval();
        console.log(`[v3] Next harvest in ${next.toFixed(2)}s`);
        currentTimeoutId = setTimeout(harvest, next * 1000);
    }

    window.__STOP_HARVEST__ = () => {
        if (currentTimeoutId) { clearTimeout(currentTimeoutId); currentTimeoutId = null; }
        console.log("[v3] Stopped. Total captured:", tokenCount);
    };

    console.log(`v3 Auto-harvester started (${MIN_INTERVAL}-${MAX_INTERVAL}s interval)`);
    harvest();
})()
"""

# Ready signal — pure fetch POST, no DOM modification. Isolated world is fine.
READY_SIGNAL_SCRIPT = """
async (windowId) => {
    try {
        await fetch('http://localhost:5000/windows/' + windowId + '/ready', { method: 'POST' });
        console.log('[harvester] Marked ready, window ' + windowId);
    } catch(e) {
        console.warn('[harvester] Ready signal failed:', e);
    }
}
"""

# ── Dashboard HTML ─────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>reCAPTCHA Harvester - Camoufox</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f0f1a; color: #e0e0e0; font-family: system-ui, -apple-system, sans-serif; padding: 24px; min-height: 100vh; }
  h1 { font-size: 20px; font-weight: 700; color: #fff; margin-bottom: 4px; }
  .subtitle { color: #6b7280; font-size: 13px; margin-bottom: 24px; }
  .stats { display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
  .stat { background: #1a1a2e; border: 1px solid #2d2d4e; border-radius: 10px; padding: 14px 20px; flex: 1; min-width: 110px; }
  .stat-label { font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; }
  .stat-value { font-size: 28px; font-weight: 700; color: #4ade80; margin-top: 4px; }
  .stat-value.blue   { color: #60a5fa; }
  .stat-value.purple { color: #c084fc; }
  .stat-value.orange { color: #fb923c; }
  .file-note { background: #111827; border: 1px solid #1f2937; border-radius: 8px; padding: 10px 16px; margin-bottom: 20px; font-size: 12px; color: #6b7280; }
  .file-note span { color: #4ade80; font-family: monospace; font-weight: 600; }
  .windows { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 16px; }
  .window-card { background: #1a1a2e; border: 1px solid #2d2d4e; border-radius: 12px; padding: 18px; }
  .window-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
  .window-title { font-size: 15px; font-weight: 600; }
  .badge { font-size: 11px; padding: 3px 10px; border-radius: 20px; font-weight: 600; }
  .badge.loading       { background: #1c2a1c; color: #6b7280;  border: 1px solid #374151; }
  .badge.ready         { background: #1c2a1c; color: #4ade80;  border: 1px solid #166534; }
  .badge.idle          { background: #1c1c2a; color: #9ca3af;  border: 1px solid #374151; }
  .badge.harvesting_v2 { background: #2a1c1c; color: #f87171;  border: 1px solid #991b1b; }
  .badge.harvesting_v3 { background: #1c1c2a; color: #60a5fa;  border: 1px solid #1d4ed8; }
  .badge.reloading     { background: #2a2a1c; color: #facc15;  border: 1px solid #854d0e; }
  .btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
  .btn { padding: 7px 14px; border: none; border-radius: 7px; cursor: pointer; font-size: 12px; font-weight: 600; transition: opacity 0.15s, transform 0.1s; }
  .btn:hover   { opacity: 0.85; transform: translateY(-1px); }
  .btn:active  { transform: translateY(0); }
  .btn:disabled { opacity: 0.35; cursor: not-allowed; transform: none; }
  .btn.v2-start  { background: #dc2626; color: #fff; }
  .btn.v2-stop   { background: #374151; color: #f87171; }
  .btn.v3-start  { background: #1d4ed8; color: #fff; }
  .btn.v3-stop   { background: #374151; color: #60a5fa; }
  .btn.inv-run   { background: #8b5cf6; color: #fff; width: 100%; margin-top: 4px; }
  .window-info { font-size: 11px; color: #4b5563; margin-top: 10px; }
  .danger-row { margin-top: 24px; display: flex; justify-content: flex-end; gap: 12px; }
  .btn-warn   { padding: 8px 18px; border: 1px solid #78350f; background: #1a100a; color: #fb923c; border-radius: 7px; cursor: pointer; font-size: 12px; font-weight: 600; transition: background 0.15s; }
  .btn-warn:hover   { background: #78350f; color: #fff; }
  .btn-danger { padding: 8px 18px; border: 1px solid #7f1d1d; background: #1a0a0a; color: #f87171; border-radius: 7px; cursor: pointer; font-size: 12px; font-weight: 600; transition: background 0.15s; }
  .btn-danger:hover { background: #7f1d1d; color: #fff; }
  .refresh-info { text-align: right; color: #374151; font-size: 11px; margin-top: 20px; }
  .toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #1a1a2e; border: 1px solid #4ade80; color: #4ade80; padding: 10px 20px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 9999; }
  .toast.show { opacity: 1; }
</style>
</head>
<body>
<h1>reCAPTCHA Harvester <span style="color:#8b5cf6;font-size:13px;font-weight:400;">&#x1F98A; Camoufox Edition</span></h1>
<p class="subtitle">Token harvesting dashboard — auto-refreshes every 3s</p>

<div class="file-note">
  Tokens persist to <span>tokens.json</span> — directly compatible with modula.py / main.py / arena_client.py
</div>

<div class="stats">
  <div class="stat"><div class="stat-label">Total Tokens</div><div class="stat-value"         id="stat-total">0</div></div>
  <div class="stat"><div class="stat-label">v2 Tokens</div>   <div class="stat-value purple"  id="stat-v2">0</div></div>
  <div class="stat"><div class="stat-label">v3 Tokens</div>   <div class="stat-value blue"    id="stat-v3">0</div></div>
  <div class="stat"><div class="stat-label">Fresh (&lt;2min)</div><div class="stat-value orange" id="stat-fresh">0</div></div>
  <div class="stat"><div class="stat-label">Windows Ready</div><div class="stat-value"        id="stat-ready">0</div></div>
</div>

<div class="windows" id="windows-container">
  <p style="color:#6b7280;font-size:13px;">Loading windows...</p>
</div>

<div class="danger-row">
  <button class="btn-warn"   onclick="clearTokens()">Clear tokens.json</button>
  <button class="btn-danger" onclick="deleteProfiles()">Delete All Profiles</button>
</div>
<div class="refresh-info" id="refresh-info">Last refresh: -</div>
<div class="toast" id="toast"></div>

<script>
function showToast(msg, color) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.borderColor = color || '#4ade80';
  t.style.color = color || '#4ade80';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}

async function apiCall(path, method = 'POST') {
  try {
    const r = await fetch(path, { method });
    return await r.json();
  } catch (e) {
    showToast('Error: ' + e.message, '#f87171');
    return null;
  }
}

function renderWindows(windows) {
  const container = document.getElementById('windows-container');
  if (!windows.length) {
    container.innerHTML = '<p style="color:#6b7280;font-size:13px;">No windows yet...</p>';
    return;
  }
  container.innerHTML = windows.map(w => {
    const bc      = w.status || 'loading';
    const bt      = bc.replace(/_/g, ' ').toUpperCase();
    const isReady = w.status !== 'loading' && w.status !== 'reloading';
    const dis     = !isReady ? 'disabled' : '';
    return `
    <div class="window-card">
      <div class="window-header">
        <span class="window-title">Window ${w.id}</span>
        <span class="badge ${bc}">${bt}</span>
      </div>
      <div class="btn-row">
        <button class="btn v2-start" onclick="v2Start(${w.id})" ${dis}>V2 Start</button>
        <button class="btn v2-stop"  onclick="v2Stop(${w.id})"  ${dis}>V2 Stop</button>
        <button class="btn v3-start" onclick="v3Start(${w.id})" ${dis}>V3 Start</button>
        <button class="btn v3-stop"  onclick="v3Stop(${w.id})"  ${dis}>V3 Stop</button>
      </div>
      <div class="btn-row">
        <button class="btn inv-run" onclick="runInvisible(${w.id})" ${dis}>Run Invisible Script</button>
      </div>
      <div class="window-info">
        Profile: harvester_profiles/window_${w.id}
        &nbsp;|&nbsp; Session tokens: ${w.token_count || 0}
      </div>
    </div>`;
  }).join('');
}

async function refresh() {
  try {
    const [status, tokData] = await Promise.all([
      fetch('/status').then(r => r.json()),
      fetch('/api/tokens').then(r => r.json()),
    ]);
    const windows = status.windows || [];
    const all   = tokData.tokens || [];
    const now   = Date.now();
    const v2    = all.filter(t => (t.version || '').includes('v2')).length;
    const v3    = all.filter(t => t.version === 'v3').length;
    const fresh = all.filter(t => {
      try { return (now - new Date(t.timestamp_utc).getTime()) / 1000 < 120; } catch { return false; }
    }).length;
    const ready = windows.filter(w => w.status !== 'loading' && w.status !== 'reloading').length;

    document.getElementById('stat-total').textContent = all.length;
    document.getElementById('stat-v2').textContent    = v2;
    document.getElementById('stat-v3').textContent    = v3;
    document.getElementById('stat-fresh').textContent = fresh;
    document.getElementById('stat-ready').textContent = `${ready}/${windows.length}`;

    const byWin = {};
    for (const t of all) byWin[t.window_id] = (byWin[t.window_id] || 0) + 1;
    for (const w of windows) w.token_count = byWin[w.id] || 0;

    renderWindows(windows);
    document.getElementById('refresh-info').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
  } catch (e) {}
}

async function v2Start(id)     { const d = await apiCall(`/windows/${id}/v2/start`);       if (d) showToast(`Window ${id}: V2 started`); await refresh(); }
async function v2Stop(id)      { const d = await apiCall(`/windows/${id}/v2/stop`);        if (d) showToast(`Window ${id}: V2 stopped`, '#f87171'); await refresh(); }
async function v3Start(id)     { const d = await apiCall(`/windows/${id}/v3/start`);       if (d) showToast(`Window ${id}: V3 started`, '#60a5fa'); await refresh(); }
async function v3Stop(id)      { const d = await apiCall(`/windows/${id}/v3/stop`);        if (d) showToast(`Window ${id}: V3 stopped`, '#6b7280'); await refresh(); }
async function runInvisible(id){ const d = await apiCall(`/windows/${id}/invisible/run`);  if (d) showToast(`Window ${id}: Invisible triggered`, '#8b5cf6'); await refresh(); }

async function clearTokens() {
  if (!confirm('Clear all tokens from tokens.json?\\nThis cannot be undone.')) return;
  const d = await apiCall('/tokens/clear', 'DELETE');
  if (d && d.ok) showToast(`Cleared ${d.removed} token(s)`, '#fb923c');
  else if (d) showToast('Error: ' + (d.detail || 'unknown'), '#f87171');
  await refresh();
}

async function deleteProfiles() {
  if (!confirm('Delete ALL harvester_profiles? Browsers must be restarted after.')) return;
  const d = await apiCall('/profiles/delete', 'DELETE');
  if (d && d.ok) showToast(`Deleted ${d.deleted} profile(s)`, '#f87171');
  else if (d) showToast('Error: ' + (d.detail || 'unknown'), '#f87171');
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""

# ── FastAPI routes ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@app.get("/status")
async def get_status():
    windows = [{"id": wid, "status": w.get("status", "loading")} for wid, w in _windows.items()]
    return {"windows": windows, "tabs_mode": False}


@app.post("/api")
async def store_token(request: Request):
    """Receive a harvested token and persist it directly to tokens.json."""
    data = await request.json()
    async with _tokens_lock:
        total = _append_token(data)
    version = data.get("version", "v3")
    action  = data.get("action", "")
    token   = data.get("token", "")
    preview = (token[:40] + "...") if len(token) > 40 else token
    print(f"[token] {version:<14} {action:<22} {preview}  (total on disk: {total})")

    # Auto-reload: only for v2 and v3 harvester tokens, gated by TUNING.
    should_reload = TUNING and data.get("_reload_after", False) and version in ("v2", "v3")
    if should_reload:
        window_id  = data.get("window_id", -1)
        target_wid = None
        for wid, w in _windows.items():
            if w.get("id") == window_id or window_id == -1:
                target_wid = wid
                if window_id != -1:
                    break
        if target_wid is not None and _windows[target_wid].get("status") not in ("loading", "reloading"):
            asyncio.create_task(_reload_window_after_token(target_wid, version))

    return {"total_count": total, "ok": True}


async def _reload_window_after_token(window_id: int, version: str):
    """
    Reload a window after a v2 or v3 token is harvested, then re-run the full
    ready flow (blocker -> ready signal -> re-inject harvester script).

    TUNING=True (standard):
        page.reload() — fast in-place reload. Keeps the existing Camoufox context
        and fingerprint.

    HARD_TUNING=True:
        Full profile wipe + brand-new Camoufox context with a fresh fingerprint:
          1. Save 4 essential cookies to RAM
                 (arena-auth-prod-v1.0, arena-auth-prod-v1.1, __cf_bm, cf_clearance)
          2. Close the Camoufox browser context and exit its manager
          3. Delete the profile directory from disk
          4. Launch a new AsyncCamoufox with a fresh BrowserForge fingerprint
          5. Re-inject the 4 saved cookies
          6. Navigate to target URL (arena.ai or arena.ai/c/<eval_id>)
          7. Blocker -> ready signal -> re-inject active harvester script
    """
    w = _windows.get(window_id)
    if not w:
        return

    page    = w.get("page")
    context = w.get("context")
    prev_status   = w.get("status", "ready")

    # If stop was clicked while token POST was in-flight — bail out immediately.
    active_script  = w.get("active_script")
    active_version = w.get("active_version")
    if not active_script or not active_version:
        print(f"[window {window_id}] Harvester was stopped — skipping reload.")
        return

    if not page or not context:
        return

    # ── HARD_TUNING path ──────────────────────────────────────────────────────
    if HARD_TUNING:
        print(f"[window {window_id}] HARD_TUNING: saving cookies, wiping profile, rebuilding context...")
        _windows[window_id]["status"] = "reloading"

        # ── 1. Save 4 essential cookies to RAM ────────────────────────────────
        HARD_COOKIE_NAMES = {
            "arena-auth-prod-v1.0",
            "arena-auth-prod-v1.1",
            "__cf_bm",
            "cf_clearance",
        }
        saved_cookies: list[dict] = []
        try:
            all_cookies = await context.cookies(["https://arena.ai"])
            saved_cookies = [c for c in all_cookies if c.get("name") in HARD_COOKIE_NAMES]
            for c in saved_cookies:
                print(f"[window {window_id}]   saved cookie: {c['name']} ({len(c.get('value',''))} chars)")
        except Exception as e:
            print(f"[window {window_id}]   Cookie save error: {e}")

        # ── 2. Close the Camoufox context and its manager ─────────────────────
        old_manager = _camoufox_managers.get(window_id)
        try:
            await context.close()
            print(f"[window {window_id}]   Context closed")
        except Exception as e:
            print(f"[window {window_id}]   Context close error: {e}")
        if old_manager is not None:
            try:
                await old_manager.__aexit__(None, None, None)
            except Exception:
                pass
            _camoufox_managers.pop(window_id, None)

        # ── 3. Delete the profile directory from disk ─────────────────────────
        profile_dir = PROFILES_DIR / f"window_{window_id}"
        try:
            if profile_dir.exists():
                shutil.rmtree(profile_dir)
                print(f"[window {window_id}]   Profile wiped: {profile_dir}")
            profile_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[window {window_id}]   Profile wipe error: {e}")

        # ── 4. Launch a fresh Camoufox context with a new BrowserForge fingerprint
        try:
            new_fp  = _generate_fingerprint()   # brand-new fingerprint values
            manager = AsyncCamoufox(
                headless=False,
                fingerprint=new_fp,
                os=list(_FP_OS),
                screen=_FP_SCREEN,
                main_world_eval=True,
            )
            new_browser = await manager.__aenter__()
            new_context = await new_browser.new_context()
            new_page    = await new_context.new_page()
            _camoufox_managers[window_id] = manager
            print(f"[window {window_id}]   Fresh Camoufox context launched (new fingerprint)")
        except Exception as e:
            print(f"[window {window_id}]   Context relaunch failed: {e}")
            _windows[window_id]["status"] = "ready"
            return

        # Update the global window state with the new context and page
        _windows[window_id]["context"] = new_context
        _windows[window_id]["page"]    = new_page
        page    = new_page
        context = new_context

        # ── 5. Re-inject the 4 saved cookies ─────────────────────────────────
        if saved_cookies:
            try:
                clean = []
                for c in saved_cookies:
                    entry = {
                        "name":     c["name"],
                        "value":    c["value"],
                        "domain":   c.get("domain", ".arena.ai"),
                        "path":     c.get("path", "/"),
                        "secure":   c.get("secure", True),
                        "httpOnly": c.get("httpOnly", False),
                        "sameSite": c.get("sameSite", "Lax"),
                    }
                    if c.get("expires", -1) > 0:
                        entry["expires"] = c["expires"]
                    clean.append(entry)
                await new_context.add_cookies(clean)
                print(f"[window {window_id}]   {len(clean)} cookies re-injected into fresh context")
            except Exception as e:
                print(f"[window {window_id}]   Cookie re-inject error: {e}")

        # ── 6. Navigate to target URL ─────────────────────────────────────────
        if FIVE_GAIN and _EVAL_ID:
            target_url = f"https://arena.ai/c/{_EVAL_ID}"
        else:
            target_url = "https://arena.ai"

        print(f"[window {window_id}]   Navigating to {target_url}")
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
        except Exception as e:
            print(f"[window {window_id}]   Navigation error: {e}")

        # Start a fresh mouse mover for the new page
        asyncio.create_task(mouse_mover(page, window_id))

    # ── Standard TUNING path (simple reload) ─────────────────────────────────
    else:
        print(f"[window {window_id}] Token received ({version}) — reloading page...")
        _windows[window_id]["status"] = "reloading"

        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
        except Exception as e:
            print(f"[window {window_id}] Reload error: {e}")
            _windows[window_id]["status"] = prev_status
            return

        # If FIVE_GAIN: navigate to eval URL after reload
        if FIVE_GAIN and _EVAL_ID:
            target_url = f"https://arena.ai/c/{_EVAL_ID}"
            print(f"[window {window_id}] 5_GAIN: navigating to {target_url}")
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)
            except Exception as e:
                print(f"[window {window_id}] 5_GAIN navigation error: {e}")

    # ── Shared post-reload steps (both TUNING and HARD_TUNING) ───────────────

    # Re-run blocker (must be main world — overrides window.fetch)
    try:
        await page.evaluate(BLOCKER_SCRIPT)
    except Exception as e:
        print(f"[window {window_id}] Post-reload blocker error: {e}")

    # Re-mark ready (isolated world — pure fetch call)
    try:
        await page.evaluate(READY_SIGNAL_SCRIPT, window_id)
    except Exception as e:
        print(f"[window {window_id}] Post-reload ready signal error: {e}")
        _windows[window_id]["status"] = "harvesting_v2" if active_version == "v2" else "harvesting_v3"

    # Re-check: if stop was clicked during reload, bail out without re-injecting
    if _windows[window_id].get("active_script") is None:
        print(f"[window {window_id}] Harvester stopped during reload — not re-injecting.")
        return

    # Re-inject the harvester script so it keeps running automatically
    print(f"[window {window_id}] Re-injecting {active_version} harvester script...")
    try:
        await page.evaluate(active_script)
        status = "harvesting_v2" if active_version == "v2" else "harvesting_v3"
        _windows[window_id]["status"] = status
        mode_label = "HARD_TUNING" if HARD_TUNING else "TUNING"
        print(f"[window {window_id}] {mode_label} cycle complete — {active_version} harvester running.")
    except Exception as e:
        print(f"[window {window_id}] Re-inject error: {e}")
        _windows[window_id]["status"] = "ready"


@app.get("/api/tokens")
async def get_tokens():
    async with _tokens_lock:
        tokens_data = _load_tokens_file()
    tokens = tokens_data.get("tokens", [])
    return {"tokens": tokens, "total": len(tokens)}


@app.get("/api/tokens/latest")
async def get_latest_tokens():
    async with _tokens_lock:
        tokens_data = _load_tokens_file()
    tokens = tokens_data.get("tokens", [])
    latest: dict[str, dict] = {}
    for t in tokens:
        v = str(t.get("version", "unknown"))
        latest[v] = t
    return {"latest": latest}


@app.delete("/tokens/clear")
async def clear_tokens():
    async with _tokens_lock:
        tokens_data = _load_tokens_file()
        removed = len(tokens_data.get("tokens", []))
        empty = {
            "tokens": [],
            "total_count": 0,
            "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _save_tokens_file(empty)
    print(f"[tokens] Cleared {removed} token(s) from {TOKENS_FILE}")
    return {"ok": True, "removed": removed}


@app.delete("/profiles/delete")
async def delete_profiles():
    if not PROFILES_DIR.exists():
        return {"ok": True, "deleted": 0, "detail": "No profiles directory found"}
    deleted = 0
    errors  = []
    for item in sorted(PROFILES_DIR.iterdir()):
        if item.is_dir():
            try:
                shutil.rmtree(item)
                deleted += 1
                print(f"[profiles] Deleted: {item}")
            except Exception as e:
                errors.append(str(e))
    if errors:
        return {"ok": False, "deleted": deleted, "detail": "; ".join(errors)}
    return {"ok": True, "deleted": deleted}


@app.post("/windows/{window_id}/ready")
async def window_ready(window_id: int):
    if window_id not in _windows:
        raise HTTPException(status_code=404, detail="Window not found")
    _windows[window_id]["status"] = "ready"
    return {"ok": True, "window_id": window_id, "status": "ready"}


@app.post("/windows/{window_id}/v2/start")
async def v2_start(window_id: int):
    w = _windows.get(window_id)
    if not w:
        raise HTTPException(status_code=404, detail="Window not found")
    try:
        # Inject window_id so tokens carry the correct window_id in their payload
        script = V2_SCRIPT.replace(
            'source_url: window.location.href,',
            f'source_url: window.location.href, window_id: {window_id},',
            1
        )
        await w["page"].evaluate(script)
        w["status"] = "harvesting_v2"
        w["active_script"]  = script
        w["active_version"] = "v2"
        return {"ok": True, "status": "harvesting_v2"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/windows/{window_id}/v2/stop")
async def v2_stop(window_id: int):
    w = _windows.get(window_id)
    if not w:
        raise HTTPException(status_code=404, detail="Window not found")
    try:
        # Must call in main world because __STOP_V2_HARVEST__ lives there
        await w["page"].evaluate("mw:if (typeof window.__STOP_V2_HARVEST__ === 'function') window.__STOP_V2_HARVEST__();")
        w["status"] = "idle"
        w["active_script"]  = None
        w["active_version"] = None
        return {"ok": True, "status": "idle"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/windows/{window_id}/v3/start")
async def v3_start(window_id: int):
    w = _windows.get(window_id)
    if not w:
        raise HTTPException(status_code=404, detail="Window not found")
    try:
        script = V3_SCRIPT.replace(
            'source_url: window.location.href,',
            f'source_url: window.location.href, window_id: {window_id},',
            1
        )
        await w["page"].evaluate(script)
        w["status"] = "harvesting_v3"
        w["active_script"]  = script
        w["active_version"] = "v3"
        return {"ok": True, "status": "harvesting_v3"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/windows/{window_id}/v3/stop")
async def v3_stop(window_id: int):
    w = _windows.get(window_id)
    if not w:
        raise HTTPException(status_code=404, detail="Window not found")
    try:
        await w["page"].evaluate("mw:if (typeof window.__STOP_HARVEST__ === 'function') window.__STOP_HARVEST__();")
        w["status"] = "idle"
        w["active_script"]  = None
        w["active_version"] = None
        return {"ok": True, "status": "idle"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/windows/{window_id}/invisible/run")
async def invisible_run(window_id: int):
    w = _windows.get(window_id)
    if not w:
        raise HTTPException(status_code=404, detail="Window not found")
    try:
        await w["page"].evaluate(ON_DEMAND_V2_SCRIPT)
        return {"ok": True, "message": "Invisible script triggered"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Mouse movement coroutine ──────────────────────────────────

async def mouse_mover(page, window_id: int):
    """Continuously moves the mouse in natural bezier curves. Never clicks."""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
    except Exception:
        vp = {"width": 1280, "height": 800}

    W, H = vp["width"], vp["height"]
    cx, cy = W // 2, H // 2

    def rand_point():
        return random.randint(80, W - 80), random.randint(80, H - 80)

    def bezier_points(x0, y0, x1, y1, steps=12):
        cpx = (x0 + x1) // 2 + random.randint(-80, 80)
        cpy = (y0 + y1) // 2 + random.randint(-80, 80)
        pts = []
        for i in range(1, steps + 1):
            t  = i / steps
            bx = (1-t)**2 * x0 + 2*(1-t)*t * cpx + t**2 * x1
            by = (1-t)**2 * y0 + 2*(1-t)*t * cpy + t**2 * y1
            pts.append((int(bx), int(by)))
        return pts

    while True:
        try:
            tx, ty = rand_point()
            for px, py in bezier_points(cx, cy, tx, ty, steps=random.randint(8, 16)):
                await page.mouse.move(px, py)
                await asyncio.sleep(random.uniform(0.03, 0.12))
            cx, cy = tx, ty
            await asyncio.sleep(random.uniform(0.8, 3.5))
        except Exception:
            break  # page/context closed — exit gracefully


# ── config.json patch helpers ─────────────────────────────────

def _load_config_file() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_config_file(cfg: dict) -> None:
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def _patch_config(updates: dict) -> None:
    cfg = _load_config_file()
    cfg.update(updates)
    _save_config_file(cfg)
    for k, v in updates.items():
        preview = (v[:30] + "...") if isinstance(v, str) and len(v) > 30 else v
        print(f"[config] {k} = {preview}")


# ── AUTO_LOGIN sign-in helper ──────────────────────────────────

async def auto_login_window(page, context, window_id: int) -> bool:
    """
    Sign in to arena.ai via fetch() inside the page.
    Runs in isolated world (no mw: prefix) — the fetch credentials flow
    is handled server-side; the browser stores Set-Cookie headers automatically.
    """
    print(f"[window {window_id}] AUTO_LOGIN: signing in as {_AUTO_LOGIN_EMAIL}...")

    email_escaped    = _AUTO_LOGIN_EMAIL.replace('"', '\\"')
    password_escaped = _AUTO_LOGIN_PASSWORD.replace('"', '\\"')

    login_script = f"""
async () => {{
    const resp = await fetch("https://arena.ai/nextjs-api/sign-in/email", {{
        method: "POST",
        headers: {{
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.6",
            "content-type": "application/json",
            "priority": "u=1, i",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin"
        }},
        referrer: "https://arena.ai/",
        body: JSON.stringify({{
            email: "{email_escaped}",
            password: "{password_escaped}",
            shouldLinkHistory: false
        }}),
        credentials: "include"
    }});

    const headers = {{}};
    resp.headers.forEach((v, k) => {{ headers[k] = v; }});
    let body = "";
    try {{ body = await resp.text(); }} catch(_) {{}}
    return {{ status: resp.status, headers, body }};
}}
"""

    try:
        result = await page.evaluate(login_script)
    except Exception as e:
        print(f"[window {window_id}] AUTO_LOGIN fetch error: {e}")
        return False

    status = result.get("status", 0)
    print(f"[window {window_id}]   Sign-in response status: {status}")

    if status not in (200, 201, 204):
        body_preview = result.get("body", "")[:200]
        print(f"[window {window_id}] AUTO_LOGIN failed (status {status}): {body_preview}")
        return False

    await asyncio.sleep(1)

    try:
        all_cookies = await context.cookies(["https://arena.ai"])
    except Exception as e:
        print(f"[window {window_id}] Could not read context cookies: {e}")
        return False

    cookie_map = {c["name"]: c["value"] for c in all_cookies}
    updates: dict = {}

    v10 = cookie_map.get("arena-auth-prod-v1.0", "")
    v11 = cookie_map.get("arena-auth-prod-v1.1", "")
    v1  = cookie_map.get("arena-auth-prod-v1",   "")

    if v10:
        updates["auth_prod"]    = v10
        updates["auth_prod_v2"] = v11
        updates["v2_auth"]      = True
        print(f"[window {window_id}]   arena-auth-prod-v1.0 captured ({len(v10)} chars)")
        if v11:
            print(f"[window {window_id}]   arena-auth-prod-v1.1 captured ({len(v11)} chars)")
    elif v1:
        updates["auth_prod"] = v1
        print(f"[window {window_id}]   arena-auth-prod-v1 captured ({len(v1)} chars)")
    else:
        print(f"[window {window_id}]   No auth cookie found in context after login")

    if updates:
        _patch_config(updates)
        print(f"[window {window_id}] AUTO_LOGIN: auth cookies saved to {CONFIG_FILE}")

    return bool(updates)


async def sync_cf_cookies_to_config(context, window_id: int) -> None:
    """Write cf_clearance and __cf_bm cookies to config.json after window is ready."""
    try:
        all_cookies = await context.cookies(["https://arena.ai"])
    except Exception as e:
        print(f"[window {window_id}] cf-cookie sync failed (read): {e}")
        return

    cookie_map = {c["name"]: c["value"] for c in all_cookies}
    updates: dict = {}

    cf_clearance = cookie_map.get("cf_clearance", "")
    cf_bm        = cookie_map.get("__cf_bm", "")

    if cf_clearance:
        updates["cf_clearance"] = cf_clearance
        print(f"[window {window_id}]   cf_clearance synced ({len(cf_clearance)} chars)")
    if cf_bm:
        updates["cf_bm"] = cf_bm
        print(f"[window {window_id}]   __cf_bm synced ({len(cf_bm)} chars)")

    if updates:
        _patch_config(updates)


async def inject_cookies(context, window_id: int) -> None:
    """Inject manual COOKIE_V1 / COOKIE_V2 into the context (COOKIES=True mode)."""
    print(f"[window {window_id}] Injecting cookies (COOKIES=True)...")
    try:
        all_cookies = await context.cookies()
        old_cookie  = next((c for c in all_cookies if c.get("name") == "arena-auth-prod-v1"), None)

        if old_cookie:
            await context.clear_cookies(name="arena-auth-prod-v1")
            print(f"[window {window_id}]   Removed arena-auth-prod-v1")
        else:
            print(f"[window {window_id}]   arena-auth-prod-v1 not found — using defaults")

        base: dict = {
            "domain":   old_cookie.get("domain",   ".arena.ai") if old_cookie else ".arena.ai",
            "path":     old_cookie.get("path",      "/")         if old_cookie else "/",
            "secure":   old_cookie.get("secure",    True)        if old_cookie else True,
            "httpOnly": old_cookie.get("httpOnly",  True)        if old_cookie else True,
            "sameSite": old_cookie.get("sameSite",  "Lax")       if old_cookie else "Lax",
        }
        if old_cookie and old_cookie.get("expires", -1) > 0:
            base["expires"] = old_cookie["expires"]

        await context.add_cookies([{**base, "name": "arena-auth-prod-v1.0", "value": COOKIE_V1}])
        print(f"[window {window_id}]   Set arena-auth-prod-v1.0")
        await context.add_cookies([{**base, "name": "arena-auth-prod-v1.1", "value": COOKIE_V2}])
        print(f"[window {window_id}]   Added arena-auth-prod-v1.1")
    except Exception as e:
        print(f"[window {window_id}] Cookie injection error: {e}")


# ── Window setup ──────────────────────────────────────────────

async def setup_window(window_id: int):
    """
    Launch a Camoufox browser window and run the full auth + ready flow.
    Each window gets its own AsyncCamoufox manager, browser, context, and page.
    """
    print(f"[window {window_id}] Launching Camoufox with BrowserForge fingerprint...")
    fingerprint = _generate_fingerprint()

    profile_dir = PROFILES_DIR / f"window_{window_id}"
    profile_dir.mkdir(parents=True, exist_ok=True)

    # AsyncCamoufox is a context manager. We enter it manually to keep the
    # browser alive across async operations.
    manager = AsyncCamoufox(
        headless=False,
        fingerprint=fingerprint,
        os=list(_FP_OS),
        screen=_FP_SCREEN,
        main_world_eval=True,    # required: enables "mw:" prefix for main-world eval
    )
    browser = await manager.__aenter__()
    _camoufox_managers[window_id] = manager

    # Create an isolated browsing context (like an incognito window) and a page.
    context = await browser.new_context()
    page    = await context.new_page()

    _windows[window_id] = {
        "id":      window_id,
        "status":  "loading",
        "page":    page,
        "context": context,
    }

    initial_url = "https://arena.ai"
    print(f"[window {window_id}] Navigating to {initial_url}...")
    try:
        await page.goto(initial_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"[window {window_id}] Navigation warning: {e}")

    await asyncio.sleep(2)

    print(f"[window {window_id}] Running initial v2 script...")
    try:
        await page.evaluate(INITIAL_V2_SCRIPT)
        await asyncio.sleep(1)
    except Exception as e:
        print(f"[window {window_id}] Initial script error: {e}")

    # ── AUTO_LOGIN ─────────────────────────────────────────────────
    if AUTO_LOGIN:
        await auto_login_window(page, context, window_id)
        await asyncio.sleep(1)

    print(f"[window {window_id}] Reloading page...")
    try:
        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(2)
    except Exception as e:
        print(f"[window {window_id}] Reload error: {e}")

    if COOKIES:
        await inject_cookies(context, window_id)

    # ── FIVE_GAIN: navigate to eval URL after auth + reload ────────
    if FIVE_GAIN and _EVAL_ID:
        target_url = f"https://arena.ai/c/{_EVAL_ID}"
        print(f"[window {window_id}] 5_GAIN: navigating to {target_url}...")
        await asyncio.sleep(3)
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
        except Exception as e:
            print(f"[window {window_id}] 5_GAIN navigation error: {e}")

    print(f"[window {window_id}] Marking as ready...")
    try:
        await page.evaluate(READY_SIGNAL_SCRIPT, window_id)
    except Exception as e:
        print(f"[window {window_id}] Ready signal JS failed ({e}), marking directly")
        _windows[window_id]["status"] = "ready"

    # ── Sync cf_clearance + __cf_bm to config.json ─────────────────
    if AUTO_LOGIN:
        await sync_cf_cookies_to_config(context, window_id)

    await asyncio.sleep(1)
    print(f"[window {window_id}] Running blocker script...")
    try:
        await page.evaluate(BLOCKER_SCRIPT)
    except Exception as e:
        print(f"[window {window_id}] Blocker script error: {e}")

    print(f"[window {window_id}] Ready. Starting mouse mover.")
    asyncio.create_task(mouse_mover(page, window_id))


# ── Browser runner ────────────────────────────────────────────

async def run_browsers(server_ready_event: asyncio.Event):
    await server_ready_event.wait()
    await asyncio.sleep(0.5)

    PROFILES_DIR.mkdir(exist_ok=True)

    for i in range(N):
        await setup_window(i)
        await asyncio.sleep(0.8)

    os_label = " / ".join(_FP_OS)
    print(f"\n{N} window(s) launched.")
    print(f"   Dashboard  : http://localhost:{SERVER_PORT}")
    print(f"   Token file : {TOKENS_FILE}  <- read by modula.py / main.py")
    print(f"   Fingerprint: Firefox / {os_label}")
    if FIVE_GAIN:
        print(f"   5_GAIN     : arena.ai/c/{_EVAL_ID}")

    while True:
        await asyncio.sleep(10)


class _ServerWithReadyEvent(uvicorn.Server):
    def __init__(self, config, ready_event: asyncio.Event):
        super().__init__(config)
        self._ready_event = ready_event

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        self._ready_event.set()


async def main():
    os_label = " / ".join(_FP_OS)
    print("=" * 55)
    print("  reCAPTCHA Token Harvester -- Camoufox Edition")
    print(f"  Windows        : {N}")
    print(f"  Engine         : Camoufox (Firefox anti-detect)")
    print(f"  Fingerprint OS : {os_label}")
    print(f"  Screen profile : {_FP_SCREEN}")
    print(f"  Cookies mode   : {COOKIES}")
    print(f"  Auto Login     : {AUTO_LOGIN}{(' (' + _AUTO_LOGIN_EMAIL + ')') if AUTO_LOGIN else ''}")
    print(f"  5_GAIN         : {FIVE_GAIN}{(' -> arena.ai/c/' + _EVAL_ID) if FIVE_GAIN and _EVAL_ID else ''}")
    print(f"  Tuning (reload): {TUNING}")
    print(f"  Hard Tuning    : {HARD_TUNING}")
    print(f"  Output file    : {TOKENS_FILE}  (modula.py compatible)")
    print(f"  Dashboard      : http://localhost:{SERVER_PORT}")
    print("=" * 55)

    server_ready = asyncio.Event()
    config = uvicorn.Config(app, host="0.0.0.0", port=SERVER_PORT, log_level="warning")
    server = _ServerWithReadyEvent(config, server_ready)

    await asyncio.gather(
        server.serve(),
        run_browsers(server_ready),
    )


if __name__ == "__main__":
    asyncio.run(main())