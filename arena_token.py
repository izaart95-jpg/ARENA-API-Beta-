"""
reCAPTCHA Token Harvester
=========================
Config at the top — edit before running.                                                                                                                                                                                                              Usage:
    pip install playwright fastapi uvicorn
    playwright install chromium
    python harvester.py
Then open http://localhost:5000
"""

import asyncio
import json
import math
import os
import random
import time
import secrets
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import async_playwright, BrowserContext, Page

# ============================================================
# CONFIGURATION — edit these
# ============================================================

CUSTOM = True
# If CUSTOM=True, set PATH to your browser executable.
#
#   Linux Brave ........ "/usr/bin/brave-browser"
#   Linux Chrome ....... "/usr/bin/google-chrome"
#   Linux Chromium ..... "/usr/bin/chromium-browser"
#
#   Windows Brave ...... r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
#   Windows Chrome ..... r"C:\Program Files\Google\Chrome\Application\chrome.exe"
#   Windows Edge ....... r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
#
#   macOS Brave ........ "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
#   macOS Chrome ....... "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
#   macOS Chromium ..... "/Applications/Chromium.app/Contents/MacOS/Chromium"
PATH = "/usr/bin/brave-browser"

N = 3  # number of windows (TABS=False) or tabs (TABS=True)

EXTENSIONS = True
# If EXTENSIONS=True you MUST set EXTENSIONS_DIR to the Extensions folder of
# the browser profile you want to load extensions from.
# Leave as "" only if EXTENSIONS=False.
#
# How to find it:
#   Open your browser → go to:  chrome://version  (or brave://version)
#   Look for "Profile Path" — your Extensions folder is inside that path.
#   Example:  Profile Path = /home/user/.config/BraveSoftware/.../Default
#             Extensions dir = /home/user/.config/BraveSoftware/.../Default/Extensions
#
#   Linux Brave ........ "/root/.config/BraveSoftware/Brave-Browser/Default/Extensions"
#   Linux Chrome ....... "/home/USERNAME/.config/google-chrome/Default/Extensions"
#   Linux Chromium ..... "/home/USERNAME/.config/chromium/Default/Extensions"
#
#   Windows Brave ...... r"C:\Users\USERNAME\AppData\Local\BraveSoftware\Brave-Browser\User Data\Default\Extensions"
#   Windows Chrome ..... r"C:\Users\USERNAME\AppData\Local\Google\Chrome\User Data\Default\Extensions"
#   Windows Edge ....... r"C:\Users\USERNAME\AppData\Local\Microsoft\Edge\User Data\Default\Extensions"
#
#   macOS Brave ........ "/Users/USERNAME/Library/Application Support/BraveSoftware/Brave-Browser/Default/Extensions"
#   macOS Chrome ....... "/Users/USERNAME/Library/Application Support/Google/Chrome/Default/Extensions"
#   macOS Chromium ..... "/Users/USERNAME/Library/Application Support/Chromium/Default/Extensions"
EXTENSIONS_DIR = "/root/.config/BraveSoftware/Brave-Browser/Default/Extensions"
# Get Extensions file in Default/EXTESIONS from browser://version
TABS = True   # False = N separate browser windows  |  True = N tabs in one window

CUS_PROFILE = False
# If CUS_PROFILE=True, ALL contexts/windows use PROFILE_PATH as their
# user_data_dir instead of the auto-generated harvester_profiles/ dirs.
# Useful when you want to reuse an existing browser profile that already has
# cookies, history, and a high reCAPTCHA trust score built up.
#
# NOTE: When TABS=False (windows mode) all N windows share the same profile dir.
#       When TABS=True  (tabs mode)   the single persistent context uses it.
#
# PROFILE_PATH must be the "User Data" or "Profile" directory, NOT the
# browser installation folder. Examples:
#
#   Linux Brave ........ "/root/.config/BraveSoftware/Brave-Browser"
#     (contains Default/, Local State, etc.)
#   Linux Chrome ....... "/home/USERNAME/.config/google-chrome"
#   Linux Chromium ..... "/home/USERNAME/.config/chromium"
#
#   Windows Brave ...... r"C:\Users\USERNAME\AppData\Local\BraveSoftware\Brave-Browser\User Data"
#   Windows Chrome ..... r"C:\Users\USERNAME\AppData\Local\Google\Chrome\User Data"
#   Windows Edge ....... r"C:\Users\USERNAME\AppData\Local\Microsoft\Edge\User Data"
#
#   macOS Brave ........ "/Users/USERNAME/Library/Application Support/BraveSoftware/Brave-Browser"
#   macOS Chrome ....... "/Users/USERNAME/Library/Application Support/Google/Chrome"
#   macOS Chromium ..... "/Users/USERNAME/Library/Application Support/Chromium"
#
# How to find it quickly: open browser → go to chrome://version → "Profile Path"
#   then go UP one directory (remove the last folder, e.g. "Default").
#   That parent directory is what you put here.
PROFILE_PATH = ""

# ============================================================
# COOKIE INJECTION — edit these when COOKIES=True
# ============================================================

COOKIES = False
# When COOKIES=True, before running the blocker script on each context/window,
# the harvester will perform three cookie operations on arena.ai:
#
#   1. Find the existing cookie named "arena-auth-prod-v1" and rename it to
#      "arena-auth-prod-v1.0" (keeping all other attributes the same).
#
#   2. Set the value of "arena-auth-prod-v1.0" to COOKIE_V1 (defined below).
#
#   3. Add a brand-new cookie named "arena-auth-prod-v1.1" whose value is
#      set to COOKIE_V2 (defined below).
#
# Both operations target the arena.ai domain so the cookies are sent with
# every request to that site.

COOKIE_V1 = ""
# Paste the full value for the renamed/updated auth cookie here.
# Example: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."

COOKIE_V2 = ""
# Paste the full value for the new arena-auth-prod-v1.1 cookie here.
# Example: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."

SERVER_PORT = 5000
# ============================================================

PROFILES_DIR = Path("harvester_profiles")

# ── Startup validation ─────────────────────────────────────────
if CUS_PROFILE:
    if not PROFILE_PATH or not PROFILE_PATH.strip():
        raise RuntimeError(
            "CUS_PROFILE=True but PROFILE_PATH is empty.\n"
            "Set PROFILE_PATH to your browser's user data directory.\n"
            "See the config comments above for examples per OS."
        )
    _profile_path_obj = Path(PROFILE_PATH.strip())
    if not _profile_path_obj.exists():
        raise RuntimeError(
            f"CUS_PROFILE=True but PROFILE_PATH does not exist: {_profile_path_obj}\n"
            "Check the path. It should be the User Data dir, not the browser exe.\n"
            "Example (Brave Linux): /root/.config/BraveSoftware/Brave-Browser"
        )
    if not _profile_path_obj.is_dir():
        raise RuntimeError(f"PROFILE_PATH is not a directory: {_profile_path_obj}")
    print(f"[profile] Using custom profile: {_profile_path_obj}")

