#!/usr/bin/env python3
"""
Parser de la planilla RG02 (PDF) — "PLANILLA DE TRABAJO Y MATERIALES".

Extrae lo que el módulo RG03 necesita como contexto autoritativo:
  · LCL / Ticket (para cruzar contra el RG03 subido)
  · Descripción General del Trabajo
  · Observaciones y/o pendientes (justifican QP como trabajos menores)
  · Prestaciones por nodo: {nodo: [{qp, desc, unidad, cantidad}]}

Los QP por nodo SIEMPRE salen de aquí — nunca se infieren del PDT ni del
baremo (lección de entrenamiento: el PDT decía "con PAT" en nodos que en la
planilla real no cobran QP1384).

También soporta el PDT / Plan de Trabajo (Mod. Pdted.) como contexto opcional.
"""
import io
import re

_UNIDADES = ("C/U", "M", "KG", "NUM", "GL", "KM", "UN", "ML", "M2", "M3")


def _texto_completo(pdf) -> str:
    partes = []
    for page in pdf.pages:
        try:
            partes.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(partes)


def _entre(texto: str, inicio: str, *fines) -> str:
    """Texto entre `inicio` y el primer `fin` que aparezca después."""
    i = texto.find(inicio)
    if i < 0:
        return ""
    resto = texto[i + len(inicio):]
    corte = len(resto)
    for fin in fines:
        j = resto.find(fin)
        if 0 <= j < corte:
            corte = j
    return resto[:corte].strip()


def _num(s: str):
    s = str(s or "").strip().replace(",", "")
    try:
        v = float(s)
        return int(v) if v == int(v) else v
    except ValueError:
        return None


def parsear_rg02(pdf_bytes: bytes) -> dict:
    import pdfplumber

    advertencias = []
    nodos = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texto = _texto_completo(pdf)

        # ── prestaciones por nodo (tablas) ──────────────────────────────────
        for page in pdf.pages:
            try:
                tablas = page.extract_tables() or []
            except Exception:
                tablas = []
            for tabla in tablas:
                for fila in tabla:
                    celdas = [str(c).strip() for c in fila if c not in (None, "")]
                    if not celdas:
                        continue
                    qp_idx = next((i for i, c in enumerate(celdas)
                                   if re.fullmatch(r"QP\d{3,4}", c)), None)
                    if qp_idx is None or qp_idx == 0:
                        continue
                    nodo = celdas[0].strip()
                    if not re.fullmatch(r"\d+(_\d+)?", nodo):
                        continue
                    resto = celdas[qp_idx + 1:]
                    desc = resto[0] if resto else ""
                    unidad = next((c for c in resto[1:] if c.upper() in _UNIDADES), "")
                    cantidad = _num(resto[-1]) if resto else None
                    nodos.setdefault(nodo, []).append({
                        "qp": celdas[qp_idx],
                        "desc": desc,
                        "unidad": unidad,
                        "cantidad": cantidad,
                    })

    # fallback por líneas de texto si las tablas no dieron nada
    if not nodos:
        patron = re.compile(
            r"^(\d+(?:_\d+)?)\s.*?(QP\d{3,4})\s+(.+?)\s+(%s)\s+([\d.,]+)\s*$"
            % "|".join(re.escape(u) for u in _UNIDADES))
        for linea in texto.splitlines():
            m = patron.match(linea.strip())
            if m:
                nodos.setdefault(m.group(1), []).append({
                    "qp": m.group(2), "desc": m.group(3).strip(),
                    "unidad": m.group(4), "cantidad": _num(m.group(5)),
                })
        if nodos:
            advertencias.append("Prestaciones extraídas por texto (las tablas del PDF no se pudieron leer).")

    if not nodos:
        advertencias.append("No se encontraron prestaciones QP en el PDF — ¿es una planilla RG02?")

    lcl = (re.search(r"\b(63\d{8})\b", texto) or [None, ""])[1]
    ticket = (re.search(r"\b(CDOPP\w{6,})\b", texto) or [None, ""])[1]

    desc_general = _entre(texto, "Descripción General del Trabajo",
                          "CONVENCIONES", "Nivel de Tensión")
    desc_general = re.sub(r"\s+", " ", desc_general).strip()

    observaciones = _entre(texto, "OBSERVACIONES Y / O PENDIENTES",
                           "HALLAZGOS", "CONVENCIONES", "Elaborado por")
    observaciones = re.sub(r"\s+", " ", observaciones).strip()

    return {
        "lcl": lcl,
        "ticket": ticket,
        "descripcion_general": desc_general,
        "observaciones": observaciones,
        "nodos": nodos,
        "advertencias": advertencias,
    }


def parsear_pdt(pdf_bytes: bytes) -> dict:
    """PDT / Plan de Trabajo (contexto opcional): descripción de actividades."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texto = _texto_completo(pdf)
    desc = _entre(texto, "Descripción de las actividades laborales:",
                  "Lugar de Trabajo")
    ticket = (re.search(r"\b(CDOPP\w{6,})\b", texto) or [None, ""])[1]
    return {"texto": re.sub(r"\s+", " ", desc).strip(), "ticket": ticket}


def combinar_rg02(resultados: list) -> dict:
    """Une varias planillas RG02 (proyectos partidos) en un solo contexto."""
    if not resultados:
        return {"lcl": "", "ticket": "", "descripcion_general": "",
                "observaciones": "", "nodos": {}, "advertencias": []}
    total = dict(resultados[0])
    total["nodos"] = dict(resultados[0]["nodos"])
    total["advertencias"] = list(resultados[0]["advertencias"])
    for r in resultados[1:]:
        for nodo, prests in r["nodos"].items():
            total["nodos"].setdefault(nodo, []).extend(prests)
        for campo in ("descripcion_general", "observaciones"):
            if r[campo] and r[campo] not in total[campo]:
                total[campo] = (total[campo] + " | " + r[campo]).strip(" |")
        total["advertencias"].extend(r["advertencias"])
    return total


if __name__ == "__main__":
    import json
    import sys

    with open(sys.argv[1], "rb") as f:
        res = parsear_rg02(f.read())
    print(json.dumps({k: v for k, v in res.items() if k != "nodos"},
                     ensure_ascii=False, indent=2))
    for nodo, prests in sorted(res["nodos"].items()):
        print(f"Nodo {nodo}:")
        for p in prests:
            print(f"   {p['qp']} ×{p['cantidad']} {p['unidad']:<4} {p['desc'][:60]}")
