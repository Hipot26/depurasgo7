"""
export_plantilla_mat.py — Plantilla MASIVA de liquidación de MATERIALES
=======================================================================
Genera el .xlsx que se sube a **Liquidador 2** (`SGS UI/modulos/liquidador2`)
para liquidar, en una sola pasada, los materiales pendientes de VARIOS
proyectos seleccionados a mano en la pestaña "Automático por Mes".

Contrato con Liquidador 2 (verificado contra `parser_liquidador2.py`)
---------------------------------------------------------------------
`parsear_mat()` reconoce tres formatos, en este orden de prioridad:

    1. hojas `N<nodo>`   → un nodo por hoja, SIN código de proyecto
    2. hoja  `SGS_BALANCE` → un solo proyecto, SIN código de proyecto
    3. hoja  `CRUCE_LIQUIDADOR` / `CRUCE_MAT` → **el único que lleva `pro_code`**

Por eso esta plantilla usa **CRUCE_LIQUIDADOR**: es el único formato que hoy
transporta el PRO de cada nodo, y `blueprint.verificar()` ya tiene la vía
rápida multi-proyecto para él:

    if pro_code:                       # ← blueprint.py ~3298
        p   = c.buscar_por_codigo(pro_code)
        pid = p["id"]                  # sin fuzzy matching por nombre

y la clave compuesta `f"{nodo}|{pro_code}"` que evita que dos proyectos con
el mismo número de nodo colisionen. Es decir: el soporte multi-proyecto ya
existe en Liquidador 2, solo que nadie le entregaba un Excel que lo usara.

Reglas que el archivo respeta para no romper el parser
------------------------------------------------------
* La cabecera va en las primeras 14 filas (el parser solo busca ahí) y
  contiene `Código` + `SGS Neto`, que es lo que dispara la detección.
* **No** se emite columna "SIP Cant": en CRUCE_LIQUIDADOR una fila con columna
  SIP vacía se descarta (`if col["sip"] and sip_cant is None: continue`).
* Ninguna cabecera contiene CANTIDAD/CANT/QTY/AMOUNT/UNIDADES/TOTAL, y la
  hoja CRUCE_LIQUIDADOR se deja como hoja activa: así `parsear_mo()` — que cae a
  `wb.active` cuando no hay hoja de MO — no detecta cabecera y devuelve
  `rows_mo = []`, que es justo lo que queremos en una plantilla solo-material.
* Ninguna hoja se llama `N<dígitos>` ni `SGS_BALANCE`, para no activar los
  parsers de mayor prioridad que perderían el `pro_code`.
* Las filas de sección se escriben `NODO: <n>  |  PRO: PRO-xxxxxx` en la
  columna A, porque el parser lee las columnas 1..4 y aplica
  `re.search(r"\\bPRO:\\s*(PRO-\\d+)", ...)` sobre ese texto.

Cantidad a liquidar
-------------------
La columna **SGS Neto** lleva el NETO despachado (despacho − devolución), no
el pendiente. Liquidador 2 calcula `pendiente = SGS Neto − advance en vivo`
(`_mat_cantidad_excel_pendiente`), así que si alguien liquidó algo entre la
generación del Excel y la subida, la cantidad se ajusta sola. Poner aquí el
pendiente ya calculado provocaría doble liquidación.
"""

import io
import re
from datetime import datetime

import openpyxl
from openpyxl.utils import get_column_letter

from verificador_proy.export_auto import (
    _set, _num, _just_bandejas, _antig_txt, _tipo_txt,
    C_HDR_BG, C_HDR_FG, C_PADRE_BG, C_NODO_BG,
    C_OK, C_WARN, C_MUTED, C_ANTIG_HI,
)

# Hoja que Liquidador 2 parsea. No cambiar sin cambiar parser_liquidador2.py.
HOJA_LIQUIDADOR = "CRUCE_LIQUIDADOR"

