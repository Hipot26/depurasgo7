"""
ia_lideres.py — Interpretación de instrucciones "nodo → líder" para liquidador2.

Dos capas, en este orden (patrón traído de CazadorPDL / ANS SG07):

  1. PARSER DETERMINISTA (`parsear_lineas_nodo_lider`). Las planillas casi
     siempre traen líneas del tipo "Nodo 2: líder uno" o "Nodo 2 al 6:
     líder dos". Eso se lee con regex: es exacto, gratis y NO pierde
     líderes. Un nodo mencionado en varias líneas acumula TODOS sus líderes
     (nunca se sobreescribe: "Nodo 2 al 6: líder dos" después de
     "Nodo 2: líder uno" significa que el nodo 2 lo trabajaron los dos).

  2. MODELO DE TEXTO (`llamar_ia_asignaciones`). Solo si el texto no encaja
     en el formato de líneas ni en la tabla por prestación. Proveedor
     principal: Cloudflare Workers AI (llama-3.3-70b, salida con json_schema).
     Respaldo: MiniMax (la función la presta el blueprint para no acoplar).

Configuración: `config_ia.json` en la raíz de SGS UI (mismas llaves que en
CazadorPDL: proveedor, cf_account_id, cf_ai_token, cf_model_texto,
cf_timeout_s). También se aceptan las variables de entorno CF_ACCOUNT_ID y
CF_AI_TOKEN. Nunca imprimir el token.
"""

import json
import logging
import os
import re

import requests

log = logging.getLogger("liquidador2.ia")

_RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_IA_PATH = os.path.join(_RAIZ, "config_ia.json")

_CFG_DEFAULT = {
    "proveedor": "auto",            # cloudflare | minimax | auto (cf y si falla minimax)
    "cf_account_id": "",
    "cf_ai_token": "",
    "cf_gateway": "",
    "cf_model_texto": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "cf_timeout_s": 60,
}


def cargar_config_ia() -> dict:
    cfg = dict(_CFG_DEFAULT)
    try:
        with open(CONFIG_IA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in _CFG_DEFAULT})
    except FileNotFoundError:
        pass
    except Exception as ex:  # JSON roto: se avisa y se sigue con defaults
        log.warning("config_ia.json ilegible: %s", ex)
    cfg["cf_account_id"] = os.environ.get("CF_ACCOUNT_ID") or cfg["cf_account_id"]
    cfg["cf_ai_token"]   = os.environ.get("CF_AI_TOKEN")   or cfg["cf_ai_token"]
    return cfg


def cloudflare_configurado(cfg=None) -> bool:
    cfg = cfg or cargar_config_ia()
    return bool(cfg.get("cf_account_id") and cfg.get("cf_ai_token"))


# ═══════════════════════════════════════════════════════════════════════════
# CAPA 1 — parser determinista de líneas "Nodo X: nombre"
# ═══════════════════════════════════════════════════════════════════════════

# Separadores de nombres dentro de una misma línea (igual que en blueprint).
_SEP_NOMBRES = re.compile(r"\s*[/;,+]\s*|\s+[-–—]\s+|\s+Y\s+|\s+E\s+", re.IGNORECASE)

# Token de nodo: 1, 17, 31A, 05
_TOK_NODO = r"\d{1,4}[A-Za-z]?"

