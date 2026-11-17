from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "sofascore.db"

# uniqueTournament ids de SofaScore
LEAGUES = {
    17: "Premier League",
    8: "LaLiga",
    23: "Serie A",
    34: "Ligue 1",
    35: "Bundesliga",
    7: "Champions League",
}

# código de cada liga en understat.com
UNDERSTAT_LEAGUES = {
    17: "EPL",
    8: "La_liga",
    23: "Serie_A",
    34: "Ligue_1",
    35: "Bundesliga",
}

# código de cada liga en la API de ESPN
ESPN_LEAGUES = {
    17: "eng.1",
    8: "esp.1",
    23: "ita.1",
    34: "fra.1",
    35: "ger.1",
    7: "uefa.champions",
}
# competiciones que Understat no cubre: ESPN crea sus partidos y equipos
ESPN_ONLY = {7}

# columna en team_stats -> claves posibles en /event/{id}/statistics
TEAM_STAT_KEYS = {
    "possession": ["ballPossession"],
    "xg": ["expectedGoals"],
    "shots": ["totalShotsOnGoal"],
    "shots_on": ["shotsOnGoal"],
    "big_chances": ["bigChanceCreated", "bigChances"],
    "corners": ["cornerKicks"],
    "fouls": ["fouls"],
    "yellow": ["yellowCards"],
    "red": ["redCards"],
    "saves": ["goalkeeperSaves"],
    "passes": ["passes"],
    "tackles": ["totalTackle"],
    "free_kicks": ["freeKicks"],
    "offsides": ["offsides"],
}
# si el partido tiene estadísticas y la clave no viene, SofaScore la omite porque vale 0
ZERO_IF_MISSING = {"yellow", "red", "offsides", "big_chances"}

# (columna, etiqueta) para mostrar en la app
METRICS = [
    ("goals", "Goles"),
    ("xg", "Goles esperados (xG)"),
    ("shots", "Tiros totales"),
    ("shots_on", "Tiros a puerta"),
    ("big_chances", "Grandes ocasiones"),
    ("corners", "Tiros de esquina"),
    ("fouls", "Faltas"),
    ("yellow", "Tarjetas amarillas"),
    ("red", "Tarjetas rojas"),
    ("possession", "Posesión %"),
    ("saves", "Atajadas"),
    ("passes", "Pases"),
    ("tackles", "Entradas"),
    ("free_kicks", "Tiros libres"),
    ("offsides", "Fueras de juego"),
]
