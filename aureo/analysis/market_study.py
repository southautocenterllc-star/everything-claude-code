"""ÁUREO — estudio CONSTANTE del mercado del oro (XAU/USD), determinístico.

Andrés pidió: "quiero que Áureo estudie constantemente todo: cómo se piensa mover
el mercado, qué pasa en el mundo, todo lo que pueda afectar el oro". Esto es
APOYO DISCRECIONAL: Andrés opera a mano. NO da órdenes de compra/venta (el
research mató el edge automático); da CONTEXTO con DATOS REALES.

Sin Claude en el bucle (regla CRÍTICA anti-BENDER). 100% reglas. Combina 4 capas:

  Capa 1 — Macro cuantitativo (la columna confiable):
    dirección reciente (~3-4d) de DXY, tasas 10Y, VIX y petróleo → macro_score y
    sesgo (mismas reglas de signo que daily_brief: dólar débil + tasas bajando =
    a favor del oro). Cada símbolo que falle se salta, no tumba la corrida.

  Capa 2 — Noticias del mundo (RSS público, stdlib urllib + ElementTree):
    pulla varios feeds financieros, filtra titulares relevantes al oro por
    keywords y los clasifica por tono (alcista/bajista para el oro). El balance
    de tono contribuye al sesgo. Feeds que den 403/timeout se saltan.

  Capa 3 — Eventos próximos (analysis/patterns/events.csv):
    marca blackout si hay evento high-impact inminente (alerta especial).

  Capa 4 — Geopolítica de PRIMER NIVEL (safe-haven del oro):
    sobre TODOS los feeds (financieros + mundiales: Al Jazeera/BBC/France24/DW/
    Guardian), filtra titulares geopolíticos, mide escalada vs distensión y deriva
    un risk_level (BAJO/MEDIO/ALTO/CRÍTICO) y un geo_score para el oro (sube con el
    miedo geopolítico). Driver destacado + alerta propia si el riesgo sube o hay
    escalada fuerte fresca.

Salida: un "outlook" combinado (ALCISTA / BAJISTA / NEUTRAL para el oro), drivers,
top titulares, próximo evento, precio actual del oro. Persiste cada corrida en
logs/market_study.jsonl (append-only) y el último estado legible en
research/output/MARKET_STUDY.md.

Alertas Telegram (anti-spam): SOLO cuando (a) el sesgo cambió respecto a la última
corrida, (b) hay evento high-impact inminente (blackout), o (c) aparece un titular
de impacto crítico. Si no, solo loguea. Por defecto NO envía (modo preview).

Uso:
    .venv/bin/python -m analysis.market_study --once             # preview (no manda)
    .venv/bin/python -m analysis.market_study --once --send      # corrida real (puede postear)
    .venv/bin/python -m analysis.market_study --daemon --send    # loop cada 4h
    .venv/bin/python -m analysis.market_study --once --send --force-alert  # test envío
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from research.macro_features import fetch_yahoo_daily

EVENTS_CSV = ROOT / "analysis" / "patterns" / "events.csv"
JSONL = ROOT / "logs" / "market_study.jsonl"
MD_OUT = ROOT / "research" / "output" / "MARKET_STUDY.md"

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HTTP_TIMEOUT = 12  # segundos por request (todo request tiene timeout)

# Feeds RSS públicos candidatos. Se prueban todos; los que den 403/404/timeout se
# saltan (robusto). Probados 2026-06-11: marketwatch/yahoo/cnbc/fxstreet/forexlive
# responden 200; investing/kitco daban 404 → fuera.
FEEDS = [
    ("MarketWatch", "http://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("CNBC Finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
    ("CNBC Economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("FXStreet", "https://www.fxstreet.com/rss/news"),
    ("ForexLive", "https://www.forexlive.com/feed/news"),
]

# Feeds de GEOPOLÍTICA / MUNDO (no solo finanzas). El oro es activo refugio:
# guerras/tensiones/sanciones lo mueven fuerte (safe-haven bid), así que la capa
# geopolítica necesita ver noticias mundiales, no solo financieras. Mismo patrón
# robusto: cada feed que dé 403/404/timeout se salta. Probados 2026-06-11: los
# cinco responden 200 (Al Jazeera, BBC World, France24 EN, DW World, Guardian World).
WORLD_FEEDS = [
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("France24", "https://www.france24.com/en/rss"),
    ("DW World", "https://rss.dw.com/rdf/rss-en-world"),
    ("Guardian World", "https://www.theguardian.com/world/rss"),
]

# Titular relevante al oro si menciona alguna de estas (case-insensitive).
GOLD_KEYWORDS = [
    "gold", "bullion", "xau", "fed", "fomc", "powell", "rate cut", "rate hike",
    "inflation", "cpi", "pce", "nfp", "payrolls", "jobs", "yield", "treasury",
    "dollar", "dxy", "war", "iran", "israel", "ukraine", "china", "geopolit",
    "recession", "safe haven", "tariff",
]

# Tono del titular PARA EL ORO.
BULLISH_GOLD = [
    "rate cut", "dovish", "weak dollar", "dollar falls", "dollar slips",
    "falling yields", "yields fall", "yields drop", "inflation rising",
    "inflation jumps", "hot inflation", "war", "escalation", "safe haven",
    "recession", "stimulus", "rate cuts",
]
BEARISH_GOLD = [
    "rate hike", "hawkish", "strong dollar", "dollar rises", "dollar surges",
    "rising yields", "yields rise", "yields climb", "risk-on", "rally stocks",
    "stocks rally", "hot economy", "tapering", "taper",
]

# Titular de impacto CRÍTICO → dispara alerta aunque el sesgo no cambie.
CRITICAL_KEYWORDS = ["rate cut", "rate hike", "war", "cpi", "fomc decision",
                     "fed decision", "fed cuts", "fed hikes"]

# "war" económica/comercial NO es guerra geopolítica de safe-haven: un titular de
# "price war" / "trade war" no debe disparar la alerta crítica de guerra real
# (bug 2026-06-11: "ChatGPT price-war report" se clasificaba como guerra). Estas
# colocaciones se EXCLUYEN del match de "war" como driver geopolítico; la guerra
# real (Iran war, war escalation, etc.) SÍ sigue contando.
WAR_NON_GEOPOLITICAL = ["price war", "bidding war", "culture war", "turf war",
                        "trade war", "tariff war", "bidding wars", "price wars",
                        "trade wars"]


# --------------------------------------------------------------------------- #
# Léxico GEOPOLÍTICO (capa de primer nivel). El oro sube con miedo geopolítico
# (safe-haven bid): guerra/invasión/misiles/sanciones lo empujan al alza; tregua/
# diplomacia/cese al fuego le restan ese refugio. Todo se matchea con word-boundary
# (`_has_kw`) sobre titulares en minúscula.
# --------------------------------------------------------------------------- #

# Actores/regiones CALIENTES (safe-haven del oro). La presencia de uno de estos
# es lo que convierte un titular en geopolítica que SÍ mueve el riesgo refugio.
GEO_HOT_ACTORS = [
    "iran", "iranian", "israel", "israeli", "gaza", "hamas", "hezbollah",
    "houthi", "houthis", "russia", "russian", "ukraine", "ukrainian", "china",
    "chinese", "taiwan", "taiwanese", "north korea", "north korean",
    "south china sea", "middle east", "venezuela", "venezuelan", "syria",
    "syrian", "lebanon", "lebanese", "red sea", "strait of hormuz", "hormuz",
    "yemen", "yemeni", "sudan", "sudanese", "pyongyang", "kremlin", "tehran",
    "moscow", "beijing", "kyiv", "kiev", "gulf of aden", "west bank",
]

# Términos de conflicto REAL, divididos en dos niveles para el GATING de hot
# (corrección falso-positivo financiero 2026-06-11). Motivo: los feeds FINANCIEROS
# traen metáforas de combate ("Fed attacks inflation", "SEC offensive against
# fraud", "Boardroom coup", "siege to AGM") que con términos genéricos sueltos
# disparaban hot=True e inflaban el riesgo. Solución: solo los términos
# INEQUÍVOCOS (que casi nunca son metáfora financiera) disparan hot por sí solos;
# los GENÉRICOS exigen co-ocurrencia de un actor caliente (GEO_HOT_ACTORS).
#
# Inequívocos: cuentan hot SOLOS (missile, airstrike, invasion, nuclear, warship,
# blockade, "war crimes", ceasefire, mobiliz, "troops deployed", ...). OJO:
# "war"/"warfare" NO van aquí (validador 2026-06-11): tienen demasiado uso
# metafórico financiero ("talent war", "streaming wars", "console war") y se
# movieron a GENERIC. Un "war" geopolítico real casi siempre trae actor caliente
# ("Russia's war in Ukraine") → sigue contando hot POR EL ACTOR.
GEO_HARD_CONFLICT_UNAMBIGUOUS = [
    "missile", "missiles", "drone", "drones", "airstrike",
    "airstrikes", "air strike", "air strikes", "drone strike", "drone strikes",
    "invasion", "invade", "invades", "invaded", "nuclear", "warship", "warships",
    "blockade", "naval", "embargo", "sanctions", "ceasefire", "truce", "armistice",
    "mobiliz", "troops deployed", "annex", "annexation", "incursion", "shelling",
    "shells", "bombard", "genocide", "war crimes", "geopolit",
]

# Genéricos: cuentan hot SOLO si co-ocurre un actor/región caliente
# (GEO_HOT_ACTORS). Solos NO bastan, porque viven también en jerga financiera
# ("attack inflation", "coup ousts CEO", "siege to AGM", "offensive against fraud").
GEO_HARD_CONFLICT_GENERIC = [
    "war", "warfare",
    "attack", "attacks", "attacked", "assault", "raid", "raids", "offensive",
    "siege", "coup", "military", "troops", "bombing", "bombs", "strike on",
    "strikes on", "clash", "clashes", "militant", "militants", "insurgent",
    "insurgents", "rebels", "rebel", "conflict", "hostages", "terrorist",
    "terrorism",
]

# Unión: lista AMPLIA de conflicto real usada para (a) el filtro de contexto
# GEOPOLITICAL_KEYWORDS y (b) la detección de "sobra conflicto real" en
# _is_economic_war_only. El GATING de hot (_is_hot_geo) usa solo los inequívocos.
# "war"/"strike" se filtran aparte (geopolítico vs económico/laboral) en _has_kw.
GEO_HARD_CONFLICT = GEO_HARD_CONFLICT_UNAMBIGUOUS + GEO_HARD_CONFLICT_GENERIC + [
    "strike",
]

# Actores/regiones calientes + términos genéricos de conflicto. Un titular es
# "geopolíticamente relevante" (amplio, para mostrar contexto) si matchea AL
# MENOS una de estas (vía _has_kw, que reusa la lógica de "war"/"strike"
# geopolítico vs económico/laboral). OJO: esto es el filtro AMPLIO; el conteo
# que mueve el risk_level usa _is_hot_geo (actor caliente O conflicto real), no
# este conjunto, para que ruido económico/laboral residual no infle el riesgo.
GEOPOLITICAL_KEYWORDS = GEO_HOT_ACTORS + GEO_HARD_CONFLICT + [
    # términos amplios que dan CONTEXTO pero NO bastan solos para mover el riesgo
    # (sin actor caliente ni conflicto duro): pueden venir de economía/política.
    "escalation", "escalate", "escalates", "tension", "tensions", "mobiliz",
    "peace talks", "sanction",
]

# Términos de ESCALADA → sube el riesgo → ALCISTA oro (safe-haven bid sube).
GEO_ESCALATION = [
    "invasion", "invade", "invades", "invaded", "missile", "missiles",
    "airstrike", "airstrikes", "air strike", "air strikes", "drone strike",
    "drone strikes", "attack", "attacks", "attacked", "escalation", "escalate",
    "escalates", "killed", "kills", "nuclear", "mobiliz", "sanctions imposed",
    "imposes sanctions", "warship", "warships", "blockade", "strike on",
    "strikes on", "bombing", "bombs", "bombard", "offensive", "shelling",
    "shells", "incursion", "assault", "raid", "raids", "seizes", "storms",
]

# Términos de DISTENSIÓN → baja el riesgo → resta safe-haven → BAJISTA oro.
GEO_DEESCALATION = [
    "ceasefire", "truce", "peace talks", "peace deal", "peace agreement",
    "de-escalation", "deescalation", "withdrawal", "withdraws", "withdraw",
    "deal reached", "diplomatic", "diplomacy", "talks resume", "talks resumed",
    "agreement reached", "peace plan", "negotiations", "negotiate", "detente",
    "normalization", "normalisation", "pullout", "pull out", "stand down",
]

# Colocaciones de "strike" que NO son guerra (huelga laboral, etc.). Mismo truco
# de enmascarado que WAR_NON_GEOPOLITICAL: si tras quitarlas no queda "strike"
# libre, el "strike" del titular era laboral y no cuenta como geopolítico.
STRIKE_NON_GEOPOLITICAL = [
    "labor strike", "labour strike", "workers strike", "worker strike",
    "workers' strike", "hunger strike", "general strike", "rail strike",
    "rail workers strike", "pilots strike", "pilot strike", "teachers strike",
    "teacher strike", "nurses strike", "nurse strike", "doctors strike",
    "doctor strike", "union strike", "strike action", "strike ballot",
    "strike threat", "strike vote", "staff strike", "transit strike",
    "lightning strike", "lucky strike",
    # colocaciones laborales que faltaban (bug 2026-06-11: "Hollywood writers
    # strike" se colaba como geopolítico e inflaba el riesgo del oro).
    "writers strike", "writer strike", "writers' strike",
    "actors strike", "actor strike", "actors' strike",
    "baggage handlers strike", "baggage handler strike",
    "dockers strike", "docker strike", "dock workers strike",
    "dock worker strike", "dockworkers strike",
    "auto workers strike", "autoworkers strike", "factory workers strike",
    "postal workers strike", "delivery workers strike", "port workers strike",
    "train drivers strike", "bus drivers strike", "junior doctors strike",
    "civil servants strike", "miners strike", "students strike", "tube strike",
]


@lru_cache(maxsize=512)
def _kw_re(kw: str) -> "re.Pattern[str]":
    """Match con límites de palabra → evita falsos positivos por substring
    ('war' dentro de 'hardware', 'jobs' dentro de algo, etc.). kw ya en minúscula."""
    return re.compile(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])")


def _war_is_geopolitical(text_lc: str) -> bool:
    """¿El 'war' del titular es guerra geopolítica (safe-haven) y no económica?

    Devuelve True solo si aparece "war" como palabra Y NO es una colocación
    económica/comercial (price/trade/tariff/culture/bidding/turf war). Se neutraliza
    cada colocación enmascarándola antes de buscar el "war" libre restante, así un
    titular con AMBOS ("trade war risk as Iran war escalates") sigue contando por el
    "war" geopolítico real."""
    if not _kw_re("war").search(text_lc):
        return False
    # Normalizá separadores (guion/underscore) a espacio para que "price-war" y
    # "price_war" cuenten como la colocación "price war" al enmascarar.
    masked = re.sub(r"[-_]+", " ", text_lc)
    for phrase in WAR_NON_GEOPOLITICAL:
        masked = _kw_re(phrase).sub(" ", masked)
    return bool(_kw_re("war").search(masked))


def _strike_is_geopolitical(text_lc: str) -> bool:
    """¿El 'strike' del titular es militar (airstrike/strike on X) y no laboral?

    Mismo enmascarado que _war_is_geopolitical: se neutralizan las colocaciones
    laborales (labor/hunger/rail/general strike...) y se mira si queda un "strike"
    libre. Así "Israel strikes Iran" cuenta y "rail workers strike" no. OJO:
    "airstrike"/"airstrikes" (una sola palabra, sin espacio) NO matchea _kw_re('strike')
    por word-boundary, por eso airstrike se lista aparte como keyword propia."""
    if not _kw_re("strike").search(text_lc):
        return False
    masked = re.sub(r"[-_]+", " ", text_lc)
    for phrase in STRIKE_NON_GEOPOLITICAL:
        masked = _kw_re(phrase).sub(" ", masked)
    return bool(_kw_re("strike").search(masked))


def _has_kw(text_lc: str, keywords: list[str]) -> bool:
    """True si el texto matchea alguna keyword (word-boundary). 'war' se trata
    aparte: solo cuenta si es guerra geopolítica, no económica (price/trade war).
    'strike' también se trata aparte: solo cuenta si es militar, no laboral."""
    for k in keywords:
        if k == "war":
            if _war_is_geopolitical(text_lc):
                return True
            continue
        if k == "strike":
            if _strike_is_geopolitical(text_lc):
                return True
            continue
        if _kw_re(k).search(text_lc):
            return True
    return False


# Colocaciones de "war" ECONÓMICAS/comerciales (no safe-haven). Subconjunto de
# WAR_NON_GEOPOLITICAL que sí es "guerra" económica (trade/tariff/price war). Un
# titular cuya ÚNICA señal de conflicto es una de estas (aunque mencione a un
# actor caliente, p.ej. "China trade war") NO debe inflar el riesgo: queda como
# CONTEXTO (geo_relevant amplio) pero class=neutro y fuera del conteo del nivel.
ECONOMIC_WAR_PHRASES = ["price war", "trade war", "tariff war", "bidding war",
                        "price wars", "trade wars", "tariff wars", "bidding wars",
                        "currency war", "currency wars"]


def _is_economic_war_only(text_lc: str) -> bool:
    """True si el titular es 'guerra' SOLO económica (trade/tariff/price/currency
    war) sin conflicto geopolítico real. Se enmascaran las colocaciones económicas
    y se mira si queda algún término de conflicto DURO (missile/airstrike/troops/
    invasion/...) o un 'war'/'strike' geopolítico libre. Si no queda nada, el único
    'evento' del titular era económico → no cuenta para el riesgo (es contexto)."""
    norm = re.sub(r"[-_]+", " ", text_lc)
    has_econ_war = any(_kw_re(p).search(norm) for p in ECONOMIC_WAR_PHRASES)
    if not has_econ_war:
        return False
    masked = norm
    for p in ECONOMIC_WAR_PHRASES:
        masked = _kw_re(p).sub(" ", masked)
    # ¿Sobra conflicto geopolítico real tras quitar la guerra económica?
    if _has_kw(masked, GEO_HARD_CONFLICT):
        return False
    return True


def _is_hot_geo(text_lc: str) -> bool:
    """¿El titular es geopolítica CALIENTE que cuenta para el risk_level?

    Corrección de raíz (validador 2026-06-11): los umbrales de conteo del riesgo
    deben contar SOLO titulares con un actor/región caliente (iran, israel, gaza,
    russia, ukraine, china, taiwan, red sea, hormuz, ...) O un término de conflicto
    INEQUÍVOCO (war geopolítica, missile, airstrike, invasion, drone strike,
    nuclear, blockade, warship, ceasefire, mobiliz, troops deployed, ...). NO los
    que quedaron 'geo_relevant' por una colocación económica/laboral residual
    (p.ej. 'tariff war escalates' sin conflicto real — su 'war' ya no es
    geopolítico y 'escalation' sola no basta; o un 'strike' laboral que sobrevivió).

    Falso positivo financiero (validador 2026-06-11): los términos de conflicto
    GENÉRICOS (attack, assault, raid, offensive, siege, coup, military, bombing,
    troops, 'strike on', clash, militant) ya NO disparan hot por sí solos, porque
    los feeds financieros los usan como metáfora ('Fed attacks inflation', 'SEC
    offensive against fraud', 'Boardroom coup ousts CEO', 'Activists lay siege to
    AGM'). Solo cuentan hot cuando co-ocurre un actor caliente — y en ese caso
    ya cuentan POR el actor, así que basta el gate de actor O inequívoco:
        hot = (actor caliente presente) OR (término inequívoco presente)
    Los genéricos siguen pudiendo definir el TONO (escalada/distensión) una vez
    que el titular YA es hot; lo que cambia es el GATING de hot, no el tono.

    Y una 'guerra' SOLO económica con actor caliente ('China trade war') tampoco
    cuenta: queda como contexto. Así el ruido neutro económico/laboral/financiero
    no infla ALTO/CRÍTICO."""
    if _is_economic_war_only(text_lc):
        return False
    return (_has_kw(text_lc, GEO_HOT_ACTORS)
            or _has_kw(text_lc, GEO_HARD_CONFLICT_UNAMBIGUOUS))


# --------------------------------------------------------------------------- #
# Capa 1 — Macro cuantitativo
# --------------------------------------------------------------------------- #
def macro_layer() -> dict:
    """Dirección reciente (~3-4d) de DXY/10Y (+VIX/petróleo si responden) →
    macro_score y sesgo. Mismas reglas de signo que daily_brief: dólar débil +
    tasas bajando = a favor del oro. Cada símbolo que falle se salta."""
    out = {"score": 0, "bias": "NEUTRAL", "drivers": [], "details": {}}

    def _safe(sym: str):
        try:
            return fetch_yahoo_daily(sym).tail(4)
        except Exception as e:  # noqa: BLE001
            out["drivers"].append(f"{sym} no disponible ({str(e)[:30]})")
            return None

    score = 0

    dxy = _safe("DX-Y.NYB")
    if dxy is not None and len(dxy) >= 2:
        d_dxy = float(dxy.iloc[-1] / dxy.iloc[-2] - 1) * 100
        out["details"]["dxy_pct"] = round(d_dxy, 3)
        # dólar débil = a favor del oro (+1); fuerte = en contra (-1)
        s = 1 if d_dxy < -0.05 else -1 if d_dxy > 0.05 else 0
        score += s
        out["drivers"].append(
            f"Dólar (DXY) {'débil ↓' if s > 0 else 'fuerte ↑' if s < 0 else 'plano'} "
            f"({d_dxy:+.2f}%) → {'a favor' if s > 0 else 'en contra' if s < 0 else 'neutro'} del oro")

    y10 = _safe("^TNX")
    if y10 is not None and len(y10) >= 2:
        d_y10 = float(y10.iloc[-1] - y10.iloc[-2])
        out["details"]["y10_diff"] = round(d_y10, 3)
        # tasas bajando = a favor del oro (+1)
        s = 1 if d_y10 < -0.02 else -1 if d_y10 > 0.02 else 0
        score += s
        out["drivers"].append(
            f"Tasas 10Y {'bajando ↓' if s > 0 else 'subiendo ↑' if s < 0 else 'planas'} "
            f"({d_y10:+.2f}pp) → {'a favor' if s > 0 else 'en contra' if s < 0 else 'neutro'} del oro")

    vix = _safe("^VIX")
    if vix is not None and len(vix) >= 2:
        d_vix = float(vix.iloc[-1] / vix.iloc[-2] - 1) * 100
        out["details"]["vix_pct"] = round(d_vix, 3)
        # miedo subiendo (VIX↑) = safe haven = a favor del oro (umbral más alto, es ruidoso)
        s = 1 if d_vix > 5 else -1 if d_vix < -5 else 0
        score += s
        if s:
            out["drivers"].append(
                f"Miedo (VIX) {'subiendo ↑' if s > 0 else 'bajando ↓'} "
                f"({d_vix:+.1f}%) → {'a favor' if s > 0 else 'en contra'} del oro")

    oil = _safe("CL=F")
    if oil is not None and len(oil) >= 2:
        d_oil = float(oil.iloc[-1] / oil.iloc[-2] - 1) * 100
        out["details"]["oil_pct"] = round(d_oil, 3)
        # petróleo↑ = presión inflacionaria leve = a favor del oro (peso suave)
        s = 1 if d_oil > 3 else -1 if d_oil < -3 else 0
        score += s
        if s:
            out["drivers"].append(
                f"Petróleo {'subiendo ↑' if s > 0 else 'bajando ↓'} "
                f"({d_oil:+.1f}%) → inflación {'+' if s > 0 else '-'} (peso suave)")

    out["score"] = score
    out["bias"] = "ALCISTA" if score > 0 else "BAJISTA" if score < 0 else "NEUTRAL"
    return out


# --------------------------------------------------------------------------- #
# Capa 2 — Noticias (RSS, stdlib)
# --------------------------------------------------------------------------- #
def _fetch_feed(url: str) -> list[tuple[str, str]]:
    """Devuelve [(title, link), ...] de un feed RSS/Atom. Timeout en el request;
    cualquier fallo (403/404/timeout/XML roto) → lista vacía (robusto)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    data = urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT).read()
    root = ET.fromstring(data)
    out: list[tuple[str, str]] = []
    items = root.findall(".//item")
    if items:
        for it in items:
            t = it.findtext("title") or ""
            l = it.findtext("link") or ""
            out.append((t.strip(), l.strip()))
    else:  # Atom fallback
        atom = "{http://www.w3.org/2005/Atom}"
        for it in root.findall(f".//{atom}entry"):
            t = it.findtext(f"{atom}title") or ""
            le = it.find(f"{atom}link")
            l = le.get("href") if le is not None else ""
            out.append((t.strip(), (l or "").strip()))
    return out


