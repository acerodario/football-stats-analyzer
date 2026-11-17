"""ESPN: estadísticas de partido (faltas, tarjetas, córners…), alineaciones con
minutos por jugador y árbitro. API JSON pública de su web, sin navegador.

- /scoreboard?dates=AAAA&limit=1000 -> todos los partidos de un año (1 petición)
- /summary?event=ID -> estadísticas + alineaciones de un partido (1 petición)
"""
import gzip
import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import db
from config import DB_PATH, ESPN_LEAGUES, ESPN_ONLY, LEAGUES
from names import assign, similarity

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0 Safari/537.36",
    "Accept-Encoding": "gzip",
}
PLAYER_OFFSET = 4_000_000_000  # ids de jugadores ESPN en player_stats/players
# partidos y equipos creados por ESPN (Champions): ids negativos que no chocan con los de Understat
EVENT_OFFSET = 9_000_000_000
TEAM_OFFSET = 7_000_000_000

# estadística ESPN -> columna de team_stats. ESPN manda sobre otras fuentes: SofaScore cuenta
# tiros y faltas con otro criterio y mezclarlas descalibra el modelo.
TEAM_STATS = {
    "fouls": "foulsCommitted", "yellow": "yellowCards", "red": "redCards", "corners": "wonCorners",
    "possession": "possessionPct", "shots": "totalShots", "shots_on": "shotsOnTarget", "saves": "saves",
    "offsides": "offsides", "passes": "totalPasses", "tackles": "totalTackles",
}


class _RateLimiter:
    def __init__(self, rate):
        self.interval = 1.0 / rate
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if self.next > now:
                time.sleep(self.next - now)
            self.next = max(now, self.next) + self.interval


def _get(url, limiter=None, retries=4):
    for attempt in range(retries):
        if limiter:
            limiter.wait()
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
            return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            time.sleep(3 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"ESPN no respondió: {url}")


