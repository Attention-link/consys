#!/usr/bin/env python3
"""Podglad logow z ekranu "Test polaczenia" (gs.py / drone.py) - Windows, tkinter.

Log z testu to zwykly plik tekstowy rozdzielony srednikami, wiec otworzy sie
i w notatniku, i w arkuszu - ale z samych liczb nie widac, jak zachowywal sie
link w czasie. Ten program rysuje to na wykresach na wspolnej osi czasu: RSSI
kazdej anteny osobno, SNR, modulacje, straty i ping. Naglowek testu (kanal,
region, moc, modulacja nadawania, karty) i podsumowanie ida do panelu obok.

Potrzebny jest tylko Python - zadnych bibliotek do doinstalowania.

Uzycie na Windowsie:
    - przeciagnij plik test-gs-....log na podglad_testu.py, albo
    - kliknij dwa razy w podglad_testu.py - otworzy najnowszy log z katalogu,
      w ktorym lezy ten plik, albo
    - python podglad_testu.py <plik.log>
"""

import math
import re
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

BG = "#ffffff"
PANEL_BG = "#fbfbfd"
GRID = "#e7e9ee"
AXIS = "#9aa0a6"
TEXT = "#202124"
MUTED = "#5f6368"
SERIES_COLORS = ("#1a73e8", "#e8710a", "#12a150", "#a142f4", "#00838f", "#c5221f")

# Progi te same, co w ocenie na ekranie testu w gs.py/drone.py - zeby ten sam
# sygnal nie byl tu "zielony", a tam "slaby".
RSSI_BANDS = ((-65, 0, "#eaf6ec"), (-75, -65, "#fff6e0"), (-200, -75, "#fdeceb"))
LOSS_BANDS = ((0, 0.5, "#eaf6ec"), (0.5, 3, "#fff6e0"), (3, 1000, "#fdeceb"))

# Tytul i legenda kazdego panelu ida NAD jego ramke - w srodku zaslanialyby
# wykres, a przy niskich panelach (modulacja) linia potrafi isc gora.
PAD_L, PAD_R, PAD_T, PAD_B = 74, 18, 26, 34
PANEL_GAP = 26
PANEL_MIN_H = 62
TITLE_DY = 9


# ------------------------- czytanie logu -------------------------

def _to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class TestLog:
    """Rozlozony na czesci plik z testu: naglowek, probki, zdarzenia
    (komentarze w srodku pliku) i podsumowanie."""

    def __init__(self, path):
        self.path = Path(path)
        self.header = []
        self.summary = []
        self.events = []     # (sekunda, tekst)
        self.columns = []
        self.samples = []
        self.antennas = []   # nazwy w kolejnosci pojawienia sie
        self._parse()

    def _parse(self):
        in_summary = False
        last_sec = 0.0
        with self.path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue

                if line.startswith("#"):
                    text = line.lstrip("#").strip()
                    if not text:
                        continue
                    if text.startswith("---") or "podsumowanie" in text.lower():
                        in_summary = True
                        continue
                    if in_summary:
                        self.summary.append(text)
                    elif self.columns:
                        # komentarz miedzy probkami = cos sie w trakcie stalo
                        self.events.append((last_sec, text))
                    else:
                        self.header.append(text)
                    continue

                if not self.columns:
                    self.columns = [c.strip() for c in line.split(";")]
                    continue

                row = dict(zip(self.columns, line.split(";")))
                sample = {name: _to_float(row.get(name)) for name in self.columns}
                sample["czas"] = row.get("czas", "")
                sample["anteny"] = self._parse_antennas(row.get("anteny_rssi", ""))
                if sample.get("sek") is None:
                    sample["sek"] = last_sec + 1
                last_sec = sample["sek"]
                self.samples.append(sample)

    def _parse_antennas(self, text):
        """'gs_wfb:ant0=-61 drone_TX:ant1=-77' -> {'gs_wfb ant0': -61.0, ...}"""
        out = {}
        for chunk in (text or "").split():
            m = re.match(r"(.+)=(-?\d+(?:\.\d+)?)$", chunk)
            if not m:
                continue
            name = m.group(1).replace(":", " ")
            out[name] = float(m.group(2))
            if name not in self.antennas:
                self.antennas.append(name)
        return out

    # --- dostep do danych ---

    def span(self):
        if not self.samples:
            return 0.0, 1.0
        first, last = self.samples[0]["sek"], self.samples[-1]["sek"]
        return (first, last) if last > first else (first, first + 1)

    def series(self, column):
        return [(s["sek"], s[column]) for s in self.samples
                if s.get(column) is not None]

    def antenna_series(self, name):
        return [(s["sek"], s["anteny"][name]) for s in self.samples
                if name in s["anteny"]]

    def has(self, column):
        return any(s.get(column) is not None for s in self.samples)

    def stat(self, column):
        """(min, srednia, max) albo None - do panelu z podsumowaniem."""
        values = [v for _, v in self.series(column)]
        if not values:
            return None
        return min(values), sum(values) / len(values), max(values)