def _tone(title_lc: str) -> str:
    """Tono del titular PARA EL ORO según keywords. Si ambos o ninguno → neutro."""
    bull = _has_kw(title_lc, BULLISH_GOLD)
    bear = _has_kw(title_lc, BEARISH_GOLD)
    if bull and not bear:
        return "alcista"
    if bear and not bull:
        return "bajista"
    return "neutro"


def fetch_all_feeds(feeds: list[tuple[str, str]]) -> tuple[list[dict], list[str], list[str]]:
    """Baja TODOS los feeds dados UNA sola vez y devuelve (items, feeds_ok, feeds_fail).
    Cada item es {'title','link','source'}. Compartido por news_layer y
    geopolitics_layer para no duplicar descargas. Feed caído se salta (robusto)."""
    socket.setdefaulttimeout(_HTTP_TIMEOUT)
    items: list[dict] = []
    feeds_ok, feeds_fail = [], []
    for source, url in feeds:
        try:
            raw = _fetch_feed(url)
            feeds_ok.append(source)
        except Exception as e:  # noqa: BLE001 — feed muerto no tumba la corrida
            feeds_fail.append(f"{source} ({type(e).__name__})")
            continue
        for title, link in raw:
            items.append({"title": title, "link": link, "source": source})
    return items, feeds_ok, feeds_fail