# Lado izquierdo: "Nodo 1", "NODOS 2, 3 y 4", "Nodo 2 al 6", "nodo 2-3-4",
# "N5", "del nodo 2 al 6", "2 y 17". Las palabras de rango se capturan aparte.
_PAL_NODO = r"(?:nodos?|nd|n)\.?\s*"
_RE_LINEA = re.compile(
    r"^\s*(?:(?:del|los|el|la)\s+)?(?:" + _PAL_NODO + r")?"
    r"(?P<nodos>" + _TOK_NODO +
    r"(?:\s*(?:,|/|\+|-|–|—|y|e|al|a|hasta|,\s*y)\s*(?:(?:el\s+|los\s+)?" + _PAL_NODO + r")?" + _TOK_NODO + r")*)"
    r"\s*(?::|=|->|→|–|—|-)\s*"
    r"(?P<nombres>[^\d:=→][^:=→]*?)\s*$",
    re.IGNORECASE,
)
_RE_RANGO = re.compile(
    r"^(?P<a>" + _TOK_NODO + r")\s*(?:al|a|hasta)\s*(?P<b>" + _TOK_NODO + r")$",
    re.IGNORECASE,
)
_RE_LISTA_SEP = re.compile(r"\s*(?:,|/|\+|-|–|—|\by\b|\be\b)\s*", re.IGNORECASE)


def _expandir_nodos(expr: str) -> list:
    """'2 al 6' → ['2AL6'] · '2-3-4' → ['2','3','4'] · '1 y 17' → ['1','17'].
    Guion = lista (como en la ayuda del panel); rango solo con 'al'/'a'/'hasta'.

    Un rango NO se expande aquí: en Dominion "nodo 2 al 6" casi siempre es el
    nodo CABLE '2AL6' (proyecto N2_N6 en SGS), que en la planilla es un nodo
    propio. Se devuelve el token canónico 'aALb' y el blueprint decide contra
    los nodos reales de la planilla (`resolver_nodos_contra_planilla`): si
    existe el cable, va al cable; si solo existen los individuales, se expande.
    Ejemplo: 'Nodo 2 al 6: líder dos' = 2AL6.
    """
    expr = expr.strip()
    m = _RE_RANGO.match(expr)
    if m and m.group("a").isdigit() and m.group("b").isdigit():
        a, b = int(m.group("a")), int(m.group("b"))
        if a <= b and b - a <= 200:
            return [f"{a}AL{b}"]
    # Un rango puede venir dentro de una lista: "1, 3 al 5 y 9"
    out = []
    # Primero separar por comas/'y', respetando "X al Y" como un solo tramo.
    for parte in re.split(r"\s*(?:,|/|\+|\by\b|\be\b)\s*", expr, flags=re.IGNORECASE):
        parte = parte.strip()
        if not parte:
            continue
        mr = _RE_RANGO.match(parte)
        if mr and mr.group("a").isdigit() and mr.group("b").isdigit():
            a, b = int(mr.group("a")), int(mr.group("b"))
            if a <= b and b - a <= 200:
                out.append(f"{a}AL{b}")
                continue
        for tok in re.split(r"\s*[-–—]\s*", parte):
            tok = tok.strip()
            if re.fullmatch(_TOK_NODO, tok):
                out.append(tok.upper())
    # Dedupe conservando orden
    vistos, res = set(), []
    for n in out:
        if n not in vistos:
            vistos.add(n)
            res.append(n)
    return res


