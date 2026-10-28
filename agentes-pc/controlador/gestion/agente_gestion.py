# -*- coding: utf-8 -*-
"""Agente de Gestión General de SGO7 (PC del operador) — la pieza que ve Outlook. Solo lectura.

    python agente_gestion.py                → vigila para siempre (cada 10 min; iniciar_agente_gestion.bat)
    python agente_gestion.py --desde 240    → relleno inicial: lee los últimos 240 días y termina
    python agente_gestion.py --una-vez      → una sola vuelta desde el cursor del servidor

Entrega al servidor (https://<host>/api/gestion/agente/) los correos de la Bandeja de entrada (y subcarpetas)
y de Elementos enviados que nombran un código de proyecto (LCL 63…, ticket DF…, plan CDOPP…) o que pertenecen a
un hilo que el servidor ya conoce. El servidor desglosa los hilos citados, ubica cada mensaje en un paso del
flujo FL-03 y calcula el estado del proyecto. No marca como leído, no mueve, no responde.

Config: config_gestion.json junto a este archivo: {"api_url": "https://<host>/api/gestion/agente", "token": "…", "buzon": "<cuenta de Outlook a vigilar>"}.
El token nunca se imprime. Mismo patrón que agente_centro_mo.py.
"""
import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta

import pythoncom
import requests
import win32com.client

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VERSION = "2026-10-22"
DIR = os.path.dirname(os.path.abspath(__file__))
RUTA_CONFIG = os.path.join(DIR, "config_gestion.json")
RUTA_ESTADO = os.path.join(DIR, "data", "gestion", "estado.json")
RUTA_LOG = os.path.join(DIR, "data", "gestion", "agente.log")
OL_MAIL = 43
RX_CODIGO = re.compile(r"\b(DF20\d{10}|CDOPP[IT]?20\d{2}R?\d{5}|63\d{8})\b", re.I)
RX_CUALQUIERA = RX_CODIGO
CARPETAS_FUERA = {"elementos eliminados", "deleted items", "correo no deseado", "junk email", "borradores", "drafts", "rss feeds",
                  "historial de conversaciones", "conversation history", "problemas de sincronización", "sync issues", "archivo"}

with open(RUTA_CONFIG, encoding="utf-8") as _f:
    CONFIG = json.load(_f)
API = CONFIG["api_url"].rstrip("/")
BUZON = CONFIG.get("buzon") or ""          # cuenta de Outlook que se vigila; viene de config_gestion.json
_H = {"X-Agente-Token": CONFIG["token"]}


