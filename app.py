"""Analizador de fútbol — app de escritorio (ttkbootstrap, una sola ventana)."""
import asyncio
import json
import math
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

import ttkbootstrap as tb
from ttkbootstrap.style import ThemeDefinition

import analysis
import db
import espn
import model
import scraper
import tracking
from config import DB_PATH, LEAGUES, METRICS
from names import best_match

# ---------------------------------------------------------------- tema

THEME = {
    "primary": "#2F80ED", "secondary": "#2A3446", "success": "#27AE60", "info": "#56A8F5",
    "warning": "#F2994A", "danger": "#EB5757", "light": "#C9D3E3", "dark": "#0E131D",
    "bg": "#121826", "fg": "#E7ECF5", "selectbg": "#2F80ED", "selectfg": "#FFFFFF",
    "border": "#273145", "inputfg": "#E7ECF5", "inputbg": "#1A2232", "active": "#1F2A3C",
}
BG, CARD, SIDEBAR = THEME["bg"], "#1A2232", "#0E131D"
FG, MUTED = THEME["fg"], "#8A96AB"
HOME_C, DRAW_C, AWAY_C = "#2F80ED", "#8A96AB", "#F2994A"
GOOD, BAD, WARN = THEME["success"], THEME["danger"], "#F2C94C"
FONT = "Segoe UI"

MISSING_REASON = {0: "otro", 1: "lesión", 2: "enfermedad", 3: "sanción", 11: "sanción", 21: "suspendido"}


def fmt(v, d=2):
    return "-" if v is None else f"{v:.{d}f}"


def pct(v):
    return "-" if v is None else f"{100 * v:.0f}%"


def fair(p):
    return "-" if not p or p <= 0 else f"{1 / p:.2f}"


DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
DIAS_LARGOS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def fdate(ts, long=False):
    d = datetime.fromtimestamp(ts)
    if long:
        return f"{DIAS_LARGOS[d.weekday()].capitalize()} {d:%d/%m/%Y %H:%M}"
    return f"{DIAS[d.weekday()]} {d:%d/%m %H:%M}"


def setup_style():
    style = tb.Style()
    style.register_theme(ThemeDefinition("estadistica", THEME, mode="dark"))
    style.theme_use("estadistica")
    style.configure(".", font=(FONT, 10))
    style.configure("Treeview", rowheight=28, background=CARD, fieldbackground=CARD, font=(FONT, 10))
    style.configure("Treeview.Heading", font=(FONT, 10, "bold"))
    style.configure("Card.TFrame", background=CARD)
    style.configure("Card.TLabel", background=CARD, foreground=FG)
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
    style.configure("CardTitle.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9, "bold"))
    style.configure("Side.TFrame", background=SIDEBAR)
    style.configure("SideTitle.TLabel", background=SIDEBAR, foreground=FG, font=(FONT, 15, "bold"))
    style.configure("SideMuted.TLabel", background=SIDEBAR, foreground=MUTED, font=(FONT, 9))
    style.configure("Title.TLabel", font=(FONT, 17, "bold"))
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("TNotebook.Tab", padding=(14, 6), font=(FONT, 10, "bold"))
    return style


# ---------------------------------------------------------------- componentes

def card(parent, title=None, padding=12):
    f = tb.Frame(parent, style="Card.TFrame", padding=padding)
    if title:
        tb.Label(f, text=title.upper(), style="CardTitle.TLabel").pack(anchor="w", pady=(0, 6))
    return f


def tile(parent, title, value, color=FG, sub=None):
    f = card(parent, padding=(14, 10))
    tb.Label(f, text=title.upper(), style="CardTitle.TLabel").pack(anchor="w")
    v = tb.Label(f, text=value, style="Card.TLabel", foreground=color, font=(FONT, 16, "bold"))
    v.pack(anchor="w")
    s = tb.Label(f, text=sub or "", style="CardMuted.TLabel")
    s.pack(anchor="w")
    f.value, f.sub = v, s
    return f


def tiles_row(parent, specs):
    """Fila de tarjetas del mismo ancho. specs: [(clave, título, valor, color, sub)]."""
    row = tb.Frame(parent)
    out = {}
    for i, (key, title, value, color, sub) in enumerate(specs):
        t = tile(row, title, value, color, sub)
        t.grid(row=0, column=i, sticky="nsew", padx=(0, 8 if i < len(specs) - 1 else 0))
        row.columnconfigure(i, weight=1, uniform="tiles")
        out[key] = t
    return row, out


def make_table(parent, columns, widths=None, height=12, stretch_first=True):
    frame = tb.Frame(parent)
    tree = tb.Treeview(frame, columns=[c for c, _ in columns], show="headings", height=height,
                       bootstyle="primary")
    for i, (c, label) in enumerate(columns):
        tree.heading(c, text=label, command=lambda c=c: _sort_tree(tree, c, False))
        tree.column(c, width=(widths or {}).get(c, 90), minwidth=40, anchor="w" if i == 0 else "center",
                    stretch=True)
    sb = tb.Scrollbar(frame, orient="vertical", command=tree.yview, bootstyle="round")
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    tree.tag_configure("odd", background="#172031")
    tree.tag_configure("good", foreground=GOOD)
    tree.tag_configure("bad", foreground=BAD)
    tree.tag_configure("home", foreground="#7FB2FF")
    tree.tag_configure("away", foreground="#FFB37A")
    tree.tag_configure("draw", foreground=MUTED)
    tree.tag_configure("muted", foreground="#B8C2D3")
    tree.tag_configure("bold", font=(FONT, 10, "bold"))
    return frame, tree


def insert_rows(tree, rows):
    """rows: tuplas de valores, o (valores, tags)."""
    tree.delete(*tree.get_children())
    for i, r in enumerate(rows):
        values, tags = r if len(r) == 2 and isinstance(r[0], tuple) else (r, ())
        tree.insert("", "end", values=values, tags=tuple(tags) + (("odd",) if i % 2 else ()))


def _sort_tree(tree, col, reverse):
    def key(v):
        try:
            return (0, float(str(v).split()[0].rstrip("%").replace("+", "")))
        except (ValueError, IndexError):
            return (1, str(v))
    items = [(tree.set(k, col), k) for k in tree.get_children("")]
    items.sort(key=lambda t: key(t[0]), reverse=reverse)
    for i, (_, k) in enumerate(items):
        tree.move(k, "", i)
    tree.heading(col, command=lambda: _sort_tree(tree, col, not reverse))


class ProbBar(tk.Canvas):
    """Barra 1-X-2 de colores."""

    def __init__(self, parent, height=38):
        super().__init__(parent, height=height, bg=CARD, highlightthickness=0)
        self.vals = (1 / 3, 1 / 3, 1 / 3)
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, ph, px, pa):
        self.vals = (ph, px, pa)
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), int(self["height"])
        x = 0
        for p, color in zip(self.vals, (HOME_C, DRAW_C, AWAY_C)):
            seg = w * p
            self.create_rectangle(x, 4, x + seg, h - 4, fill=color, outline="")
            if seg > 44:
                self.create_text(x + seg / 2, h / 2, text=f"{100 * p:.0f}%", fill="white",
                                 font=(FONT, 11, "bold"))
            x += seg


def scroll_area(parent, bg=CARD):
    """Frame con scroll vertical (para tablas con colores por casilla)."""
    outer = tb.Frame(parent)
    canvas = tk.Canvas(outer, bg=bg, highlightthickness=0)
    sb = tb.Scrollbar(outer, orient="vertical", command=canvas.yview, bootstyle="round")
    inner = tk.Frame(canvas, bg=bg)
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
    canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    def wheel(e):
        canvas.yview_scroll(int(-e.delta / 120), "units")
    canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
    canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
    return outer, inner


class DetailPanel(tb.Frame):
    """Panel fijo que explica el dato en el que se hizo clic (sin ventanas emergentes)."""

    def __init__(self, parent):
        super().__init__(parent, style="Card.TFrame", padding=14)
        tb.Label(self, text="DETALLE  ·  haz clic en un dato", style="CardTitle.TLabel").pack(anchor="w")
        self.title = tb.Label(self, text="", style="Card.TLabel", font=(FONT, 13, "bold"))
        self.title.pack(anchor="w", pady=(6, 0))
        self.headline = tb.Label(self, text="", style="Card.TLabel", font=(FONT, 11), wraplength=330,
                                 justify="left")
        self.headline.pack(anchor="w", pady=(2, 6))
        self.canvas = tk.Canvas(self, height=90, bg=CARD, highlightthickness=0)
        self.canvas.pack(fill="x")
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.lines = tb.Frame(self, style="Card.TFrame")
        self.lines.pack(fill="x", pady=(6, 0))
        self.note = tb.Label(self, text="", style="CardMuted.TLabel", wraplength=330, justify="left")
        self.note.pack(anchor="w", pady=(8, 0))
        self.bars = []

    def show(self, title, headline, bars=None, lines=None, note="", color=FG):
        """bars: [(etiqueta, prob, resaltar)] · lines: [(texto, color)]."""
        self.title.configure(text=title)
        self.headline.configure(text=headline, foreground=color)
        self.bars = bars or []
        self.canvas.configure(height=90 if self.bars else 1)
        self._draw()
        for w in self.lines.winfo_children():
            w.destroy()
        for r, (text, c) in enumerate(lines or []):
            if isinstance(text, tuple):  # dos columnas (Más | Menos)
                for col, t in enumerate(text):
                    tb.Label(self.lines, text=t, style="Card.TLabel", foreground=c, font=(FONT, 9)).grid(
                        row=r, column=col, sticky="w", padx=(0, 12))
            else:
                tb.Label(self.lines, text=text, style="Card.TLabel", foreground=c, font=(FONT, 10),
                         wraplength=330, justify="left").grid(row=r, column=0, columnspan=2, sticky="w")
        self.note.configure(text=note)

    def _draw(self):
        c = self.canvas
        c.delete("all")
        if not self.bars:
            return
        w = max(c.winfo_width(), 200)
        top = max(p for _, p, _ in self.bars) or 1
        bw = w / len(self.bars)
        for i, (lab, p, hi) in enumerate(self.bars):
            x0, x1 = i * bw + 5, (i + 1) * bw - 5
            y1 = 70
            y0 = y1 - max(2, p / top * 50)
            c.create_rectangle(x0, y0, x1, y1, fill=HOME_C if hi else "#3A4A66", outline="")
            c.create_text((x0 + x1) / 2, y0 - 9, text=pct(p), fill=FG if hi else MUTED,
                          font=(FONT, 9, "bold" if hi else "normal"))
            c.create_text((x0 + x1) / 2, 82, text=str(lab), fill=FG if hi else MUTED, font=(FONT, 10, "bold"))


def on_click(widget, fn):
    """Clic en el widget o en cualquiera de sus hijos."""
    for w in [widget] + list(widget.winfo_children()):
        w.bind("<Button-1>", lambda e: fn(), add="+")
        w.configure(cursor="hand2")


RELIAB_STYLE = {"fiable": ("✔ fiable", GOOD), "regular": ("≈ regular", WARN), "no fiable": ("✖ no fiable", BAD)}


def form_chips(parent, form, bg=CARD):
    f = tk.Frame(parent, bg=bg)
    colors = {"G": GOOD, "E": "#6B778C", "P": BAD}
    for ch in form:
        tk.Label(f, text=ch, bg=colors.get(ch, MUTED), fg="white", width=2, font=(FONT, 9, "bold")).pack(
            side="left", padx=1)
    return f


def styled_listbox(parent, height=24):
    return tk.Listbox(parent, selectmode="extended", height=height, exportselection=False,
                      font=("Consolas", 10), bg=THEME["inputbg"], fg=FG, selectbackground=HOME_C,
                      selectforeground="white", highlightthickness=0, borderwidth=0, activestyle="none")


_ui_queue = queue.Queue()  # resultados de hilos -> los procesa App._poll en el hilo de la interfaz


def run_in_thread(fn, on_done, widget):
    """fn() en otro hilo; on_done(resultado, error) en el hilo de la interfaz."""
    def worker():
        try:
            res, err = fn(), None
        except Exception as e:  # noqa: BLE001
            res, err = None, e
        _ui_queue.put((widget, on_done, res, err))
    threading.Thread(target=worker, daemon=True).start()


FIXTURE_LEAGUE = {7: "Champions"}  # nombre corto en la tabla de partidos


def league_combo(parent, with_all=False, width=18):
    values = (["Todas"] if with_all else []) + list(LEAGUES.values())
    c = tb.Combobox(parent, values=values, state="readonly", width=width, bootstyle="primary")
    c.current(0)
    return c


def league_id(name):
    return next((k for k, v in LEAGUES.items() if v == name), None)


def active_teams(conn, ut):
    """Equipos con partidos de liga en los últimos 60 días o próximos."""
    return conn.execute(
        """SELECT DISTINCT t.id, t.name FROM teams t JOIN events e ON (e.home_id=t.id OR e.away_id=t.id)
           WHERE e.ut_id=? AND e.start_ts>=? AND e.dup=0 ORDER BY t.name""",
        (ut, int(time.time()) - 60 * 86400)).fetchall()


# ---------------------------------------------------------------- ventana principal

