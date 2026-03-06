# Arena reCAPTCHA Harvester — Extension
# ORGINAL REPO - https://github.com/izaart95-jpg/Captcha-Extension
## Can be tuned easily for any website with recaptcha v3 and v2
### Note v2 captcha isnt standalone supported and recommended use rektcaptcha extesnion along
### with this etension to get v2 tokens
## Architecture

```
Your Browser (arena.ai tab)
        │
        │  chrome.scripting.executeScript
        ▼
   content.js  ←──── manifest loads on arena.ai
        │
        │  installs fetch blocker in page world
        │
   background.js  (service worker)
        │
        │  injects v2/v3 harvester JS into page
        │  manages TUNING / HARD_TUNING reload cycles
        │  handles cookie wipe + restore (HARD_TUNING)
        │
        │  POST /api
        ▼
   server.py  (local FastAPI)
        │
        ▼
   tokens.json  ──→  modula.py / main.py
```

## Setup

### 1. Start the Python server
```bash
pip install fastapi uvicorn
python captcha_server.py
```
Dashboard: http://localhost:5000

### 2. Install the extension
1. Open your browser → `chrome://extensions` (or `brave://extensions`)
2. Enable **Developer mode** (top right toggle)
3. Click **Load unpacked**
4. Select the `extension/` folder
5. The 🎯 icon appears in your toolbar

### 3. Harvest
1. Open [https://arena.ai](https://arena.ai) (log in if needed)
2. Click the 🎯 toolbar icon
3. Your arena.ai tab appears in the popup
4. Click **V2 Start** or **V3 Start**

## Features

### TUNING
When enabled: after each token is harvested, the tab reloads automatically and the harvester re-injects itself. Continuous loop until you click Stop.

### HARD TUNING (requires TUNING=true)
After each token:
1. Saves 4 essential cookies to RAM: `arena-auth-prod-v1.0`, `arena-auth-prod-v1.1`, `__cf_bm`, `cf_clearance`
2. Deletes ALL other arena.ai cookies (clears session fingerprint data)
3. Reloads the tab (fresh cookie jar)
4. Restores the 4 saved cookies so you stay logged in
5. Re-injects the harvester

Effect: each reCAPTCHA request originates from a "fresh" browser session — no stale reCAPTCHA history, fresh fingerprint, higher token scores.

### FIVE_GAIN
When enabled: after each reload cycle, navigates to `arena.ai/c/<eval_id>` instead of `arena.ai`. Enter your eval/conversation ID in Settings.

## Limitations vs Playwright version

| Feature | Playwright | Extension |
|---------|-----------|-----------|
| Multiple browser windows | ✅ | Use multiple tabs |
| Profile directory wipe (HARD_TUNING) | Full disk wipe | Cookie wipe (equivalent for fingerprint) |
| Auto-login | ✅ | Manual (log in once, stays logged in) |
| Mouse mover (anti-bot) | ✅ | Not needed (real browser) |
| Stealth mode | Injected | Real browser = already stealthy |
| Extensions (RektCaptcha) | ✅ | Install normally in browser |

## Files

| File | Purpose |
|------|---------|
| `manifest.json` | Extension config |
| `background.js` | Service worker — all logic, token routing, TUNING/HARD_TUNING |
| `content.js` | Injected into arena.ai pages — installs fetch blocker |
| `popup.html/js` | Extension popup UI — dashboard |
| `captcha_server.py` | Local FastAPI server — token storage |
