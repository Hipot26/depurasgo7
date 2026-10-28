"""comparar_sgs_presupuesto.py
Compara proyectos liquidados en SGS vs el Excel de presupuesto del Ing. Wilerson.

Lógica de matching:
  MI (TIPO PDT = 'DF', LCL = fórmula =D{n}): match por PDT (col H) == SGS.pdl_code
  PG (TIPO PDT = PDL/PI/TP, LCL = numérico): match por str(LCL col F) == SGS.lcl
"""

import openpyxl
from datetime import datetime


def _resolver_lcl(lcl_val, d_val):
    """Si LCL es una fórmula =D{n}, retorna el valor de columna D (mismo DF code).
    Si es numérico (6300XXXXXXX), lo convierte a string sin decimales."""
    if isinstance(lcl_val, str) and lcl_val.startswith('=D'):
        return str(d_val).strip() if d_val else ''
    if isinstance(lcl_val, (int, float)):
        return str(int(lcl_val))
    return str(lcl_val).strip() if lcl_val else ''


def comparar_sgs_presupuesto(ruta_excel: str, sgs_proyectos: list) -> dict:
    """
    Lee el Excel de presupuesto y lo cruza con los proyectos consolidados de SGS.

    Returns dict con:
      filas            — lista de filas (verde=liquidado, rojo=pendiente, amarillo=solo SGS)
      total_excel      — total de filas del Excel (sin encabezado)
      total_liquidado  — filas del Excel con match en SGS
      total_pendiente  — filas del Excel sin match
      total_extra_sgs  — proyectos SGS no presentes en el Excel
      n_sgs_total      — total proyectos SGS del mes
      mao_esperado     — suma VALOR MAO TOTAL del Excel
      mao_liquidado    — suma man_obra SGS de filas liquidadas (Excel ∩ SGS)
      mao_sgs_total    — suma man_obra de TODOS los proyectos SGS del mes
    """
    # ── Índices SGS ──────────────────────────────────────────────────────────
    idx_pdl = {}   # pdl_code.upper() → proyecto SGS   (para MI)
    idx_lcl = {}   # lcl.upper()      → proyecto SGS   (para PG)

    for p in sgs_proyectos:
        pdl = (p.get('pdl_code') or '').strip().upper()
        if pdl:
            idx_pdl[pdl] = p
            # La casilla PDL acumula historial ';' — indexar cada PDL por separado
            for parte in pdl.replace(':', ';').replace(',', ';').split(';'):
                parte = parte.strip().strip('/').strip()
                if parte and parte not in idx_pdl:
                    idx_pdl[parte] = p
        lcl = (p.get('lcl') or '').strip().upper()
        if lcl and lcl not in ('N/A', ''):
            idx_lcl[lcl] = p

    # MAO real de todos los proyectos SGS del mes
    mao_sgs_total = sum(float(p.get('man_obra') or 0) for p in sgs_proyectos)
    n_sgs_total   = len(sgs_proyectos)

    # ── Leer Excel ───────────────────────────────────────────────────────────
    wb = openpyxl.load_workbook(ruta_excel, data_only=False)
    ws = wb.active

    filas = []
    mao_esperado    = 0.0
    mao_liquidado   = 0.0
    total_liquidado = 0
    total_pendiente = 0
    matched_sgs_ids = set()   # id() de objetos SGS ya cruzados con Excel

    for i, row in enumerate(ws.iter_rows(min_row=2), start=2):
        if len(row) < 8:
            continue

        fecha_val        = row[0].value   # A
        proceso          = row[1].value   # B
        subproceso       = row[2].value   # C
        proyecto         = row[3].value   # D  (DF code o nombre proyecto)
        circuito         = row[4].value   # E
        lcl_raw          = row[5].value   # F  (fórmula o numérico)
        tipo_pdt         = row[6].value   # G
        pdt              = row[7].value   # H  (PDL / DF code)
        municipio        = row[8].value   if len(row) > 8  else None   # I
        supervisor_excel = row[11].value  if len(row) > 11 else None   # L
        valor_mao        = row[12].value  if len(row) > 12 else None   # M

        if not tipo_pdt or not pdt:
            continue

        tipo_str = str(tipo_pdt).strip().upper()
        pdt_str  = str(pdt).strip().upper()
        lcl_res  = _resolver_lcl(lcl_raw, proyecto)

        # ── Determinar clave y buscar en SGS ─────────────────────────────
        if tipo_str == 'DF':
            clave     = pdt_str
            t_match   = 'MI'
            match_via = 'pdl'
            sgs_p     = idx_pdl.get(clave)
        else:
            t_match = 'PG'
            # 1) Intentar por LCL (col F)
            clave_lcl = lcl_res.upper()
            sgs_p     = idx_lcl.get(clave_lcl)
            if sgs_p is not None:
                clave     = clave_lcl
                match_via = 'lcl'
            else:
                # 2) Intentar por PDL (col H); soporta varios separados por ';'
                clave     = clave_lcl
                match_via = ''
                for pdl_part in pdt_str.split(';'):
                    pdl_part = pdl_part.strip()
                    if not pdl_part:
                        continue
                    hit = idx_pdl.get(pdl_part)
                    if hit is not None:
                        sgs_p     = hit
                        clave     = pdl_part
                        match_via = 'pdl'
                        break

        liquidado = sgs_p is not None

        if isinstance(fecha_val, datetime):
            fecha_str = fecha_val.strftime('%Y-%m-%d')
        elif fecha_val:
            fecha_str = str(fecha_val)
        else:
            fecha_str = ''

        mao_val = float(valor_mao) if isinstance(valor_mao, (int, float)) else 0.0
        mao_esperado += mao_val
        if liquidado:
            total_liquidado += 1
            mao_liquidado += float(sgs_p.get('man_obra') or 0)
            matched_sgs_ids.add(id(sgs_p))
        else:
            total_pendiente += 1

        filas.append({
            'fila':             i,
            'fecha':            fecha_str,
            'proceso':          str(proceso or ''),
            'subproceso':       str(subproceso or ''),
            'proyecto':         str(proyecto or ''),
            'circuito':         str(circuito or ''),
            'lcl':              lcl_res,
            'tipo_pdt':         tipo_str,
            'pdt':              str(pdt or ''),
            'municipio':        str(municipio or ''),
            'supervisor_excel': str(supervisor_excel or ''),
            'valor_mao':        mao_val,
            'tipo_match':       t_match,
            'match_via':        match_via,
            'clave_match':      clave,
            'liquidado':        liquidado,
            'sgs_extra':        False,
            'sgs': {
                'pro_code':      sgs_p.get('pro_code', ''),
                'man_obra':      float(sgs_p.get('man_obra') or 0),
                'liquidado_por': sgs_p.get('liquidado_por', ''),
                'estado':        sgs_p.get('estado', ''),
                'supervisor':    sgs_p.get('supervisor', ''),
            } if sgs_p else None,
        })

    total_excel = total_liquidado + total_pendiente

    # ── Filas amarillas: en SGS pero no en el Excel del presupuesto ──────────
    total_extra_sgs = 0
    for p in sgs_proyectos:
        if id(p) in matched_sgs_ids:
            continue
        total_extra_sgs += 1

        fecha = p.get('fecha')
        if isinstance(fecha, datetime):
            fecha_str = fecha.strftime('%Y-%m-%d')
        elif fecha:
            fecha_str = str(fecha)
        else:
            fecha_str = ''

        proceso_str = str(p.get('proceso') or '')
        tipo_match  = 'MI' if proceso_str.upper() == 'MI' else 'PG'
        pdl_val = (p.get('pdl_code') or '').strip()
        lcl_val = (p.get('lcl') or '').strip()
        clave   = pdl_val.upper() if tipo_match == 'MI' else lcl_val.upper()

        filas.append({
            'fila':             None,
            'fecha':            fecha_str,
            'proceso':          proceso_str,
            'subproceso':       '',
            'proyecto':         p.get('lider', '') or pdl_val,
            'circuito':         '',
            'lcl':              lcl_val,
            'tipo_pdt':         tipo_match,
            'pdt':              pdl_val,
            'municipio':        '',
            'supervisor_excel': p.get('supervisor', ''),
            'valor_mao':        0.0,
            'tipo_match':       tipo_match,
            'match_via':        '',
            'clave_match':      clave,
            'liquidado':        False,
            'sgs_extra':        True,
            'sgs': {
                'pro_code':      p.get('pro_code', ''),
                'man_obra':      float(p.get('man_obra') or 0),
                'liquidado_por': p.get('liquidado_por', ''),
                'estado':        p.get('estado', ''),
                'supervisor':    p.get('supervisor', ''),
            },
        })

    return {
        'filas':            filas,
        'total_excel':      total_excel,
        'total_liquidado':  total_liquidado,
        'total_pendiente':  total_pendiente,
        'total_extra_sgs':  total_extra_sgs,
        'n_sgs_total':      n_sgs_total,
        'mao_esperado':     mao_esperado,
        'mao_liquidado':    mao_liquidado,
        'mao_sgs_total':    mao_sgs_total,
    }
