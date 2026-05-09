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
# CSS / XPath selectors — calibrate these against the live game UI
# ---------------------------------------------------------------------------
SELECTORS = {
    # Login — step 1: "Log In" link in the top-right header nav
    "login_nav_btn":     '.header__login, .header-login, a[href*="login"]',

    # Login — step 2: the email+password form (shown after clicking the nav link)
    # Scoped to #login popup to avoid matching the registration form's email field
    "login_email":       '#login input[name="email"]',
    "login_password":    '#login input[name="password"]',  # scoped to login popup
    "login_submit":      '#login [data-handler="login_form_handler"]',  # scoped to login popup

    # Map canvas (used for pixel sampling)
    "canvas":            "canvas",

    # Watchtower / Great Watchtower search button in the HUD
    "search_btn":        '[data-id="watchtower-btn"], .watchtower-icon, .great-watchtower',

    # Tabs inside the search window
    "crypts_tab":        '[data-tab="crypts"], .crypts-tab, .tab-crypt',

    # Quality filter toggles
    "quality_rare":      '[data-quality="rare"],  .quality-rare,  .filter-rare',
    "quality_epic":      '[data-quality="epic"],  .quality-epic,  .filter-epic',

    # Level range inputs
    "level_min_input":   '.filter-level-min input, input[name="minLevel"]',
    "level_max_input":   '.filter-level-max input, input[name="maxLevel"]',

    # "Search" execute button and result list
    "search_execute":    '.search-execute-btn, [data-action="search"], .btn-search',
    "first_result":      '.search-result-item:first-child, .result-list li:first-child',

    # Object interaction popup (appears after clicking the crypt on the map)
    "explore_btn":       '.explore-btn, [data-action="explore"], .btn-explore',

    # Captain / army selection → final march confirmation
    "march_final_btn":   '.march-confirm-btn, [data-action="march"], .btn-march-confirm',

    # March-slot indicator in the HUD (top bar)
    "march_slot":        '.march-slot, .captain-slot',
    "march_slot_busy":   '.march-slot.busy, .captain-slot.active, .march-slot--occupied',

    # Generic close / dismiss
    "close_popup":       '.close-btn, .popup-close, .modal-close, .btn-close',
}


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
                f"--window-size={self.settings['window_width']},{self.settings['window_height']}",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            viewport={
                "width":  self.settings["window_width"],
                "height": self.settings["window_height"],
            },
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
        logger.info("[%s] Crypt loop started.", self.account["name"])
        while True:
            try:
                # ── Check if captain is already out ────────────────────
                if is_captain_busy(self.page):
                    logger.info(
                        "[%s] Captain marching — sleeping %ds.",
                        self.account["name"],
                        self.settings["march_sleep_seconds"],
                    )
                    time.sleep(self.settings["march_sleep_seconds"])
                    continue

                # ── One full crypt cycle ────────────────────────────────
                self._dismiss_any_popup()
                self._open_watchtower()
                self._apply_filters()

                if not self._jump_to_first_result():
                    logger.warning("[%s] No results — retrying in 30s.", self.account["name"])
                    self._dismiss_any_popup()
                    time.sleep(30)
                    continue

                self._wait_for_crypt_at_center()
                self._click_map_center()
                self._do_solo_march()

                self.crypt_count += 1
                logger.info("[%s] Crypt dispatched (#%d).", self.account["name"], self.crypt_count)

                # ── Periodic page reload to clear WebGL bloat ───────────
                if self.crypt_count % self.settings["reload_every_n_crypts"] == 0:
                    logger.info("[%s] Reloading page (WebGL cache flush).", self.account["name"])
                    self.page.reload(timeout=90_000)
                    self._wait_for_map()

            except PlaywrightTimeoutError as exc:
                logger.error("[%s] Timeout: %s — recovering.", self.account["name"], exc)
                self._dismiss_any_popup()
                time.sleep(5)

            except Exception:
                logger.exception("[%s] Unexpected error — recovering.", self.account["name"])
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

    def _open_watchtower(self):
        """Click the search icon and navigate to the Crypts tab."""
        logger.debug("[%s] Opening Watchtower.", self.account["name"])
        self.page.click(SELECTORS["search_btn"], timeout=10_000)
        self.page.wait_for_timeout(1_000)
        self.page.click(SELECTORS["crypts_tab"],  timeout=10_000)
        self.page.wait_for_timeout(600)

    def _apply_filters(self):
        """Toggle quality buttons and set min/max level inputs."""
        logger.debug("[%s] Applying search filters.", self.account["name"])

        for quality in self.settings["crypt_types"]:
            sel = SELECTORS.get(f"quality_{quality.lower()}")
            if sel:
                try:
                    self.page.click(sel, timeout=5_000)
                except PlaywrightTimeoutError:
                    logger.warning("[%s] Quality button '%s' not found.", self.account["name"], quality)

        for key, value in [
            ("level_min_input", self.settings["crypt_min_level"]),
            ("level_max_input", self.settings["crypt_max_level"]),
        ]:
            try:
                self.page.fill(SELECTORS[key], str(value), timeout=5_000)
            except PlaywrightTimeoutError:
                logger.warning("[%s] Level input '%s' not found.", self.account["name"], key)

    def _jump_to_first_result(self) -> bool:
        """
        Execute the search and click the first result to teleport the camera.
        Returns False when the result list is empty.
        """
        self.page.click(SELECTORS["search_execute"], timeout=10_000)
        self.page.wait_for_timeout(2_500)   # wait for server response + animation

        result = self.page.query_selector(SELECTORS["first_result"])
        if result is None:
            return False

        result.click()
        self.page.wait_for_timeout(2_000)   # camera fly-in animation
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

    def _do_solo_march(self):
        """
        Handle the two-step Explore popup:
          1. Click "Explore" in the object popup.
          2. Skip troop sliders entirely.
          3. Click the final "March" / "Confirm" button.
        """
        # Step 1 — open the Explore / Send Captain panel
        self.page.wait_for_selector(SELECTORS["explore_btn"], timeout=10_000)
        self.page.click(SELECTORS["explore_btn"])
        self.page.wait_for_timeout(1_000)

        # Step 2 — wait until the march button is enabled (captain is ready)
        try:
            self.page.wait_for_selector(
                SELECTORS["march_final_btn"],
                state="attached",
                timeout=10_000,
            )
        except PlaywrightTimeoutError:
            logger.warning("[%s] March button never appeared — skipping.", self.account["name"])
            self._dismiss_any_popup()
            return

        march_btn = self.page.query_selector(SELECTORS["march_final_btn"])
        if march_btn and march_btn.is_enabled():
            march_btn.click()
            self.page.wait_for_timeout(800)
            logger.debug("[%s] Captain dispatched.", self.account["name"])
        else:
            logger.warning(
                "[%s] March button present but disabled — captain may be busy.",
                self.account["name"],
            )
            self._dismiss_any_popup()
