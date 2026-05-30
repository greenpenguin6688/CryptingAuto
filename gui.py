"""
gui.py -- CryptingAuto interactive startup wizard.

Run this instead of main.py to configure the bot with a graphical interface:

    python gui.py

The wizard walks you through three steps:
  1. Optional screen-region calibration  (same as --mark-regions)
  2. Crypt type selection                (Common / Rare / Epic / Arenas / Others)
  3. Level range + run limit            (min 1-35, max 1-35, 0 = unlimited runs)

After launch a live "Running" page shows cycle count and lets you stop the bot
and return to settings without closing the window.
"""

import copy
import logging
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox

from config import ACCOUNTS, SETTINGS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_CRYPT_TYPES = ["Common", "Rare", "Epic", "Arenas", "Others"]

BG       = "#1e1e2e"
BG_ALT   = "#313244"
FG       = "#cdd6f4"
FG_DIM   = "#a6adc8"
ACCENT   = "#cba6f7"
ACCENT2  = "#89b4fa"
BTN_FLAT = "#45475a"
RED      = "#f38ba8"
GREEN    = "#a6e3a1"


# ---------------------------------------------------------------------------
# Widget helpers
# ---------------------------------------------------------------------------

def _style_root(root: tk.Tk):
    root.configure(bg=BG)


def _title(parent, text: str) -> tk.Label:
    lbl = tk.Label(parent, text=text, bg=BG, fg=ACCENT,
                   font=("Segoe UI", 15, "bold"), pady=6, anchor="w")
    lbl.pack(fill="x")
    return lbl


def _subtitle(parent, text: str) -> tk.Label:
    lbl = tk.Label(parent, text=text, bg=BG, fg=FG_DIM,
                   font=("Segoe UI", 10), anchor="w", wraplength=420, justify="left")
    lbl.pack(fill="x", pady=(0, 10))
    return lbl


def _separator(parent):
    tk.Frame(parent, bg=BTN_FLAT, height=1).pack(fill="x", pady=8)


def _make_btn(parent, text: str, command, accent: bool = False,
              danger: bool = False) -> tk.Button:
    if danger:
        bg, fg = RED, BG
    elif accent:
        bg, fg = ACCENT, BG
    else:
        bg, fg = BTN_FLAT, FG
    return tk.Button(
        parent, text=text, command=command,
        bg=bg, fg=fg,
        activebackground=ACCENT2, activeforeground=BG,
        font=("Segoe UI", 10, "bold"),
        relief="flat", padx=16, pady=7,
        cursor="hand2",
    )


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