# Umbral de antigüedad que se resalta en rojo claro (igual que export_auto).
_ANTIG_ALERTA = 30


def _fila_seccion(nodo: str, pro_code: str, pdl: str = "") -> str:
    """
    Texto de la fila de sección que Liquidador 2 lee para saber a qué nodo y a
    qué PRO pertenecen las filas siguientes.

    El nodo va PRIMERO porque el parser extrae el nodo con
    `re.search(r"\\d+(?:\\s*AL\\s*\\d+|[_\\-]N?\\d+)?", fila_str)` — el primer
    número del texto. Si el PRO fuera primero, "PRO-063373" daría nodo 063373.

    El PDL va al final y hoy el parser lo ignora. Se escribe igualmente porque
    en una plantilla multi-proyecto no existe un PDL global: `pdl_detectado`
    queda vacío y, en la fase de cierre, `browser_client` solo marca
    "1.4-TERMINADO/EN BALANCE" cuando recibe un PDL. Dejarlo por bloque permite
    que Liquidador 2 lo lea por nodo el día que quiera cerrar en masivo, sin
    tocar el formato.
    """
    txt = f"NODO: {nodo}  |  PRO: {pro_code}"
    return f"{txt}  |  PDL: {pdl}" if pdl else txt


def _nodo_etiqueta(nodo, pro_code: str) -> str:
    """
    Nodo tal como se escribe en la plantilla. El padre de una familia puede no
    tener número de nodo; en ese caso se usa '0', que el regex del parser sí
    reconoce. El pid igual se resuelve por `pro_code` (vía rápida), así que el
    nodo solo actúa como etiqueta y como clave compuesta `nodo|pro_code`.
    """
    n = str(nodo or "").strip()
    return n if n else "0"


def _recolectar_pendientes(familias: list) -> tuple[list, list]:
    """
    Aplana las familias seleccionadas a una lista de nodos con material
    pendiente, cada uno con sus materiales.

    Devuelve (bloques, excluidos). En `bloques` solo va lo liquidable en el mes
    analizado; el material cuya última salida de almacén es POSTERIOR al mes
    (`posterior_al_mes`, calculado en `analisis_mes`) se aparta en `excluidos` y
    nunca llega a la hoja que lee Liquidador 2. Un programado puede pasar meses
    dormido y recibir una bandeja nueva días antes: liquidar esa salida dentro
    de un mes viejo es un error contable que no se puede deshacer solo.
    """
    out, fuera = [], []
    for f in familias or []:
        for n in f.get("nodos_mat") or []:
            if n.get("estado_mat") != "pendiente":
                continue
            ctx = {
                "padre_code": f.get("padre_code", ""),
                "pdl_code":   f.get("pdl_code", ""),
                "pro_code":   n.get("pro_code", ""),
                "nodo":       _nodo_etiqueta(n.get("nodo"), n.get("pro_code", "")),
            }
            mats, mats_fuera = [], []
            for p in (n.get("pendientes") or []):
                if float(p.get("pendiente") or 0) <= 0.001:
                    continue
                if not str(p.get("codigo") or "").strip():
                    continue
                (mats_fuera if p.get("posterior_al_mes") else mats).append(p)
            for p in mats_fuera:
                fuera.append({**ctx, "material": p})
            if not mats:
                continue
            out.append({
                "padre_code": f.get("padre_code", ""),
                "padre_name": f.get("padre_name", ""),
                "pdl_code":   f.get("pdl_code", ""),
                "lcl":        f.get("lcl", ""),
                "supervisor": f.get("supervisor_full") or f.get("supervisor", ""),
                "tipo":       f.get("tipo", ""),
                "mo_value":   f.get("mo_value", 0.0),
                "fecha_mo":   f.get("fecha_mo", ""),
                "pro_code":   n.get("pro_code", ""),
                "pro_id":     n.get("pro_id"),
                "nodo":       _nodo_etiqueta(n.get("nodo"), n.get("pro_code", "")),
                "materiales": mats,
            })
    # Orden estable: por proyecto padre y luego por nodo numérico.
    def _k(b):
        m = re.search(r"\d+", b["nodo"])
        return (b["padre_code"], int(m.group()) if m else 0, b["pro_code"])
    out.sort(key=_k)
    fuera.sort(key=_k)
    return out, fuera


