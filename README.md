# Analizador de fútbol — 5 grandes ligas y Champions League

App de escritorio local (Python + ttkbootstrap + SQLite) para Premier League, LaLiga, Serie A,
Ligue 1 y Bundesliga: pronóstico 1X2, goles, faltas, tarjetas, córners, tiros, aporte de cada
jugador y calculadora de valor contra las cuotas de las casas.

## Instalación y uso

```
pip install -r requirements.txt
playwright install chromium      # solo para SofaScore (opcional)
```

Abrir: doble clic en `Abrir analizador.bat` (o `python app.py`).
Actualizar datos sin interfaz: `python scraper.py` (todas las ligas) o `python scraper.py 17 8`.

## Fuentes (gratuitas)

| Fuente | Qué trae | Peticiones |
|---|---|---|
| Understat (`understat.py`) | Resultados, xG, calendario, totales de jugadores por temporada | 1 por liga y temporada |
| ESPN (`espn.py`) | Faltas, amarillas, rojas, córners, tiros, posesión, atajadas, fueras de juego; alineaciones con minutos, faltas y tarjetas por jugador; árbitro | 1 por partido (solo los nuevos) |
| ESPN · Champions League | Calendario y resultados de la Champions (Understat no la cubre) y marcadores en vivo de todas las competiciones cada 2 min con la app abierta | 1 por competición |
| SofaScore (`sofa_client.py`) | Alineación probable y bajas antes del partido (botón en cada partido). Descarga masiva opcional: suele bloquear | — |

Los equipos se emparejan entre fuentes por nombre (`names.py`); si un partido está en dos
fuentes se cuenta una sola vez. Todo queda en `data/sofascore.db` y las actualizaciones son
incrementales. football-data.co.uk sería otra buena fuente, pero el DNS del proveedor actual la bloquea.

## Modelo (`model.py`)

Para cada estadística de conteo (goles, faltas, tarjetas, córners, tiros, fueras de juego, atajadas):

```
esperado_local = media_liga_local × produce_local × concede_visitante × árbitro × alineación × sesgo
```

- **produce / concede**: lo que el equipo genera y lo que le generan los rivales (ej. faltas que
  le hacen), respecto a la media de la liga; 50 % en su condición local/visitante y 50 % total;
  partidos recientes pesan más (vida media `hl`); con pocos partidos se acerca a la media (`k`).
- **Árbitro**: su media de faltas/amarillas frente a la de la liga, encogida por nº de partidos.
- **Alineación**: el XI elegido frente al XI habitual — producción ofensiva (xG+xA), rating, y
  faltas/amarillas por 90' de cada jugador.
- **Distribución**: binomial negativa (faltas y tarjetas varían más que una Poisson), con la
  dispersión medida en partidos reales. Goles: Poisson con corrección Dixon-Coles.
- **Calibración** (al terminar cada scraping): por estadística elige `hl` y `k` con menor error
  en los últimos 400 partidos (prediciendo cada uno solo con datos anteriores), y mide sesgo y dispersión.

**Validación del modelo** (pestaña propia): calibra con partidos antiguos y evalúa en los 250
más recientes que no vio. Muestra acierto 1X2, mejora frente a las frecuencias, y por mercado
cuántas veces acertó cuando daba ≥ 55/60/65 % a Más o Menos. Usa solo los mercados que salgan
en verde.

**Calculadora de valor** (pestaña «Mercados y valor» de cada partido): eliges mercado
(ej. «Faltas · Fiorentina»), línea (11.5) y la cuota de la casa (1.88). Devuelve la probabilidad
del modelo, la cuota justa (1 / probabilidad), el valor esperado y una fracción de Kelly ¼.
Hay valor cuando la cuota de la casa es mayor que la justa; con valores pequeños (< 4 %) el
error del modelo pesa más que la ventaja.

**Aporte de jugador**: `0.5·on/off + 0.8·(xG+xA)/90 + 0.4·(rating − medio)`; on/off es la
diferencia de gol del equipo con el jugador titular menos sin él, ajustada por tamaño de muestra.

**Resultados (historial)**: cada predicción se guarda antes del partido (`tracking.py`, tabla
`predictions`) y la sección «Resultados» la compara con lo que pasó: 1X2, más/menos 2.5, faltas,
amarillas y córners (esperado → real, ✔/✖). Los últimos 60 días se reconstruyen usando solo datos
anteriores a cada partido.

**Actualización automática**: con la app abierta, cada 5 min revisa los partidos que empiezan en
menos de 45 min y trae de ESPN el árbitro y la alineación confirmada; como mucho cada 3 h
actualiza también los datos de esa liga y recalibra.

**Prueba de interfaz**: `python tools/ui_smoke.py` recorre todas las vistas y guarda capturas en
`data/screens/`.

Es un modelo estadístico: no sabe de motivación, rotaciones, clima ni noticias de última hora.
