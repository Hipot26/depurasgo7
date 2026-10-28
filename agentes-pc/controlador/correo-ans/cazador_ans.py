# -*- coding: utf-8 -*-
"""cazador_ans.py — Agente local de correos ANS (corre en el PC del operador, no en el servidor).

Flujo (técnica heredada de CazadorPDL):
  1. Outlook Classic por COM (pywin32) — el buzón vive aquí; el VPS jamás lo ve.
  2. Busca en todas las carpetas de correo los mensajes con CATEGORÍA «ANS»
     (el operador los etiqueta a mano al revisarlos) Y, desde el 2026-10-08, en la Bandeja
     de entrada los correos recientes (10 días) que NOMBREN UN PDL aunque no tengan etiqueta:
     un correo de anulaciones sin etiqueta no llegaba a la plataforma. El servidor decide a qué caso sirve cada uno.
  3. Cada uno se exporta a .msg temporal y se sube a la plataforma
     (POST /api/correo-bot/inbox, X-Bot-Token) — allá se cazan los PDL y se cruzan
     contra el ANS; el tab «Correos ANS» muestra a qué caso sirve cada correo.
  4. Dedupe doble: EntryID en estado local (estado.json) + unique en el servidor.

Uso:
  python cazador_ans.py            # una pasada
  python cazador_ans.py --loop     # cada 15 min (dejarlo corriendo con el .bat)

Config en config.json junto al script: {"api_url", "token", "categoria"}.
Gotchas de CazadorPDL que se respetan: stdout UTF-8 (consola cp1252),
CoInitialize por si se usa desde hilos, DASL para búsqueda store-side.
"""
import os
import sys

# Bajo pythonw (la tarea programada, sin consola) stdout es None: todo va a agente.log.
# En consola: UTF-8 forzado (la consola cp1252 revienta con tildes — gotcha CazadorPDL).
if sys.stdout is None:
    _log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "agente.log"),
                "a", encoding="utf-8")
    sys.stdout = sys.stderr = _log
else:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import re
import tempfile
import time
from datetime import datetime, timedelta

import requests
import win32com.client
import pythoncom

AQUI = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(AQUI, "config.json"), encoding="utf-8"))
ESTADO_PATH = os.path.join(AQUI, "estado.json")
CADA_SEG = 15 * 60
# Red sin etiqueta: Bandeja de entrada, correos de los últimos N días que nombren un PDL. APAGADA
# por defecto (2026-10-08): la regla es «entra lo que el operador etiqueta como ANS»; con la red
# encendida entraban correos ajenos a la bandeja. Solo con "sin_etiqueta": true.
RE_PDL = re.compile("(?<![A-Z0-9])CDOP[A-Z][A-Z0-9]{6,14}(?![A-Z0-9])", re.IGNORECASE)
DIAS_SIN_ETIQUETA = int(CONFIG.get("dias_sin_etiqueta") or 10)


def _estado():
    try:
        return set(json.load(open(ESTADO_PATH, encoding="utf-8")))
    except Exception:
        return set()


def _guardar_estado(ids):
    json.dump(sorted(ids), open(ESTADO_PATH, "w", encoding="utf-8"))


def _carpetas_correo(ns):
    """Todas las carpetas de correo de todos los stores (recursivo), saltando
    calendario/contactos/etc. y las carpetas públicas — igual que CazadorPDL."""
    out = []

    def bajar(carpeta):
        try:
            if carpeta.DefaultItemType != 0:      # 0 = correo
                return
        except Exception:
            return
        out.append(carpeta)
        for sub in carpeta.Folders:
            bajar(sub)

    for store in ns.Folders:
        if "públic" in (store.Name or "").lower() or "public" in (store.Name or "").lower():
            continue
        for c in store.Folders:
            bajar(c)
    return out


def _bandejas_entrada(ns):
    """La Bandeja de entrada de cada store (sin subcarpetas): donde cae lo que aún no se etiquetó."""
    out = []
    for store in ns.Folders:
        if "públic" in (store.Name or "").lower() or "public" in (store.Name or "").lower():
            continue
        for c in store.Folders:
            if (c.Name or "").strip().lower() in ("bandeja de entrada", "inbox"):
                out.append(c)
    return out


