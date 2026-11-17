"""Modelo de predicción por liga.

Para cada estadística de conteo (goles, faltas, tarjetas, córners, tiros…):

    esperado_local = media_liga_local × produce_local × concede_visitante × árbitro × alineación × sesgo

- produce_X: lo que el equipo X genera respecto a la media de la liga (50 % en su
  condición local/visitante, 50 % total), con más peso a los partidos recientes
  (vida media `hl` días) y encogido hacia 1 con `k` partidos "de liga promedio".
- concede_Y: lo que los rivales de Y generan contra él (ej. faltas que le hacen).
- árbitro: media de su liga en faltas/tarjetas respecto a la media, encogida.

La distribución de cada conteo es binomial negativa (más dispersa que Poisson,
como pasa en faltas y tarjetas). `hl`, `k`, el sesgo y la dispersión se
calibran con los partidos ya jugados (backtest sin mirar el futuro).
Goles: Poisson con corrección Dixon-Coles para marcadores bajos.
1X2: el modelo de goles se queda algo corto con los favoritos (en los 3 años probados); `fit_1x2` aprende
cuánto estirar local vs visitante (el empate no se toca) y solo lo aplica si mejora en el año de prueba.
"""
import json
import math
import time
from bisect import bisect_left
from collections import defaultdict

from names import similarity

DAY = 86400
WINDOW = 730 * DAY
COUNT_METRICS = ["gx", "shots", "shots_on", "corners", "fouls", "yellow", "red", "offsides", "saves"]
REF_METRICS = {"fouls", "yellow", "red"}
# mercados de apuestas que se muestran
MARKETS = [
    ("fouls", "Faltas"), ("yellow", "Tarjetas amarillas"), ("corners", "Córners"),
    ("shots", "Tiros totales"), ("shots_on", "Tiros a puerta"), ("offsides", "Fueras de juego"),
    ("saves", "Atajadas"),
]
BASE_HL = 75.0  # vida media (días) del nivel de la liga: capta rápido cambios de criterio arbitral
DEFAULT = {"hl": 240.0, "k": 5.0, "bias": 1.0, "r_side": 30.0, "r_tot": 30.0}
GRID = [(hl, k) for hl in (120.0, 240.0, 480.0) for k in (3.0, 8.0)]


# ---------------------------------------------------------------- distribuciones

def nb_pmf(mu, r, kmax):
    """Binomial negativa con media mu y var = mu + mu²/r (r grande -> Poisson)."""
    mu = max(mu, 1e-6)
    if r is None or r > 5000:
        out = [math.exp(-mu)]
        for k in range(1, kmax + 1):
            out.append(out[-1] * mu / k)
        return out
    p = r / (r + mu)
    out = [p ** r]
    for k in range(1, kmax + 1):
        out.append(out[-1] * (k - 1 + r) / k * (1 - p))
    return out


def prob_over(mu, r, line):
    """P(X > line) para líneas tipo 11.5."""
    kmax = int(line)
    return max(0.0, min(1.0, 1 - sum(nb_pmf(mu, r, kmax))))


