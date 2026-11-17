import json
import sqlite3
import time

from config import ESPN_ONLY, LEAGUES, TEAM_STAT_KEYS, ZERO_IF_MISSING

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY, name TEXT, short_name TEXT, league_id INTEGER
);

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY, name TEXT, short_name TEXT, position TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    custom_id TEXT,
    ut_id INTEGER, ut_name TEXT,
    season_id INTEGER, season_name TEXT,
    round INTEGER,
    start_ts INTEGER,
    status TEXT,
    home_id INTEGER, away_id INTEGER,
    home_goals INTEGER, away_goals INTEGER,
    home_ht INTEGER, away_ht INTEGER,
    winner_code INTEGER,
    details INTEGER DEFAULT 0          -- 0 pendiente, 1 descargado
);
CREATE INDEX IF NOT EXISTS ix_events_home ON events(home_id, start_ts);
CREATE INDEX IF NOT EXISTS ix_events_away ON events(away_id, start_ts);
CREATE INDEX IF NOT EXISTS ix_events_ut ON events(ut_id, start_ts);

CREATE TABLE IF NOT EXISTS team_stats (
    event_id INTEGER, team_id INTEGER, is_home INTEGER,
    possession REAL, xg REAL, shots REAL, shots_on REAL, big_chances REAL,
    corners REAL, fouls REAL, yellow REAL, red REAL, saves REAL, passes REAL,
    tackles REAL, free_kicks REAL, offsides REAL,
    raw TEXT,
    PRIMARY KEY (event_id, team_id)
);

CREATE TABLE IF NOT EXISTS player_stats (
    event_id INTEGER, team_id INTEGER, player_id INTEGER, is_home INTEGER,
    starter INTEGER, position TEXT, minutes REAL, rating REAL,
    goals REAL, assists REAL, xg REAL, xa REAL, shots REAL, shots_on REAL,
    key_passes REAL, fouls REAL, was_fouled REAL, tackles REAL,
    interceptions REAL, duels_won REAL, duels_lost REAL, saves REAL,
    yellow REAL, red REAL,
    PRIMARY KEY (event_id, player_id)
);
CREATE INDEX IF NOT EXISTS ix_pstats_team ON player_stats(team_id, event_id);

-- id de un equipo en otra fuente -> id en teams (SofaScore, o negativo si aún no se emparejó)
CREATE TABLE IF NOT EXISTS team_alias (
    source TEXT, ext_id TEXT, team_id INTEGER, PRIMARY KEY (source, ext_id)
);

-- totales por temporada de Understat (respaldo para el aporte de jugadores)
CREATE TABLE IF NOT EXISTS us_players (
    season INTEGER, ut_id INTEGER, team_id INTEGER, us_player_id INTEGER, name TEXT, position TEXT,
    games REAL, minutes REAL, goals REAL, assists REAL, xg REAL, xa REAL, shots REAL,
    key_passes REAL, yellow REAL, red REAL,
    PRIMARY KEY (season, team_id, us_player_id)
);

-- lo que dijo el modelo antes de cada partido (para compararlo con el resultado)
CREATE TABLE IF NOT EXISTS predictions (
    event_id INTEGER PRIMARY KEY, ut_id INTEGER, saved_ts INTEGER, source TEXT,
    p_home REAL, p_draw REAL, p_away REAL, lam_h REAL, lam_a REAL, over25 REAL, btts REAL,
    means TEXT, disp TEXT, referee TEXT
);

-- árbitro y alineación de partidos próximos (actualización automática antes del partido)
CREATE TABLE IF NOT EXISTS fixture_live (
    event_id INTEGER PRIMARY KEY, referee TEXT, lineups TEXT, fetched_ts INTEGER
);

