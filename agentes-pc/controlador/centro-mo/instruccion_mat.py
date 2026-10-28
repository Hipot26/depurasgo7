# -*- coding: utf-8 -*-
"""instruccion_mat.py — ¿El correo deja liquidar MATERIAL? (determinista, sin IA)

Regla (22 oct 2026): si el correo NO dice que no se liquide material
(«solo MO», «no liquidar material»…), la planilla estaba habilitada para liquidar MAT.

valor              qué dijo el correo                                      en el exporte validado
-----------------  ------------------------------------------------------  ----------------------
no_mat             «solo MO», «solo mano de obra», «no liquidar material»  se omite (hoja OMITIDOS)
parcial            «a excepción de…», «excepto…» junto a liquidar/material ámbar, decide quien liquida
pendiente_almacen  «pendiente confirmar materiales (Sonny) por almacén»   se incluye con aviso
con_mat            «mano de obra y materiales», «MO y MTE», «liquidar mat» se incluye
solo_menciona_mo   pide liquidar MO y no nombra el material                se incluye (regla)
sin_mencion        no habla de liquidar                                    se incluye (regla)

Manda el MENSAJE RECIENTE (asunto + cuerpo hasta la cadena de reenvíos). Un «solo MO» que solo
aparece en la cadena vieja baja a 'parcial': puede referirse a otro envío del mismo hilo.
Archivo único: lo usa analizador_correo.py (agente del PC) y su copia canónica vive en
SGO7/parches/centro-mo/pc/instruccion_mat.py.
"""
import re
import unicodedata

VERSION = "2026-10-22"

RE_CORTE_CADENA = re.compile(r"\n\s*(?:De|From|-{3,}\s*Mensaje original)\s*:", re.IGNORECASE)

_MO = r"(?:m\.?\s?o\.?|mano\s+de\s+obra|prestaciones)"
_MAT = r"(?:materiale?s?|mte|mat\.?)"

PATRONES = [
    ("no_mat", re.compile(
        rf"\b(?:solo|solamente|unicamente|exclusivamente)\s+(?:la\s+|el\s+|para\s+)?{_MO}(?!\w)"
        rf"|\bno\s+(?:se\s+)?(?:debe[n]?\s+|deb[ea]mos\s+|favor\s+)?(?:liquid\w+|cargar?\w*|facturar?\w*)\s+(?:el\s+|los\s+|la\s+)?{_MAT}(?!\w)"
        rf"|\b{_MAT}\s+no\s+(?:se\s+)?(?:debe[n]?\s+)?(?:liquid\w+|carga\w*|factur\w+)"
        rf"|\b(?:liquid\w+|planilla|presupuesto|cargue|cargar)\s+sin\s+{_MAT}(?!\w)"
        rf"|\b(?:excepto|salvo)\s+(?:el\s+|los\s+)?{_MAT}(?!\w)"
        rf"|\b{_MAT}\s+(?:lo|los)\s+liquida\s+(?:almacen|otro|otra)", re.I)),
    ("parcial", re.compile(
        r"\b(?:a\s+excepcion\s+de|excepto|salvo|exceptuando)\b", re.I)),
    ("pendiente_almacen", re.compile(
        rf"\bpendiente\s+(?:por\s+)?confirm\w*\s+(?:de\s+|los\s+|el\s+)?{_MAT}"
        rf"|\bno\s+se\s+ha(?:n)?\s+confirmado\s+(?:los\s+|el\s+)?{_MAT}"
        rf"|\bpendiente\s+por\s+confirmacion\s+de\s+{_MAT}", re.I)),
    ("con_mat", re.compile(
        rf"\b{_MO}\s+(?:y|e|\+|/)\s+{_MAT}(?!\w)"
        rf"|\b{_MAT}\s+(?:y|e|\+|/)\s+{_MO}(?!\w)"
        rf"|\bliquid\w*\s+(?:el\s+|los\s+|la\s+)?{_MAT}(?!\w)"
        rf"|\bliquidacion\s+de\s+(?:pendiente\s+de\s+)?{_MAT}(?!\w)"
        rf"|\bpresupuesto\s+de\s+{_MAT}(?!\w)", re.I)),
    ("solo_menciona_mo", re.compile(
        rf"\b(?:liquid\w*|cargue|carg\w+|presupuesto)\b[^.\n]{{0,40}}\b{_MO}(?!\w)"
        rf"|\b{_MO}\b[^.\n]{{0,25}}\bliquid\w*", re.I)),
]
ORDEN = {"no_mat": 0, "parcial": 1, "pendiente_almacen": 2, "con_mat": 3,
         "solo_menciona_mo": 4, "sin_mencion": 5}
INCLUYE = {"no_mat": False, "parcial": None, "pendiente_almacen": True, "con_mat": True,
           "solo_menciona_mo": True, "sin_mencion": True}
EXPLICITOS = ("no_mat", "parcial", "pendiente_almacen", "con_mat")


def _plano(s: str) -> str:
    """Sin tildes y con la MISMA longitud que el texto NFC: así la frase se corta del original."""
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _frase(texto: str, ini: int, fin: int) -> str:
    a = max(texto.rfind("\n", 0, ini), texto.rfind(". ", 0, ini) + 1, 0)
    cortes = [i for i in (texto.find("\n", fin), texto.find(". ", fin)) if i != -1]
    b = min(cortes) if cortes else len(texto)
    return re.sub(r"\s+", " ", texto[a:b]).strip()[:220]


def _clasificar(texto: str):
    orig = unicodedata.normalize("NFC", texto or "")
    plano = _plano(orig)
    fuente_frase = orig if len(orig) == len(plano) else plano
    mejor = None
    for valor, rx in PATRONES:
        for m in rx.finditer(plano):
            frase = _frase(fuente_frase, m.start(), m.end())
            if valor == "parcial" and not re.search(r"liquid|material|prestaci|mte", _plano(frase), re.I):
                continue
            cand = (ORDEN[valor], valor, frase)
            if mejor is None or cand[0] < mejor[0]:
                mejor = cand
            break
    return mejor


def instruccion_material(asunto: str, cuerpo: str, en: str = "") -> dict:
    """→ {valor, incluye, frase, fuente, en, version}."""
    cuerpo = cuerpo or ""
    m = RE_CORTE_CADENA.search(cuerpo)
    arriba = cuerpo[:m.start()] if m else cuerpo
    cadena = cuerpo[m.start():] if m else ""
    r = _clasificar((asunto or "") + "\n" + arriba)
    fuente = "correo"
    if r is None or r[1] in ("solo_menciona_mo", "sin_mencion"):
        rc = _clasificar(cadena)
        if rc and rc[1] == "no_mat":
            r, fuente = (ORDEN["parcial"], "parcial", rc[2]), "cadena"
    if r is None:
        r = (ORDEN["sin_mencion"], "sin_mencion", "")
    return {"valor": r[1], "incluye": INCLUYE[r[1]], "frase": r[2], "fuente": fuente,
            "en": en, "version": VERSION}