def news_layer(items: list[dict] | None = None) -> dict:
    """Filtra titulares relevantes al oro y los clasifica por tono. Dedup por título.
    Devuelve conteos por tono, top ~5 titulares y feeds vivos. Si recibe `items`
    (ya bajados por fetch_all_feeds) los reusa; si no, baja solo FEEDS (compat)."""
    if items is None:
        items, feeds_ok, feeds_fail = fetch_all_feeds(FEEDS)
    else:
        feeds_ok, feeds_fail = None, None  # los aporta el caller que ya bajó todo

    seen: set[str] = set()
    relevant: list[dict] = []
    for it in items:
        title, link, source = it["title"], it["link"], it["source"]
        if not title:
            continue
        lc = title.lower()
        if not _has_kw(lc, GOLD_KEYWORDS):
            continue
        key = lc[:80]
        if key in seen:
            continue
        seen.add(key)
        tone = _tone(lc)
        critical = _has_kw(lc, CRITICAL_KEYWORDS)
        # mencionar oro explícito o ser crítico sube prioridad de ranking
        prio = (2 if _has_kw(lc, ["gold", "bullion", "xau"]) else 0) \
            + (1 if tone != "neutro" else 0) + (2 if critical else 0)
        relevant.append({"title": title, "link": link, "source": source,
                         "tone": tone, "critical": critical, "_prio": prio})

    counts = {"alcista": 0, "bajista": 0, "neutro": 0}
    for r in relevant:
        counts[r["tone"]] += 1

    relevant.sort(key=lambda r: r["_prio"], reverse=True)
    top = [{k: r[k] for k in ("title", "link", "source", "tone", "critical")}
           for r in relevant[:5]]
    critical_hits = [r["title"] for r in relevant if r["critical"]][:5]

    # score de tono para el oro: alcistas - bajistas (clamp a ±2 para no dominar el macro)
    raw = counts["alcista"] - counts["bajista"]
    tone_score = max(-2, min(2, raw))
    out = {"counts": counts, "tone_score": tone_score, "n_relevant": len(relevant),
           "top": top, "critical_hits": critical_hits}
    if feeds_ok is not None:
        out["feeds_ok"], out["feeds_fail"] = feeds_ok, feeds_fail
    return out


