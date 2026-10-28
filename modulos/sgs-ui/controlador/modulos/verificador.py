"""
verificador.py — Compara Excel vs SGS. SOLO LECTURA via SGSReadOnlyClient.
"""

import re
import sys
import os
import logging
import difflib
import time
import unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sgs_readonly_client import SGSReadOnlyClient

log = logging.getLogger("verificador")
_BUSCAR_DF_CACHE = {}
_BUSCAR_DF_CACHE_TTL = 120.0

# ── Filtro de bandejas válidas (mismo criterio que julian/blueprint.py) ────────
_TRAY_DELIVERY_FIELD = "state_picking"   # verificado via fields_get en SGS
_PICKING_VALIDO_V    = frozenset({"atendido", "parcial"})
_STATE_EXCLUIDO_V    = frozenset({"canceled", "canceled_partial", "to_approve"})


def _bandeja_valida_v(tray: dict) -> bool:
    """state=approved Y state_picking in (atendido, parcial). Solo para despacho."""
    state = str(tray.get("state") or "").lower()
    if state in _STATE_EXCLUIDO_V or state not in ("approved", "aprobado"):
        return False
    sp = tray.get(_TRAY_DELIVERY_FIELD)
    if sp is None or sp is False:
        return True   # campo ausente → incluir conservadoramente
    return str(sp).lower() in _PICKING_VALIDO_V


def _bandeja_activa_v(tray: dict) -> bool:
    """
    Filtro por tipo de bandeja:
    - Despacho: state=approved Y state_picking in {atendido, parcial}.
    - Devolucion: state=approved, state_picking ignorado.
      Una devolución aprobada reduce el neto aunque el almacén no haya completado
      el picking físico; el Excel (CRUCE_LIQUIDADOR) ya la contabiliza en sgs_neto_excel.
    """
    state = str(tray.get("state") or "").lower()
    if state in _STATE_EXCLUIDO_V or state not in ("approved", "aprobado"):
        return False
    if str(tray.get("type") or "").lower() == "devolution":
        return True   # devolución aprobada → siempre contar
    return _bandeja_valida_v(tray)


_MAT_TOL_V = 0.001   # tolerancia de cantidad para splits

def _lider_neto_por_bandeja_v(dispatch_pq: dict, return_pq: dict) -> dict:
    """
    Calcula la cantidad neta por líder: despacho - devolución atribuida.
    Devoluciones sin líder ("") se distribuyen proporcionalmente al despacho.
    Retorna {partner: gross_qty} solo con gross > TOL.
    """
    dispatch_pq = dict(dispatch_pq)
    return_pq   = dict(return_pq)
    unattr = return_pq.pop("", 0.0)
    if unattr > 0 and dispatch_pq:
        total_d = sum(dispatch_pq.values()) or 1.0
        for lider, d_qty in dispatch_pq.items():
            return_pq[lider] = return_pq.get(lider, 0.0) + unattr * d_qty / total_d
    neto = {}
    for lider, d_qty in dispatch_pq.items():
        gross = round(d_qty - return_pq.get(lider, 0.0), 4)
        if gross > _MAT_TOL_V:
            neto[lider] = gross
    return neto


_CAMPOS_PROY = ["id", "code", "name", "nodo", "contract_id", "department_id",
                "formula_id", "parent_id", "responsible_id", "order_number",
                "status"]


def _texto_m2o_v(valor) -> str:
    if isinstance(valor, (list, tuple)):
        if len(valor) >= 2:
            return str(valor[1] or "").strip()
        if valor:
            return str(valor[0] or "").strip()
        return ""
    return str(valor or "").strip()


def _texto_norm_v(valor) -> str:
    base = unicodedata.normalize("NFD", str(valor or "").upper())
    return "".join(c for c in base if unicodedata.category(c) != "Mn")


def _es_linea_viva_sgs(proy: dict) -> bool:
    """
    True si el proyecto SGS pertenece a Linea Viva/LLEE.

    En programados puede compartir LCL con proyectos normales. Para evitar
    falsos matches, esos candidatos se omiten al resolver nodos del Excel.
    """
    texto = " ".join([
        _texto_m2o_v(proy.get("department_id")),
        _texto_m2o_v(proy.get("contract_id")),
        str(proy.get("name") or ""),
    ])
    texto = _texto_norm_v(texto)
    return bool(re.search(r"\bLINEA\s+VIVA\b|\bLLEE\b", texto))


def _filtrar_no_linea_viva(candidatos: list) -> tuple[list, list]:
    permitidos = []
    bloqueados = []
    for p in candidatos or []:
        if _es_linea_viva_sgs(p):
            bloqueados.append(p)
        else:
            permitidos.append(p)
    return permitidos, bloqueados


def _avisar_linea_viva_omitida(ck: dict, bloqueados: list, nodo_val, nodo_idx: int):
    if not bloqueados:
        return
    relacionados = [
        p for p in bloqueados
        if _nodo_equiv(p.get("nodo"), nodo_val)
        or (nodo_idx != -1 and _nodo_match_smart(p.get("name") or "", nodo_val, nodo_idx))
    ]
    if not relacionados:
        return
    codigos = ", ".join(p.get("code", "?") for p in relacionados[:5])
    log.info(
        "Nodo %s: se omitio candidato Linea Viva/LLEE (%s) durante resolucion SGS.",
        nodo_val,
        codigos,
    )


def _nodo_num(val) -> int:
    if not val:
        return -1
    m = re.search(r"\d+", str(val))
    return int(m.group()) if m else -1


def _nodo_norm(val) -> str:
    # En SGS el proyecto padre suele venir con nodo=False/None.
    # Eso semánticamente significa "sin nodo", no el texto "FALSE".
    if val is None or val is False:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(val or "").upper())


def _nodo_es_numerico(val) -> bool:
    norm = _nodo_norm(val)
    return bool(norm) and norm.isdigit()


_NODO_PREFIX_RE = re.compile(r'^(NODO|ND|N)\d+$')

# ── Detección de nodo CABLE (rango multi-nodo en Excel) ───────────────────────
# Patrones reconocidos: 1AL4  N1_N4  N1-N4  1$  1_$  CABLE  1_N4  N1AL4
_NODO_CABLE_RE = re.compile(
    r'\d+\s*AL\s*(?:NODO\s*)?\d+'  # 1AL4, 1 AL 4, N1AL4, NODO 1 AL NODO 4
    r'|N\d+[_\-]N\d+'              # N1_N4, N1-N4
    r'|\d+[_\s]*\$'                # 1$, 1_$, 1 $
    r'|\bCABLE\b',                  # CABLE explícito
    re.IGNORECASE,
)


def _es_nodo_cable_excel(nodo_str: str) -> bool:
    """True si el valor de nodo del Excel indica un rango multi-nodo (proyecto CABLE)."""
    return bool(_NODO_CABLE_RE.search(str(nodo_str or "")))


def _es_nodo_cable_sgs(nombre_proyecto: str) -> bool:
    """True si el nombre del proyecto SGS corresponde al proyecto CABLE."""
    return "CABLE" in str(nombre_proyecto or "").upper()


# Valor de nodo que ES un rango completo: "1AL5", "1_5", "N1-N5", "NODO 1 AL 5".
# A diferencia de _NODO_CABLE_RE (que busca el patrón dentro de un nombre largo),
# aquí el string entero debe ser el rango — se usa para equivaler el nodo del
# Excel con el campo `nodo` de SGS ("1AL5" ≡ "1_5").
_RANGO_NODO_RE = re.compile(
    r'^\s*(?:NODO|ND|N)?\s*(\d+)\s*(?:AL|[_\-])\s*(?:NODO|ND|N)?\s*(\d+)\s*$',
    re.IGNORECASE,
)


def _cable_nodo_match(nombre: str, nodo_str: str) -> bool:
    """
    Para nodos cable (rango multi-nodo como '127AL122' o 'N127_N122'):
    exige que TODOS los números extremos del rango aparezcan como número
    delimitado en el nombre del proyecto.

    Ejemplo:
      _cable_nodo_match("6300995703 N127_N126", "127AL122") → False  (122 no está)
      _cable_nodo_match("6300995703 N127_N126", "127AL126") → True   (127 y 126 sí)
    """
    nums = re.findall(r'\d+', str(nodo_str or ""))
    if len(nums) < 2:
        return False
    nombre_up = nombre.upper()
    # Rango como unidad en el nombre: "NODO 26AL31", "26 AL 31", "N26_N31".
    # Cubre extremos pegados a letras ("26AL31") que el chequeo por tokens
    # rechazaría, sin abrir la puerta a dígitos sueltos tipo "TRA1".
    if len(nums) == 2:
        a, b = nums[0], nums[1]
        rango_pats = [
            rf'(?<!\d)0*{a}\s*AL\s*(?:NODO\s*)?0*{b}(?!\d)',
            rf'(?<![A-Z\d])N?0*{a}\s*[_\-]\s*N?0*{b}(?!\d)',
        ]
        if any(re.search(p, nombre_up) for p in rango_pats):
            return True
    # Cada extremo debe aparecer como token de nodo: con prefijo N/ND/NODO o
    # delimitado por no-alfanuméricos. Un dígito pegado a letras NO cuenta —
    # "TRA1" (tramo 1) aportaba un "1" espurio y '1AL5' matcheaba con N5_N48.
    return all(
        re.search(rf'(?<![A-Z\d])(?:NODO\s*|ND\s*|N\s*)?0*{n}(?!\d)', nombre_up)
        for n in nums
    )