def _limpiar_nombre(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip(" .;-–—")
    return s.upper()


def parsear_lineas_nodo_lider(texto: str):
    """
    Parser determinista. Devuelve
        [{"nodos": ["2"], "lideres": ["LIDER UNO"]}, ...]   (una por línea, en orden)
    o None si el texto no parece ese formato (ninguna línea válida o menos del
    60% de las líneas no vacías encajan).

    La acumulación por nodo (varios líderes para el mismo nodo) se hace en
    `agrupar_por_nodo`; aquí se conserva la línea original para poder mostrarla.
    """
    filas, candidatas = [], 0
    for linea in (texto or "").splitlines():
        ln = linea.strip()
        if not ln:
            continue
        candidatas += 1
        m = _RE_LINEA.match(ln)
        if not m:
            continue
        nodos = _expandir_nodos(m.group("nodos"))
        if not nodos:
            continue
        nombres = [_limpiar_nombre(x) for x in _SEP_NOMBRES.split(m.group("nombres"))]
        nombres = [x for x in nombres if len(x) >= 3 and re.search(r"[A-ZÁÉÍÓÚÑ]", x)]
        if not nombres:
            continue
        filas.append({"nodos": nodos, "lideres": nombres, "linea": ln})
    if filas and candidatas and len(filas) >= 0.6 * candidatas:
        return filas
    return None


_RE_TOKEN_RANGO = re.compile(r"^(\d+)AL(\d+)$", re.IGNORECASE)


def resolver_nodos_contra_planilla(por_nodo: dict, nodos_planilla, equiv) -> dict:
    """
    Reemplaza cada nodo interpretado por el nodo LITERAL de la planilla al que
    corresponde, usando `equiv(nodo_planilla, nodo_texto)` (rango-aware, el
    `_nodo_equiv` del verificador).

      · '2AL6' y la planilla tiene el cable '2AL6' / 'N2_N6' → ese nodo (NO se expande).
      · '2AL6' sin cable pero con nodos 2..6 individuales → se expande a los que existan.
      · '02' vs '2', '31A' → el literal de la planilla.
      · Sin coincidencia → se deja tal cual (la UI lo marca "no en Excel").
    Si dos tokens caen en el mismo nodo real, sus líderes se acumulan.
    """
    conocidos = [str(n) for n in (nodos_planilla or []) if str(n or "").strip()]
    out = {}

    def _add(nodo, lids):
        lst = out.setdefault(nodo, [])
        for l in lids:
            if l not in lst:
                lst.append(l)

    for nodo, lids in (por_nodo or {}).items():
        directo = [c for c in conocidos if equiv(c, nodo)]
        if directo:
            _add(directo[0], lids)
            continue
        m = _RE_TOKEN_RANGO.match(str(nodo))
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            hallados = []
            for n in range(a, b + 1):
                for c in conocidos:
                    if equiv(c, str(n)) and c not in hallados:
                        hallados.append(c)
            if hallados:
                for c in hallados:
                    _add(c, lids)
                continue
        _add(nodo, lids)
    return out


def agrupar_por_nodo(asignaciones) -> dict:
    """
    [{"nodos":[...], "lideres":[...]}] → {nodo: [lider, ...]} conservando el
    orden de aparición y SIN sobreescribir: un nodo que aparece en dos líneas
    acumula los líderes de ambas. Acepta también el formato viejo {"lider": "X"}.
    """
    por_nodo = {}
    for a in asignaciones or []:
        lids = a.get("lideres")
        if not lids and a.get("lider"):
            lids = [a["lider"]]
        lids = [_limpiar_nombre(str(l)) for l in (lids or []) if str(l or "").strip()]
        nodos = a.get("nodos") or ([a["nodo"]] if a.get("nodo") else [])
        for nodo in nodos:
            nodo = str(nodo).strip().upper()
            if not nodo:
                continue
            lst = por_nodo.setdefault(nodo, [])
            for l in lids:
                if l and l not in lst:
                    lst.append(l)
    return por_nodo


# ═══════════════════════════════════════════════════════════════════════════
# CAPA 2 — modelo de texto (Cloudflare Workers AI, respaldo MiniMax)
# ═══════════════════════════════════════════════════════════════════════════

PROMPT_ASIGNACIONES = (
    "Eres un asistente de Dominion Colombia que ayuda a asignar líderes de cuadrilla "
    "en proyectos de telecomunicaciones.\n"
    "Del siguiente texto, determina cómo se deben asignar los líderes:\n\n"
    "• Si el texto indica qué líder va a qué NODO específico "
    "(ej: 'nodo 54 con Juan', 'el 55 le toca a María', 'Nodo 2 al 6: Pedro'), responde:\n"
    '  {"mode":"nodo","asignaciones":[{"nodos":["54","55"],"lideres":["NOMBRE"]}]}\n'
    "  - 'nodos' lleva cada nodo por separado; 'X al Y' es un rango (incluye todos).\n"
    "  - Si un nodo aparece en varias líneas o con varios nombres, conserva TODOS los "
    "nombres para ese nodo (una entrada por línea del texto). NUNCA descartes un líder "
    "porque el nodo ya tuviera otro: significa que ambos trabajaron ese nodo.\n\n"
    "• Si el texto especifica por PRESTACIÓN (código QP) de un nodo quién o quiénes "
    "liquidan (tablas NODO/CODIGO/CANT/OBSERVACION, o frases como 'en el nodo 309 "
    "la QP1180 la liquidan Vicente y Willinton'), responde:\n"
    '  {"mode":"prestacion","asignaciones":[{"nodo":"309","codigo":"QP1180","lideres":["VICENTE FUQUENE","WILLINTON CARDENAS"]}]}\n'
    "  - 'lideres' lleva TODOS los nombres mencionados para esa fila "
    "(pueden venir separados por -, /, y, comas).\n"
    "  - Una entrada por cada combinación nodo+código mencionada.\n\n"
    "• En CUALQUIER OTRO CASO (nombrar líderes sin nodo específico, pedir reparto, "
    "división, distribución, o solo listar nombres), usa modo equitativo.\n"
    "  - Si el texto menciona porcentajes o proporciones exactas, extráelos (deben sumar 100).\n"
    "  - Si NO hay porcentajes, omite 'pct' para que el sistema reparta en partes iguales.\n"
    "  Ejemplos:\n"
    '  Con pct:  {"mode":"equitativo","lideres":[{"nombre":"Juan","pct":50},{"nombre":"María","pct":30},{"nombre":"Luis","pct":20}]}\n'
    '  Sin pct:  {"mode":"equitativo","lideres":[{"nombre":"Juan"},{"nombre":"María"},{"nombre":"Luis"}]}\n\n'
    "Copia los nombres tal como vienen en el texto (no inventes apellidos). "
    "El JSON lleva siempre las tres llaves 'mode', 'asignaciones' y 'lideres': "
    "en modo nodo/prestacion deja 'lideres' = []; en modo equitativo deja 'asignaciones' = []. "
    "En modo nodo deja 'nodo' y 'codigo' como cadena vacía; en modo prestacion deja 'nodos' = []. "
    "'pct' = null si no hay porcentaje.\n"
    "Responde SOLO con JSON válido, sin texto adicional, sin markdown, sin ```json.\n\n"
    "Texto:\n"
)

# Esquema para la salida guiada de Workers AI (json_schema). Todas las ramas
# comparten un mismo objeto para que el modelo no tenga que elegir esquema.
# OJO: el decodificador guiado de Workers AI SOLO emite las llaves 'required';
# una llave opcional se omite siempre (verificado 2026-09-25: con required=[mode]
# devolvía {"mode":"nodo"} y nada más). Por eso todo va en required.
SCHEMA_ASIGNACIONES = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["nodo", "prestacion", "equitativo"]},
        "asignaciones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nodos":   {"type": "array", "items": {"type": "string"}},
                    "nodo":    {"type": "string"},
                    "codigo":  {"type": "string"},
                    "lideres": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["nodos", "nodo", "codigo", "lideres"],
            },
        },
        "lideres": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "pct":    {"anyOf": [{"type": "number"}, {"type": "null"}]},
                },
                "required": ["nombre", "pct"],
            },
        },
    },
    "required": ["mode", "asignaciones", "lideres"],
}


