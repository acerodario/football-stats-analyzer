"""Promedios de equipos, H2H, aporte de jugadores y ajuste por alineación.

El pronóstico en sí está en model.py.
"""
import time
from collections import defaultdict

from config import METRICS
from names import best_match

DAY = 86400
ESPN_OFFSET = 4_000_000_000
# Peso del ajuste de goles por alineación. Backtest 02/11/2026 (1657 partidos del último año, XI real vs habitual,
# solo datos previos): el ajuste completo (1.0) empeoraba goles y Más/Menos 2.5, sobre todo con rotaciones grandes
# (Le Havre 1-5 PSG: 2.16 → 1.42 goles). Log-loss de goles: 1.0 → 2.9419 · 0.5 → 2.9311 · 0 → 2.9319 · 0.25 → 2.9302
# (el mejor; también mejora el 1X2: 0.9969 → 0.9947, y deja o25 igual).
XI_GOALS_WEIGHT = 0.25


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


# ---------------------------------------------------------------- partidos de un equipo

def team_matches(conn, team_id, venue="all", ut_id=None, since_ts=None, limit=None):
    """Partidos terminados desde el punto de vista de team_id.

    Cada fila tiene s_<métrica> (del equipo) y o_<métrica> (del rival).
    """
    cols = [m for m, _ in METRICS if m != "goals"]
    sel = ", ".join(f"me.{c} AS s_{c}, op.{c} AS o_{c}" for c in cols)
    where = ["e.status='finished'", "e.dup=0", "(e.home_id=:t OR e.away_id=:t)"]
    if venue == "home":
        where.append("e.home_id=:t")
    elif venue == "away":
        where.append("e.away_id=:t")
    if ut_id:
        where.append("e.ut_id=:ut")
    if since_ts:
        where.append("e.start_ts>=:since")
    sql = f"""
        SELECT e.id, e.start_ts AS ts, e.ut_name, e.home_id, e.away_id, e.home_goals, e.away_goals, e.referee,
               th.name AS home_name, ta.name AS away_name, (me.fouls IS NOT NULL) AS has_stats, {sel}
        FROM events e
        JOIN teams th ON th.id=e.home_id JOIN teams ta ON ta.id=e.away_id
        LEFT JOIN team_stats me ON me.event_id=e.id AND me.team_id=:t
        LEFT JOIN team_stats op ON op.event_id=e.id AND op.team_id<>:t
        WHERE {' AND '.join(where)}
        ORDER BY e.start_ts DESC {'LIMIT :lim' if limit else ''}"""
    out = []
    for r in conn.execute(sql, {"t": team_id, "ut": ut_id, "since": since_ts, "lim": limit}):
        r = dict(r)
        home = r["home_id"] == team_id
        r["is_home"] = home
        r["opp_name"] = r["away_name"] if home else r["home_name"]
        r["s_goals"] = r["home_goals"] if home else r["away_goals"]
        r["o_goals"] = r["away_goals"] if home else r["home_goals"]
        if r["s_goals"] is None or r["o_goals"] is None:
            continue
        r["result"] = "G" if r["s_goals"] > r["o_goals"] else "P" if r["s_goals"] < r["o_goals"] else "E"
        out.append(r)
    return out


def profile(rows):
    """Promedio por partido de cada métrica a favor y en contra."""
    res = {"n": len(rows), "n_stats": sum(1 for r in rows if r["has_stats"])}
    for m, _ in METRICS:
        res[m] = (_mean([r[f"s_{m}"] for r in rows]), _mean([r[f"o_{m}"] for r in rows]))
    res["G"] = sum(r["result"] == "G" for r in rows)
    res["E"] = sum(r["result"] == "E" for r in rows)
    res["P"] = sum(r["result"] == "P" for r in rows)
    res["form"] = "".join(r["result"] for r in rows[:5])
    return res


