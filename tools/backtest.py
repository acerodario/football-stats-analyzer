"""Uso: python tools/backtest.py  (desde la carpeta del proyecto; ~1 min)
Prueba retrospectiva: cada partido del último año pronosticado solo con datos anteriores a él.
Mide (1) sesgo real - previsto por local/visitante, favorito y dominio, y (2) si los % son honestos."""
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import db  # noqa: E402
import model  # noqa: E402

DAYS = 365
METRICS = ["shots", "shots_on", "corners", "fouls", "yellow", "offsides", "saves"]
conn = db.connect(config.DB_PATH)
since = int(time.time()) - DAYS * 86400
rows = []
for ut in config.LEAGUES:
    m = model.get_model(conn, ut)
    evs = conn.execute(
        """SELECT e.*, h.shots h_shots, a.shots a_shots, h.shots_on h_shots_on, a.shots_on a_shots_on,
                  h.corners h_corners, a.corners a_corners, h.fouls h_fouls, a.fouls a_fouls,
                  h.yellow h_yellow, a.yellow a_yellow, h.offsides h_offsides, a.offsides a_offsides,
                  h.saves h_saves, a.saves a_saves, h.possession h_poss
           FROM events e JOIN team_stats h ON h.event_id=e.id AND h.team_id=e.home_id
           JOIN team_stats a ON a.event_id=e.id AND a.team_id=e.away_id
           WHERE e.ut_id=? AND e.status='finished' AND e.dup=0 AND e.start_ts>=? AND e.home_goals IS NOT NULL
             AND h.fouls IS NOT NULL AND h.fouls + a.fouls >= 6""", (ut, since)).fetchall()
    for e in evs:
        p = m.predict(e["home_id"], e["away_id"], as_of=e["start_ts"], ref=e["referee"])
        r = {"ut": ut, "hg": e["home_goals"], "ag": e["away_goals"], "p": (p["p_home"], p["p_draw"], p["p_away"]),
             "o25": p["over25"], "btts": p["btts"], "lam": (p["lam_h"], p["lam_a"]),
             "poss_pred": p["means"]["possession"][0] if p["means"].get("possession") else None,
             "poss_real": e["h_poss"], "m": {}}
        for k in METRICS:
            mh, ma = p["means"][k]
            if mh is None or e[f"h_{k}"] is None:
                continue
            r["m"][k] = (mh, ma, e[f"h_{k}"], e[f"a_{k}"], m.params[k]["r_side"], m.params[k]["r_tot"])
        rows.append(r)
print(f"Partidos analizados: {len(rows)} (último año, 6 competiciones)\n")

# ---------------------------------------------------------------- 1) sesgo
def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


print("=" * 78)
print("1) SESGO: real − previsto (promedio por partido). + = el modelo se queda corto")
print("=" * 78)
print(f"{'estadística':12} {'local':>16} {'visitante':>16} {'favorito':>16} {'no favorito':>16}")
for k in METRICS:
    loc, vis, fav, und = [], [], [], []
    for r in rows:
        if k not in r["m"]:
            continue
        mh, ma, yh, ya, *_ = r["m"][k]
        loc.append((yh, mh))
        vis.append((ya, ma))
        if r["p"][0] > r["p"][2]:
            fav.append((yh, mh)); und.append((ya, ma))
        else:
            fav.append((ya, ma)); und.append((yh, mh))

    def cell(pairs):
        d = mean([y - mu for y, mu in pairs])
        rel = d / mean([mu for _, mu in pairs]) * 100
        return f"{d:+5.2f} ({rel:+4.1f}%)"
    print(f"{k:12} {cell(loc):>16} {cell(vis):>16} {cell(fav):>16} {cell(und):>16}")

# tiros según cuánto domina el local en la predicción (cuota de tiros previstos)
print("\nTiros según el dominio previsto del local (tiros previstos local / total):")
print(f"{'dominio previsto':18} {'n':>5} {'local real−prev':>16} {'visit. real−prev':>17} {'posesión real loc.':>19}")
buckets = [(0, .40), (.40, .47), (.47, .53), (.53, .60), (.60, 1.01)]
for lo, hi in buckets:
    sel = [r for r in rows if "shots" in r["m"] and lo <= r["m"]["shots"][0] / (r["m"]["shots"][0] + r["m"]["shots"][1]) < hi]
    if not sel:
        continue
    dl = mean([r["m"]["shots"][2] - r["m"]["shots"][0] for r in sel])
    dv = mean([r["m"]["shots"][3] - r["m"]["shots"][1] for r in sel])
    pr = mean([r["poss_real"] for r in sel if r["poss_real"] is not None])
    print(f"{f'{lo:.0%}–{min(hi, 1):.0%}':18} {len(sel):5d} {dl:+16.2f} {dv:+17.2f} {pr:19.1f}")