def _url_chat(cfg: dict) -> str:
    acc = cfg["cf_account_id"]
    gw = (cfg.get("cf_gateway") or "").strip()
    if gw:
        return f"https://gateway.ai.cloudflare.com/v1/{acc}/{gw}/workers-ai/v1/chat/completions"
    return f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/v1/chat/completions"


def _extraer_json(texto):
    if isinstance(texto, dict):
        return texto
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (texto or "").strip(), flags=re.S)
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except ValueError:
            return {}


def llamar_cloudflare_asignaciones(texto: str, cfg=None) -> dict:
    """Cloudflare Workers AI (OpenAI-compatible) con salida json_schema.
    Lanza RuntimeError con el motivo si no hay respuesta usable."""
    cfg = cfg or cargar_config_ia()
    if not cloudflare_configurado(cfg):
        raise RuntimeError("Cloudflare no configurado (falta cf_account_id / cf_ai_token en config_ia.json)")
    cuerpo = {
        "model": cfg["cf_model_texto"],
        # Workers AI exige el MISMO tipo de content en todos los mensajes → strings.
        "messages": [{"role": "user", "content": PROMPT_ASIGNACIONES + texto}],
        "response_format": {"type": "json_schema", "json_schema": SCHEMA_ASIGNACIONES},
        "max_tokens": 4000,
        "temperature": 0.0,
    }
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {cfg['cf_ai_token']}"}
    timeout = float(cfg.get("cf_timeout_s") or 60)
    ultimo = ""
    # 1er intento con json_schema; si el backend lo rechaza, 2do sin él.
    for intento in ("schema", "libre"):
        if intento == "libre":
            cuerpo.pop("response_format", None)
        try:
            resp = requests.post(_url_chat(cfg), headers=headers, json=cuerpo, timeout=timeout)
        except Exception as ex:
            ultimo = f"{type(ex).__name__}: {ex}"
            continue
        if resp.status_code != 200:
            ultimo = f"HTTP {resp.status_code} {resp.text[:300]}"
            # 4xx de esquema → reintentar libre; auth/cuenta → no insistir
            if resp.status_code in (401, 403, 404):
                break
            continue
        try:
            d = resp.json()
        except ValueError:
            ultimo = "respuesta no JSON"
            continue
        contenido = None
        if isinstance(d.get("choices"), list) and d["choices"]:
            contenido = (d["choices"][0].get("message") or {}).get("content")
        elif isinstance(d.get("result"), dict):
            contenido = d["result"].get("response")
        js = _extraer_json(contenido)
        if isinstance(js, dict) and js.get("mode"):
            return js
        ultimo = f"sin JSON usable: {str(contenido)[:200]}"
    raise RuntimeError(f"Cloudflare Workers AI falló ({ultimo})")


