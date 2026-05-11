"""
bot.py — CryptBot: login, search, 25%-zoom click, solo march.

Selector constants near the top of this file are the first thing you
should calibrate against the live game HTML.  Open DevTools (F12) on
the game page and locate the real class names / data-attributes for
each UI element listed in SELECTORS.
"""

import io
import json
import time
import logging
import math
from pathlib import Path
import numpy as np
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

logger = logging.getLogger(__name__)

GAME_URL = "https://totalbattle.com"

# ---------------------------------------------------------------------------
# CSS selectors — only used for the HTML login page (pre-canvas)
# ---------------------------------------------------------------------------
SELECTORS = {
    "login_email":    '#login input[name="email"]',
    "login_password": '#login input[name="password"]',
    "login_submit":   '#login [data-handler="login_form_handler"]',
    "canvas":         "canvas",
}

# ---------------------------------------------------------------------------
# Canvas pixel coordinates for all in-game UI interactions.
# The game runs in a Unity WebGL canvas (#unityCanvas) so every click
# must be expressed as (x, y) relative to the canvas element.
#
# To calibrate: open DevTools Console and paste:
#   document.getElementById('unityCanvas').addEventListener('click', e => {
#     const r = e.target.getBoundingClientRect();
#     console.log(`x=${Math.round(e.clientX-r.left)}, y=${Math.round(e.clientY-r.top)}`);
#   });
# then click the element you want to locate.
# ---------------------------------------------------------------------------
CANVAS_COORDS = {
    # HUD button that opens the Watchtower window
    "watchtower_btn":     (698, 815),

    # Left sidebar inside the Watchtower window
    "crypts_and_arenas":  (715, 397),

    # Quality filter tab buttons (top of the Crypts panel).
    # *** These are estimated — calibrate by hovering over each tab ***
    "tab_common":   (1134, 363),
    "tab_rare":     (1276, 361),
    "tab_epic":     (1468, 361),
    "tab_arenas":   (1613, 361),
    "tab_others":   (1193, 406),

    # Level range slider — measured with DevTools undocked.
    # Drag start: current handle positions. Drag end: desired target positions.
    "slider_track_left":   (867, 339),   # left handle current position
    "slider_track_right":  (1276, 341),   # right handle current position
    "slider_left_target":  (877, 340),   # where to drag left handle to
    "slider_right_target": (877, 340),   # where to drag right handle to

    # "Go" button next to the SECOND result in the filtered list
    "second_result_go":    (1190, 523),

    # March speedup controls in the top HUD bar
    "march_speedup_btn":   (1210, 42),    # "Speed up" button beside March (Carter)
    # "Use" button for the 50% speedup (first entry in the Speedups popup)
    "speedup_use_btn":     (1156, 390),

    
    "crypt_location": (1301, 611),

    # # "Explore" button in the crypt detail popup that appears on the map
    # after pressing Escape from the purchase/info overlay
    "crypt_popup_explore": (1537, 939),
}

# Ordered list of all quality tabs — order must match left-to-right layout
ALL_QUALITY_TABS = ["Common", "Rare", "Epic", "Arenas", "Others"]


# ---------------------------------------------------------------------------
# Colour check helper
# ---------------------------------------------------------------------------

def _rgb_distance(c1: tuple, c2: tuple) -> float:
    """Euclidean distance in RGB space."""
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(c1, c2)))


def sample_center_color(page, settings: dict) -> tuple | None:
    """
    Read the average RGB of a small square centred on the screen.

    Uses the game's own WebGL canvas via JS so no screenshot parsing is needed.
    Returns (r, g, b) integers, or None if the canvas is unavailable.

    TIP: Call this manually once while the map is centred on a known Crypt at
    25% zoom to calibrate the 'crypt_colors' list in config.py.
    """
    cx   = settings["screen_center_x"]
    cy   = settings["screen_center_y"]
    half = settings["color_sample_size"] // 2

    color = page.evaluate(f"""() => {{
        const canvas = document.querySelector('canvas');
        if (!canvas) return null;
        // Try 2d context; WebGL canvases expose a 2d readback after a draw call
        let ctx = canvas.getContext('2d');
        if (!ctx) {{
            // Fallback: WebGL readPixels  (works for most game engines)
            const gl = canvas.getContext('webgl') || canvas.getContext('webgl2');
            if (!gl) return null;
            const buf = new Uint8Array(4);
            gl.readPixels({cx}, {cy}, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, buf);
            return [buf[0], buf[1], buf[2]];
        }}
        const d  = ctx.getImageData({cx - half}, {cy - half},
                                    {settings['color_sample_size']},
                                    {settings['color_sample_size']}).data;
        let r = 0, g = 0, b = 0;
        const px = d.length / 4;
        for (let i = 0; i < d.length; i += 4) {{
            r += d[i]; g += d[i + 1]; b += d[i + 2];
        }}
        return [Math.round(r / px), Math.round(g / px), Math.round(b / px)];
    }}""")
    return tuple(color) if color else None


def check_crypt_visible(page, settings: dict) -> bool:
    """Return True if the centre-screen pixel cluster matches a known Crypt colour."""
    color = sample_center_color(page, settings)
    if color is None:
        return False
    for target in settings["crypt_colors"]:
        if _rgb_distance(color, target) <= settings["color_tolerance"]:
            return True
    return False