# ------------------------- rysowanie -------------------------

def nice_step(span, target=5):
    """Odstep miedzy kreskami osi: 1, 2, 2.5, 5, 10 razy potega dziesiatki -
    zeby na osi stawaly okragle liczby, a nie 3.7 albo 812."""
    if span <= 0:
        return 1.0
    raw = span / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 2.5, 5):
        if raw <= mag * mult:
            return mag * mult
    return mag * 10


def nice_time_step(span, target=8):
    """To samo co nice_step, ale dla czasu: 15 albo 30 sekund czyta sie duzo
    lepiej niz "co 25 s", ktore wyszloby z potegi dziesiatki."""
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
        if span / step <= target:
            return step
    return 3600


def fmt_time(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60:d}:{seconds % 60:02d}"


class Panel:
    """Jeden wykres na wspolnym plotnie: ma swoj prostokat, zakres osi Y
    i liste serii (etykieta, kolor, punkty)."""

    def __init__(self, key, title, weight, series, unit="", bands=(),
                 step=False, y_fmt="{:.0f}", y_floor=None):
        self.key = key
        self.title = title
        self.weight = weight
        self.series = series
        self.unit = unit
        self.bands = bands
        self.step = step
        self.y_fmt = y_fmt
        self.y_floor = y_floor  # np. straty i ping nie schodza ponizej zera
        self.box = (0, 0, 0, 0)
        self.lo, self.hi = 0.0, 1.0

    def compute_range(self):
        values = [v for _, _, points in self.series for _, v in points]
        if not values:
            self.lo, self.hi = 0.0, 1.0
            return
        lo, hi = min(values), max(values)
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.12
        self.lo, self.hi = lo - pad, hi + pad
        if self.y_floor is not None:
            self.lo = min(self.lo, self.y_floor) if lo < self.y_floor else self.y_floor

    def y_at(self, value):
        x0, y0, x1, y1 = self.box
        frac = (value - self.lo) / (self.hi - self.lo)
        return y1 - frac * (y1 - y0)