if COOKIES:
    if not COOKIE_V1 or not COOKIE_V1.strip():
        raise RuntimeError(
            "COOKIES=True but COOKIE_V1 is empty.\n"
            "Set COOKIE_V1 to the value for the arena-auth-prod-v1.0 cookie."
        )
    if not COOKIE_V2 or not COOKIE_V2.strip():
        raise RuntimeError(
            "COOKIES=True but COOKIE_V2 is empty.\n"
            "Set COOKIE_V2 to the value for the new arena-auth-prod-v1.1 cookie."
        )

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global state ──────────────────────────────────────────────
_windows: dict[int, dict] = {}
# {
#   id: int,
#   status: "loading" | "ready" | "idle" | "harvesting_v2" | "harvesting_v3",
#   page: Page,
#   context: BrowserContext,
# }

_tokens: list[dict] = []
_tokens_lock = asyncio.Lock()

# ── New scripts ───────────────────────────────────────────────

# Script to run on initial page load
INITIAL_V2_SCRIPT = """
(function() {
  'use strict';
  
  const CONFIG = {
    SITE_KEY: '6Led_uYrAAAAAKjxDIF58fgFtX3t8loNAK85bW9I',
    TIMEOUT: 60000
  };
  
  console.log('🎯 INITIAL v2 reCAPTCHA Token Generator');
  
  async function getV2Token() {
    const w = window.wrappedJSObject || window;
    
    console.log('🔍 Checking for grecaptcha.enterprise...');
    
    await waitForGrecaptcha(w);
    
    const g = w.grecaptcha?.enterprise;
    if (!g || typeof g.render !== 'function') {
      throw new Error('NO_GRECAPTCHA_V2');
    }
    
    console.log('✅ grecaptcha.enterprise found');
    
    let settled = false;
    const done = (fn, arg) => {
      if (settled) return;
      settled = true;
      fn(arg);
    };
    
    return new Promise((resolve, reject) => {
      try {
        const el = w.document.createElement('div');
        el.style.cssText = 'position:fixed;left:-9999px;top:-9999px;width:1px;height:1px;';
        w.document.body.appendChild(el);
        
        console.log('📦 Created hidden div');
        
        const timer = w.setTimeout(() => {
          console.log('⏱️ Timeout reached');
          done(reject, 'V2_TIMEOUT');
        }, CONFIG.TIMEOUT);
        
        const wid = g.render(el, {
          sitekey: CONFIG.SITE_KEY,
          size: 'invisible',
          callback: (tok) => {
            console.log('✅ INITIAL token received');
            w.clearTimeout(timer);
            done(resolve, tok);
          },
          'error-callback': () => {
            console.log('❌ Widget error');
            w.clearTimeout(timer);
            done(reject, 'V2_ERROR');
          }
        });
        
        console.log('Widget rendered with ID:', wid);
        
        try {
          if (typeof g.execute === 'function') {
            console.log('🚀 Executing widget...');
            g.execute(wid);
          }
        } catch (e) {
          console.log('Execute error:', e.message);
        }
        
      } catch (e) {
        console.log('❌ Setup error:', e);
        done(reject, String(e));
      }
    });
  }
  
  async function waitForGrecaptcha(w) {
    const startTime = Date.now();
    const maxWait = 60000;
    
    while (Date.now() - startTime < maxWait) {
      const g = w.grecaptcha?.enterprise;
      if (g && typeof g.render === 'function') {
        console.log(`✅ grecaptcha ready after ${Date.now() - startTime}ms`);
        return true;
      }
      await new Promise(r => setTimeout(r, 100));
    }
    throw new Error('Timeout waiting for grecaptcha');
  }
  
  function displayToken(token) {
    console.log('\\n' + '='.repeat(60));
    console.log('✅ INITIAL TOKEN GENERATED:');
    console.log('='.repeat(60));
    console.log('\\n📋 Token:', token);
    console.log('\\n📏 Length:', token.length, 'characters');
    console.log('🔍 Preview:', token.substring(0, 50) + '...');
    
    fetch('http://localhost:5000/api', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ 
        token, 
        version: 'v2_initial', 
        action: 'initial_page_load',
        source_url: window.location.href 
      })
    }).catch(err => console.log('Store failed:', err));
  }
  
  (async function() {
    console.log('\\n🚀 Starting INITIAL v2 token generation...');
    try {
      const token = await getV2Token();
      displayToken(token);
    } catch (error) {
      console.error('❌ Initial token failed:', error);
    }
  })();
})();
"""

# Blocker script to run after reload
BLOCKER_SCRIPT = """
(function() {
    console.log('🔧 Installing COMPLETE forceLowRecaptchaScore blocker...');

    const originalFetch = window.fetch;
    window.fetch = function(...args) {
        let [url, options = {}] = args;
        
        if (options.body && typeof options.body === 'string') {
            try {
                const body = JSON.parse(options.body);
                
                const deepClean = (obj) => {
                    if (!obj || typeof obj !== 'object') return obj;
                    
                    if (Array.isArray(obj)) {
                        return obj.map(item => deepClean(item));
                    }
                    
                    const cleaned = {};
                    for (const [key, value] of Object.entries(obj)) {
                        if (key === 'forceLowRecaptchaScore') {
                            console.log(`🚫 REMOVED forceLowRecaptchaScore from request to ${url}`);
                            continue;
                        }
                        cleaned[key] = deepClean(value);
                    }
                    return cleaned;
                };
                
                const cleanedBody = deepClean(body);
                options = { ...options, body: JSON.stringify(cleanedBody) };
                args[1] = options;
            } catch (e) {
                console.log('Error cleaning body:', e);
            }
        }
        
        return originalFetch.apply(this, args);
    };

    console.log('✅ Blocker installed with verification!');
})();
"""