# --------------------------------------------------------------------------- #
# Capa GEOPOLÍTICA — primer nivel (safe-haven del oro)
# --------------------------------------------------------------------------- #
def _geo_classify(title_lc: str) -> str:
    """Clasifica un titular geopolítico: 'escalada' / 'distension' / 'neutro'.
    Si matchea ambos (raro), gana escalada (el miedo manda en el safe-haven)."""
    esc = _has_kw(title_lc, GEO_ESCALATION)
    deesc = _has_kw(title_lc, GEO_DEESCALATION)
    if esc:
        return "escalada"
    if deesc:
        return "distension"
    return "neutro"


# Escalada FUERTE (en región/actor caliente) → un solo titular ya justifica CRÍTICO.
_GEO_STRONG_ESCALATION = ["invasion", "invade", "invades", "invaded", "missile",
                          "missiles", "airstrike", "airstrikes", "air strike",
                          "air strikes", "drone strike", "drone strikes",
                          "nuclear", "warship", "warships", "blockade", "bombing",
                          "offensive", "strike on", "strikes on", "incursion"]


def geopolitics_layer(items: list[dict]) -> dict:
    """Capa de PRIMER NIVEL. Sobre los titulares de TODOS los feeds ya bajados
    (financieros + mundiales), filtra los geopolíticamente relevantes y mide el
    riesgo refugio del oro. 100% determinístico.

    Conteo del riesgo (corrección de raíz 2026-06-11): se separa el "geo_relevant
    AMPLIO" (n_geo, para mostrar contexto) del "geo CALIENTE que cuenta para el
    nivel" (n_hot = titulares con actor/región caliente O conflicto real, vía
    _is_hot_geo). Los UMBRALES de riesgo usan n_hot, NO n_geo, así un titular que
    quedó geo_relevant solo por una colocación económica/laboral residual
    ('tariff war escalates' sin actor, un 'strike' laboral que sobrevivió) NO
    infla ALTO/CRÍTICO. La escalada/distensión también cuentan solo sobre hot-geo.

    Riesgo (umbrales documentados, sobre titulares CALIENTES):
      - CRÍTICO: ≥1 titular de escalada FUERTE (invasión/misil/airstrike/nuclear...)
                 en el flujo, O ≥8 titulares geo CALIENTES con escalada neta dominante.
      - ALTO:    escalada neta clara (esc - deesc ≥ 2) o ≥5 titulares geo CALIENTES.
      - MEDIO:   hay actividad geo CALIENTE (≥2 titulares) sin escalada fuerte.
      - BAJO:    poco o nada (<2 titulares geo calientes relevantes).

    geo_score para el oro (el oro sube con miedo): CRÍTICO→+2, ALTO→+1,
    distensión neta→-1, equilibrado/bajo→0."""
    seen: set[str] = set()
    geo: list[dict] = []
    for it in items:
        title, link, source = it["title"], it["link"], it["source"]
        if not title:
            continue
        lc = title.lower()
        if not _has_kw(lc, GEOPOLITICAL_KEYWORDS):
            continue
        key = lc[:80]
        if key in seen:
            continue
        seen.add(key)
        hot = _is_hot_geo(lc)
        cls = _geo_classify(lc) if hot else "neutro"  # solo lo caliente clasifica
        strong = hot and _has_kw(lc, _GEO_STRONG_ESCALATION)
        prio = (2 if strong else 0) + (1 if cls == "escalada" else 0) + (1 if hot else 0)
        geo.append({"title": title, "link": link, "source": source,
                    "class": cls, "strong": strong, "hot": hot, "_prio": prio})

    n = len(geo)                                    # geo_relevant amplio (contexto)
    n_hot = sum(1 for g in geo if g["hot"])         # cuenta para el risk_level
    esc = sum(1 for g in geo if g["class"] == "escalada")     # ya solo hot (cls)
    deesc = sum(1 for g in geo if g["class"] == "distension")
    strong_n = sum(1 for g in geo if g["strong"])
    net = esc - deesc

    if strong_n >= 1 or (n_hot >= 8 and net >= 2):
        risk_level = "CRÍTICO"
    elif net >= 2 or n_hot >= 5:
        risk_level = "ALTO"
    elif n_hot >= 2:
        risk_level = "MEDIO"
    else:
        risk_level = "BAJO"

    # geo_score: el oro sube con miedo geopolítico.
    if risk_level == "CRÍTICO":
        geo_score = 2
    elif risk_level == "ALTO":
        geo_score = 1
    elif deesc > esc and deesc >= 1:
        geo_score = -1  # distensión neta resta safe-haven
    else:
        geo_score = 0

    geo_bias = "ALCISTA" if geo_score > 0 else "BAJISTA" if geo_score < 0 else "NEUTRAL"

    geo.sort(key=lambda g: g["_prio"], reverse=True)
    geo_headlines = [{k: g[k] for k in ("title", "link", "source", "class", "strong")}
                     for g in geo[:5]]
    # titulares geopolíticos de escalada FUERTE frescos → para la alerta crítica.
    geo_critical_hits = [g["title"] for g in geo if g["strong"]][:5]

    return {"risk_level": risk_level, "geo_score": geo_score, "bias": geo_bias,
            "n_geo": n, "n_hot": n_hot,
            "escalation_count": esc, "deescalation_count": deesc,
            "geo_headlines": geo_headlines, "geo_critical_hits": geo_critical_hits}


