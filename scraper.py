"""Descarga histórica + incremental de las ligas seleccionadas."""
import asyncio
import time
from datetime import datetime

import db
import espn
import model
import tracking
import understat
from config import DB_PATH, LEAGUES
from names import best_match
from sofa_client import BlockedError, SofaClient


async def _scrape_event_list(client, conn, ut_id, season_id, direction, stop):
    """Recorre /events/last/N o /events/next/N hasta que no haya más páginas."""
    page, total = 0, 0
    while not stop.is_set():
        data = await client.get(f"/unique-tournament/{ut_id}/season/{season_id}/events/{direction}/{page}")
        if not data:
            break
        for e in data.get("events", []):
            db.upsert_event(conn, e)
            total += 1
        conn.commit()
        if not data.get("hasNextPage"):
            break
        page += 1
    return total


async def _scrape_details(client, conn, log, progress, stop, batch=12):
    pending = db.pending_details(conn)
    total = len(pending)
    log(f"Partidos pendientes de detalle (estadísticas, alineaciones, tarjetas): {total}")
    done = 0
    for i in range(0, total, batch):
        if stop.is_set():
            return
        chunk = pending[i:i + batch]
        paths = []
        for ev in chunk:
            paths += [f"/event/{ev['id']}/statistics", f"/event/{ev['id']}/lineups", f"/event/{ev['id']}/incidents"]
        results = await client.get_many(paths)
        for j, ev in enumerate(chunk):
            stats, lineups, incidents = results[3 * j:3 * j + 3]
            if any(isinstance(r, Exception) for r in (stats, lineups, incidents)):
                continue  # se reintenta en el próximo scrape
            db.save_team_stats(conn, ev["id"], ev["home_id"], ev["away_id"], stats)
            db.save_lineups(conn, ev["id"], ev["home_id"], ev["away_id"], lineups, incidents)
            db.mark_details(conn, ev["id"])
        conn.commit()
        done += len(chunk)
        progress(done, total)
        if done % 120 < batch:
            log(f"  detalle {done}/{total}")