def _autoancho(ws, anchos: list):
    for i, w in enumerate(anchos, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ── Hoja 1: CRUCE_LIQUIDADOR (la que consume Liquidador 2) ───────────────────────

def _hoja_cruce(wb, bloques: list, titulo: str):
    ws = wb.create_sheet(HOJA_LIQUIDADOR)

    cols = ["Nodo", "Código", "Descripción", "SGS Neto", "Estado"]
    _autoancho(ws, [10, 16, 52, 12, 14])

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
    _set(ws, 1, 1, titulo, bold=True, fg=C_HDR_FG, bg=C_PADRE_BG, size=11)

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(cols))
    _set(ws, 2, 1,
         "Hoja leída por Liquidador 2. 'SGS Neto' = despacho − devolución; el "
         "pendiente real se recalcula contra el advance en vivo de SGS.",
         fg=C_MUTED, size=9)

    # Fila 3 vacía a propósito: separa el bloque de título de la cabecera.
    # La cabecera queda en la fila 4, dentro de las 14 primeras que inspecciona
    # el parser al buscar 'Código' + 'SGS Neto'.
    HDR = 4
    for c, name in enumerate(cols, start=1):
        _set(ws, HDR, c, name, bold=True, fg=C_HDR_FG, bg=C_HDR_BG,
             align="center" if c in (1, 4, 5) else "left")

    r = HDR + 1
    for b in bloques:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(cols))
        _set(ws, r, 1, _fila_seccion(b["nodo"], b["pro_code"], b["pdl_code"]),
             bold=True, bg=C_NODO_BG, size=10)
        r += 1
        for p in b["materiales"]:
            _set(ws, r, 1, b["nodo"], align="center")
            _set(ws, r, 2, str(p.get("codigo") or ""), align="center")
            _set(ws, r, 3, str(p.get("nombre") or ""))
            _set(ws, r, 4, float(p.get("neto") or 0), align="right",
                 num_fmt="#,##0.##")
            # 'PENDIENTE' es el único estado que Liquidador 2 acepta como
            # liquidable aquí: 'DEVOLUCION' lo descarta en _mat_es_liquidable.
            _set(ws, r, 5, "PENDIENTE", align="center", fg=C_WARN, bold=True)
            r += 1

    ws.freeze_panes = ws.cell(row=HDR + 1, column=1)
    return ws


# ── Hoja 2: detalle completo (contexto para Liquidador 2 y para revisión) ────