# --------------------------------------------------------------------------- #
# Capa 3 — Eventos
# --------------------------------------------------------------------------- #
def events_layer() -> dict:
    """Próximo evento high-impact del calendario. blackout = evento de alta
    importancia HOY (events.csv solo tiene granularidad de día; el "<2h" real
    requeriría hora — acá se aproxima a 'evento fuerte hoy')."""
    out = {"next_event": None, "next_when": None, "next_date": None, "blackout": False}
    try:
        ev = pd.read_csv(EVENTS_CSV, parse_dates=["date"])
        today = pd.Timestamp.now().normalize()
        fut = ev[ev["date"] >= today].sort_values("date")
        if fut.empty:
            return out
        row = fut.iloc[0]
        dias = int((row["date"].normalize() - today).days)
        cuando = "HOY" if dias == 0 else "mañana" if dias == 1 else f"en {dias} días"
        high = str(row.get("importance", "")).strip().lower() == "alta"
        out.update({"next_event": str(row["event"]), "next_when": cuando,
                    "next_date": str(row["date"].date()),
                    "blackout": bool(high and dias == 0)})
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:60]
    return out


# --------------------------------------------------------------------------- #
# Precio del oro
# --------------------------------------------------------------------------- #
def gold_price() -> tuple[float | None, str]:
    try:
        from core.data_feed import get_h1
        df, provider = get_h1(lookback=5)
        return float(df["close"].iloc[-1]), provider
    except Exception as e:  # noqa: BLE001
        return None, f"n/d ({str(e)[:30]})"