# ---------------------------------------------------------------------------
# March-slot check
# ---------------------------------------------------------------------------

def is_captain_busy(page) -> bool:
    """True when every march slot in the HUD is occupied."""
    try:
        return page.query_selector(SELECTORS["march_slot_busy"]) is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CryptBot
# ---------------------------------------------------------------------------

class CryptBot:
    """Manages one browser profile and runs the continuous crypt-farming loop."""

    _ocr_reader = None  # shared across all instances; loaded once

    def __init__(self, account: dict, settings: dict):
        self.account     = account
        self.settings    = settings
        self.crypt_count = 0
        self.page        = None
        self.context     = None
        self._pw         = None   # playwright handle
        self.region_overrides = self._load_region_overrides()

    def _load_region_overrides(self) -> dict[str, tuple[int, int, int, int]]:
        """
        Load optional OCR region overrides from a JSON file.

                Supports both legacy and normalized formats.

                Legacy format:
                    {"watchtower_crypts": [x, y, w, h], ...}

                Normalized format:
                    {
                        "_meta": {"format": "normalized-v1", "viewport": [w, h]},
                        "watchtower_crypts": {"px": [x, y, w, h], "norm": [nx, ny, nw, nh]},
                        ...
                    }
        """
        path = Path(self.settings.get("region_overrides_file", "manual_regions.json"))
        if not path.exists():
            return {}

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[%s] Could not parse region override file '%s': %s",
                           self.account["name"], path, exc)
            return {}

        out: dict[str, object] = {}
        for key, value in payload.items():
            if key.startswith("_"):
                continue

            # Legacy [x, y, w, h]
            if isinstance(value, list) and len(value) == 4:
                try:
                    x, y, w, h = [int(v) for v in value]
                except Exception:
                    continue
                if w > 0 and h > 0:
                    out[key] = (x, y, w, h)
                continue

            # New dict format with optional px and norm
            if isinstance(value, dict):
                entry: dict[str, object] = {}

                px = value.get("px")
                if isinstance(px, list) and len(px) == 4:
                    try:
                        x, y, w, h = [int(v) for v in px]
                        if w > 0 and h > 0:
                            entry["px"] = (x, y, w, h)
                    except Exception:
                        pass

                norm = value.get("norm")
                if isinstance(norm, list) and len(norm) == 4:
                    try:
                        nx, ny, nw, nh = [float(v) for v in norm]
                        if nw > 0 and nh > 0:
                            entry["norm"] = (nx, ny, nw, nh)
                    except Exception:
                        pass

                if entry:
                    out[key] = entry

        if out:
            logger.info("[%s] Loaded %d OCR region override(s) from %s",
                        self.account["name"], len(out), path)
        return out

    def _viewport_size(self) -> tuple[int, int]:
        """Return current viewport size, with config fallback before page is ready."""
        if self.page:
            try:
                dims = self.page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
                w = int(dims.get("w", 0))
                h = int(dims.get("h", 0))
                if w > 0 and h > 0:
                    return w, h
            except Exception:
                pass
        return int(self.settings.get("window_width", 1280)), int(self.settings.get("window_height", 720))

    def _region(self, key: str, fallback: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        """Return an override region when available; otherwise use fallback."""
        entry = self.region_overrides.get(key)
        if entry is None:
            return fallback

        if isinstance(entry, tuple) and len(entry) == 4:
            region = entry
        elif isinstance(entry, dict):
            region = None
            norm = entry.get("norm")
            if isinstance(norm, tuple) and len(norm) == 4:
                vw, vh = self._viewport_size()
                nx, ny, nw, nh = norm
                region = (
                    int(round(nx * vw)),
                    int(round(ny * vh)),
                    max(1, int(round(nw * vw))),
                    max(1, int(round(nh * vh))),
                )
            elif isinstance(entry.get("px"), tuple) and len(entry["px"]) == 4:
                region = entry["px"]
            else:
                return fallback
        else:
            return fallback

        try:
            x, y, w, h = [int(v) for v in region]
            region = (x, y, w, h)
        except Exception:
            return fallback

        if region[2] <= 0 or region[3] <= 0:
            return fallback

        if region != fallback:
            logger.debug("[%s] Using region override '%s': %s", self.account["name"], key, region)
        return region

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Launch the browser with a persistent profile and navigate to the game."""
        self._pw = sync_playwright().start()

        profile_path = Path("./profiles") / self.account["name"]
        profile_path.mkdir(parents=True, exist_ok=True)

        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=self.settings["headless"],
            args=[
                "--start-maximized",
                "--disable-features=IsolateOrigins,site-per-process",
                "--force-device-scale-factor=1",
            ],
            no_viewport=True,
        )

        self.page = self.context.new_page()
        logger.info("[%s] Navigating to %s", self.account["name"], GAME_URL)
        self.page.goto(GAME_URL, timeout=90_000)

        self._handle_login_if_needed()
        self._wait_for_map()

    def stop(self):
        try:
            if self.context:
                self.context.close()
        finally:
            if self._pw:
                self._pw.stop()

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def _handle_login_if_needed(self):
        """
        Always perform the full two-step login flow:
          1. Wait for page to settle, then find the 'Log in' link by text via JS.
          2. Click it — reveals the Email + Password form.
          3. Fill credentials and click the Login button.
        """
        # ── Wait for page to fully load ──────────────────────────────────
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_timeout(2_000)

        # ── If the map canvas is already up, no login is required ────────
        if self.page.query_selector(SELECTORS["canvas"]):
            logger.info("[%s] Map already loaded — no login required.", self.account["name"])
            return

        # ── Step 1: find and click the 'Log in' nav link by visible text ─
        # Uses JavaScript so it works regardless of CSS class names.
        logger.info("[%s] Looking for 'Log in' link by text...", self.account["name"])
        clicked = self.page.evaluate("""() => {
            const all = Array.from(document.querySelectorAll('a, span, button, div'));
            const el = all.find(e => {
                const t = e.textContent.trim().toLowerCase();
                return (t === 'log in' || t === 'login') &&
                       e.offsetParent !== null;  // only visible elements
            });
            if (el) { el.click(); return true; }
            return false;
        }""")

        if not clicked:
            logger.warning("[%s] Could not find a visible 'Log in' element — skipping login.", self.account["name"])
            return

        logger.info("[%s] Clicked 'Log In' — waiting for email input.", self.account["name"])
        self.page.wait_for_timeout(1_500)

        # ── Step 2: wait for the login popup email field ──────────────────
        try:
            self.page.wait_for_selector(SELECTORS["login_email"], state="visible", timeout=10_000)
        except PlaywrightTimeoutError:
            logger.error("[%s] Email input did not appear after clicking 'Log In'.", self.account["name"])
            return

        # ── Step 3: fill and submit ───────────────────────────────────────
        logger.info("[%s] Filling login form.", self.account["name"])
        self.page.click(SELECTORS["login_email"])
        self.page.fill(SELECTORS["login_email"], self.account["user"])
        self.page.wait_for_timeout(300)
        self.page.click(SELECTORS["login_password"])
        self.page.fill(SELECTORS["login_password"], self.account["pass"])
        self.page.wait_for_timeout(300)
        self.page.click(SELECTORS["login_submit"])
        logger.info("[%s] Login submitted — waiting for map to load.", self.account["name"])
        self.page.wait_for_timeout(3_000)

    def _wait_for_map(self):
        """
        Wait for the game to fully load:
          1. Loading screen with progress bar fades away.
          2. Canvas becomes visible.
          3. Any post-login popup (purchase offer, daily reward, etc.) is dismissed.
        """
        logger.info("[%s] Waiting for game loading screen to finish...", self.account["name"])
        # Wait for the loading indicator to disappear
        try:
            self.page.wait_for_selector(
                ".game-loading-indicator",
                state="hidden",
                timeout=120_000,
            )
        except PlaywrightTimeoutError:
            pass  # loading indicator may not exist if already loaded

        # Wait for the canvas itself
        logger.info("[%s] Waiting for map canvas...", self.account["name"])
        self.page.wait_for_selector(SELECTORS["canvas"], timeout=60_000)
        self.page.wait_for_timeout(2_000)   # allow initial assets to settle

        # Dismiss any post-login popup (purchase offer, daily reward, etc.)
        self._dismiss_post_login_popup()
        logger.info("[%s] Map ready.", self.account["name"])

    def _dismiss_post_login_popup(self):
        """
        Dismiss the purchase/offer popup that appears right after login.
        The red X can't be inspected easily, so we try multiple strategies:
          1. Common close-button CSS classes.
          2. JS scan for any visible element that looks like a close/X button.
        """
        self.page.wait_for_timeout(1_500)  # let popup animate in

        # Strategy 0: press Escape — dismisses most post-login popups
        logger.info("[%s] Pressing Escape to dismiss post-login popup.", self.account["name"])
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(800)

        # Strategy 1: common CSS selectors for close buttons (fallback)
        close_selectors = [
            ".popup-close", ".modal-close", ".close-btn", ".btn-close",
            ".close-button", ".close_button", ".popup__close",
            '[data-action="close"]', '[data-handler*="close"]',
            ".icon-close", ".cross-btn", ".cross_btn",
        ]
        for sel in close_selectors:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    logger.info("[%s] Dismissing popup via '%s'.", self.account["name"], sel)
                    btn.click()
                    self.page.wait_for_timeout(800)
                    return
            except Exception:
                pass

        # Strategy 2: JS scan — find a visible element whose text is "×", "✕", "X"
        # or whose size is small (typical for icon-only close buttons)
        dismissed = self.page.evaluate("""() => {
            const candidates = Array.from(document.querySelectorAll('*'));
            for (const el of candidates) {
                if (el.offsetParent === null) continue;  // skip hidden
                const t = el.textContent.trim();
                const r = el.getBoundingClientRect();
                const isCloseIcon = ['×', '✕', '✖', 'x', 'X', ''].includes(t) &&
                                    r.width < 60 && r.height < 60 &&
                                    r.width > 5  && r.height > 5;
                const hasCloseClass = el.className && typeof el.className === 'string' &&
                    /close|dismiss|cross|cancel/i.test(el.className);
                if (isCloseIcon || hasCloseClass) {
                    el.click();
                    return el.className || el.tagName;
                }
            }
            return null;
        }""")
        if dismissed:
            logger.info("[%s] Dismissed popup via JS scan (matched: %s).", self.account["name"], dismissed)
            self.page.wait_for_timeout(800)
        else:
            logger.info("[%s] No post-login popup found — continuing.", self.account["name"])

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_loop(self):
        """
        Main loop:
          1. Open Watchtower → Crypts and Arenas
          2. Apply quality-tab and level-slider filters
          3. Click 'Go' on the second result
          4. Press Escape to dismiss the purchase/info overlay
          5. Click Explore in the crypt popup to send the captain
          6. Speed up the march 5× with the 50% speedup
          7. Wait for March (Carter) to disappear from the HUD
          8. Repeat
        """
        logger.info("[%s] Crypt loop started.", self.account["name"])
        filters_applied = False
        while True:
            try:
                self._open_watchtower()
                # Apply filters only once per session (or after a page reload).
                if not filters_applied:
                    self._apply_filters()
                    filters_applied = True
                else:
                    logger.info("[%s] Filters already set — skipping.", self.account["name"])
                self._pick_second_result()
                self._explore_crypt()
                self._speedup_march(times=5)
                self._wait_for_march_end()

                self.crypt_count += 1
                logger.info("[%s] Cycle #%d complete.", self.account["name"], self.crypt_count)

                # Periodic page reload to clear WebGL memory
                if self.crypt_count % self.settings["reload_every_n_crypts"] == 0:
                    logger.info("[%s] Reloading page (WebGL cache flush).", self.account["name"])
                    self.page.reload(timeout=90_000)
                    self._wait_for_map()
                    filters_applied = False  # re-apply after reload

            except PlaywrightTimeoutError as exc:
                logger.error("[%s] Timeout: %s — recovering.", self.account["name"], exc)
                self._dismiss_overlays(count=2)
                time.sleep(5)

            except Exception:
                logger.exception("[%s] Unexpected error — recovering.", self.account["name"])
                self._dismiss_overlays(count=2)
                time.sleep(10)

    # ------------------------------------------------------------------
    # Watchtower / search
    # ------------------------------------------------------------------

    def _dismiss_any_popup(self):
        """Silently close any visible overlay so the HUD is accessible."""
        try:
            btn = self.page.query_selector(SELECTORS["close_popup"])
            if btn:
                btn.click()
                self.page.wait_for_timeout(600)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # OCR helpers
    # ------------------------------------------------------------------

    def _get_ocr_reader(self):
        """Lazily load EasyOCR (downloads ~100 MB of models on first run)."""
        if CryptBot._ocr_reader is None:
            import easyocr
            logger.info("[%s] Loading OCR model — first run may download models…", self.account["name"])
            CryptBot._ocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
            logger.info("[%s] OCR model ready.", self.account["name"])
        return CryptBot._ocr_reader

    def _ocr_screenshot(self) -> np.ndarray:
        """Capture a full-page screenshot and return it as a numpy RGB array."""
        from PIL import Image
        png = self.page.screenshot()
        img = Image.open(io.BytesIO(png)).convert("RGB")
        return np.array(img)

    # ------------------------------------------------------------------
    # Image template matching
    # ------------------------------------------------------------------

    _TEMPLATES_DIR = Path("./templates")

    def _image_find(self, template_name: str,
                    threshold: float = 0.80,
                    region: tuple[int, int, int, int] | None = None) -> tuple[int, int] | None:
        """
        Search the current screenshot for `template_name` (a PNG file inside
        the templates/ folder) using multi-scale normalised cross-correlation.

        Returns the (x, y) centre of the best match when confidence >= threshold,
        or None if no match is found.
        """
        import cv2
        tpl_path = self._TEMPLATES_DIR / template_name
        if not tpl_path.exists():
            logger.warning("[%s] Template not found: %s", self.account["name"], tpl_path)
            return None

        screenshot = self._ocr_screenshot()                       # RGB numpy
        ox, oy = 0, 0
        if region:
            rx, ry, rw, rh = region
            screenshot = screenshot[ry:ry+rh, rx:rx+rw]
            ox, oy = rx, ry
        screen_bgr = cv2.cvtColor(screenshot, cv2.COLOR_RGB2BGR)
        tpl_bgr    = cv2.imread(str(tpl_path))
        if tpl_bgr is None:
            logger.error("[%s] Could not read template: %s", self.account["name"], tpl_path)
            return None

        th, tw = tpl_bgr.shape[:2]
        sh, sw = screen_bgr.shape[:2]

        best_val, best_loc, best_scale = -1.0, (0, 0), 1.0
        # Try a range of scales in case the browser zoom differs from template capture
        for scale in [1.0, 0.9, 0.8, 1.1, 1.2]:
            rw, rh = int(tw * scale), int(th * scale)
            if rw > sw or rh > sh:
                continue
            tpl_scaled = cv2.resize(tpl_bgr, (rw, rh))
            result = cv2.matchTemplate(screen_bgr, tpl_scaled,
                                       cv2.TM_CCOEFF_NORMED)
            _, val, _, loc = cv2.minMaxLoc(result)
            if val > best_val:
                best_val, best_loc, best_scale = val, loc, scale

        if best_val < threshold:
            # Save a debug crop so we can compare to the template visually
            debug_path = self._TEMPLATES_DIR / f"_debug_{template_name}"
            tpl_h, tpl_w = tpl_bgr.shape[:2]
            # Crop 3× the template size around the best match location for context
            pad_x, pad_y = tpl_w, tpl_h
            bx, by = best_loc
            x1 = max(0, bx - pad_x)
            y1 = max(0, by - pad_y)
            x2 = min(screen_bgr.shape[1], bx + tpl_w + pad_x)
            y2 = min(screen_bgr.shape[0], by + tpl_h + pad_y)
            crop = screen_bgr[y1:y2, x1:x2]
            cv2.imwrite(str(debug_path), crop)
            logger.warning("[%s] Template '%s' not matched (best=%.2f < %.2f scale=%.1f) — debug crop saved to %s",
                           self.account["name"], template_name, best_val, threshold, best_scale, debug_path)
            return None

        rw, rh = int(tw * best_scale), int(th * best_scale)
        cx = best_loc[0] + rw // 2 + ox
        cy = best_loc[1] + rh // 2 + oy
        logger.info("[%s] Template '%s' matched at (%d,%d) conf=%.2f scale=%.1f",
                    self.account["name"], template_name, cx, cy, best_val, best_scale)
        return cx, cy

    def _image_click(self, template_name: str, wait_ms: int = 600,
                     threshold: float = 0.80,
                     fallback_key: str | None = None,
                     region: tuple[int, int, int, int] | None = None):
        """
        Click the centre of the best template match.
        Falls back to a canvas coordinate key if the image is not found.
        """
        pos = self._image_find(template_name, threshold=threshold, region=region)
        if pos:
            x, y = pos
            logger.info("[%s] image_click '%s' → (%d, %d)",
                        self.account["name"], template_name, x, y)
            self.page.mouse.click(x, y)
        elif fallback_key:
            logger.warning("[%s] image_click missed '%s' — coord fallback '%s'",
                           self.account["name"], template_name, fallback_key)
            self._canvas_click(fallback_key, wait_ms=0)
        else:
            logger.error("[%s] image_click could not find '%s' — skipping.",
                         self.account["name"], template_name)
        if wait_ms:
            self.page.wait_for_timeout(wait_ms)

    def _ocr_find_all(self, text: str,
                      region: tuple[int, int, int, int] | None = None
                      ) -> list[tuple[int, int]]:
        """
        Return viewport (x, y) centre-points for every occurrence of `text`.
        `region` is an optional (x, y, w, h) crop in viewport pixels — pass this
        to limit the scan area and make OCR ~10× faster.
        Minimum confidence threshold: 0.25.
        """
        reader = self._get_ocr_reader()
        img    = self._ocr_screenshot()          # full RGB numpy array

        ox, oy = 0, 0
        if region:
            rx, ry, rw, rh = region
            img = img[ry:ry + rh, rx:rx + rw]
            ox, oy = rx, ry                      # offset to map back to viewport

        results = reader.readtext(img, detail=1)
        needle  = text.strip().lower()
        hits    = []
        for (bbox, detected, conf) in results:
            if conf >= 0.25 and needle in detected.strip().lower():
                xs = [p[0] for p in bbox]
                ys = [p[1] for p in bbox]
                hits.append((int(sum(xs) / len(xs)) + ox,
                             int(sum(ys) / len(ys)) + oy))
        hits.sort(key=lambda p: (p[1], p[0]))
        logger.debug("[%s] OCR '%s' → %d match(es): %s", self.account["name"], text, len(hits), hits)
        return hits

    def _ocr_click(self, text: str, wait_ms: int = 600, index: int = 0,
                   fallback_key: str | None = None,
                   region: tuple[int, int, int, int] | None = None):
        """
        Find `text` via OCR and click the `index`-th match (0 = first).
        Falls back to a canvas coordinate key when OCR finds nothing.
        """
        hits = self._ocr_find_all(text, region=region)
        if len(hits) > index:
            x, y = hits[index]
            logger.info("[%s] OCR click '%s'[%d] → (%d, %d)",
                        self.account["name"], text, index, x, y)
            self.page.mouse.click(x, y)
        elif fallback_key:
            logger.warning("[%s] OCR missed '%s' — coord fallback '%s'",
                           self.account["name"], text, fallback_key)
            self._canvas_click(fallback_key, wait_ms=0)
        else:
            logger.error("[%s] OCR could not find '%s' (index=%d) — skipping.",
                         self.account["name"], text, index)
        if wait_ms:
            self.page.wait_for_timeout(wait_ms)

    def _canvas_offset(self) -> tuple[int, int]:
        """Return the (left, top) viewport offset of the game canvas."""
        rect = self.page.evaluate(
            "() => { const r = document.querySelector('canvas').getBoundingClientRect(); "
            "return {left: r.left, top: r.top}; }"
        )
        return int(rect["left"]), int(rect["top"])

    def _canvas_click(self, coord_key: str, wait_ms: int = 600):
        """Convert canvas-relative coords to viewport coords and click.

        If a region override exists for this key in manual_regions.json,
        the centre of that region is used as the absolute click point instead
        of the hardcoded CANVAS_COORDS entry.
        """
        ox, oy = self._canvas_offset()
        
        entry = self.region_overrides.get(coord_key)
        if entry is not None:
            # Resolve the pixel region (with optional normalized scaling)
            fallback = CANVAS_COORDS.get(coord_key, (0, 0))
            fallback_region = (fallback[0] - 50, fallback[1] - 50, 100, 100)
            rx, ry, rw, rh = self._region(coord_key, fallback_region)
            # The region from manual_regions is already an absolute page coordinate!
            x, y = rx + rw // 2, ry + rh // 2
            logger.info("[%s] click '%s' → region centre absolute page(%d,%d)",
                        self.account["name"], coord_key, x, y)
        else:
            cx, cy = CANVAS_COORDS[coord_key]
            x, y = cx + ox, cy + oy
            logger.info("[%s] click '%s' → canvas(%d,%d) + offset(%d,%d) = page(%d,%d)",
                        self.account["name"], coord_key, cx, cy, ox, oy, x, y)
            
        self.page.mouse.click(x, y)
        if wait_ms:
            self.page.wait_for_timeout(wait_ms)

    def _open_watchtower(self):
        """Click the Watchtower HUD icon then select 'Crypts and Arenas'."""
        logger.debug("[%s] Opening Watchtower.", self.account["name"])
        self.page.bring_to_front()
        self._canvas_click("watchtower_btn", wait_ms=1_200)
        logger.debug("[%s] Clicking 'Crypts and Arenas'.", self.account["name"])
        # Sidebar is centered around page(700, 549) - crop tightly around it
        self._ocr_click("Crypts", wait_ms=800, fallback_key="crypts_and_arenas",
                        region=self._region("watchtower_crypts", (560, 460, 320, 180)))

    def _save_tab_debug_screenshot(self, img: np.ndarray):
        """
        Save an annotated screenshot showing exactly where tab sampling boxes
        are drawn.  Check templates/_debug_tabs.png after a run to verify
        the coordinates are landing on the actual tab buttons.
        """
        from PIL import Image, ImageDraw
        pil = Image.fromarray(img)
        draw = ImageDraw.Draw(pil)
        ox, oy = self._canvas_offset()
        for tab in ALL_QUALITY_TABS:
            tab_key = f"tab_{tab.lower()}"
            cx, cy = CANVAS_COORDS[tab_key]
            px, py = cx + ox, cy + oy
            # Draw the sampling box (60×40)
            draw.rectangle([px-30, py-20, px+30, py+20], outline=(255, 0, 0), width=2)
            draw.text((px-30, py-32), tab_key, fill=(255, 0, 0))
        out = self._TEMPLATES_DIR / "_debug_tabs.png"
        pil.save(str(out))
        logger.info("[%s] Tab debug screenshot saved to %s", self.account["name"], out)

    def _is_tab_green(self, tab: str, img: np.ndarray, search_region: tuple[int, int, int, int]) -> bool:
        """
        Uses OCR to find the tab text, then checks the pixel colors inside its
        bounding box to see if the button is green (selected).
        """
        reader = self._get_ocr_reader()
        rx, ry, rw, rh = search_region
        crop = img[ry:ry+rh, rx:rx+rw]
        results = reader.readtext(crop, detail=1)

        for (bbox, text, conf) in results:
            if tab.lower() in text.lower():
                # bbox is [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
                xs = [int(p[0]) for p in bbox]
                ys = [int(p[1]) for p in bbox]
                x1, x2 = min(xs), max(xs)
                y1, y2 = min(ys), max(ys)
                
                # Expand box slightly to catch the button background, not just text
                x1 = max(0, x1 - 10)
                y1 = max(0, y1 - 10)
                x2 = min(rw, x2 + 10)
                y2 = min(rh, y2 + 10)
                
                patch = crop[y1:y2, x1:x2]
                r_ch = patch[:, :, 0].astype(float)
                g_ch = patch[:, :, 1].astype(float)
                
                green_pixels = int(((g_ch - r_ch) > 15).sum())
                total_pixels = patch.shape[0] * patch.shape[1]
                is_green = green_pixels > (total_pixels * 0.05)  # 5% green is enough
                
                logger.info("[%s] Tab '%s' OCR color check: %d/%d green px → %s", 
                            self.account["name"], tab, green_pixels, total_pixels, "GREEN" if is_green else "TAN")
                return is_green
                
        # Fallback if OCR can't find the tab text
        logger.warning("[%s] OCR could not find tab '%s' to check color! Assuming default.", self.account["name"], tab)
        return tab in {"Rare", "Epic", "Arenas"}

    def _apply_filters(self):
        """
        Set each quality tab to match settings['crypt_types'].
        
        Uses OCR to find the tab's location, then checks the pixel colors 
        around that text to confirm if it is currently selected (green).
        """
        logger.debug("[%s] Applying search filters.", self.account["name"])
        self.page.wait_for_timeout(800)

        wanted = {t.capitalize() for t in self.settings["crypt_types"]}
        img = self._ocr_screenshot()
        
        # Use your custom manual region from JSON if available
        tab_region = self._region("quality_tabs", (700, 200, 700, 200))

        for tab in ALL_QUALITY_TABS:
            tab_key = f"tab_{tab.lower()}"
            should_be_selected = tab in wanted
            currently_selected = self._is_tab_green(tab, img, tab_region)

            if should_be_selected == currently_selected:
                logger.info("[%s] Tab '%s' state is correct — skipping.", self.account["name"], tab)
                continue

            action = "Selecting" if should_be_selected else "Deselecting"
            logger.info("[%s] %s tab '%s'.", self.account["name"], action, tab)
            
            self._ocr_click(tab, wait_ms=500, fallback_key=tab_key,
                            region=tab_region)

        # Dynamically drag both sliders until the number 25 is read
        self._drag_slider_to_level("left", CANVAS_COORDS["slider_track_left"], "25")
        self._drag_slider_to_level("right", CANVAS_COORDS["slider_track_right"], "25")

    def _drag_slider_to_level(self, side: str, start_coord: tuple[int, int], target_level: str):
        """
        Dynamically finds the current slider handle position by reading the numbers
        above the track within `slider_levels`, then sweeps horizontally until the
        target_level is met.
        """
        logger.info("[%s] Dragging %s slider to level %s...", self.account["name"], side, target_level)
        ox, oy = self._canvas_offset()
        cx, cy = start_coord
        y = cy + oy
        
        # Pull dynamic scanning region from overrides if set
        scan_region = self._region("slider_levels", (700, cy - 35, 700, 40))
        rx, ry, rw, rh = scan_region
        
        reader = self._get_ocr_reader()
        img = self._ocr_screenshot()
        crop = img[ry:ry+rh, rx:rx+rw]
        
        # Parse current numbers and target X coordinates
        results = reader.readtext(crop, detail=1)
        parsed_handles = []
        for bbox, text, conf in results:
            num_str = ''.join(filter(str.isdigit, text))
            if num_str and conf >= 0.2:
                xs = [p[0] for p in bbox]
                cx_local = int(sum(xs) / len(xs))
                cx_viewport = cx_local + rx
                parsed_handles.append((int(num_str), cx_viewport))
                
        parsed_handles.sort(key=lambda item: item[1]) # Sort left-to-right by X coord
        
        if not parsed_handles:
            logger.warning("[%s] Could not read any numbers in slider_levels. Falling back to defaults.", self.account["name"])
            current_x = cx + ox
            current_val = -1
        else:
            if side == "left":
                current_val, current_x = parsed_handles[0]
            else:
                current_val, current_x = parsed_handles[-1]
                
            logger.info("[%s] %s slider currently at level %d (x: %d).", self.account["name"], side, current_val, current_x)
            
            if str(current_val) == str(target_level):
                logger.info("[%s] Slider already at %s, no dragging needed.", self.account["name"], target_level)
                return
        
        target_int = int(target_level)
        self.page.mouse.move(current_x, y)
        self.page.mouse.down()
        
        # Determine sweep direction
        if current_val != -1:
            direction = 1 if target_int > current_val else -1
            # Scale sweeping steps a bit smaller if we're scanning dynamically
            step_pixels = 12 * direction
        else:
            direction = 1 if side == "left" else -1
            step_pixels = 25 * direction
            
        found = False
        # Sweep track max ~25 times to find the right number
        for _ in range(25):
            current_x += step_pixels
            self.page.mouse.move(current_x, y, steps=3)
            self.page.wait_for_timeout(350)  # Wait for UI number to update
            
            img2 = self._ocr_screenshot()
            crop2 = img2[ry:ry+rh, rx:rx+rw]
            results2 = reader.readtext(crop2, detail=0)
            
            # check if target number has appeared in the text box
            texts_digits = [''.join(filter(str.isdigit, t)) for t in results2]
            
            logger.debug("Slider %s at %d OCR: %s", side, current_x, " ".join(texts_digits))
            
            if str(target_level) in texts_digits:
                logger.info("[%s] Found level %s at x=%d!", self.account["name"], target_level, current_x)
                found = True
                break
                
        self.page.mouse.up()
        self.page.wait_for_timeout(300)
        
        if not found:
            logger.warning("[%s] Failed to find level %s via OCR drag.", self.account["name"], target_level)

    def _pick_second_result(self) -> bool:
        """
        Click the 'Go' button next to the second result in the filtered list.
        Returns False if the click position is unreachable (no results visible).
        After clicking, the game shows a purchase/confirmation overlay — the
        caller is responsible for dismissing it with Escape.
        """
        logger.debug("[%s] Clicking 'Go' on second result.", self.account["name"])
        # Allow the filtered list to populate
        self.page.wait_for_timeout(1_500)
        # index=1 → second "Go" button in the results list
        # Results list Go buttons — page x≈1217, y≈698 for first result
        self._ocr_click("Go", wait_ms=1_000, index=1, fallback_key="second_result_go",
                        region=self._region("second_result_go", (1100, 580, 300, 350)))
        return True

    # ------------------------------------------------------------------
    # 25%-zoom centre interaction
    # ------------------------------------------------------------------

    def _wait_for_crypt_at_center(self):
        """
        Poll the 10×10 px cluster around the screen centre until the
        characteristic Crypt Stone/Blue colour is detected.

        This guards against clicking before the map tile has fully rendered
        after the camera jump — at 25% zoom one pixel ≈ many map tiles, so
        timing matters.
        """
        attempts = self.settings["color_check_attempts"]
        for i in range(attempts):
            if check_crypt_visible(self.page, self.settings):
                logger.debug("[%s] Crypt colour confirmed at centre.", self.account["name"])
                return
            logger.debug(
                "[%s] Centre pixel not ready (%d/%d) — waiting %dms.",
                self.account["name"], i + 1, attempts,
                self.settings["load_wait_ms"],
            )
            self.page.wait_for_timeout(self.settings["load_wait_ms"])

        # Log the actual colour so you can add it to config if needed
        actual = sample_center_color(self.page, self.settings)
        logger.warning(
            "[%s] Crypt colour not confirmed — proceeding anyway. "
            "Actual centre RGB: %s  (add to crypt_colors in config.py if correct).",
            self.account["name"], actual,
        )

    def _click_map_center(self):
        """Click the fixed screen centre — where the crypt sits after a camera jump."""
        cx = self.settings["screen_center_x"]
        cy = self.settings["screen_center_y"]
        logger.debug("[%s] Clicking map centre (%d, %d).", self.account["name"], cx, cy)
        self.page.mouse.click(cx, cy)
        self.page.wait_for_timeout(1_500)

    # ------------------------------------------------------------------
    # Solo march (Captain only, no troops)
    # ------------------------------------------------------------------

    def _explore_crypt(self):
        """
        After clicking 'Go' on a crypt result:
          1. Press Escape to dismiss the purchase/info overlay.
          2. Click the crypt on the map to open the detail popup.
          3. Wait for the popup to appear, then click Explore.
          4. Press Escape once more to clear any remaining popup.
        """
        # Step 1 — dismiss purchase/info overlay that appears after clicking Go.
        # Wait a moment for it to fully render before pressing Escape.
        self.page.wait_for_timeout(800)
        logger.debug("[%s] Pressing Escape to dismiss purchase overlay.", self.account["name"])
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(1_200)

        # Step 2 — click the crypt on the map to open the detail popup
        logger.debug("[%s] Clicking crypt on map.", self.account["name"])
        self._canvas_click("crypt_location", wait_ms=1_200)

        # Step 3 — click Explore in the crypt detail popup
        logger.debug("[%s] Clicking Explore in crypt popup.", self.account["name"])
        # Explore button — page(1156, 880)
        self._ocr_click("Explore", wait_ms=800, fallback_key="crypt_popup_explore",
                        region=self._region("crypt_popup_explore", (950, 800, 400, 150)))

        # Step 4 — dismiss any remaining popup
        logger.debug("[%s] Pressing Escape to clear any remaining popup.", self.account["name"])
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(700)

    def _speedup_march(self, times: int = 5):
        """
        Scan `march_status` for 'Carter', determine which line he's on,
        then click the speedup button on that line, and then click 'Use' `times` times.
        """
        # Wait for the march to register and UI to show Carter
        logger.info("[%s] Waiting for March (Carter) to register in UI...", self.account["name"])
        self.page.wait_for_timeout(3_000)

        # Look for Carter in the user-defined march_status region to find the correct Y coordinate vertically
        region = self._region("march_status", (500, 40, 700, 60))
        hits = self._ocr_find_all("Carter", region=region)
        
        if hits:
            # First hit's Y coordinate tells us Carter's row
            cx, cy = hits[0]
            logger.info("[%s] Found 'Carter' on row Y=%d. Clicking speedup here.", self.account["name"], cy)
            
            canvas_ox, canvas_oy = self._canvas_offset()
            btn_x, btn_y = CANVAS_COORDS["march_speedup_btn"]
            
            target_x = btn_x + canvas_ox
            target_y = cy
            
            self.page.mouse.click(target_x, target_y)
            self.page.wait_for_timeout(1_500)
        else:
            logger.warning("[%s] Could not locate 'Carter' in march_status! Resorting to default button.", self.account["name"])
            self._canvas_click("march_speedup_btn", wait_ms=1_500)

        # Click Use (50% speedup, first entry) the requested number of times
        for i in range(times):
            logger.info("[%s] Speedup click %d/%d.", self.account["name"], i + 1, times)
            self._canvas_click("speedup_use_btn", wait_ms=700)

        # Close the popup
        logger.info("[%s] Closing speedup popup.", self.account["name"])
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(700)

    def _wait_for_march_end(self, poll_ms: int = 5_000, timeout_s: int = 600):
        """
        Poll the top HUD bar via OCR until 'Carter' is no longer visible,
        meaning the march has returned.  Times out after `timeout_s` seconds.
        The HUD bar is a thin strip at the very top of the viewport.
        """
        logger.info("[%s] Waiting for March (Carter) to finish...", self.account["name"])
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            hits = self._ocr_find_all("Carter", region=self._region("march_status", (500, 40, 700, 60)))
            if not hits:
                logger.info("[%s] March (Carter) gone — ready for next cycle.", self.account["name"])
                return
            remaining = int(deadline - time.time())
            logger.info("[%s] Still marching (Carter visible) — %ds until timeout.",
                        self.account["name"], remaining)
            self.page.wait_for_timeout(poll_ms)

        logger.warning("[%s] March wait timed out after %ds — proceeding anyway.",
                       self.account["name"], timeout_s)

    def _dismiss_overlays(self, count: int = 1):
        """
        Press Escape `count` times. Used in error-recovery paths.
        """
        for i in range(count):
            logger.debug("[%s] Pressing Escape (%d/%d).", self.account["name"], i + 1, count)
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(700)