def h2h(conn, a_id, b_id, limit=15):
    rows = conn.execute(
        """SELECT e.*, th.name AS home_name, ta.name AS away_name,
                  h.fouls AS hf, a.fouls AS af, h.yellow AS hy, a.yellow AS ay,
                  h.corners AS hc, a.corners AS ac
           FROM events e JOIN teams th ON th.id=e.home_id JOIN teams ta ON ta.id=e.away_id
           LEFT JOIN team_stats h ON h.event_id=e.id AND h.team_id=e.home_id
           LEFT JOIN team_stats a ON a.event_id=e.id AND a.team_id=e.away_id
           WHERE e.status='finished' AND e.dup=0 AND e.home_goals IS NOT NULL
             AND ((e.home_id=? AND e.away_id=?) OR (e.home_id=? AND e.away_id=?))
           ORDER BY e.start_ts DESC LIMIT ?""",
        (a_id, b_id, b_id, a_id, limit),
    ).fetchall()
    rows = [dict(r) for r in rows]
    summ = {"n": len(rows), "a_wins": 0, "b_wins": 0, "draws": 0}
    for r in rows:
        ga = r["home_goals"] if r["home_id"] == a_id else r["away_goals"]
        gb = r["away_goals"] if r["home_id"] == a_id else r["home_goals"]
        summ["a_wins" if ga > gb else "b_wins" if gb > ga else "draws"] += 1
    summ["goals"] = _mean([r["home_goals"] + r["away_goals"] for r in rows])
    summ["fouls"] = _mean([r["hf"] + r["af"] for r in rows if r["hf"] is not None and r["af"] is not None])
    summ["cards"] = _mean([r["hy"] + r["ay"] for r in rows if r["hy"] is not None and r["ay"] is not None])
    summ["corners"] = _mean([r["hc"] + r["ac"] for r in rows if r["hc"] is not None and r["ac"] is not None])
    return summ, rows


# ---------------------------------------------------------------- jugadores

def _shrunk90(total, minutes, prior90, prior_min=450):
    """Tasa por 90' encogida hacia prior90: con pocos minutos manda el promedio."""
    return (total + prior90 * prior_min / 90) * 90 / (minutes + prior_min)


