# -*- coding: utf-8 -*-
"""Agente del Centro MO (PC del operador) — la única pieza que ve Outlook.

    python agente_centro_mo.py            → vigila para siempre (iniciar_agente_centro_mo.bat)
    python agente_centro_mo.py --una-vez  → una sola vuelta (pruebas)

Desde el 14 oct 2026 el Centro MO vive en SGO7 (módulo `centro-mo` del servidor). Este agente reemplaza al hilo vigilante y a los botones de Outlook del viejo
centro_mo.py local; mismo patrón que el agente de correos ANS (correo-bot/cazador_ans.py):

  - Cada ~5 s pregunta al servidor (`GET /agente/estado`): qué correos ya conoce (para no
    reanalizar), qué conversaciones vigilar (consultas abiertas) y qué PEDIDOS hay.
  - Cada 3 min (o cuando el servidor pide «scan ahora») recorre la Bandeja de entrada y sus
    subcarpetas: correos de los últimos N días con categoría MO / Orden Pendiente que el servidor
    no tenga (o que tenga descartados y volvieron a etiquetarse) se analizan con
    analizador_correo.analizar_item (texto + Excel + imágenes vía IA, igual que antes) y se
    entregan (`POST /agente/ingesta`). Las respuestas de supervisores en los hilos vigilados
    también.
  - Pedidos: reanalizar un correo, abrir el borrador de consulta (ReplyAll + Display: el envío
    sigue siendo manual), abrir el correo en Outlook, traer la planilla de un link de
    SharePoint (perfil de Microsoft de este PC) o abrir la ventana de login de Microsoft.

Config: config_centro_mo.json junto a este archivo: {"api_url": "https://<servidor>/centro-mo/agente",
"token": "<token del servidor>"}. El token nunca se imprime.
"""
import argparse
import base64
import json
import os
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta

import pythoncom
import requests
import win32com.client

from balance_mo_bridge import _carpetas_de_correo
from analizador_correo import analizar_item

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VERSION = "2026-10-22"
DIR = os.path.dirname(os.path.abspath(__file__))
RUTA_CONFIG = os.path.join(DIR, "config_centro_mo.json")
RUTA_LOG = os.path.join(DIR, "data", "centro", "agente.log")
POLL_PEDIDOS = 5
OL_MAIL = 43
FILTRO_CATEGORIAS = ('@SQL="urn:schemas-microsoft-com:office:office#Keywords" '
                     "LIKE '%MO%' OR "
                     '"urn:schemas-microsoft-com:office:office#Keywords" '
                     "LIKE '%Orden Pendiente%'")

with open(RUTA_CONFIG, encoding="utf-8") as _f:
    CONFIG = json.load(_f)
API = CONFIG["api_url"].rstrip("/")
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


def api(metodo, ruta, timeout=60, **kw):
    r = requests.request(metodo, API + ruta, headers=_H, timeout=timeout, **kw)
    if r.status_code >= 400:
        try:
            det = r.json().get("error")
        except Exception:
            det = r.text[:200]
        raise RuntimeError(f"servidor HTTP {r.status_code}: {det}")
    return r.json()


# ── Outlook ───────────────────────────────────────────────────────────────────

def _ns():
    outlook = win32com.client.Dispatch("Outlook.Application")
    return outlook.GetNamespace("MAPI")


def _item(ns, entry_id, store_id=None):
    return ns.GetItemFromID(entry_id, store_id or None)


def _empaquetar(reg: dict) -> dict:
    """La planilla del sistema que venía adjunta (analizar_item la copió a Descargas) viaja en
    base64; el servidor la guarda junto a sus registros."""
    ruta = reg.get("excel_nativa_ruta")
    if reg.get("planilla_nativa") and ruta and os.path.exists(ruta):
        try:
            with open(ruta, "rb") as f:
                reg["excel_nativa_b64"] = base64.b64encode(f.read()).decode("ascii")
            reg["excel_nativa_nombre"] = os.path.basename(ruta)
        except OSError as e:
            reg.setdefault("notas", []).append(f"Planilla nativa: no se pudo leer para enviarla ({e})")
    return reg