def _hoja_detalle(wb, bloques: list, titulo: str):
    ws = wb.create_sheet("DETALLE_MATERIALES")

    cols = [
        ("PRO padre", 16), ("PDL / OT", 20), ("LCL", 15),
        ("PRO nodo", 16), ("Nodo", 8), ("Código SAP", 14), ("Descripción", 46),
        ("Neto despachado", 15), ("Ya liquidado", 13), ("Pendiente", 12),
        ("Líder a liquidar", 30), ("Reparto por líder", 34),
        ("Antigüedad", 13), ("Fecha despacho", 15),
        ("1ª aprob. almacén", 17), ("Última salida almacén", 19),
        ("Justificación (bandejas)", 62), ("Supervisor", 26),
    ]
    _autoancho(ws, [w for _, w in cols])

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
    _set(ws, 1, 1, titulo, bold=True, fg=C_HDR_FG, bg=C_PADRE_BG, size=11)

    for c, (name, _) in enumerate(cols, start=1):
        _set(ws, 2, c, name, bold=True, fg=C_HDR_FG, bg=C_HDR_BG,
             align="center", wrap=True)

    r = 3
    for b in bloques:
        for p in b["materiales"]:
            antig = p.get("antiguedad_aprob_dias")
            if antig is None:
                antig = p.get("antiguedad_dias")
            bg_antig = C_ANTIG_HI if (antig is not None and antig > _ANTIG_ALERTA) else None

            split = " / ".join(
                f"{d['nombre']} {d['pct']:.0f}% ({_num(d['neto'])})"
                for d in (p.get("lider_detalle") or [])
            )

            _set(ws, r,  1, b["padre_code"], bold=True)
            _set(ws, r,  2, b["pdl_code"])
            _set(ws, r,  3, b["lcl"] if b["lcl"] and b["lcl"] != "N/A" else "")
            _set(ws, r,  4, b["pro_code"], fg=C_PADRE_BG)
            _set(ws, r,  5, b["nodo"], align="center")
            _set(ws, r,  6, str(p.get("codigo") or ""), align="center")
            _set(ws, r,  7, str(p.get("nombre") or ""), wrap=True)
            _set(ws, r,  8, float(p.get("neto") or 0), align="right", num_fmt="#,##0.##")
            _set(ws, r,  9, float(p.get("advance") or 0), align="right", num_fmt="#,##0.##")
            _set(ws, r, 10, float(p.get("pendiente") or 0), align="right",
                 num_fmt="#,##0.##", bold=True, fg=C_WARN)
            _set(ws, r, 11, str(p.get("lider") or ""), wrap=True)
            _set(ws, r, 12, split, wrap=True, size=9)
            _set(ws, r, 13, _antig_txt(antig), align="center", bg=bg_antig)
            _set(ws, r, 14, str(p.get("fecha_despacho") or ""), align="center")
            _set(ws, r, 15, str(p.get("fecha_aprobacion") or ""), align="center")
            # La última salida es la que decide el mes; va resaltada porque es
            # el dato con el que se descarta o se acepta liquidar la fila.
            _set(ws, r, 16, str(p.get("fecha_aprob_ultima") or ""), align="center",
                 bold=True)
            _set(ws, r, 17, _just_bandejas(p), wrap=True, size=9)
            _set(ws, r, 18, b["supervisor"], size=9)
            r += 1

    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:{get_column_letter(len(cols))}{max(r - 1, 2)}"
    return ws


# ── Hoja 3: resumen por proyecto seleccionado ────────────────────────────────