def lines_around(mu, n=5):
    c = math.floor(mu) + 0.5
    start = c - (n // 2)
    return [start + i for i in range(n) if start + i > 0]


# ---------------------------------------------------------------- datos

class LeagueModel:
    def __init__(self, conn, ut_id):
        self.ut_id = ut_id
        self.conn = conn
        rows = conn.execute(
            """SELECT e.id, e.start_ts, e.home_id, e.away_id, e.home_goals, e.away_goals, e.referee,
                      h.xg AS h_xg, a.xg AS a_xg, h.shots AS h_shots, a.shots AS a_shots,
                      h.shots_on AS h_shots_on, a.shots_on AS a_shots_on, h.corners AS h_corners,
                      a.corners AS a_corners, h.fouls AS h_fouls, a.fouls AS a_fouls, h.yellow AS h_yellow,
                      a.yellow AS a_yellow, h.red AS h_red, a.red AS a_red, h.offsides AS h_offsides,
                      a.offsides AS a_offsides, h.saves AS h_saves, a.saves AS a_saves,
                      h.possession AS h_possession, a.possession AS a_possession
               FROM events e
               LEFT JOIN team_stats h ON h.event_id=e.id AND h.team_id=e.home_id
               LEFT JOIN team_stats a ON a.event_id=e.id AND a.team_id=e.away_id
               WHERE e.ut_id=? AND e.status='finished' AND e.dup=0 AND e.home_goals IS NOT NULL
               ORDER BY e.start_ts""", (ut_id,)).fetchall()
        self.matches = []
        for r in rows:
            m = {"id": r["id"], "ts": r["start_ts"], "home": r["home_id"], "away": r["away_id"],
                 "ref": r["referee"], "h": {}, "a": {}}
            for side, g, xg in (("h", r["home_goals"], r["h_xg"]), ("a", r["away_goals"], r["a_xg"])):
                m[side]["goals"] = g
                m[side]["gx"] = g if xg is None else 0.5 * g + 0.5 * xg
                m[side]["xg"] = xg
                for k in ("shots", "shots_on", "corners", "fouls", "yellow", "red", "offsides", "saves",
                          "possession"):
                    m[side][k] = r[f"{side}_{k}"]
            # estadísticas incompletas (algunos partidos antiguos): se descartan
            if m["h"]["fouls"] is not None and m["a"]["fouls"] is not None and m["h"]["fouls"] + m["a"]["fouls"] < 6:
                for side in ("h", "a"):
                    for k in ("shots", "shots_on", "corners", "fouls", "yellow", "red", "offsides", "saves",
                              "possession"):
                        m[side][k] = None
            self.matches.append(m)
        self.ts = [m["ts"] for m in self.matches]
        self.by_team = defaultdict(list)
        for i, m in enumerate(self.matches):
            self.by_team[m["home"]].append(i)
            self.by_team[m["away"]].append(i)
        self.by_ref = defaultdict(list)
        for i, m in enumerate(self.matches):
            if m["ref"]:
                self.by_ref[m["ref"]].append(i)
        self._build_baselines()
        self.params = self._load_params()
        row = conn.execute("SELECT value FROM meta WHERE key=?", (f"reliab:{ut_id}",)).fetchone()
        self.reliability = json.loads(row["value"]) if row else {}
        row = conn.execute("SELECT value FROM meta WHERE key=?", (X12_KEY,)).fetchone()
        self.x12_b = json.loads(row["value"])["b"] if row else 1.0

    # -- parámetros calibrados
    def _load_params(self):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (f"model:{self.ut_id}",)).fetchone()
        p = json.loads(row["value"]) if row else {}
        out = {m: {**DEFAULT, **p.get(m, {})} for m in COUNT_METRICS}
        out["rho"] = p.get("rho", -0.05)
        out["calibrated"] = bool(row)
        return out

    def save_params(self):
        data = {m: self.params[m] for m in COUNT_METRICS}
        data["rho"] = self.params["rho"]
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                          (f"model:{self.ut_id}", json.dumps(data)))
        self.conn.commit()

    # -- nivel de la liga en cada momento
    def _build_baselines(self):
        """Media móvil exponencial (vida media BASE_HL) de cada estadística, local y visitante.

        base_before[metric][i] = nivel de la liga justo antes del partido i. Entre partidos la
        media no cambia (decaer todos los pesos por igual no altera el promedio).
        """
        self.base_before = {}
        self.base_final = {}
        for metric in COUNT_METRICS + ["possession"]:
            sh = sa = w = 0.0
            last_ts = None
            before = []
            for m in self.matches:
                if last_ts is not None:
                    f = 0.5 ** ((m["ts"] - last_ts) / DAY / BASE_HL)
                    sh, sa, w = sh * f, sa * f, w * f
                last_ts = m["ts"]
                before.append((sh / w, sa / w) if w > 1.5 else (None, None))
                vh, va = m["h"].get(metric), m["a"].get(metric)
                if vh is not None and va is not None:
                    sh, sa, w = sh + vh, sa + va, w + 1
            self.base_before[metric] = before
            self.base_final[metric] = (sh / w, sa / w) if w > 1.5 else (None, None)

    def baseline(self, metric, as_of, hl=None):
        i = bisect_left(self.ts, as_of)
        return self.base_final[metric] if i >= len(self.matches) else self.base_before[metric][i]

    def _ratio(self, team, metric, kind, as_of, hl, k, venue, bh, ba):
        idx = self.by_team.get(team, [])
        num = den = 0.0
        for i in reversed(idx):
            m = self.matches[i]
            if m["ts"] >= as_of:
                continue
            if m["ts"] < as_of - WINDOW:
                break
            is_home = m["home"] == team
            if (venue == "home" and not is_home) or (venue == "away" and is_home):
                continue
            own, opp = ("h", "a") if is_home else ("a", "h")
            side = own if kind == "for" else opp
            v = m[side].get(metric)
            if v is None:
                continue
            # se compara con el nivel de la liga cuando se jugó ese partido
            bh_t, ba_t = self.base_before[metric][i]
            if bh_t is None:
                continue
            w = 0.5 ** ((as_of - m["ts"]) / DAY / hl)
            num += w * v
            den += w * (bh_t if side == "h" else ba_t)
        avg = (bh + ba) / 2
        return (num + k * avg) / (den + k * avg)

    def team_strengths(self, home, away, metric, as_of, hl, k):
        bh, ba = self.baseline(metric, as_of, hl)
        if bh is None or bh + ba == 0:
            return None
        att_h = 0.5 * self._ratio(home, metric, "for", as_of, hl, k, "home", bh, ba) + \
            0.5 * self._ratio(home, metric, "for", as_of, hl, k, "all", bh, ba)
        con_a = 0.5 * self._ratio(away, metric, "against", as_of, hl, k, "away", bh, ba) + \
            0.5 * self._ratio(away, metric, "against", as_of, hl, k, "all", bh, ba)
        att_a = 0.5 * self._ratio(away, metric, "for", as_of, hl, k, "away", bh, ba) + \
            0.5 * self._ratio(away, metric, "for", as_of, hl, k, "all", bh, ba)
        con_h = 0.5 * self._ratio(home, metric, "against", as_of, hl, k, "home", bh, ba) + \
            0.5 * self._ratio(home, metric, "against", as_of, hl, k, "all", bh, ba)
        return {"bh": bh, "ba": ba, "att_h": att_h, "con_a": con_a, "att_a": att_a, "con_h": con_h,
                "mu_h": bh * att_h * con_a, "mu_a": ba * att_a * con_h}

    def match_ref(self, name):
        """Nombre del árbitro tal como está en la base (las fuentes lo escriben distinto)."""
        if not name:
            return None
        if name in self.by_ref:
            return name
        best = max(((similarity(name, r), r) for r in self.by_ref), default=(0, None))
        return best[1] if best[0] >= 0.8 else None

    def ref_factor(self, ref, metric, as_of, kr=8.0):
        bh, ba = self.baseline(metric, as_of, DEFAULT["hl"])
        if not ref or ref not in self.by_ref or bh is None:
            return 1.0, 0
        tot = n = 0
        for i in self.by_ref[ref]:
            m = self.matches[i]
            if m["ts"] >= as_of or m["ts"] < as_of - WINDOW:
                continue
            vh, va = m["h"].get(metric), m["a"].get(metric)
            if vh is None or va is None:
                continue
            tot += vh + va
            n += 1
        avg = bh + ba
        return (tot + kr * avg) / ((n + kr) * avg), n

    # -- predicción
    def predict(self, home, away, as_of=None, ref=None, lineup=None):
        """lineup: multiplicadores {'gx_h','gx_a','fouls_h','fouls_a','yellow_h','yellow_a'} (opcional)."""
        as_of = as_of or int(time.time())
        lineup = lineup or {}
        ref = self.match_ref(ref)
        out = {"means": {}, "ref": ref, "ref_games": 0, "strength": {}}
        for metric in COUNT_METRICS:
            p = self.params[metric]
            s = self.team_strengths(home, away, metric, as_of, p["hl"], p["k"])
            if s is None:
                out["means"][metric] = (None, None)
                continue
            mh, ma = s["mu_h"] * p["bias"], s["mu_a"] * p["bias"]
            if metric in REF_METRICS:
                f, n = self.ref_factor(ref, metric, as_of)
                mh, ma = mh * f, ma * f
                if metric == "fouls":
                    out["ref_factor_fouls"], out["ref_games"] = f, n
                if metric == "yellow":
                    out["ref_factor_yellow"] = f
            mh *= lineup.get(f"{metric}_h", 1.0)
            ma *= lineup.get(f"{metric}_a", 1.0)
            out["means"][metric] = (mh, ma)
            out["strength"][metric] = s
        out["means"]["possession"] = self._possession(home, away, as_of)
        lam_h, lam_a = out["means"]["gx"]
        out.update(self._score_probs(lam_h or 1.3, lam_a or 1.1, self.params["rho"]))
        out["p_raw"] = (out["p_home"], out["p_draw"], out["p_away"])
        out["p_home"], out["p_draw"], out["p_away"] = stretch_1x2(out["p_raw"], self.x12_b)
        return out

    def _avg_own(self, team, key, as_of, hl=240.0):
        num = den = 0.0
        for i in reversed(self.by_team.get(team, [])):
            m = self.matches[i]
            if m["ts"] >= as_of:
                continue
            if m["ts"] < as_of - WINDOW:
                break
            v = m["h" if m["home"] == team else "a"].get(key)
            if v is None:
                continue
            w = 0.5 ** ((as_of - m["ts"]) / DAY / hl)
            num += w * v
            den += w
        return num / den if den else None

    def _possession(self, home, away, as_of):
        ph, pa = self._avg_own(home, "possession", as_of), self._avg_own(away, "possession", as_of)
        if ph is None or pa is None:
            return (None, None)
        h = (ph + (100 - pa)) / 2
        return (h, 100 - h)

    @staticmethod
    def _score_probs(lam_h, lam_a, rho, n=10):
        ph = nb_pmf(lam_h, None, n - 1)
        pa = nb_pmf(lam_a, None, n - 1)
        grid = [[ph[i] * pa[j] for j in range(n)] for i in range(n)]
        # Dixon-Coles: corrige 0-0, 1-0, 0-1, 1-1
        grid[0][0] *= 1 - lam_h * lam_a * rho
        grid[0][1] *= 1 + lam_h * rho
        grid[1][0] *= 1 + lam_a * rho
        grid[1][1] *= 1 - rho
        tot = sum(map(sum, grid))
        grid = [[v / tot for v in row] for row in grid]
        p1 = sum(grid[i][j] for i in range(n) for j in range(n) if i > j)
        px = sum(grid[i][i] for i in range(n))
        p2 = 1 - p1 - px
        over = lambda line: sum(grid[i][j] for i in range(n) for j in range(n) if i + j > line)
        return {
            "lam_h": lam_h, "lam_a": lam_a, "p_home": p1, "p_draw": px, "p_away": p2, "grid": grid,
            "scores": sorted(((grid[i][j], f"{i}-{j}") for i in range(n) for j in range(n)), reverse=True)[:6],
            "over15": over(1.5), "over25": over(2.5), "over35": over(3.5),
            "btts": sum(grid[i][j] for i in range(1, n) for j in range(1, n)),
        }

    def market_probs(self, metric, means, line, who="total"):
        """P(más) de una línea. who: 'home', 'away' o 'total'."""
        mh, ma = means[metric]
        if mh is None:
            return None
        p = self.params[metric]
        if who == "total":
            return prob_over(mh + ma, p["r_tot"], line)
        return prob_over(mh if who == "home" else ma, p["r_side"], line)

    # ---------------------------------------------------------------- calibración y validación

    def _eval_set(self, n, skip_last=0):
        """Últimos n partidos con al menos 120 días de historia previa (sin los skip_last más recientes)."""
        if not self.matches:
            return []
        first = self.matches[0]["ts"] + 120 * DAY
        cand = [m for m in self.matches if m["ts"] >= first]
        if skip_last:
            cand = cand[:-skip_last]
        return cand[-n:]

    def calibrate(self, n=400, progress=None, skip_last=0, save=True):
        """Ajusta hl/k por estadística (mínimo error), luego sesgo y dispersión. Sin mirar el futuro."""
        evs = self._eval_set(n, skip_last)
        if len(evs) < 60:
            return False
        for mi, metric in enumerate(COUNT_METRICS):
            obs_key = "goals" if metric == "gx" else metric
            sample = [m for m in evs if m["h"].get(obs_key) is not None and m["a"].get(obs_key) is not None]
            if len(sample) < 60:
                continue
            best = None
            for hl, k in GRID:
                err, preds = 0.0, []
                for m in sample:
                    s = self.team_strengths(m["home"], m["away"], metric, m["ts"], hl, k)
                    if s is None:
                        continue
                    f = self.ref_factor(m["ref"], metric, m["ts"])[0] if metric in REF_METRICS else 1.0
                    mh, ma = s["mu_h"] * f, s["mu_a"] * f
                    yh, ya = m["h"][obs_key], m["a"][obs_key]
                    err += (yh - mh) ** 2 + (ya - ma) ** 2
                    preds.append((mh, ma, yh, ya))
                if preds and (best is None or err / len(preds) < best[0]):
                    best = (err / len(preds), hl, k, preds)
            if best is None:
                continue
            _, hl, k, preds = best
            sy = sum(yh + ya for _, _, yh, ya in preds)
            smu = sum(mh + ma for mh, ma, _, _ in preds)
            bias = max(0.9, min(1.1, sy / smu)) if smu else 1.0
            r_side = self._dispersion([(mh * bias, yh) for mh, _, yh, _ in preds] +
                                      [(ma * bias, ya) for _, ma, _, ya in preds])
            r_tot = self._dispersion([((mh + ma) * bias, yh + ya) for mh, ma, yh, ya in preds])
            self.params[metric] = {"hl": hl, "k": k, "bias": bias, "r_side": r_side, "r_tot": r_tot}
            if progress:
                progress(mi + 1, len(COUNT_METRICS) + 1)
        self._calibrate_rho(evs)
        self.params["calibrated"] = True
        if save:
            self.save_params()
        return True

    def holdout_report(self, n_test=250, progress=None):
        """Validación honesta: calibra con partidos anteriores y evalúa en los n_test más recientes,
        que el modelo no vio. Después recalibra con todo para las predicciones."""
        def part(lo, hi):  # progress(fracción 0-1) repartido entre las tres etapas
            return (lambda d, t: progress(lo + (hi - lo) * d / t)) if progress else None
        self.calibrate(400, skip_last=n_test, save=False, progress=part(0.0, 0.45))
        report = self.validate(n_test)
        if progress:
            progress(0.55)
        self.calibrate(400, progress=part(0.55, 1.0))
        if report["n"]:
            self.reliability = reliability(report)
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                              (f"reliab:{self.ut_id}", json.dumps(self.reliability)))
            self.conn.commit()
        return report

    @staticmethod
    def _dispersion(pairs):
        """r de la binomial negativa por momentos: var(resid) = mu + mu²/r."""
        if not pairs:
            return DEFAULT["r_side"]
        mu = sum(p for p, _ in pairs) / len(pairs)
        var = sum((y - p) ** 2 for p, y in pairs) / len(pairs)
        mu2 = sum(p * p for p, _ in pairs) / len(pairs)
        extra = var - mu
        if extra <= 0.02 * mu:
            return 10000.0  # prácticamente Poisson
        return max(1.0, mu2 / extra)

    def _calibrate_rho(self, evs):
        p = self.params["gx"]
        cache = []
        for m in evs:
            s = self.team_strengths(m["home"], m["away"], "gx", m["ts"], p["hl"], p["k"])
            if s:
                cache.append((s["mu_h"] * p["bias"], s["mu_a"] * p["bias"], m["h"]["goals"], m["a"]["goals"]))
        best = None
        for rho in (-0.2, -0.15, -0.1, -0.05, 0.0, 0.05):
            ll = 0.0
            for lh, la, gh, ga in cache:
                g = self._score_probs(lh, la, rho)["grid"]
                ll += math.log(max(g[min(gh, 9)][min(ga, 9)], 1e-9))
            if best is None or ll > best[0]:
                best = (ll, rho)
        if best:
            self.params["rho"] = best[1]

    def validate(self, n=250):
        """Backtest: para cada partido se predice solo con lo que había antes."""
        evs = self._eval_set(n)
        res = {"n": 0, "acc": 0, "ll": 0.0, "ll_base": 0.0, "brier": 0.0, "o25_hit": 0, "o25_brier": 0.0,
               "markets": {}}
        if not evs:
            return res
        outcomes = [0, 0, 0]
        preds = []
        for m in evs:
            pr = self.predict(m["home"], m["away"], m["ts"], m["ref"])
            gh, ga = m["h"]["goals"], m["a"]["goals"]
            o = 0 if gh > ga else 1 if gh == ga else 2
            outcomes[o] += 1
            preds.append((m, pr, o))
        base = [c / len(evs) for c in outcomes]
        for m, pr, o in preds:
            probs = [pr["p_home"], pr["p_draw"], pr["p_away"]]
            res["n"] += 1
            res["acc"] += probs.index(max(probs)) == o
            res["ll"] += -math.log(max(probs[o], 1e-9))
            res["ll_base"] += -math.log(base[o])
            res["brier"] += sum((probs[i] - (i == o)) ** 2 for i in range(3))
            tot = m["h"]["goals"] + m["a"]["goals"]
            res["o25_hit"] += (pr["over25"] > 0.5) == (tot > 2.5)
            res["o25_brier"] += (pr["over25"] - (tot > 2.5)) ** 2
            for metric, _ in MARKETS:
                yh, ya = m["h"].get(metric), m["a"].get(metric)
                mh, ma = pr["means"][metric]
                if yh is None or ya is None or mh is None:
                    continue
                st = res["markets"].setdefault(metric, {"n": 0, "mae_tot": 0.0, "mae_side": 0.0,
                                                        "picks": defaultdict(lambda: [0, 0]), "sum_y": 0.0,
                                                        "sum_mu": 0.0})
                st["n"] += 1
                st["mae_tot"] += abs((yh + ya) - (mh + ma))
                st["mae_side"] += (abs(yh - mh) + abs(ya - ma)) / 2
                st["sum_y"] += yh + ya
                st["sum_mu"] += mh + ma
                # ¿acierta el lado que el modelo prefiere en la línea .5 más cercana a su media?
                line = math.floor(mh + ma) + 0.5
                po = prob_over(mh + ma, self.params[metric]["r_tot"], line)
                conf = max(po, 1 - po)
                hit = (yh + ya > line) == (po > 0.5)
                for t in (0.55, 0.60, 0.65):
                    if conf >= t:
                        st["picks"][t][0] += 1
                        st["picks"][t][1] += hit
        n = res["n"]
        for k in ("ll", "ll_base", "brier", "o25_brier"):
            res[k] /= n
        res["acc"] /= n
        res["o25_hit"] /= n
        for st in res["markets"].values():
            st["mae_tot"] /= st["n"]
            st["mae_side"] /= st["n"]
            st["avg_y"] = st["sum_y"] / st["n"]
            st["avg_mu"] = st["sum_mu"] / st["n"]
            st["picks"] = {t: (c, h / c if c else None) for t, (c, h) in st["picks"].items()}
        return res


