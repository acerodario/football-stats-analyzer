"""Understat: resultados, xG, calendario y estadísticas de jugadores.

Una sola petición por liga y temporada (getLeagueData), así que no hay riesgo
de bloqueo. No trae faltas ni córners: eso sale de SofaScore.
"""
import gzip
import json
import time
import urllib.request
from datetime import datetime, timezone

import db
from config import DB_PATH, LEAGUES, UNDERSTAT_LEAGUES

URL = "https://understat.com/getLeagueData/{league}/{season}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Encoding": "gzip",
}
POSITION = {"GK": "G", "D": "D", "M": "M", "F": "F"}


def fetch_league(code, year):
    req = urllib.request.Request(URL.format(league=code, season=year), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=40) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body)


def current_season_year():
    now = datetime.now()
    return now.year if now.month >= 7 else now.year - 1


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _position(pos):
    for tok in (pos or "").split():
        if tok in POSITION:
            return POSITION[tok]
    return None


def _save(conn, ut_id, year, data):
    teams = {t["id"]: db.team_for_source(conn, "us", t["id"], t["title"], ut_id)
             for t in data.get("teams", {}).values()}
    season_name = f"{year}/{(year + 1) % 100:02d}"
    n = 0
    for m in data.get("dates", []):
        home = teams.get(m["h"]["id"]) or db.team_for_source(conn, "us", m["h"]["id"], m["h"]["title"], ut_id)
        away = teams.get(m["a"]["id"]) or db.team_for_source(conn, "us", m["a"]["id"], m["a"]["title"], ut_id)
        ts = int(datetime.strptime(m["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
        done = bool(m.get("isResult"))
        hg = int(m["goals"]["h"]) if done and m["goals"]["h"] is not None else None
        ag = int(m["goals"]["a"]) if done and m["goals"]["a"] is not None else None
        db.upsert_external_event(conn, -int(m["id"]), "us", ut_id, LEAGUES[ut_id], -year, season_name, ts,
                                 "finished" if done else "notstarted", home, away, hg, ag)
        if done:
            for team, side in ((home, "h"), (away, "a")):
                conn.execute(
                    """INSERT INTO team_stats(event_id, team_id, is_home, xg) VALUES (?,?,?,?)
                       ON CONFLICT(event_id, team_id) DO UPDATE SET xg=excluded.xg""",
                    (-int(m["id"]), team, 1 if side == "h" else 0, _num(m["xG"][side])))
        n += 1

    # estadísticas de jugadores en la temporada (respaldo si SofaScore no tiene alineaciones)
    conn.execute("DELETE FROM us_players WHERE season=? AND ut_id=?", (year, ut_id))
    by_title = {t["title"]: teams[t["id"]] for t in data.get("teams", {}).values()}
    for p in data.get("players", []):
        for title in (p.get("team_title") or "").split(","):
            team = by_title.get(title.strip())
            if team is None:
                continue
            conn.execute(
                """INSERT OR REPLACE INTO us_players(season, ut_id, team_id, us_player_id, name, position, games,
                       minutes, goals, assists, xg, xa, shots, key_passes, yellow, red)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (year, ut_id, team, int(p["id"]), p["player_name"], _position(p.get("position")),
                 _num(p["games"]), _num(p["time"]), _num(p["goals"]), _num(p["assists"]), _num(p["xG"]),
                 _num(p["xA"]), _num(p["shots"]), _num(p["key_passes"]), _num(p["yellow_cards"]),
                 _num(p["red_cards"])))
    conn.commit()
    return n


def import_understat(league_ids, n_seasons, log, stop, delay=2.0):
    conn = db.connect(DB_PATH)
    cur = current_season_year()
    for ut_id in league_ids:
        if ut_id not in UNDERSTAT_LEAGUES:
            continue  # p. ej. Champions: sus partidos vienen de ESPN
        code = UNDERSTAT_LEAGUES[ut_id]
        for year in range(cur, cur - n_seasons, -1):
            if stop.is_set():
                break
            done_key = f"us_done:{code}:{year}"
            if year != cur and db.get_meta(conn, done_key):
                continue
            try:
                data = fetch_league(code, year)
            except Exception as e:  # noqa: BLE001
                log(f"Understat {LEAGUES[ut_id]} {year}: error {e}")
                continue
            finally:
                time.sleep(delay)
            if not data.get("dates"):
                log(f"Understat {LEAGUES[ut_id]} {year}: sin datos")
                continue
            n = _save(conn, ut_id, year, data)
            if year != cur:
                db.set_meta(conn, done_key, "1")
            log(f"Understat {LEAGUES[ut_id]} {year}/{(year + 1) % 100:02d}: {n} partidos")
    db.reconcile(conn)
    conn.close()