# On-demand invisible script
ON_DEMAND_V2_SCRIPT = """
(function() {
  'use strict';
  
  const CONFIG = {
    SITE_KEY: '6Led_uYrAAAAAKjxDIF58fgFtX3t8loNAK85bW9I',
    TIMEOUT: 60000
  };
  
  console.log('🎯 ON-DEMAND v2 reCAPTCHA Token Generator');
  
  async function getV2Token() {
    const w = window.wrappedJSObject || window;
    
    if (!w.grecaptcha?.enterprise) {
      console.log('📦 Loading reCAPTCHA script...');
      await loadRecaptchaScript(w);
    }
    
    await waitForGrecaptcha(w);
    
    const g = w.grecaptcha?.enterprise;
    
    let settled = false;
    const done = (fn, arg) => {
      if (settled) return;
      settled = true;
      fn(arg);
    };
    
    return new Promise((resolve, reject) => {
      try {
        const el = w.document.createElement('div');
        el.style.cssText = 'position:fixed;left:-9999px;top:-9999px;width:1px;height:1px;';
        w.document.body.appendChild(el);
        
        const timer = w.setTimeout(() => {
          done(reject, 'V2_TIMEOUT');
        }, CONFIG.TIMEOUT);
        
        const wid = g.render(el, {
          sitekey: CONFIG.SITE_KEY,
          size: 'invisible',
          callback: (tok) => {
            console.log('✅ Token received');
            w.clearTimeout(timer);
            done(resolve, tok);
          },
          'error-callback': () => {
            console.log('❌ Widget error');
            w.clearTimeout(timer);
            done(reject, 'V2_ERROR');
          }
        });
        
        if (typeof g.execute === 'function') {
          g.execute(wid);
        }
        
      } catch (e) {
        done(reject, String(e));
      }
    });
  }
  
  async function loadRecaptchaScript(w) {
    return new Promise((resolve, reject) => {
      if (w.document.querySelector('script[src*="recaptcha/enterprise.js"]')) {
        resolve();
        return;
      }
      
      const script = w.document.createElement('script');
      script.src = 'https://www.google.com/recaptcha/enterprise.js?render=' + CONFIG.SITE_KEY;
      script.async = true;
      script.defer = true;
      script.onload = resolve;
      script.onerror = reject;
      w.document.head.appendChild(script);
    });
  }
  
  async function waitForGrecaptcha(w) {
    const startTime = Date.now();
    const maxWait = 30000;
    
    while (Date.now() - startTime < maxWait) {
      const g = w.grecaptcha?.enterprise;
      if (g && typeof g.render === 'function') {
        return true;
      }
      await new Promise(r => setTimeout(r, 100));
    }
    throw new Error('Timeout');
  }
  
  (async function() {
    try {
      const token = await getV2Token();
      console.log('\\n✅ TOKEN:', token);
      
      fetch('http://localhost:5000/api', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          token, 
          version: 'v2_ondemand', 
          action: 'manual_trigger',
          source_url: window.location.href 
        })
      }).then(r => r.json()).then(data => {
        console.log('📤 Token stored. Total:', data.total_count);
      }).catch(err => console.log('Store failed:', err));
      
      return token;
    } catch (error) {
      console.error('❌ Failed:', error);
    }
  })();
})();
"""

V2_SCRIPT = r"""
(() => {
    const SERVER_URL   = "http://localhost:5000/api";
    const V2_SITEKEY   = "6Ld7ePYrAAAAAB34ovoFoDau1fqCJ6IyOjFEQaMn";
    const FORCE_MODE   = "checkbox";
    const INV_MIN_INTERVAL = 80;
    const INV_MAX_INTERVAL = 100;
    const INV_RETRY        = 15;

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
        console.log(`\n[v2-${mode} #${v2Count}] Token generated (${token.length} chars)`);
        updateCount();
        if (panelCreated) updateStatus(`Token #${v2Count} captured! Sending...`);
        return fetch(SERVER_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token, version: "v2", action: mode === "invisible" ? "invisible_auto" : "checkbox_challenge", harvest_number: v2Count, source_url: window.location.href })
        }).then(r => r.json()).then(data => {
            console.log(`[v2-${mode} #${v2Count}] Stored. Server total: ${data.total_count}`);
            if (panelCreated) updateStatus(`Token #${v2Count} stored! Total: ${data.total_count}`);
        }).catch(err => { console.error(`[v2-${mode} #${v2Count}] Store failed:`, err); });
    }

    function harvestInvisible() {
        const g = window.grecaptcha?.enterprise;
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
        const timer = setTimeout(() => { if (!settled) { settled = true; el.remove(); handleInvisibleFailure(); } }, 60000);
        try {
            const wid = g.render(el, {
                sitekey: V2_SITEKEY,
                size: 'invisible',
                callback: (token) => {
                    if (settled) return; settled = true; clearTimeout(timer); el.remove();
                    sendToken(token, "invisible").then(() => {
                        const next = getRandomInterval(INV_MIN_INTERVAL, INV_MAX_INTERVAL);
                        currentTimeoutId = setTimeout(harvestInvisible, next * 1000);
                    });
                },
                'error-callback': () => { if (settled) return; settled = true; clearTimeout(timer); el.remove(); handleInvisibleFailure(); }
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
        if (panelCreated) return; panelCreated = true;
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
    function updateCount() { const el = document.getElementById('__v2_count'); if (el) el.textContent = `${v2Count} token${v2Count !== 1 ? 's' : ''}`; }

    function startCheckboxMode() { createPanel(); renderCheckbox(); }

    function renderCheckbox() {
        const g = window.grecaptcha?.enterprise;
        if (!g || typeof g.render !== 'function') { updateStatus('Waiting for grecaptcha.enterprise...'); setTimeout(renderCheckbox, 1000); return; }
        const panel = document.getElementById('__v2_harvest_panel');
        if (!panel) return;
        const oldContainer = document.getElementById('__v2_checkbox_container');
        if (oldContainer) oldContainer.remove();
        const container = document.createElement('div');
        container.id = '__v2_checkbox_container';
        container.style.cssText = 'display:flex;justify-content:center;';
        const closeBtn = panel.lastElementChild;
        panel.insertBefore(container, closeBtn);
        updateStatus('Click the checkbox below to harvest a v2 token');
        const timeout = setTimeout(() => { updateStatus('Widget expired. Rendering fresh...'); renderCheckbox(); }, 60000);
        try {
            g.render(container, {
                sitekey: V2_SITEKEY,
                callback: (token) => {
                    clearTimeout(timeout);
                    sendToken(token, "checkbox").then(() => { updateStatus(`Token #${v2Count} stored! New widget in 3s...`); setTimeout(renderCheckbox, 3000); });
                },
                'error-callback': () => { clearTimeout(timeout); updateStatus('Challenge failed. New widget in 5s...'); setTimeout(renderCheckbox, 5000); },
                'expired-callback': () => { clearTimeout(timeout); updateStatus('Token expired. New widget in 3s...'); setTimeout(renderCheckbox, 3000); },
                theme: document.documentElement.classList.contains('dark') ? 'dark' : 'light',
            });
        } catch (e) { clearTimeout(timeout); updateStatus(`Error: ${e.message}. Retry in 10s...`); setTimeout(renderCheckbox, 10000); }
    }

    window.__STOP_V2_HARVEST__ = () => {
        if (currentTimeoutId) { clearTimeout(currentTimeoutId); currentTimeoutId = null; }
        const panel = document.getElementById('__v2_harvest_panel');
        if (panel) panel.remove();
        panelCreated = false;
        console.log(`[v2] Stopped. Tokens: ${v2Count}`);
    };
    window.__V2_SWITCH_INVISIBLE__ = () => { window.__STOP_V2_HARVEST__(); currentMode = "invisible"; invisibleErrors = 0; harvestInvisible(); };
    window.__V2_SWITCH_CHECKBOX__ = () => { window.__STOP_V2_HARVEST__(); currentMode = "checkbox"; startCheckboxMode(); };

    console.log(`v2 Harvester started (mode: ${FORCE_MODE})`);
    if (FORCE_MODE === "checkbox") {
        currentMode = "checkbox";
        if (window.grecaptcha?.enterprise?.ready) { window.grecaptcha.enterprise.ready(() => startCheckboxMode()); } else { startCheckboxMode(); }
    } else {
        currentMode = "invisible";
        if (window.grecaptcha?.enterprise?.ready) { window.grecaptcha.enterprise.ready(() => harvestInvisible()); } else { harvestInvisible(); }
    }
})();
"""