def _subir(item, entry_id):
    """Exporta el .msg y lo sube a la plataforma. Devuelve la respuesta o lanza."""
    ruta = os.path.join(tempfile.gettempdir(), f"ans_{abs(hash(entry_id)) % 10**10}.msg")
    item.SaveAs(ruta, 3)                          # 3 = olMSG con adjuntos
    try:
        with open(ruta, "rb") as f:
            r = requests.post(
                f"{CONFIG['api_url']}/correo-bot/inbox",
                headers={"X-Bot-Token": CONFIG["token"]},
                data={"entry_id": entry_id,
                      "asunto": str(item.Subject or "")[:400],
                      "remitente": str(getattr(item, "SenderName", "") or "")[:200],
                      "fecha": str(getattr(item, "ReceivedTime", "") or "")[:60],
                      "categorias": str(item.Categories or "")[:200]},
                files={"msg": ("correo.msg", f, "application/vnd.ms-outlook")},
                timeout=120)
        r.raise_for_status()
        return r.json()
    finally:
        try:
            os.remove(ruta)
        except OSError:
            pass


def pasada():
    pythoncom.CoInitialize()
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        ns = outlook.GetNamespace("MAPI")
        vistos = _estado()
        objetivo = (CONFIG.get("categoria") or "ANS").upper()
        subidos = errores = 0
        for carpeta in _carpetas_correo(ns):
            try:
                # DASL store-side (rápido sobre el OST de 10 GB); LIKE es amplio a
                # propósito — el filtro exacto por categoría va abajo en Python.
                items = carpeta.Items.Restrict(
                    '@SQL="urn:schemas-microsoft-com:office:office#Keywords" '
                    f"LIKE '%{objetivo}%'")
            except Exception:
                continue
            for item in items:
                try:
                    cats = [c.strip().upper() for c in str(item.Categories or "").split(";")]
                    if objetivo not in cats:
                        continue                   # p. ej. categoría «TRANS» ≠ «ANS»
                    entry_id = str(item.EntryID)
                    if entry_id in vistos:
                        continue
                    resp = _subir(item, entry_id)
                    vistos.add(entry_id)
                    subidos += 1
                    casos = ", ".join(resp.get("casos") or []) or "sin caso en la tabla"
                    dup = " (ya estaba)" if resp.get("duplicado") else ""
                    print(f"[subido]{dup} {str(item.Subject or '')[:60]} -> {casos}")
                except Exception as e:
                    errores += 1
                    print(f"[error] {str(getattr(item, 'Subject', '?'))[:50]}: {e}")
        # ── Sin etiqueta: Bandeja de entrada, recientes, que nombren un PDL ─────────────
        if CONFIG.get("sin_etiqueta", False):
            desde = (datetime.now() - timedelta(days=DIAS_SIN_ETIQUETA)).strftime("%m/%d/%Y %H:%M")
            for carpeta in _bandejas_entrada(ns):
                try:
                    items = carpeta.Items.Restrict(f"[ReceivedTime] >= '{desde}'")
                except Exception:
                    continue
                for item in items:
                    try:
                        entry_id = str(item.EntryID)
                        if entry_id in vistos:
                            continue
                        texto = f"{item.Subject or ''} {item.Body or ''}"
                        if not RE_PDL.search(texto):
                            continue
                        resp = _subir(item, entry_id)
                        vistos.add(entry_id)
                        subidos += 1
                        casos = ", ".join(resp.get("casos") or []) or "sin caso en la tabla"
                        print(f"[subido · sin etiqueta] {str(item.Subject or '')[:60]} -> {casos}")
                    except Exception as e:
                        errores += 1
                        print(f"[error] {str(getattr(item, 'Subject', '?'))[:50]}: {e}")
        _guardar_estado(vistos)
        try:
            # Latido: el tab muestra «última pasada hace X min» aunque no subiera nada
            requests.post(f"{CONFIG['api_url']}/correo-bot/latido",
                          headers={"X-Bot-Token": CONFIG["token"]}, timeout=15)
        except Exception:
            pass
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] pasada completa: "
              f"{subidos} subido(s), {errores} error(es)", flush=True)
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="repetir cada 15 min")
    args = ap.parse_args()
    while True:
        try:
            pasada()
        except Exception as e:                     # noqa: BLE001 — el loop no muere
            print(f"[pasada fallida] {e}")
        if not args.loop:
            break
        time.sleep(CADA_SEG)