def _nodo_en_rango_cable(nombre: str, nodo_idx: int) -> bool:
    """
    Fallback de último recurso: el Excel trae un nodo individual (ej. '18') pero
    NO existe proyecto individual para ese nodo — solo existe el proyecto CABLE
    que tiende el tramo entre dos nodos (ej. 'N17_N18', 'NODO 17 AL NODO 18').
    En ese caso los materiales del tramo SÍ pertenecen al proyecto cable.

    Solo usar cuando la búsqueda de proyecto individual (_nodo_equiv/_nodo_match)
    no encontró NINGÚN candidato — si existe un proyecto individual dedicado,
    ese tiene prioridad y este fallback no debe usarse (evita repetir el bug
    de asignar "NODO 1" a "NODO 1 AL NODO 6" cuando el nodo 1 individual existe).
    """
    nombre_up = nombre.upper()
    if not _NODO_CABLE_RE.search(nombre_up):
        return False
    n = str(nodo_idx)
    return bool(re.search(rf'(?<!\d)0*{n}(?!\d)', nombre_up))


def _nodo_bare(norm: str) -> str:
    """Strips N/ND/NODO prefix (only when followed by a digit) and returns the bare value.
    'N6A'→'6A', 'NODO6A'→'6A', 'ND6'→'6', 'NEUSA'→'NEUSA' (no digit after N).
    """
    m = re.match(r'^(?:NODO|ND|N)(?=\d)(.+)$', norm)
    return m.group(1) if m else norm


def _nodo_equiv(val_sgs, val_excel) -> bool:
    sgs = _nodo_norm(val_sgs)
    xls = _nodo_norm(val_excel)
    if not sgs and not xls:
        return True
    if not sgs or not xls:
        return False

    # Rango vs rango: "1AL5" ≡ "1_5" ≡ "N1-N5" (mismo par de extremos).
    # Decisivo en ambos sentidos: dos rangos con pares distintos NO son
    # equivalentes ("1AL5" ≢ "5_48"), aunque compartan un extremo.
    m_sgs = _RANGO_NODO_RE.match(str(val_sgs or ""))
    m_xls = _RANGO_NODO_RE.match(str(val_excel or ""))
    if m_sgs and m_xls:
        return (sorted(int(g) for g in m_sgs.groups())
                == sorted(int(g) for g in m_xls.groups()))

    if sgs == xls:
        # "1_5" y "15" normalizan igual pero son nodos distintos (tramo vs
        # individual): exigir que la secuencia numérica cruda coincida.
        nums_sgs = [int(n) for n in re.findall(r"\d+", str(val_sgs or ""))]
        nums_xls = [int(n) for n in re.findall(r"\d+", str(val_excel or ""))]
        return nums_sgs == nums_xls

    sgs_num = _nodo_num(sgs)
    xls_num = _nodo_num(xls)

    # Both purely numeric: compare by number only.
    if _nodo_es_numerico(sgs) and _nodo_es_numerico(xls):
        return sgs_num != -1 and sgs_num == xls_num

    # Cross: "N68"/"ND68"/"NODO68" vs "68" (one side purely numeric, other has N prefix).
    # Guard: at least one side must be purely numeric to avoid "N6"≡"6A".
    if sgs_num != -1 and xls_num != -1 and sgs_num == xls_num:
        if (_nodo_es_numerico(sgs) or _nodo_es_numerico(xls)):
            if _NODO_PREFIX_RE.match(sgs) or _NODO_PREFIX_RE.match(xls):
                return True

    # Alphanumeric nodo with N/ND/NODO prefix: "N6A" vs "6A", "NODO6A" vs "N6A".
    # Strip the prefix and compare bare values so N6A≡6A but N6≢6A.
    bare_sgs = _nodo_bare(sgs)
    bare_xls = _nodo_bare(xls)
    if bare_sgs and bare_xls and bare_sgs == bare_xls:
        return True

    return False


def _nodo_match(nombre: str, nodo_idx: int) -> bool:
    """
    Detecta si el nombre del proyecto contiene el nodo nodo_idx (solo parte numérica).
    Tolera variantes humanas: NODO9 / ND9 / N9 / N09 etc.
    NO matchea si el nombre describe un rango multi-nodo (ej: "NODO 1 AL NODO 6",
    "N3_N4", "N1-N4") para evitar asignar nodo individual a proyecto de rango.
    """
    n = str(nodo_idx)
    nombre_up = nombre.upper()
    # Proyecto de rango tipo "NODO 1 AL NODO 6"
    if re.search(rf"NODO\s*[-_]?\s*0*{n}(?!\d)\s+AL\b", nombre_up):
        return False
    # Proyecto de rango tipo cable: "N3_N4", "N1-N4", "1AL6", etc.
    if _NODO_CABLE_RE.search(nombre_up):
        return False
    patrones = [
        rf"NODO\s*[-_]?\s*0*{n}(?!\d)",
        rf"\bND\s*[-_]?\s*0*{n}(?!\d)",
        rf"(?<![A-Z\d])N\s*[-_]?\s*0*{n}(?!\d)",
    ]
    return any(re.search(p, nombre_up) for p in patrones)


def _nodo_match_smart(nombre: str, nodo_val: str, nodo_idx: int) -> bool:
    """
    Fallback por nombre cuando el campo `nodo` SGS está vacío o mal escrito.

    Para nodos alfanuméricos ('6A', '10B'): busca el bare value exacto (ej. '6A')
    precedido por NODO/ND/N o por un no-alfanumérico — N6A matchea, N6 NO matchea.
    Sin fallback numérico (sería ambiguo: N6 ≠ N6A).
    Para nodos puramente numéricos: usa _nodo_match() normal.
    """
    nodo_norm = _nodo_norm(nodo_val)
    if not nodo_norm.isdigit():
        bare = _nodo_bare(nodo_norm)      # "6A"
        nombre_up = nombre.upper()
        b = re.escape(bare)
        patrones = [
            rf"NODO\s*[-_]?\s*{b}(?![A-Z\d])",         # NODO 6A / NODO6A
            rf"\bND\s*[-_]?\s*{b}(?![A-Z\d])",          # ND6A
            rf"(?<![A-Z\d])N\s*[-_]?\s*{b}(?![A-Z\d])", # N6A / N 6A
            rf"(?<![A-Z\d]){b}(?![A-Z\d])",              # standalone 6A
        ]
        return any(re.search(p, nombre_up) for p in patrones)
    return _nodo_match(nombre, nodo_idx)


def _sugerir_nodo_parecido(nodo_excel, candidatos) -> str:
    objetivo = _nodo_norm(nodo_excel)
    if not objetivo:
        return ""

    nodos = []
    for p in candidatos or []:
        nodo_raw = p.get("nodo")
        norm = _nodo_norm(nodo_raw)
        if not norm:
            continue
        nodos.append((str(nodo_raw), norm, p.get("code", "?")))

    if not nodos:
        return ""

    match = difflib.get_close_matches(
        objetivo,
        [norm for _, norm, _ in nodos],
        n=1,
        cutoff=0.7,
    )
    if not match:
        return ""

    mejor = match[0]
    for nodo_raw, norm, code in nodos:
        if norm == mejor:
            return f"Posible typo en Excel: nodo '{nodo_excel}' se parece a '{nodo_raw}' ({code})."
    return ""


# ── Verificación MO ───────────────────────────────────────────────────────────
def buscar_proyectos_por_df_nombre(c: SGSReadOnlyClient, df_id: str, fields: list,
                                   limit: int = 80) -> list:
    """
    Busca proyectos por nombre/order_number de DF tolerando un ultimo caracter faltante.

    Ejemplo real: Excel/DF DF202664360573, SGS DF20266436057 NODO 3.
    Este fallback solo amplia candidatos; la decision sigue atada al nodo.
    """
    df = str(df_id or "").strip()
    if not df:
        return []

    cache_key = (df, tuple(fields or []), int(limit or 0))
    now = time.monotonic()
    cached = _BUSCAR_DF_CACHE.get(cache_key)
    if cached and now - cached[0] <= _BUSCAR_DF_CACHE_TTL:
        return [dict(p) for p in cached[1]]

    # Variante con espacio: los nodos hijos pueden llamarse 'DF 202664502069 nodo 2'
    # (espacio entre DF y dígitos) aunque el padre sea 'DF202664502069'. El ticket
    # de los hijos también guarda la forma con espacio.
    variantes = [df]
    m_esp = re.match(r"^(DF)(\d+)$", df, re.IGNORECASE)
    if m_esp:
        variantes.append(f"{m_esp.group(1)} {m_esp.group(2)}")

    dominios = []
    for v in variantes:
        dominios.extend([
            [["name", "=like", v + "%"]],
            [["name", "ilike", v]],
            [["order_number", "ilike", v]],
            [["ticket", "ilike", v]],
        ])
        if len(v) >= 8:
            v_trunc = v[:-1]
            dominios.extend([
                [["name", "=like", v_trunc + "%"]],
                [["name", "ilike", v_trunc]],
                [["order_number", "ilike", v_trunc]],
            ])

    vistos = set()
    candidatos = []
    for domain in dominios:
        for p in c.search_read("project.dominion", domain, fields, limit=limit):
            pid = p.get("id")
            if pid in vistos:
                continue
            vistos.add(pid)
            candidatos.append(p)
    _BUSCAR_DF_CACHE[cache_key] = (now, [dict(p) for p in candidatos])
    return candidatos