V3_SCRIPT = r"""
(() => {
    const SERVER_URL    = "http://localhost:5000/api";
    const SITE_KEY      = "6Led_uYrAAAAAKjxDIF58fgFtX3t8loNAK85bW9I";
    const ACTION        = "chat_submit";
    const MIN_INTERVAL  = 15;
    const MAX_INTERVAL  = 18;

    let tokenCount = 0;
    let currentTimeoutId = null;

    function getRandomInterval() {
        const randomArray = new Uint32Array(1);
        crypto.getRandomValues(randomArray);
        const randomFloat = randomArray[0] / (0xFFFFFFFF + 1);
        return MIN_INTERVAL + (randomFloat * (MAX_INTERVAL - MIN_INTERVAL));
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
                        body: JSON.stringify({ token, version: "v3", action: ACTION, harvest_number: tokenCount, source_url: window.location.href })
                    }).then(res => res.json()).then(data => {
                        console.log(`[v3 #${tokenCount}] Stored. Server total: ${data.total_count}`);
                        window.__RECAPTCHA_TOKEN__ = token;
                        scheduleNext();
                    });
                }).catch(err => { console.error("[v3] Error:", err); scheduleNext(); });
        });
    }

    function scheduleNext() {
        const nextInterval = getRandomInterval();
        console.log(`[v3] Next harvest in ${nextInterval.toFixed(2)}s`);
        currentTimeoutId = setTimeout(harvest, nextInterval * 1000);
    }

    window.__STOP_HARVEST__ = () => {
        if (currentTimeoutId) { clearTimeout(currentTimeoutId); currentTimeoutId = null; }
        console.log("[v3] Stopped. Total captured:", tokenCount);
    };

    console.log(`v3 Auto-harvester started (random interval: ${MIN_INTERVAL}-${MAX_INTERVAL}s)`);
    harvest();
})();
"""

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

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {} };
"""

# ── Dashboard HTML ─────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>reCAPTCHA Harvester</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f0f1a; color: #e0e0e0; font-family: system-ui, -apple-system, sans-serif; padding: 24px; min-height: 100vh; }
  h1 { font-size: 20px; font-weight: 700; color: #fff; margin-bottom: 4px; }
  .subtitle { color: #6b7280; font-size: 13px; margin-bottom: 24px; }
  .stats { display: flex; gap: 16px; margin-bottom: 28px; }
  .stat { background: #1a1a2e; border: 1px solid #2d2d4e; border-radius: 10px; padding: 14px 20px; flex: 1; }
  .stat-label { font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; }
  .stat-value { font-size: 28px; font-weight: 700; color: #4ade80; margin-top: 4px; }
  .stat-value.blue { color: #60a5fa; }
  .stat-value.purple { color: #c084fc; }
  .windows { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 16px; }
  .window-card { background: #1a1a2e; border: 1px solid #2d2d4e; border-radius: 12px; padding: 18px; }
  .window-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
  .window-title { font-size: 15px; font-weight: 600; }
  .badge { font-size: 11px; padding: 3px 10px; border-radius: 20px; font-weight: 600; }
  .badge.loading  { background: #1c2a1c; color: #6b7280; border: 1px solid #374151; }
  .badge.ready    { background: #1c2a1c; color: #4ade80; border: 1px solid #166534; }
  .badge.idle     { background: #1c1c2a; color: #9ca3af; border: 1px solid #374151; }
  .badge.harvesting_v2 { background: #2a1c1c; color: #f87171; border: 1px solid #991b1b; }
  .badge.harvesting_v3 { background: #1c1c2a; color: #60a5fa; border: 1px solid #1d4ed8; }
  .btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
  .btn { padding: 7px 14px; border: none; border-radius: 7px; cursor: pointer; font-size: 12px; font-weight: 600; transition: opacity 0.15s, transform 0.1s; }
  .btn:hover { opacity: 0.85; transform: translateY(-1px); }
  .btn:active { transform: translateY(0); }
  .btn:disabled { opacity: 0.35; cursor: not-allowed; transform: none; }
  .btn.v2-start  { background: #dc2626; color: #fff; }
  .btn.v2-stop   { background: #374151; color: #f87171; }
  .btn.v3-start  { background: #1d4ed8; color: #fff; }
  .btn.v3-stop   { background: #374151; color: #60a5fa; }
  .btn.invisible-run { background: #8b5cf6; color: #fff; grid-column: span 2; }
  .window-info { font-size: 11px; color: #6b7280; margin-top: 10px; }
  .refresh-info { text-align: right; color: #374151; font-size: 11px; margin-top: 20px; }
  .toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #1a1a2e; border: 1px solid #4ade80; color: #4ade80; padding: 10px 20px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 9999; }
  .toast.show { opacity: 1; }
  .danger-row { margin-top: 24px; display: flex; justify-content: flex-end; }
  .btn-danger { padding: 8px 18px; border: 1px solid #7f1d1d; background: #1a0a0a; color: #f87171; border-radius: 7px; cursor: pointer; font-size: 12px; font-weight: 600; transition: background 0.15s; }
  .btn-danger:hover { background: #7f1d1d; color: #fff; }
</style>
</head>
<body>
<h1>reCAPTCHA Harvester</h1>
<p class="subtitle">Token harvesting dashboard &mdash; auto-refreshes every 3s</p>

<div class="stats">
  <div class="stat">
    <div class="stat-label">Total Tokens</div>
    <div class="stat-value" id="stat-total">0</div>
  </div>
  <div class="stat">
    <div class="stat-label">v2 Tokens</div>
    <div class="stat-value purple" id="stat-v2">0</div>
  </div>
  <div class="stat">
    <div class="stat-label">v3 Tokens</div>
    <div class="stat-value blue" id="stat-v3">0</div>
  </div>
  <div class="stat">
    <div class="stat-label">Windows Ready</div>
    <div class="stat-value" id="stat-ready">0</div>
  </div>
</div>

<div class="windows" id="windows-container">
  <p style="color:#6b7280;font-size:13px;">Loading windows...</p>
</div>

<div class="danger-row">
  <button class="btn-danger" onclick="deleteProfiles()">🗑 Delete All Profiles</button>
</div>
<div class="refresh-info" id="refresh-info">Last refresh: —</div>
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

async function apiCall(path, method='POST') {
  try {
    const r = await fetch(path, { method });
    const d = await r.json();
    return d;
  } catch(e) {
    showToast('Error: ' + e.message, '#f87171');
    return null;
  }
}

function renderWindows(windows) {
  const container = document.getElementById('windows-container');
  if (!windows.length) { container.innerHTML = '<p style="color:#6b7280;font-size:13px;">No windows yet...</p>'; return; }

  const label = _tabsMode ? 'Tab' : 'Window';
  let html = '';
  for (const w of windows) {
    const badgeClass = w.status || 'loading';
    const badgeText = (w.status || 'loading').replace(/_/g, ' ').toUpperCase();
    const isReady = w.status !== 'loading';
    html += `
    <div class="window-card">
      <div class="window-header">
        <span class="window-title">${label} ${w.id}</span>
        <span class="badge ${badgeClass}">${badgeText}</span>
      </div>
      <div class="btn-row">
        <button class="btn v2-start" onclick="v2Start(${w.id})" ${!isReady ? 'disabled' : ''}>V2 Start</button>
        <button class="btn v2-stop"  onclick="v2Stop(${w.id})"  ${!isReady ? 'disabled' : ''}>V2 Stop</button>
        <button class="btn v3-start" onclick="v3Start(${w.id})" ${!isReady ? 'disabled' : ''}>V3 Start</button>
        <button class="btn v3-stop"  onclick="v3Stop(${w.id})"  ${!isReady ? 'disabled' : ''}>V3 Stop</button>
      </div>
      <div class="btn-row" style="margin-top: 8px;">
        <button class="btn invisible-run" onclick="runInvisibleScript(${w.id})" ${!isReady ? 'disabled' : ''}>🎯 Run Invisible Script</button>
      </div>
      <div class="window-info">Profile: harvester_profiles/${label.toLowerCase()}_${w.id} &nbsp;|&nbsp; Tokens: ${w.token_count || 0}</div>
    </div>`;
  }
  container.innerHTML = html;
}

let _tabsMode = false;

async function refresh() {
  try {
    const [status, tokens] = await Promise.all([
      fetch('/status').then(r => r.json()),
      fetch('/api/tokens').then(r => r.json()),
    ]);
    const windows = status.windows || [];
    _tabsMode = !!status.tabs_mode;
    const allTokens = tokens.tokens || [];
    const v2 = allTokens.filter(t => t.version && t.version.includes('v2')).length;
    const v3 = allTokens.filter(t => t.version === 'v3').length;
    const ready = windows.filter(w => w.status !== 'loading').length;

    document.getElementById('stat-total').textContent = allTokens.length;
    document.getElementById('stat-v2').textContent = v2;
    document.getElementById('stat-v3').textContent = v3;
    document.getElementById('stat-ready').textContent = ready + '/' + windows.length;

    const countByWindow = {};
    for (const t of allTokens) { countByWindow[t.window_id] = (countByWindow[t.window_id] || 0) + 1; }
    for (const w of windows) { w.token_count = countByWindow[w.id] || 0; }

    renderWindows(windows);
    document.getElementById('refresh-info').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
  } catch(e) {}
}

async function v2Start(id) { const d = await apiCall(`/windows/${id}/v2/start`); if(d) showToast(`Window ${id}: V2 started`); await refresh(); }
async function v2Stop(id)  { const d = await apiCall(`/windows/${id}/v2/stop`);  if(d) showToast(`Window ${id}: V2 stopped`, '#f87171'); await refresh(); }
async function v3Start(id) { const d = await apiCall(`/windows/${id}/v3/start`); if(d) showToast(`Window ${id}: V3 started`, '#60a5fa'); await refresh(); }
async function v3Stop(id)  { const d = await apiCall(`/windows/${id}/v3/stop`);  if(d) showToast(`Window ${id}: V3 stopped`, '#6b7280'); await refresh(); }
async function runInvisibleScript(id) { const d = await apiCall(`/windows/${id}/invisible/run`); if(d) showToast(`Window ${id}: Invisible script triggered`, '#8b5cf6'); await refresh(); }

async function deleteProfiles() {
  if (!confirm('Delete ALL harvester_profiles? This cannot be undone.\\nBrowsers must be restarted after.')) return;
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
    windows = []
    for wid, w in _windows.items():
        windows.append({"id": wid, "status": w.get("status", "loading")})
    return {"windows": windows, "tabs_mode": TABS}


@app.delete("/profiles/delete")
async def delete_profiles():
    if not PROFILES_DIR.exists():
        return {"ok": True, "deleted": 0, "detail": "No profiles directory found"}
    deleted = 0
    errors = []
    for item in sorted(PROFILES_DIR.iterdir()):
        if item.is_dir():
            try:
                import shutil
                shutil.rmtree(item)
                deleted += 1
                print(f"[profiles] Deleted: {item}")
            except Exception as e:
                errors.append(str(e))
    if errors:
        return {"ok": False, "deleted": deleted, "detail": "; ".join(errors)}
    return {"ok": True, "deleted": deleted}


@app.post("/api")
async def store_token(request: Request):
    data = await request.json()
    async with _tokens_lock:
        data["window_id"] = data.get("window_id", -1)
        data["timestamp"] = time.time()
        _tokens.append(data)
        total = len(_tokens)
    return {"total_count": total, "ok": True}


@app.get("/api/tokens")
async def get_tokens():
    async with _tokens_lock:
        return {"tokens": list(_tokens), "total": len(_tokens)}


@app.get("/api/tokens/latest")
async def get_latest_tokens():
    async with _tokens_lock:
        latest: dict[str, dict] = {}
        for t in _tokens:
            v = str(t.get("version", "unknown"))
            latest[v] = t
    return {"latest": latest}


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
    page: Page = w["page"]
    try:
        await page.evaluate(V2_SCRIPT)
        w["status"] = "harvesting_v2"
        return {"ok": True, "status": "harvesting_v2"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/windows/{window_id}/v2/stop")
async def v2_stop(window_id: int):
    w = _windows.get(window_id)
    if not w:
        raise HTTPException(status_code=404, detail="Window not found")
    page: Page = w["page"]
    try:
        await page.evaluate("if (typeof window.__STOP_V2_HARVEST__ === 'function') window.__STOP_V2_HARVEST__();")
        w["status"] = "idle"
        return {"ok": True, "status": "idle"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/windows/{window_id}/v3/start")
async def v3_start(window_id: int):
    w = _windows.get(window_id)
    if not w:
        raise HTTPException(status_code=404, detail="Window not found")
    page: Page = w["page"]
    try:
        await page.evaluate(V3_SCRIPT)
        w["status"] = "harvesting_v3"
        return {"ok": True, "status": "harvesting_v3"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/windows/{window_id}/v3/stop")
async def v3_stop(window_id: int):
    w = _windows.get(window_id)
    if not w:
        raise HTTPException(status_code=404, detail="Window not found")
    page: Page = w["page"]
    try:
        await page.evaluate("if (typeof window.__STOP_HARVEST__ === 'function') window.__STOP_HARVEST__();")
        w["status"] = "idle"
        return {"ok": True, "status": "idle"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/windows/{window_id}/invisible/run")
async def invisible_run(window_id: int):
    w = _windows.get(window_id)
    if not w:
        raise HTTPException(status_code=404, detail="Window not found")
    page: Page = w["page"]
    try:
        await page.evaluate(ON_DEMAND_V2_SCRIPT)
        return {"ok": True, "message": "Invisible script triggered"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Mouse movement coroutine ──────────────────────────────────

async def mouse_mover(page: Page, window_id: int):
    """
    Continuously moves mouse in natural bezier-like curves.
    Never clicks. Runs forever until page/context closes.
    """
    # Get viewport size
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
    except Exception:
        vp = {"width": 1280, "height": 800}

    W, H = vp["width"], vp["height"]

    # Current position
    cx, cy = W // 2, H // 2

    def rand_point():
        # Keep away from edges to avoid accidental link hovers
        x = random.randint(80, W - 80)
        y = random.randint(80, H - 80)
        return x, y

    def bezier_points(x0, y0, x1, y1, steps=12):
        """Simple quadratic bezier with random control point."""
        cx_ = (x0 + x1) // 2 + random.randint(-80, 80)
        cy_ = (y0 + y1) // 2 + random.randint(-80, 80)
        pts = []
        for i in range(1, steps + 1):
            t = i / steps
            bx = (1-t)**2 * x0 + 2*(1-t)*t * cx_ + t**2 * x1
            by = (1-t)**2 * y0 + 2*(1-t)*t * cy_ + t**2 * y1
            pts.append((int(bx), int(by)))
        return pts

    while True:
        try:
            tx, ty = rand_point()
            pts = bezier_points(cx, cy, tx, ty, steps=random.randint(8, 16))
            for px, py in pts:
                await page.mouse.move(px, py)
                await asyncio.sleep(random.uniform(0.03, 0.12))
            cx, cy = tx, ty
            # Random pause between movements — human-like idle time
            await asyncio.sleep(random.uniform(0.8, 3.5))
        except Exception:
            # Page closed or context destroyed — exit gracefully
            break


# ── Cookie injection helper ────────────────────────────────────

async def inject_cookies(context: BrowserContext, window_id: int) -> None:
    """
    COOKIES mode — performs three operations on the arena.ai cookie jar
    for the given context:

      1. Find the cookie named "auth-prod-v1" and DELETE it (it will be
         replaced under the new name in step 2).

      2. Add / overwrite "arena-auth-prod-v1.0" with the value from COOKIE_V1,
         preserving the domain / path / security attributes of the original
         cookie when found, otherwise using safe defaults for arena.ai.

      3. Add "arena-auth-prod-v1.1" with the value from COOKIE_V2 using the
         same domain / path / security attributes.

    This runs once per context/window, immediately before the blocker script.
    """
    label = "tab" if TABS else "window"
    print(f"[{label} {window_id}] 🍪 Injecting cookies (COOKIES=True)...")

    try:
        # ── 1. Find and remove the old "auth-prod-v1" cookie ──────────────
        all_cookies = await context.cookies()
        old_cookie = next(
            (c for c in all_cookies if c.get("name") == "arena-auth-prod-v1"),
            None,
        )

        if old_cookie:
            await context.clear_cookies(name="arena-auth-prod-v1")
            print(f"[{label} {window_id}]   ✓ Removed auth-prod-v1")
        else:
            print(f"[{label} {window_id}]   ℹ auth-prod-v1 not found — will create with defaults")

        # ── Derive base attributes from old cookie or use safe defaults ───
        base: dict = {
            "domain":   old_cookie.get("domain",   ".arena.ai") if old_cookie else ".arena.ai",
            "path":     old_cookie.get("path",      "/")         if old_cookie else "/",
            "secure":   old_cookie.get("secure",    True)        if old_cookie else True,
            "httpOnly": old_cookie.get("httpOnly",  True)        if old_cookie else True,
            "sameSite": old_cookie.get("sameSite",  "Lax")       if old_cookie else "Lax",
        }
        # Only include expires if the original had one (avoid session→persistent
        # downgrade; also Playwright rejects expires=-1).
        if old_cookie and old_cookie.get("expires", -1) > 0:
            base["expires"] = old_cookie["expires"]

        # ── 2. Set arena-auth-prod-v1.0 = COOKIE_V1 ──────────────────────
        await context.add_cookies([{**base, "name": "arena-auth-prod-v1.0", "value": COOKIE_V1}])
        print(f"[{label} {window_id}]   ✓ Set arena-auth-prod-v1.0")

        # ── 3. Add arena-auth-prod-v1.1 = COOKIE_V2 ──────────────────────
        await context.add_cookies([{**base, "name": "arena-auth-prod-v1.1", "value": COOKIE_V2}])
        print(f"[{label} {window_id}]   ✓ Added arena-auth-prod-v1.1")

    except Exception as e:
        print(f"[{label} {window_id}] ⚠ Cookie injection error: {e}")


# ── Browser launch ─────────────────────────────────────────────

def _get_extension_args() -> list[str]:
    """
    Build --load-extension / --disable-extensions-except args from EXTENSIONS_DIR.

    EXTENSIONS_DIR must point to the Extensions folder of your browser profile,
    e.g. /root/.config/BraveSoftware/Brave-Browser/Default/Extensions

    Structure expected:
      Extensions/
        <ext-id>/
          <version>_0/
            manifest.json   ← this is the path passed to --load-extension
    """
    if not CUSTOM or not EXTENSIONS:
        return []

    # ── Validate EXTENSIONS_DIR ───────────────────────────────────
    if not EXTENSIONS_DIR or not EXTENSIONS_DIR.strip():
        raise RuntimeError(
            "EXTENSIONS=True but EXTENSIONS_DIR is empty.\n"
            "Set EXTENSIONS_DIR to your browser's Extensions folder path.\n"
            "See the config comments at the top of this file for examples."
        )

    base = Path(EXTENSIONS_DIR.strip())
    if not base.exists():
        raise RuntimeError(
            f"EXTENSIONS_DIR does not exist: {base}\n"
            "Check the path and make sure the browser has been run at least once.\n"
            "See the config comments at the top of this file for correct paths."
        )
    if not base.is_dir():
        raise RuntimeError(f"EXTENSIONS_DIR is not a directory: {base}")

    # ── Scan for versioned extension dirs ────────────────────────
    ext_dirs: list[str] = []
    print(f"[extensions] Scanning: {base}")

    for ext_id_dir in sorted(base.iterdir()):
        if not ext_id_dir.is_dir():
            continue
        # Each sub-dir is a versioned folder (e.g. "1.2.3_0") containing manifest.json
        for version_dir in sorted(ext_id_dir.iterdir(), reverse=True):
            if version_dir.is_dir() and (version_dir / "manifest.json").exists():
                ext_dirs.append(str(version_dir))
                print(f"[extensions]   + {ext_id_dir.name}/{version_dir.name}")
                break  # only latest version per extension ID

    if not ext_dirs:
        raise RuntimeError(
            f"No extensions found in: {base}\n"
            "The folder exists but contains no valid extension subdirectories.\n"
            "Make sure the path points to the Extensions folder (not Default or User Data)."
        )

    joined = ",".join(ext_dirs)
    return [
        "--enable-extensions",
        f"--load-extension={joined}",
        f"--disable-extensions-except={joined}",
    ]


_BASE_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--disable-features=TranslateUI",
    "--window-size=1280,800",
] + ([] if os.name == "nt" else [
    # Linux/macOS only — Chromium sandbox requires kernel features unavailable
    # in PRoot/Docker/WSL. Harmless to omit on Windows (sandbox works natively).
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
])

# Shared browser instance used in TABS mode (one persistent context, N pages)
_shared_browser = None   # sentinel bool, True when context is ready
_shared_context = None   # the single BrowserContext all tabs share
# Per-tab cookie snapshots: {tab_id: [cookie_dicts]}
_tab_cookie_store: dict[int, list] = {}


def _resolve_profile_dir(slot: str) -> Path:
    """
    Return the user_data_dir to use for a given slot (e.g. 'window_0', 'tab_0').
    When CUS_PROFILE=True every slot uses PROFILE_PATH directly.
    When CUS_PROFILE=False each slot gets its own isolated dir under PROFILES_DIR.
    """
    if CUS_PROFILE:
        return Path(PROFILE_PATH.strip())
    d = PROFILES_DIR / slot
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _launch_persistent(playwright, window_id: int) -> tuple[BrowserContext, Page]:
    """WINDOWS mode — each window is its own persistent browser context with isolated profile."""
    profile_dir = _resolve_profile_dir(f"window_{window_id}")
    profile_dir.mkdir(parents=True, exist_ok=True)

    args = _BASE_ARGS + [
        f"--window-position={100 + window_id * 40},{50 + window_id * 40}",
    ] + _get_extension_args()

    launch_kwargs = dict(
        user_data_dir=str(profile_dir),
        headless=False,
        args=args,
    )
    if CUSTOM and PATH:
        launch_kwargs["executable_path"] = PATH

    context: BrowserContext = await playwright.chromium.launch_persistent_context(**launch_kwargs)
    await context.add_init_script(STEALTH_SCRIPT)
    page: Page = await context.new_page()
    await page.set_viewport_size({"width": 1280, "height": 800})
    return context, page


async def _launch_tab(playwright, tab_id: int) -> tuple[BrowserContext, Page]:
    """
    TABS mode — all tabs live inside ONE browser window (one persistent context),
    but each tab is a separate Page. Isolation is achieved by:

    1. Each tab gets its own on-disk profile dir used as the persistent context's
       user_data_dir — BUT we only launch the persistent context once (for tab 0)
       and reuse it for all other tabs as new pages within the same context.

    Why not separate contexts per tab?
    On Linux, each BrowserContext.new_page() that comes from browser.new_context()
    opens a NEW OS window — defeating the purpose. Pages created via
    context.new_page() on a PERSISTENT context share one OS window and appear
    as real tabs.

    Cookie/storage isolation between tabs is handled by:
    - Clearing all cookies + localStorage for the tab's "slot" before navigating
    - Saving/restoring per-tab cookie snapshots in _tab_cookie_store
    """
    global _shared_browser, _shared_context

    if _shared_browser is None:
        profile_dir = _resolve_profile_dir("tab_0")
        profile_dir.mkdir(parents=True, exist_ok=True)

        args = _BASE_ARGS + _get_extension_args()
        launch_kwargs = dict(
            user_data_dir=str(profile_dir),
            headless=False,
            args=args,
        )
        if CUSTOM and PATH:
            launch_kwargs["executable_path"] = PATH

        _shared_context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
        await _shared_context.add_init_script(STEALTH_SCRIPT)
        _shared_browser = True  # sentinel — means context is ready

    # All tabs are pages inside the same persistent context = same OS window
    page: Page = await _shared_context.new_page()
    await page.set_viewport_size({"width": 1280, "height": 800})

    # Before navigating, wipe any cookies/storage this page slot might inherit
    # so each tab starts clean (gets its own fresh session from arena.ai)
    try:
        await _shared_context.clear_cookies()
    except Exception:
        pass

    # Restore this tab's previously saved cookies if any
    state_file = PROFILES_DIR / f"tab_{tab_id}" / "cookies.json"
    if state_file.exists():
        try:
            saved = json.loads(state_file.read_text())
            if saved:
                await _shared_context.add_cookies(saved)
        except Exception:
            pass

    return _shared_context, page


async def _save_tab_cookies(tab_id: int) -> None:
    """Snapshot current browser cookies into per-tab store and persist to disk."""
    if _shared_context is None:
        return
    try:
        cookies = await _shared_context.cookies()
        _tab_cookie_store[tab_id] = cookies
        state_dir = PROFILES_DIR / f"tab_{tab_id}"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "cookies.json").write_text(json.dumps(cookies))
    except Exception as e:
        print(f"[tab {tab_id}] Cookie save failed: {e}")


async def _restore_tab_cookies(tab_id: int) -> None:
    """Clear browser cookies and restore the snapshot for tab_id."""
    if _shared_context is None:
        return
    try:
        await _shared_context.clear_cookies()
        cookies = _tab_cookie_store.get(tab_id)
        if cookies:
            await _shared_context.add_cookies(cookies)
        else:
            # Try loading from disk
            state_file = PROFILES_DIR / f"tab_{tab_id}" / "cookies.json"
            if state_file.exists():
                saved = json.loads(state_file.read_text())
                if saved:
                    await _shared_context.add_cookies(saved)
    except Exception as e:
        print(f"[tab {tab_id}] Cookie restore failed: {e}")


async def setup_window(playwright, window_id: int):
    """Launch browser/tab, navigate to arena.ai, run initial script, reload, then mark ready."""
    label = "tab" if TABS else "window"

    if TABS:
        context, page = await _launch_tab(playwright, window_id)
    else:
        context, page = await _launch_persistent(playwright, window_id)

    _windows[window_id] = {
        "id": window_id,
        "status": "loading",
        "page": page,
        "context": context,
    }

    print(f"[{label} {window_id}] Navigating to arena.ai...")
    try:
        await page.goto("https://arena.ai", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"[{label} {window_id}] Navigation warning: {e}")

    # Wait a bit for page to stabilize
    await asyncio.sleep(2)

    # Run initial v2 script
    print(f"[{label} {window_id}] Running initial v2 script...")
    try:
        await page.evaluate(INITIAL_V2_SCRIPT)
        await asyncio.sleep(1)  # Give it time to execute
    except Exception as e:
        print(f"[{label} {window_id}] Initial script error: {e}")

    # Reload the page
    print(f"[{label} {window_id}] Reloading page...")
    try:
        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(2)  # Wait after reload
    except Exception as e:
        print(f"[{label} {window_id}] Reload error: {e}")

    # ── Cookie injection (COOKIES=True only) ──────────────────────
    # Runs after the page has loaded its own cookies but before the blocker
    # script, so arena.ai will see the injected credentials on subsequent
    # requests without any script interference.
    if COOKIES:
        await inject_cookies(context, window_id)

    # Persist initial cookies/state for tabs
    if TABS:
        await _save_tab_cookies(window_id)

    # Signal server ready
    print(f"[{label} {window_id}] Marking as ready...")
    try:
        await page.evaluate(READY_SIGNAL_SCRIPT, window_id)
    except Exception as e:
        print(f"[{label} {window_id}] Ready signal JS failed ({e}), marking directly")
        _windows[window_id]["status"] = "ready"

    # Wait 1 second then run blocker script
    await asyncio.sleep(1)
    print(f"[{label} {window_id}] Running blocker script...")
    try:
        await page.evaluate(BLOCKER_SCRIPT)
    except Exception as e:
        print(f"[{label} {window_id}] Blocker script error: {e}")

    print(f"[{label} {window_id}] Ready. Starting mouse mover.")
    asyncio.create_task(mouse_mover(page, window_id))


# ── Main entry ─────────────────────────────────────────────────

async def tab_switcher():
    """
    TABS mode only — cycles through all tabs every 15 seconds.

    Before switching TO a tab:
      1. Save current tab's cookies to its snapshot
      2. Restore the target tab's cookies into the shared context
      3. Bring the tab's page to front

    This gives each tab its own isolated cookie jar even though they share
    one BrowserContext. The tab that is currently in front always has its
    own cookies loaded — reCAPTCHA tokens are minted with those cookies active.
    """
    current_tab_id: Optional[int] = None

    while True:
        await asyncio.sleep(15)
        ids = sorted(_windows.keys())
        if not ids:
            continue

        for wid in ids:
            w = _windows.get(wid)
            if not w:
                continue
            page: Page = w.get("page")
            if page is None:
                continue

            try:
                # Save outgoing tab's cookies
                if current_tab_id is not None and current_tab_id != wid:
                    await _save_tab_cookies(current_tab_id)
                    # Restore incoming tab's cookies
                    await _restore_tab_cookies(wid)

                await page.bring_to_front()
                current_tab_id = wid
                await asyncio.sleep(0.15)
            except Exception:
                pass


async def run_browsers(server_ready_event: asyncio.Event):
    await server_ready_event.wait()
    await asyncio.sleep(0.5)

    PROFILES_DIR.mkdir(exist_ok=True)
    async with async_playwright() as pw:
        for i in range(N):
            await setup_window(pw, i)
            await asyncio.sleep(0.8)

        label = "tab(s) in isolated contexts" if TABS else "window(s)"
        print(f"\n✅ {N} {label} launched. Dashboard: http://localhost:{SERVER_PORT}")

        if TABS:
            asyncio.create_task(tab_switcher())
            print("   Tab switcher active (cycles every 15s to prevent throttling)")

        while True:
            await asyncio.sleep(10)


class _ServerWithReadyEvent(uvicorn.Server):
    """Subclass that sets an asyncio.Event when the server starts accepting."""

    def __init__(self, config, ready_event: asyncio.Event):
        super().__init__(config)
        self._ready_event = ready_event

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        self._ready_event.set()


async def main():
    print("=" * 50)
    print(f"  reCAPTCHA Harvester")
    print(f"  Windows/Tabs: {N}")
    print(f"  Custom      : {CUSTOM}{(' → ' + PATH) if CUSTOM else ''}")
    print(f"  Extensions  : {EXTENSIONS}")
    print(f"  Cookies     : {COOKIES}")
    print(f"  Dashboard   : http://localhost:{SERVER_PORT}")
    print("=" * 50)

    server_ready = asyncio.Event()
    config = uvicorn.Config(app, host="0.0.0.0", port=SERVER_PORT, log_level="warning")
    server = _ServerWithReadyEvent(config, server_ready)

    await asyncio.gather(
        server.serve(),
        run_browsers(server_ready),
    )


if __name__ == "__main__":
    asyncio.run(main())