print("\nPor competición (tiros, real − previsto):")
for ut, name in config.LEAGUES.items():
    sel = [r for r in rows if r["ut"] == ut and "shots" in r["m"]]
    print(f"  {name:18} n={len(sel):4d}  local {mean([r['m']['shots'][2] - r['m']['shots'][0] for r in sel]):+5.2f}"
          f"  visitante {mean([r['m']['shots'][3] - r['m']['shots'][1] for r in sel]):+5.2f}")

# ---------------------------------------------------------------- 2) calibración
print("\n" + "=" * 78)
print("2) ¿SON HONESTOS LOS %? Cuando el modelo dice X %, ¿cuánto acierta de verdad?")
print("=" * 78)
BINS = [(.50, .55), (.55, .60), (.60, .65), (.65, .70), (.70, .80), (.80, 1.01)]


def calib(pairs, title):
    """pairs: (prob del lado elegido, acertó)"""
    print(f"\n{title}  (n={len(pairs)})")
    print(f"   {'dice':>9} {'n':>6} {'acierta':>8} {'diferencia':>11}")
    for lo, hi in BINS:
        sel = [h for pr, h in pairs if lo <= pr < hi]
        if len(sel) < 15:
            continue
        exp = mean([pr for pr, h in pairs if lo <= pr < hi])
        got = mean(sel)
        print(f"   {f'{lo:.0%}–{min(hi, 1):.0%}':>9} {len(sel):6d} {got:8.1%} {got - exp:+10.1%}")


def pick(mu, r, line, y):
    po = model.prob_over(mu, r, line)
    return (max(po, 1 - po), (y > line) == (po >= 0.5))


# 1X2: favorito
calib([(max(r["p"]), r["p"].index(max(r["p"])) == (0 if r["hg"] > r["ag"] else 1 if r["hg"] == r["ag"] else 2))
       for r in rows], "Ganador (favorito del modelo)")
calib([(max(r["o25"], 1 - r["o25"]), (r["o25"] >= .5) == (r["hg"] + r["ag"] > 2.5)) for r in rows],
      "Goles más/menos 2.5")
calib([(max(r["btts"], 1 - r["btts"]), (r["btts"] >= .5) == (r["hg"] > 0 and r["ag"] > 0)) for r in rows],
      "Ambos marcan")
side_goals = []
for r in rows:
    for lam, y in zip(r["lam"], (r["hg"], r["ag"])):
        side_goals.append(pick(lam, None, math.floor(lam) + .5, y))
calib(side_goals, "Goles por equipo (línea .5 más cercana)")

summary = {}
for k in METRICS:
    side, tot = [], []
    for r in rows:
        if k not in r["m"]:
            continue
        mh, ma, yh, ya, rs, rt = r["m"][k]
        side.append(pick(mh, rs, math.floor(mh) + .5, yh))
        side.append(pick(ma, rs, math.floor(ma) + .5, ya))
        tot.append(pick(mh + ma, rt, math.floor(mh + ma) + .5, yh + ya))
    calib(side, f"{k} — por equipo")
    calib(tot, f"{k} — total del partido")
    summary[k] = (side, tot)

print("\n" + "=" * 78)
print("RESUMEN: acierto global de la línea preferida y acierto cuando dice ≥ 60 %")
print("=" * 78)
print(f"{'estadística':12} {'equipo: todo':>14} {'equipo ≥60%':>14} {'total: todo':>14} {'total ≥60%':>14}")
for k, (side, tot) in summary.items():
    def f(pairs, th=0):
        sel = [h for pr, h in pairs if pr >= th]
        return f"{mean(sel):.1%} ({len(sel)})" if sel else "-"
    print(f"{k:12} {f(side):>14} {f(side, .6):>14} {f(tot):>14} {f(tot, .6):>14}")