class ChartArea:
    """Plotno z panelami jeden pod drugim, wspolna os czasu i krzyzykiem
    pokazujacym wartosci pod kursorem."""

    def __init__(self, canvas):
        self.canvas = canvas
        self.log = None
        self.panels = []
        self.t0, self.t1 = 0.0, 1.0
        self.x0, self.x1 = PAD_L, PAD_L + 1
        self.top, self.bottom = PAD_T, PAD_T
        canvas.bind("<Configure>", lambda e: self.redraw())
        canvas.bind("<Motion>", self._on_motion)
        canvas.bind("<Leave>", lambda e: self.canvas.delete("kursor"))

    def set_log(self, log):
        self.log = log
        self.redraw()

    # --- budowa paneli ---

    def _build_panels(self):
        log = self.log
        panels = []

        ant_series = [(name, SERIES_COLORS[i % len(SERIES_COLORS)], log.antenna_series(name))
                      for i, name in enumerate(log.antennas)]
        ant_series = [s for s in ant_series if len(s[2]) > 1]
        if not ant_series and log.has("rssi_best_dBm"):
            ant_series = [("najlepsza antena", SERIES_COLORS[0], log.series("rssi_best_dBm"))]
        elif ant_series and log.has("rssi_best_dBm") and len(ant_series) > 1:
            ant_series.append(("najlepsza", "#202124", log.series("rssi_best_dBm")))
        if ant_series:
            panels.append(Panel("rssi", "Sygnal RSSI [dBm]", 3, ant_series,
                                unit="dBm", bands=RSSI_BANDS))

        if log.has("snr_best_dB"):
            panels.append(Panel("snr", "SNR [dB]", 2,
                                [("SNR", SERIES_COLORS[2], log.series("snr_best_dB"))],
                                unit="dB"))
        if log.has("rx_mcs"):
            panels.append(Panel("mcs", "Modulacja (MCS odbioru)", 1,
                                [("MCS", SERIES_COLORS[1], log.series("rx_mcs"))],
                                step=True))
        if log.has("straty_%"):
            panels.append(Panel("loss", "Straty pakietow [%]", 2,
                                [("straty", SERIES_COLORS[5], log.series("straty_%"))],
                                unit="%", bands=LOSS_BANDS, y_fmt="{:.1f}", y_floor=0))
        if log.has("rx_Mbit_s"):
            panels.append(Panel("mbit", "Przeplyw odbioru [Mbit/s]", 2,
                                [("Mbit/s", SERIES_COLORS[4], log.series("rx_Mbit_s"))],
                                unit="Mbit/s", y_fmt="{:.1f}", y_floor=0))
        if log.has("ping_ms"):
            panels.append(Panel("ping", "Ping przez tunel [ms]", 2,
                                [("ping", SERIES_COLORS[3], log.series("ping_ms"))],
                                unit="ms", y_fmt="{:.0f}", y_floor=0))
        return panels

    # --- rysowanie ---

    def redraw(self):
        c = self.canvas
        c.delete("all")
        width = c.winfo_width()
        height = c.winfo_height()
        if width < 60 or height < 60:
            return

        if not self.log or len(self.log.samples) < 2:
            msg = ("Przeciagnij plik test-*.log na podglad_testu.py\n"
                   "albo kliknij \"Otworz plik...\"")
            if self.log:
                msg = f"Za malo danych w {self.log.path.name}\n(potrzebne co najmniej dwie probki)"
            c.create_text(width // 2, height // 2, text=msg, fill=MUTED,
                          font=("Segoe UI", 11), justify="center")
            return

        self.panels = self._build_panels()
        if not self.panels:
            c.create_text(width // 2, height // 2, text="Log nie zawiera zadnych wartosci",
                          fill=MUTED, font=("Segoe UI", 11))
            return

        weights = sum(p.weight for p in self.panels)
        gaps = PANEL_GAP * (len(self.panels) - 1)
        # przy malym oknie panele nie moga sie zlac w paski - wtedy plotno
        # robi sie wyzsze niz widok i wchodzi suwak
        need = PANEL_MIN_H * len(self.panels) + gaps + PAD_T + PAD_B
        total = max(height, need)
        c.configure(scrollregion=(0, 0, width, total))

        self.t0, self.t1 = self.log.span()
        self.x0, self.x1 = PAD_L, max(PAD_L + 10, width - PAD_R)
        usable = total - PAD_T - PAD_B - gaps
        self.top, self.bottom = PAD_T, total - PAD_B

        y = PAD_T
        for i, panel in enumerate(self.panels):
            h = usable * panel.weight / weights
            panel.box = (self.x0, y, self.x1, y + h)
            panel.compute_range()
            self._draw_panel(panel, last=(i == len(self.panels) - 1))
            y += h + PANEL_GAP

    def x_at(self, t):
        return self.x0 + (t - self.t0) / (self.t1 - self.t0) * (self.x1 - self.x0)

    def _draw_panel(self, panel, last):
        c = self.canvas
        x0, y0, x1, y1 = panel.box

        c.create_rectangle(x0, y0, x1, y1, fill=PANEL_BG, outline="")
        for lo, hi, color in panel.bands:
            top = panel.y_at(min(hi, panel.hi))
            bottom = panel.y_at(max(lo, panel.lo))
            if bottom > top:
                c.create_rectangle(x0, max(y0, top), x1, min(y1, bottom),
                                   fill=color, outline="")

        step = nice_step(panel.hi - panel.lo, 4)
        value = math.ceil(panel.lo / step) * step
        while value <= panel.hi:
            y = panel.y_at(value)
            c.create_line(x0, y, x1, y, fill=GRID)
            c.create_text(x0 - 8, y, text=panel.y_fmt.format(value), anchor="e",
                          fill=MUTED, font=("Segoe UI", 8))
            value += step

        t_step = nice_time_step(self.t1 - self.t0)
        t = math.ceil(self.t0 / t_step) * t_step
        while t <= self.t1:
            x = self.x_at(t)
            c.create_line(x, y0, x, y1, fill=GRID)
            if last:
                c.create_text(x, y1 + 14, text=fmt_time(t), fill=MUTED,
                              font=("Segoe UI", 8))
            t += t_step

        for sec, _text in self.log.events:
            x = self.x_at(sec)
            c.create_line(x, y0, x, y1, fill="#b0b6c0", dash=(3, 3))

        c.create_rectangle(x0, y0, x1, y1, outline=AXIS)

        for label, color, points in panel.series:
            coords = self._line_coords(panel, points)
            if len(coords) >= 4:
                c.create_line(*coords, fill=color, width=2, joinstyle="round",
                              capstyle="round")

        ty = y0 - TITLE_DY
        c.create_text(x0, ty, text=panel.title, anchor="w", fill=TEXT,
                      font=("Segoe UI", 9, "bold"))

        if len(panel.series) > 1:
            lx = x1
            title_end = x0 + 8 + 7 * len(panel.title)
            for label, color, _points in reversed(panel.series):
                if lx - (7 * len(label) + 34) < title_end:
                    break  # w waskim oknie legenda nie moze wejsc na tytul
                c.create_text(lx, ty, text=label, anchor="e", fill=MUTED,
                              font=("Segoe UI", 8))
                lx -= 7 * len(label) + 8
                c.create_line(lx - 14, ty, lx - 2, ty, fill=color, width=3)
                lx -= 26

        if last:
            c.create_text((x0 + x1) / 2, y1 + 28, text="czas testu [min:s]",
                          fill=MUTED, font=("Segoe UI", 8))

    def _line_coords(self, panel, points):
        coords = []
        prev_y = None
        for t, value in points:
            x, y = self.x_at(t), panel.y_at(value)
            if panel.step and prev_y is not None:
                coords.extend([x, prev_y])  # schodek: najpierw w bok, potem w gore
            coords.extend([x, y])
            prev_y = y
        return coords

    # --- krzyzyk z odczytem ---

    def _on_motion(self, event):
        c = self.canvas
        c.delete("kursor")
        if not self.log or not self.panels:
            return
        x = c.canvasx(event.x)
        if not (self.x0 <= x <= self.x1):
            return

        t = self.t0 + (x - self.x0) / (self.x1 - self.x0) * (self.t1 - self.t0)
        sample = min(self.log.samples, key=lambda s: abs(s["sek"] - t))
        sx = self.x_at(sample["sek"])
        c.create_line(sx, self.top, sx, self.bottom, fill="#5f6368", dash=(2, 2),
                      tags="kursor")

        for panel in self.panels:
            for _label, color, points in panel.series:
                value = next((v for tt, v in points if tt == sample["sek"]), None)
                if value is None:
                    continue
                y = panel.y_at(value)
                c.create_oval(sx - 3, y - 3, sx + 3, y + 3, fill=color, outline=BG,
                              tags="kursor")

        self._draw_readout(sample, sx, c.canvasy(event.y))

    def _readout_lines(self, sample):
        lines = [f"{sample.get('czas') or ''}   {fmt_time(sample['sek'])} od startu"]
        for name in self.log.antennas:
            if name in sample["anteny"]:
                lines.append(f"{name}: {sample['anteny'][name]:.0f} dBm")
        for column, label, fmt in (("rssi_best_dBm", "najlepszy sygnal", "{:.0f} dBm"),
                                   ("snr_best_dB", "SNR", "{:.0f} dB"),
                                   ("rx_mcs", "MCS", "{:.0f}"),
                                   ("straty_%", "straty", "{:.1f} %"),
                                   ("rx_Mbit_s", "przeplyw", "{:.2f} Mbit/s"),
                                   ("ping_ms", "ping", "{:.1f} ms")):
            value = sample.get(column)
            if value is not None:
                lines.append(f"{label}: {fmt.format(value)}")
        return lines

    def _draw_readout(self, sample, sx, sy):
        c = self.canvas
        lines = self._readout_lines(sample)
        width = 9 * max(len(ln) for ln in lines) + 16
        height = 15 * len(lines) + 12
        x = sx + 14 if sx + 14 + width < self.x1 else sx - 14 - width
        y = min(max(self.top, sy - height / 2), self.bottom - height)

        c.create_rectangle(x, y, x + width, y + height, fill="#ffffff",
                           outline="#c8ccd4", tags="kursor")
        for i, text in enumerate(lines):
            c.create_text(x + 8, y + 12 + i * 15, text=text, anchor="w", fill=TEXT,
                          font=("Consolas", 8, "bold" if i == 0 else "normal"),
                          tags="kursor")