def _ts(iso):
    return int(datetime.strptime(iso.replace("Z", ""), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp())


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _minute(play):
    m = re.match(r"(\d+)", (play.get("clock") or {}).get("displayValue", ""))
    return min(int(m.group(1)), 90) if m else None


def _minutes_played(entry):
    starter = entry.get("starter")
    if not starter and not entry.get("subbedIn"):
        return 0
    subs = [p for p in entry.get("plays") or [] if p.get("substitution")]
    start, end = 0, 90
    if not starter:
        start = _minute(subs.pop(0)) if subs else 60
    if entry.get("subbedOut") and subs:
        end = _minute(subs[-1]) or end
    for p in entry.get("plays") or []:
        if p.get("redCard"):
            end = min(end, _minute(p) or end)
    return max(0, end - (start or 0))


def _position(entry):
    abbr = ((entry.get("position") or {}).get("abbreviation") or "").upper()
    if abbr in ("SUB", ""):
        return None
    if abbr in ("G", "GK"):
        return "G"
    if abbr.endswith("B") or abbr in ("D", "CD", "CD-L", "CD-R", "SW"):
        return "D"
    if abbr in ("F", "CF", "ST", "LF", "RF", "CF-L", "CF-R", "LW", "RW"):
        return "F"
    return "M" if abbr else None


# ---------------------------------------------------------------- equipos

def _map_teams(conn, ut_id, espn_teams, log):
    """espn_teams: {espn_id: nombre}. Crea los alias que falten (por nombre, dentro de la liga)."""
    have = {r["ext_id"] for r in conn.execute("SELECT ext_id FROM team_alias WHERE source='espn'")}
    pending = [(i, n) for i, n in espn_teams.items() if i not in have]
    if not pending:
        return
    cands = conn.execute(
        """SELECT id, name FROM teams WHERE league_id=?
           AND id NOT IN (SELECT team_id FROM team_alias WHERE source='espn')""", (ut_id,)).fetchall()
    matched = assign(pending, [(r["id"], r["name"]) for r in cands], threshold=0.5)
    for ext, tid in matched.items():
        conn.execute("INSERT OR REPLACE INTO team_alias(source, ext_id, team_id) VALUES ('espn',?,?)", (ext, tid))
    missing = [n for i, n in pending if i not in matched]
    if missing:
        log(f"  ESPN {LEAGUES[ut_id]}: sin emparejar {', '.join(missing)}")
    conn.commit()


def _alias(conn, espn_team_id):
    r = conn.execute("SELECT team_id FROM team_alias WHERE source='espn' AND ext_id=?", (str(espn_team_id),)).fetchone()
    return r["team_id"] if r else None


def _status(e):
    """Estado ESPN -> (status, minuto en vivo)."""
    st = e["status"]["type"]
    if st.get("state") == "in":
        return "inprogress", st.get("shortDetail") or e["status"].get("displayClock")
    if st.get("state") == "post":
        return ("finished", "Final") if st.get("completed") else ("postponed", st.get("shortDetail"))
    return "notstarted", None


def _score(c):
    try:
        return int(c.get("score"))
    except (TypeError, ValueError):
        return None


def _cup_team(conn, ut_id, team):
    """Equipo de una competición sin Understat: los de las 5 ligas ya tienen alias ESPN (mismo id en
    todas las competiciones de ESPN); los demás (Benfica, PSV…) se crean."""
    tid = _alias(conn, team["id"])
    if tid:
        return tid
    tid = -(TEAM_OFFSET + int(team["id"]))
    conn.execute("INSERT OR IGNORE INTO teams(id, name, short_name, league_id) VALUES (?,?,?,?)",
                 (tid, team["displayName"], team.get("shortDisplayName") or team["displayName"], ut_id))
    conn.execute("INSERT OR REPLACE INTO team_alias(source, ext_id, team_id) VALUES ('espn',?,?)",
                 (str(team["id"]), tid))
    return tid


def _save_cup_event(conn, ut_id, e, cs):
    home, away = _cup_team(conn, ut_id, cs["home"]["team"]), _cup_team(conn, ut_id, cs["away"]["team"])
    status, clock = _status(e)
    ts = _ts(e["date"])
    year = datetime.fromtimestamp(ts, timezone.utc).year
    season = year if datetime.fromtimestamp(ts, timezone.utc).month >= 7 else year - 1
    hg, ag = (_score(cs["home"]), _score(cs["away"])) if status in ("inprogress", "finished") else (None, None)
    eid = -(EVENT_OFFSET + int(e["id"]))
    db.upsert_external_event(conn, eid, "espn", ut_id, LEAGUES[ut_id], -season, f"{season}/{(season + 1) % 100:02d}",
                             ts, status, home, away, hg, ag)
    conn.execute("UPDATE events SET live_clock=? WHERE id=?", (clock, eid))


def _sync_dates(conn, ut_id, upcoming, log):
    """Understat tarda en reflejar partidos adelantados/aplazados (Nice–Rennes pasó del sáb. 31/10 al vie. 30/10 y la
    app no lo mostraba). ESPN tiene la fecha buena: se corrige la de los partidos aún no jugados. En una liga cada
    cruce local–visitante se juega una vez por temporada, así que se empareja por equipos en ±60 días."""
    n, soon = 0, time.time() + 7 * 86400
    for ts, home_t, away_t in upcoming:
        home, away = _alias(conn, home_t["id"]), _alias(conn, away_t["id"])
        if not home or not away:
            continue
        r = conn.execute(
            """SELECT id, start_ts FROM events WHERE ut_id=? AND dup=0 AND status='notstarted' AND home_id=? AND away_id=?
               AND ABS(start_ts-?) < 60*86400 ORDER BY ABS(start_ts-?) LIMIT 1""",
            (ut_id, home, away, ts, ts)).fetchone()
        if not r:
            continue
        conn.execute("UPDATE events SET start_ts=?, ts_espn=1 WHERE id=?", (ts, r["id"]))
        if r["start_ts"] != ts:
            n += 1
            if ts < soon and abs(r["start_ts"] - ts) > 3 * 3600:
                log(f"  Fecha corregida: {home_t['displayName']} vs {away_t['displayName']} → "
                    f"{datetime.fromtimestamp(ts):%d/%m %H:%M} (antes {datetime.fromtimestamp(r['start_ts']):%d/%m %H:%M})")
    conn.commit()
    if n:
        log(f"  {LEAGUES[ut_id]}: {n} fechas/horas del calendario corregidas con ESPN")
    return n


def _canonical_event(conn, home, away, ts):
    r = conn.execute(
        """SELECT id FROM events WHERE dup=0 AND home_id=? AND away_id=? AND ABS(start_ts-?) < 172800
           ORDER BY ABS(start_ts-?), source='sofa' DESC LIMIT 1""", (home, away, ts, ts)).fetchone()
    return r["id"] if r else None


# ---------------------------------------------------------------- guardar un partido

def _save_summary(conn, eid, summary):
    box = (summary.get("boxscore") or {}).get("teams") or []
    comp = (summary.get("header") or {}).get("competitions", [{}])[0]
    side_of = {c["team"]["id"]: c.get("homeAway") for c in comp.get("competitors", [])}
    ev = conn.execute("SELECT home_id, away_id FROM events WHERE id=?", (eid,)).fetchone()
    team_id = {"home": ev["home_id"], "away": ev["away_id"]}

    for t in box:
        side = side_of.get(t["team"]["id"])
        if side not in team_id:
            continue
        stats = {s["name"]: _num(s.get("displayValue")) for s in t.get("statistics", [])}
        vals = {col: stats.get(key) for col, key in TEAM_STATS.items()}
        if vals["fouls"] is None:
            continue
        cols = list(vals)
        conn.execute(
            f"""INSERT INTO team_stats(event_id, team_id, is_home, {', '.join(cols)})
                VALUES (?,?,?,{','.join('?' * len(cols))})
                ON CONFLICT(event_id, team_id) DO UPDATE SET
                {', '.join(f'{c}=excluded.{c}' for c in cols)}""",
            (eid, team_id[side], 1 if side == "home" else 0, *vals.values()))

    officials = (summary.get("gameInfo") or {}).get("officials") or []
    if officials:
        conn.execute("UPDATE events SET referee=? WHERE id=?", (officials[0].get("displayName"), eid))

    for roster in summary.get("rosters") or []:
        side = roster.get("homeAway") or side_of.get(roster["team"]["id"])
        if side not in team_id:
            continue
        for entry in roster.get("roster") or []:
            a = entry.get("athlete") or {}
            if not a.get("id"):
                continue
            pid = PLAYER_OFFSET + int(a["id"])
            s = {x["name"]: _num(x.get("displayValue")) or 0 for x in entry.get("stats") or []}
            conn.execute("""INSERT INTO players(id, name, short_name, position) VALUES (?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                            position=COALESCE(excluded.position, players.position)""",
                         (pid, a.get("displayName"), a.get("shortName"), _position(entry)))
            conn.execute(
                """INSERT OR REPLACE INTO player_stats(event_id, team_id, player_id, is_home, starter, position,
                       minutes, rating, goals, assists, xg, xa, shots, shots_on, key_passes, fouls, was_fouled,
                       tackles, interceptions, duels_won, duels_lost, saves, yellow, red)
                   VALUES (?,?,?,?,?,?,?,NULL,?,?,NULL,NULL,?,?,NULL,?,?,NULL,NULL,NULL,NULL,?,?,?)""",
                (eid, team_id[side], pid, 1 if side == "home" else 0, 1 if entry.get("starter") else 0,
                 _position(entry), _minutes_played(entry), s.get("totalGoals", 0), s.get("goalAssists", 0),
                 s.get("totalShots", 0), s.get("shotsOnTarget", 0), s.get("foulsCommitted", 0),
                 s.get("foulsSuffered", 0), s.get("saves", 0), s.get("yellowCards", 0), s.get("redCards", 0)))


# ---------------------------------------------------------------- descarga

def import_espn(league_ids, n_years, log, progress, stop, rate=2.5):
    conn = db.connect(DB_PATH)
    limiter = _RateLimiter(rate)
    year = datetime.now().year
    for ut_id in league_ids:
        if stop.is_set():
            break
        code = ESPN_LEAGUES[ut_id]
        teams, n_new, upcoming = {}, 0, []
        for y in range(year - n_years + 1, year + 1):
            data = _get(f"{BASE.format(code=code)}/scoreboard?dates={y}&limit=1000", limiter) or {}
            for e in data.get("events", []):
                comp = e["competitions"][0]
                cs = {c["homeAway"]: c for c in comp["competitors"]}
                if "home" not in cs or "away" not in cs:
                    continue
                for c in cs.values():
                    teams[c["team"]["id"]] = c["team"]["displayName"]
                if ut_id in ESPN_ONLY:
                    _save_cup_event(conn, ut_id, e, cs)
                elif e["status"]["type"].get("state") == "pre":
                    upcoming.append((_ts(e["date"]), cs["home"]["team"], cs["away"]["team"]))
                completed = e["status"]["type"].get("completed")
                cur = conn.execute("SELECT done FROM espn_events WHERE espn_id=?", (e["id"],)).fetchone()
                if cur and cur["done"] == 1:
                    continue
                conn.execute(
                    """INSERT OR REPLACE INTO espn_events(espn_id, ut_id, ts, home_ext, away_ext, completed, done)
                       VALUES (?,?,?,?,?,?,0)""",
                    (e["id"], ut_id, _ts(e["date"]), cs["home"]["team"]["id"], cs["away"]["team"]["id"],
                     1 if completed else 0))
                n_new += completed is True
        conn.commit()
        if ut_id not in ESPN_ONLY:
            _map_teams(conn, ut_id, teams, log)
            _sync_dates(conn, ut_id, upcoming, log)
            db.mark_dups(conn)
        log(f"ESPN {LEAGUES[ut_id]}: calendario {n_years} años, {n_new} partidos por descargar")

    pending = conn.execute(
        "SELECT * FROM espn_events WHERE completed=1 AND done=0 ORDER BY ts DESC").fetchall()
    total = len(pending)
    log(f"ESPN: descargando estadísticas y alineaciones de {total} partidos (~{total / rate / 60:.0f} min)…")
    done = unmatched = 0

    def fetch(row):
        return row, _get(f"{BASE.format(code=ESPN_LEAGUES[row['ut_id']])}/summary?event={row['espn_id']}", limiter)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = []
        for row in pending:
            futures.append(pool.submit(fetch, row))
        for fut in as_completed(futures):
            if stop.is_set():
                for f in futures:
                    f.cancel()
                break
            try:
                row, summary = fut.result()
            except Exception as e:  # noqa: BLE001
                log(f"  ESPN error: {e}")
                continue
            home, away = _alias(conn, row["home_ext"]), _alias(conn, row["away_ext"])
            eid = _canonical_event(conn, home, away, row["ts"]) if home and away else None
            if summary and eid:
                _save_summary(conn, eid, summary)
                conn.execute("UPDATE espn_events SET done=1, event_id=? WHERE espn_id=?", (eid, row["espn_id"]))
            else:
                unmatched += 1
                conn.execute("UPDATE espn_events SET done=2 WHERE espn_id=?", (row["espn_id"],))
            done += 1
            if done % 25 == 0:
                conn.commit()
                progress(done, total)
            if done % 250 == 0:
                log(f"  ESPN {done}/{total}")
    conn.commit()
    progress(total, total)
    if unmatched:
        log(f"  ESPN: {unmatched} partidos sin pareja en la base (otros torneos o equipos sin emparejar)")
    conn.close()


def fetch_fixture(conn, event):
    """Árbitro y alineación (si ya está publicada) de un partido próximo. 1-2 peticiones."""
    code = ESPN_LEAGUES[event["ut_id"]]
    day = datetime.fromtimestamp(event["start_ts"], timezone.utc).strftime("%Y%m%d")
    data = _get(f"{BASE.format(code=code)}/scoreboard?dates={day}") or {}
    target = None
    for e in data.get("events", []):
        cs = {c["homeAway"]: c["team"] for c in e["competitions"][0]["competitors"]}
        h, a = _alias(conn, cs["home"]["id"]), _alias(conn, cs["away"]["id"])
        if (h == event["home_id"] and a == event["away_id"]) or (
                similarity(cs["home"]["displayName"], event["home_name"]) > 0.75
                and similarity(cs["away"]["displayName"], event["away_name"]) > 0.75):
            target = e["id"]
            break
    if not target:
        return None
    s = _get(f"{BASE.format(code=code)}/summary?event={target}") or {}
    officials = (s.get("gameInfo") or {}).get("officials") or []
    comp = (s.get("header") or {}).get("competitions", [{}])[0]
    side_of = {c["team"]["id"]: c.get("homeAway") for c in comp.get("competitors", [])}
    lineups = {}
    for roster in s.get("rosters") or []:
        side = roster.get("homeAway") or side_of.get(roster["team"]["id"])
        players = [(PLAYER_OFFSET + int(e["athlete"]["id"]), e["athlete"].get("displayName"), _position(e),
                    bool(e.get("starter"))) for e in roster.get("roster") or [] if (e.get("athlete") or {}).get("id")]
        if players:
            lineups[side] = players
    return {"referee": officials[0].get("displayName") if officials else None, "lineups": lineups}


# ---------------------------------------------------------------- marcadores en vivo

def live_scores(conn, ut_ids, log=print):
    """Estado y marcador de los partidos de hoy (1 petición por liga). Un partido que acaba de terminar
    baja también sus estadísticas, para compararlo enseguida con la predicción. Devuelve cuántos cambiaron."""
    now = datetime.now(timezone.utc)
    days = {now.strftime("%Y%m%d")}
    if now.hour < 6:  # partidos de la noche anterior (hora UTC)
        days.add(datetime.fromtimestamp(now.timestamp() - 86400, timezone.utc).strftime("%Y%m%d"))
    changed = 0
    db.mark_dups(conn)
    for ut_id in ut_ids:
        code = ESPN_LEAGUES[ut_id]
        for day in days:
            data = _get(f"{BASE.format(code=code)}/scoreboard?dates={day}") or {}
            for e in data.get("events", []):
                cs = {c["homeAway"]: c for c in e["competitions"][0]["competitors"]}
                if "home" not in cs or "away" not in cs:
                    continue
                if ut_id in ESPN_ONLY:
                    _save_cup_event(conn, ut_id, e, cs)
                home, away = _alias(conn, cs["home"]["team"]["id"]), _alias(conn, cs["away"]["team"]["id"])
                eid = _canonical_event(conn, home, away, _ts(e["date"])) if home and away else None
                if not eid:
                    continue
                status, clock = _status(e)
                if status == "notstarted":
                    continue
                hg, ag = _score(cs["home"]), _score(cs["away"])
                cur = conn.execute("SELECT status, home_goals, away_goals, live_clock FROM events WHERE id=?",
                                   (eid,)).fetchone()
                # terminado y sin estadísticas: en Champions _save_cup_event ya dejó el estado «finished», así que
                # mirar solo el cambio de estado nunca bajaba las estadísticas (Galatasaray–Stuttgart 03/11)
                need_stats = status == "finished" and not conn.execute(
                    "SELECT 1 FROM team_stats WHERE event_id=?", (eid,)).fetchone()
                if tuple(cur) == (status, hg, ag, clock) and not need_stats:
                    continue
                winner = None if hg is None or ag is None else 1 if hg > ag else 2 if ag > hg else 3
                conn.execute("UPDATE events SET status=?, home_goals=?, away_goals=?, winner_code=?, live_clock=? "
                             "WHERE id=?", (status, hg, ag, winner, clock, eid))
                changed += 1
                if need_stats:
                    summary = _get(f"{BASE.format(code=code)}/summary?event={e['id']}")
                    if summary:
                        _save_summary(conn, eid, summary)
                        conn.execute(
                            """INSERT OR REPLACE INTO espn_events(espn_id, ut_id, ts, home_ext, away_ext, completed,
                                   done, event_id) VALUES (?,?,?,?,?,1,1,?)""",
                            (e["id"], ut_id, _ts(e["date"]), cs["home"]["team"]["id"], cs["away"]["team"]["id"], eid))
                    log(f"Final: {cs['home']['team']['displayName']} {hg}-{ag} {cs['away']['team']['displayName']}")
            conn.commit()
    return changed