async def run_scrape(league_ids, us_seasons, espn_years, sofa_seasons, rate, log, progress, stop):
    """1) Understat: historial de resultados/xG/jugadores (pocas peticiones).
    2) ESPN: faltas, tarjetas, córners, alineaciones con minutos y árbitro (1 petición por partido).
    3) SofaScore (opcional, lento): lo mismo con más detalle; suele bloquear.
    4) Calibración del modelo de cada liga."""
    t0 = time.time()
    log("Understat: descargando historial…")
    await asyncio.to_thread(understat.import_understat, league_ids, us_seasons, log, stop)
    if espn_years > 0 and not stop.is_set():
        log("ESPN: estadísticas de partido…")
        try:
            await asyncio.to_thread(espn.import_espn, league_ids, espn_years, log, progress, stop)
        except Exception as e:  # noqa: BLE001
            log(f"⚠ ESPN: {e}. Lo descargado quedó guardado; vuelve a darle a Scrapear para continuar.")
    conn = db.connect(DB_PATH)
    blocked = False
    if sofa_seasons > 0 and not stop.is_set():
        log("SofaScore: abriendo navegador…")
        try:
            await _run(conn, league_ids, sofa_seasons, rate, log, progress, stop)
        except BlockedError:
            blocked = True
            log("⚠ SofaScore bloqueó temporalmente las peticiones. Lo descargado quedó guardado y el "
                "análisis ya funciona con Understat. Vuelve a darle a Scrapear más tarde; continuará donde quedó.")
        except Exception as e:  # noqa: BLE001
            blocked = True
            log(f"⚠ SofaScore no disponible ({e}). El análisis funciona con los datos de Understat.")
    db.reconcile(conn)
    if not stop.is_set():
        log("Calibrando el modelo con los partidos jugados…")
        await asyncio.to_thread(calibrate_all, league_ids, log)
        n = tracking.backfill(conn, league_ids)
        tracking.save_upcoming(conn)
        if n:
            log(f"Historial: {n} predicciones reconstruidas para comparar en «Resultados»")
    if not stop.is_set() and not blocked:
        db.set_meta(conn, "last_scrape", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log(f"{'Detenido' if stop.is_set() else 'Terminado'} en {(time.time() - t0) / 60:.1f} min. {db.counts(conn)}")
    conn.close()


def quick_update(league_ids, log, job=None):
    """Datos nuevos de las ligas indicadas: resultados (Understat), estadísticas de partidos nuevos
    (ESPN), recalibración y predicciones guardadas. job(fracción 0-1, texto) informa el progreso."""
    import threading

    job = job or (lambda f, t: None)
    stop = threading.Event()
    n = len(league_ids)
    for i, ut in enumerate(league_ids):
        job(0.15 * i / n, f"Resultados y calendario · {LEAGUES[ut]}")
        understat.import_understat([ut], 1, log, stop)
    job(0.15, "Estadísticas de partidos (ESPN): revisando calendario…")
    espn.import_espn(league_ids, 1, log,
                     lambda d, t: job(0.15 + 0.45 * d / max(t, 1), f"Estadísticas de partidos (ESPN) {d}/{t}"), stop)
    conn = db.connect(DB_PATH)
    db.reconcile(conn)
    calibrate_all(league_ids, log, job, (0.6, 0.95))
    job(0.96, "Guardando predicciones…")
    tracking.backfill(conn, league_ids)
    tracking.save_upcoming(conn)
    db.set_meta(conn, "last_quick_refresh", str(int(time.time())))
    db.set_meta(conn, "last_scrape", datetime.now().strftime("%Y-%m-%d %H:%M"))
    conn.close()
    job(1.0, "Listo")


def refresh_upcoming(log, minutes=45, job=None):
    """Para partidos que empiezan en menos de `minutes`: árbitro y alineación (ESPN) cada 15 min,
    y datos frescos de su liga (resultados + estadísticas nuevas + recalibración) cada 3 h como mucho.
    Devuelve [(event_id, "Local vs Visitante")] actualizados."""
    import json
    import threading

    conn = db.connect(DB_PATH)
    now = int(time.time())
    q = ",".join("?" * len(LEAGUES))
    events = conn.execute(
        f"""SELECT e.*, th.name AS home_name, ta.name AS away_name FROM events e
            JOIN teams th ON th.id=e.home_id JOIN teams ta ON ta.id=e.away_id
            WHERE e.status='notstarted' AND e.dup=0 AND e.ut_id IN ({q}) AND e.start_ts BETWEEN ? AND ?""",
        (*LEAGUES, now - 600, now + minutes * 60)).fetchall()
    updated, leagues = [], set()
    pending = []
    for ev in events:
        row = conn.execute("SELECT fetched_ts FROM fixture_live WHERE event_id=?", (ev["id"],)).fetchone()
        if not row or now - row["fetched_ts"] >= 15 * 60:
            pending.append(dict(ev))
    refresh_league = pending and now - float(db.get_meta(conn, "last_quick_refresh") or 0) > 3 * 3600
    share = 0.2 if refresh_league else 1.0  # parte de la barra para árbitro/alineaciones
    for i, ev in enumerate(pending):
        if job:
            job(share * i / len(pending), f"Auto · árbitro y alineación: {ev['home_name']} vs {ev['away_name']}")
        info = espn.fetch_fixture(conn, ev) or {}
        conn.execute("INSERT OR REPLACE INTO fixture_live(event_id, referee, lineups, fetched_ts) VALUES (?,?,?,?)",
                     (ev["id"], info.get("referee"), json.dumps(info.get("lineups") or {}), now))
        conn.commit()
        updated.append((ev["id"], f"{ev['home_name']} vs {ev['away_name']}"))
        leagues.add(ev["ut_id"])
        log(f"Auto: {ev['home_name']} vs {ev['away_name']} — árbitro {info.get('referee') or '?'}, "
            f"alineación {'confirmada' if info.get('lineups') else 'aún no publicada'}")
    if refresh_league:
        quick_update(sorted(leagues), log,
                     (lambda f, t: job(share + (1 - share) * f, f"Auto · {t}")) if job else None)
    tracking.save_upcoming(conn)
    conn.close()
    if job and pending:
        job(1.0, "Listo")
    return updated


def live_refresh(log):
    """Marcadores en vivo y finales de hoy (ESPN). Devuelve cuántos partidos cambiaron de estado/marcador."""
    conn = db.connect(DB_PATH)
    try:
        return espn.live_scores(conn, list(LEAGUES), log)
    finally:
        conn.close()


def calibrate_all(league_ids, log, job=None, span=(0.0, 1.0)):
    conn = db.connect(DB_PATH)
    model.clear_cache()
    for i, ut_id in enumerate(league_ids):
        if job:
            job(span[0] + (span[1] - span[0]) * i / len(league_ids), f"Recalibrando modelo · {LEAGUES[ut_id]}")
        m = model.LeagueModel(conn, ut_id)
        step = (span[1] - span[0]) / len(league_ids)
        rep = m.holdout_report(250, (lambda f, i=i, ut=ut_id: job(
            span[0] + step * (i + f), f"Recalibrando modelo · {LEAGUES[ut]} {100 * f:.0f}%")) if job else None)
        if rep["n"]:
            ok = [label for metric, label in [("gx", "Goles +/-2.5")] + model.MARKETS
                  if m.reliability.get(metric, {}).get("status") == "fiable"]
            log(f"  {LEAGUES[ut_id]}: modelo calibrado · mercados fiables: {', '.join(ok) or 'ninguno'}")
    model.clear_cache()
    if job:
        job(span[1], "Ajustando probabilidades del ganador…")
    model.fit_1x2(conn, list(LEAGUES), log=log)  # global (todas las ligas): ~10 s
    model.clear_cache()
    conn.close()


async def _run(conn, league_ids, n_seasons, rate, log, progress, stop):
    async with SofaClient(concurrency=3, rate=rate) as client:
        log("SofaScore cargado.")
        for ut_id in league_ids:
            if stop.is_set():
                break
            seasons = ((await client.get(f"/unique-tournament/{ut_id}/seasons")) or {}).get("seasons", [])
            for idx, s in enumerate(seasons[:n_seasons]):
                if stop.is_set():
                    break
                current = idx == 0
                done_key = f"season_done:{s['id']}"
                if not current and db.get_meta(conn, done_key):
                    log(f"{LEAGUES[ut_id]} {s['year']}: ya descargada")
                    continue
                n = await _scrape_event_list(client, conn, ut_id, s["id"], "last", stop)
                if current:
                    n += await _scrape_event_list(client, conn, ut_id, s["id"], "next", stop)
                elif not stop.is_set():
                    db.set_meta(conn, done_key, "1")
                log(f"{LEAGUES[ut_id]} {s['year']}: {n} partidos")

        db.reconcile(conn)
        if not stop.is_set():
            await _scrape_details(client, conn, log, progress, stop)


async def _find_sofa_event(client, event):
    """id de SofaScore de un partido que vino de Understat (pocas peticiones)."""
    if event["home_id"] > 0:
        paths = [f"/team/{event['home_id']}/events/next/0"]
    else:
        seasons = ((await client.get(f"/unique-tournament/{event['ut_id']}/seasons")) or {}).get("seasons", [])
        if not seasons:
            return None
        paths = [f"/unique-tournament/{event['ut_id']}/season/{seasons[0]['id']}/events/next/{p}" for p in (0, 1)]
    for path in paths:
        for e in ((await client.get(path)) or {}).get("events", []):
            if abs(e.get("startTimestamp", 0) - event["start_ts"]) > 2 * 86400:
                continue
            if (best_match(event["home_name"], [(1, e["homeTeam"]["name"])], 0.6)
                    and best_match(event["away_name"], [(1, e["awayTeam"]["name"])], 0.6)):
                return e["id"]
    return None


async def fetch_fixture_live(event):
    """Alineación (confirmada o probable), bajas y árbitro de un partido próximo."""
    async with SofaClient(concurrency=2) as client:
        event_id = event["id"] if event["id"] > 0 else await _find_sofa_event(client, event)
        if event_id is None:
            raise LookupError("No se encontró este partido en SofaScore")
        lineups, event = await client.get_many([f"/event/{event_id}/lineups", f"/event/{event_id}"])
    if isinstance(event, Exception):
        raise event
    return (
        lineups if isinstance(lineups, dict) else None,
        event.get("event") if isinstance(event, dict) else None,
    )


if __name__ == "__main__":
    import sys
    import threading

    leagues = [int(x) for x in sys.argv[1:]] or list(LEAGUES)
    asyncio.run(run_scrape(leagues, 8, 3, 0, 1.5, print, lambda d, t: None, threading.Event()))