def llamar_ia_asignaciones(texto: str, minimax_fn=None, cfg=None) -> dict:
    """
    Punto de entrada de la capa 2. Según `proveedor` en config_ia.json:
      cloudflare → solo Cloudflare
      minimax    → solo MiniMax (minimax_fn)
      auto       → Cloudflare y, si falla, MiniMax
    Devuelve el dict {"mode": ..., ...} con `_proveedor` anotado.
    """
    cfg = cfg or cargar_config_ia()
    prov = (cfg.get("proveedor") or "auto").lower()
    errores = []
    if prov in ("cloudflare", "auto") and cloudflare_configurado(cfg):
        try:
            d = llamar_cloudflare_asignaciones(texto, cfg)
            d["_proveedor"] = "cloudflare"
            return d
        except Exception as ex:
            log.warning("IA asignaciones: Cloudflare falló: %s", ex)
            errores.append(str(ex))
            if prov == "cloudflare":
                raise
    elif prov in ("cloudflare", "auto"):
        errores.append("Cloudflare no configurado")
        if prov == "cloudflare":
            raise RuntimeError(errores[-1])
    if minimax_fn is None:
        raise RuntimeError("; ".join(errores) or "Sin proveedor de IA disponible")
    d = minimax_fn(texto)
    if not isinstance(d, dict):
        raise RuntimeError("MiniMax devolvió algo que no es un objeto JSON")
    d["_proveedor"] = "minimax"
    if errores:
        d["_aviso"] = "Cloudflare falló, se usó MiniMax: " + errores[-1][:160]
    return d
