"""
bot.py — CryptBot: login, search, 25%-zoom click, solo march.

Selector constants near the top of this file are the first thing you
should calibrate against the live game HTML.  Open DevTools (F12) on
the game page and locate the real class names / data-attributes for
each UI element listed in SELECTORS.
"""

import time
import logging
import math
from pathlib import Path
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
    "watchtower_btn":     (693, 944),

    # Left sidebar inside the Watchtower window
    "crypts_and_arenas":  (700, 506),

    # Quality filter tab buttons (top of the Crypts panel).
    # *** These are estimated — calibrate by hovering over each tab ***
    "tab_common":   (875, 396),
    "tab_rare":     (990, 396),
    "tab_epic":     (1133, 396),
    "tab_arenas":   (1279, 396),
    "tab_others":   (910, 429),

    # Level range slider.
    # Left handle is dragged to this target; right handle to its target.
    "slider_left_target":  (1080, 463),
    "slider_right_target": (1207, 345),
    # Approximate far-left / far-right of the slider track (drag start points)
    "slider_track_left":   (889, 350),
    "slider_track_right":  (1293, 350),

    # "Go" button next to the SECOND result in the filtered list
    "second_result_go":    (1217, 655),

    
    "crypt_location": (977, 587),

    # # "Explore" button in the crypt detail popup that appears on the map
    # after pressing Escape from the purchase/info overlay
    "crypt_popup_explore": (1156, 837),
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

    def __init__(self, account: dict, settings: dict):
        self.account     = account
        self.settings    = settings
        self.crypt_count = 0
        self.page        = None
        self.context     = None
        self._pw         = None   # playwright handle

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
            ],
            no_viewport=True,
            device_scale_factor=1,
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
          6. Press Escape to clear any remaining popup
          7. Repeat
        """
        logger.info("[%s] Crypt loop started.", self.account["name"])
        while True:
            try:
                self._open_watchtower()
                self._apply_filters()
                self._pick_second_result()
                self._explore_crypt()

                self.crypt_count += 1
                logger.info("[%s] Cycle #%d complete.", self.account["name"], self.crypt_count)

                # Brief pause between cycles
                time.sleep(self.settings.get("cycle_sleep_seconds", 5))

                # Periodic page reload to clear WebGL memory
                if self.crypt_count % self.settings["reload_every_n_crypts"] == 0:
                    logger.info("[%s] Reloading page (WebGL cache flush).", self.account["name"])
                    self.page.reload(timeout=90_000)
                    self._wait_for_map()

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

    def _canvas_offset(self) -> tuple[int, int]:
        """Return the (left, top) viewport offset of the game canvas."""
        rect = self.page.evaluate(
            "() => { const r = document.querySelector('canvas').getBoundingClientRect(); "
            "return {left: r.left, top: r.top}; }"
        )
        return int(rect["left"]), int(rect["top"])

    def _canvas_click(self, coord_key: str, wait_ms: int = 600):
        """Convert canvas-relative coords to viewport coords and click."""
        cx, cy = CANVAS_COORDS[coord_key]
        ox, oy = self._canvas_offset()
        x, y = cx + ox, cy + oy
        logger.info("[%s] click '%s' → canvas(%d,%d) + offset(%d,%d) = page(%d,%d)",
                    self.account["name"], coord_key, cx, cy, ox, oy, x, y)
        self.page.mouse.click(x, y)
        if wait_ms:
            self.page.wait_for_timeout(wait_ms)

    def _open_watchtower(self):
        """Click the Watchtower HUD icon then select 'Crypts and Arenas'."""
        logger.debug("[%s] Opening Watchtower.", self.account["name"])
        self._canvas_click("watchtower_btn", wait_ms=1_200)
        logger.debug("[%s] Clicking 'Crypts and Arenas'.", self.account["name"])
        self._canvas_click("crypts_and_arenas", wait_ms=800)

    def _apply_filters(self):
        """
        Select only the quality tabs listed in settings['crypt_types'].

        Assumption: when the Crypts panel first opens all tabs are selected
        (shown green).  We click every tab that is NOT wanted to deselect it,
        leaving only the desired tabs active.

        Then drag the level-range slider handles to the target positions.
        """
        logger.debug("[%s] Applying search filters.", self.account["name"])

        wanted = {t.capitalize() for t in self.settings["crypt_types"]}

        for tab in ALL_QUALITY_TABS:
            if tab not in wanted:
                coord_key = f"tab_{tab.lower()}"
                logger.debug("[%s] Deselecting tab '%s'.", self.account["name"], tab)
                self._canvas_click(coord_key, wait_ms=300)

        # Drag left slider handle to target position
        ox, oy = self._canvas_offset()
        lx, ly = CANVAS_COORDS["slider_track_left"]
        tx, ty = CANVAS_COORDS["slider_left_target"]
        logger.debug("[%s] Setting left level slider → (%d, %d).", self.account["name"], tx, ty)
        self.page.mouse.move(lx + ox, ly + oy)
        self.page.mouse.down()
        self.page.mouse.move(tx + ox, ty + oy, steps=15)
        self.page.mouse.up()
        self.page.wait_for_timeout(300)

        # Drag right slider handle to target position
        rx, ry = CANVAS_COORDS["slider_track_right"]
        tx2, ty2 = CANVAS_COORDS["slider_right_target"]
        logger.debug("[%s] Setting right level slider → (%d, %d).", self.account["name"], tx2, ty2)
        self.page.mouse.move(rx + ox, ry + oy)
        self.page.mouse.down()
        self.page.mouse.move(tx2 + ox, ty2 + oy, steps=15)
        self.page.mouse.up()
        self.page.wait_for_timeout(500)

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
        self._canvas_click("second_result_go", wait_ms=1_000)
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
        # Step 1 — dismiss purchase/info overlay
        logger.debug("[%s] Pressing Escape to dismiss purchase overlay.", self.account["name"])
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(900)

        # Step 2 — click the crypt on the map to open the detail popup
        logger.debug("[%s] Clicking crypt on map.", self.account["name"])
        self._canvas_click("crypt_location", wait_ms=1_200)

        # Step 3 — click Explore in the crypt detail popup
        logger.debug("[%s] Clicking Explore in crypt popup.", self.account["name"])
        self._canvas_click("crypt_popup_explore", wait_ms=800)

        # Step 4 — dismiss any remaining popup
        logger.debug("[%s] Pressing Escape to clear any remaining popup.", self.account["name"])
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(700)

    def _dismiss_overlays(self, count: int = 1):
        """
        Press Escape `count` times. Used in error-recovery paths.
        """
        for i in range(count):
            logger.debug("[%s] Pressing Escape (%d/%d).", self.account["name"], i + 1, count)
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(700)
