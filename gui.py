"""
gui.py — CryptingAuto interactive startup wizard.

Run this instead of main.py to configure the bot with a graphical interface:

    python gui.py

The wizard walks you through three steps:
  1. Optional screen-region calibration  (same as --mark-regions)
  2. Crypt type selection                (Common / Rare / Epic / Arenas / Others)
  3. Level range                         (min 1–35, max 1–35, min ≤ max)

On finish the bot launches with those settings automatically.
"""

import copy
import logging
import multiprocessing
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

from config import ACCOUNTS, SETTINGS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_CRYPT_TYPES = ["Common", "Rare", "Epic", "Arenas", "Others"]

# Catppuccin Mocha-inspired palette
BG       = "#1e1e2e"
BG_ALT   = "#313244"
FG       = "#cdd6f4"
FG_DIM   = "#a6adc8"
ACCENT   = "#cba6f7"
ACCENT2  = "#89b4fa"
BTN_FLAT = "#45475a"


# ---------------------------------------------------------------------------
# Widget helpers
# ---------------------------------------------------------------------------

def _style_root(root: tk.Tk):
    root.configure(bg=BG)
    root.option_add("*Font", "\"Segoe UI\" 10")


def _title(parent: tk.Widget, text: str) -> tk.Label:
    lbl = tk.Label(parent, text=text, bg=BG, fg=ACCENT,
                   font=("Segoe UI", 15, "bold"), pady=6, anchor="w")
    lbl.pack(fill="x")
    return lbl


def _subtitle(parent: tk.Widget, text: str) -> tk.Label:
    lbl = tk.Label(parent, text=text, bg=BG, fg=FG_DIM,
                   font=("Segoe UI", 10), anchor="w", wraplength=400, justify="left")
    lbl.pack(fill="x", pady=(0, 10))
    return lbl


def _separator(parent: tk.Widget):
    tk.Frame(parent, bg=BTN_FLAT, height=1).pack(fill="x", pady=8)


