# -*- coding: utf-8 -*-
"""Casos de Gestión General que nacen de Sonny, PMS o SCM (2026-10-23). Corre después de cargar_sonny_gestion.py,
cargar_pms_gestion.py y cargar_scm_gestion.py.

    python -I nacer_casos_gestion.py                → cuenta (simulado) y luego crea, de 40 en 40, hasta que no quede nada
    python -I nacer_casos_gestion.py --simular      → solo cuenta
    python -I nacer_casos_gestion.py --desde 2026-03-25

Al principio un caso solo nacía de un correo. Desde 2026-10-23 nace también de una planilla de Sonny, una solicitud de PMS o un
libreto de SCM si su código (ticket DF o LCL) existe en SGS: el servidor le pregunta al Centro MO, elige el padre con la misma
regla que el correo (el supervisor de la planilla; si no, el que no es línea viva) y guarda la fuente en `origen`.
Guarda de la primera carga: si hay más de 600 grupos de códigos sin caso, solo nacen los que tienen planilla, solicitud o
libreto con movimiento desde el 2026-03-25 (inicio del historial de Sonny); el resto queda en las tablas, sin caso, anotado
en sgo7_gg_sin_caso. POST {api_url}/nacer con X-Agente-Token de config_gestion.json (el token nunca se imprime).
"""
import argparse
import json
import os
import sys
from collections import Counter

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
DIR = os.path.dirname(os.path.abspath(__file__))
INICIO_SONNY = "2026-03-25"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default=None, help="solo códigos con movimiento desde esta fecha (AAAA-MM-DD)")
    ap.add_argument("--tope-guarda", type=int, default=600, help="si hay más grupos sin caso que esto, se aplica --desde 2026-03-25")
    ap.add_argument("--limite", type=int, default=40)
    ap.add_argument("--simular", action="store_true")
    ap.add_argument("--config", default=os.path.join(DIR, "config_gestion.json"))
    a = ap.parse_args()
    with open(a.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    url = cfg["api_url"].rstrip("/") + "/nacer"
    cab = {"X-Agente-Token": cfg["token"]}

    def post(cuerpo):
        r = requests.post(url, headers=cab, json=cuerpo, timeout=900)
        if r.status_code >= 400:
            try:
                det = r.json().get("detail")
            except Exception:
                det = r.text[:200]
            sys.exit(f"nacer: HTTP {r.status_code} {det}")
        return r.json()

    s = post({"simular": True, "desde": a.desde})
    print(f"grupos de códigos sin caso: {s['grupos_sin_caso']} · pendientes de revisar {s['pendientes']} · por fuente {s['por_fuente']}")
    desde = a.desde
    if not desde and s["pendientes"] > a.tope_guarda:
        desde = INICIO_SONNY
        s2 = post({"simular": True, "desde": desde})
        print(f"GUARDA: más de {a.tope_guarda} pendientes → solo con movimiento desde {desde}: {s2['con_movimiento']} de {s2['pendientes']}")
    if a.simular:
        return
    tot, por_origen = Counter(), Counter()
    n = 0
    while True:
        n += 1
        d = post({"limite": a.limite, "desde": desde})
        for k in ("creados", "no_existe_en_sgs", "fuera_de_corte", "revisados"):
            tot[k] += d.get(k) or 0
        por_origen.update(d.get("por_origen") or {})
        print(f"  vuelta {n}: revisados {d.get('revisados')} · creados {d.get('creados')} {d.get('por_origen')} · no existen en SGS {d.get('no_existe_en_sgs')}"
              f" · fuera del corte {d.get('fuera_de_corte')} · quedan {d.get('quedan')}" + (" · RECÁLCULO PENDIENTE" if d.get("recalculo_pendiente") else ""))
        if not d.get("quedan") or not d.get("revisados"):
            break
    print(f"TOTAL: casos nuevos {tot['creados']} por origen {dict(por_origen)} · grupos sin código en SGS {tot['no_existe_en_sgs']}"
          f" · fuera del corte {tot['fuera_de_corte']}")


if __name__ == "__main__":
    main()
