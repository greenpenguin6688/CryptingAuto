"""
main.py — CryptingAuto entry point.

Usage
-----
Single kingdom (good for calibrating selectors):
    python main.py --single Kingdom1

Calibration mode (opens browser, does nothing — measure coordinates freely):
    python main.py --calibrate 1020

All 6 kingdoms, visible windows (default):
    python main.py

All 6 kingdoms, headless (background mode):
    python main.py --headless

Colour calibration (print the centre-pixel RGB while a Crypt is centred):
    python main.py --sample Kingdom1
"""

import argparse
import copy
import logging
import multiprocessing
import sys
import time

from bot import CryptBot, sample_center_color
from config import ACCOUNTS, SETTINGS

# ---------------------------------------------------------------------------
# Logging — writes to console AND cryptbot.log simultaneously
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(process)d %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("cryptbot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-process worker
# ---------------------------------------------------------------------------

def _run_account(args: tuple):
    """
    Top-level function executed in each worker process.
    Must be a module-level function (not a lambda) for multiprocessing on Windows.
    """
    account, settings, delay_s = args

    if delay_s:
        logger.info("[%s] Staggered start — waiting %ds.", account["name"], delay_s)
        time.sleep(delay_s)

    bot = CryptBot(account, settings)
    try:
        bot.start()
        bot.run_loop()
    except KeyboardInterrupt:
        logger.info("[%s] Interrupted by user.", account["name"])
    except Exception:
        logger.exception("[%s] Fatal error in worker.", account["name"])
    finally:
        bot.stop()


# ---------------------------------------------------------------------------
# Colour-sample helper (single-kingdom, interactive)
# ---------------------------------------------------------------------------

def _cmd_sample(account_name: str):
    """
    Launch one browser, navigate to the game, then print the centre-pixel
    colour every 2 seconds.  Use this to find the right 'crypt_colors' values:
      1. Open the game manually, centre the map on a Crypt at 25% zoom.
      2. Run:  python main.py --sample Kingdom1
      3. Copy the printed (R, G, B) tuple into config.py → 'crypt_colors'.
    """
    from playwright.sync_api import sync_playwright
    from pathlib import Path

    account = next((a for a in ACCOUNTS if a["name"] == account_name), None)
    if account is None:
        print(f"Unknown account '{account_name}'. Check ACCOUNTS in config.py.")
        sys.exit(1)

    settings = copy.deepcopy(SETTINGS)
    settings["headless"] = False   # must be visible for calibration

    print(f"[sample] Launching {account_name} — navigate to a Crypt at 25% zoom, then watch the output.")
    print("         Press Ctrl+C to stop.\n")

    with sync_playwright() as pw:
        profile_path = Path("./profiles") / account_name
        profile_path.mkdir(parents=True, exist_ok=True)

        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            args=[f"--window-size={settings['window_width']},{settings['window_height']}"],
            viewport={"width": settings["window_width"], "height": settings["window_height"]},
        )
        page = ctx.new_page()
        page.goto("https://totalbattle.com/game/", timeout=90_000)
        page.wait_for_selector("canvas", timeout=120_000)

        try:
            while True:
                rgb = sample_center_color(page, settings)
                print(f"  Centre RGB: {rgb}  ← paste into crypt_colors if this matches a Crypt")
                time.sleep(2)
        except KeyboardInterrupt:
            pass
        finally:
            ctx.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="CryptingAuto — Total Battle crypt farmer")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--single", metavar="KINGDOM",
        help="Run only one kingdom by name (useful for testing selectors).",
    )
    group.add_argument(
        "--calibrate", metavar="KINGDOM",
        help="Open the browser and wait — use to measure coordinates without the bot clicking.",
    )
    group.add_argument(
        "--sample", metavar="KINGDOM",
        help="Colour-calibration mode: print centre-pixel RGB every 2 s.",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Override config.py and force headless mode for all instances.",
    )
    args = parser.parse_args()

    # ── Calibration mode ─────────────────────────────────────────────────
    if args.calibrate:
        account = next((a for a in ACCOUNTS if a["name"] == args.calibrate), None)
        if account is None:
            logger.error("Unknown account '%s'.", args.calibrate)
            sys.exit(1)
        bot = CryptBot(account, copy.deepcopy(SETTINGS))
        bot.start()
        print("\n[calibrate] Browser is open. Measure coordinates freely.")
        print("            Paste this in DevTools Console (undocked window):")
        print("            document.getElementById('unityCanvas').addEventListener('click', e => {")
        print("              const r = e.target.getBoundingClientRect();")
        print("              console.log('canvas:', Math.round(e.clientX-r.left), Math.round(e.clientY-r.top), '| page:', Math.round(e.clientX), Math.round(e.clientY));")
        print("            });")
        print("\n            Press Ctrl+C when done.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            bot.stop()
        return

    # ── Colour calibration ───────────────────────────────────────────────
    if args.sample:
        _cmd_sample(args.sample)
        return

    # ── Build per-process task list ──────────────────────────────────────
    settings = copy.deepcopy(SETTINGS)
    if args.headless:
        settings["headless"] = True
        logger.info("Headless mode forced via --headless flag.")

    if args.single:
        account = next((a for a in ACCOUNTS if a["name"] == args.single), None)
        if account is None:
            logger.error("Unknown kingdom '%s'. Check ACCOUNTS in config.py.", args.single)
            sys.exit(1)
        tasks = [(account, settings, 0)]
    else:
        stagger = settings["stagger_start_seconds"]
        tasks   = [
            (account, settings, i * stagger)
            for i, account in enumerate(ACCOUNTS)
        ]

    # ── Launch worker processes ──────────────────────────────────────────
    logger.info(
        "Starting %d bot process(es).  headless=%s  stagger=%ds",
        len(tasks),
        settings["headless"],
        settings["stagger_start_seconds"],
    )

    if len(tasks) == 1:
        # Single-process path avoids multiprocessing overhead during testing
        _run_account(tasks[0])
    else:
        # Windows requires the spawn guard (if __name__ == "__main__")
        with multiprocessing.Pool(processes=len(tasks)) as pool:
            pool.map(_run_account, tasks)


if __name__ == "__main__":
    main()