def _make_btn(parent: tk.Widget, text: str, command, accent: bool = False) -> tk.Button:
    bg = ACCENT if accent else BTN_FLAT
    fg = BG     if accent else FG
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
    """Three-page setup wizard that configures and launches the bot."""

    def __init__(self):
        super().__init__()
        self.title("CryptingAuto — Setup Wizard")
        self.resizable(False, False)
        _style_root(self)

        # Mutable wizard state (seeded from config defaults)
        self._type_vars: dict[str, tk.BooleanVar] = {
            ct: tk.BooleanVar(value=(ct in SETTINGS.get("crypt_types", ["Common"])))
            for ct in ALL_CRYPT_TYPES
        }
        self.crypt_min = tk.IntVar(value=max(1, min(35, int(SETTINGS.get("crypt_min_level", 25)))))
        self.crypt_max = tk.IntVar(value=max(1, min(35, int(SETTINGS.get("crypt_max_level", 25)))))

        self._page_frame: tk.Frame | None = None
        self._show_page_1()

    # ── Page management ────────────────────────────────────────────────────

    def _clear_page(self):
        if self._page_frame:
            self._page_frame.destroy()
        self._page_frame = tk.Frame(self, bg=BG, padx=32, pady=22)
        self._page_frame.pack(fill="both", expand=True)
        return self._page_frame

    def _footer_buttons(self, parent: tk.Widget, back_cmd=None, fwd_text="Next →",
                        fwd_cmd=None, fwd_accent=True):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(12, 0))
        if back_cmd:
            _make_btn(row, "← Back", back_cmd).pack(side="left")
        if fwd_cmd:
            _make_btn(row, fwd_text, fwd_cmd, accent=fwd_accent).pack(side="right")

    # ── Page 1 — Region calibration ───────────────────────────────────────

    def _show_page_1(self):
        f = self._clear_page()

        _title(f, "Step 1 of 3 — Screen Regions")
        _subtitle(f,
            "If this is your first run, or your game window has moved/resized, "
            "calibrate the screen regions so the bot knows exactly where to look.\n\n"
            "Clicking \"Calibrate\" opens the game browser and lets you draw boxes "
            "over each UI element using the same Enter / s / q controls as before. "
            "You can skip this step if your regions are already saved."
        )
        _separator(f)

        btn_row = tk.Frame(f, bg=BG)
        btn_row.pack(pady=6)
        _make_btn(btn_row, "  Calibrate Regions  ", self._run_calibration, accent=True).pack(side="left", padx=(0, 10))
        _make_btn(btn_row, "Skip →", self._show_page_2).pack(side="left")

    def _run_calibration(self):
        name = ACCOUNTS[0]["name"] if ACCOUNTS else "1020"
        # Launch the interactive mark-regions flow in a new console window.
        # We block until it finishes, then advance to page 2.
        subprocess.run(
            [sys.executable, "main.py", "--mark-regions", name],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        self._show_page_2()

    # ── Page 2 — Crypt types ──────────────────────────────────────────────

    def _show_page_2(self):
        f = self._clear_page()

        _title(f, "Step 2 of 3 — Crypt Types")
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
        self._footer_buttons(f,
            back_cmd=self._show_page_1,
            fwd_text="Next →", fwd_cmd=self._advance_from_page_2)

    def _advance_from_page_2(self):
        selected = [ct for ct, var in self._type_vars.items() if var.get()]
        if not selected:
            messagebox.showwarning("Nothing selected",
                                   "Please select at least one crypt type before continuing.")
            return
        self._show_page_3()

    # ── Page 3 — Level range ──────────────────────────────────────────────

    def _show_page_3(self):
        f = self._clear_page()

        _title(f, "Step 3 of 3 — Level Range")
        _subtitle(f,
            "Choose the minimum and maximum crypt level to target (1 – 35).\n"
            "Set both to the same value to farm one specific level only.\n"
            "Min must be ≤ Max."
        )
        _separator(f)

        grid = tk.Frame(f, bg=BG)
        grid.pack(anchor="w", pady=8)

        def _row_label(text, row):
            tk.Label(grid, text=text, bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 10), anchor="e").grid(
                row=row, column=0, sticky="e", padx=(0, 12), pady=6)

        def _spinbox(row, var):
            sb = tk.Spinbox(
                grid, from_=1, to=35, width=5,
                textvariable=var,
                font=("Segoe UI", 12), bg=BG_ALT, fg=FG,
                buttonbackground=BTN_FLAT, relief="flat",
                insertbackground=FG,
            )
            sb.grid(row=row, column=1, sticky="w")
            return sb

        _row_label("Min level:", 0)
        _spinbox(0, self.crypt_min)

        _row_label("Max level:", 1)
        _spinbox(1, self.crypt_max)

        # Live validation hint
        self._level_hint = tk.Label(f, text="", bg=BG, fg="#f38ba8",  # red
                                    font=("Segoe UI", 9))
        self._level_hint.pack(anchor="w")

        def _on_change(*_):
            try:
                mn, mx = self.crypt_min.get(), self.crypt_max.get()
                if mn > mx:
                    self._level_hint.config(text=f"⚠  Min ({mn}) cannot exceed Max ({mx})")
                else:
                    self._level_hint.config(text="")
            except tk.TclError:
                pass

        self.crypt_min.trace_add("write", _on_change)
        self.crypt_max.trace_add("write", _on_change)

        _separator(f)
        self._footer_buttons(f,
            back_cmd=self._show_page_2,
            fwd_text="▶  Start Bot", fwd_cmd=self._launch_bot, fwd_accent=True)

    # ── Launch ────────────────────────────────────────────────────────────

    def _launch_bot(self):
        try:
            mn = self.crypt_min.get()
            mx = self.crypt_max.get()
        except tk.TclError:
            messagebox.showerror("Invalid input", "Level values must be whole numbers.")
            return

        if not (1 <= mn <= 35 and 1 <= mx <= 35):
            messagebox.showwarning("Out of range", "Both levels must be between 1 and 35.")
            return
        if mn > mx:
            messagebox.showwarning("Invalid range",
                                   f"Min level ({mn}) cannot be greater than max level ({mx}).")
            return

        selected_types = [ct for ct, var in self._type_vars.items() if var.get()]

        # Build final settings
        settings = copy.deepcopy(SETTINGS)
        settings["crypt_types"]    = selected_types
        settings["crypt_min_level"] = mn
        settings["crypt_max_level"] = mx

        # Close the wizard window before launching
        self.destroy()

        # ---------------------------------------------------------------------------
        # Launch bot (mirrors the logic in main.py)
        # ---------------------------------------------------------------------------
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(process)d %(message)s",
            handlers=[
                logging.StreamHandler(sys.stdout),
                logging.FileHandler("cryptbot.log", encoding="utf-8"),
            ],
        )

        import main as _main

        stagger = settings["stagger_start_seconds"]
        tasks = [
            (account, settings, i * stagger)
            for i, account in enumerate(ACCOUNTS)
        ]

        logging.getLogger(__name__).info(
            "Wizard complete — launching %d bot(s). types=%s levels=%d–%d",
            len(tasks), selected_types, mn, mx,
        )

        if len(tasks) == 1:
            _main._run_account(tasks[0])
        else:
            with multiprocessing.Pool(processes=len(tasks)) as pool:
                pool.map(_main._run_account, tasks)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Windows multiprocessing spawn guard
    multiprocessing.freeze_support()

    app = SetupWizard()
    app.mainloop()