class App(tb.Window):
    NAV = [("partidos", "⚽  Partidos"), ("resultados", "✅  Resultados"), ("equipos", "🛡  Equipos"),
           ("jugadores", "👤  Jugadores"),
           ("validacion", "📈  Validación"), ("datos", "⟳  Datos / Scraping")]

    def __init__(self):
        super().__init__(title="Analizador de fútbol — 5 grandes ligas y Champions", size=(1450, 900), minsize=(1150, 700))
        setup_style()
        try:
            assets = Path(__file__).resolve().parent / "assets"
            self.iconbitmap(default=str(assets / "icon.ico"))
            self._icon_png = tk.PhotoImage(file=str(assets / "icon.png"))
            self.iconphoto(True, self._icon_png)  # ícono grande (barra de tareas / Alt+Tab)
        except tk.TclError:
            pass
        self._set_taskbar_identity()
        self.conn = db.connect(DB_PATH)
        self.stack = []
        self.msgs = queue.Queue()
        self.scraping = False
        self.stop_flag = threading.Event()
        self.log_lines = []

        side = tb.Frame(self, style="Side.TFrame", padding=(14, 18), width=240)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tb.Label(side, text="📊 Analizador", style="SideTitle.TLabel").pack(anchor="w")
        tb.Label(side, text="Premier · LaLiga · Serie A\nLigue 1 · Bundesliga\nChampions League", style="SideMuted.TLabel",
                 justify="left").pack(anchor="w", pady=(2, 18))
        self.nav_buttons = {}
        for key, label in self.NAV:
            b = tb.Button(side, text=label, bootstyle="secondary", command=lambda k=key: self.go_root(k))
            b.pack(fill="x", pady=3, ipady=6)
            self.nav_buttons[key] = b
        self.status_lbl = tb.Label(side, text="", style="SideMuted.TLabel", wraplength=210, justify="left")
        self.status_lbl.pack(side="bottom", anchor="w")

        main = tb.Frame(self, padding=(18, 12))
        main.pack(side="left", fill="both", expand=True)
        top = tb.Frame(main)
        top.pack(fill="x", pady=(0, 10))
        self.back_btn = tb.Button(top, text="←  Volver", bootstyle="primary-outline", command=self.back)
        self.title_lbl = tb.Label(top, text="", style="Title.TLabel")
        self.title_lbl.pack(side="left")
        self.job_box = tb.Frame(top)
        self.job_text = tb.Label(self.job_box, text="", style="Muted.TLabel")
        self.job_text.pack(side="left", padx=(0, 10))
        self.job_bar = tb.Progressbar(self.job_box, bootstyle="info-striped", mode="determinate", length=230,
                                      maximum=100)
        self.job_bar.pack(side="left")
        self.job_pct = tb.Label(self.job_box, text="", font=(FONT, 11, "bold"), foreground=THEME["info"], width=5)
        self.job_pct.pack(side="left", padx=(8, 0))
        self.job_running = False
        self.content = tb.Frame(main)
        self.content.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda e: self.back())

        self.after(200, self._poll)
        self.auto_busy = False
        self.auto_note = "Auto-actualización activa: partidos que empiezan en < 45 min"
        self.refresh_status()
        self.go_root("partidos")
        self.after(20_000, self._auto_check)
        self.live_busy = False
        self.after(8_000, self._live_check)
        run_in_thread(self._track_job, lambda r, e: None, self)

    def _set_taskbar_identity(self):
        """Identidad de la ventana en la barra de tareas: así se agrupa con el ícono anclado
        (y no como pythonw.exe) y, si se ancla desde la ventana abierta, guarda el comando correcto."""
        try:
            import sys

            import pythoncom
            from win32com.propsys import propsys, pscon
            self.update_idletasks()
            hwnd = int(self.wm_frame(), 16)
            root = Path(__file__).resolve().parent
            pyw = Path(sys.executable).with_name("pythonw.exe")
            store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
            for key, value in ((pscon.PKEY_AppUserModel_ID, APP_ID),
                               (pscon.PKEY_AppUserModel_RelaunchCommand, f'"{pyw}" "{root / "app.py"}"'),
                               (pscon.PKEY_AppUserModel_RelaunchDisplayNameResource, "Analizador de fútbol"),
                               (pscon.PKEY_AppUserModel_RelaunchIconResource, f"{root / 'assets' / 'icon.ico'},0")):
                store.SetValue(key, propsys.PROPVARIANTType(value, pythoncom.VT_LPWSTR))
            store.Commit()
        except Exception:  # noqa: BLE001 - sin pywin32 la app funciona igual
            pass

    @staticmethod
    def _track_job():
        conn = db.connect(DB_PATH)
        tracking.backfill(conn)
        tracking.save_upcoming(conn)
        conn.close()

    # -- actualización automática antes de los partidos
    def _auto_check(self):
        if not self.scraping and not self.auto_busy and not self.job_running:
            self.auto_busy = True
            self.run_job(lambda job: scraper.refresh_upcoming(self.log, 45, job), self._auto_done)
        self.after(5 * 60_000, self._auto_check)

    # -- marcadores en vivo: los partidos que empiezan pasan a «En vivo» y los que acaban a «Terminados»
    def _live_check(self):
        now = int(time.time())
        active = self.conn.execute(
            """SELECT 1 FROM events WHERE dup=0 AND status IN ('notstarted', 'inprogress')
               AND start_ts BETWEEN ? AND ? LIMIT 1""", (now - 4 * 3600, now + 300)).fetchone()
        if active and not self.live_busy and not self.scraping:
            self.live_busy = True
            run_in_thread(lambda: scraper.live_refresh(self.log), self._live_done, self)
        self.after(120_000, self._live_check)

    def _live_done(self, changed, err):
        self.live_busy = False
        if err:
            self.log(f"Marcadores en vivo: {err}")
            return
        if changed:
            for _, frame in self.stack:
                if isinstance(frame, FixturesView):
                    frame.load()

    # -- tareas en segundo plano con barra de progreso
    def run_job(self, fn, on_done=None):
        """fn(job) en otro hilo; job(fracción 0-1, texto) mueve la barra de arriba."""
        if self.job_running or self.scraping:
            self._job_show(None, "Ya hay una actualización en curso…")
            return False
        self.job_running = True

        def job(frac, text):
            self.msgs.put(("job", (frac, text)))

        def wrapped():
            try:
                return fn(job)
            finally:
                self.msgs.put(("job_end", None))
        run_in_thread(wrapped, on_done or (lambda r, e: None), self)
        return True

    def _job_show(self, frac, text):
        if not self.job_box.winfo_ismapped():
            self.job_box.pack(side="right")
        self.job_text.configure(text=text[:70])
        if frac is not None:
            self.job_bar.configure(value=100 * frac)
            self.job_pct.configure(text=f"{100 * frac:.0f}%")

    def _job_end(self):
        self.job_running = False
        if self.job_box.winfo_ismapped():
            self.job_bar.configure(value=100)
            self.job_pct.configure(text="100%")
            self.job_text.configure(text=f"✔ Actualizado {datetime.now():%H:%M}")
            self.after(4000, lambda: None if self.job_running or self.scraping else self.job_box.pack_forget())

    def _auto_done(self, updated, err):
        self.auto_busy = False
        if err:
            self.log(f"Auto-actualización: {err}")
            return
        if not updated:
            return
        model.clear_cache()
        names = ", ".join(n for _, n in updated)
        self.auto_note = f"⟳ {datetime.now():%H:%M} datos frescos: {names}"
        self.refresh_status()
        ids = {eid for eid, _ in updated}
        for _, frame in self.stack:
            if isinstance(frame, MatchView) and frame.event["id"] in ids:
                frame.model = model.get_model(self.conn, frame.event["ut_id"])
                frame.load_stored_live()

    # -- navegación (una sola ventana, con pila para "Volver")
    def _show_top(self):
        title, frame = self.stack[-1]
        frame.pack(fill="both", expand=True)
        self.title_lbl["text"] = title
        if len(self.stack) > 1:
            self.back_btn.pack(side="left", padx=(0, 14), before=self.title_lbl)
        else:
            self.back_btn.pack_forget()

    def go_root(self, key):
        for _, f in self.stack:
            f.destroy()
        self.stack = []
        for k, b in self.nav_buttons.items():
            b.configure(bootstyle="primary" if k == key else "secondary")
        builders = {"partidos": FixturesView, "resultados": ResultsView, "equipos": TeamsView,
                    "jugadores": PlayersView,
                    "validacion": ValidationView, "datos": DataView}
        title = {"validacion": "Validación del modelo"}.get(key, dict(self.NAV)[key].split("  ", 1)[1])
        self.push(title, lambda parent: builders[key](parent, self))

    def push(self, title, builder):
        if self.stack:
            self.stack[-1][1].pack_forget()
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            frame = builder(self.content)
        finally:
            self.config(cursor="")
        self.stack.append((title, frame))
        self._show_top()

    def back(self):
        if len(self.stack) > 1:
            _, f = self.stack.pop()
            f.destroy()
            self._show_top()

    def open_match(self, event):
        self.push(f"{event['home_name']}  vs  {event['away_name']}", lambda p: MatchView(p, self, event))

    def open_team(self, team_id, name):
        self.push(name, lambda p: TeamView(p, self, team_id))

    def open_comparison(self, event_id, title):
        self.push(title, lambda p: ComparisonView(p, self, event_id))

    def open_players(self, team_id, name):
        self.push(f"Jugadores · {name}", lambda p: PlayersView(p, self, team_id))

    # -- estado / scraping
    def refresh_status(self):
        c = db.counts(self.conn)
        self.status_lbl["text"] = (f"{c['partidos']:,} partidos\n{c['con_faltas_tarjetas']:,} con faltas/tarjetas\n"
                                   f"{c['proximos']:,} próximos\n\nÚltima actualización:\n{c['ultimo_scrape']}"
                                   f"\n\n{getattr(self, 'auto_note', '')}")

    def log(self, msg):
        self.msgs.put(("log", f"[{datetime.now():%H:%M:%S}] {msg}"))

    def _poll(self):
        try:
            while True:
                kind, val = self.msgs.get_nowait()
                if kind == "log":
                    self.log_lines.append(val)
                    if self.scraping:
                        self._job_show(None, val.split("] ", 1)[-1].strip())
                elif kind == "progress" and self.scraping:
                    done, total = val
                    self._job_show(done / max(total, 1), self.job_text.cget("text") or "Scrapeando…")
                elif kind == "job":
                    self._job_show(*val)
                elif kind == "job_end":
                    self._job_end()
                elif kind == "finished":
                    self.scraping = False
                    self._job_end()
                    model.clear_cache()
                    self.refresh_status()
                for _, frame in self.stack:
                    if isinstance(frame, DataView):
                        frame.on_msg(kind, val)
        except queue.Empty:
            pass
        while not _ui_queue.empty():
            widget, on_done, res, err = _ui_queue.get_nowait()
            try:
                on_done(res, err)
            except tk.TclError:
                pass  # la vista se cerró mientras el hilo trabajaba
        self.after(200, self._poll)

    def start_scrape(self, leagues, us_seasons, espn_years, sofa_seasons, rate):
        if self.scraping or self.job_running or not leagues:
            return
        self.scraping = True
        self._job_show(0, "Scrapeando…")
        self.stop_flag.clear()

        def worker():
            try:
                asyncio.run(scraper.run_scrape(leagues, us_seasons, espn_years, sofa_seasons, rate, self.log,
                                               lambda d, t: self.msgs.put(("progress", (d, t))), self.stop_flag))
            except Exception as e:  # noqa: BLE001
                self.log(f"ERROR: {e!r}")
            self.msgs.put(("finished", None))
        threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------- próximos partidos