def log(msg):
    linea = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(linea, flush=True)
    try:
        os.makedirs(os.path.dirname(RUTA_LOG), exist_ok=True)
        with open(RUTA_LOG, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError:
        pass


def api(metodo, ruta, timeout=120, **kw):
    r = requests.request(metodo, API + ruta, headers=_H, timeout=timeout, **kw)
    if r.status_code >= 400:
        try:
            det = r.json().get("detail")
        except Exception:
            det = r.text[:200]
        raise RuntimeError(f"HTTP {r.status_code} {ruta}: {det}")
    return r.json()


def hilo_normalizado(asunto: str) -> str:
    s = asunto or ""
    s = re.sub(r"^\s*((RE|RV|FW|FWD|TR|AW)\s*:\s*)+", "", s, flags=re.I)
    s = RX_CUALQUIERA.sub(" ", s)
    s = re.sub(r"\b(LCL|PDL|PI|PST|PSTL|ODM|PMS)\b[:\s]*", " ", s, flags=re.I)
    s = re.sub(r"[^\w\sáéíóúñ]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s[:160]


def cargar_estado():
    try:
        with open(RUTA_ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"vistos": [], "cursor": None}


def guardar_estado(est):
    os.makedirs(os.path.dirname(RUTA_ESTADO), exist_ok=True)
    est["vistos"] = est["vistos"][-30000:]
    with open(RUTA_ESTADO, "w", encoding="utf-8") as f:
        json.dump(est, f)


def limpiar_html(h: str) -> str:
    h = re.sub(r'src="cid:[^"]*"', 'src="" alt="[imagen incrustada]"', h or "", flags=re.I)
    h = re.sub(r"(?is)<script.*?</script>", "", h)
    return h[:200_000]


def carpetas(folder, ruta, prof=0):
    yield folder, ruta
    if prof < 3:
        try:
            subs = list(folder.Folders)
        except Exception:
            subs = []
        for s in subs:
            if s.Name.lower() in CARPETAS_FUERA:
                continue
            yield from carpetas(s, ruta + "/" + s.Name, prof + 1)


def leer_correo(it, carpeta, enviados):
    fecha = it.SentOn if enviados else it.ReceivedTime
    try:
        html = it.HTMLBody or ""
    except Exception:
        html = ""
    return {
        "entry_id": it.EntryID, "carpeta": carpeta, "asunto": (it.Subject or "")[:500], "de": (it.SenderName or "")[:200],
        "de_correo": (it.SenderEmailAddress or "")[:200] if "@" in (it.SenderEmailAddress or "") else "",
        "para": (it.To or "")[:2000], "cc": (it.CC or "")[:2000], "fecha": fecha.strftime("%Y-%m-%d %H:%M"),
        "categorias": (it.Categories or "")[:200], "adjuntos": [a.FileName for a in it.Attachments][:20],
        "conversacion": (it.ConversationTopic or "")[:500], "cuerpo": it.Body or "", "html": limpiar_html(html),
    }


def escanear(desde: datetime, hilos: set, est: dict, lote: int, relleno: bool) -> dict:
    pythoncom.CoInitialize()
    try:
        ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        buzon = [s for s in ns.Folders if "públicas" not in s.Name.lower() and "public" not in s.Name.lower()][0]
        vistos = set(est["vistos"])
        pendientes, enviados_total, cursor, revisados = [], 0, est.get("cursor"), 0
        filtro = desde.strftime("%m/%d/%Y %H:%M %p")

        def entregar(final=False):
            nonlocal pendientes, enviados_total
            if not pendientes and not final:
                return
            lote_actual, pendientes = pendientes, []
            cuerpo = {"buzon": BUZON, "version": VERSION, "cursor": cursor if final else None}
            try:
                r = api("POST", "/ingesta", json=dict(cuerpo, correos=lote_actual))
                enviados_total += len(lote_actual)
                log(f"  entregados {len(lote_actual)} → correos {r.get('correos')} · eventos nuevos {r.get('eventos_nuevos')} · proyectos {r.get('proyectos_tocados')} · sin proyecto {r.get('sin_proyecto')}")
                for c in lote_actual:
                    vistos.add(c["entry_id"])
            except Exception as e:
                log(f"  lote de {len(lote_actual)} falló ({str(e)[:160]}); reintento uno por uno")
                for c in lote_actual:
                    try:
                        api("POST", "/ingesta", json=dict(cuerpo, correos=[c]))
                        vistos.add(c["entry_id"])
                        enviados_total += 1
                    except Exception as e2:
                        log(f"  correo {c['fecha']} «{c['asunto'][:60]}» falló: {str(e2)[:160]}")
                        est.setdefault("fallidos", []).append(c["entry_id"])
            est["vistos"] = sorted(vistos)
            guardar_estado(est)

        for nombre in ("Bandeja de entrada", "Inbox", "Elementos enviados", "Sent Items"):
            try:
                raiz = buzon.Folders[nombre]
            except Exception:
                continue
            enviados = nombre in ("Elementos enviados", "Sent Items")
            for carpeta, ruta in carpetas(raiz, nombre):
                try:
                    if carpeta.DefaultItemType != 0:
                        continue
                    items = carpeta.Items
                    items.Sort("[ReceivedTime]" if not enviados else "[SentOn]", False)
                    items = items.Restrict(f"[{'SentOn' if enviados else 'ReceivedTime'}] >= '{filtro}'")
                except Exception as e:
                    log(f"  (salto {ruta}: {e})")
                    continue
                for it in items:
                    try:
                        if it.Class != OL_MAIL:
                            continue
                        revisados += 1
                        f = (it.SentOn if enviados else it.ReceivedTime).strftime("%Y-%m-%d %H:%M")
                        if cursor is None or f > cursor:
                            cursor = f
                        if it.EntryID in vistos:
                            continue
                        texto = (it.Subject or "") + "\n" + (it.Body or "")
                        if not RX_CODIGO.search(texto) and hilo_normalizado(it.ConversationTopic or it.Subject or "") not in hilos:
                            continue
                        pendientes.append(leer_correo(it, ruta, enviados))
                        if len(pendientes) >= lote:
                            entregar()
                    except Exception as e:
                        log(f"  (error item: {e})")
        entregar(final=True)
        est["cursor"] = cursor
        guardar_estado(est)
        return {"revisados": revisados, "enviados": enviados_total, "cursor": cursor}
    finally:
        pythoncom.CoUninitialize()


def vuelta(desde_dias=None):
    est = cargar_estado()
    estado = api("GET", "/estado", params={"buzon": BUZON, "version": VERSION})
    hilos = set(estado.get("hilos") or [])
    lote = int((estado.get("config") or {}).get("lote") or 15)
    if desde_dias:
        desde = datetime.now() - timedelta(days=desde_dias)
    elif estado.get("cursor"):
        desde = datetime.fromisoformat(estado["cursor"][:19]) - timedelta(days=2)
    else:
        desde = datetime.now() - timedelta(days=int(estado.get("dias_atras_defecto") or 240))
    log(f"escaneo desde {desde:%Y-%m-%d %H:%M} · hilos conocidos {len(hilos)} · lote {lote}")
    r = escanear(desde, hilos, est, lote, relleno=bool(desde_dias))
    log(f"fin de vuelta: revisados {r['revisados']} · entregados {r['enviados']} · cursor {r['cursor']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", type=int, default=None, help="relleno inicial: días hacia atrás")
    ap.add_argument("--una-vez", action="store_true")
    ap.add_argument("--cada", type=int, default=10, help="minutos entre vueltas")
    a = ap.parse_args()
    log(f"agente de Gestión General {VERSION} · buzón {BUZON}")
    if a.desde or a.una_vez:
        vuelta(a.desde)
        return
    while True:
        try:
            vuelta()
        except Exception as e:
            log(f"ERROR en la vuelta: {e}")
            log(traceback.format_exc()[-800:])
        time.sleep(a.cada * 60)


if __name__ == "__main__":
    main()
