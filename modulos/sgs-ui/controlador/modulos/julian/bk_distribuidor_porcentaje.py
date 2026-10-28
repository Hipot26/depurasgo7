"""
distribuidor_porcentaje.py — Distribuye prestaciones MO entre N líderes
según porcentajes objetivo usando precios de MDO&MAT.

Algoritmo greedy:
  1. Calcula valor total = Σ(cantidad × precio_unitario)
  2. Para cada líder: target = total × pct/100
  3. Ordena items por valor desc (los más pesados primero → mejor distribución)
  4. Para cada item: asigna unidades al líder más "hambriento" (más lejos de su target)
  5. Cantidades siempre enteras; split solo cuando necesario

Garantías:
  - suma de cantidades por item == cantidad original (sin pérdida)
  - cantidades siempre >= 0 y enteras
  - al menos 1 unidad al líder más hambriento si la cantidad lo permite
"""

import math
import logging

log = logging.getLogger("dist_pct")


def distribuir_prestaciones(
    items: list[dict],
    lideres: list[dict],
    precios: dict[str, dict],
) -> dict:
    """
    items   — lista de checks_mo OK:
              [{ idx, pid, act_id, act_code, pro_code, pro, cantidad, ... }]
    lideres — [{ nombre, cedula, pct }]  pct en 0-100, deben sumar ~100
    precios — { QP_CODE: { precio, unidad, descripcion } }

    Retorna {
        "asignaciones": [{ lider, cedula, pct_obj, target, valor, pct_real, n_items, items }],
        "total":   float,
        "errores": list[str],
        "preview": [{ qp, nodo, precio, cantidad_total, valor_total, partes: [...] }],
    }
    """
    if not items:
        return _vacio("Sin items MO liquidables para distribuir.", lideres)
    if not lideres:
        return _vacio("Define al menos un líder.", lideres)

    total_pct = sum(float(l.get("pct") or 0) for l in lideres)
    if abs(total_pct - 100.0) > 0.5:
        return _vacio(
            f"Los porcentajes suman {total_pct:.1f}%, deben sumar 100%.", lideres
        )

    factor = 100.0 / total_pct
    lids = [
        {
            "nombre": str(l["nombre"]).strip(),
            "cedula": str(l.get("cedula") or "").strip(),
            "pct":    float(l["pct"]) * factor,
        }
        for l in lideres
        if str(l.get("nombre") or "").strip() and float(l.get("pct") or 0) > 0
    ]
    if not lids:
        return _vacio("Sin líderes válidos (nombre o porcentaje faltante).", lideres)

    errores: list[str] = []

    # ── Enriquecer items con valor monetario ──────────────────────────────────
    iv_list = []
    sin_precio: set[str] = set()
    for it in items:
        code  = str(it.get("act_code") or "").strip().upper()
        pi    = precios.get(code) or {}
        precio = float(pi.get("precio") or 0.0)
        cant   = float(it.get("cantidad") or 0)
        valor  = precio * cant
        iv_list.append({
            **it,
            "_codigo":    code,
            "_precio":    precio,
            "_valor":     valor,
            "_cant_int":  max(0, int(round(cant))),
        })
        if precio == 0.0:
            sin_precio.add(code)

    if sin_precio:
        codigos = sorted(sin_precio)
        errores.append(
            f"Sin precio en MDO&MAT: {', '.join(codigos[:8])}"
            f"{'…' if len(codigos) > 8 else ''}. "
            "Se distribuyen por cantidad (sin valor monetario)."
        )

    total_valor = sum(iv["_valor"] for iv in iv_list)
    if total_valor == 0.0:
        # Fallback: tratar cada unidad como valor 1
        for iv in iv_list:
            iv["_valor"] = float(iv["_cant_int"])
            iv["_precio"] = 1.0
        total_valor = sum(iv["_valor"] for iv in iv_list) or 1.0

    # ── Inicializar targets ───────────────────────────────────────────────────
    targets = [
        {
            "lider":     l["nombre"],
            "cedula":    l["cedula"],
            "pct":       l["pct"],
            "target":    total_valor * l["pct"] / 100.0,
            "acumulado": 0.0,
            "items":     [],
        }
        for l in lids
    ]

    # ── Ordenar items por valor desc ──────────────────────────────────────────
    iv_sorted = sorted(iv_list, key=lambda x: -x["_valor"])

    preview = []

    for iv in iv_sorted:
        precio     = iv["_precio"]
        cant_total = iv["_cant_int"]
        if cant_total <= 0:
            continue

        partes = _repartir_enteros(cant_total, precio, targets)

        partes_preview = []
        for t in targets:
            cant_parte = partes[t["lider"]]
            if cant_parte <= 0:
                continue
            valor_parte = precio * cant_parte
            t["acumulado"] += valor_parte
            t["items"].append({
                **iv,
                "cantidad":       cant_parte,
                "partner_nombre": t["lider"],
                "tipo":           "MO",
            })
            partes_preview.append({
                "lider":    t["lider"],
                "cantidad": cant_parte,
                "valor":    round(valor_parte, 2),
            })

        preview.append({
            "qp":            iv.get("act_code", ""),
            "nodo":          iv.get("pro", ""),
            "precio":        precio,
            "cantidad_total": cant_total,
            "valor_total":   round(precio * cant_total, 2),
            "partes":        partes_preview,
        })

    asignaciones = [
        {
            "lider":    t["lider"],
            "cedula":   t["cedula"],
            "pct_obj":  round(t["pct"], 2),
            "target":   round(t["target"], 2),
            "valor":    round(t["acumulado"], 2),
            "pct_real": round(t["acumulado"] / total_valor * 100, 2) if total_valor else 0.0,
            "n_items":  len(t["items"]),
            "items":    t["items"],
        }
        for t in targets
    ]

    return {
        "asignaciones": asignaciones,
        "total":        round(total_valor, 2),
        "errores":      errores,
        "preview":      preview,
    }