class FixturesView(tb.Frame):
    MODES = (("prox", "Próximos"), ("vivo", "● En vivo"), ("fin", "Terminados"))

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        bar = tb.Frame(self)
        bar.pack(fill="x", pady=(0, 10))
        self.mode = tb.StringVar(value="prox")
        for val, txt in self.MODES:
            tb.Radiobutton(bar, text=txt, value=val, variable=self.mode, bootstyle="primary-toolbutton",
                           command=self.load).pack(side="left", ipadx=6)
        tb.Label(bar, text="Liga").pack(side="left", padx=(16, 0))
        self.league = league_combo(bar, with_all=True)
        self.league.pack(side="left", padx=(6, 16))
        self.league.bind("<<ComboboxSelected>>", lambda e: self.load())
        self.days_lbl = tb.Label(bar, text="Próximos días")
        self.days_lbl.pack(side="left")
        self.days = tb.IntVar(value=8)
        self.days_spin = tb.Spinbox(bar, from_=1, to=60, width=4, textvariable=self.days, command=self.load)
        self.days_spin.pack(side="left", padx=6)
        self.btn_update = tb.Button(bar, text="⟳ Actualizar datos", bootstyle="primary-outline",
                                    command=self.update_data)
        self.btn_update.pack(side="left", padx=10)
        self.hint = tb.Label(self, text="", style="Muted.TLabel")
        self.hint.pack(anchor="w", pady=(0, 6))

        cols = [("fecha", "Fecha"), ("liga", "Liga"), ("local", "Local"), ("marcador", "Marcador"),
                ("visit", "Visitante"), ("p1", "1"), ("px", "X"), ("p2", "2"), ("pick", "Pronóstico"),
                ("goles", "Goles esp."), ("o25", "+2.5"), ("faltas", "Faltas"), ("amar", "Amarillas"),
                ("corn", "Córners")]
        # anchos medidos con la fuente de la tabla (nombres largos: «Paris Saint Germain», «✔ … (59%)»)
        frame, self.tree = make_table(self, cols, {"fecha": 145, "liga": 125, "local": 160, "marcador": 110,
                                                   "visit": 160, "pick": 235, "p1": 46, "px": 46, "p2": 46,
                                                   "goles": 95, "o25": 52, "faltas": 62, "amar": 90, "corn": 75}, 24)
        self.tree.column("marcador", anchor="center")
        frame.pack(fill="both", expand=True)
        self.empty = tb.Label(self, text="", style="Muted.TLabel")
        self.tree.bind("<Double-1>", self._open)
        self.tree.bind("<Return>", self._open)
        self.rows = {}
        self.load()

    def update_data(self):
        """Baja resultados/estadísticas nuevas de las ligas elegidas, recalibra y recarga."""
        name = self.league.get()
        uts = list(LEAGUES) if name == "Todas" else [league_id(name)]

        def done(_, err):
            model.clear_cache()
            self.app.refresh_status()
            self.btn_update.configure(state="normal")
            if err:
                self.app._job_show(None, f"Error al actualizar: {str(err)[:50]}")
            self.load()
        if self.app.run_job(lambda job: scraper.quick_update(uts, self.app.log, job), done):
            self.btn_update.configure(state="disabled")

    def _events(self, uts):
        mode, conn = self.mode.get(), self.app.conn
        if mode == "prox":
            return db.upcoming(conn, uts, self.days.get())
        q = ",".join("?" * len(uts))
        base = f"""SELECT e.*, th.name AS home_name, ta.name AS away_name FROM events e
                   JOIN teams th ON th.id=e.home_id JOIN teams ta ON ta.id=e.away_id
                   WHERE e.dup=0 AND e.ut_id IN ({q}) AND """
        if mode == "vivo":
            return conn.execute(base + "e.status='inprogress' ORDER BY e.start_ts", uts).fetchall()
        return conn.execute(base + "e.status='finished' AND e.start_ts>=? ORDER BY e.start_ts DESC",
                            (*uts, int(time.time()) - self.days.get() * 86400)).fetchall()

    def _prediction(self, e):
        """La predicción guardada antes del partido (la que se compara después); si no hay, se calcula."""
        if self.mode.get() != "prox":
            row = self.app.conn.execute("SELECT * FROM predictions WHERE event_id=?", (e["id"],)).fetchone()
            if row:
                p = dict(row)
                p["means"] = json.loads(p["means"])
                return p, True
        m = model.get_model(self.app.conn, e["ut_id"])
        p = m.predict(e["home_id"], e["away_id"], as_of=None if self.mode.get() == "prox" else e["start_ts"])
        if self.mode.get() == "prox":
            tracking.snapshot(self.app.conn, m, e, p, "pre")
        return p, False

    def load(self):
        mode = self.mode.get()
        name = self.league.get()
        uts = list(LEAGUES) if name == "Todas" else [league_id(name)]
        if mode == "vivo":
            self.days_lbl.pack_forget()
            self.days_spin.pack_forget()
        elif not self.days_spin.winfo_ismapped():
            self.days_lbl.pack(side="left", before=self.btn_update)
            self.days_spin.pack(side="left", padx=6, before=self.btn_update)
        if mode == "fin" and self.days.get() > 14:
            self.days.set(2)
        self.days_lbl.configure(text="Últimos días" if mode == "fin" else "Próximos días")
        self.hint.configure(text={
            "prox": "Doble clic: análisis completo  ·  ★ = favorito ≥ 60 %",
            "vivo": "El marcador se actualiza cada 2 min mientras la app está abierta  ·  doble clic: análisis",
            "fin": "✔/✖ = acertó o no el ganador  ·  doble clic: análisis del partido (y desde ahí, predicción vs "
                   "resultado)"}[mode])
        # en vivo/terminados el marcador ocupa el sitio de 1/X/2 (el % va junto al pronóstico)
        hide = {"marcador"} if mode == "prox" else {"p1", "px", "p2"}
        self.tree["displaycolumns"] = [c for c in self.tree["columns"] if c not in hide]
        sel = self.tree.selection()
        top = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        for i, e in enumerate(self._events(uts)):
            e = dict(e)
            p, stored = self._prediction(e)
            probs = (p["p_home"], p["p_draw"], p["p_away"])
            best = probs.index(max(probs))
            pick = (e["home_name"], "Empate", e["away_name"])[best] + (" ★" if max(probs) >= 0.6 else "")
            score = ""
            if mode != "prox":
                pick = pick.replace(" ★", "") + f" ({pct(max(probs))})"
            if mode != "prox" and e["home_goals"] is not None:
                score = f"{e['home_goals']} - {e['away_goals']}"
                if mode == "vivo":
                    score = f"{e.get('live_clock') or ''}   {score}".strip()
                else:
                    hg, ag = e["home_goals"], e["away_goals"]
                    hit = best == (0 if hg > ag else 1 if hg == ag else 2)
                    pick = ("✔ " if hit else "✖ ") + pick
            means = p["means"]

            def tot(m):
                v = means.get(m, (None, None))
                return fmt(v[0] + v[1], 1) if v[0] is not None else "-"
            iid = str(e["id"])
            self.rows[iid] = (e, stored)
            color = ("good" if hit else "bad") if mode == "fin" and score else ("home", "draw", "away")[best]
            self.tree.insert("", "end", iid=iid, tags=(color,) + (("odd",) if i % 2 else ()),
                             values=(fdate(e["start_ts"]), FIXTURE_LEAGUE.get(e["ut_id"], LEAGUES.get(e["ut_id"])),
                                     e["home_name"], score,
                                     e["away_name"], pct(p["p_home"]), pct(p["p_draw"]), pct(p["p_away"]), pick,
                                     f"{p['lam_h']:.1f} - {p['lam_a']:.1f}", pct(p["over25"]), tot("fouls"),
                                     tot("yellow"), tot("corners")))
        self.app.conn.commit()
        keep = [i for i in sel if i in self.rows]
        if keep:
            self.tree.selection_set(keep)
        self.tree.yview_moveto(top)
        if self.rows:
            self.empty.pack_forget()
        else:
            self.empty.configure(text={"prox": "No hay partidos próximos en ese rango.",
                                       "vivo": "No hay partidos en juego ahora mismo.",
                                       "fin": "No hay partidos terminados en esos días."}[mode])
            self.empty.pack(pady=10)

    def _open(self, _):
        sel = self.tree.selection()
        if not sel:
            return
        self.app.open_match(self.rows[sel[0]][0])


# ---------------------------------------------------------------- resultados (predicción vs realidad)

LEAGUE_SHORT = {17: "PL", 8: "LL", 23: "SA", 34: "L1", 35: "BL", 7: "UCL"}
SOURCE_SHORT = {"pre": "antes", "pre_xi": "antes+XI", "rebuilt": "reconst."}


class ResultsView(tb.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        bar = tb.Frame(self)
        bar.pack(fill="x", pady=(0, 10))
        tb.Label(bar, text="Liga").pack(side="left")
        self.league = league_combo(bar, with_all=True)
        self.league.pack(side="left", padx=(6, 16))
        self.league.bind("<<ComboboxSelected>>", lambda e: self.load())
        tb.Label(bar, text="Últimos días").pack(side="left")
        self.days = tb.IntVar(value=7)
        tb.Spinbox(bar, from_=1, to=60, width=4, textvariable=self.days, command=self.load).pack(side="left", padx=6)
        self.btn_update = tb.Button(bar, text="⟳ Actualizar datos", bootstyle="primary-outline",
                                    command=self.update_data)
        self.btn_update.pack(side="left", padx=10)
        tb.Label(bar, text=("Lo que dijo el modelo antes del partido vs lo que pasó  ·  ✔ acierto  ✖ fallo"),
                 style="Muted.TLabel").pack(side="right")
        self.tiles = tb.Frame(self)
        self.tiles.pack(fill="x")
        cols = [("fecha", "Fecha"), ("liga", "Liga"), ("local", "Local"), ("res", "Res."), ("visit", "Visitante"),
                ("pick", "Pronóstico"), ("ok", "1X2"), ("goles", "Goles"), ("o25", "+2.5"),
                ("faltas", "Faltas"), ("amar", "Amarillas"), ("corn", "Córners"), ("origen", "Predicción")]
        fr, self.tree = make_table(self, cols, {"fecha": 90, "liga": 45, "local": 125, "res": 50, "visit": 125,
                                                "pick": 145, "ok": 45, "goles": 70, "o25": 70, "faltas": 95,
                                                "amar": 95, "corn": 95, "origen": 80}, 20)
        fr.pack(fill="both", expand=True, pady=(10, 0))
        self.tree.bind("<ButtonRelease-1>", self._open)
        self.tree.bind("<Return>", self._open)
        tb.Label(self, text=("Clic en un partido para ver la comparación completa (pronóstico vs resultado).  "
                             "Faltas / amarillas / córners: «esperado → real» y ✔ si acertó el lado (Más/Menos) que "
                             "prefería el modelo en la línea .5 más cercana a su media.  Predicción: «antes» = guardada "
                             "antes del partido · «antes+XI» = con alineación/árbitro · «reconst.» = calculada solo con "
                             "datos anteriores al partido.  «pendiente» = dale a Scrapear para traer el resultado."),
                 style="Muted.TLabel", wraplength=1100).pack(anchor="w", pady=(6, 0))
        self.load()

    def update_data(self):
        """Baja resultados/estadísticas nuevas de las ligas elegidas, recalibra y recarga."""
        name = self.league.get()
        uts = list(LEAGUES) if name == "Todas" else [league_id(name)]

        def done(_, err):
            model.clear_cache()
            self.app.refresh_status()
            self.btn_update.configure(state="normal")
            if err:
                self.app._job_show(None, f"Error al actualizar: {str(err)[:50]}")
            self.load()
        if self.app.run_job(lambda job: scraper.quick_update(uts, self.app.log, job), done):
            self.btn_update.configure(state="disabled")

    def load(self):
        name = self.league.get()
        uts = list(LEAGUES) if name == "Todas" else [league_id(name)]
        rows = tracking.results(self.app.conn, uts, self.days.get())
        done = [r for r in rows if r["finished"]]
        for w in self.tiles.winfo_children():
            w.destroy()

        def rate(vals):
            vals = [v for v in vals if v is not None]
            return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)

        def mk_hits(k):
            return [r["markets"][k][2][1] if r["markets"][k][2] else None for r in done]
        specs = [("n", "Partidos jugados", str(len(done)), FG, f"{len(rows) - len(done)} pendientes")]
        for key, title, vals in (("1x2", "Acierto 1X2", [r["hit_1x2"] for r in done]),
                                 ("o25", "Acierto +/-2.5", [r["hit_o25"] for r in done]),
                                 ("yellow", "Amarillas", mk_hits("yellow")), ("fouls", "Faltas", mk_hits("fouls")),
                                 ("corners", "Córners", mk_hits("corners"))):
            v, n = rate(vals)
            color = FG if v is None else GOOD if v >= 0.55 else WARN if v >= 0.48 else BAD
            specs.append((key, title, pct(v), color, f"de {n} partidos"))
        row, _ = tiles_row(self.tiles, specs)
        row.pack(fill="x")

        def mk(r, k):
            mu, actual, res = r["markets"][k]
            if mu is None:
                return "-"
            if actual is None:
                return f"{mu:.1f} → -"
            return f"{mu:.1f} → {actual:.0f} {'✔' if res and res[1] else '✖'}"

        out = []
        for r in rows:
            pick = (r["home_name"], "Empate", r["away_name"])[r["pick"]]
            if r["finished"]:
                res = f"{r['home_goals']} - {r['away_goals']}"
                ok = "✔" if r["hit_1x2"] else "✖"
                o25 = f"{pct(r['over25'])} {'✔' if r['hit_o25'] else '✖'}"
                tags = ("good",) if r["hit_1x2"] else ("bad",)
            else:
                res, ok, o25, tags = "pendiente", "", pct(r["over25"]), ("draw",)
            out.append(((datetime.fromtimestamp(r["start_ts"]).strftime("%d/%m %H:%M"), LEAGUE_SHORT.get(r["ut_id"]),
                         r["home_name"], res, r["away_name"], f"{pick} {pct(r['pick_prob'])}", ok,
                         f"{r['lam_h']:.1f} - {r['lam_a']:.1f}", o25, mk(r, "fouls"), mk(r, "yellow"),
                         mk(r, "corners"), SOURCE_SHORT.get(r["source"], r["source"])), tags, r))
        self.tree.delete(*self.tree.get_children())
        self.titles = {}
        for i, (values, tags, r) in enumerate(out):
            iid = str(r["id"])
            self.titles[iid] = f"{r['home_name']}  vs  {r['away_name']}"
            self.tree.insert("", "end", iid=iid, values=values, tags=tags + (("odd",) if i % 2 else ()))

    def _open(self, e):
        if e.type == tk.EventType.ButtonRelease and self.tree.identify_region(e.x, e.y) != "cell":
            return  # clic en el encabezado = ordenar
        sel = self.tree.selection()
        if sel:
            self.app.open_comparison(int(sel[0]), self.titles[sel[0]])