# --------------------------------------------------------------------------- #
# Outlook combinado
# --------------------------------------------------------------------------- #
_GEO_RISK_EMOJI = {"BAJO": "🟢", "MEDIO": "🟡", "ALTO": "🔴", "CRÍTICO": "🚨"}
# orden para comparar si el riesgo SUBIÓ entre corridas (alerta).
_GEO_RISK_ORDER = {"BAJO": 0, "MEDIO": 1, "ALTO": 2, "CRÍTICO": 3}


def build_outlook() -> dict:
    macro = macro_layer()
    # Baja TODOS los feeds (financieros + mundiales) UNA vez y los comparte entre
    # la capa de noticias y la geopolítica (no duplica descargas).
    items, feeds_ok, feeds_fail = fetch_all_feeds(FEEDS + WORLD_FEEDS)
    news = news_layer(items)
    geo = geopolitics_layer(items)
    events = events_layer()
    price, provider = gold_price()

    combined_score = macro["score"] + news["tone_score"] + geo["geo_score"]
    bias = "ALCISTA" if combined_score > 0 else "BAJISTA" if combined_score < 0 else "NEUTRAL"

    drivers = list(macro["drivers"])
    c = news["counts"]
    # Driver geopolítico DESTACADO (primer nivel) — va arriba de los titulares oro.
    geo_dir = ("oro al alza por refugio" if geo["geo_score"] > 0
               else "resta refugio al oro" if geo["geo_score"] < 0
               else "sin sesgo refugio")
    drivers.append(
        f"🌍 Riesgo geopolítico: {geo['risk_level']} {_GEO_RISK_EMOJI[geo['risk_level']]} "
        f"({geo_dir}) — {geo['escalation_count']} escalada / "
        f"{geo['deescalation_count']} distensión de {geo['n_geo']} titulares geo")
    drivers.append(f"Titulares oro: {c['alcista']} alcistas / {c['bajista']} bajistas / "
                   f"{c['neutro']} neutros (de {news['n_relevant']} relevantes)")
    if events["blackout"]:
        drivers.append(f"⚠️ BLACKOUT: {events['next_event']} HOY (evento high-impact)")

    return {
        "ts": pd.Timestamp.now(tz="UTC").isoformat(),
        "price": round(price, 2) if price is not None else None,
        "provider": provider,
        "macro_score": macro["score"],
        "tone_score": news["tone_score"],
        "geo_score": geo["geo_score"],
        "combined_score": combined_score,
        "bias": bias,
        "macro_bias": macro["bias"],
        "news_counts": news["counts"],
        "risk_level": geo["risk_level"],
        "geo_bias": geo["bias"],
        "geo_escalation_count": geo["escalation_count"],
        "geo_deescalation_count": geo["deescalation_count"],
        "geo_n": geo["n_geo"],
        "geo_headlines": geo["geo_headlines"],
        "geo_critical_hits": geo["geo_critical_hits"],
        "blackout": events["blackout"],
        "next_event": events["next_event"],
        "next_when": events["next_when"],
        "next_date": events["next_date"],
        "drivers": drivers,
        "top_titulares": news["top"],
        "critical_hits": news["critical_hits"],
        "feeds_ok": feeds_ok,
        "feeds_fail": feeds_fail,
    }


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
_BIAS_EMOJI = {"ALCISTA": "📈 ALCISTA", "BAJISTA": "📉 BAJISTA", "NEUTRAL": "↔️ NEUTRAL"}