def reliability(report, good=0.58, ok=0.53):
    """Estado de cada mercado según la validación: fiable / regular / no fiable.

    Se mira el acierto cuando el modelo daba ≥ 60 % (si hay 20+ casos) o ≥ 55 % (30+ casos).
    """
    out = {}
    for metric, st in report["markets"].items():
        c60, h60 = st["picks"].get(0.6, (0, None))
        c55, h55 = st["picks"].get(0.55, (0, None))
        if c60 >= 20:
            hit, n = h60, c60
        elif c55 >= 30:
            hit, n = h55, c55
        else:
            continue  # muy pocos casos: sin validar
        out[metric] = {"status": "fiable" if hit >= good else "regular" if hit >= ok else "no fiable",
                       "hit": hit, "n": n}
    o25 = report["o25_hit"]
    out["gx"] = {"status": "fiable" if o25 >= 0.56 else "regular" if o25 >= 0.52 else "no fiable",
                 "hit": o25, "n": report["n"]}
    return out


X12_KEY = "calib_1x2"


def stretch_1x2(p, b):
    """Estira local vs visitante con exponente b (b > 1: favoritos más claros); el empate queda igual."""
    ph, pd, pa = p
    if b == 1.0 or ph <= 0 or pa <= 0:
        return p
    pa2 = (1 - pd) / (1 + (ph / pa) ** b)
    return (1 - pd - pa2, pd, pa2)


