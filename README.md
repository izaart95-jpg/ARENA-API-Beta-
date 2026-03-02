# ARENA-API — Beta

> **Status:** Beta · ~80% complete — CAPTCHA bypass remains outstanding (no viable fast/free solution exists for production use).

---

## Quickstart - Install Dependencies
```bash
git clone https://github.com/izaart95-jpg/ARENA-API-Beta.git
cd ARENA-API-Beta
pip install -r requirements.txt
playwright install chromium
```

---

## Configuration

Open `arena_token.py` and edit the configuration block at the top of the file.

### Flags

| Flag | Description | Default |
|------|-------------|---------|
| `CUSTOM` | Use a custom browser executable specified by `PATH` | `True` |
| `N` | Number of browser windows or tabs to open | `1` |
| `EXTENSIONS` | Load extensions from `EXTENSIONS_DIR` | `True` |
| `CUS_PROFILE` | Use a custom browser profile from `PROFILE_PATH` | `False` |
| `TABS` | `False` = N separate windows · `True` = N tabs in one window | `False` |
| `AUTO_LOGIN` | `True` = automated login and cookie fetching · `False` = manual login | `True` |
| `COOKIES` | Inject stored auth cookies into each browser context | `False` |

> ⚠️ **Compatibility note:** `AUTO_LOGIN=True` and `COOKIES=True` are mutually exclusive. An error is raised at startup if both are enabled simultaneously.

### Parameters

| Parameter | Description |
|-----------|-------------|
| `PATH` | Browser executable path — required when `CUSTOM=True` |
| `EXTENSIONS_DIR` | Extensions folder path — required when `EXTENSIONS=True` |
| `PROFILE_PATH` | Browser profile path — required when `CUS_PROFILE=True` |
| `COOKIE_V1` | Value for `arena-auth-prod-v1.0` — required when `COOKIES=True` |
| `COOKIE_V2` | Value for `arena-auth-prod-v1.1` — required when `COOKIES=True` |

---

## Parameter Reference

### `PATH` — Browser Executable

Navigate to `chrome://version` in your browser and copy the **Executable Path** value.

> **Only for Windows users:** Always use a raw string prefix `r"..."` to avoid backslash escape issues.
> - ✅ PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
> - ❌ PATH = "C:\Program Files\Google\Chrome\Application\chrome.exe"
> - ✅ EXTENSIONS_DIR = r"C:\Users\USERNAME\AppData\Local\BraveSoftware\Brave-Browser\User Data\Default\Extension"
> - ❌ EXTENSIONS_DIR = "C:\Users\USERNAME\AppData\Local\BraveSoftware\Brave-Browser\User Data\Default\Extension"

**Default paths by platform:**

| Platform | Browser | Path |
|----------|---------|------|
| Linux | Brave | `/usr/bin/brave-browser` |
| Linux | Chrome | `/usr/bin/google-chrome` |
| Linux | Chromium | `/usr/bin/chromium-browser` |
| Windows | Brave | `r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"` |
| Windows | Chrome | `r"C:\Program Files\Google\Chrome\Application\chrome.exe"` |
| Windows | Edge | `r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"` |
| macOS | Brave | `/Applications/Brave Browser.app/Contents/MacOS/Brave Browser` |
| macOS | Chrome | `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` |
| macOS | Chromium | `/Applications/Chromium.app/Contents/MacOS/Chromium` |

---

### `EXTENSIONS_DIR` — Extensions Directory

Required when `EXTENSIONS=True`. To locate it:

1. Open your browser and navigate to `chrome://version`
2. Find the **Profile Path** value
3. Append `/Extensions` to that path

> At least one extension must be installed for the `Extensions` directory to exist.

**Default paths by platform:**

| Platform | Browser | Path |
|----------|---------|------|
| Linux | Brave | `/root/.config/BraveSoftware/Brave-Browser/Default/Extensions` |
| Linux | Chrome | `/home/USERNAME/.config/google-chrome/Default/Extensions` |
| Linux | Chromium | `/home/USERNAME/.config/chromium/Default/Extensions` |
| Windows | Brave | `r"C:\Users\USERNAME\AppData\Local\BraveSoftware\Brave-Browser\User Data\Default\Extensions"` |
| Windows | Chrome | `r"C:\Users\USERNAME\AppData\Local\Google\Chrome\User Data\Default\Extensions"` |
| Windows | Edge | `r"C:\Users\USERNAME\AppData\Local\Microsoft\Edge\User Data\Default\Extensions"` |
| macOS | Brave | `/Users/USERNAME/Library/Application Support/BraveSoftware/Brave-Browser/Default/Extensions` |
| macOS | Chrome | `/Users/USERNAME/Library/Application Support/Google/Chrome/Default/Extensions` |
| macOS | Chromium | `/Users/USERNAME/Library/Application Support/Chromium/Default/Extensions` |

---

### `PROFILE_PATH` — Browser Profile Directory

Required when `CUS_PROFILE=True`. All contexts will use this directory as `user_data_dir` instead of the auto-generated `harvester_profiles/` directories.

**Default paths by platform:**

| Platform | Browser | Path |
|----------|---------|------|
| Linux | Brave | `/root/.config/BraveSoftware/Brave-Browser` |
| Linux | Chrome | `/home/USERNAME/.config/google-chrome` |
| Windows | Brave | `r"C:\Users\USERNAME\AppData\Local\BraveSoftware\Brave-Browser\User Data"` |
| Windows | Chrome | `r"C:\Users\USERNAME\AppData\Local\Google\Chrome\User Data"` |
| macOS | Brave | `/Users/USERNAME/Library/Application Support/BraveSoftware/Brave-Browser` |
| macOS | Chrome | `/Users/USERNAME/Library/Application Support/Google/Chrome` |

---

### `COOKIE_V1` / `COOKIE_V2` — Auth Cookies

Required when `COOKIES=True`. Retrieve these values from your browser's DevTools under the **Application → Cookies** tab.

> `AUTO_LOGIN=True` is the recommended alternative — it handles authentication and cookie fetching automatically.

---

## Usage

📺 [Watch the usage walkthrough on YouTube](https://www.youtube.com/watch?v=QFHfF0bMoi0)

## Preview & Features

📺 [Watch the feature preview on YouTube](https://www.youtube.com/watch?v=pmgOZFKTvdc)