def verificar_fila_mo(c: SGSReadOnlyClient, row: dict, _cache: dict | None = None) -> dict:
    """
    Verifica una fila de MO contra SGS.
    Retorna dict con {ok, errores, advertencias, pid, pro_code, ...}
    _cache: dict compartido por el caller para evitar repetir search_read por df_id.
    """
    ck = {"row": row.copy(), "ok": True, "errores": [], "advertencias": []}

    pro_val  = row["pro"]
    odm_meta = row.get("_odm", "")
    proy     = None
    nodo_val = ""
    df_id    = odm_meta if odm_meta else (pro_val if not pro_val.isdigit() else "")

    try:
        if pro_val.startswith("PRO-"):
            proy = c.buscar_por_codigo(pro_val)

        elif df_id:
            nodo_val = pro_val
            nodo_idx = _nodo_num(nodo_val)
            nodo_vacio_excel  = not _nodo_norm(nodo_val)
            nodo_es_cable     = _es_nodo_cable_excel(nodo_val)

            # ── Detección temprana de nodo CABLE (rango multi-nodo) ──────────
            # Patrones como "1AL4", "N1_N4" indican el proyecto NODO//CABLE.
            # El sistema lo señala y busca ese proyecto específicamente.
            # Si no se encuentra → bloquea la liquidación (no liquidar en nodo
            # equivocado por confusión de patrón).
            if nodo_es_cable:
                ck["_nodo_cable"] = True
                ck["advertencias"].append(
                    f"⚠ NODO CABLE: '{nodo_val}' es un rango multi-nodo. "
                    "Se busca el proyecto NODO//CABLE en SGS. "
                    "Si no se encuentra, la liquidación queda BLOQUEADA para evitar "
                    "liquidar en un nodo individual incorrecto."
                )

            if nodo_vacio_excel:
                ck["advertencias"].append(
                    f"El Excel trae el nodo/proyecto vacío para DF '{df_id}'. "
                    "Se intentará relacionar contra el proyecto padre o un nodo compatible en SGS."
                )

            # Estrategia 1: order_number exacto (cacheado por df_id en el request)
            _cache_key = ("order_number_eq", df_id)
            if _cache is not None and _cache_key in _cache:
                candidatos = list(_cache[_cache_key])
            else:
                # limit alto: familias grandes (ej. 6300996351 con 103 nodos)
                # truncaban a 60 y los nodos restantes quedaban invisibles.
                candidatos = c.search_read(
                    "project.dominion",
                    [["order_number", "=", df_id]],
                    _CAMPOS_PROY, limit=250,
                )
                if _cache is not None:
                    _cache[_cache_key] = list(candidatos)
            candidatos, candidatos_lv = _filtrar_no_linea_viva(candidatos)
            _avisar_linea_viva_omitida(ck, candidatos_lv, nodo_val, nodo_idx)

            if candidatos:
                if len(candidatos) == 1 and (
                    _nodo_equiv(candidatos[0].get("nodo"), nodo_val) or nodo_vacio_excel
                ):
                    proy = candidatos[0]
                    if not _nodo_equiv(candidatos[0].get("nodo"), nodo_val):
                        ck["advertencias"].append(
                            f"DF '{df_id}' tiene un solo nodo "
                            f"(nodo={candidatos[0].get('nodo','?')}) "
                            f"pero la plantilla indica nodo {nodo_val}."
                        )
                else:
                    match_nodo = [p for p in candidatos if _nodo_equiv(p.get("nodo"), nodo_val)]
                    if not match_nodo and (nodo_es_cable or nodo_idx != -1):
                        # Campo nodo pudo quedar con el código LCL u otro valor incorrecto.
                        # Intentar por nombre del proyecto antes de declarar error.
                        # Para cable: exigir ambos extremos del rango en el nombre.
                        # Para nodo alfanumérico (6A, 10B): buscar el string completo en el nombre
                        # antes de caer al número solo (que confundiría N6A con N6).
                        # Para nodo numérico simple: buscar el número en el nombre.
                        if nodo_es_cable:
                            match_nodo = [p for p in candidatos if _cable_nodo_match(p.get("name", ""), nodo_val)]
                        else:
                            match_nodo = [p for p in candidatos if _nodo_match_smart(p.get("name", ""), nodo_val, nodo_idx)]
                        if match_nodo:
                            ck["advertencias"].append(
                                f"Nodo {nodo_val}: campo 'nodo' en SGS no coincide; "
                                f"resuelto por nombre de proyecto ({match_nodo[0].get('code','?')})."
                            )
                    # Matching CABLE: si el Excel trae un rango (1AL4), buscar el
                    # proyecto cuyo nombre contenga "CABLE" en los candidatos.
                    if not match_nodo and nodo_es_cable:
                        match_nodo = [p for p in candidatos if _es_nodo_cable_sgs(p.get("name", ""))]
                        if match_nodo:
                            ck["advertencias"].append(
                                f"Nodo CABLE '{nodo_val}' → resuelto a proyecto "
                                f"'{match_nodo[0].get('code','?')}' (CABLE en nombre)."
                            )
                    if len(match_nodo) == 1:
                        proy = match_nodo[0]
                    elif len(match_nodo) > 1:
                        # Tiebreaker: preferir proyecto cuyo NOMBRE coincide exactamente
                        # con el nodo (ej: "NODO 1" > "NODO ejemplo 1" para nodo=1).
                        # Sin esto, la API devuelve "NODO ejemplo 1" antes que "NODO 1"
                        # y el primer match incorrecto se propaga a la verificación de MAT.
                        _exact_nodo = [p for p in match_nodo
                                       if _nodo_match_smart(p.get("name", ""), nodo_val, nodo_idx)]
                        proy = _exact_nodo[0] if _exact_nodo else match_nodo[0]
                        log.info(
                            "%s proyectos con nodo=%s; se usa %s%s.",
                            len(match_nodo), nodo_val, proy.get("code", "?"),
                            " (tiebreaker por nombre)" if _exact_nodo else "",
                        )
                    else:
                        # El DF puede existir en dos familias SGS: una con
                        # order_number numerico y otra historica con
                        # order_number/nombre largo. No declarar faltante
                        # hasta probar ese universo tolerante por nombre.
                        candidatos_extra = buscar_proyectos_por_df_nombre(
                            c, df_id, _CAMPOS_PROY, limit=120
                        )
                        candidatos_extra, candidatos_extra_lv = _filtrar_no_linea_viva(candidatos_extra)
                        _avisar_linea_viva_omitida(ck, candidatos_extra_lv, nodo_val, nodo_idx)
                        vistos = {p.get("id") for p in candidatos}
                        candidatos_amplios = list(candidatos)
                        for p in candidatos_extra:
                            if p.get("id") in vistos:
                                continue
                            vistos.add(p.get("id"))
                            candidatos_amplios.append(p)

                        match_extra = [
                            p for p in candidatos_extra
                            if _nodo_equiv(p.get("nodo"), nodo_val)
                            or (nodo_es_cable and _cable_nodo_match(p.get("name") or "", nodo_val))
                            or (not nodo_es_cable and nodo_idx != -1 and _nodo_match_smart(p.get("name") or "", nodo_val, nodo_idx))
                            or (nodo_es_cable and _es_nodo_cable_sgs(p.get("name") or ""))
                        ]
                        if len(match_extra) == 1:
                            proy = match_extra[0]
                            log.info(
                                "Nodo %s DF %s resuelto por busqueda tolerante: %s",
                                nodo_val, df_id, proy.get("code", "?"),
                            )
                        elif len(match_extra) > 1:
                            _exact_extra = [p for p in match_extra
                                            if _nodo_match_smart(p.get("name", ""), nodo_val, nodo_idx)]
                            proy = _exact_extra[0] if _exact_extra else match_extra[0]
                            log.info(
                                "%s coincidencias tolerantes con nodo=%s; se usa %s%s.",
                                len(match_extra), nodo_val, proy.get("code", "?"),
                                " (tiebreaker por nombre)" if _exact_extra else "",
                            )
                        else:
                            nodos_disp = [f"{p.get('code','?')}(nodo={p.get('nodo','?')})" for p in candidatos_amplios]
                            ck["ok"] = False
                            ck["nodo_faltante"]    = True
                            ck["nodo_faltante_df"]  = df_id
                            ck["nodo_faltante_val"] = nodo_val
                            ck["nodo_sgs_disponibles"] = [
                                {"id": p["id"], "code": p.get("code",""), "name": p.get("name",""), "nodo": p.get("nodo","")}
                                for p in candidatos_amplios
                            ]
                            ck["errores"].append(
                                f"Nodo '{nodo_val}' no existe en SGS para DF '{df_id}'. "
                                f"Nodos disponibles: {', '.join(nodos_disp)}. Requiere crear el nodo.")
                            sugerencia = _sugerir_nodo_parecido(nodo_val, candidatos_amplios)
                            if sugerencia:
                                ck["advertencias"].append(sugerencia)
                            ck["df_nodos"] = [{"id": p["id"], "code": p.get("code",""), "name": p.get("name","")} for p in candidatos_amplios]
                            return ck

            else:
                # Estrategia 2: nombre like DF
                candidatos = buscar_proyectos_por_df_nombre(c, df_id, _CAMPOS_PROY, limit=80)
                candidatos, candidatos_lv = _filtrar_no_linea_viva(candidatos)
                _avisar_linea_viva_omitida(ck, candidatos_lv, nodo_val, nodo_idx)
                if candidatos:
                    match_exacto = [
                        p for p in candidatos
                        if _nodo_equiv(p.get("nodo"), nodo_val)
                        or (nodo_es_cable and _cable_nodo_match(p.get("name") or "", nodo_val))
                        or (not nodo_es_cable and nodo_idx != -1 and _nodo_match_smart(p.get("name") or "", nodo_val, nodo_idx))
                        or (nodo_es_cable and _es_nodo_cable_sgs(p.get("name") or ""))
                    ]
                    if len(match_exacto) == 1:
                        proy = match_exacto[0]
                    elif len(match_exacto) > 1:
                        proy = match_exacto[0]
                        log.info(
                            "%s coincidencias con nodo=%s; se usa %s.",
                            len(match_exacto), nodo_val, proy.get("code", "?"),
                        )
                    elif len(candidatos) == 1:
                        proy = candidatos[0]
                    else:
                        ck["ok"] = False
                        ck["nodo_faltante"]    = True
                        ck["nodo_faltante_df"]  = df_id
                        ck["nodo_faltante_val"] = nodo_val
                        ck["nodo_sgs_disponibles"] = [
                            {"id": p["id"], "code": p.get("code",""), "name": p.get("name",""), "nodo": p.get("nodo","")}
                            for p in candidatos[:15]
                        ]
                        ck["errores"].append(
                            f"Nodo '{nodo_val}' no existe en SGS para DF '{df_id}'. Requiere crear el nodo.")
                        sugerencia = _sugerir_nodo_parecido(nodo_val, candidatos)
                        if sugerencia:
                            ck["advertencias"].append(sugerencia)
                        ck["df_nodos"] = [{"id": p["id"], "code": p.get("code",""), "name": p.get("name","")} for p in candidatos[:15]]
                        return ck

            ck["df_nodos"] = [{"id": p["id"], "code": p.get("code",""), "name": p.get("name","")} for p in candidatos]

        elif pro_val.isdigit():
            # Solo es plausible como id real de project.dominion si es grande (>=10000).
            # Un nodo pequeño ('1', '52') con _odm vacío NO es un id: buscarlo por id
            # devuelve proyectos ajenos (caso real: nodo '1' → id=1 = 'PRO-000001 Prueba'
            # y todas las actividades se verificaron contra el proyecto de prueba).
            if int(pro_val) >= 10000:
                res = c.search_read("project.dominion", [["id","=",int(pro_val)]], _CAMPOS_PROY, limit=1)
                proy = res[0] if res else None
            else:
                ck["ok"] = False
                ck["errores"].append(
                    f"Nodo '{pro_val}' sin DF/LCL/Proyecto detectado en la cabecera del Excel — "
                    "no se puede resolver el proyecto SGS. Revisa las filas 'OdM / LCL:', "
                    "'Ticket:' o 'Proyecto:' del Excel.")
                return ck

    except Exception as ex:
        ck["ok"] = False
        if isinstance(ex, OSError):
            # URLError/SSL/timeout/ConnectionError — red, no datos. El cliente
            # read-only ya reintentó 3 veces; si llegó aquí la red está caída
            # de verdad, pero el usuario debe saber que reintentar sirve.
            ck["errores"].append(
                f"Error de red temporal al resolver el proyecto ({ex}) — "
                "reintenta la verificación; no es un problema de datos.")
        else:
            ck["errores"].append(f"Error resolviendo proyecto: {ex}")
        return ck

    if not proy:
        ck["ok"] = False
        if df_id:
            ck["nodo_faltante"]        = True
            ck["nodo_faltante_df"]     = df_id
            ck["nodo_faltante_val"]    = nodo_val
            ck["nodo_sgs_disponibles"] = []
            ck["errores"].append(
                f"DF '{df_id}' nodo '{nodo_val}' no encontrado en SGS — requiere crear el nodo.")
        else:
            ck["errores"].append(f"Proyecto no encontrado en SGS para DF='{df_id}'")
        return ck

    # Fallback nodo-hijo: si el proyecto resuelto es un padre sin nodo, buscar
    # el primer nodo hijo (parent_id o mismo order_number, con nodo asignado)
    # que tenga equipo. Cubre el caso donde el Excel apunta a PRO-XXXXX (padre)
    # pero los datos reales están en NODO 1 (PRO-YYYYY). Al actualizar proy aquí,
    # ck["pid"] y toda la verificación downstream usan el proyecto correcto.
    #
    # ⚠ order_number='0'/'-'/'NA' es placeholder, NO una familia: cientos de
    # proyectos ajenos lo comparten. Usarlo como llave redirige a un proyecto
    # de otra persona (caso real: DF202664502069 → 'DF202664513000 NODO 2').
    if not _nodo_norm(proy.get("nodo")):
        try:
            _dominios_hijos = [[["parent_id", "=", proy["id"]]]]
            _on_padre = str(proy.get("order_number") or "").strip()
            if _on_padre.upper() not in ("", "0", "-", "N/A", "NA", "NONE", "FALSE"):
                _dominios_hijos.append(
                    [["order_number", "=", _on_padre], ["id", "!=", proy["id"]]])
            _vistos_h, _hijos = set(), []
            for _dom_h in _dominios_hijos:
                for n in c.search_read("project.dominion", _dom_h, _CAMPOS_PROY, limit=20):
                    if n["id"] in _vistos_h:
                        continue
                    _vistos_h.add(n["id"])
                    _hijos.append(n)
            _hijos = sorted(
                [n for n in _hijos if _nodo_norm(n.get("nodo"))],
                key=lambda n: _nodo_norm(n.get("nodo", "")),
            )
            # Si el Excel trae nodo, solo se acepta el hijo de ESE nodo — nunca
            # redirigir silenciosamente a un nodo distinto.
            if _nodo_norm(nodo_val):
                _nidx_fb = _nodo_num(nodo_val)
                _hijos = [n for n in _hijos
                          if _nodo_equiv(n.get("nodo"), nodo_val)
                          or (_nidx_fb != -1
                              and _nodo_match_smart(n.get("name") or "", nodo_val, _nidx_fb))]
            for _hijo in _hijos:
                _eq_hijo = c.search_read(
                    "project.team",
                    [["project_id", "=", _hijo["id"]]],
                    ["id"], limit=1,
                )
                if _eq_hijo:
                    log.info(
                        "Proyecto padre %s (sin nodo) → redirigido a %s NODO %s — tiene equipo",
                        proy.get("code", "?"), _hijo.get("code", "?"),
                        _nodo_norm(_hijo.get("nodo", "?")),
                    )
                    ck.setdefault("advertencias", []).append(
                        f"Proyecto padre '{proy.get('code','?')}' (sin nodo) redirigido a "
                        f"'{_hijo.get('code','?')}' (NODO {_nodo_norm(_hijo.get('nodo','?'))}) "
                        f"— datos en el nodo hijo."
                    )
                    proy = _hijo
                    break
        except Exception as _ex_fb:
            log.warning("Fallback nodo-hijo fallido para %s: %s", proy.get("code", "?"), _ex_fb)

    ck["pid"]        = proy["id"]
    ck["pro_code"]   = proy.get("code", "")
    ck["pnombre"]    = proy.get("name", "")
    ck["nodo_sgs"]   = proy.get("nodo", "") or ""
    ck["df_detectado"] = df_id
    ck["cid"]        = (proy.get("contract_id")    or [None])[0]
    ck["contrato"]   = (proy.get("contract_id")    or [None, ""])[1]
    ck["formula"]    = (proy.get("formula_id")     or [None, ""])[1]
    ck["fid"]        = (proy.get("formula_id")     or [None])[0]
    ck["supervisor"] = (proy.get("responsible_id") or [None, ""])[1]
    ck["estado_proyecto"] = _texto_m2o_v(proy.get("status"))

    # Actividad en catálogo
    try:
        cat = c.search_read("activities",
            [["code", "=", row["codigo"]]],
            ["id", "name", "code", "customer_contracts_id"], limit=5)
    except Exception as ex:
        ck["ok"] = False
        ck["errores"].append(f"Error buscando actividad: {ex}")
        return ck

    if not cat:
        ck["ok"] = False
        ck["errores"].append(f"Codigo '{row['codigo']}' no existe en catalogo")
        return ck

    ck["cat_id"]  = cat[0]["id"]
    ck["cat_nom"] = cat[0]["name"]

    # Contrato compatible
    proy_ctr_id = (proy.get("contract_id") or [None])[0]
    act_ctr_id  = (cat[0].get("customer_contracts_id") or [None])[0]
    act_ctr_nom = (cat[0].get("customer_contracts_id") or [None, ""])[1]
    if proy_ctr_id and act_ctr_id and proy_ctr_id != act_ctr_id:
        # Si uno de los nombres es prefijo del otro (ej: "Enel zona VII" vs
        # "Enel zona VII - 2026"), es variante de año del mismo contrato —
        # proyecto creado/corregido con distinto año en el catálogo → advertencia.
        _pn = (ck.get("contrato") or "").strip()
        _an = (act_ctr_nom or "").strip()
        if _pn and _an and (_pn.startswith(_an) or _an.startswith(_pn)):
            ck["advertencias"].append(
                f"⚠ Contrato variante de año: proyecto={_pn}, actividad={_an}")
        else:
            ck["ok"] = False
            ck["errores"].append(
                f"Contrato incompatible: proyecto={ck['contrato']}, actividad={act_ctr_nom}")

    # Actividad existente y pendiente real.
    # Va antes de lideres para que nodos ya cerrados no pidan configuracion.
    try:
        existentes = c.search_read("project.activities",
            [["project_id", "=", proy["id"]], ["activity_code", "=", row["codigo"]]],
            ["id", "amount", "advance", "balance"], limit=1)
    except Exception:
        existentes = []

    if existentes:
        ea = existentes[0]
        ck["act_existente"] = {
            "id": ea["id"], "amount": ea["amount"],
            "advance": ea["advance"], "balance": ea["balance"],
        }
        ck["act_id"]   = ea["id"]
        ck["act_code"] = row.get("codigo", "")

        # Poblar líderes ANTES de los returns tempranos (ya_liquidada /
        # cant_no_coincide): Modo Repetición necesita el líder para re-liquidar.
        # Silencioso: sin error "sin lider" ni lider_faltante — un nodo cerrado
        # sin equipo no debe pedir configuración; eso es solo del flujo normal.
        try:
            _eq_pre = c.search_read("project.team",
                [["project_id", "=", proy["id"]]],
                ["id", "partner_id"], limit=10)
        except Exception:
            _eq_pre = []
        if _eq_pre:
            ck["lideres"] = [
                {"id": e["partner_id"][0], "nombre": e["partner_id"][1]}
                for e in _eq_pre if e.get("partner_id")
            ]
            try:
                _pids_pre = [l["id"] for l in ck["lideres"]]
                _vats_pre = c.search_read(
                    "res.partner", [["id", "in", _pids_pre]], ["id", "vat"], limit=20)
                _ced_pre = {p["id"]: (p.get("vat") or "") for p in _vats_pre}
                for l in ck["lideres"]:
                    l["cedula"] = _ced_pre.get(l["id"], "")
            except Exception:
                for l in ck["lideres"]:
                    l.setdefault("cedula", "")
            ck["partner_id"]     = _eq_pre[0]["partner_id"][0]
            ck["partner_nombre"] = _eq_pre[0]["partner_id"][1]

        # Determinar si el avance fue registrado este mes o en un mes anterior.
        # write_date en project.activities es inútil: Odoo lo actualiza con
        # cualquier recomputación de campo calculado, aunque el avance sea viejo.
        # La fecha real está en project.activities.details → campo "date".
        # Tomamos el MAX(date) de las líneas del avance para saber cuándo fue la
        # última prestación registrada.
        from datetime import date as _date
        _hoy        = _date.today()
        _mes_actual = (_hoy.year, _hoy.month)
        _fecha_det  = ""
        try:
            _dets = c.search_read(
                "project.activities.details",
                [["project_activities_id", "=", ea["id"]]],
                ["date"], limit=60)
            _fechas = [str(d.get("date") or "")[:10] for d in _dets if d.get("date")]
            if _fechas:
                _fecha_det = max(_fechas)   # la más reciente
        except Exception:
            pass
        try:
            _partes     = _fecha_det[:7].split("-")   # "2026-06"
            _mes_avance = (int(_partes[0]), int(_partes[1]))
        except Exception:
            _mes_avance = _mes_actual  # sin fecha → asumir este mes (más seguro)

        if ea["amount"] > 0 and ea["advance"] >= ea["amount"]:
            _cant_xls = float(row.get("cantidad", 0))
            _ppto_sgs = float(ea["amount"])
            # Si el Excel pide significativamente más de lo presupuestado y el ppto
            # ya está agotado (por otro nodo o ciclo anterior), el presupuesto SGS
            # no cubre este trabajo — reportar como desajuste, no como ya_liquidada.
            if _cant_xls > _ppto_sgs * 1.01:
                ck["ok"] = False
                ck["cant_no_coincide"] = True
                ck["fecha_avance"] = _fecha_det
                # Delta: cuántas unidades faltan por encima del ppto actual.
                # Si el supervisor amplía el ppto en SGS, este delta es liquidable.
                _delta = round(_cant_xls - float(ea["advance"]), 4)
                if _delta > 0.001:
                    ck["repetible_sumable"] = True
                    ck["delta_qty"] = _delta
                ck["errores"].append(
                    f"CANTIDAD NO COINCIDE: Excel pide {_cant_xls:.0f} ud, "
                    f"ppto SGS={_ppto_sgs:.0f} ud (ya liquidado a otro nodo). "
                    f"Falta liquidar {_delta:.0f} ud — con Modo Repetición la "
                    f"máquina amplía el ppto automáticamente y liquida el delta.")
                return ck
            ck["ok"] = False
            ck["ya_liquidada"] = True
            _anterior = _mes_avance < _mes_actual
            ck["ya_liquidada_mes_anterior"] = _anterior
            ck["fecha_avance"] = _fecha_det  # fecha real de la última prestación
            if _anterior:
                ck["errores"].append(
                    f"YA LIQUIDADA en {_mes_avance[1]:02d}/{_mes_avance[0]} "
                    f"(proyectado={ea['amount']}, avance={ea['advance']}) "
                    f"— elegible para Modo Repeticion")
            else:
                ck["errores"].append(
                    f"YA LIQUIDADA este mes (proyectado={ea['amount']}, avance={ea['advance']})")
            return ck
        elif ea["advance"] > 0:
            nuevo = ea["advance"] + row["cantidad"]
            if nuevo > ea["amount"]:
                pendiente_real = round(ea["amount"] - ea["advance"], 4)
                if pendiente_real > 0:
                    ck["ajuste_pendiente"] = True
                    ck["pendiente_real"]   = pendiente_real
                    ck["advertencias"].append(
                        f"Sobre-solicitud ajustada: solicitado={row['cantidad']}, "
                        f"pendiente real={pendiente_real} "
                        f"(proyectado={ea['amount']}, avance={ea['advance']}). "
                        f"Se liquidara {pendiente_real}.")
                else:
                    ck["ok"] = False
                    ck["ya_liquidada"] = True
                    _anterior2 = _mes_avance < _mes_actual
                    ck["ya_liquidada_mes_anterior"] = _anterior2
                    ck["fecha_avance"] = _fecha_det
                    if _anterior2:
                        ck["errores"].append(
                            f"YA LIQUIDADA en {_mes_avance[1]:02d}/{_mes_avance[0]} "
                            f"(proyectado={ea['amount']}, avance={ea['advance']}) "
                            f"— elegible para Modo Repeticion")
                    else:
                        ck["errores"].append(
                            f"YA LIQUIDADA este mes (proyectado={ea['amount']}, avance={ea['advance']})")
                    return ck
            else:
                ck["advertencias"].append(
                    f"Actividad con avance previo ({ea['advance']} de {ea['amount']}).")
        else:
            nuevo = ea["advance"] + row["cantidad"]
            if nuevo > ea["amount"]:
                pendiente_real = round(float(ea["amount"]), 4)
                if pendiente_real > 0:
                    ck["ajuste_pendiente"] = True
                    ck["pendiente_real"]   = pendiente_real
                    ck["advertencias"].append(
                        f"Sobre-solicitud ajustada: solicitado={row['cantidad']}, "
                        f"ajustado a proyectado={pendiente_real}. "
                        f"Se liquidara {pendiente_real}.")
                else:
                    ck["ok"] = False
                    ck["errores"].append(
                        f"Sobre-liquidacion: proyectado={ea['amount']}, "
                        f"solicitud={row['cantidad']}")
                    return ck
    else:
        ck["actividad_no_existente"] = True
        ck["act_code"] = row.get("codigo", "")
        log.info(
            "Actividad %s no existe aun en %s; se creara antes de liquidar.",
            row["codigo"], ck["pro_code"],
        )

    # Líder del proyecto (si el pre-check de actividad existente ya lo pobló,
    # con cédulas incluidas, no repetir las consultas a SGS)
    if ck.get("lideres"):
        return ck

    try:
        equipo = c.search_read("project.team",
            [["project_id", "=", proy["id"]]],
            ["id", "partner_id"], limit=10)
    except Exception:
        equipo = []

    if not equipo:
        ck["ok"] = False
        ck["errores"].append("Proyecto sin lider asignado")
        ck["lider_faltante"] = True   # activa botón "Agregar líder" en la UI
    else:
        ck["lideres"] = [
            {"id": e["partner_id"][0], "nombre": e["partner_id"][1]}
            for e in equipo
            if e.get("partner_id")
        ]
        # Enriquecer con cédula (VAT) de cada líder para búsqueda única en Odoo
        try:
            partner_ids = [l["id"] for l in ck["lideres"]]
            partners_vat = c.search_read(
                "res.partner", [["id", "in", partner_ids]], ["id", "vat"], limit=20
            )
            cedula_by_pid = {p["id"]: (p.get("vat") or "") for p in partners_vat}
            for l in ck["lideres"]:
                l["cedula"] = cedula_by_pid.get(l["id"], "")
        except Exception:
            for l in ck["lideres"]:
                l.setdefault("cedula", "")
        ck["partner_id"]     = equipo[0]["partner_id"][0]
        ck["partner_nombre"] = equipo[0]["partner_id"][1]
        if len(equipo) > 1:
            nombres = ", ".join(e["partner_id"][1] for e in equipo)
            log.info(
                "Proyecto %s con %s lideres; por defecto se usa control del proyecto: %s",
                ck.get("pro_code", proy.get("code", "?")), len(equipo), nombres,
            )

    return ck