class ComparisonView(tb.Frame):
    """Lo que pronosticó el modelo antes del partido vs lo que pasó."""

    def __init__(self, parent, app, event_id):
        super().__init__(parent)
        self.app = app
        c = tracking.comparison(app.conn, event_id)
        if not c:
            tb.Label(self, text="No hay predicción guardada para este partido.", style="Muted.TLabel").pack()
            return
        e, done = c["event"], c["finished"]
        names = (e["home_name"], "Empate", e["away_name"])

        # cabecera: marcador y datos de la predicción
        h = card(self, padding=(16, 12))
        h.pack(fill="x")
        row = tb.Frame(h, style="Card.TFrame")
        row.pack(fill="x")
        tb.Label(row, text=e["home_name"], style="Card.TLabel", foreground=HOME_C, font=(FONT, 16, "bold")).pack(
            side="left")
        tb.Label(row, text=f"  {e['home_goals']} - {e['away_goals']}  " if done else "  vs  ", style="Card.TLabel",
                 font=(FONT, 20, "bold")).pack(side="left")
        tb.Label(row, text=e["away_name"], style="Card.TLabel", foreground=AWAY_C, font=(FONT, 16, "bold")).pack(
            side="left")
        tb.Label(row, text=f"{LEAGUES.get(e['ut_id'])}  ·  {fdate(e['start_ts'], long=True)}",
                 style="CardMuted.TLabel").pack(side="right")
        saved = datetime.fromtimestamp(e["saved_ts"]).strftime("%d/%m %H:%M")
        tb.Label(h, text=(f"Predicción: {c['source_label']} (guardada {saved})"
                          + (f"  ·  Árbitro: {e['referee']}" if e.get("referee") else "")
                          + ("" if done else "  ·  Aún no hay resultado: dale a «⟳ Actualizar datos» después del "
                                             "partido")), style="CardMuted.TLabel").pack(anchor="w", pady=(6, 0))
        bar = ProbBar(h)
        bar.pack(fill="x", pady=(10, 4))
        bar.set(*c["probs"])
        if done:
            got = c["outcome"]
            hit = got == c["pick"]
            verdict = (f"Resultado: {'gana ' + names[got] if got != 1 else 'empate'}.  El modelo le daba "
                       f"{pct(c['p_outcome'])} a lo que pasó  →  "
                       + ("✔ acertó el favorito" if hit else f"✖ su favorito era {names[c['pick']]} "
                                                               f"({pct(c['probs'][c['pick']])})"))
            tb.Label(h, text=verdict, style="Card.TLabel", foreground=GOOD if hit else BAD,
                     font=(FONT, 11, "bold")).pack(anchor="w")
        else:
            tb.Label(h, text=f"Pronóstico: {names[c['pick']]} ({pct(c['probs'][c['pick']])})", style="Card.TLabel",
                     font=(FONT, 11, "bold")).pack(anchor="w")

        # resumen de aciertos
        items = c["items"]
        judged = [i for i in items if i["hit"] is not None]
        checks = ([c["pick"] == c["outcome"], c["hit_o25"], c["hit_btts"]] if done else []) + \
            [i["hit"] for i in judged]
        n_ok = sum(checks)
        specs = [("tot", "Aciertos", f"{n_ok} de {len(checks)}" if checks else "-",
                  FG if not checks else GOOD if n_ok / len(checks) >= 0.55 else WARN if n_ok / len(checks) >= 0.45
                  else BAD, "total del partido (y ganador)")]
        if done:
            o25_txt = f"{pct(e['over25'])} a Más de 2.5"
            btts_txt = f"{pct(e['btts'])} a que ambos marquen"
            specs += [("1x2", "Ganador", "✔" if c["pick"] == c["outcome"] else "✖",
                       GOOD if c["pick"] == c["outcome"] else BAD, f"le daba {pct(c['p_outcome'])} a lo que pasó"),
                      ("o25", "Más/menos 2.5", "✔" if c["hit_o25"] else "✖", GOOD if c["hit_o25"] else BAD, o25_txt),
                      ("btts", "Ambos marcan", "✔" if c["hit_btts"] else "✖", GOOD if c["hit_btts"] else BAD,
                       btts_txt),
                      ("exact", "Marcador exacto", pct(c["p_exact"]), FG,
                       f"probabilidad que daba al {e['home_goals']}-{e['away_goals']}")]
        team_checks = [i[w]["hit"] for i in items for w in ("home", "away") if i[w]["hit"] is not None]
        if team_checks:
            n_t = sum(team_checks)
            specs.insert(1, ("teams", "Líneas por equipo", f"{n_t} de {len(team_checks)}",
                             GOOD if n_t / len(team_checks) >= 0.55 else WARN if n_t / len(team_checks) >= 0.45
                             else BAD, "local y visitante por separado"))
        tiles, _ = tiles_row(self, specs)
        tiles.pack(fill="x", pady=(10, 0))

        # pronóstico a la izquierda, resultado real a la derecha, veredicto en el centro
        body = card(self, padding=(14, 10))
        body.pack(fill="both", expand=True, pady=(10, 0))
        outer, grid = scroll_area(body)
        outer.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1, uniform="lado")
        grid.columnconfigure(2, weight=1, uniform="lado")
        grid.columnconfigure(1, minsize=330)
        for col, (title, sub) in ((0, ("PRONÓSTICO", "lo que dijo el modelo antes del partido")),
                                  (1, ("¿ACERTÓ?", "local  ·  total  ·  visitante")),
                                  (2, ("RESULTADO REAL", "lo que pasó en el partido"))):
            head = tk.Frame(grid, bg=CARD)
            head.grid(row=0, column=col, sticky="ew", pady=(0, 4))
            tk.Label(head, text=title, bg=CARD, fg=HOME_C if col == 0 else GOOD if col == 2 else MUTED,
                     font=(FONT, 11, "bold")).pack()
            tk.Label(head, text=sub, bg=CARD, fg=MUTED, font=(FONT, 9)).pack()
        teams = tk.Frame(grid, bg=CARD)
        for col in (0, 2):
            row_t = tk.Frame(grid, bg=CARD)
            row_t.grid(row=1, column=col, sticky="ew", padx=8)
            tk.Label(row_t, text=e["home_name"], bg=CARD, fg=HOME_C, font=(FONT, 10, "bold")).pack(side="left")
            tk.Label(row_t, text=e["away_name"], bg=CARD, fg=AWAY_C, font=(FONT, 10, "bold")).pack(side="right")
        teams.destroy()

        self.canvases = []
        for n, i in enumerate(items, start=2):
            bg = CARD if n % 2 == 0 else "#172031"
            pred = tk.Canvas(grid, bg=bg, height=42, highlightthickness=0)
            pred.grid(row=n, column=0, sticky="ew")
            real = tk.Canvas(grid, bg=bg, height=42, highlightthickness=0)
            real.grid(row=n, column=2, sticky="ew")
            mid = tk.Frame(grid, bg=bg)
            mid.grid(row=n, column=1, sticky="nsew")
            total = {k: i[k] for k in ("line", "pick", "prob", "actual", "hit")}
            for col, (v, fg) in enumerate(((i["home"], HOME_C), (total, FG), (i["away"], AWAY_C))):
                mid.columnconfigure(col, weight=1, uniform="v")
                cell = tk.Frame(mid, bg=bg)
                cell.grid(row=0, column=col, sticky="nsew", padx=2)
                if v["hit"] is None:
                    mark, color = "–", MUTED
                else:
                    mark, color = ("✔", GOOD) if v["hit"] else ("✖", BAD)
                real_v = "-" if v["actual"] is None else f"{v['actual']:.0f}"
                tk.Label(cell, text=f"{mark} {v['pick']} {v['line']}", bg=bg, fg=color,
                         font=(FONT, 9, "bold")).pack(pady=(3, 0))
                tk.Label(cell, text=f"{pct(v['prob'])} · real {real_v}", bg=bg, fg=MUTED, font=(FONT, 8)).pack()
            self.canvases.append((pred, i["label"], i["eh"], i["ea"], 1))
            self.canvases.append((real, i["label"], i["rh"], i["ra"], 0))
            for cv in (pred, real):
                cv.bind("<Configure>", lambda ev, c=cv: self._draw(c))

        tk.Label(grid, bg=CARD, fg=MUTED, font=(FONT, 9), justify="left", wraplength=1050, text=(
                     "Cada fila: local a la izquierda (azul) y visitante a la derecha (naranja), igual que en "
                             "SofaScore. En el centro, para el local, el total del partido y el visitante: el lado "
                             "(Más/Menos) que prefería el modelo en la línea .5 más cercana a su media (la misma del "
                             "Resumen), su probabilidad, lo que pasó y si acertó. Un partido suelto puede fallar "
                             "aunque el modelo sea bueno: la fiabilidad se mide con muchos partidos (Validación).")
                 ).grid(row=len(items) + 2, column=0, columnspan=3, sticky="w", pady=(10, 4))

    def _draw(self, cv):
        """Fila estilo SofaScore: valor local · estadística · valor visitante, y barras debajo."""
        spec = next(x for x in self.canvases if x[0] is cv)
        _, label, h, a, decimals = spec
        cv.delete("all")
        w = cv.winfo_width()
        mid = w / 2

        def txt(v):
            if v is None:
                return "-"
            return f"{v:.1f}" if decimals else f"{v:.0f}"
        cv.create_text(14, 14, text=txt(h), anchor="w", fill=FG, font=(FONT, 12, "bold"))
        cv.create_text(mid, 14, text=label, fill=FG, font=(FONT, 10))
        cv.create_text(w - 14, 14, text=txt(a), anchor="e", fill=FG, font=(FONT, 12, "bold"))
        y0, y1 = 30, 36
        cv.create_rectangle(14, y0, mid - 3, y1, fill="#2A3446", outline="")
        cv.create_rectangle(mid + 3, y0, w - 14, y1, fill="#2A3446", outline="")
        if h is not None and a is not None and (h + a) > 0:
            top = max(h, a)
            half = mid - 3 - 14
            cv.create_rectangle(mid - 3 - half * h / top, y0, mid - 3, y1, fill=HOME_C, outline="")
            cv.create_rectangle(mid + 3, y0, mid + 3 + half * a / top, y1, fill=AWAY_C, outline="")


# ---------------------------------------------------------------- partido