def render_md(o: dict) -> str:
    when = pd.Timestamp(o["ts"]).strftime("%d-%b %H:%M UTC")
    px = f"{o['price']:,.1f} USD/oz" if o["price"] is not None else "n/d"
    lines = [
        f"# ÁUREO — Estudio de mercado del oro",
        f"_Actualizado {when} · precio {px} (fuente {o['provider']})_",
        "",
        f"## Outlook: {_BIAS_EMOJI[o['bias']]} (oro)",
        f"score combinado {o['combined_score']:+d}  "
        f"(macro {o['macro_score']:+d} + tono noticias {o['tone_score']:+d} + "
        f"geopolítica {o['geo_score']:+d})",
        "",
        f"## 🌍 Riesgo geopolítico: {o['risk_level']} {_GEO_RISK_EMOJI[o['risk_level']]}",
        f"{o['geo_escalation_count']} escalada / {o['geo_deescalation_count']} distensión "
        f"de {o['geo_n']} titulares geopolíticos "
        f"→ aporte al oro {o['geo_score']:+d} "
        f"({'al alza por refugio' if o['geo_score'] > 0 else 'resta refugio' if o['geo_score'] < 0 else 'sin sesgo refugio'})",
    ]
    if o["geo_headlines"]:
        for g in o["geo_headlines"]:
            flag = "🚨 " if g["strong"] else ""
            lines.append(f"- {flag}[{g['class']}] {g['title']} — _{g['source']}_  ({g['link']})")
    else:
        lines.append("- (sin titulares geopolíticos relevantes en esta corrida)")
    lines += ["", "## Drivers"]
    lines += [f"- {d}" for d in o["drivers"]]
    lines += ["", "## Próximo evento fuerte"]
    if o["next_event"]:
        bl = " — ⚠️ BLACKOUT (hoy)" if o["blackout"] else ""
        lines.append(f"- {o['next_event']} {o['next_when']} ({o['next_date']}){bl}")
    else:
        lines.append("- (sin eventos cargados próximos)")
    lines += ["", "## Top titulares del mundo (relevantes al oro)"]
    if o["top_titulares"]:
        for t in o["top_titulares"]:
            star = " 🔴" if t["critical"] else ""
            lines.append(f"- [{t['tone']}]{star} {t['title']} — _{t['source']}_  ({t['link']})")
    else:
        lines.append("- (sin titulares relevantes en esta corrida)")
    fail = f" · caídos: {', '.join(o['feeds_fail'])}" if o["feeds_fail"] else ""
    lines += ["", f"_Feeds vivos: {', '.join(o['feeds_ok']) or 'ninguno'}{fail}_",
              "", "_Esto es CONTEXTO, no una orden de compra/venta. Vos decidís._"]
    return "\n".join(lines)


def render_telegram(o: dict) -> str:
    when = pd.Timestamp(o["ts"]).strftime("%d-%b %H:%M UTC")
    px = f"{o['price']:,.1f}" if o["price"] is not None else "n/d"
    head = f"🥇 *ÁUREO — Estudio de mercado*  _{when}_\n"
    head += f"Precio oro: *{px}* USD/oz\n"
    head += f"Outlook: *{_BIAS_EMOJI[o['bias']]}*  (score {o['combined_score']:+d})\n\n"
    geo_dir = ("oro al alza por refugio" if o["geo_score"] > 0
               else "resta refugio al oro" if o["geo_score"] < 0 else "sin sesgo refugio")
    head += (f"🌍 *Riesgo geopolítico: {o['risk_level']}* {_GEO_RISK_EMOJI[o['risk_level']]} "
             f"({geo_dir})\n")
    head += (f"_{o['geo_escalation_count']} escalada / {o['geo_deescalation_count']} distensión "
             f"de {o['geo_n']} titulares geo (aporte {o['geo_score']:+d})_\n")
    if o["geo_headlines"]:
        head += "\n".join(f"  • {'🚨 ' if g['strong'] else ''}[{g['class']}] {g['title'][:80]}"
                          for g in o["geo_headlines"][:4]) + "\n"
    head += "\n*Drivers:*\n" + "\n".join(f"• {d}" for d in o["drivers"][:7])
    if o["next_event"]:
        bl = "  ⚠️ BLACKOUT HOY" if o["blackout"] else ""
        head += f"\n\n⚠️ Próximo evento: {o['next_event']} {o['next_when']} ({o['next_date']}){bl}"
    if o["top_titulares"]:
        head += "\n\n*Titulares:*\n" + "\n".join(
            f"• [{t['tone']}] {t['title'][:90]}" for t in o["top_titulares"][:5])
    head += "\n\n_Contexto, no una orden. Vos decidís._"
    return head


# --------------------------------------------------------------------------- #
# Persistencia y estado
# --------------------------------------------------------------------------- #
def _last_record() -> dict | None:
    if not JSONL.exists():
        return None
    try:
        last = None
        with JSONL.open() as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    last = ln
        return json.loads(last) if last else None
    except Exception:  # noqa: BLE001
        return None