# ------------------------- okno -------------------------

class App(tk.Tk):
    def __init__(self, path=None):
        super().__init__()
        self.title("WFB-NG - podglad testu polaczenia")
        # na malym ekranie (laptop 1366x768) okno w stalym rozmiarze wychodzi
        # poza pulpit i chowa dolna os wykresu
        width = min(1200, self.winfo_screenwidth() - 80)
        height = min(800, self.winfo_screenheight() - 110)
        self.geometry(f"{width}x{height}+30+20")
        self.minsize(820, 520)
        self.log = None

        self._build_menu()
        self._build_widgets()

        if path:
            self.load(path)
        else:
            newest = self.newest_log()
            if newest:
                self.load(newest)

    # --- budowa okna ---

    def _build_menu(self):
        menu = tk.Menu(self)
        plik = tk.Menu(menu, tearoff=0)
        plik.add_command(label="Otworz...", accelerator="Ctrl+O", command=self.ask_open)
        plik.add_command(label="Odswiez", accelerator="F5", command=self.reload)
        plik.add_separator()
        plik.add_command(label="Zamknij", command=self.destroy)
        menu.add_cascade(label="Plik", menu=plik)
        self.config(menu=menu)
        self.bind("<Control-o>", lambda e: self.ask_open())
        self.bind("<F5>", lambda e: self.reload())

    def _build_widgets(self):
        self.configure(bg=BG)
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="Otworz plik...", command=self.ask_open).pack(side="left")
        ttk.Button(bar, text="Odswiez", command=self.reload).pack(side="left", padx=6)
        self.file_label = ttk.Label(bar, text="(brak pliku)", foreground=MUTED)
        self.file_label.pack(side="left", padx=12)

        # pasek stanu pakowany przed trescia, inaczej rozciagajaca sie ramka
        # z wykresami zabiera mu miejsce i tekst zostaje przyciety
        self.status = ttk.Label(self, text="", foreground=MUTED, anchor="w",
                                padding=(12, 5))
        self.status.pack(side="bottom", fill="x")

        body = ttk.Frame(self, padding=(10, 0, 10, 8))
        body.pack(fill="both", expand=True)

        side = ttk.Frame(body, width=366)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        info_box = ttk.LabelFrame(side, text="Warunki testu i podsumowanie", padding=6)
        info_box.pack(fill="both", expand=True)
        self.info = tk.Text(info_box, width=40, wrap="word", relief="flat",
                            bg="#f7f8fa", fg=TEXT, font=("Consolas", 9),
                            padx=6, pady=6)
        scroll = ttk.Scrollbar(info_box, command=self.info.yview)
        self.info.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        self.info.pack(side="left", fill="both", expand=True)
        self.info.tag_configure("naglowek", font=("Segoe UI", 9, "bold"),
                                spacing1=6, spacing3=2)

        chart_box = ttk.Frame(body)
        chart_box.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.canvas = tk.Canvas(chart_box, bg=BG, highlightthickness=1,
                                highlightbackground="#d8dbe2")
        vbar = ttk.Scrollbar(chart_box, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.chart = ChartArea(self.canvas)

    # --- dane ---

    def newest_log(self):
        """Bez argumentu otwieramy najswiezszy log z katalogu programu - po to,
        zeby dalo sie po prostu kliknac w plik i zobaczyc ostatni test."""
        folder = Path(__file__).resolve().parent
        logs = sorted(folder.glob("test-*.log"), key=lambda p: p.stat().st_mtime)
        return logs[-1] if logs else None

    def ask_open(self):
        path = filedialog.askopenfilename(
            title="Wybierz log z testu",
            initialdir=str(Path(__file__).resolve().parent),
            filetypes=[("Logi testu", "test-*.log"), ("Pliki log", "*.log"),
                       ("Wszystkie pliki", "*.*")])
        if path:
            self.load(path)

    def reload(self):
        if self.log:
            self.load(self.log.path)

    def load(self, path):
        try:
            log = TestLog(path)
        except OSError as e:
            messagebox.showerror("Nie moge otworzyc pliku", str(e))
            return
        if not log.samples:
            messagebox.showwarning(
                "Pusty log",
                f"{Path(path).name} nie zawiera ani jednej probki.\n"
                "Czy na pewno to plik z ekranu \"Test polaczenia\"?")

        self.log = log
        self.title(f"{Path(path).name} - podglad testu polaczenia")
        self.file_label.configure(text=log.path.name)
        self._fill_info()
        self.chart.set_log(log)
        span = log.span()
        self.status.configure(
            text=f"Probek: {len(log.samples)}   czas: {fmt_time(span[1] - span[0])}"
                 f"   anteny: {', '.join(log.antennas) or 'brak'}"
                 f"   zdarzenia: {len(log.events)}   |   {log.path}")

    def _fill_info(self):
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")

        def block(title, lines):
            if not lines:
                return
            self.info.insert("end", title + "\n", "naglowek")
            for line in lines:
                self.info.insert("end", line + "\n")

        block("Warunki testu", self.log.header)

        stats = []
        for column, label, fmt in (("rssi_best_dBm", "RSSI dBm", "{:.0f}"),
                                   ("snr_best_dB", "SNR dB", "{:.0f}"),
                                   ("straty_%", "straty %", "{:.1f}"),
                                   ("rx_Mbit_s", "Mbit/s", "{:.2f}"),
                                   ("ping_ms", "ping ms", "{:.1f}")):
            stat = self.log.stat(column)
            if stat:
                lo, avg, hi = stat
                stats.append(f"{label:<9}{fmt.format(lo):>7} /{fmt.format(avg):>7} /"
                             f"{fmt.format(hi):>7}")
        if stats:
            stats.insert(0, f"{'':<9}{'min':>7} /{'srednio':>7} /{'max':>7}")
        mcs = sorted({int(v) for _, v in self.log.series("rx_mcs")})
        if mcs:
            stats.append("MCS      " + ", ".join(str(m) for m in mcs))
        block("Policzone z probek", stats)

        block("Podsumowanie z pliku", self.log.summary)
        block("Zdarzenia w trakcie",
              [f"{fmt_time(sec)}  {text}" for sec, text in self.log.events])

        self.info.configure(state="disabled")


def main():
    path = None
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        if not path.exists():
            # komunikat i na konsole, i w okienku - po zmianie rozszerzenia na
            # .pyw (zeby nie wyskakiwalo czarne okno) konsoli po prostu nie ma
            print(f"Nie ma takiego pliku: {path}")
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Nie ma takiego pliku", str(path))
            root.destroy()
            return 2
    App(path).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