def _hoja_proyectos(wb, familias: list, bloques: list, titulo: str):
    ws = wb.create_sheet("PROYECTOS")

    cols = [
        ("Tipo", 8), ("PRO padre", 16), ("Nombre", 44), ("PDL / OT", 20),
        ("LCL", 15), ("Supervisor", 26), ("MO liquidada", 16), ("Últ. liq.", 12),
        ("Líder a liquidar", 32), ("Nodos pend.", 11),
        ("Materiales pend.", 15), ("Unidades pend.", 15), ("Antigüedad máx.", 15),
    ]
    _autoancho(ws, [w for _, w in cols])

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
    _set(ws, 1, 1, titulo, bold=True, fg=C_HDR_FG, bg=C_PADRE_BG, size=11)

    for c, (name, _) in enumerate(cols, start=1):
        _set(ws, 2, c, name, bold=True, fg=C_HDR_FG, bg=C_HDR_BG,
             align="center", wrap=True)

    # Agregados por familia, calculados sobre lo que realmente entra al Excel.
    por_padre: dict = {}
    for b in bloques:
        acc = por_padre.setdefault(b["padre_code"], {"nodos": 0, "mats": 0, "qty": 0.0})
        acc["nodos"] += 1
        acc["mats"]  += len(b["materiales"])
        acc["qty"]   += sum(float(p.get("pendiente") or 0) for p in b["materiales"])

    r = 3
    for f in familias:
        acc = por_padre.get(f.get("padre_code", ""))
        if not acc:
            continue          # familia seleccionada sin nada pendiente que liquidar
        antig = f.get("antiguedad_aprob_dias")
        if antig is None:
            antig = f.get("antiguedad_dias")
        nombre = str(f.get("padre_name") or "")

        _set(ws, r,  1, _tipo_txt(f.get("tipo", "")), align="center")
        _set(ws, r,  2, f.get("padre_code", ""), bold=True, fg=C_PADRE_BG)
        _set(ws, r,  3, nombre, wrap=True, size=9)
        _set(ws, r,  4, f.get("pdl_code", ""))
        _set(ws, r,  5, f.get("lcl", "") if f.get("lcl") != "N/A" else "")
        _set(ws, r,  6, f.get("supervisor_full") or f.get("supervisor", ""), size=9)
        _set(ws, r,  7, float(f.get("mo_value") or 0), align="right",
             num_fmt='"$" #,##0', fg=C_OK)
        _set(ws, r,  8, str(f.get("fecha_mo") or ""), align="center")
        _set(ws, r,  9, str(f.get("lider_liquidar") or ""), wrap=True, size=9)
        _set(ws, r, 10, acc["nodos"], align="center")
        _set(ws, r, 11, acc["mats"], align="center")
        _set(ws, r, 12, round(acc["qty"], 2), align="right", num_fmt="#,##0.##",
             bold=True, fg=C_WARN)
        _set(ws, r, 13, _antig_txt(antig), align="center",
             bg=C_ANTIG_HI if (antig is not None and antig > _ANTIG_ALERTA) else None)
        r += 1

    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:{get_column_letter(len(cols))}{max(r - 1, 2)}"
    return ws


# ── Hoja: material apartado por salir fuera del mes ──────────────────────────

def _hoja_excluidos(wb, excluidos: list, titulo: str, periodo: str):
    """
    Deja por escrito lo que NO entró a la plantilla y por qué. Sin esta hoja el
    material apartado desaparecería en silencio y volvería a aparecer como deuda
    el mes siguiente sin explicación.
    """
    ws = wb.create_sheet("FUERA_DEL_MES")
    cols = [("PRO padre", 16), ("PDL / OT", 20), ("PRO nodo", 16), ("Nodo", 8),
            ("Código SAP", 14), ("Descripción", 46), ("Pendiente", 12),
            ("Última salida de almacén", 20), ("Líder", 30)]
    _autoancho(ws, [w for _, w in cols])

    _set(ws, 1, 1, titulo, bold=True, fg=C_HDR_FG, bg=C_PADRE_BG, size=11)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(cols))
    _set(ws, 2, 1,
         f"Este material NO entró a la hoja {HOJA_LIQUIDADOR}: su última salida "
         f"de almacén es posterior a {periodo or 'el mes analizado'}. Para "
         "liquidarlo, genera la plantilla del mes al que pertenece la salida.",
         fg=C_WARN, size=9, wrap=True)
    ws.row_dimensions[2].height = 28

    for c, (name, _) in enumerate(cols, start=1):
        _set(ws, 3, c, name, bold=True, fg=C_HDR_FG, bg=C_HDR_BG,
             align="center", wrap=True)

    r = 4
    for e in excluidos:
        p = e["material"]
        _set(ws, r, 1, e["padre_code"], bold=True)
        _set(ws, r, 2, e["pdl_code"])
        _set(ws, r, 3, e["pro_code"], fg=C_PADRE_BG)
        _set(ws, r, 4, e["nodo"], align="center")
        _set(ws, r, 5, str(p.get("codigo") or ""), align="center")
        _set(ws, r, 6, str(p.get("nombre") or ""), wrap=True)
        _set(ws, r, 7, float(p.get("pendiente") or 0), align="right",
             num_fmt="#,##0.##", bold=True, fg=C_WARN)
        _set(ws, r, 8, str(p.get("fecha_aprob_ultima") or ""), align="center",
             bg=C_ANTIG_HI)
        _set(ws, r, 9, str(p.get("lider") or ""), wrap=True, size=9)
        r += 1

    ws.freeze_panes = "A4"
    return ws