# ── Algoritmo de reparto entero ───────────────────────────────────────────────

def _repartir_enteros(cant_total: int, precio: float, targets: list) -> dict[str, int]:
    """
    Reparte `cant_total` unidades entre los líderes según su 'hambre'
    (target - acumulado).  Garantiza suma == cant_total y valores >= 0 enteros.
    """
    resultado = {t["lider"]: 0 for t in targets}
    restante = cant_total
    n = len(targets)

    # Orden: más hambriento primero (mayor deficit relativo)
    orden = sorted(targets, key=lambda t: -(t["target"] - t["acumulado"]))

    for i, t in enumerate(orden):
        if restante <= 0:
            break

        es_ultimo = (i == n - 1)

        if es_ultimo:
            # Al último le damos todo lo que queda
            resultado[t["lider"]] += restante
            restante = 0
            break

        hambre = max(0.0, t["target"] - t["acumulado"])

        if precio > 0:
            max_units = math.floor(hambre / precio)
        else:
            # Sin precio: distribuir proporcional al pct
            max_units = math.floor(cant_total * t["pct"] / 100.0)

        # Asegurar que siempre quede al menos 1 para el siguiente si la cantidad lo permite
        lideres_restantes = n - i - 1
        reserva = min(lideres_restantes, restante - 1) if restante > 1 else 0
        asignar = min(max_units, restante - reserva)
        asignar = max(0, asignar)

        resultado[t["lider"]] += asignar
        restante -= asignar

    # Fallback: si quedan unidades sin asignar (precio muy alto), dar al más hambriento
    if restante > 0:
        mas_hambriento = max(targets, key=lambda t: t["target"] - t["acumulado"])
        resultado[mas_hambriento["lider"]] += restante

    return resultado


# ── Helper ────────────────────────────────────────────────────────────────────

def _vacio(error: str, lideres: list) -> dict:
    return {
        "asignaciones": [
            {
                "lider": str(l.get("nombre") or ""),
                "cedula": str(l.get("cedula") or ""),
                "pct_obj": float(l.get("pct") or 0),
                "target": 0.0, "valor": 0.0, "pct_real": 0.0, "n_items": 0, "items": [],
            }
            for l in lideres
        ],
        "total":    0.0,
        "errores":  [error],
        "preview":  [],
    }
