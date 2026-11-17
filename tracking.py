"""Historial de predicciones: se guarda lo que dijo el modelo ANTES de cada partido
para compararlo después con el resultado real (sección «Resultados»)."""
import json
import math
import time

import db
import model
from config import LEAGUES

STATS = ["gx", "fouls", "yellow", "corners", "shots", "shots_on", "offsides", "saves"]
SOURCE_LABEL = {"pre": "antes del partido", "pre_xi": "antes del partido (con XI/árbitro)",
                "rebuilt": "reconstruida"}


def snapshot(conn, m, ev, pred, source="pre"):
    """Guarda la predicción de un partido que aún no empezó (sobrescribe la anterior).
    Las reconstruidas solo se guardan si no hay otra."""
    now = int(time.time())
    if source != "rebuilt" and now >= ev["start_ts"]:
        return
    if source == "pre":
        cur = conn.execute("SELECT source FROM predictions WHERE event_id=?", (ev["id"],)).fetchone()
        if cur and cur["source"] == "pre_xi":
            return  # no pisar una predicción hecha con alineación/árbitro
    means = {k: list(pred["means"].get(k, (None, None))) for k in STATS}
    disp = {k: m.params[k]["r_tot"] for k in STATS if k in m.params}
    disp.update({f"{k}_side": m.params[k]["r_side"] for k in STATS if k in m.params})
    conn.execute(
        f"""INSERT {'OR IGNORE' if source == 'rebuilt' else 'OR REPLACE'} INTO predictions(event_id, ut_id,
               saved_ts, source, p_home, p_draw, p_away, lam_h, lam_a, over25, btts, means, disp, referee)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ev["id"], ev["ut_id"], now, source, pred["p_home"], pred["p_draw"], pred["p_away"], pred["lam_h"],
         pred["lam_a"], pred["over25"], pred["btts"], json.dumps(means), json.dumps(disp), pred.get("ref")))


def save_upcoming(conn, days=14):
    """Predicción de todos los próximos partidos (sin alineación; con árbitro si ya se conoce)."""
    n = 0
    for ev in db.upcoming(conn, list(LEAGUES), days):
        ev = dict(ev)
        live = conn.execute("SELECT referee FROM fixture_live WHERE event_id=?", (ev["id"],)).fetchone()
        m = model.get_model(conn, ev["ut_id"])
        pred = m.predict(ev["home_id"], ev["away_id"], ref=live["referee"] if live else None)
        snapshot(conn, m, ev, pred, "pre")
        n += 1
    conn.commit()
    return n


def backfill(conn, ut_ids=None, days=60):
    """Reconstruye la predicción de partidos ya jugados usando SOLO datos anteriores a cada partido."""
    since = int(time.time()) - days * 86400
    n = 0
    for ut in ut_ids or list(LEAGUES):
        m = model.get_model(conn, ut)
        rows = conn.execute(
            """SELECT e.* FROM events e LEFT JOIN predictions p ON p.event_id=e.id
               WHERE e.ut_id=? AND e.dup=0 AND e.start_ts BETWEEN ? AND ? AND p.event_id IS NULL
                 AND e.status IN ('finished', 'notstarted')""", (ut, since, int(time.time()))).fetchall()
        for ev in rows:
            ev = dict(ev)
            pred = m.predict(ev["home_id"], ev["away_id"], as_of=ev["start_ts"], ref=ev.get("referee"))
            snapshot(conn, m, ev, pred, "rebuilt")
            n += 1
        conn.commit()
    return n


def _side_hit(mu_tot, r, actual):
    """¿Acertó el lado que prefería el modelo en la línea .5 más cercana a su media?"""
    if mu_tot is None or actual is None:
        return None
    line = math.floor(mu_tot) + 0.5
    p_over = model.prob_over(mu_tot, r, line)
    pick_over = p_over >= 0.5
    return line, (actual > line) == pick_over, ("Más" if pick_over else "Menos"), max(p_over, 1 - p_over)


def results(conn, ut_ids, days_back):
    """Partidos ya empezados con su predicción guardada y el resultado real (si ya está)."""
    now = int(time.time())
    q = ",".join("?" * len(ut_ids))
    rows = conn.execute(
        f"""SELECT e.*, th.name AS home_name, ta.name AS away_name, p.*, p.source AS pred_source,
                   h.fouls AS h_fouls, a.fouls AS a_fouls, h.yellow AS h_yellow, a.yellow AS a_yellow,
                   h.corners AS h_corners, a.corners AS a_corners
            FROM predictions p JOIN events e ON e.id=p.event_id
            JOIN teams th ON th.id=e.home_id JOIN teams ta ON ta.id=e.away_id
            LEFT JOIN team_stats h ON h.event_id=e.id AND h.team_id=e.home_id
            LEFT JOIN team_stats a ON a.event_id=e.id AND a.team_id=e.away_id
            WHERE e.ut_id IN ({q}) AND e.start_ts BETWEEN ? AND ?
            ORDER BY e.start_ts DESC""", (*ut_ids, now - days_back * 86400, now)).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        means = json.loads(r["means"])
        disp = json.loads(r["disp"] or "{}")
        probs = (r["p_home"], r["p_draw"], r["p_away"])
        r["pick"] = probs.index(max(probs))
        r["pick_prob"] = max(probs)
        r["finished"] = r["status"] == "finished" and r["home_goals"] is not None
        if r["finished"]:
            hg, ag = r["home_goals"], r["away_goals"]
            r["outcome"] = 0 if hg > ag else 1 if hg == ag else 2
            r["hit_1x2"] = r["outcome"] == r["pick"]
            r["hit_o25"] = (r["over25"] >= 0.5) == (hg + ag > 2.5)
        r["markets"] = {}
        for k in ("fouls", "yellow", "corners"):
            mh, ma = means.get(k, (None, None))
            mu = None if mh is None else mh + ma
            actual = None if r[f"h_{k}"] is None or r[f"a_{k}"] is None else r[f"h_{k}"] + r[f"a_{k}"]
            res = _side_hit(mu, disp.get(k), actual) if mu is not None and actual is not None else None
            r["markets"][k] = (mu, actual, res)
        r["source"] = r["pred_source"]  # events también tiene «source» (fuente de datos)
        r["source_label"] = SOURCE_LABEL.get(r["source"], r["source"])
        out.append(r)
    return out


COMPARE_STATS = [("gx", "Goles"), ("shots", "Tiros totales"), ("shots_on", "Tiros a puerta"),
                 ("corners", "Córners"), ("fouls", "Faltas"), ("yellow", "Amarillas"),
                 ("offsides", "Fueras de juego"), ("saves", "Atajadas")]


def _poisson(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def comparison(conn, event_id):
    """Predicción guardada de un partido vs lo que pasó, estadística por estadística."""
    r = conn.execute(
        """SELECT e.*, th.name AS home_name, ta.name AS away_name, p.*, p.source AS pred_source
           FROM predictions p JOIN events e ON e.id=p.event_id
           JOIN teams th ON th.id=e.home_id JOIN teams ta ON ta.id=e.away_id
           WHERE e.id=?""", (event_id,)).fetchone()
    if not r:
        return None
    r = dict(r)
    stats = {row["team_id"]: dict(row) for row in conn.execute("SELECT * FROM team_stats WHERE event_id=?",
                                                               (event_id,))}
    sh, sa = stats.get(r["home_id"], {}), stats.get(r["away_id"], {})
    means = json.loads(r["means"])
    disp = json.loads(r["disp"] or "{}")
    finished = r["status"] == "finished" and r["home_goals"] is not None
    probs = (r["p_home"], r["p_draw"], r["p_away"])
    out = {"event": r, "finished": finished, "probs": probs, "pick": probs.index(max(probs)),
           "source_label": SOURCE_LABEL.get(r["pred_source"], r["pred_source"]), "items": []}
    if finished:
        hg, ag = r["home_goals"], r["away_goals"]
        out["outcome"] = 0 if hg > ag else 1 if hg == ag else 2
        out["p_outcome"] = probs[out["outcome"]]
        out["p_exact"] = _poisson(hg, r["lam_h"]) * _poisson(ag, r["lam_a"])
        out["hit_o25"] = (r["over25"] >= 0.5) == (hg + ag > 2.5)
        out["hit_btts"] = (r["btts"] >= 0.5) == (hg > 0 and ag > 0)
    side_r = _side_dispersion(conn, r["ut_id"], disp)
    for k, label in COMPARE_STATS:
        eh, ea = means.get(k, (None, None))
        if eh is None:
            continue
        if k == "gx":
            rh, ra = (r["home_goals"], r["away_goals"]) if finished else (None, None)
        else:
            rh, ra = sh.get(k), sa.get(k)
        mu = eh + ea
        line = math.floor(mu) + 0.5
        p_over = model.prob_over(mu, None if k == "gx" else disp.get(k), line)
        pick_over = p_over >= 0.5
        actual = None if rh is None or ra is None else rh + ra
        out["items"].append({
            "key": k, "label": label, "eh": eh, "ea": ea, "rh": rh, "ra": ra, "mu": mu, "actual": actual,
            "line": line, "pick": "Más" if pick_over else "Menos", "prob": max(p_over, 1 - p_over),
            "hit": None if actual is None else (actual > line) == pick_over,
            "home": _line_verdict(eh, None if k == "gx" else side_r.get(k), rh),
            "away": _line_verdict(ea, None if k == "gx" else side_r.get(k), ra)})
    return out


def _line_verdict(mu, r, actual):
    """Lado que prefería el modelo en la línea .5 más cercana a la media de un equipo, y si acertó."""
    line = math.floor(mu) + 0.5
    p_over = model.prob_over(mu, r, line)
    pick_over = p_over >= 0.5
    return {"line": line, "pick": "Más" if pick_over else "Menos", "prob": max(p_over, 1 - p_over),
            "actual": actual, "hit": None if actual is None else (actual > line) == pick_over}


def _side_dispersion(conn, ut_id, disp):
    """Dispersión por equipo guardada con la predicción; las antiguas no la tienen: se usa la del modelo actual."""
    out = {k[:-5]: v for k, v in disp.items() if k.endswith("_side")}
    if not out:
        try:
            out = {k: p["r_side"] for k, p in model.get_model(conn, ut_id).params.items()
                   if isinstance(p, dict) and "r_side" in p}
        except Exception:
            out = {}
    # último recurso: la dispersión del total (nunca Poisson: subestima la variación por equipo)
    return {k: out.get(k, v) for k, v in disp.items() if not k.endswith("_side")} | out