# ── Hoja 4: qué es esto y qué necesita Liquidador 2 ──────────────────────────

_LEEME = [
    ("¿Qué es este archivo?", True),
    ("Plantilla MASIVA para liquidar MATERIALES de varios proyectos a la vez, "
     "generada desde el Verificador SGS (pestaña 'Automático por Mes').", False),
    ("Solo contiene materiales con pendiente > 0 de los proyectos que se "
     "marcaron con checkbox. Nada de mano de obra.", False),
    ("", False),
    ("Cómo se usa", True),
    ("1. Liquidador 2 → Subir plantilla Excel.", False),
    ("2. Escribir la 'Fecha de planilla': es la fecha que queda registrada en "
     "SGS. Esta plantilla NO la trae, a propósito — al cubrir varios proyectos "
     "no existe una sola fecha correcta, así que la decides tú al subir.", False),
    ("3. Verificar contra SGS y liquidar normalmente.", False),
    ("", False),
    ("Qué hoja lee Liquidador 2", True),
    (f"Solo la hoja '{HOJA_LIQUIDADOR}'. Las demás son contexto para revisar "
     "antes de liquidar y no se parsean.", False),
    ("Cada bloque empieza con una fila 'NODO: <n>  |  PRO: PRO-xxxxxx'. Ese PRO "
     "es lo que permite mezclar proyectos distintos en un mismo archivo: "
     "Liquidador 2 resuelve el pid con buscar_por_codigo(pro_code), sin "
     "adivinar por nombre de nodo.", False),
    ("", False),
    ("Material apartado por fecha", True),
    ("Solo entra material cuya ÚLTIMA salida de almacén (aprobación, "
     "project.tray.write_date) sea anterior al fin del mes analizado. Lo que "
     "salió después va a la hoja FUERA_DEL_MES y no se liquida aquí: un "
     "programado puede pasar meses dormido y recibir una bandeja nueva hace "
     "días, y meter esa salida en un mes viejo es un error contable.", False),
    ("Se usa la salida MÁS RECIENTE, no la más antigua: si un material mezcla "
     "una bandeja vieja con una de ayer, el material entero se aparta.", False),
    ("", False),
    ("Sobre las cantidades", True),
    ("La columna 'SGS Neto' es el NETO despachado (despacho − devolución), no "
     "el pendiente. Liquidador 2 resta el advance que lea en vivo de SGS, así "
     "que si algo se liquidó después de generar este archivo, la cantidad se "
     "corrige sola. Por eso no se debe editar esa columna a mano.", False),
    ("", False),
    ("Lo que Liquidador 2 necesita soportar", True),
    ("· Plantilla sin mano de obra: rows_mo llega vacío. El filtro de alcance "
     "por planilla MO ya lo tolera (no se aplica si no hay nodos MO, y las "
     "filas con PRO explícito se respetan siempre).", False),
    ("· PDL global vacío: en una plantilla multi-proyecto no hay un solo PDL, "
     "así que 'pdl_detectado' queda en blanco. Consecuencia verificada: "
     "browser_client.actualizar_fechas_proyecto solo pone el estado "
     "'1.4-TERMINADO/EN BALANCE' cuando recibe un PDL, de modo que los "
     "proyectos NO se cierran solos. El PDL nunca se borra (todo el bloque que "
     "lo escribe está detrás de 'if pdl:'). Si algún día se quiere cerrar en "
     "masivo, el PDL de cada bloque va en su fila de sección "
     "('NODO: n | PRO: PRO-x | PDL: ...') y también en la hoja PROYECTOS.", False),
    ("· El líder a liquidar de la hoja DETALLE_MATERIALES sale de la misma "
     "fuente que usa Liquidador 2 (partner_id de la bandeja de despacho "
     "aprobada), así que sirve para contrastar, no para reemplazar su lookup.", False),
]