def player_table(conn, team_id, since_ts=None, recent_matches=15, before_ts=None):
    """Aporte de cada jugador del equipo.

    Fuente por partido: ESPN (preferida) o SofaScore; si no hay, totales de Understat.
    on/off: diferencia de gol del equipo con el jugador titular vs sin él, encogida
    según la muestra. Aporte = 0.5·on/off + 0.8·(xG+xA)/90 + 0.4·(rating − medio).
    """
    before_ts = before_ts or int(time.time()) + DAY  # partido ya jugado: solo lo anterior a él
    since_ts = since_ts or before_ts - 400 * DAY
    for cond in ("ps.player_id >= :off", "ps.player_id < :off"):
        matches = conn.execute(
            f"""SELECT DISTINCT e.id, e.start_ts, e.home_id, e.home_goals, e.away_goals FROM events e
                JOIN player_stats ps ON ps.event_id=e.id AND ps.team_id=:t AND {cond}
                WHERE e.status='finished' AND e.dup=0 AND e.start_ts>=:since AND e.start_ts<:before
                  AND e.home_goals IS NOT NULL
                ORDER BY e.start_ts DESC""",
            {"t": team_id, "since": since_ts, "before": before_ts, "off": ESPN_OFFSET}).fetchall()
        if len(matches) >= 3:
            break
    else:
        return understat_player_table(conn, team_id)

    gd = {}
    for m in matches:
        d = m["home_goals"] - m["away_goals"]
        gd[m["id"]] = d if m["home_id"] == team_id else -d
    recent_ids = {m["id"] for m in matches[:recent_matches]}
    recent_order = [m["id"] for m in matches]

    rows = conn.execute(
        f"""SELECT ps.*, p.name, p.position AS ppos FROM player_stats ps JOIN players p ON p.id=ps.player_id
            WHERE ps.team_id=:t AND {cond} AND ps.event_id IN ({','.join(map(str, gd))})""",
        {"t": team_id, "off": ESPN_OFFSET}).fetchall()

    agg = defaultdict(lambda: defaultdict(float))
    starts = defaultdict(set)
    ratings = defaultdict(list)
    for r in rows:
        a = agg[r["player_id"]]
        a["name"] = r["name"]
        a["position"] = r["ppos"] or r["position"] or a.get("position")
        a["in_recent"] = a.get("in_recent", 0) or (r["event_id"] in recent_ids and (r["minutes"] or 0) > 0)
        if r["starter"]:
            starts[r["player_id"]].add(r["event_id"])
        if (r["minutes"] or 0) > 0:
            a["apps"] += 1
            for k in ("minutes", "goals", "assists", "xg", "xa", "shots", "fouls", "was_fouled",
                      "tackles", "interceptions", "yellow", "red", "saves"):
                a[k] += r[k] or 0
            if r["rating"]:
                ratings[r["player_id"]].append(r["rating"])

    tot_min = sum(a["minutes"] for a in agg.values()) or 1
    team_f90 = sum(a["fouls"] for a in agg.values()) * 90 / tot_min
    team_y90 = sum(a["yellow"] for a in agg.values()) * 90 / tot_min
    team_avg_rating = _mean([x for v in ratings.values() for x in v])

    # xG/xA de Understat por nombre (ESPN no los da)
    us = understat_player_table(conn, team_id)
    us_names = [(pid, p["name"]) for pid, p in us.items()]

    out = {}
    for pid, a in agg.items():
        mins = a["minutes"]
        per90 = lambda k: a[k] * 90 / mins if mins >= 90 else 0.0
        with_ids = starts[pid]
        without_ids = set(gd) - with_ids
        gd_with = _mean([gd[i] for i in with_ids])
        gd_without = _mean([gd[i] for i in without_ids])
        onoff = 0.0
        if gd_with is not None and gd_without is not None:
            nw, nwo = len(with_ids), len(without_ids)
            n_eff = nw * nwo / (nw + nwo)
            onoff = (gd_with - gd_without) * n_eff / (n_eff + 6)
        rating = _mean(ratings[pid])

        u = us.get(best_match(a["name"], us_names, 0.8)) if us_names else None
        if a["xg"] or a["xa"]:
            xg90, xa90 = per90("xg"), per90("xa")
            off90 = _shrunk90(a["xg"] + a["xa"], mins, 0.12)
        elif u:
            xg90, xa90, off90 = u["xg90"], u["xa90"], u["off90"]
        else:
            xg90 = xa90 = None
            off90 = 0.8 * _shrunk90(a["goals"] + a["assists"], mins, 0.12)
        rating_term = 0.4 * (rating - team_avg_rating) if rating and team_avg_rating else 0.0
        aporte = 0.5 * onoff + 0.8 * off90 + rating_term
        out[pid] = {
            "id": pid, "name": a["name"], "position": a["position"],
            "apps": int(a["apps"]), "starts": len(with_ids), "minutes": int(mins),
            "rating": rating, "goals": int(a["goals"]), "assists": int(a["assists"]),
            "xg90": xg90, "xa90": xa90, "ga90": per90("goals") + per90("assists"),
            "off90": off90, "fouls90": per90("fouls"), "fouled90": per90("was_fouled"),
            "tackles90": per90("tackles") + per90("interceptions") if a["tackles"] else None,
            "f90_adj": _shrunk90(a["fouls"], mins, team_f90),
            "y90_adj": _shrunk90(a["yellow"], mins, team_y90),
            "yellow": int(a["yellow"]), "red": int(a["red"]),
            "gd_with": gd_with, "gd_without": gd_without, "onoff": onoff, "aporte": aporte,
            "in_recent": bool(a["in_recent"]),
            "recent_starts": sum(1 for i in recent_order[:8] if i in with_ids),
        }
    return out


