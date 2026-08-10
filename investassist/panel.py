"""Always-on-top companion panel.

Sits beside thinkorswim and shows the verdict for whatever symbol you last
copied. Tkinter on purpose: it ships with Python, it has a real always-on-top
attribute on every platform, and a trading side panel does not justify an
Electron install.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import font as tkfont

from .engine import Engine, Recommendation
from .score import InsufficientData, Verdict
from .symbolwatch import ClipboardWatcher, tk_clipboard_reader

log = logging.getLogger(__name__)

BG = "#101418"
FG = "#e6edf3"
MUTED = "#7d8590"
PANEL = "#161b22"

VERDICT_COLOURS = {
    Verdict.STRONG_BUY: "#3fb950",
    Verdict.BUY: "#56d364",
    Verdict.HOLD: "#8b949e",
    Verdict.TRIM: "#d29922",
    Verdict.SELL: "#f85149",
}

POLL_MS = 400


class Panel:
    """The panel window. Scoring happens off the UI thread."""

    def __init__(self, engine: Engine, topmost: bool = True, poll_ms: int = POLL_MS):
        self.engine = engine
        self.poll_ms = poll_ms
        self._results: queue.Queue = queue.Queue()
        self._pending: set[str] = set()

        self.root = tk.Tk()
        self.root.title("InvestAssist")
        self.root.configure(bg=BG)
        self.root.geometry("380x560")
        self.root.minsize(320, 420)
        if topmost:
            self.root.attributes("-topmost", True)

        self._build_ui()

        self.watcher = ClipboardWatcher(
            read=tk_clipboard_reader(self.root),
            on_symbol=self.request,
        )

    # ------------------------------------------------------------------ ui
    def _build_ui(self) -> None:
        mono = tkfont.nametofont("TkFixedFont").actual()["family"]

        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=14, pady=(14, 6))

        self.symbol_label = tk.Label(
            header, text="—", bg=BG, fg=FG, font=(mono, 22, "bold"), anchor="w"
        )
        self.symbol_label.pack(side="left")

        self.price_label = tk.Label(
            header, text="", bg=BG, fg=MUTED, font=(mono, 12), anchor="e"
        )
        self.price_label.pack(side="right", pady=(10, 0))

        self.verdict_label = tk.Label(
            self.root, text="COPY A TICKER", bg=PANEL, fg=MUTED,
            font=(mono, 20, "bold"), pady=14,
        )
        self.verdict_label.pack(fill="x", padx=14)

        self.sub_label = tk.Label(
            self.root, text="in thinkorswim, then it appears here",
            bg=BG, fg=MUTED, font=(mono, 10), pady=6,
        )
        self.sub_label.pack(fill="x", padx=14)

        self.detail = tk.Text(
            self.root, bg=PANEL, fg=FG, font=(mono, 10), relief="flat",
            padx=12, pady=10, height=20, wrap="word", highlightthickness=0,
        )
        self.detail.pack(fill="both", expand=True, padx=14, pady=(6, 6))
        self.detail.configure(state="disabled")
        self.detail.tag_configure("muted", foreground=MUTED)
        self.detail.tag_configure("pos", foreground="#3fb950")
        self.detail.tag_configure("neg", foreground="#f85149")

        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill="x", padx=14, pady=(0, 12))
        self.status_label = tk.Label(
            footer, text="watching clipboard", bg=BG, fg=MUTED, font=(mono, 9), anchor="w"
        )
        self.status_label.pack(side="left")
        tk.Button(
            footer, text="Refresh", command=self._refresh_current,
            bg=PANEL, fg=FG, relief="flat", font=(mono, 9), padx=10,
        ).pack(side="right")

        self._current: str | None = None

    # --------------------------------------------------------------- logic
    def request(self, symbol: str, refresh: bool = False) -> None:
        """Queue a scoring job. Called from the clipboard watcher."""
        if symbol in self._pending:
            return
        self._pending.add(symbol)
        self._current = symbol
        self.symbol_label.configure(text=symbol)
        self.status_label.configure(text=f"scoring {symbol}…")
        threading.Thread(
            target=self._score, args=(symbol, refresh), daemon=True
        ).start()

    def _score(self, symbol: str, refresh: bool) -> None:
        """Runs on a worker thread — never touches Tk widgets directly."""
        try:
            self._results.put((symbol, self.engine.recommend(symbol, refresh=refresh), None))
        except InsufficientData as exc:
            self._results.put((symbol, None, str(exc)))
        except Exception as exc:
            log.exception("scoring %s failed", symbol)
            self._results.put((symbol, None, f"{type(exc).__name__}: {exc}"))

    def _refresh_current(self) -> None:
        if self._current:
            self._pending.discard(self._current)
            self.request(self._current, refresh=True)

    def _drain(self) -> None:
        while True:
            try:
                symbol, rec, error = self._results.get_nowait()
            except queue.Empty:
                break
            self._pending.discard(symbol)
            # A late result for a symbol you have moved on from is stale.
            if symbol != self._current:
                continue
            if error:
                self._render_error(symbol, error)
            else:
                self._render(rec)

    def _tick(self) -> None:
        self.watcher.poll()
        self._drain()
        self.root.after(self.poll_ms, self._tick)

    # -------------------------------------------------------------- render
    def _render_error(self, symbol: str, message: str) -> None:
        self.verdict_label.configure(text="NO DATA", fg=MUTED, bg=PANEL)
        self.sub_label.configure(text=message)
        self.price_label.configure(text="")
        self._set_detail([("", "muted")])
        self.status_label.configure(text="watching clipboard")

    def _render(self, rec: Recommendation) -> None:
        a = rec.assessment
        colour = VERDICT_COLOURS[a.verdict]

        self.verdict_label.configure(text=a.verdict.value, fg=colour, bg=PANEL)
        self.sub_label.configure(
            text=f"score {a.score:+.0f}   confidence {a.confidence:.0f}%   {a.regime.value}"
        )
        self.price_label.configure(text=f"{a.price:,.2f}")

        rows: list[tuple[str, str]] = []
        rows.append(("WHY\n", "muted"))
        for reason in a.rationale:
            rows.append((f"  • {reason}\n", ""))

        rows.append(("\nCOMPONENTS\n", "muted"))
        for name, value in sorted(
            a.contributions.items(), key=lambda kv: abs(kv[1]), reverse=True
        ):
            tag = "pos" if value > 0 else "neg" if value < 0 else "muted"
            rows.append((f"  {name:<16}{value:+7.1f}\n", tag))

        rows.append(("\nEXITS\n", "muted"))
        e = a.exits
        rows.append((f"  entry           {e.entry:>9,.2f}\n", ""))
        rows.append((f"  initial stop    {e.initial_stop:>9,.2f}\n", ""))
        rows.append((f"  risk/share      {e.risk_per_share:>9,.2f}\n", ""))
        for i, target in enumerate(e.targets, start=1):
            rows.append((f"  target {i}        {target:>9,.2f}\n", ""))
        if e.trail_active:
            rows.append((f"  trailing stop   {e.trail_stop:>9,.2f}\n", "pos"))
        rows.append((f"  time stop       {e.time_stop_bars:>9} bars\n", ""))

        if rec.size is not None:
            rows.append(("\nSIZE\n", "muted"))
            if rec.size.approved:
                rows.append((f"  {rec.size.shares} shares  ${rec.size.dollars:,.0f}\n", ""))
                rows.append((f"  risking ${rec.size.risk_dollars:,.0f}\n", ""))
                if rec.size.limited_by:
                    rows.append((f"  capped by {rec.size.limited_by}\n", "muted"))
            else:
                rows.append((f"  no position: {rec.size.limited_by}\n", "neg"))

        self._set_detail(rows)
        self.status_label.configure(text=f"updated {a.asof:%Y-%m-%d}")

    def _set_detail(self, rows: list[tuple[str, str]]) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        for text, tag in rows:
            self.detail.insert("end", text, tag)
        self.detail.configure(state="disabled")

    def run(self) -> None:
        self.root.after(self.poll_ms, self._tick)
        self.root.mainloop()


def launch(engine: Engine, topmost: bool = True) -> None:
    Panel(engine, topmost=topmost).run()