def _enviar(registros, respuestas=None):
    if not registros and not respuestas:
        return
    r = api("POST", "/ingesta", json={"registros": registros, "respuestas": respuestas or []}, timeout=900)
    log(f"ingesta → nuevos {r.get('nuevos')} · reabiertos {r.get('reabiertos')} · "
        f"fusionados {r.get('fusionados')} · respuestas {r.get('respuestas')}")


def _latir_mientras(parar: threading.Event):
    """Mientras dura el scan el bucle no pregunta `/estado` y el servidor da al agente por apagado
    a los 150 s (un correo tarda ~1 min y la entrega va de a 5). Cada 30 s se repite la marca de
    «revisando el buzón»: cualquier llamada con token es latido. `/estado` no sirve aquí porque
    marcaría los pedidos como tomados sin atenderlos."""
    while not parar.wait(30):
        try:
            api("POST", "/scan", json={"fase": "inicio"}, timeout=20)
        except Exception:
            pass


def scan(estado: dict):
    rids = estado.get("rids") or {}
    cfg = estado.get("config") or {}
    dias = int(cfg.get("dias_atras") or 120)
    api("POST", "/scan", json={"fase": "inicio"})
    nuevos, err = 0, ""
    parar = threading.Event()
    threading.Thread(target=_latir_mientras, args=(parar,), daemon=True).start()
    pythoncom.CoInitialize()
    try:
        ns = _ns()
        inbox = ns.GetDefaultFolder(6)          # Bandeja de entrada
        limite = datetime.now() - timedelta(days=dias)
        lote = []
        for ruta, carpeta in _carpetas_de_correo(inbox):
            try:
                hits = carpeta.Items.Restrict(FILTRO_CATEGORIAS)
                it = hits.GetFirst()
            except Exception:
                continue
            while it is not None:
                try:
                    if it.Class == OL_MAIL:
                        fecha_dt = it.ReceivedTime.replace(tzinfo=None)
                        cats = [c.strip().lower()
                                for c in (getattr(it, "Categories", "") or "").split(";")]
                        if fecha_dt >= limite and ("orden pendiente" in cats or "mo" in cats):
                            rid = uuid.uuid5(uuid.NAMESPACE_OID, it.EntryID).hex[:12]
                            previo = rids.get(rid)
                            if previo is None or previo == "descartado":
                                log(f"analizando {'nuevo' if previo is None else 'reabierto'}: "
                                    f"{(it.Subject or '')[:70]}")
                                reg = analizar_item(it, ruta)
                                reg["motivo"] = "nuevo" if previo is None else "reapertura"
                                lote.append(_empaquetar(reg))
                                rids[rid] = "enviado"
                                nuevos += 1
                                if len(lote) >= 5:
                                    _enviar(lote)
                                    lote = []
                except Exception as e:
                    log(f"correo saltado: {e}")
                it = hits.GetNext()
        if lote:
            _enviar(lote)

        # ── Respuestas de supervisores en los hilos con consulta abierta ──────
        respuestas = []
        for v in estado.get("vigilar") or []:
            topic = (v.get("conversacion") or v.get("asunto") or "").replace("'", "''")[:120]
            if not topic:
                continue
            try:
                filtro = f'@SQL="urn:schemas:httpmail:subject" LIKE \'%{topic}%\''
                hits = inbox.Items.Restrict(filtro)
                hits.Sort("[ReceivedTime]", True)
                h = hits.GetFirst()
            except Exception:
                continue
            consultado = v.get("consulta_en", "")
            while h is not None:
                try:
                    if h.Class == OL_MAIL and h.EntryID != v.get("entry_id"):
                        f_h = h.ReceivedTime.strftime("%Y-%m-%d %H:%M")
                        if not consultado or f_h > consultado:
                            log(f"respuesta en «{topic[:50]}» de {h.SenderName} ({f_h})")
                            nuevo = _empaquetar(analizar_item(h, "Bandeja de entrada"))
                            respuestas.append({"id": v["id"], "analisis": nuevo, "f_h": f_h,
                                               "remitente": h.SenderName or ""})
                            break
                except Exception as e:
                    log(f"hilo saltado: {e}")
                h = hits.GetNext()
        if respuestas:
            _enviar([], respuestas)
    except Exception as e:
        err = str(e)[:300]
        log(f"scan con error: {e}\n{traceback.format_exc()}")
    finally:
        parar.set()
        pythoncom.CoUninitialize()
    api("POST", "/scan", json={"fase": "fin", "nuevos": nuevos, "error": err})
    log(f"scan completo: {nuevos} correo(s) analizados" + (f" · error: {err}" if err else ""))