class MatchView(tb.Frame):
    def __init__(self, parent, app, event):
        super().__init__(parent)
        self.app, self.conn, self.event = app, app.conn, event
        self.model = model.get_model(self.conn, event["ut_id"])
        self.referee = None
        self.missing = {"home": [], "away": []}
        self.extra_names = {}
        # partido ya empezado o jugado: todo se calcula con lo anterior a su inicio (si no, el propio
        # resultado entraría en el pronóstico)
        self.as_of = event["start_ts"] if event["start_ts"] <= time.time() else None
        self.players = {"home": analysis.player_table(self.conn, event["home_id"], before_ts=self.as_of),
                        "away": analysis.player_table(self.conn, event["away_id"], before_ts=self.as_of)}
        # partidos de liga del último año de cada equipo (para avisar si hay pocos datos)
        year_ago = time.time() - 365 * 86400
        self.n_recent = {side: sum(1 for i in self.model.by_team.get(event[f"{side}_id"], [])
                                   if self.model.matches[i]["ts"] >= year_ago) for side in ("home", "away")}

        self._build_header()
        nb = tb.Notebook(self, bootstyle="primary")
        nb.pack(fill="both", expand=True, pady=(10, 0))
        self.t_sum, self.t_mkt, self.t_line = (tb.Frame(nb, padding=10) for _ in range(3))
        self.t_avg, self.t_h2h, self.t_pl = (tb.Frame(nb, padding=10) for _ in range(3))
        for frame, text in ((self.t_sum, "Resumen"), (self.t_mkt, "Mercados y valor"),
                            (self.t_line, "Alineaciones y árbitro"), (self.t_avg, "Promedios"),
                            (self.t_h2h, "H2H"), (self.t_pl, "Jugadores")):
            nb.add(frame, text=text)
        self._build_summary()
        self._build_markets()
        self._build_lineups()
        self._build_avg()
        self._build_h2h()
        self._build_players()
        self.recalc()
        self.load_stored_live()

    def load_stored_live(self):
        """Árbitro y alineación guardados por la actualización automática (si los hay)."""
        row = self.conn.execute("SELECT referee, lineups, fetched_ts FROM fixture_live WHERE event_id=?",
                                (self.event["id"],)).fetchone()
        if not row:
            return
        lineups = {side: [tuple(x) for x in pl] for side, pl in json.loads(row["lineups"] or "{}").items()}
        self._on_live({"espn": {"referee": row["referee"], "lineups": lineups}, "sofa": None, "errors": [
            f"actualizado automáticamente a las {datetime.fromtimestamp(row['fetched_ts']):%H:%M}"]}, None)

    # -- cabecera
    def _build_header(self):
        e = self.event
        h = card(self, padding=(16, 12))
        h.pack(fill="x")
        row = tb.Frame(h, style="Card.TFrame")
        row.pack(fill="x")
        tb.Label(row, text="Ver equipo:", style="CardMuted.TLabel").pack(side="left", padx=(0, 6))
        tb.Button(row, text=e["home_name"], bootstyle="primary-outline",
                  command=lambda: self.app.open_team(e["home_id"], e["home_name"])).pack(side="left", padx=(0, 6))
        tb.Button(row, text=e["away_name"], bootstyle="warning-outline",
                  command=lambda: self.app.open_team(e["away_id"], e["away_name"])).pack(side="left")
        if e.get("status") == "finished" and self.conn.execute(
                "SELECT 1 FROM predictions WHERE event_id=?", (e["id"],)).fetchone():
            tb.Button(row, text=f"✅  Predicción vs resultado ({e['home_goals']}-{e['away_goals']})",
                      bootstyle="success-outline",
                      command=lambda: self.app.open_comparison(e["id"], f"{e['home_name']}  vs  {e['away_name']}")
                      ).pack(side="left", padx=(16, 0))
        info = f"{LEAGUES.get(e['ut_id'])}  ·  {fdate(e['start_ts'], long=True)}"
        tb.Label(row, text=info, style="CardMuted.TLabel").pack(side="right")
        self.bar = ProbBar(h)
        self.bar.pack(fill="x", pady=(10, 4))
        legend = tb.Frame(h, style="Card.TFrame")
        legend.pack(fill="x")
        self.leg = []
        for col, color in enumerate((HOME_C, DRAW_C, AWAY_C)):
            lbl = tb.Label(legend, text="", style="Card.TLabel", foreground=color, font=(FONT, 10, "bold"))
            lbl.grid(row=0, column=col, sticky="w", padx=(0, 40))
            self.leg.append(lbl)
        self.ref_lbl = tb.Label(h, text="", style="CardMuted.TLabel")
        self.ref_lbl.pack(anchor="w", pady=(6, 0))
        self.adj_lbl = tb.Label(h, text="", style="CardMuted.TLabel", justify="left", wraplength=1100)
        self.adj_lbl.pack(anchor="w")

    # -- resumen
    def _build_summary(self):
        f = self.t_sum
        tiles, self.tiles = tiles_row(f, [(k, t, "-", FG, "") for k, t in (
            ("pick", "Pronóstico"), ("goals", "Goles esp."), ("o25", "Más de 2.5"), ("btts", "Ambos marcan"),
            ("fouls", "Faltas"), ("yellow", "Amarillas"), ("corners", "Córners"))])
        tiles.pack(fill="x")
        body = tb.Frame(f)
        body.pack(fill="both", expand=True, pady=(10, 0))
        left = card(body, "Estadísticas esperadas")
        left.pack(side="left", fill="both", expand=True)
        fr, self.exp_tree = make_table(left, [("m", "Estadística"), ("h", self.event["home_name"]),
                                              ("a", self.event["away_name"]), ("t", "Total del partido")],
                                       {"m": 110, "h": 175, "a": 175, "t": 175}, 10)
        self.exp_tree.tag_configure("pick_ok", foreground=GOOD)
        self.exp_tree.tag_configure("rel_ok", foreground=GOOD)
        self.exp_tree.tag_configure("rel_mid", foreground=WARN)
        self.exp_tree.tag_configure("rel_bad", foreground=BAD)
        fr.pack(fill="both", expand=True)
        self.exp_tree.bind("<ButtonRelease-1>", self._exp_click)
        legend = tk.Frame(left, bg=CARD)
        legend.pack(anchor="w", pady=(6, 0))
        tk.Label(legend, text="Cada casilla: promedio · línea que prefiere el modelo (probabilidad).   Color de la fila:",
                 bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side="left")
        for text, color in (("fiable", GOOD), ("regular", WARN), ("no fiable", BAD), ("sin validar", FG)):
            tk.Label(legend, text=f"  ■ {text}", bg=CARD, fg=color, font=(FONT, 9, "bold")).pack(side="left")
        tb.Label(left, text=("Clic en un número para ver sus líneas y probabilidades →   ·   clic en el nombre de la "
                             "estadística para ver su fiabilidad en la liga"),
                 style="CardMuted.TLabel").pack(anchor="w", pady=(2, 0))
        self.scores_lbl = tb.Label(left, text="", style="Card.TLabel", font=(FONT, 10), justify="left")

        for key, metric in (("goals", "gx"), ("fouls", "fouls"), ("yellow", "yellow"), ("corners", "corners")):
            on_click(self.tiles[key], lambda m=metric: self.show_dist(m, "total"))
        on_click(self.tiles["o25"], lambda: self.show_dist("gx", "total"))
        on_click(self.tiles["pick"], self.show_pick)
        on_click(self.tiles["btts"], self.show_btts)
        right = tb.Frame(body, width=450)
        right.pack(side="left", fill="y", padx=(10, 0))
        right.pack_propagate(False)
        self.detail = DetailPanel(right)
        self.detail.pack(fill="both", expand=True)
        self._detail_last = self.show_pick

    # -- mercados
    def _build_markets(self):
        f = self.t_mkt
        top = card(f, "Calculadora de valor — ¿la casa paga más de lo que vale?")
        top.pack(fill="x")
        row = tb.Frame(top, style="Card.TFrame")
        row.pack(fill="x")
        tb.Label(row, text="Mercado", style="Card.TLabel").pack(side="left")
        self.calc_market = tb.Combobox(row, state="readonly", width=34, bootstyle="primary")
        self.calc_market.pack(side="left", padx=(6, 14))
        self.calc_market.bind("<<ComboboxSelected>>", lambda e: self._calc_default_line())
        entries = {}
        for key, label in (("line", "Línea"), ("over", "Cuota Más"), ("under", "Cuota Menos")):
            tb.Label(row, text=label, style="Card.TLabel").pack(side="left")
            entries[key] = tb.Entry(row, width=7)
            entries[key].pack(side="left", padx=(6, 14))
            entries[key].bind("<Return>", lambda e: self.calc())
        self.calc_line, self.calc_over, self.calc_under = entries["line"], entries["over"], entries["under"]
        tb.Button(row, text="Calcular", bootstyle="primary", command=self.calc).pack(side="left")
        self.calc_out = tb.Label(top, text=("Ejemplo: «Faltas · Fiorentina», línea 11.5, cuota Más 1.88 → probabilidad "
                                            "real según el modelo, cuota justa y si hay valor."),
                                 style="Card.TLabel", font=(FONT, 11), justify="left")
        self.calc_out.pack(anchor="w", pady=(10, 0))

        legend = tb.Frame(f)
        legend.pack(fill="x", pady=(10, 4))
        for text, bg, fg in (("  ≥ 60 % y mercado fiable en esta liga  ", GOOD, "white"),
                             ("  ≥ 60 % pero mercado no validado: con cuidado  ", WARN, "#1A1A1A")):
            tk.Label(legend, text=text, bg=bg, fg=fg, font=(FONT, 9, "bold")).pack(side="left", padx=(0, 8))
        tb.Label(legend, text=("Fiabilidad = aciertos del modelo en ese mercado en los últimos 250 partidos de la liga "
                               "(validación). Cuota justa = 1 / probabilidad. Clic en una fila → la lleva a la calculadora."),
                 style="Muted.TLabel", wraplength=480, justify="left").pack(side="left")
        outer, self.mkt_inner = scroll_area(f)
        outer.pack(fill="both", expand=True)

    def _market_options(self):
        e = self.event
        opts = []
        for metric, label in [("gx", "Goles")] + model.MARKETS:
            opts += [(metric, "total", f"{label} · Total"), (metric, "home", f"{label} · {e['home_name']}"),
                     (metric, "away", f"{label} · {e['away_name']}")]
        return opts

    def _prob_over(self, metric, who, line):
        if metric == "gx":
            lam = {"home": self.pred["lam_h"], "away": self.pred["lam_a"],
                   "total": self.pred["lam_h"] + self.pred["lam_a"]}[who]
            return model.prob_over(lam, None, line)
        return self.model.market_probs(metric, self.pred["means"], line, who)

    def _mean_of(self, metric, who):
        mh, ma = self.pred["means"][metric]
        if mh is None:
            return None
        return {"home": mh, "away": ma, "total": mh + ma}[who]

    def _calc_default_line(self):
        opt = self._market_options()[self.calc_market.current()]
        mu = self._mean_of(opt[0], opt[1])
        if mu is not None:
            self.calc_line.delete(0, "end")
            self.calc_line.insert(0, f"{int(mu) + 0.5:.1f}")

    def _pick_from_table(self, idx, line):
        """Doble clic en una fila de la tabla -> la carga en la calculadora."""
        self.calc_market.current(idx)
        self.calc_line.delete(0, "end")
        self.calc_line.insert(0, f"{line:.1f}")
        self.calc_over.focus_set()
        self.calc()

    def _reliab(self, metric):
        return self.model.reliability.get(metric, {}).get("status")

    EXP_KEYS = ("gx", "shots", "shots_on", "corners", "fouls", "yellow", "red", "offsides", "saves", "possession")

    def _exp_click(self, e):
        iid = self.exp_tree.identify_row(e.y)
        col = self.exp_tree.identify_column(e.x)
        if not iid or not col:
            return
        metric = self.EXP_KEYS[self.exp_tree.index(iid)]
        c = int(col[1:]) - 1
        if metric == "possession":
            self._set_detail(self.show_possession)
        elif c == 0:
            self._set_detail(lambda: self.show_reliab(metric))
        else:
            self._set_detail(lambda: self.show_dist(metric, {1: "home", 2: "away"}.get(c, "total")))

    def _set_detail(self, fn):
        self._detail_last = fn
        fn()

    def _reliab_line(self, metric):
        status = self._reliab(metric)
        rel = self.model.reliability.get(metric, {})
        if not status:
            return "Fiabilidad en la liga: sin validar (pocos casos)", MUTED
        return (f"Fiabilidad en la liga: {status} ({pct(rel.get('hit'))} de aciertos)",
                RELIAB_STYLE[status][1])

    def show_dist(self, metric, who):
        self._detail_last = lambda: self.show_dist(metric, who)
        mu = self._mean_of(metric, who)
        e = self.event
        name = {"home": e["home_name"], "away": e["away_name"], "total": "Total del partido"}[who]
        label = "goles" if metric == "gx" else dict(METRICS).get(metric, metric).lower()
        if mu is None:
            self.detail.show(f"{name} · {label}", "Sin datos suficientes para esta estadística.")
            return
        r = None if metric == "gx" else self.model.params[metric]["r_tot" if who == "total" else "r_side"]
        pmf = model.nb_pmf(mu, r, int(mu * 3) + 8)
        mode = max(range(len(pmf)), key=pmf.__getitem__)
        lo = max(0, mode - 3)
        bars = [(k, pmf[k], k == mode) for k in range(lo, mode + 4)]
        lines = []
        for line in model.lines_around(mu, 3):
            over = 1 - sum(pmf[:int(line) + 1])
            color = GOOD if max(over, 1 - over) >= 0.6 else FG
            lines.append(((f"Más de {line}:  {pct(over)}  ({fair(over)})",
                           f"Menos:  {pct(1 - over)}  ({fair(1 - over)})"), color))
        safe = self.safe_lines(metric, who)
        if safe:
            lines.append((tuple(f"≥70 %  {t}" for t in safe) if len(safe) == 2 else f"≥70 %  {safe[0]}", GOOD))
        notes = []
        for side in ("home", "away"):
            if who in (side, "total") and self.n_recent[side] < 12:
                notes.append(f"⚠ {e[side + '_name']}: solo {self.n_recent[side]} partidos de liga recientes")
        side, line, prob = self.line_pick(metric, who)
        self.detail.show(f"{name} · {label}",
                         f"Promedio esperado {mu:.2f}. Lo más probable: {mode} ({pct(pmf[mode])}).\n"
                         f"Pronóstico: {side} de {line} ({pct(prob)}).",
                         bars, lines, "\n".join(notes) or "Entre paréntesis: cuota justa.")

    def line_pick(self, metric, who):
        """Lado (Más/Menos) que prefiere el modelo en la línea .5 más cercana a su media."""
        mu = self._mean_of(metric, who)
        line = math.floor(mu) + 0.5
        p = self._prob_over(metric, who, line)
        return ("Más", line, p) if p >= 0.5 else ("Menos", line, 1 - p)

    def safe_lines(self, metric, who, target=0.7):
        """La línea más alta con Más ≥ 70 % y la más baja con Menos ≥ 70 %."""
        mu = self._mean_of(metric, who)
        if mu is None:
            return []
        out = []
        line = math.floor(mu) - 0.5
        while line > 0 and self._prob_over(metric, who, line) < target:
            line -= 1
        if line > 0:
            out.append(f"Más de {line} ({pct(self._prob_over(metric, who, line))})")
        line = math.floor(mu) + 1.5
        while 1 - self._prob_over(metric, who, line) < target and line < mu * 3 + 5:
            line += 1
        if line < mu * 3 + 5:
            out.append(f"Menos de {line} ({pct(1 - self._prob_over(metric, who, line))})")
        return out

    def show_pick(self):
        self._detail_last = self.show_pick
        p, e = self.pred, self.event
        bars = [(e["home_name"][:10], p["p_home"], True), ("Empate", p["p_draw"], False),
                (e["away_name"][:10], p["p_away"], False)]
        best = max(range(3), key=lambda i: bars[i][1])
        bars = [(lab, pr, i == best) for i, (lab, pr, _) in enumerate(bars)]
        lines = [(f"{n}: {pct(pr)} · cuota justa {fair(pr)}", FG) for n, pr in
                 ((e["home_name"], p["p_home"]), ("Empate", p["p_draw"]), (e["away_name"], p["p_away"]))]
        lines.append(self._reliab_line("gx"))
        scores = "   ".join(f"{sc} ({pct(pr)})" for pr, sc in p["scores"][:4])
        self.detail.show("¿Quién gana?", f"Lo más probable: {bars[best][0]} ({pct(bars[best][1])}).", bars, lines,
                         f"Marcadores más probables: {scores}")

    def show_btts(self):
        self._detail_last = self.show_btts
        b = self.pred["btts"]
        self.detail.show("Ambos marcan", f"Que los dos equipos hagan al menos 1 gol: {pct(b)}.",
                         [("Sí", b, b >= 0.5), ("No", 1 - b, b < 0.5)],
                         [(f"Sí: cuota justa {fair(b)}   ·   No: cuota justa {fair(1 - b)}", FG)])

    def show_possession(self):
        self._detail_last = self.show_possession
        h, a = self.pred["means"]["possession"]
        if h is None:
            self.detail.show("Posesión", "Sin datos suficientes.")
            return
        e = self.event
        self.detail.show("Posesión esperada", f"{e['home_name']} {h:.0f} %  ·  {e['away_name']} {a:.0f} %",
                         [(e["home_name"][:10], h / 100, h >= a), (e["away_name"][:10], a / 100, a > h)])

    def show_reliab(self, metric):
        self._detail_last = lambda: self.show_reliab(metric)
        label = "Goles (más/menos 2.5)" if metric == "gx" else dict(METRICS).get(metric, metric)
        rel = self.model.reliability.get(metric)
        if not rel:
            self.detail.show(f"Fiabilidad · {label}", "Sin validar",
                             note="En los últimos 250 partidos de la liga el modelo casi nunca tuvo confianza alta "
                                  "en este mercado, así que no se puede medir si acierta. Úsalo con cuidado.")
            return
        self.detail.show(f"Fiabilidad · {label}",
                         f"Acertó el {pct(rel['hit'])} de {rel['n']} veces → {rel['status']}.",
                         [("Aciertos", rel["hit"], True), ("Fallos", 1 - rel["hit"], False)],
                         [("fiable ≥ 58 %   ·   regular 53–58 %   ·   no fiable < 53 %", MUTED)],
                         "Medido en los últimos 250 partidos de la liga, que el modelo no vio al calibrarse, "
                         "cuando daba 55–60 % o más a un lado.", RELIAB_STYLE[rel["status"]][1])

    def calc(self):
        try:
            metric, who, label = self._market_options()[self.calc_market.current()]
            line = float(self.calc_line.get().replace(",", "."))
        except (ValueError, IndexError):
            self.calc_out.configure(text="Escribe una línea válida, por ejemplo 11.5", foreground=WARN)
            return
        p = self._prob_over(metric, who, line)
        if p is None:
            self.calc_out.configure(text="Sin datos suficientes para este mercado.", foreground=WARN)
            return
        lines = [f"{label}  ·  línea {line}  ·  media esperada {fmt(self._mean_of(metric, who))}",
                 f"Más de {line}: {pct(p)}  (cuota justa {fair(p)})        Menos de {line}: {pct(1 - p)}  "
                 f"(cuota justa {fair(1 - p)})"]
        best = None
        for side, prob, entry in (("Más", p, self.calc_over), ("Menos", 1 - p, self.calc_under)):
            try:
                odds = float(entry.get().replace(",", "."))
            except ValueError:
                continue
            ev = prob * odds - 1
            kelly = max(0.0, ev / (odds - 1)) if odds > 1 else 0
            lines.append(f"{side} @ {odds:.2f}:  valor esperado {ev:+.1%} por unidad"
                         + (f"   ·   Kelly ¼ sugiere {kelly / 4:.1%} del bank" if ev > 0 else ""))
            if best is None or ev > best[1]:
                best = (side, ev)
        color = FG
        status = self._reliab(metric)
        if best:
            if best[1] > 0.04 and status != "fiable":
                rel = self.model.reliability.get(metric, {})
                lines.append(f"⚠ Valor en {best[0]} ({best[1]:+.1%}), pero este mercado "
                             + (f"es «{status}» en esta liga (acierto {pct(rel.get('hit'))})" if status
                                else "aún no está validado en esta liga") + ": con cuidado")
                color = WARN
            elif best[1] > 0.04:
                lines.append(f"✅ Hay valor en {best[0]} ({best[1]:+.1%}) y el mercado es fiable en esta liga")
                color = GOOD
            elif best[1] > 0:
                lines.append(f"≈ Valor pequeño en {best[0]} ({best[1]:+.1%}): menor que el margen de error del modelo")
                color = WARN
            else:
                lines.append("❌ Sin valor: la casa paga menos de lo que vale según el modelo")
                color = BAD
        self.calc_out.configure(text="\n".join(lines), foreground=color)

    def _fill_markets(self):
        inner = self.mkt_inner
        for w in inner.winfo_children():
            w.destroy()
        opts = self._market_options()
        heads = ["Mercado", "Fiabilidad", "Media", "Línea", "P(Más)", "Cuota justa", "P(Menos)", "Cuota justa"]
        for c, h in enumerate(heads):
            tk.Label(inner, text=h, bg=HOME_C, fg="white", font=(FONT, 10, "bold"), pady=6).grid(
                row=0, column=c, sticky="nsew")
            inner.columnconfigure(c, weight=3 if c == 0 else 1)
        row = 1
        for idx, (metric, who, label) in enumerate(opts):
            mu = self._mean_of(metric, who)
            if mu is None:
                continue
            status = self._reliab(metric)
            rel_txt, rel_fg = RELIAB_STYLE.get(status, ("sin validar", MUTED))
            hit = self.model.reliability.get(metric, {}).get("hit")
            if status and hit is not None:
                rel_txt += f" {pct(hit)}"
            bg = CARD if idx % 2 == 0 else "#172031"
            for i, line in enumerate(model.lines_around(mu, 5 if who == "total" else 3)):
                p = self._prob_over(metric, who, line)
                cells = [(label if i == 0 else "", FG, bg, True), (rel_txt if i == 0 else "", rel_fg, bg, True),
                         (fmt(mu) if i == 0 else "", FG, bg, True), (f"{line:.1f}", FG, bg, False)]
                for prob in (p, 1 - p):
                    if prob >= 0.6:
                        cbg, cfg = (GOOD, "white") if status == "fiable" else (WARN, "#1A1A1A")
                    else:
                        cbg, cfg = bg, "#B8C2D3"
                    cells += [(pct(prob), cfg, cbg, prob >= 0.6), (fair(prob), cfg, cbg, False)]
                for c, (text, fg, cbg, bold) in enumerate(cells):
                    lbl = tk.Label(inner, text=text, bg=cbg, fg=fg, anchor="w" if c == 0 else "center",
                                   font=(FONT, 10, "bold" if bold and text else "normal"), padx=8, pady=3)
                    lbl.grid(row=row, column=c, sticky="nsew", padx=(0, 1), pady=(0, 1))
                    lbl.bind("<Button-1>", lambda e, ix=idx, ln=line: self._pick_from_table(ix, ln))
                    lbl.configure(cursor="hand2")
                row += 1
        self.calc_market["values"] = [o[2] for o in opts]
        if self.calc_market.current() < 0:
            self.calc_market.current(next((i for i, o in enumerate(opts) if o[0] == "fouls"), 0))
            self._calc_default_line()

    # -- alineaciones y árbitro
    def _build_lineups(self):
        f = self.t_line
        top = tb.Frame(f)
        top.pack(fill="x")
        self.btn_live = tb.Button(top, text="⟳ Traer árbitro y alineaciones",
                                  bootstyle="info", command=self.fetch_live)
        self.btn_live.pack(side="right")
        tb.Label(top, text=("Marca los 11 que crees que jugarán (Ctrl+clic). El XI cambia goles, faltas y tarjetas "
                            "esperadas según lo que aporta cada jugador."), style="Muted.TLabel",
                 wraplength=800, justify="left").pack(side="left", fill="x", expand=True)
        reff = tb.Frame(f)
        reff.pack(fill="x", pady=(8, 0))
        tb.Label(reff, text="Árbitro:").pack(side="left")
        refs = sorted(self.model.by_ref, key=lambda r: -len(self.model.by_ref[r]))
        self.ref_combo = tb.Combobox(reff, values=["(desconocido)"] + refs, width=30, bootstyle="primary")
        self.ref_combo.current(0)
        self.ref_combo.pack(side="left", padx=6)
        self.ref_combo.bind("<<ComboboxSelected>>", lambda e: self._set_ref(self.ref_combo.get()))
        tb.Label(reff, text="si ya se sabe por noticias, elígelo aquí: cambia faltas y tarjetas esperadas",
                 style="Muted.TLabel").pack(side="left")

        cols = tb.Frame(f)
        cols.pack(fill="both", expand=True, pady=8)
        self.lb, self.lb_ids, self.lb_count, self.miss_lbl, self.lu_info = {}, {}, {}, {}, {}
        for i, side in enumerate(("home", "away")):
            c = card(cols, None, padding=10)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            cols.columnconfigure(i, weight=1)
            cols.rowconfigure(0, weight=1)
            tb.Label(c, text=self.event[f"{side}_name"], style="Card.TLabel", font=(FONT, 13, "bold"),
                     foreground=HOME_C if side == "home" else AWAY_C).pack(anchor="w")
            self.lu_info[side] = tb.Label(c, text="XI habitual (más titularidades recientes)", style="CardMuted.TLabel")
            self.lu_info[side].pack(anchor="w")
            tb.Label(c, text="    Jugador                aporte  tit  f/90", style="CardMuted.TLabel",
                     font=("Consolas", 9)).pack(anchor="w", pady=(6, 0))
            lb = styled_listbox(c, 20)
            lb.pack(fill="both", expand=True, pady=4)
            lb.bind("<<ListboxSelect>>", lambda e, s=side: self._update_count(s))
            self.lb[side] = lb
            self.lb_count[side] = tb.Label(c, text="", style="Card.TLabel")
            self.lb_count[side].pack(anchor="w")
            self.miss_lbl[side] = tb.Label(c, text="", style="Card.TLabel", foreground=BAD, wraplength=560,
                                           justify="left")
            self.miss_lbl[side].pack(anchor="w")
            self._fill_listbox(side, analysis.typical_xi(self.players[side]))
        btns = tb.Frame(f)
        btns.pack(fill="x")
        tb.Button(btns, text="✔ Recalcular con estas alineaciones", bootstyle="success", command=self.recalc).pack(
            side="left")
        tb.Button(btns, text="Volver al XI habitual", bootstyle="secondary", command=self._reset_xi).pack(
            side="left", padx=8)

    def _set_ref(self, name):
        self.referee = None if not name or name.startswith("(") else name
        self.recalc()

    def _reset_xi(self):
        for side in ("home", "away"):
            self._fill_listbox(side, analysis.typical_xi(self.players[side]))
        self.recalc()

    def _fill_listbox(self, side, selected_ids, extra=None):
        players = self.players[side]
        order = {"G": 0, "D": 1, "M": 2, "F": 3}
        ids = [pid for pid, p in players.items() if p["in_recent"]]
        for pid, name, pos in extra or []:
            self.extra_names[pid] = (name, pos)
            if pid not in ids:
                ids.append(pid)
        missing_ids = {m[0] for m in self.missing[side]}

        def info(pid):
            p = players.get(pid)
            return (p["name"], p["position"] or "-") if p else self.extra_names.get(pid, (str(pid), "-"))

        ids.sort(key=lambda i: (order.get(info(i)[1], 4), -(players.get(i, {}).get("aporte", -9))))
        lb = self.lb[side]
        lb.delete(0, "end")
        self.lb_ids[side] = ids
        for idx, pid in enumerate(ids):
            name, pos = info(pid)
            p = players.get(pid)
            stats = (f"{fmt(p['aporte']):>6} {p['starts']:>4} {fmt(p['fouls90'], 1):>5}" if p else "  sin datos")
            flag = "  ✚ BAJA" if pid in missing_ids else ""
            lb.insert("end", f" {pos or '-':<2} {(name or '')[:22]:<22} {stats}{flag}")
            if pid in missing_ids:
                lb.itemconfig(idx, foreground=BAD)
            if pid in selected_ids:
                lb.selection_set(idx)
        self._update_count(side)

    def _update_count(self, side):
        n = len(self.lb[side].curselection())
        self.lb_count[side].configure(text=f"Seleccionados: {n}" + ("" if n == 11 else "  (deberían ser 11)"),
                                      foreground=FG if n == 11 else WARN)

    def selected(self, side):
        return [self.lb_ids[side][i] for i in self.lb[side].curselection()]

    def fetch_live(self):
        self.btn_live.configure(state="disabled", text="Consultando…")
        ev = self.event

        def job():
            conn = db.connect(DB_PATH)
            out = {"espn": None, "sofa": None, "errors": []}
            try:
                out["espn"] = espn.fetch_fixture(conn, ev)
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"ESPN: {e}")
            try:
                out["sofa"] = asyncio.run(scraper.fetch_fixture_live(ev))
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"SofaScore no disponible ({str(e)[:60]})")
            conn.close()
            return out
        run_in_thread(job, self._on_live, self)

    def _on_live(self, res, err):
        self.btn_live.configure(state="normal", text="⟳ Traer árbitro y alineaciones")
        if err:
            self.ref_lbl.configure(text=f"Error: {err}")
            return
        notes = []
        es = res["espn"] or {}
        sofa_lineups, sofa_event = res["sofa"] or (None, None)
        ref = es.get("referee") or ((sofa_event or {}).get("referee") or {}).get("name")
        if ref:
            matched = self.model.match_ref(ref)
            self.referee = matched or ref
            self.ref_combo.set(self.referee)
            notes.append(f"Árbitro: {ref}" + ("" if matched else " (sin historial en la base)"))
        got_lineup = False
        for side in ("home", "away"):
            known = [(pid, p["name"]) for pid, p in self.players[side].items()]

            def pid_of(pid, name):
                return pid if pid in self.players[side] else (best_match(name, known, 0.8) or pid)

            starters, extra, state = [], [], None
            if (es.get("lineups") or {}).get(side):
                for pid, name, pos, starter in es["lineups"][side]:
                    p = pid_of(pid, name)
                    extra.append((p, name, pos))
                    if starter:
                        starters.append(p)
                state = "CONFIRMADA (ESPN)"
            elif sofa_lineups and sofa_lineups.get(side):
                for p in sofa_lineups[side].get("players", []):
                    pl = p["player"]
                    pid = pid_of(pl["id"], pl.get("name"))
                    extra.append((pid, pl.get("name"), p.get("position") or pl.get("position")))
                    if not p.get("substitute"):
                        starters.append(pid)
                state = ("CONFIRMADA" if sofa_lineups.get("confirmed") else "probable") + " (SofaScore)"
            if sofa_lineups and sofa_lineups.get(side):
                self.missing[side] = []
                for m in sofa_lineups[side].get("missingPlayers", []):
                    pl = m["player"]
                    pid = pid_of(pl["id"], pl.get("name"))
                    extra.append((pid, pl.get("name"), pl.get("position")))
                    self.missing[side].append((pid, pl.get("name"), m.get("type"),
                                               MISSING_REASON.get(m.get("reason"), "")))
                self.miss_lbl[side].configure(text=("Bajas / dudas: " + ", ".join(
                    f"{n} ({'duda' if t == 'doubtful' else 'baja'}{': ' + r if r else ''})"
                    for _, n, t, r in self.missing[side])) if self.missing[side] else "Sin bajas reportadas")
            if state:
                got_lineup = True
                self._fill_listbox(side, starters, extra)
                self.lu_info[side].configure(text=f"Alineación {state}")
        if not got_lineup:
            notes.append("Aún no hay alineaciones publicadas: se usa el XI habitual")
        notes += res["errors"]
        self.ref_lbl.configure(text="  ·  ".join(notes))
        self.recalc()

    # -- promedios / h2h / jugadores
    def _build_avg(self):
        e = self.event
        since = int(time.time()) - 600 * 86400
        hh = analysis.profile(analysis.team_matches(self.conn, e["home_id"], "home", e["ut_id"], since))
        aa = analysis.profile(analysis.team_matches(self.conn, e["away_id"], "away", e["ut_id"], since))
        ha = analysis.profile(analysis.team_matches(self.conn, e["home_id"], "all", e["ut_id"], since, 10))
        al = analysis.profile(analysis.team_matches(self.conn, e["away_id"], "all", e["ut_id"], since, 10))
        top = tb.Frame(self.t_avg)
        top.pack(fill="x", pady=(0, 8))
        for name, p, form, side, color in ((e["home_name"], hh, ha["form"], "en casa", HOME_C),
                                           (e["away_name"], aa, al["form"], "fuera", AWAY_C)):
            c = card(top, padding=(14, 10))
            c.pack(side="left", padx=(0, 10))
            tb.Label(c, text=f"{name} {side}", style="Card.TLabel", foreground=color,
                     font=(FONT, 12, "bold")).pack(anchor="w")
            tb.Label(c, text=f"{p['n']} PJ · {p['G']}G {p['E']}E {p['P']}P (~2 temporadas)",
                     style="CardMuted.TLabel").pack(anchor="w")
            form_chips(c, form).pack(anchor="w", pady=(4, 0))
        cols = [("m", "Promedio por partido"), ("hh_f", "Local en casa (a favor)"), ("hh_c", "Local en casa (en contra)"),
                ("aa_f", "Visit. fuera (a favor)"), ("aa_c", "Visit. fuera (en contra)"),
                ("h10", "Local últ. 10"), ("a10", "Visit. últ. 10")]
        fr, tree = make_table(self.t_avg, cols, {"m": 200, **{c: 150 for c, _ in cols[1:]}}, 16)
        fr.pack(fill="both", expand=True)
        insert_rows(tree, [(label, fmt(hh[m][0]), fmt(hh[m][1]), fmt(aa[m][0]), fmt(aa[m][1]), fmt(ha[m][0]),
                            fmt(al[m][0])) for m, label in METRICS])

    def _build_h2h(self):
        e = self.event
        s, rows = analysis.h2h(self.conn, e["home_id"], e["away_id"])
        tiles = tb.Frame(self.t_h2h)
        tiles.pack(fill="x", pady=(0, 8))
        for title, val, color in ((f"Gana {e['home_name']}", s["a_wins"], HOME_C), ("Empates", s["draws"], DRAW_C),
                                  (f"Gana {e['away_name']}", s["b_wins"], AWAY_C), ("Goles / partido", fmt(s["goals"]), FG),
                                  ("Faltas / partido", fmt(s["fouls"], 1), FG),
                                  ("Amarillas / partido", fmt(s["cards"]), FG),
                                  ("Córners / partido", fmt(s["corners"], 1), FG)):
            tile(tiles, title[:22], str(val), color).pack(side="left", fill="x", expand=True, padx=(0, 8))
        fr, tree = make_table(self.t_h2h, [("fecha", "Fecha"), ("comp", "Competición"), ("local", "Local"),
                                           ("res", "Res."), ("visit", "Visitante"), ("faltas", "Faltas L-V"),
                                           ("amar", "Amarillas L-V"), ("corn", "Córners L-V"), ("arb", "Árbitro")],
                              {"comp": 140, "local": 170, "visit": 170, "arb": 150}, 14)
        fr.pack(fill="both", expand=True)

        def pair(r, a, b):
            return "-" if r[a] is None or r[b] is None else f"{r[a]:.0f}-{r[b]:.0f}"

        def tag(r):
            if r["home_goals"] == r["away_goals"]:
                return ("draw",)
            home_won = r["home_goals"] > r["away_goals"]
            return ("home",) if home_won == (r["home_id"] == e["home_id"]) else ("away",)
        insert_rows(tree, [((datetime.fromtimestamp(r["start_ts"]).strftime("%d/%m/%Y"), r["ut_name"], r["home_name"],
                             f"{r['home_goals']}-{r['away_goals']}", r["away_name"], pair(r, "hf", "af"),
                             pair(r, "hy", "ay"), pair(r, "hc", "ac"), r.get("referee") or "-"), tag(r))
                           for r in rows])

    def _build_players(self):
        nb = tb.Notebook(self.t_pl, bootstyle="info")
        nb.pack(fill="both", expand=True)
        for side in ("home", "away"):
            fr = tb.Frame(nb, padding=4)
            nb.add(fr, text=self.event[f"{side}_name"])
            t, tree = make_table(fr, PLAYER_COLS, PLAYER_WIDTHS, 20)
            t.pack(fill="both", expand=True)
            fill_player_tree(tree, self.players[side])

    # -- cálculo
    def recalc(self):
        e = self.event
        base = self.model.predict(e["home_id"], e["away_id"], as_of=self.as_of, ref=self.referee)
        bm = base["means"]
        fh = analysis.lineup_factors(self.players["home"], self.selected("home"), base["lam_h"], bm["fouls"][0],
                                     bm["yellow"][0])
        fa = analysis.lineup_factors(self.players["away"], self.selected("away"), base["lam_a"], bm["fouls"][1],
                                     bm["yellow"][1])
        lineup = {"gx_h": fh["att"] * fa["opp"], "gx_a": fa["att"] * fh["opp"], "fouls_h": fh["fouls"],
                  "fouls_a": fa["fouls"], "yellow_h": fh["yellow"], "yellow_a": fa["yellow"]}
        self.pred = p = self.model.predict(e["home_id"], e["away_id"], as_of=self.as_of, ref=self.referee,
                                           lineup=lineup)
        informed = self.referee or any(abs(v - 1) > 1e-6 for v in lineup.values())
        tracking.snapshot(self.conn, self.model, e, p, "pre_xi" if informed else "pre")
        self.conn.commit()
        self.bar.set(p["p_home"], p["p_draw"], p["p_away"])
        for lbl, prob, name in zip(self.leg, (p["p_home"], p["p_draw"], p["p_away"]),
                                   (e["home_name"], "Empate", e["away_name"])):
            lbl.configure(text=f"{name}  {pct(prob)}   cuota justa {fair(prob)}")
        probs = (p["p_home"], p["p_draw"], p["p_away"])
        best = probs.index(max(probs))
        self.tiles["pick"].value.configure(text=(e["home_name"], "Empate", e["away_name"])[best][:13],
                                           foreground=(HOME_C, DRAW_C, AWAY_C)[best])
        self.tiles["pick"].sub.configure(text=f"{pct(max(probs))} de probabilidad")
        self.tiles["goals"].value.configure(text=f"{p['lam_h']:.1f} - {p['lam_a']:.1f}")
        self.tiles["goals"].sub.configure(text=f"total {p['lam_h'] + p['lam_a']:.2f}")
        self.tiles["o25"].value.configure(text=pct(p["over25"]), foreground=GOOD if p["over25"] >= 0.55 else FG)
        self.tiles["o25"].sub.configure(text=f"+1.5 {pct(p['over15'])} · +3.5 {pct(p['over35'])}")
        self.tiles["btts"].value.configure(text=pct(p["btts"]))
        self.tiles["btts"].sub.configure(text=f"cuota justa {fair(p['btts'])}")
        for k in ("fouls", "yellow", "corners"):
            mh, ma = p["means"][k]
            self.tiles[k].value.configure(text=fmt(mh + ma, 1) if mh is not None else "-")
            self.tiles[k].sub.configure(text=f"{fmt(mh, 1)} - {fmt(ma, 1)}" if mh is not None else "sin datos")

        labels = dict(METRICS)
        rows = []
        for key in ("gx", "shots", "shots_on", "corners", "fouls", "yellow", "red", "offsides", "saves", "possession"):
            h, a = p["means"].get(key, (None, None))
            rel = self.model.reliability.get(key, {})
            status = rel.get("status")
            if key in ("red", "possession"):
                rel_txt, tag = "", ()
            elif status:
                rel_txt = f"{RELIAB_STYLE[status][0]} {pct(rel.get('hit'))}"
                tag = ({"fiable": "rel_ok", "regular": "rel_mid", "no fiable": "rel_bad"}[status],)
            else:
                rel_txt, tag = "sin validar", ()

            def cell(who, mu):
                """Promedio y, para conteos, la línea que prefiere el modelo: «5.95 · Más 5.5 (54%)»."""
                if mu is None:
                    return "-"
                if key in ("red", "possession"):
                    return fmt(mu)
                side, line, prob = self.line_pick(key, who)
                return f"{mu:.1f} · {side} {line} ({pct(prob)})"
            tot = None if key == "possession" or h is None else h + a
            short = {"gx": "Goles", "corners": "Córners", "yellow": "Amarillas", "red": "Rojas", "shots": "Tiros",
                     "shots_on": "Tiros a puerta", "possession": "Posesión %"}
            rows.append(((short.get(key, labels.get(key, key)), cell("home", h), cell("away", a),
                          "" if tot is None else cell("total", tot)), tag))
        insert_rows(self.exp_tree, rows)
        self.scores_lbl.configure(text="Marcadores más probables:   " +
                                  "   ".join(f"{s} ({pct(pr)})" for pr, s in p["scores"]))
        self._detail_last()

        adj = []
        if p.get("ref"):
            adj.append(f"Árbitro {p['ref']} ({p['ref_games']} partidos en la base): faltas "
                       f"×{p.get('ref_factor_fouls', 1):.2f}, amarillas ×{p.get('ref_factor_yellow', 1):.2f}")
        else:
            adj.append("Árbitro desconocido: usa ⟳ en «Alineaciones y árbitro» o elígelo de la lista")
        adj.append(f"XI {e['home_name']}: goles ×{lineup['gx_h']:.2f}, faltas ×{fh['fouls']:.2f}, "
                   f"amarillas ×{fh['yellow']:.2f}")
        adj.append(f"XI {e['away_name']}: goles ×{lineup['gx_a']:.2f}, faltas ×{fa['fouls']:.2f}, "
                   f"amarillas ×{fa['yellow']:.2f}")
        if not self.model.params.get("calibrated"):
            adj.append("⚠ Modelo aún sin calibrar (se calibra al terminar el scraping)")
        self.adj_lbl.configure(text="Ajustes:  " + "   ·   ".join(adj))
        self._fill_markets()
        if self.calc_over.get() or self.calc_under.get():
            self.calc()