def _hoja_leeme(wb, titulo: str):
    ws = wb.create_sheet("LEEME")
    _autoancho(ws, [110])

    _set(ws, 1, 1, titulo, bold=True, fg=C_HDR_FG, bg=C_PADRE_BG, size=11)

    r = 3
    for texto, es_titulo in _LEEME:
        if not texto:
            r += 1
            continue
        _set(ws, r, 1, texto, bold=es_titulo,
             fg=C_PADRE_BG if es_titulo else "000000",
             size=11 if es_titulo else 10, wrap=not es_titulo)
        if not es_titulo:
            ws.row_dimensions[r].height = 30
        r += 1
    return ws


# ── API pública ──────────────────────────────────────────────────────────────

def generar_plantilla_materiales(familias: list, periodo: str = "") -> tuple[bytes, str]:
    """
    Construye la plantilla masiva a partir de las familias seleccionadas en la
    UI (los mismos dicts que devuelve `sgs_checker.analisis_mes`).

    Devuelve (contenido_xlsx, nombre_archivo).
    Lanza ValueError si la selección no tiene ningún material pendiente.
    """
    familias = familias or []
    bloques, excluidos = _recolectar_pendientes(familias)
    if not bloques:
        if excluidos:
            raise ValueError(
                f"Los {len(excluidos)} material(es) pendiente(s) de la selección "
                f"salieron de almacén DESPUÉS de {periodo or 'el mes analizado'}, "
                "así que no pueden liquidarse en este mes. Genera la plantilla "
                "del mes al que pertenece esa salida.")
        raise ValueError(
            "Los proyectos seleccionados no tienen materiales pendientes. "
            "Marca proyectos con estado 'Pendiente'.")

    n_mats = sum(len(b["materiales"]) for b in bloques)
    n_qty  = sum(float(p.get("pendiente") or 0)
                 for b in bloques for p in b["materiales"])
    n_proy = len({b["padre_code"] for b in bloques})

    gen = datetime.now().strftime("%d/%m/%Y %H:%M")
    titulo = (f"Plantilla masiva de MATERIALES — Verificador SGS"
              f"{'  ·  ' + periodo if periodo else ''}  ·  "
              f"{n_proy} proyecto(s) · {len(bloques)} nodo(s) · "
              f"{n_mats} material(es) · {_num(n_qty)} ud pendientes"
              + (f"  ·  {len(excluidos)} apartado(s) por salir fuera del mes"
                 if excluidos else "")
              + f"  ·  generado {gen}")

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # CRUCE_LIQUIDADOR se crea primero y se deja activa: `parsear_mo()` cae a
    # wb.active cuando no encuentra hoja de MO, y esta cabecera no tiene
    # ninguna columna de cantidad, así que no detecta cabecera y devuelve
    # rows_mo = [] — el resultado correcto para una plantilla solo-material.
    _hoja_cruce(wb, bloques, titulo)
    _hoja_detalle(wb, bloques, titulo)
    _hoja_proyectos(wb, familias, bloques, titulo)
    if excluidos:
        _hoja_excluidos(wb, excluidos, titulo, periodo)
    _hoja_leeme(wb, titulo)
    wb.active = 0

    # El nombre NO debe contener un código 6300xxxx: Liquidador 2 lo tomaría
    # como el LCL de toda la plantilla (_codigo_programado_desde_nombre) y se
    # lo aplicaría a proyectos que no le corresponden.
    slug = re.sub(r"[^A-Za-z0-9]+", "_", periodo).strip("_").upper()
    fname = (f"PLANTILLA_MAT_MASIVA{'_' + slug if slug else ''}_"
             f"{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue(), fname