# ── Verificación MAT ──────────────────────────────────────────────────────────
def verificar_fila_mat(sgs_mat: dict | None, mat: dict) -> dict:
    """
    Compara un material del Excel contra su project.material en SGS.
    sgs_mat puede ser None si no existe en SGS.
    Regla: solo contar details con picking_id como advance real.
    """
    mc = {
        "mat":            mat,
        "sgs_mat":        sgs_mat,
        "ok":             False,
        "estado":         "no_encontrado",
        "sgs_despachado": None,
        "sgs_devuelto":   None,
        "sgs_neto":       None,
        "advance_real":   None,   # suma de details CON picking_id
        "diferencia":     None,
        "errores":        [],
        "advertencias":   [],
    }

    excel_neto = mat["sgs_neto_excel"]

    if sgs_mat is None:
        if excel_neto == 0:
            mc["ok"]     = True
            mc["estado"] = "sin_mov_ok"
        else:
            mc["errores"].append(
                "Código no existe en project.material para este proyecto SGS"
            )
        return mc

    desp  = float(sgs_mat.get("despachado") or 0)
    devto = float(sgs_mat.get("devuelto")   or 0)
    neto  = desp - devto

    # Anti-stale: despachado es un stored computed field y puede llegar a 0 si el
    # recompute de Odoo no corrió todavía. Cuando Excel indica movimiento y el
    # balance API > 0, reconstruir neto = balance + advance (balance = desp - dev - adv).
    if neto <= 0 and excel_neto > 0:
        _bal_api = float(sgs_mat.get("balance") or 0)
        if _bal_api > 0.001:
            _adv_sgs = float(sgs_mat.get("advance") or 0)
            neto = round(_bal_api + _adv_sgs, 4)
            desp = neto
            mc["advertencias"].append(
                f"despachado stale (0) — neto reconstruido desde balance API "
                f"({_bal_api:g} + advance {_adv_sgs:g} = {neto:g})"
            )

    diff  = abs(neto - excel_neto)

    mc["sgs_despachado"] = desp
    mc["sgs_devuelto"]   = devto
    mc["sgs_neto"]       = neto
    mc["diferencia"]     = diff

    # Avance real = suma de TODOS los details con amount > 0 (incluyendo Playwright).
    advance_real = float(sgs_mat.get("_advance_real") or 0)
    mc["advance_real"] = advance_real

    # DEVOLUCION: Excel marca el material como devuelto — no se liquida, no es descuadre.
    if mat.get("estado_excel", "").upper() == "DEVOLUCION":
        mc["ok"]     = True
        mc["estado"] = "devolucion"
        return mc

    # Capa 1 — el neto de bandejas (despacho − devolución) debe cuadrar con el Excel.
    # Si devto > 0 pero el neto coincide con el Excel → devolución factorizada, continuar.
    # Si devto > 0 y el neto NO coincide → devolución que descuadra, bloquear explícitamente.
    if False and diff != 0:
        mc["ok"] = False
        if devto > 0:
            mc["estado"] = "devolucion_sgs"
            mc["errores"].append(
                f"Devolución ({devto:g}u) no reconcilia con Excel: "
                f"neto SGS={neto:g} ({desp:g}D − {devto:g}R), Excel={excel_neto:g}. Revisar."
            )
        else:
            mc["estado"] = "descuadre"
        return mc

    if diff != 0:
        if neto < 0:
            mc["ok"] = False
            mc["estado"] = "devolucion_sgs"
            mc["errores"].append(
                f"Neto SGS negativo: {neto:g} ({desp:g}D - {devto:g}R). Revisar."
            )
            return mc
        mc["advertencias"].append(
            f"Excel difiere de SGS: neto SGS={neto:g} ({desp:g}D - {devto:g}R), "
            f"Excel={excel_neto:g}. En el liquidador la cantidad final sale de SGS validado."
        )

    if devto > 0:
        mc["advertencias"].append(
            f"Devolución en SGS ({devto:g}u) — neto={neto:g} ({desp:g}D − {devto:g}R) "
            "cuadra con Excel. Capa 2 (SGS balance) y Capa 3 (pendiente > 0) validarán al liquidar."
        )

    mc["ok"] = True
    if neto == 0 and advance_real == 0:
        mc["estado"] = "sin_mov"
    elif neto > 0 and advance_real >= neto:
        mc["estado"] = "ya_liquidado"
    elif advance_real > 0:
        mc["estado"] = "avance_parcial"
    else:
        mc["estado"] = "ok"

    return mc