def fit_1x2(conn, leagues, years=3, log=print):
    """Aprende b con los partidos de hace 1-`years` años y lo prueba con el último año (que no vio).
    Si mejora, lo guarda aprendido con todo; si no, guarda b=1 (sin corrección)."""
    now = time.time()
    data = []
    for ut in leagues:
        m = get_model(conn, ut)
        for e in conn.execute("""SELECT start_ts, home_id, away_id, home_goals, away_goals FROM events
                                 WHERE ut_id=? AND status='finished' AND dup=0 AND home_goals IS NOT NULL
                                 AND start_ts>=?""", (ut, now - years * 365 * DAY)):
            p = m.predict(e["home_id"], e["away_id"], as_of=e["start_ts"])["p_raw"]
            o = 0 if e["home_goals"] > e["away_goals"] else 1 if e["home_goals"] == e["away_goals"] else 2
            data.append((e["start_ts"], p, o))

    def loss(rows, b):
        return -sum(math.log(max(stretch_1x2(p, b)[o], 1e-9)) for _, p, o in rows) / len(rows)
    grid = [0.8 + 0.02 * i for i in range(51)]
    cut = now - 365 * DAY
    train, test = [r for r in data if r[0] < cut], [r for r in data if r[0] >= cut]
    if len(train) < 500 or len(test) < 300:
        return None
    b_train = min(grid, key=lambda x: loss(train, x))
    before, after = loss(test, 1.0), loss(test, b_train)
    b = min(grid, key=lambda x: loss(data, x)) if after < before else 1.0
    info = {"b": round(b, 2), "b_train": round(b_train, 2), "test_before": before, "test_after": after,
            "n_train": len(train), "n_test": len(test), "ts": int(now)}
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (X12_KEY, json.dumps(info)))
    conn.commit()
    log(f"  Ganador (1X2): corrección de favoritos b={b:.2f} · error en el año de prueba "
        f"{before:.4f} → {after:.4f} ({'aplicada' if b != 1.0 else 'no mejora: sin corrección'})")
    return info


_cache = {}


def get_model(conn, ut_id):
    if ut_id not in _cache:
        _cache[ut_id] = LeagueModel(conn, ut_id)
    return _cache[ut_id]


def clear_cache():
    _cache.clear()
