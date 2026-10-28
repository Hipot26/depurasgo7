#!/usr/bin/env python3
"""
Generador del Excel RG11 para el supervisor.

Parte de la plantilla assets/rg11_base.xlsx y reemplaza SOLO las celdas
amarillas de la hoja RG11_budget:

    A = nodo        E = codigo (prest/mat)   F = cantidad   G = TIPO (MO/MAT)

Las columnas H..M son fórmulas (VLOOKUP a 'BR-MAT 2026' + totales) y se
preservan. El resto de hojas (BR-MAT 2026, RG11_node, RG11_drivers) no se tocan.

Reglas de cantidad (decididas con el usuario):
  · MO  → cantidad de SIPWEBORA (lo ejecutado).
  · MAT → cantidad de SIPWEBORA (balanceada). Si SIP != SGS neto, se genera una
          advertencia para que el supervisor revise antes de descargar.
  · La chatarra (tipos I RZ / R CH / R RZ o códigos 970xxx) se excluye: no es
    presupuestable y SGS nunca la maneja.
"""
import io
import os

import openpyxl

from balance_liquidar import _normalizar_nodo_sip
from integracion import BalanceIntegrado, CHATARRA_TIPOS, _nodo_key_sgs

_HERE          = os.path.dirname(os.path.abspath(__file__))
PLANTILLA_RG11 = os.path.join(_HERE, "assets", "rg11_base.xlsx")

HOJA_BUDGET   = "RG11_budget"
FILA_INICIO   = 6      # primera fila de datos
FILA_FIN_TPL  = 881    # última fila de datos en la plantilla original


def _num(v):
    """Convierte a número (int si es entero) o 0."""
    try:
        f = float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return 0
    return int(f) if f == int(f) else f


def _es_chatarra(cod: str, tipo: str) -> bool:
    return (tipo in CHATARRA_TIPOS) or str(cod).startswith("970")


def _norm_cod(cod) -> str:
    """Normaliza un código para comparar SIP↔SGS: mayúsculas, sin sufijo 'C'."""
    c = str(cod).strip().upper()
    return c[:-1] if (c.endswith("C") and len(c) > 1) else c


def construir_filas_rg11(sip_datos: dict, sgs_data: dict):
    """
    Construye las filas del RG11 y la lista de advertencias.

    Cantidades:
      · MO  → cantidad de SIPWEBORA (lo ejecutado).
      · MAT → SGS neto instalado (despachado - devuelto), igual que la hoja
              CRUCE_LIQUIDADOR. Se omite el material con neto <= 0 (sin movimiento).

    Retorna (filas, advertencias) donde:
      filas        = [(nodo, codigo, cantidad, tipo)]  tipo in {"MO","MAT"}
      advertencias = [{nodo, pro, codigo, desc, sip, sgs, tipo}]  incidencias SIP!=SGS
    """
    filas = []   # (nodo, cod, cantidad, tipo, desc)

    # ── MO: agregado por (nodo, codigo), cantidad SIP ────────────────────────
    mo_agg = {}
    for m in sip_datos.get("mo", []):
        cod = str(m.get("codigo") or "").strip()
        if not cod:
            continue
        nodo = (m.get("nodo") or "").strip()
        if (nodo, cod) in mo_agg:
            mo_agg[(nodo, cod)][0] += _num(m.get("cantidad"))
        else:
            mo_agg[(nodo, cod)] = [_num(m.get("cantidad")), m.get("desc", "")]
    for (nodo, cod), (q, desc) in mo_agg.items():
        filas.append((nodo, cod, _num(q), "MO", desc))

    # ── MAT: neto instalado desde SGS (como CRUCE_LIQUIDADOR) ────────────────────
    sgs_total = {}   # cod_norm -> suma del neto instalado en TODO el proyecto
    for n in sgs_data.get("nodos", []):
        k = _nodo_key_sgs(n)
        for e in BalanceIntegrado._sgs_ordenado_desde_bandejas(n):
            if e["es_c"] or str(e["cod_base"]).startswith("970"):
                continue                      # chatarra: SGS no la maneja
            neto = _num(e["neto"])
            if neto <= 0:
                continue                      # sin movimiento neto → no instalado
            filas.append((k, e["cod_base"], neto, "MAT", e.get("desc", "")))
            cb = _norm_cod(e["cod_base"])
            sgs_total[cb] = sgs_total.get(cb, 0) + neto

    # ── Advertencias a NIVEL PROYECTO (no por nodo) ──────────────────────────
    #   La regla es "todo material instalado en SIP debe estar en SGS". SIP y SGS
    #   asignan los nodos de forma distinta, así que comparar por nodo marca falsos
    #   positivos (un material "en otro nodo" igual está cubierto). Se comparan los
    #   TOTALES del proyecto y solo se alerta lo realmente faltante:
    #     · NO ESTÁ EN SGS → el material no existe en SGS en ninguna parte.
    #     · FALTAN EN SGS  → SIP instaló más unidades de las que SGS respalda.
    sip_total = {}   # cod_norm -> [suma_sip, desc, codigo_original]
    sip_idx = BalanceIntegrado._sip_mat_idx(sip_datos["mat"])
    for (nodo, cod), info in sip_idx.items():
        if _es_chatarra(cod, info.get("tipo", "")):
            continue
        cb = _norm_cod(cod)
        q  = _num(info.get("cantidad"))
        if cb in sip_total:
            sip_total[cb][0] += q
        else:
            sip_total[cb] = [q, info.get("desc", ""), cod]

    advertencias = []
    for cb, (sq, desc, cod0) in sip_total.items():
        gq = _num(sgs_total.get(cb, 0))
        if gq <= 0:
            advertencias.append({
                "codigo": cod0, "desc": desc, "sip": _num(sq), "sgs": 0,
                "falta": _num(sq), "tipo": "NO ESTÁ EN SGS",
                "detalle": f"SIP instaló {_num(sq)} unidad(es) y no existe en SGS.",
            })
        elif sq - gq > 0.001:
            advertencias.append({
                "codigo": cod0, "desc": desc, "sip": _num(sq), "sgs": _num(gq),
                "falta": _num(sq - gq), "tipo": "FALTAN EN SGS",
                "detalle": f"SIP instaló {_num(sq)}, SGS respalda {_num(gq)} → faltan {_num(sq - gq)}.",
            })
    advertencias.sort(key=lambda a: -a["falta"])

    # Orden estable: por nodo (natural) y tipo (MO antes que MAT)
    def _sort_key(f):
        nodo = f[0]
        m = _normalizar_nodo_sip(nodo) or nodo
        import re as _re
        mm = _re.match(r'^(\d+)', m)
        return (int(mm.group(1)) if mm else 9999, m, 0 if f[3] == "MO" else 1, f[1])

    filas.sort(key=_sort_key)
    return filas, advertencias