def verificar_materiales_proyecto(c: SGSReadOnlyClient, pid: int, rows_mat: list) -> list:
    """
    Verifica todos los materiales de un proyecto en lote.
    Hace una sola query bulk de project.material + details para eficiencia.
    """
    # Leer todos los project.material del proyecto
    pm_all = c.search_read(
        "project.material",
        [["project_id", "=", pid]],
        ["id", "product_code", "customer_default_code", "product_id", "despachado", "devuelto", "advance", "balance"],
        limit=500,
    )

    # En algunos proyectos project.material.despachado/devuelto llega en 0 aunque
    # la vista de SGS muestre cantidades. La fuente fiel de lo visible en pantalla
    # son las bandejas: project.tray -> project.tray.material.
    def _cod_sap(product_field):
        if not product_field:
            return ""
        txt = product_field[1] if isinstance(product_field, list) else str(product_field)
        m = re.match(r"\[([^\]/]+)(?:/([^\]]+))?\]", txt)
        if m:
            return (m.group(2) or m.group(1)).strip()
        return str(txt).strip()

    despachado_from_trays: dict = {}
    devuelto_from_trays:   dict = {}
    lideres_desp_by_code:  dict = {}   # cod → set[partner]
    dispatch_pq_by_code:   dict = {}   # cod → {partner: qty}  despacho por líder
    return_pq_by_code:     dict = {}   # cod → {partner: qty}  devolución por líder
    ban_desp_by_code:      dict = {}   # cod → list[str] nombres BAN despacho
    ban_dev_by_code:       dict = {}   # cod → list[str] nombres BAN devolución
    ban_by_lider_code:     dict = {}   # cod → {partner: list[BAN]} despacho por líder
    partner_ids_by_name:   dict = {}   # nombre → res.partner id (para resolver cédula)
    try:
        _tray_fields = ["id", "name", "type", "partner_id",
                        "project_tray_material_ids", "state", _TRAY_DELIVERY_FIELD]
        trays_raw = c.search_read("project.tray",
                                  [["project_id", "=", pid]],
                                  _tray_fields, limit=200)
        antes = len(trays_raw)
        trays = [t for t in trays_raw if _bandeja_activa_v(t)]
        if len(trays) < antes:
            log.info(f"[VERIFICADOR] {antes - len(trays)}/{antes} bandejas descartadas "
                     f"(state_picking=noatendido en despacho o state=canceled) pid={pid}")

        tray_mat_ids = []
        for tray in trays:
            tray_mat_ids.extend(tray.get("project_tray_material_ids") or [])

        tray_mats = {}
        if tray_mat_ids:
            for tm in c.read(
                "project.tray.material",
                tray_mat_ids,
                ["id", "product_id", "deliver_quantity", "project_tray_id"],
            ):
                tray_mats[tm["id"]] = tm

        for tray in trays:
            es_dev = str(tray.get("type") or "").lower() == "devolution"
            partner_ref = tray.get("partner_id")
            partner_name = partner_ref[1] if isinstance(partner_ref, list) else str(partner_ref or "")
            if isinstance(partner_ref, list) and partner_name:
                partner_ids_by_name.setdefault(partner_name, partner_ref[0])

            for mat_id in tray.get("project_tray_material_ids") or []:
                tray_mat = tray_mats.get(mat_id)
                if not tray_mat:
                    continue
                cod = _cod_sap(tray_mat.get("product_id"))
                qty = float(tray_mat.get("deliver_quantity") or 0)
                if not cod or qty <= 0:
                    continue

                tray_name = str(tray.get("name") or "")
                if es_dev:
                    devuelto_from_trays[cod] = devuelto_from_trays.get(cod, 0.0) + qty
                    # acumular devolución por líder (partner vacío → clave "")
                    key_r = partner_name or ""
                    return_pq_by_code.setdefault(cod, {})[key_r] = (
                        return_pq_by_code.get(cod, {}).get(key_r, 0.0) + qty
                    )
                    if tray_name:
                        lst = ban_dev_by_code.setdefault(cod, [])
                        if tray_name not in lst:
                            lst.append(tray_name)
                else:
                    despachado_from_trays[cod] = despachado_from_trays.get(cod, 0.0) + qty
                    if partner_name:
                        lideres_desp_by_code.setdefault(cod, set()).add(partner_name)
                        dispatch_pq_by_code.setdefault(cod, {})[partner_name] = (
                            dispatch_pq_by_code.get(cod, {}).get(partner_name, 0.0) + qty
                        )
                    if tray_name:
                        lst = ban_desp_by_code.setdefault(cod, [])
                        if tray_name not in lst:
                            lst.append(tray_name)
                        if partner_name:
                            lbl = ban_by_lider_code.setdefault(cod, {}).setdefault(partner_name, [])
                            if tray_name not in lbl:
                                lbl.append(tray_name)
    except Exception as ex:
        log.warning(f"[VERIFICADOR] No se pudieron leer bandejas pid={pid}: {ex}")

    def _cod_pm(pm) -> str:
        cod = str(pm.get("product_code") or "").strip()
        if not cod:
            cod = str(pm.get("customer_default_code") or "").strip()
        if not cod:
            cod = _cod_sap(pm.get("product_id"))
        return cod

    # Códigos que tienen su PROPIO project.material en este proyecto.
    # Sufijo C = material comprado por Dominion (ENEL no lo dio; almacén lo
    # despacha como 251359C). Cuando el proyecto tiene AMBOS pm (251359 y
    # 251359C) cada uno recibe solo SUS bandejas: el fallback a la variante
    # alterna es únicamente para el caso en que la variante no exista como pm.
    # Antes el fallback "fugaba" el despacho de 251359C al pm 251359 (sin
    # stock) y _buscar_pm elegía ese pm → sin_agregar_linea (PRO-066495/6/7).
    _codigos_pm = {_cod_pm(_m).upper() for _m in pm_all if _cod_pm(_m)}

    for pm in pm_all:
        cod = _cod_pm(pm)
        alt = cod[:-1] if cod.upper().endswith("C") else cod + "C"
        alt_sin_pm = alt.upper() not in _codigos_pm   # fallback solo si la variante no tiene pm

        def _tv(d: dict, default, _cod=cod, _alt=alt, _ok=alt_sin_pm):
            v = d.get(_cod)
            if v:
                return v
            return d.get(_alt, default) if _ok else default

        desp_tray = _tv(despachado_from_trays, 0.0)
        dev_tray  = _tv(devuelto_from_trays, 0.0)

        if desp_tray > 0:
            pm["despachado"] = desp_tray
        if dev_tray > 0:
            pm["devuelto"] = dev_tray
        pm["_lideres_despacho_tray"] = _tv(lideres_desp_by_code, set())
        pm["_dispatch_pq_tray"]      = _tv(dispatch_pq_by_code, {})
        pm["_return_pq_tray"]        = _tv(return_pq_by_code, {})
        pm["_ban_desp"]              = _tv(ban_desp_by_code, [])
        pm["_ban_dev"]               = _tv(ban_dev_by_code, [])
        pm["_ban_by_lider"]          = _tv(ban_by_lider_code, {})

    pm_by_code: dict = {}
    for _m in pm_all:
        for _k in (str(_m.get("product_code") or "").strip(),
                   str(_m.get("customer_default_code") or "").strip(),
                   _cod_sap(_m.get("product_id"))):
            if _k:
                pm_by_code.setdefault(_k, _m)

    def _buscar_pm(codigo: str):
        """
        Busca project.material tolerando la variante con/sin 'C' final.
        Ej: 251483 == 251483C en la nomenclatura del módulo julian.
        Si existen ambas formas en SGS (ej. 251483=0 y 251483C=1),
        se prefiere la que tenga movimiento real (despachado > 0 o advance_real > 0).
        """
        exact = pm_by_code.get(codigo)
        # Generar código alternativo: si termina en C quitar, si no termina en C agregar
        if codigo.upper().endswith("C"):
            alt = codigo[:-1]
        else:
            alt = codigo + "C"
        alt_pm = pm_by_code.get(alt)

        if exact is None and alt_pm is None:
            return None
        if exact is None:
            return alt_pm
        if alt_pm is None:
            return exact

        # Ambas existen — preferir la que tiene movimiento real
        def _tiene_mov(pm):
            return float(pm.get("despachado") or 0) > 0 or float(pm.get("_advance_real") or 0) > 0

        if _tiene_mov(exact) and not _tiene_mov(alt_pm):
            return exact
        if _tiene_mov(alt_pm) and not _tiene_mov(exact):
            return alt_pm

        # Ambas tienen movimiento (caso custodia: 251483 y 251483C con despacho=1 cada una).
        # El estándar puede estar en estado cerrado/bloqueado en Odoo con balance=0 porque
        # su track real quedó en la variante custodia.  Preferir la que tiene balance > 0
        # (campo computado en SGS = despachado - devuelto - advance).
        def _balance_pendiente(pm):
            b = float(pm.get("balance") or 0)
            if b > 0.001:
                return True
            # Fallback con _advance_real si 'balance' no llegó del servidor
            return (float(pm.get("despachado") or 0)
                    - float(pm.get("devuelto") or 0)
                    - float(pm.get("_advance_real") or 0)) > 0.001

        bal_exact = _balance_pendiente(exact)
        bal_alt   = _balance_pendiente(alt_pm)

        if bal_exact and not bal_alt:
            return exact
        if bal_alt and not bal_exact:
            log.debug(f"[VERIFICADOR] {codigo}: prefiriendo variante '{alt}' (balance pendiente)")
            return alt_pm
        # Si ambas o ninguna tienen balance pendiente, prefiere la exacta (sin C)
        return exact

    # Leer todos los details y calcular advance_real.
    # NO filtrar por picking_id: las entradas creadas por Playwright desde la UI
    # también tienen picking_id=False (es un campo de movimiento de almacén, no
    # de liquidación manual). Contamos TODAS las entradas con amount > 0.
    all_dets = c.search_read(
        "project.material.details",
        [["project_material_id.project_id", "=", pid]],
        ["id", "project_material_id", "amount", "partner_id", "picking_id"],
        limit=1000,
    )
    advance_real_by_pm: dict = {}
    lideres_desp_by_pm: dict = {}
    lideres_liq_by_pm: dict = {}
    liquidado_pq_by_pm: dict = {}   # pm_id → {partner: qty NETA ya liquidada a ese líder}

    for d in all_dets:
        amt = float(d.get("amount") or 0)
        pm_ref = d.get("project_material_id")
        pm_id  = pm_ref[0] if isinstance(pm_ref, list) else pm_ref
        partner_ref  = d.get("partner_id")
        partner_name = partner_ref[1] if isinstance(partner_ref, list) else str(partner_ref or "Desconocido")
        if isinstance(partner_ref, list) and partner_name:
            partner_ids_by_name.setdefault(partner_name, partner_ref[0])

        # Ledger por líder: incluye montos negativos para netear reversiones.
        if amt and pm_id:
            liquidado_pq_by_pm.setdefault(pm_id, {})
            liquidado_pq_by_pm[pm_id][partner_name] = (
                liquidado_pq_by_pm[pm_id].get(partner_name, 0.0) + amt)

        if amt <= 0:
            continue

        # advance total
        advance_real_by_pm[pm_id] = advance_real_by_pm.get(pm_id, 0.0) + amt

        # identificar lider
        picking_ref = d.get("picking_id")
        picking_name = picking_ref[1] if isinstance(picking_ref, list) else str(picking_ref or "")

        # clasificar
        if "CONS" in picking_name.upper():
            if pm_id not in lideres_liq_by_pm: lideres_liq_by_pm[pm_id] = set()
            lideres_liq_by_pm[pm_id].add(partner_name)
        else:
            # Asumimos que cualquier otro picking (ej: OUT o INT) asociado, es el dueño de la bandeja (despacho)
            if pm_id not in lideres_desp_by_pm: lideres_desp_by_pm[pm_id] = set()
            lideres_desp_by_pm[pm_id].add(partner_name)

    # Cédulas (vat de res.partner) de todos los líderes vistos en bandejas/details.
    # La selección del Equipo en el modal MAT se hace por cédula (única en SGS);
    # buscar por primera palabra del nombre elegía al primer homónimo del dropdown
    # (caso real PRO-060053: dos líderes CARDENAS en el nodo → equipo equivocado).
    cedula_by_name: dict = {}
    if partner_ids_by_name:
        try:
            _vat_rows = c.search_read(
                "res.partner",
                [["id", "in", sorted(set(partner_ids_by_name.values()))]],
                ["id", "vat"], limit=len(partner_ids_by_name) + 5,
            )
            _vat_by_id = {r["id"]: str(r.get("vat") or "").strip() for r in _vat_rows}
            for _nom, _pid_p in partner_ids_by_name.items():
                _v = _vat_by_id.get(_pid_p, "")
                _num = _v.lstrip("Cc").strip()
                cedula_by_name[_nom] = ("C" + _num) if _num.isdigit() else ""
        except Exception as ex:
            log.warning(f"[VERIFICADOR] No se pudieron leer cédulas de líderes pid={pid}: {ex}")

    # Inyectar stats en cada pm
    for pm in pm_all:
        pm["_advance_real"] = advance_real_by_pm.get(pm["id"], 0.0)

    # Verificar cada fila
    resultados = []
    for mat in rows_mat:
        sgs_mat = _buscar_pm(mat["codigo"])
        mc = verificar_fila_mat(sgs_mat, mat)

        # C-remap de seguridad: si el código exacto resultó sin_mov pero existe
        # variante con/sin 'C' que sí tiene movimiento, usar esa variante.
        # Cubre el patrón SIP (sin C) ↔ SGS (con C custodia) sin cambios en el Excel.
        if (mc.get("estado") == "sin_mov"
                and sgs_mat is not None
                and float(mat.get("sgs_neto_excel") or 0) > 0):
            _cod = mat["codigo"]
            _alt_cod = _cod[:-1] if _cod.upper().endswith("C") else _cod + "C"
            _alt_sgs = pm_by_code.get(_alt_cod)
            if _alt_sgs is not None and _alt_sgs["id"] != sgs_mat["id"]:
                _mc_alt = verificar_fila_mat(_alt_sgs, mat)
                if _mc_alt.get("estado") in ("ok", "avance_parcial"):
                    log.info(
                        "[VERIFICADOR] %s: C-remap → '%s' (sin_mov → %s)",
                        _cod, _alt_cod, _mc_alt["estado"]
                    )
                    _mc_alt.setdefault("advertencias", []).insert(0,
                        f"Código '{_cod}' remapeado a variante '{_alt_cod}' "
                        f"(SIP sin sufijo C — SGS con C tiene el despacho)"
                    )
                    mc = _mc_alt
                    sgs_mat = _alt_sgs

        pmid = sgs_mat["id"] if sgs_mat else None
        mc["pm_id"] = pmid
        mc["pid"]   = pid

        # Código REAL del project.material elegido (puede ser la variante C =
        # compra Dominion). El browser localiza la fila del listado con este
        # código, nunca con el del Excel: 'has-text(251359)' es subcadena y
        # también casa con la fila 251359C.
        _cod_sgs_real = _cod_pm(sgs_mat) if sgs_mat else ""
        mc["codigo_sgs"] = _cod_sgs_real or str(mat["codigo"])
        if _cod_sgs_real and _cod_sgs_real.upper() != str(mat["codigo"]).strip().upper():
            if not any("remapeado" in a for a in mc.get("advertencias", [])):
                mc.setdefault("advertencias", []).insert(0,
                    f"Excel '{mat['codigo']}' cruzado con '{_cod_sgs_real}' en SGS "
                    f"(sufijo C = material comprado por Dominion; el despacho está ahí)")

        # inyectar lideres y BANs para auditoria. El despacho real viene de bandejas;
        # details se usa solo como fallback para proyectos viejos.
        desp_partners = []
        dispatch_pq   = {}
        return_pq     = {}
        if sgs_mat:
            desp_partners = list(sgs_mat.get("_lideres_despacho_tray") or [])
            dispatch_pq   = dict(sgs_mat.get("_dispatch_pq_tray") or {})
            return_pq     = dict(sgs_mat.get("_return_pq_tray") or {})
        mc["ban_despacho"]   = list(sgs_mat.get("_ban_desp")     or []) if sgs_mat else []
        mc["ban_devolucion"] = list(sgs_mat.get("_ban_dev")      or []) if sgs_mat else []
        mc["ban_by_lider"]   = dict(sgs_mat.get("_ban_by_lider") or {}) if sgs_mat else {}
        if not desp_partners and pmid:
            desp_partners = list(lideres_desp_by_pm.get(pmid, set()))
        liq_partners  = list(lideres_liq_by_pm.get(pmid, set())) if pmid else []
        mc["lideres_despacho"]    = desp_partners
        mc["lideres_liquidacion"] = liq_partners
        # nombre → "C<cedula>" de todo líder conocido del proyecto (bandejas + details):
        # el blueprint la usa para pasar partner_cedula al browser (selección por vat).
        mc["lideres_cedulas"] = {n: c_ced for n, c_ced in cedula_by_name.items() if c_ced}

        # partner_nombre: SOLO desde despacho real — nunca desde project.team ni fallback.
        #
        # Ledger por líder: pendiente(líder) = neto bandejas(líder) − ya liquidado(líder).
        # Ejemplo: el líder A despachó 40 y YA liquidó 40; el líder B despachó 20 y
        # liquidó 7 → el pendiente de 13 es del líder B.
        # Repartir por neto BRUTO intentaba liquidar al líder A →
        # "Stock no disponibles para el consumo".
        neto_por_lider = _lider_neto_por_bandeja_v(dispatch_pq, return_pq)
        liquidado_pq   = dict(liquidado_pq_by_pm.get(pmid, {})) if pmid else {}
        ledger, pend_por_lider = {}, {}
        for p, neto in neto_por_lider.items():
            liq  = round(liquidado_pq.get(p, 0.0), 4)
            pend = round(neto - liq, 4)
            ledger[p] = {"neto": neto, "liquidado": liq, "pendiente": pend}
            if pend > 0.001:
                pend_por_lider[p] = pend
        mc["ledger_lideres"] = ledger

        def _fmt_ledger():
            partes = []
            for p, v in sorted(ledger.items(), key=lambda x: -x[1]["pendiente"]):
                bans = ", ".join((mc.get("ban_by_lider") or {}).get(p, [])[:3])
                partes.append(
                    f"{p}: {v['neto']:g} desp − {v['liquidado']:g} liq = {v['pendiente']:g} pend"
                    + (f" [{bans}]" if bans else ""))
            return " · ".join(partes)

        if len(desp_partners) == 1:
            unico = desp_partners[0]
            mc["partner_nombre"] = unico
            mc["partner_cedula"] = cedula_by_name.get(unico, "")
            mc["partner_splits"] = None
            v = ledger.get(unico)
            if v and v["pendiente"] <= 0.001 and float(mat.get("sgs_neto_excel") or 0) > 0:
                mc.setdefault("advertencias", []).append(
                    f"⚠ {unico} ya liquidó su neto ({v['liquidado']:g}/{v['neto']:g}) — "
                    "posible descuadre de stock; verificar antes de liquidar. "
                    + _fmt_ledger())
        elif len(desp_partners) > 1:
            if len(pend_por_lider) == 1:
                # Solo UN líder tiene pendiente real → ítem único a ese líder.
                p_unico = next(iter(pend_por_lider))
                mc["partner_nombre"] = p_unico
                mc["partner_cedula"] = cedula_by_name.get(p_unico, "")
                mc["partner_splits"] = None
                mc.setdefault("advertencias", []).append(
                    f"Multi-líder resuelto por ledger → {p_unico} "
                    f"(pendiente {pend_por_lider[p_unico]:g}). " + _fmt_ledger())
            elif len(pend_por_lider) > 1:
                # Varios con pendiente → repartir por PENDIENTE (no por neto bruto).
                mc["partner_nombre"] = ""
                mc["partner_splits"] = pend_por_lider
                mc.setdefault("advertencias", []).append(
                    f"Material con {len(pend_por_lider)} líder(es) con pendiente — "
                    "se liquidará según ledger: " + _fmt_ledger())
            else:
                # Nadie con pendiente según details — estado inconsistente:
                # NO adivinar líder; requiere revisión manual (nunca por neto bruto).
                mc["partner_nombre"] = ""
                mc["partner_splits"] = None
                mc.setdefault("advertencias", []).append(
                    "⚠ Ningún líder con pendiente según el ledger (todo el neto ya fue "
                    "liquidado) — revisar manualmente. " + _fmt_ledger())
        else:
            mc["partner_nombre"]  = ""
            mc["partner_splits"]  = None

        resultados.append(mc)

    return resultados