class SetupWizard(tk.Tk):
    """Setup wizard + live running page."""

    def __init__(self):
        super().__init__()
        self.title("CryptingAuto")
        self.resizable(False, False)
        _style_root(self)

        # Persistent wizard state (survives "Back to Settings")
        self._type_vars: dict = {
            ct: tk.BooleanVar(value=(ct in SETTINGS.get("crypt_types", ["Common"])))
            for ct in ALL_CRYPT_TYPES
        }
        self.crypt_min = tk.IntVar(
            value=max(1, min(35, int(SETTINGS.get("crypt_min_level", 25)))))
        self.crypt_max = tk.IntVar(
            value=max(1, min(35, int(SETTINGS.get("crypt_max_level", 25)))))
        self.run_limit = tk.IntVar(value=0)  # 0 = unlimited

        # Bot thread state
        self._stop_event = None
        self._bot_thread = None

        self._page_frame = None
        self._show_page_1()

    # ------------------------------------------------------------------
    # Page management
    # ------------------------------------------------------------------

    def _clear_page(self) -> tk.Frame:
        if self._page_frame:
            self._page_frame.destroy()
        self._page_frame = tk.Frame(self, bg=BG, padx=32, pady=22)
        self._page_frame.pack(fill="both", expand=True)
        return self._page_frame

    def _footer_buttons(self, parent, back_cmd=None,
                        fwd_text="Next ->", fwd_cmd=None, fwd_accent=True):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(12, 0))
        if back_cmd:
            _make_btn(row, "<- Back", back_cmd).pack(side="left")
        if fwd_cmd:
            _make_btn(row, fwd_text, fwd_cmd, accent=fwd_accent).pack(side="right")

    # ------------------------------------------------------------------
    # Page 1 -- Region calibration
    # ------------------------------------------------------------------

    def _show_page_1(self):
        f = self._clear_page()
        _title(f, "Step 1 of 3 -- Screen Regions")
        _subtitle(f,
            "If this is your first run, or your game window has moved/resized, "
            "calibrate the screen regions so the bot knows exactly where to look.\n\n"
            "Clicking \"Calibrate\" opens the game browser and lets you draw boxes "
            "over each UI element. You can skip this step if regions are already saved.")
        _separator(f)
        btn_row = tk.Frame(f, bg=BG)
        btn_row.pack(pady=6)
        _make_btn(btn_row, "  Calibrate Regions  ",
                  self._run_calibration, accent=True).pack(side="left", padx=(0, 10))
        _make_btn(btn_row, "Skip ->", self._show_page_2).pack(side="left")

    def _run_calibration(self):
        name = ACCOUNTS[0]["name"] if ACCOUNTS else "1020"
        run_kwargs = {}
        if sys.platform.startswith("win"):
            run_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE

        subprocess.run(
            [sys.executable, "main.py", "--mark-regions", name],
            **run_kwargs,
        )
        self._show_page_2()

    # ------------------------------------------------------------------
    # Page 2 -- Crypt types
    # ------------------------------------------------------------------

    def _show_page_2(self):
        f = self._clear_page()
        _title(f, "Step 2 of 3 -- Crypt Types")
        _subtitle(f, "Select the quality tiers you want the bot to target.\n"
                     "At least one must be checked.")
        _separator(f)
        cb_frame = tk.Frame(f, bg=BG)
        cb_frame.pack(anchor="w", pady=4)
        for ct in ALL_CRYPT_TYPES:
            tk.Checkbutton(
                cb_frame, text=ct, variable=self._type_vars[ct],
                bg=BG, fg=FG, selectcolor=BG_ALT,
                activebackground=BG, activeforeground=ACCENT2,
                font=("Segoe UI", 11), anchor="w",
            ).pack(fill="x", pady=3)
        _separator(f)
        self._footer_buttons(f, back_cmd=self._show_page_1,
                             fwd_text="Next ->", fwd_cmd=self._advance_page_2)

    def _advance_page_2(self):
        if not any(v.get() for v in self._type_vars.values()):
            messagebox.showwarning("Nothing selected",
                                   "Please select at least one crypt type.")
            return
        self._show_page_3()

    # ------------------------------------------------------------------
    # Page 3 -- Level range + run limit
    # ------------------------------------------------------------------

    def _show_page_3(self):
        f = self._clear_page()
        _title(f, "Step 3 of 3 -- Levels & Run Limit")
        _subtitle(f,
            "Set the level range to target (1-35). "
            "Run limit: how many crypts to farm before stopping (0 = unlimited).")
        _separator(f)

        grid = tk.Frame(f, bg=BG)
        grid.pack(anchor="w", pady=8)

        def _lbl(text, row):
            tk.Label(grid, text=text, bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 10), anchor="e").grid(
                row=row, column=0, sticky="e", padx=(0, 12), pady=6)

        def _spin(row, var, lo, hi):
            sb = tk.Spinbox(grid, from_=lo, to=hi, width=6,
                            textvariable=var,
                            font=("Segoe UI", 12), bg=BG_ALT, fg=FG,
                            buttonbackground=BTN_FLAT, relief="flat",
                            insertbackground=FG)
            sb.grid(row=row, column=1, sticky="w")

        _lbl("Min level:", 0);  _spin(0, self.crypt_min, 1, 35)
        _lbl("Max level:", 1);  _spin(1, self.crypt_max, 1, 35)
        _lbl("Run limit:", 2);  _spin(2, self.run_limit, 0, 9999)
        tk.Label(grid, text="(0 = unlimited)", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).grid(row=2, column=2, sticky="w", padx=8)

        self._level_hint = tk.Label(f, text="", bg=BG, fg=RED,
                                    font=("Segoe UI", 9))
        self._level_hint.pack(anchor="w")

        def _validate(*_):
            try:
                mn, mx = self.crypt_min.get(), self.crypt_max.get()
                self._level_hint.config(
                    text=("  Min ({}) cannot exceed Max ({})".format(mn, mx)
                          if mn > mx else ""))
            except tk.TclError:
                pass

        self.crypt_min.trace_add("write", _validate)
        self.crypt_max.trace_add("write", _validate)

        _separator(f)
        self._footer_buttons(f, back_cmd=self._show_page_2,
                             fwd_text="Start Bot", fwd_cmd=self._launch_bot,
                             fwd_accent=True)

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------

    def _launch_bot(self):
        try:
            mn = self.crypt_min.get()
            mx = self.crypt_max.get()
            rl = self.run_limit.get()
        except tk.TclError:
            messagebox.showerror("Invalid input", "All values must be whole numbers.")
            return
        if not (1 <= mn <= 35 and 1 <= mx <= 35):
            messagebox.showwarning("Out of range", "Level values must be between 1 and 35.")
            return
        if mn > mx:
            messagebox.showwarning("Invalid range",
                                   "Min ({}) cannot exceed Max ({}).".format(mn, mx))
            return

        selected_types = [ct for ct, v in self._type_vars.items() if v.get()]
        settings = copy.deepcopy(SETTINGS)
        settings["crypt_types"]     = selected_types
        settings["crypt_min_level"] = mn
        settings["crypt_max_level"] = mx

        self._stop_event = threading.Event()

        # Switch to the running page first, then start the thread
        self._show_running_page(selected_types, mn, mx, rl)

        self._bot_thread = threading.Thread(
            target=self._bot_worker,
            args=(settings, rl),
            daemon=True,
        )
        self._bot_thread.start()

    def _bot_worker(self, settings: dict, run_limit: int):
        """Runs in a background daemon thread."""
        from bot import CryptBot

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[
                logging.StreamHandler(sys.stdout),
                logging.FileHandler("cryptbot.log", encoding="utf-8"),
            ],
        )

        account = ACCOUNTS[0]
        bot = CryptBot(account, settings)
        try:
            bot.start()
            bot.run_loop(
                stop_event=self._stop_event,
                run_limit=run_limit,
                on_cycle=self._on_cycle_complete,
            )
        except Exception:
            logging.getLogger(__name__).exception("Bot worker error")
        finally:
            try:
                bot.stop()
            except Exception:
                pass
            self.after(0, self._on_bot_finished)

    # ------------------------------------------------------------------
    # Running page
    # ------------------------------------------------------------------

    def _show_running_page(self, types, mn, mx, limit):
        f = self._clear_page()
        _title(f, "Bot Running")

        info = "Types: {}    Levels: {}-{}".format(", ".join(types), mn, mx)
        info += ("    Limit: {} run{}".format(limit, "s" if limit != 1 else "")
                 if limit else "    Limit: unlimited")
        _subtitle(f, info)
        _separator(f)

        # Cycle counter row
        counter_row = tk.Frame(f, bg=BG)
        counter_row.pack(pady=12)
        tk.Label(counter_row, text="Cycles completed:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 11)).pack(side="left", padx=(0, 10))
        self._cycle_label = tk.Label(counter_row, text="0", bg=BG, fg=GREEN,
                                     font=("Segoe UI", 22, "bold"))
        self._cycle_label.pack(side="left")
        if limit:
            tk.Label(counter_row, text=" / {}".format(limit), bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 14)).pack(side="left")

        self._status_label = tk.Label(f, text="Running...", bg=BG, fg=GREEN,
                                      font=("Segoe UI", 10))
        self._status_label.pack()

        _separator(f)
        self._stop_btn = _make_btn(f, "Stop Bot", self._stop_bot, danger=True)
        self._stop_btn.pack(pady=4)

    def _on_cycle_complete(self, count: int):
        """Called from bot thread -- schedule GUI update on Tk thread."""
        self.after(0, lambda: self._cycle_label.config(text=str(count)))

    def _stop_bot(self):
        if self._stop_event:
            self._stop_event.set()
        if hasattr(self, "_stop_btn"):
            self._stop_btn.config(state="disabled", text="Stopping...")
        if hasattr(self, "_status_label"):
            self._status_label.config(text="Stopping after current cycle...", fg=FG_DIM)

    def _on_bot_finished(self):
        """Called on Tk thread when the bot worker has fully exited."""
        if hasattr(self, "_status_label"):
            self._status_label.config(text="Stopped.", fg=RED)
        if hasattr(self, "_stop_btn"):
            self._stop_btn.config(state="disabled", text="Stopped")
        if self._page_frame:
            _separator(self._page_frame)
            _make_btn(self._page_frame, "<- Back to Settings",
                      self._show_page_1, accent=True).pack(pady=4)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = SetupWizard()
    app.mainloop()