def persist(o: dict) -> None:
    JSONL.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": o["ts"], "price": o["price"], "macro_score": o["macro_score"],
        "tone_score": o["tone_score"], "geo_score": o["geo_score"],
        "combined_score": o["combined_score"],
        "bias": o["bias"], "news_counts": o["news_counts"],
        "risk_level": o["risk_level"],
        "geo_escalation_count": o["geo_escalation_count"],
        "geo_deescalation_count": o["geo_deescalation_count"],
        "blackout": o["blackout"], "next_event": o["next_event"],
        "next_date": o["next_date"],
        "top_titulares": [{"title": t["title"], "link": t["link"],
                           "source": t["source"], "tone": t["tone"]}
                          for t in o["top_titulares"]],
        "geo_headlines": [{"title": g["title"], "link": g["link"],
                           "source": g["source"], "class": g["class"]}
                          for g in o["geo_headlines"]],
    }
    with JSONL.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    MD_OUT.parent.mkdir(parents=True, exist_ok=True)
    MD_OUT.write_text(render_md(o))


def alert_decision(o: dict, prev: dict | None) -> tuple[bool, str]:
    """¿Hay que alertar? (a) sesgo cambió, (b) blackout, (c) titular crítico.
    Anti-spam: no repetir el mismo motivo si el prev ya lo tenía."""
    reasons = []
    prev_bias = (prev or {}).get("bias")
    if prev is not None and prev_bias != o["bias"]:
        reasons.append(f"sesgo cambió {prev_bias}→{o['bias']}")
    if o["blackout"] and not (prev or {}).get("blackout"):
        reasons.append("blackout evento high-impact")
    if o["critical_hits"]:
        prev_titles = {t.get("title") for t in (prev or {}).get("top_titulares", [])}
        fresh = [h for h in o["critical_hits"] if h not in prev_titles]
        if fresh:
            reasons.append(f"titular crítico: {fresh[0][:60]}")
    # (a) el riesgo geopolítico SUBIÓ respecto a la última corrida (ej. MEDIO→ALTO).
    prev_risk = (prev or {}).get("risk_level")
    if prev is not None and prev_risk in _GEO_RISK_ORDER and \
            _GEO_RISK_ORDER[o["risk_level"]] > _GEO_RISK_ORDER.get(prev_risk, -1):
        reasons.append(f"riesgo geopolítico subió {prev_risk}→{o['risk_level']}")
    # (b) titular geopolítico de escalada FUERTE fresco (no estaba en el top anterior).
    if o["geo_critical_hits"]:
        prev_geo = {g.get("title") for g in (prev or {}).get("geo_headlines", [])}
        fresh_geo = [h for h in o["geo_critical_hits"] if h not in prev_geo]
        if fresh_geo:
            reasons.append(f"escalada geopolítica fresca: {fresh_geo[0][:60]}")
    return (bool(reasons), "; ".join(reasons))


def send_telegram(text: str) -> int:
    """Idéntico a analysis.daily_brief.send_telegram (grupo TRADING)."""
    import os
    import requests
    token = os.environ.get("AUREO_TG_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("AUREO_TG_BROADCAST_CHATS", "").split(",") if c.strip()]
    if not token or not chats:
        print("ERROR: falta AUREO_TG_BOT_TOKEN o AUREO_TG_BROADCAST_CHATS en .env", file=sys.stderr)
        return 1
    ok = 0
    for chat in chats:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
                          timeout=15)
        if r.status_code == 200:
            ok += 1
            print(f"enviado a {chat} ✅")
        else:
            print(f"falló {chat}: {r.status_code} {r.text[:120]}", file=sys.stderr)
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run_once(send: bool = False, force_alert: bool = False) -> int:
    prev = _last_record()
    o = build_outlook()
    print("=" * 64)
    print(render_md(o))
    print("=" * 64)
    persist(o)

    should, why = alert_decision(o, prev)
    if force_alert:
        should, why = True, (why or "force-alert (test)")

    px = f"{o['price']:,.1f}" if o["price"] is not None else "n/d"
    print(f"[market_study] precio {px} | sesgo {o['bias']} "
          f"(macro {o['macro_score']:+d}, tono {o['tone_score']:+d}, geo {o['geo_score']:+d}) | "
          f"riesgo geo {o['risk_level']} | "
          f"titulares a/b/n {o['news_counts']['alcista']}/{o['news_counts']['bajista']}/"
          f"{o['news_counts']['neutro']} | feeds_ok {len(o['feeds_ok'])} | "
          f"alerta={'SÍ' if should else 'no'}{(' — '+why) if should else ''}")

    if should and send:
        return send_telegram(render_telegram(o))
    if should and not send:
        print("[market_study] (alerta ameritada pero modo PREVIEW — agregá --send para postear)")
    elif not should:
        print("[market_study] sin cambios relevantes — solo logueado, no se manda.")
    return 0


def run_daemon(poll_seconds: int = 14400, send: bool = False) -> int:
    print(f"[market_study] estudio de mercado ON (cada {poll_seconds//3600}h). Ctrl-C para parar.")
    while True:
        try:
            run_once(send=send)
        except Exception as e:  # noqa: BLE001 — el estudio no se muere
            print(f"[market_study] err ciclo (sigue): {e}", file=sys.stderr)
        time.sleep(poll_seconds)


def main() -> int:
    ap = argparse.ArgumentParser(description="ÁUREO — estudio constante de mercado del oro")
    ap.add_argument("--once", action="store_true", help="Una corrida (imprime el outlook).")
    ap.add_argument("--daemon", action="store_true", help="Loop cada 4h (14400s).")
    ap.add_argument("--send", action="store_true",
                    help="Permitir postear al Telegram. Sin él = preview (no manda).")
    ap.add_argument("--force-alert", action="store_true",
                    help="Test: fuerza el envío del outlook actual.")
    ap.add_argument("--poll", type=int, default=14400, help="Segundos entre corridas en daemon.")
    args = ap.parse_args()
    if args.daemon:
        return run_daemon(poll_seconds=args.poll, send=args.send)
    return run_once(send=args.send, force_alert=args.force_alert)


if __name__ == "__main__":
    raise SystemExit(main())
