"""Emparejar nombres de equipos/jugadores entre fuentes (Understat ↔ SofaScore)."""
import re
import unicodedata
from difflib import SequenceMatcher

# palabras que no ayudan a distinguir equipos
STOP = {
    "fc", "cf", "afc", "ac", "as", "ssc", "sc", "sv", "vfb", "vfl", "tsg", "rc", "rcd", "cd", "ud", "sd",
    "ca", "club", "de", "calcio", "1", "fsv", "bsc", "olympique", "stade", "hotspur", "and", "albion",
    "us", "ogc", "losc", "aj", "sco", "1899", "1846", "1913", "1907", "04", "05", "09", "balompie",
}
# nombres que no se parecen lo suficiente por texto
ALIASES = {
    "rasenballsport leipzig": "rb leipzig",
    "fc cologne": "koln",
    "borussia m gladbach": "monchengladbach",
    "borussia m'gladbach": "monchengladbach",
    "borussia monchengladbach": "monchengladbach",
    "paris saint germain": "psg",
    "paris saint-germain": "psg",
    "wolverhampton wanderers": "wolves",
    "wolverhampton": "wolves",
    "athletic club": "athletic bilbao",
    "deportivo alaves": "alaves",
    "hertha berlin": "hertha",
    "hertha bsc": "hertha",
}


def norm(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.\-']", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = ALIASES.get(s, s)
    tokens = [t for t in s.split() if t not in STOP]
    return " ".join(tokens) or s


def similarity(a, b):
    na, nb = norm(a), norm(b)
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb and (ta <= tb or tb <= ta):
        return 0.9
    jacc = len(ta & tb) / len(ta | tb) if ta | tb else 0
    return max(SequenceMatcher(None, na, nb).ratio(), jacc)


def assign(left, right, threshold=0.55):
    """Empareja listas [(id, nombre)] de forma única, empezando por los pares más parecidos.

    Devuelve {id_left: id_right}.
    """
    pairs = sorted(((similarity(ln, rn), li, ri) for li, ln in left for ri, rn in right), reverse=True)
    used_l, used_r, out = set(), set(), {}
    for score, li, ri in pairs:
        if score < threshold:
            break
        if li in used_l or ri in used_r:
            continue
        out[li] = ri
        used_l.add(li)
        used_r.add(ri)
    return out


def best_match(name, candidates, threshold=0.75):
    """candidates: [(id, nombre)]. Devuelve el id más parecido o None."""
    best = max(((similarity(name, n), i) for i, n in candidates), default=(0, None))
    return best[1] if best[0] >= threshold else None