-- partidos de ESPN y a qué evento de la base corresponden
CREATE TABLE IF NOT EXISTS espn_events (
    espn_id TEXT PRIMARY KEY, ut_id INTEGER, ts INTEGER, home_ext TEXT, away_ext TEXT,
    completed INTEGER, done INTEGER DEFAULT 0, event_id INTEGER  -- done: 0 pendiente, 1 ok, 2 sin pareja
);
"""

# columnas añadidas después de la primera versión
MIGRATIONS = {
    "events": [("source", "TEXT DEFAULT 'sofa'"), ("dup", "INTEGER DEFAULT 0"), ("referee", "TEXT"),
               ("live_clock", "TEXT"),
               ("ts_espn", "INTEGER DEFAULT 0")],  # 1 = fecha corregida con ESPN (Understat no la pisa)  # minuto en vivo («67'», «ET», «Final») según ESPN
}


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()
    return conn


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


# ---------------------------------------------------------------- escritura

def upsert_team(conn, t, league_id=None):
    conn.execute(
        """INSERT INTO teams(id, name, short_name, league_id) VALUES (?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name, short_name=excluded.short_name,
           league_id=COALESCE(excluded.league_id, teams.league_id)""",
        (t["id"], t.get("name"), t.get("shortName"), league_id),
    )


def upsert_player(conn, p):
    conn.execute(
        """INSERT INTO players(id, name, short_name, position) VALUES (?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name, short_name=excluded.short_name,
           position=COALESCE(excluded.position, players.position)""",
        (p["id"], p.get("name"), p.get("shortName"), p.get("position")),
    )


def upsert_event(conn, e):
    ut = (e.get("tournament") or {}).get("uniqueTournament") or {}
    ut_id = ut.get("id")
    league = ut_id if ut_id in LEAGUES else None
    upsert_team(conn, e["homeTeam"], league)
    upsert_team(conn, e["awayTeam"], league)
    hs, as_ = e.get("homeScore") or {}, e.get("awayScore") or {}
    season = e.get("season") or {}
    conn.execute(
        """INSERT INTO events(id, custom_id, ut_id, ut_name, season_id, season_name, round, start_ts,
               status, home_id, away_id, home_goals, away_goals, home_ht, away_ht, winner_code)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET custom_id=excluded.custom_id, ut_id=excluded.ut_id,
               ut_name=excluded.ut_name, season_id=excluded.season_id, season_name=excluded.season_name,
               round=excluded.round, start_ts=excluded.start_ts, status=excluded.status,
               home_id=excluded.home_id, away_id=excluded.away_id, home_goals=excluded.home_goals,
               away_goals=excluded.away_goals, home_ht=excluded.home_ht, away_ht=excluded.away_ht,
               winner_code=excluded.winner_code""",
        (
            e["id"], e.get("customId"), ut_id, ut.get("name") or (e.get("tournament") or {}).get("name"),
            season.get("id"), season.get("name"), (e.get("roundInfo") or {}).get("round"),
            e.get("startTimestamp"), (e.get("status") or {}).get("type"),
            e["homeTeam"]["id"], e["awayTeam"]["id"],
            hs.get("normaltime", hs.get("current")), as_.get("normaltime", as_.get("current")),
            hs.get("period1"), as_.get("period1"), e.get("winnerCode"),
        ),
    )


def save_team_stats(conn, event_id, home_id, away_id, data):
    if not data:
        return
    items = {}
    for period in data.get("statistics", []):
        if period.get("period") != "ALL":
            continue
        for g in period.get("groups", []):
            for it in g.get("statisticsItems", []):
                items.setdefault(it["key"], (it.get("homeValue"), it.get("awayValue")))
    if not items:
        return
    for side, team_id in ((0, home_id), (1, away_id)):
        vals = {}
        for col, keys in TEAM_STAT_KEYS.items():
            v = next((items[k][side] for k in keys if k in items), None)
            if v is None and col in ZERO_IF_MISSING:
                v = 0
            vals[col] = v
        raw = {k: v[side] for k, v in items.items()}
        cols = list(vals)
        conn.execute(
            f"""INSERT OR REPLACE INTO team_stats(event_id, team_id, is_home, {', '.join(cols)}, raw)
                VALUES (?,?,?,{','.join('?' * len(cols))},?)""",
            (event_id, team_id, 1 - side, *vals.values(), json.dumps(raw)),
        )


def save_lineups(conn, event_id, home_id, away_id, lineups, incidents):
    if not lineups:
        return
    cards = {}
    for inc in (incidents or {}).get("incidents", []):
        if inc.get("incidentType") != "card" or inc.get("rescinded"):
            continue
        pid = inc.get("player", {}).get("id") if isinstance(inc.get("player"), dict) else inc.get("player")
        if pid is None:
            continue
        y, r = cards.get(pid, (0, 0))
        cls = inc.get("incidentClass")
        if cls == "yellow":
            y += 1
        elif cls in ("red", "yellowRed"):
            r += 1
        cards[pid] = (y, r)

    for side, is_home, team_id in (("home", 1, home_id), ("away", 0, away_id)):
        for p in (lineups.get(side) or {}).get("players", []):
            pl = p["player"]
            upsert_player(conn, pl)
            s = p.get("statistics") or {}
            y, r = cards.get(pl["id"], (0, 0))
            conn.execute(
                """INSERT OR REPLACE INTO player_stats VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, team_id, pl["id"], is_home, 0 if p.get("substitute") else 1,
                    p.get("position") or pl.get("position"), s.get("minutesPlayed", 0), s.get("rating"),
                    s.get("goals", 0), s.get("goalAssist", 0), s.get("expectedGoals", 0),
                    s.get("expectedAssists", 0), s.get("totalShots", 0), s.get("onTargetScoringAttempt", 0),
                    s.get("keyPass", 0), s.get("fouls", 0), s.get("wasFouled", 0), s.get("totalTackle", 0),
                    s.get("interceptionWon", 0), s.get("duelWon", 0), s.get("duelLost", 0),
                    s.get("saves", 0), y, r,
                ),
            )