# ── Pedidos ───────────────────────────────────────────────────────────────────

def atender(p: dict):
    tipo = p.get("tipo")
    pid = p.get("id")
    datos = p.get("datos") or {}
    estado, resultado, error = "hecho", None, None
    log(f"pedido {pid} · {tipo} · {p.get('rid') or ''}")
    pythoncom.CoInitialize()
    try:
        if tipo == "reanalizar":
            item = _item(_ns(), p["entry_id"], p.get("store_id"))
            reg = analizar_item(item, p.get("carpeta") or "")
            reg["motivo"] = "reanalisis"
            _enviar([_empaquetar(reg)])
            resultado = {"codigo": reg.get("codigo"), "faltantes": reg.get("faltantes")}
        elif tipo == "consulta":
            item = _item(_ns(), p["entry_id"], p.get("store_id"))
            reply = item.ReplyAll()                    # el equipo en CC queda enterado
            reply.HTMLBody = (datos.get("cuerpo") or "") + (reply.HTMLBody or "")
            reply.Display()                            # NUNCA .Send(): el envío es manual
        elif tipo == "abrir":
            _item(_ns(), p["entry_id"], p.get("store_id")).Display()
        elif tipo == "traer_link":
            import sharepoint_dl
            destino = os.path.join(tempfile.gettempdir(), f"centro_sp_{p.get('rid')}")
            ruta, err = sharepoint_dl.descargar_presupuesto(datos["url"], destino)
            if err == "login_requerido" and sharepoint_dl.login_reciente():
                ruta, err = sharepoint_dl.descargar_presupuesto(datos["url"], destino, headless=False)
            if err:
                estado, error = "error", err
            else:
                with open(ruta, "rb") as f:
                    r = requests.post(f"{API}/excel/{p['rid']}", headers=_H, timeout=300,
                                      files={"archivo": (os.path.basename(ruta), f,
                                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                                      data={"etiqueta": "link"})
                if r.status_code >= 400:
                    estado, error = "error", f"servidor HTTP {r.status_code}: {r.text[:200]}"
                else:
                    resultado = r.json()
        elif tipo == "sp_login":
            import sharepoint_dl
            threading.Thread(target=sharepoint_dl.login_interactivo, daemon=True).start()
            resultado = {"msg": "Ventana de inicio de sesión abierta en el PC"}
        else:
            estado, error = "error", f"tipo de pedido desconocido: {tipo}"
    except Exception as e:
        estado, error = "error", str(e)[:300]
        log(f"pedido {pid} falló: {e}")
    finally:
        pythoncom.CoUninitialize()
    try:
        api("POST", f"/pedido/{pid}", json={"estado": estado, "resultado": resultado, "error": error})
    except Exception as e:
        log(f"no pude cerrar el pedido {pid}: {e}")


# ── Bucle ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--una-vez", action="store_true", help="una sola vuelta: pedidos + scan, y sale")
    args = ap.parse_args()
    log(f"agente Centro MO v{VERSION} → {API}")
    ultimo_scan = 0.0
    while True:
        try:
            estado = api("GET", f"/estado?version={VERSION}")
            for p in estado.get("pedidos") or []:
                atender(p)
            cada = int((estado.get("config") or {}).get("poll_segundos") or 180)
            if args.una_vez or estado.get("scan_ahora") or time.time() - ultimo_scan >= cada:
                scan(estado)
                ultimo_scan = time.time()
        except requests.exceptions.ConnectionError:
            log("sin conexión con el servidor — reintento")
        except Exception as e:
            log(f"vuelta fallida: {e}")
        if args.una_vez:
            break
        time.sleep(POLL_PEDIDOS)


if __name__ == "__main__":
    main()