# ---------------------------------------------------------------- tabla de jugadores

PLAYER_COLS = [("name", "Jugador"), ("pos", "Pos"), ("aporte", "Aporte"), ("rating", "Rating"), ("apps", "PJ"),
               ("starts", "Tit."), ("min", "Min"), ("g", "Goles"), ("a", "Asist."), ("xg90", "xG/90"),
               ("xa90", "xA/90"), ("onoff", "On/Off"), ("gdw", "DG con"), ("gdwo", "DG sin"),
               ("f90", "Faltas/90"), ("fd90", "Recibe/90"), ("y", "Amar."), ("r", "Rojas")]
PLAYER_WIDTHS = {"name": 190, "pos": 45, "apps": 45, "starts": 45, "min": 60, "g": 55, "a": 55, "y": 55, "r": 55,
                 "aporte": 70, "rating": 65, "xg90": 65, "xa90": 65, "onoff": 65, "gdw": 65, "gdwo": 65,
                 "f90": 75, "fd90": 75}


def fill_player_tree(tree, players):
    ranked = sorted((p for p in players.values() if p["in_recent"]), key=lambda p: p["aporte"], reverse=True)
    insert_rows(tree, [((p["name"], p["position"] or "", fmt(p["aporte"]), fmt(p["rating"]), p["apps"], p["starts"],
                         p["minutes"], p["goals"], p["assists"], fmt(p["xg90"]), fmt(p["xa90"]), fmt(p["onoff"]),
                         fmt(p["gd_with"]), fmt(p["gd_without"]), fmt(p["fouls90"]), fmt(p["fouled90"]),
                         p["yellow"], p["red"]), ("good",) if i < 3 else ()) for i, p in enumerate(ranked)])


