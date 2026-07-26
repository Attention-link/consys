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
        self.label = self.path.stem
        self.header = []
        self.summary = []
        self.events = []     # (sekunda, tekst)
        self.columns = []
        self.samples = []
        self.antennas = []   # nazwy w kolejnosci pojawienia sie
        self._parse()
        # Przy porownywaniu kilku testow liczy sie czas OD POCZATKU kazdego
        # z nich, a nie zegar - inaczej dwa przeloty zrobione o roznych porach
        # leza na wykresie obok siebie zamiast jeden na drugim.
        self.t0 = self.samples[0]["sek"] if self.samples else 0.0

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

    def duration(self):
        first, last = self.span()
        return last - first

    def series(self, column):
        return [(s["sek"] - self.t0, s[column]) for s in self.samples
                if s.get(column) is not None]

    def antenna_series(self, name):
        return [(s["sek"] - self.t0, s["anteny"][name]) for s in self.samples
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


def as_paths(value):
    """Jedna sciezka albo kilka - zawsze lista. Bez tego pojedynczy napis
    rozsypalby sie na pojedyncze znaki, bo str tez jest iterowalny."""
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [Path(value)]
    return [Path(p) for p in value]


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
        self.logs = []
        self.panels = []
        self.t0, self.t1 = 0.0, 1.0
        self.x0, self.x1 = PAD_L, PAD_L + 1
        self.top, self.bottom = PAD_T, PAD_T
        canvas.bind("<Configure>", lambda e: self.redraw())
        canvas.bind("<Motion>", self._on_motion)
        canvas.bind("<Leave>", lambda e: self.canvas.delete("kursor"))

    def set_logs(self, logs):
        self.logs = list(logs)
        self.redraw()

    def log_color(self, index):
        return SERIES_COLORS[index % len(SERIES_COLORS)]

    # --- budowa paneli ---

    def _build_panels(self):
        """Przy jednym pliku kolor oznacza antene/wielkosc, przy kilku - PLIK.
        Inaczej przy porownaniu nie dalo by sie odroznic, ktora krzywa jest
        z ktorego testu, a o to w porownaniu chodzi."""
        multi = len(self.logs) > 1
        panels = []

        rssi = []
        for i, log in enumerate(self.logs):
            color = self.log_color(i)
            if multi:
                rssi.append((log.label, color, log.series("rssi_best_dBm")))
                continue
            for j, name in enumerate(log.antennas):
                points = log.antenna_series(name)
                if len(points) > 1:
                    rssi.append((name, SERIES_COLORS[j % len(SERIES_COLORS)], points))
            if not rssi and log.has("rssi_best_dBm"):
                rssi.append(("najlepsza antena", color, log.series("rssi_best_dBm")))
            elif len(rssi) > 1 and log.has("rssi_best_dBm"):
                rssi.append(("najlepsza", "#202124", log.series("rssi_best_dBm")))
        rssi = [s for s in rssi if len(s[2]) > 1]
        if rssi:
            panels.append(Panel("rssi", "Sygnal RSSI [dBm]", 3, rssi,
                                unit="dBm", bands=RSSI_BANDS))

        for key, title, weight, column, opts in (
                ("snr", "SNR [dB]", 2, "snr_best_dB", {"unit": "dB"}),
                ("mcs", "Modulacja (MCS odbioru)", 1, "rx_mcs", {"step": True}),
                ("loss", "Straty pakietow [%]", 2, "straty_%",
                 {"unit": "%", "bands": LOSS_BANDS, "y_fmt": "{:.1f}", "y_floor": 0}),
                ("mbit", "Przeplyw odbioru [Mbit/s]", 2, "rx_Mbit_s",
                 {"unit": "Mbit/s", "y_fmt": "{:.1f}", "y_floor": 0}),
                ("ping", "Ping przez tunel [ms]", 2, "ping_ms",
                 {"unit": "ms", "y_floor": 0})):
            default_color = {"snr": SERIES_COLORS[2], "mcs": SERIES_COLORS[1],
                             "loss": SERIES_COLORS[5], "mbit": SERIES_COLORS[4],
                             "ping": SERIES_COLORS[3]}[key]
            series = []
            for i, log in enumerate(self.logs):
                if not log.has(column):
                    continue
                series.append((log.label if multi else title.split(" [")[0],
                               self.log_color(i) if multi else default_color,
                               log.series(column)))
            if series:
                panels.append(Panel(key, title, weight, series, **opts))
        return panels

    # --- rysowanie ---

    def redraw(self):
        c = self.canvas
        c.delete("all")
        width = c.winfo_width()
        height = c.winfo_height()
        if width < 60 or height < 60:
            return

        usable_logs = [log for log in self.logs if len(log.samples) >= 2]
        if not usable_logs:
            msg = ("Przeciagnij plik (albo kilka na raz) test-*.log\n"
                   "na podglad_testu.py, albo kliknij \"Otworz pliki...\"")
            if self.logs:
                msg = ("Za malo danych w: "
                       + ", ".join(log.path.name for log in self.logs)
                       + "\n(potrzebne co najmniej dwie probki)")
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

        # wspolna os: od zera do najdluzszego z porownywanych testow
        self.t0, self.t1 = 0.0, max(log.duration() for log in usable_logs) or 1.0
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

        # Zdarzenia rysujemy tylko przy jednym pliku - przy porownaniu kreski
        # z kilku testow zlewaja sie w plot i nie wiadomo, czyja jest ktora.
        if len(self.logs) == 1:
            for sec, _text in self.logs[0].events:
                x = self.x_at(sec - self.logs[0].t0)
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
        usable = [log for log in self.logs if log.samples]
        if not usable or not self.panels:
            return
        x = c.canvasx(event.x)
        if not (self.x0 <= x <= self.x1):
            return

        t = self.t0 + (x - self.x0) / (self.x1 - self.x0) * (self.t1 - self.t0)
        sx = self.x_at(t)
        c.create_line(sx, self.top, sx, self.bottom, fill="#5f6368", dash=(2, 2),
                      tags="kursor")

        # przy porownaniu kazdy plik ma w tej chwili wlasna probke - kropki
        # i odczyt pokazuja je wszystkie
        picks = []
        for i, log in enumerate(usable):
            sample = min(log.samples, key=lambda s: abs(s["sek"] - log.t0 - t))
            if abs(sample["sek"] - log.t0 - t) <= max(2.0, self.t1 / 200):
                picks.append((i, log, sample))

        for panel in self.panels:
            for i, log, sample in picks:
                rel = sample["sek"] - log.t0
                for _label, color, points in panel.series:
                    value = next((v for tt, v in points if abs(tt - rel) < 1e-6), None)
                    if value is None:
                        continue
                    y = panel.y_at(value)
                    c.create_oval(self.x_at(rel) - 3, y - 3, self.x_at(rel) + 3, y + 3,
                                  fill=color, outline=BG, tags="kursor")

        self._draw_readout(picks, sx, c.canvasy(event.y))

    READOUT = (("rssi_best_dBm", "najlepszy sygnal", "{:.0f} dBm", "{:.0f} dBm"),
               ("snr_best_dB", "SNR", "{:.0f} dB", "SNR {:.0f}"),
               ("rx_mcs", "MCS", "{:.0f}", "MCS {:.0f}"),
               ("straty_%", "straty", "{:.1f} %", "straty {:.1f}%"),
               ("rx_Mbit_s", "przeplyw", "{:.2f} Mbit/s", "{:.2f} Mbit/s"),
               ("ping_ms", "ping", "{:.1f} ms", "ping {:.1f} ms"))

    def _readout_lines(self, picks):
        """Przy jednym pliku wypisujemy wszystko po kolei, przy porownaniu -
        po jednej gestej linii na plik. Inaczej ramka z odczytem zaslania pol
        wykresu, czyli dokladnie to, co chcemy porownac."""
        multi = len(self.logs) > 1
        lines = []
        for _i, log, sample in picks:
            rel = sample["sek"] - log.t0
            if multi:
                cells = [fmt.format(sample[col]) for col, _lbl, _long, fmt in self.READOUT
                         if sample.get(col) is not None]
                lines.append(f"{log.label}  ({fmt_time(rel)})")
                lines.append("  " + "  ".join(cells))
                continue

            lines.append(f"{sample.get('czas') or ''}   {fmt_time(rel)} od startu")
            for name in log.antennas:
                if name in sample["anteny"]:
                    lines.append(f"{name}: {sample['anteny'][name]:.0f} dBm")
            for column, label, long_fmt, _short in self.READOUT:
                value = sample.get(column)
                if value is not None:
                    lines.append(f"{label}: {long_fmt.format(value)}")
        return lines or ["brak danych w tym miejscu"]

    def _draw_readout(self, picks, sx, sy):
        c = self.canvas
        lines = self._readout_lines(picks)
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
    def __init__(self, paths=()):
        super().__init__()
        self.title("WFB-NG - podglad testu polaczenia")
        # na malym ekranie (laptop 1366x768) okno w stalym rozmiarze wychodzi
        # poza pulpit i chowa dolna os wykresu
        width = min(1200, self.winfo_screenwidth() - 80)
        height = min(800, self.winfo_screenheight() - 110)
        self.geometry(f"{width}x{height}+30+20")
        self.minsize(820, 520)
        self.logs = []

        self._build_menu()
        self._build_widgets()

        paths = as_paths(paths)
        if paths:
            self.load(paths)
        else:
            newest = self.newest_log()
            if newest:
                self.load([newest])

    # --- budowa okna ---

    def _build_menu(self):
        menu = tk.Menu(self)
        plik = tk.Menu(menu, tearoff=0)
        plik.add_command(label="Otworz...", accelerator="Ctrl+O", command=self.ask_open)
        plik.add_command(label="Dodaj do porownania...", accelerator="Ctrl+D",
                         command=self.ask_add)
        plik.add_command(label="Zostaw tylko pierwszy", command=self.clear_extra)
        plik.add_separator()
        plik.add_command(label="Odswiez", accelerator="F5", command=self.reload)
        plik.add_separator()
        plik.add_command(label="Zamknij", command=self.destroy)
        menu.add_cascade(label="Plik", menu=plik)
        self.config(menu=menu)
        self.bind("<Control-o>", lambda e: self.ask_open())
        self.bind("<Control-d>", lambda e: self.ask_add())
        self.bind("<F5>", lambda e: self.reload())

    def _build_widgets(self):
        self.configure(bg=BG)
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="Otworz pliki...", command=self.ask_open).pack(side="left")
        ttk.Button(bar, text="Dodaj do porownania...",
                   command=self.ask_add).pack(side="left", padx=6)
        self.clear_button = ttk.Button(bar, text="Zostaw pierwszy",
                                       command=self.clear_extra, state="disabled")
        self.clear_button.pack(side="left")
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

    def _ask_files(self, title):
        """Okno wyboru pozwala zaznaczyc kilka plikow na raz (Ctrl / Shift)."""
        return filedialog.askopenfilenames(
            title=title,
            initialdir=str(Path(__file__).resolve().parent),
            filetypes=[("Logi testu", "test-*.log"), ("Pliki log", "*.log"),
                       ("Wszystkie pliki", "*.*")])

    def ask_open(self):
        paths = self._ask_files("Wybierz log (albo kilka do porownania)")
        if paths:
            self.load(paths)

    def ask_add(self):
        paths = self._ask_files("Dolacz log do porownania")
        if paths:
            self.load(paths, add=True)

    def clear_extra(self):
        if len(self.logs) > 1:
            self.load([self.logs[0].path])

    def reload(self):
        if self.logs:
            self.load([log.path for log in self.logs])

    def load(self, paths, add=False):
        logs = list(self.logs) if add else []
        known = {str(log.path) for log in logs}
        for path in as_paths(paths):
            if str(path) in known:
                continue  # ten sam plik dwa razy nic nie wnosi
            try:
                log = TestLog(path)
            except OSError as e:
                messagebox.showerror("Nie moge otworzyc pliku", str(e))
                continue
            if not log.samples:
                messagebox.showwarning(
                    "Pusty log",
                    f"{path.name} nie zawiera ani jednej probki.\n"
                    "Czy na pewno to plik z ekranu \"Test polaczenia\"?")
            logs.append(log)
            known.add(str(path))

        if not logs:
            return
        self.logs = logs
        self._shorten_labels()

        many = len(logs) > 1
        self.title(("porownanie " + str(len(logs)) + " testow" if many
                    else logs[0].path.name) + " - podglad testu polaczenia")
        self.file_label.configure(
            text="   ".join(log.path.name for log in logs) if many else logs[0].path.name)
        self.clear_button.configure(state="normal" if many else "disabled")
        self._fill_info()
        self.chart.set_logs(logs)
        self.status.configure(
            text=f"Plikow: {len(logs)}   probek: {sum(len(l.samples) for l in logs)}"
                 f"   najdluzszy test: {fmt_time(max(l.duration() for l in logs))}"
                 + ("" if many else f"   anteny: {', '.join(logs[0].antennas) or 'brak'}"
                                    f"   |   {logs[0].path}"))

    def _shorten_labels(self):
        """Nazwy logow roznia sie zwykle tylko koncowka z godzina - pelna nazwa
        zajelaby pol legendy, wiec obcinamy wspolny poczatek. Ciecie cofamy do
        najblizszego myslnika, zeby z 'test-gs-20260727-2100' zostalo '2100',
        a nie '100'."""
        names = [log.path.stem for log in self.logs]
        prefix = 0
        if len(names) > 1:
            first = names[0]
            while prefix < len(first) and all(
                    len(n) > prefix and n[prefix] == first[prefix] for n in names):
                prefix += 1
            cut = max(first.rfind("-", 0, prefix), first.rfind("_", 0, prefix))
            prefix = cut + 1 if cut >= 0 else prefix
        for log, name in zip(self.logs, names):
            log.label = name[prefix:] if prefix and len(name) - prefix >= 3 else name

    STAT_ROWS = (("rssi_best_dBm", "RSSI dBm", "{:.0f}"),
                 ("snr_best_dB", "SNR dB", "{:.0f}"),
                 ("straty_%", "straty %", "{:.1f}"),
                 ("rx_Mbit_s", "Mbit/s", "{:.2f}"),
                 ("ping_ms", "ping ms", "{:.1f}"))

    def _fill_info(self):
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")

        def block(title, lines):
            if not lines:
                return
            self.info.insert("end", title + "\n", "naglowek")
            for line in lines:
                self.info.insert("end", line + "\n")

        if len(self.logs) > 1:
            block("Porownanie (srednie)", self._comparison_lines())
            for i, log in enumerate(self.logs):
                block(f"[{i + 1}] {log.path.name}",
                      log.header + self._stat_lines(log) + log.summary)
        else:
            log = self.logs[0]
            block("Warunki testu", log.header)
            block("Policzone z probek", self._stat_lines(log))
            block("Podsumowanie z pliku", log.summary)
            block("Zdarzenia w trakcie",
                  [f"{fmt_time(sec - log.t0)}  {text}" for sec, text in log.events])

        self.info.configure(state="disabled")

    def _stat_lines(self, log):
        lines = []
        for column, label, fmt in self.STAT_ROWS:
            stat = log.stat(column)
            if stat:
                lo, avg, hi = stat
                lines.append(f"{label:<9}{fmt.format(lo):>7} /{fmt.format(avg):>7} /"
                             f"{fmt.format(hi):>7}")
        if lines:
            lines.insert(0, f"{'':<9}{'min':>7} /{'srednio':>7} /{'max':>7}")
        mcs = sorted({int(v) for _, v in log.series("rx_mcs")})
        if mcs:
            lines.append("MCS      " + ", ".join(str(m) for m in mcs))
        return lines

    def _comparison_lines(self):
        """Srednie z kazdego pliku jedna obok drugiej - to jest wlasciwa
        odpowiedz na pytanie "czy po przestawieniu anteny jest lepiej", ktorej
        z samych krzywych nie da sie odczytac z dokladnoscia do liczby."""
        width = 9
        lines = [" " * 10 + "".join(f"[{i + 1}]".rjust(width) for i in range(len(self.logs)))]
        for column, label, fmt in self.STAT_ROWS:
            cells = []
            for log in self.logs:
                stat = log.stat(column)
                cells.append((fmt.format(stat[1]) if stat else "-").rjust(width))
            lines.append(f"{label:<10}" + "".join(cells))
        lines.append(f"{'czas':<10}" + "".join(
            fmt_time(log.duration()).rjust(width) for log in self.logs))
        lines.append(f"{'probek':<10}" + "".join(
            str(len(log.samples)).rjust(width) for log in self.logs))
        lines.append("")
        for i, log in enumerate(self.logs):
            lines.append(f"[{i + 1}] {log.label}")
        return lines


def main():
    # kilka plikow naraz: mozna zaznaczyc je w eksploratorze i przeciagnac
    # razem na podglad_testu.py - wtedy od razu widac je na wspolnych wykresach
    paths = [Path(a) for a in sys.argv[1:]]
    missing = [p for p in paths if not p.exists()]
    if missing:
        # komunikat i na konsole, i w okienku - po zmianie rozszerzenia na
        # .pyw (zeby nie wyskakiwalo czarne okno) konsoli po prostu nie ma
        text = "\n".join(str(p) for p in missing)
        print(f"Nie ma takich plikow:\n{text}")
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Nie ma takich plikow", text)
        root.destroy()
        return 2
    App(paths).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