def generar_rg11(sip_datos: dict, sgs_data: dict, meta: dict = None,
                 fecha: str = None, lideres: list = None,
                 template_path: str = None):
    """
    Genera el Excel RG11 del supervisor.

    Retorna (bytes, advertencias).
    """
    template_path = template_path or PLANTILLA_RG11
    filas, advertencias = construir_filas_rg11(sip_datos, sgs_data)

    wb = openpyxl.load_workbook(template_path)
    ws = wb[HOJA_BUDGET]

    # Mapa código→valor tipado de 'BR-MAT 2026' col A. Los códigos numéricos
    # (201020…) están guardados como NÚMERO en la tabla, así que escribir el
    # código como texto rompía el VLOOKUP (match exacto exige mismo tipo → #N/D).
    ws_brmat = wb["BR-MAT 2026"]
    brmat = {}
    for rr in range(2, ws_brmat.max_row + 1):
        v = ws_brmat.cell(row=rr, column=1).value
        if v is not None:
            brmat.setdefault(str(v).strip().upper(), v)

    n        = len(filas)
    fila_fin = max(FILA_FIN_TPL, FILA_INICIO + n - 1)

    # Volcar filas reales en las celdas amarillas (A,E,F,G) + asegurar fórmulas H,I,J.
    # B,C,D se limpian para no arrastrar los valores de ejemplo de la plantilla.
    for i, (nodo, cod, qty, tipo, desc) in enumerate(filas):
        r   = FILA_INICIO + i
        key = str(cod).strip().upper()
        en_brmat = key in brmat
        ws.cell(row=r, column=1, value=_num(nodo) if str(nodo).isdigit() else nodo)  # A nodo
        ws.cell(row=r, column=5, value=brmat.get(key, cod))    # E codigo (tipo BR-MAT)
        ws.cell(row=r, column=6, value=qty)    # F cantidad
        ws.cell(row=r, column=7, value=tipo)   # G tipo
        if en_brmat:
            ws.cell(row=r, column=8, value=f"=+VLOOKUP(E{r},'BR-MAT 2026'!A:C,3,0)")
            ws.cell(row=r, column=9, value=f"=+VLOOKUP(E{r},'BR-MAT 2026'!A:L,12,0)")
        else:
            # Código sin tarifa en BR-MAT 2026: en vez de #N/D, mostrar la
            # descripción real y valor 0 para que el supervisor vea el material
            # y sepa que falta cargar su tarifa.
            ws.cell(row=r, column=8, value=(desc or str(cod)) + "  (SIN TARIFA EN BR-MAT)")
            ws.cell(row=r, column=9, value=0)
        ws.cell(row=r, column=10, value=f"=I{r}*F{r}")
        # B,C,D: limpiar los valores de ejemplo de la plantilla (no son amarillos
        # pero arrastrarían datos falsos). value=None en ws.cell() NO borra, hay
        # que asignar .value directamente.
        ws.cell(row=r, column=2).value = None  # B tipo nodo
        ws.cell(row=r, column=3).value = None  # C tipo trabajo
        ws.cell(row=r, column=4).value = None  # D nivel tension

    # Limpiar filas sobrantes (A..J) para que no queden datos ni #N/A de la plantilla.
    for r in range(FILA_INICIO + n, FILA_FIN_TPL + 1):
        for col in range(1, 11):
            ws.cell(row=r, column=col).value = None

    # Totales (K6/L6/M6) — extender el rango si el balance supera la plantilla.
    ws["K6"] = f'=SUMIF(G{FILA_INICIO}:G{fila_fin},"MO",J{FILA_INICIO}:J{fila_fin})'
    ws["L6"] = f'=SUMIF(G{FILA_INICIO}:G{fila_fin},"MAT",J{FILA_INICIO}:J{fila_fin})'
    ws["M6"] = "=K6+L6"

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), advertencias