# ---------------------------------------------------------------- equipos

def _season_start(ts_list):
    """Inicio de la temporada actual: primer partido tras un hueco de más de 40 días."""
    ts = sorted(ts_list)
    start = ts[0] if ts else 0
    for a, b in zip(ts, ts[1:]):
        if b - a > 40 * 86400:
            start = b
    return start


class TeamsView(tb.Frame):
    """Tabla de la temporada actual; doble clic abre el equipo."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        bar = tb.Frame(self)
        bar.pack(fill="x", pady=(0, 10))
        tb.Label(bar, text="Liga").pack(side="left")
        self.league = league_combo(bar)
        self.league.pack(side="left", padx=6)
        self.league.bind("<<ComboboxSelected>>", lambda e: self.load())
        tb.Label(bar, text="Temporada actual, promedios por partido · doble clic en un equipo para ver su detalle",
                 style="Muted.TLabel").pack(side="right")
        cols = [("team", "Equipo"), ("pj", "PJ"), ("pts", "Pts"), ("g", "G"), ("e", "E"), ("p", "P"), ("gf", "GF"),
                ("gc", "GC"), ("xg", "xG"), ("xga", "xGA"), ("faltas", "Faltas"), ("faltas_r", "Faltas rec."),
                ("amar", "Amar."), ("corn", "Córners"), ("forma", "Forma")]
        fr, self.tree = make_table(self, cols, {"team": 200, "forma": 90, "pj": 50, "pts": 55, "g": 45, "e": 45,
                                                "p": 45}, 24)
        fr.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self._open)
        self.load()

    def load(self):
        ut = league_id(self.league.get())
        conn = self.app.conn
        since = int(time.time()) - 400 * 86400
        all_ts = [r[0] for r in conn.execute(
            "SELECT start_ts FROM events WHERE ut_id=? AND status='finished' AND dup=0 AND start_ts>=?", (ut, since))]
        start = _season_start(all_ts)
        rows = []
        for t in active_teams(conn, ut):
            ms = analysis.team_matches(conn, t["id"], "all", ut, start)
            p = analysis.profile(ms)
            pts = 3 * p["G"] + p["E"]
            gd = (p["goals"][0] or 0) - (p["goals"][1] or 0)
            rows.append((t["id"], (pts, gd), (t["name"], p["n"], pts, p["G"], p["E"], p["P"], fmt(p["goals"][0]),
                                              fmt(p["goals"][1]), fmt(p["xg"][0]), fmt(p["xg"][1]),
                                              fmt(p["fouls"][0], 1), fmt(p["fouls"][1], 1), fmt(p["yellow"][0]),
                                              fmt(p["corners"][0], 1), p["form"])))
        rows.sort(key=lambda r: r[1], reverse=True)
        self.tree.delete(*self.tree.get_children())
        self.ids = {}
        for i, (tid, _, vals) in enumerate(rows):
            tags = ("good",) if i < 4 else ("bad",) if i >= len(rows) - 3 else ()
            iid = self.tree.insert("", "end", values=vals, tags=tags + (("odd",) if i % 2 else ()))
            self.ids[iid] = (tid, vals[0])

    def _open(self, _):
        sel = self.tree.selection()
        if sel:
            self.app.open_team(*self.ids[sel[0]])


class TeamView(tb.Frame):
    def __init__(self, parent, app, team_id):
        super().__init__(parent)
        self.app, self.team_id = app, team_id
        bar = tb.Frame(self)
        bar.pack(fill="x", pady=(0, 10))
        tb.Label(bar, text="Condición").pack(side="left", padx=(0, 6))
        self.venue = tb.StringVar(value="all")
        for val, label in (("all", "Todos"), ("home", "Local"), ("away", "Visitante")):
            tb.Radiobutton(bar, text=label, value=val, variable=self.venue, bootstyle="primary-toolbutton",
                           command=self.load).pack(side="left", padx=1)
        tb.Label(bar, text="Últimos").pack(side="left", padx=(16, 4))
        self.n = tb.IntVar(value=10)
        tb.Spinbox(bar, from_=0, to=500, width=5, textvariable=self.n, command=self.load).pack(side="left")
        tb.Label(bar, text="partidos (0 = todos)", style="Muted.TLabel").pack(side="left", padx=4)
        name = app.conn.execute("SELECT name FROM teams WHERE id=?", (team_id,)).fetchone()["name"]
        tb.Button(bar, text="👤 Ver jugadores", bootstyle="info-outline",
                  command=lambda: app.open_players(team_id, name)).pack(side="right")
        self.tiles = tb.Frame(self)
        self.tiles.pack(fill="x")
        body = tb.Frame(self)
        body.pack(fill="both", expand=True, pady=(10, 0))
        left = card(body, "Promedio por partido")
        left.pack(side="left", fill="y")
        fr, self.avg = make_table(left, [("m", "Estadística"), ("f", "A favor"), ("c", "En contra")],
                                  {"m": 180, "f": 80, "c": 80}, 16)
        fr.pack(fill="both", expand=True)
        right = card(body, "Partidos")
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        fr, self.last = make_table(right, [("fecha", "Fecha"), ("comp", "Competición"), ("lv", "L/V"),
                                           ("rival", "Rival"), ("res", "Resultado"), ("faltas", "Faltas"),
                                           ("faltas_r", "Faltas rival"), ("amar", "Amar."), ("corn", "Córners"),
                                           ("tiros", "Tiros"), ("xg", "xG"), ("arb", "Árbitro")],
                                   {"fecha": 85, "comp": 110, "lv": 40, "rival": 160, "res": 75, "arb": 140}, 16)
        fr.pack(fill="both", expand=True)
        self.load()

    def load(self):
        rows = analysis.team_matches(self.app.conn, self.team_id, self.venue.get(), limit=self.n.get() or None)
        p = analysis.profile(rows)
        for w in self.tiles.winfo_children():
            w.destroy()
        tile(self.tiles, "Partidos", str(p["n"]), sub=f"{p['n_stats']} con faltas/tarjetas").pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        tile(self.tiles, "Ganados", str(p["G"]), GOOD).pack(side="left", fill="x", expand=True, padx=(0, 8))
        tile(self.tiles, "Empatados", str(p["E"]), DRAW_C).pack(side="left", fill="x", expand=True, padx=(0, 8))
        tile(self.tiles, "Perdidos", str(p["P"]), BAD).pack(side="left", fill="x", expand=True, padx=(0, 8))
        t = tile(self.tiles, "Forma reciente", "")
        t.value.pack_forget()
        form_chips(t, p["form"]).pack(anchor="w", pady=8, before=t.sub)
        t.pack(side="left", fill="x", expand=True)
        insert_rows(self.avg, [(label, fmt(p[m][0]), fmt(p[m][1])) for m, label in METRICS])
        insert_rows(self.last, [((datetime.fromtimestamp(r["ts"]).strftime("%d/%m/%y"), r["ut_name"],
                                  "L" if r["is_home"] else "V", r["opp_name"],
                                  f"{r['s_goals']}-{r['o_goals']} {r['result']}", fmt(r["s_fouls"], 0),
                                  fmt(r["o_fouls"], 0), fmt(r["s_yellow"], 0), fmt(r["s_corners"], 0),
                                  fmt(r["s_shots"], 0), fmt(r["s_xg"]), r["referee"] or "-"),
                                 ("good",) if r["result"] == "G" else ("bad",) if r["result"] == "P" else ("draw",))
                                for r in rows])


# ---------------------------------------------------------------- jugadores

class PlayersView(tb.Frame):
    def __init__(self, parent, app, team_id=None):
        super().__init__(parent)
        self.app = app
        if team_id is None:
            bar = tb.Frame(self)
            bar.pack(fill="x", pady=(0, 6))
            tb.Label(bar, text="Liga").pack(side="left")
            self.league = league_combo(bar)
            self.league.pack(side="left", padx=6)
            self.league.bind("<<ComboboxSelected>>", lambda e: self._teams())
            tb.Label(bar, text="Equipo").pack(side="left", padx=(12, 0))
            self.team = tb.Combobox(bar, state="readonly", width=26, bootstyle="primary")
            self.team.pack(side="left", padx=6)
            self.team.bind("<<ComboboxSelected>>", lambda e: self.show())
        tb.Label(self, text=("Aporte = 0.5·on/off + 0.8·(xG+xA)/90 + 0.4·(rating − medio). On/Off = diferencia de gol "
                             "del equipo con el jugador titular menos sin él (ajustada por muestra). En verde los 3 "
                             "que más aportan. Clic en una columna para ordenar."),
                 style="Muted.TLabel", wraplength=1100).pack(anchor="w", pady=4)
        fr, self.tree = make_table(self, PLAYER_COLS, PLAYER_WIDTHS, 24)
        fr.pack(fill="both", expand=True)
        self.team_ids = {}
        if team_id is None:
            self._teams()
        else:
            fill_player_tree(self.tree, analysis.player_table(app.conn, team_id))

    def _teams(self):
        teams = active_teams(self.app.conn, league_id(self.league.get()))
        self.team_ids = {t["name"]: t["id"] for t in teams}
        self.team["values"] = list(self.team_ids)
        if teams:
            self.team.current(0)
            self.show()

    def show(self):
        tid = self.team_ids.get(self.team.get())
        if tid:
            fill_player_tree(self.tree, analysis.player_table(self.app.conn, tid))


# ---------------------------------------------------------------- validación

class ValidationView(tb.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        bar = tb.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        tb.Label(bar, text="Liga").pack(side="left")
        self.league = league_combo(bar)
        self.league.pack(side="left", padx=6)
        self.btn = tb.Button(bar, text="▶ Validar con partidos ya jugados", bootstyle="primary", command=self.run)
        self.btn.pack(side="left", padx=10)
        self.status = tb.Label(bar, text="", style="Muted.TLabel")
        self.status.pack(side="left")
        tb.Label(self, text=("Se calibra el modelo con partidos antiguos y se evalúa en los 250 más recientes, que no "
                             "vio: cada partido se predice solo con lo que se sabía antes. «Aciertos conf ≥60 %» = cuando "
                             "el modelo daba ≥ 60 % a Más o Menos en la línea .5 más cercana, cuántas veces acertó. "
                             "Verde = mercado fiable (≥ 58 % con 20+ casos). Rojo = no confíes en él."),
                 style="Muted.TLabel", wraplength=1100).pack(anchor="w", pady=(0, 8))
        self.tiles = tb.Frame(self)
        self.tiles.pack(fill="x")
        cols = [("mkt", "Mercado"), ("n", "Partidos"), ("real", "Media real (total)"), ("mod", "Media modelo"),
                ("mae", "Error medio total"), ("c55", "Aciertos conf ≥55%"), ("c60", "Aciertos conf ≥60%"),
                ("c65", "Aciertos conf ≥65%")]
        fr, self.tree = make_table(self, cols, {"mkt": 180, "c55": 150, "c60": 150, "c65": 150, "mae": 130,
                                                "real": 130, "mod": 120}, 8)
        fr.pack(fill="x", pady=(10, 0))
        self.params_lbl = tb.Label(self, text="", style="Muted.TLabel", justify="left", wraplength=1100)
        self.params_lbl.pack(anchor="w", pady=6)

    def run(self):
        ut = league_id(self.league.get())
        self.btn.configure(state="disabled")
        self.status.configure(text="Calibrando y validando… (unos segundos)")

        def job():
            conn = db.connect(DB_PATH)
            m = model.LeagueModel(conn, ut)
            rep = m.holdout_report(250)
            params = m.params
            conn.close()
            return rep, params
        run_in_thread(job, self._done, self)

    def _done(self, res, err):
        self.btn.configure(state="normal")
        model.clear_cache()
        if err:
            self.status.configure(text=f"Error: {err}")
            return
        rep, params = res
        if not rep["n"]:
            self.status.configure(text="No hay suficientes partidos para validar esta liga.")
            return
        self.status.configure(text=f"Evaluado en {rep['n']} partidos recientes. Modelo recalibrado con todos.")
        for w in self.tiles.winfo_children():
            w.destroy()
        gain = 1 - rep["ll"] / rep["ll_base"]
        for title, val, color, sub in (
                ("Acierto 1X2", pct(rep["acc"]), GOOD if rep["acc"] > 0.5 else WARN, "el favorito del modelo ganó"),
                ("Mejora vs frecuencias", f"{gain:+.1%}", GOOD if gain > 0 else BAD, "log-loss (más es mejor)"),
                ("Brier 1X2", f"{rep['brier']:.3f}", FG, "menor es mejor · 0.667 = azar"),
                ("Más/menos 2.5 goles", pct(rep["o25_hit"]), GOOD if rep["o25_hit"] > 0.53 else WARN,
                 f"Brier {rep['o25_brier']:.3f}")):
            tile(self.tiles, title, val, color, sub).pack(side="left", fill="x", expand=True, padx=(0, 8))
        rows = []
        for metric, label in model.MARKETS:
            st = rep["markets"].get(metric)
            if not st:
                continue

            def cell(t):
                c, h = st["picks"].get(t, (0, None))
                return f"{pct(h)}  (n={c})" if c else "-"
            good = any(c >= 20 and h and h >= 0.58 for c, h in st["picks"].values())
            bad = any(c >= 20 and h is not None and h < 0.52 for c, h in st["picks"].values())
            rows.append(((label, st["n"], fmt(st["avg_y"], 1), fmt(st["avg_mu"], 1), fmt(st["mae_tot"]),
                          cell(0.55), cell(0.6), cell(0.65)), ("good",) if good else ("bad",) if bad else ()))
        insert_rows(self.tree, rows)
        txt = ["Parámetros calibrados — vida media de la forma (días), encogimiento k (partidos), sesgo, dispersión:"]
        for metric, label in [("gx", "Goles")] + model.MARKETS:
            pm = params[metric]
            r = "Poisson" if pm["r_tot"] >= 5000 else f"r={pm['r_tot']:.0f}"
            txt.append(f"   {label}: vida media {pm['hl']:.0f} · k {pm['k']:.0f} · sesgo ×{pm['bias']:.3f} · {r}")
        txt.append(f"   Dixon-Coles ρ = {params['rho']}")
        self.params_lbl.configure(text="\n".join(txt))


# ---------------------------------------------------------------- datos

class DataView(tb.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        c = card(self, "Fuentes")
        c.pack(fill="x")
        tb.Label(c, text=("1) Understat — resultados, xG, calendario y jugadores (≈1 petición por liga y temporada).\n"
                          "2) ESPN — faltas, tarjetas, córners, tiros, alineaciones con minutos y árbitro "
                          "(1 petición por partido, solo los nuevos).\n"
                          "3) SofaScore — opcional y lento; suele bloquear. La alineación probable y las bajas se "
                          "piden desde cada partido.\n"
                          "Al terminar se recalibra el modelo de cada liga."),
                 style="Card.TLabel", justify="left").pack(anchor="w")
        opts = card(self, "Opciones")
        opts.pack(fill="x", pady=10)
        row = tb.Frame(opts, style="Card.TFrame")
        row.pack(fill="x")
        self.league_vars = {}
        for ut, name in LEAGUES.items():
            v = tb.BooleanVar(value=True)
            tb.Checkbutton(row, text=name, variable=v, bootstyle="primary-round-toggle").pack(side="left", padx=8)
            self.league_vars[ut] = v
        row2 = tb.Frame(opts, style="Card.TFrame")
        row2.pack(fill="x", pady=(10, 0))
        self.vars = {}
        for key, label, val, lo, hi in (("us", "Temporadas Understat", 8, 1, 12), ("espn", "Años ESPN", 3, 0, 8),
                                        ("sofa", "Temporadas SofaScore", 0, 0, 5)):
            tb.Label(row2, text=label, style="Card.TLabel").pack(side="left", padx=(0, 4))
            self.vars[key] = tb.IntVar(value=val)
            tb.Spinbox(row2, from_=lo, to=hi, width=4, textvariable=self.vars[key]).pack(side="left", padx=(0, 16))
        self.btn = tb.Button(row2, text="▶ Scrapear", bootstyle="success", command=self.start)
        self.btn.pack(side="left", padx=4)
        tb.Button(row2, text="■ Detener", bootstyle="danger-outline", command=app.stop_flag.set).pack(side="left")
        self.progress = tb.Progressbar(self, bootstyle="info-striped", mode="determinate")
        self.progress.pack(fill="x", pady=(0, 8))
        self.log = tk.Text(self, height=20, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                           font=("Consolas", 9), padx=10, pady=8)
        self.log.pack(fill="both", expand=True)
        for line in app.log_lines[-300:]:
            self.log.insert("end", line + "\n")
        self.log.see("end")
        if app.scraping:
            self.btn.configure(state="disabled")

    def start(self):
        leagues = [ut for ut, v in self.league_vars.items() if v.get()]
        self.btn.configure(state="disabled")
        self.app.start_scrape(leagues, self.vars["us"].get(), self.vars["espn"].get(), self.vars["sofa"].get(), 1.5)

    def on_msg(self, kind, val):
        if kind == "log":
            self.log.insert("end", val + "\n")
            self.log.see("end")
        elif kind == "progress":
            done, total = val
            self.progress.configure(maximum=max(total, 1), value=done)
        elif kind == "finished":
            self.btn.configure(state="normal")


# identidad propia en Windows: la barra de tareas agrupa/ancla la ventana como «Analizador de fútbol»
# (con su ícono y su acceso directo) y no como pythonw.exe. Debe coincidir con el de los accesos directos.
APP_ID = "SteelSteel.AnalizadorFutbol"

if __name__ == "__main__":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass
    App().mainloop()