def understat_player_table(conn, team_id):
    """Totales de temporada de Understat (sin alineaciones por partido: sin on/off ni rating).
    Ids negativos para no chocar con los de otras fuentes."""
    seasons = [r["season"] for r in conn.execute(
        "SELECT DISTINCT season FROM us_players WHERE team_id=? ORDER BY season DESC LIMIT 2", (team_id,))]
    if not seasons:
        return {}
    rows = conn.execute(
        f"SELECT * FROM us_players WHERE team_id=? AND season IN ({','.join('?' * len(seasons))})",
        (team_id, *seasons)).fetchall()
    agg = defaultdict(lambda: defaultdict(float))
    for r in rows:
        a = agg[-r["us_player_id"]]
        a["name"] = r["name"]
        a["position"] = r["position"] or a.get("position")
        if r["season"] == seasons[0]:
            a["in_recent"] = 1
            a["cur_minutes"] = r["minutes"] or 0
        for k in ("games", "minutes", "goals", "assists", "xg", "xa", "yellow", "red"):
            a[k] += r[k] or 0
    tot_min = sum(a["minutes"] for a in agg.values()) or 1
    team_y90 = sum(a["yellow"] for a in agg.values()) * 90 / tot_min
    out = {}
    for pid, a in agg.items():
        mins = a["minutes"]
        per90 = lambda k: a[k] * 90 / mins if mins >= 90 else 0.0
        off90 = _shrunk90(a["xg"] + a["xa"], mins, 0.12)
        out[pid] = {
            "id": pid, "name": a["name"], "position": a["position"],
            "apps": int(a["games"]), "starts": int(a["games"]), "minutes": int(mins),
            "rating": None, "goals": int(a["goals"]), "assists": int(a["assists"]),
            "xg90": per90("xg"), "xa90": per90("xa"), "ga90": per90("goals") + per90("assists"),
            "off90": off90, "fouls90": None, "fouled90": None, "tackles90": None,
            "f90_adj": None, "y90_adj": _shrunk90(a["yellow"], mins, team_y90),
            "yellow": int(a["yellow"]), "red": int(a["red"]),
            "gd_with": None, "gd_without": None, "onoff": 0.0, "aporte": 0.8 * off90,
            "in_recent": bool(a.get("in_recent")),
            "recent_starts": a.get("cur_minutes", 0) / 90,
        }
    return out


def typical_xi(players):
    ranked = sorted((p for p in players.values() if p["in_recent"]),
                    key=lambda p: (p["recent_starts"], p["minutes"]), reverse=True)
    keepers = [p for p in ranked if p["position"] == "G"]
    field = [p for p in ranked if p["position"] != "G"]
    xi = keepers[:1] + field[:11 - len(keepers[:1])]
    return [p["id"] for p in xi]


def lineup_factors(players, xi_ids, team_goals_pm, team_fouls_pm, team_yellow_pm):
    """Multiplicadores del XI elegido respecto al XI habitual.

    att: goles propios · opp: goles del rival · fouls / yellow: faltas y amarillas propias.
    """
    neutral = {"att": 1.0, "opp": 1.0, "fouls": 1.0, "yellow": 1.0}
    typ = typical_xi(players)
    if not xi_ids or not typ or set(xi_ids) == set(typ):
        return neutral
    scale = 11 / len(xi_ids)

    def total(ids, key, default):
        return sum((players[i][key] if i in players and players[i][key] is not None else default) for i in ids)

    def rat(i):
        p = players.get(i)
        return p["rating"] if p and p["rating"] and p["minutes"] >= 180 else None

    d_off = total(xi_ids, "off90", 0.12) * scale - total(typ, "off90", 0.12)
    r_xi, r_typ = _mean([rat(i) for i in xi_ids]), _mean([rat(i) for i in typ])
    d_rat = (r_xi - r_typ) if r_xi and r_typ else 0.0
    att = 1 + 0.6 * d_off / max(team_goals_pm or 1.3, 0.6) + 0.15 * d_rat
    opp = 1 - 0.35 * d_rat
    att, opp = min(1.35, max(0.65, att)), min(1.25, max(0.8, opp))
    out = {"att": 1 + XI_GOALS_WEIGHT * (att - 1), "opp": 1 + XI_GOALS_WEIGHT * (opp - 1), "fouls": 1.0, "yellow": 1.0}
    # los titulares hacen ~80 % de los minutos del equipo
    for key, metric, team_pm in (("f90_adj", "fouls", team_fouls_pm), ("y90_adj", "yellow", team_yellow_pm)):
        if not team_pm:
            continue
        typ_avg = total(typ, key, 0) / 11
        d = total(xi_ids, key, typ_avg) * scale - total(typ, key, typ_avg)
        out[metric] = min(1.2, max(0.83, 1 + 0.8 * d / team_pm))
    return out