def mark_details(conn, event_id):
    conn.execute("UPDATE events SET details=1 WHERE id=?", (event_id,))


# ---------------------------------------------------------------- otras fuentes

def team_for_source(conn, source, ext_id, name, league_id):
    """id del equipo para un equipo de otra fuente; si no existe se crea con id negativo
    hasta que reconcile() lo empareje con el de SofaScore."""
    row = conn.execute("SELECT team_id FROM team_alias WHERE source=? AND ext_id=?", (source, str(ext_id))).fetchone()
    if row:
        conn.execute("UPDATE teams SET league_id=? WHERE id=?", (league_id, row["team_id"]))
        return row["team_id"]
    tid = -int(ext_id)
    conn.execute("INSERT OR IGNORE INTO teams(id, name, short_name, league_id) VALUES (?,?,?,?)",
                 (tid, name, name, league_id))
    conn.execute("INSERT INTO team_alias(source, ext_id, team_id) VALUES (?,?,?)", (source, str(ext_id), tid))
    return tid


def upsert_external_event(conn, eid, source, ut_id, ut_name, season_id, season_name, ts, status,
                          home_id, away_id, hg, ag):
    winner = None if hg is None else 1 if hg > ag else 2 if ag > hg else 3
    conn.execute(
        """INSERT INTO events(id, ut_id, ut_name, season_id, season_name, start_ts, status, home_id, away_id,
               home_goals, away_goals, winner_code, details, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)
           ON CONFLICT(id) DO UPDATE SET
               start_ts=CASE WHEN events.ts_espn=1 AND excluded.source<>'espn' THEN events.start_ts
                             ELSE excluded.start_ts END,
               status=CASE WHEN excluded.status='notstarted' AND events.status IN ('inprogress', 'finished')
                           THEN events.status ELSE excluded.status END,
               home_id=excluded.home_id, away_id=excluded.away_id,
               home_goals=COALESCE(excluded.home_goals, events.home_goals),
               away_goals=COALESCE(excluded.away_goals, events.away_goals),
               winner_code=COALESCE(excluded.winner_code, events.winner_code)""",
        (eid, ut_id, ut_name, season_id, season_name, ts, status, home_id, away_id, hg, ag, winner, source),
    )


