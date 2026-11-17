"""Prueba de humo de la interfaz: abre cada vista, entra a un partido, usa la calculadora,
navega con «Volver», corre la validación y guarda capturas en data/screens/.

Uso (desde la carpeta del proyecto):  python tools/ui_smoke.py
Revisar después las capturas: deben verse sin textos cortados ni columnas fuera de pantalla.
Los avisos «Exception ignored ... main thread is not in main loop» son normales aquí
(sin mainloop, Python libera variables de Tk desde los hilos); en la app real no salen.
"""
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PIL import ImageGrab  # noqa: E402

import app as A  # noqa: E402

OUT = ROOT / "data" / "screens"
OUT.mkdir(parents=True, exist_ok=True)


def shot(w, name):
    w.update()
    time.sleep(0.4)
    w.update()
    x, y = w.winfo_rootx(), w.winfo_rooty()
    ImageGrab.grab(bbox=(x, y, x + w.winfo_width(), y + w.winfo_height())).save(OUT / f"{name}.png")
    print("captura", OUT / f"{name}.png")


def main():
    a = A.App()
    a.geometry("1450x900+20+20")
    a.attributes("-topmost", True)
    a.update()
    fx = a.stack[-1][1]
    print("próximos partidos:", len(fx.tree.get_children()))
    shot(a, "1_partidos")
    for mode in ("vivo", "fin"):
        fx.mode.set(mode)
        fx.load()
        print(f"modo {mode}: {len(fx.tree.get_children())} partidos")
        shot(a, f"1_partidos_{mode}")
    fx.mode.set("prox")
    fx.load()
    iid = next(iter(fx.rows))
    a.open_match(fx.rows[iid][0])
    mv = a.stack[-1][1]
    shot(a, "2_partido_resumen")
    mv.show_dist("fouls", "total")
    shot(a, "3_detalle_faltas")
    mv.t_mkt.master.select(mv.t_mkt)
    opts = mv._market_options()
    mv.calc_market.current(next(i for i, o in enumerate(opts) if o[0] == "fouls" and o[1] == "home"))
    mv.calc_line.delete(0, "end")
    mv.calc_line.insert(0, "11.5")
    mv.calc_over.insert(0, "1.88")
    mv.calc_under.insert(0, "1.90")
    mv.calc()
    print(mv.calc_out.cget("text"))
    shot(a, "4_mercados")
    mv.t_line.master.select(mv.t_line)
    shot(a, "5_alineaciones")
    a.open_team(mv.event["home_id"], mv.event["home_name"])
    shot(a, "6_equipo")
    a.back()
    a.back()
    print("tras 2× Volver:", a.title_lbl["text"])
    for key in ("resultados", "equipos", "jugadores", "datos"):
        a.go_root(key)
        shot(a, f"7_{key}")
    a.go_root("validacion")
    v = a.stack[-1][1]
    v.run()
    for _ in range(600):
        a.update()
        time.sleep(0.1)
        if str(v.btn.cget("state")) == "normal":
            break
    print(v.status.cget("text"))
    shot(a, "8_validacion")
    a.destroy()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