def _merge_team(conn, old, new):
    for sql in ("UPDATE events SET home_id=? WHERE home_id=?",
                "UPDATE events SET away_id=? WHERE away_id=?",
                "UPDATE OR REPLACE team_stats SET team_id=? WHERE team_id=?",
                "UPDATE OR REPLACE us_players SET team_id=? WHERE team_id=?",
                "UPDATE OR REPLACE player_stats SET team_id=? WHERE team_id=?",
                "UPDATE team_alias SET team_id=? WHERE team_id=?"):
        conn.execute(sql, (new, old))
    conn.execute("DELETE FROM teams WHERE id=?", (old,))


def reconcile(conn):
    """Empareja equipos de Understat con los de SofaScore y marca partidos duplicados.

    Cuando un partido está en las dos fuentes se usa el de SofaScore (tiene más
    estadísticas) y el de Understat queda con dup=1.
    """
    from names import assign

    for league_id in LEAGUES:
        if league_id in ESPN_ONLY:
            continue
        pending = conn.execute("SELECT id, name FROM teams WHERE id<0 AND league_id=?", (league_id,)).fetchall()
        if not pending:
            continue
        sofa = conn.execute(
            """SELECT id, name FROM teams WHERE id>0 AND league_id=?
               AND id NOT IN (SELECT team_id FROM team_alias WHERE source='us')""", (league_id,)).fetchall()
        for old, new in assign([(r["id"], r["name"]) for r in pending], [(r["id"], r["name"]) for r in sofa]).items():
            _merge_team(conn, old, new)
    mark_dups(conn)


def mark_dups(conn):
    """dup=1 al partido de Understat/ESPN que ya está en SofaScore. Se repite tras corregir fechas con ESPN y en
    cada refresco en vivo: si no, un partido cuya fecha se igualó después quedaba doble (Bologna–Monza 31/10).
    Basta con que el de SofaScore vaya igual o más avanzado (no empezado < en vivo < terminado): exigir el mismo
    estado lo desmarcaba en cuanto uno de los dos pasaba a «en vivo» (Udinese–Roma 31/10)."""
    rank = "CASE {t}.status WHEN 'finished' THEN 2 WHEN 'inprogress' THEN 1 ELSE 0 END"
    conn.execute("UPDATE events SET dup=0 WHERE source<>'sofa'")
    conn.execute(
        f"""UPDATE events SET dup=1 WHERE source<>'sofa' AND EXISTS (
               SELECT 1 FROM events s WHERE s.source='sofa' AND s.home_id=events.home_id
               AND s.away_id=events.away_id AND ABS(s.start_ts - events.start_ts) < 172800
               AND {rank.format(t='s')} >= {rank.format(t='events')})""")
    conn.commit()


# ---------------------------------------------------------------- lectura

def pending_details(conn):
    return conn.execute(
        """SELECT id, home_id, away_id FROM events
           WHERE status='finished' AND details=0 AND source='sofa' ORDER BY start_ts DESC"""
    ).fetchall()


def upcoming(conn, ut_ids, days):
    now = int(time.time())
    q = ",".join("?" * len(ut_ids))
    return conn.execute(
        f"""SELECT e.*, th.name AS home_name, ta.name AS away_name FROM events e
            JOIN teams th ON th.id=e.home_id JOIN teams ta ON ta.id=e.away_id
            WHERE e.status='notstarted' AND e.dup=0 AND e.ut_id IN ({q}) AND e.start_ts BETWEEN ? AND ?
            ORDER BY e.start_ts""",
        (*ut_ids, now - 3 * 3600, now + days * 86400),
    ).fetchall()


def teams_of_league(conn, ut_id):
    return conn.execute(
        "SELECT * FROM teams WHERE league_id=? ORDER BY name", (ut_id,)
    ).fetchall()


def counts(conn):
    c = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "partidos": c("SELECT COUNT(*) FROM events WHERE status='finished' AND dup=0"),
        "con_faltas_tarjetas": c("SELECT COUNT(DISTINCT event_id) FROM team_stats WHERE fouls IS NOT NULL"),
        "proximos": c("SELECT COUNT(*) FROM events WHERE status='notstarted' AND dup=0"),
        "jugadores": c("SELECT COUNT(*) FROM players") + c("SELECT COUNT(DISTINCT us_player_id) FROM us_players"),
        "ultimo_scrape": get_meta(conn, "last_scrape") or "nunca",
    }
