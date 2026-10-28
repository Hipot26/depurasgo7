"""VIGILANTE del ANS: el sistema se supervisa solo y avisa al operador únicamente cuando hace
falta una decisión o una intervención (2026-09-07).

Corre por cron cada 30 min en el VPS. Revisa:
  · agente de correos del PC (latido): si lleva >3 h callado en horario laboral → aviso
    (es lo ÚNICO del flujo que depende de que el PC esté encendido; la VM Kali no hace falta);
  · bot de WhatsApp: proceso caído o >18 h sin mensajes → aviso;
  · IA: fallos en las últimas 24 h (API sin créditos, proveedor caído) → aviso;
  · disco del VPS < 8 GB libres → aviso;
  · ciclos nocturnos (índice de media, análisis pendientes) con error → aviso;
  · y a las 07:00 (Bogotá) un RESUMEN del día: qué espera tu decisión.

Los avisos van por ntfy (NTFY_URL del .env; vacío = solo se imprimen) y no se repiten: cada clave
se manda como mucho una vez cada `REPETIR_H` horas (estado en data/vigilante.json).
Regla dura: por este canal nunca van credenciales ni datos de clientes.

Uso:  venv/bin/python scripts/vigilante.py            # ciclo normal
      venv/bin/python scripts/vigilante.py --prueba   # muestra qué mandaría, sin mandar
      venv/bin/python scripts/vigilante.py --resumen  # fuerza el resumen del día
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.database import AsyncSessionLocal  # noqa: E402

NTFY = settings.NTFY_URL
TITULO = "Sabogal ANS"
DATA = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
ESTADO = os.path.join(DATA, "vigilante.json")
LATIDO = os.path.join(DATA, "correos_ans", "latido.txt")
LOGS_API = ["/home/sgo7/.pm2/logs/sabogal-api-out.log", "/home/sgo7/.pm2/logs/sabogal-api-error.log"]
LOG_INDEXAR = "/var/log/sabogal-wa-indexar.log"
LOG_ANALIZAR = "/var/log/sabogal-wa-analizar.log"
BOGOTA = timezone(timedelta(hours=-5))
REPETIR_H = {"agente_correos": 6, "bot": 6, "ia": 12, "disco": 24, "ciclos": 24}


def ahora_bog() -> datetime:
    return datetime.now(timezone.utc).astimezone(BOGOTA)


def horario_laboral(t: datetime) -> bool:
    return t.weekday() <= 5 and 7 <= t.hour < 19          # lun–sáb, 7 a 19


def cargar_estado() -> dict:
    try:
        with open(ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"alertas": {}, "resumen_dia": None}


def guardar_estado(e: dict) -> None:
    os.makedirs(DATA, exist_ok=True)
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(e, f, ensure_ascii=False, indent=1)


def enviar(texto: str, prioridad: str = "default", prueba: bool = False) -> None:
    if prueba or not NTFY:
        print(f"[{prioridad}] {texto}")
        return
    try:
        requests.post(NTFY, data=texto.encode("utf-8"),
                      headers={"Title": TITULO, "Priority": prioridad}, timeout=15)
    except Exception as e:  # noqa: BLE001
        print("ntfy falló:", e)


def pm2_estado(nombre: str) -> str | None:
    try:
        out = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=30).stdout
        for p in json.loads(out or "[]"):
            if p.get("name") == nombre:
                return p.get("pm2_env", {}).get("status")
    except Exception:  # noqa: BLE001
        return None
    return None


def lineas_recientes(rutas: list, horas: int, patron: str) -> int:
    """Cuenta líneas de log con fecha dentro de las últimas `horas` que casen `patron`."""
    limite = datetime.now(timezone.utc) - timedelta(hours=horas)
    n = 0
    rx = re.compile(patron, re.I)
    for ruta in rutas:
        try:
            with open(ruta, encoding="utf-8", errors="replace") as f:
                for ln in f.readlines()[-4000:]:
                    m = re.search(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})", ln)
                    if not m:
                        continue
                    try:
                        t = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    if t >= limite and rx.search(ln):
                        n += 1
        except OSError:
            continue
    return n


def log_con_error(ruta: str, horas: int = 30) -> bool:
    try:
        if datetime.now(timezone.utc) - datetime.fromtimestamp(os.path.getmtime(ruta), timezone.utc) > timedelta(hours=horas):
            return False
        with open(ruta, encoding="utf-8", errors="replace") as f:
            cola = f.readlines()[-200:]
        return any("Traceback" in ln or "Error" in ln and "error:" in ln.lower() for ln in cola)
    except OSError:
        return False


async def q(db, sql, **p):
    return [tuple(r) for r in (await db.execute(text(sql), p))]


async def main():
    prueba = "--prueba" in sys.argv
    forzar_resumen = "--resumen" in sys.argv
    t = ahora_bog()
    estado = cargar_estado()
    alertas = []          # (clave, texto, prioridad)

    # 1. Agente de correos (PC del operador)
    try:
        latido = datetime.fromisoformat(open(LATIDO).read().strip().replace("Z", "+00:00"))
        horas = (datetime.now(timezone.utc) - latido).total_seconds() / 3600
    except (OSError, ValueError):
        latido, horas = None, 999
    if horas > 3 and horario_laboral(t):
        alertas.append(("agente_correos",
                        f"El agente de correos ANS (tu PC / Outlook) lleva {horas:.0f} h sin reportar. "
                        f"Enciende el PC o revisa que iniciar_correo_ans.bat esté corriendo. "
                        f"Todo lo demás (WhatsApp, índice, análisis) sigue solo en el servidor.", "high"))

    # 2. Bot de WhatsApp
    st = pm2_estado("sabogal-wa-bot") or "online"  # el bot corre en otro servidor; allá lo vigila su propio bot_check.sh
    async with AsyncSessionLocal() as db:
        ult = (await q(db, "select max(creado_en) from wa_mensajes"))[0][0]
        h_bot = (datetime.now(timezone.utc) - ult.replace(tzinfo=timezone.utc)).total_seconds() / 3600 if ult else 999
        competente = (await q(db, "select max(periodo) from ans_registros where entorno='real'"))[0][0]
        propuestos = (await q(db, """select count(distinct r.id) from wa_analisis a join ans_registros r on r.id=a.reg_id
            where a.estado='propuesto' and r.periodo=:p and r.entorno='real'
              and a.id = (select max(id) from wa_analisis x where x.reg_id=a.reg_id and x.estado<>'descartado')""", p=competente))[0][0]
        fallidos = (await q(db, """select count(*) from wa_analisis a join ans_registros r on r.id=a.reg_id
            where a.estado<>'descartado' and r.periodo=:p and r.entorno='real' and a.resultado like '%"error"%'""", p=competente))[0][0]
        correos_nuevos = (await q(db, "select count(*) from correo_ans where estado='nuevo'"))[0][0]
        respuestas_24h = (await q(db, """select count(*) from ans_consulta_msgs where direccion='recibido'
            and creado_en > now() - interval '24 hours'"""))[0][0]
        sin_analizar = 0
        try:
            from app.services import wa_analista
            sin_analizar = len(await wa_analista.casos_pendientes(db, competente)) if competente else 0
        except Exception:  # noqa: BLE001
            pass
        backlog_indice = (await q(db, """select count(*) from wa_mensajes m left join wa_media_meta w on w.msg_id=m.id
            where m.media_ruta is not null and (w.id is null or w.estado='pendiente')"""))[0][0]
    if st != "online":
        alertas.append(("bot", f"El bot de WhatsApp está {st or 'ausente'} en pm2. Revísalo: pm2 logs sabogal-wa-bot", "high"))
    elif h_bot > 18:
        alertas.append(("bot", f"El bot de WhatsApp lleva {h_bot:.0f} h sin recibir mensajes (grupos activos a diario). "
                               f"Puede haber perdido la sesión: pm2 logs sabogal-wa-bot", "high"))

    # 3. IA
    fallos_ia = lineas_recientes(LOGS_API, 24, r"credit balance|no disponible|Cloudflare .* HTTP [45]\d\d|sin JSON usable")
    if fallos_ia:
        alertas.append(("ia", f"La IA falló {fallos_ia} veces en 24 h (API sin créditos o proveedor caído). "
                              f"Hay {fallidos} análisis con error en {competente}. Recarga créditos o activa Cloudflare "
                              f"(IA_PROVIDER en .env) y pulsa «Reintentar fallidos» en Auditoría IA.", "high"))

    # 4. Disco
    libre_gb = shutil.disk_usage("/").free / 1024 ** 3
    if libre_gb < 8:
        alertas.append(("disco", f"Al VPS le quedan {libre_gb:.1f} GB libres. El archivo de WhatsApp crece 4 GB/mes: "
                                 f"toca decidir la retención (ARCHIVO_WHATSAPP.html).", "high"))

    # 5. Ciclos nocturnos
    rotos = [n for n, r in (("índice de media", LOG_INDEXAR), ("análisis pendientes", LOG_ANALIZAR)) if log_con_error(r)]
    if rotos:
        alertas.append(("ciclos", f"Ciclo nocturno con error: {', '.join(rotos)}. Revisa /var/log/sabogal-wa-*.log", "default"))

    # Enviar alertas con dedupe
    enviadas = 0
    for clave, texto, prio in alertas:
        ultimo = estado["alertas"].get(clave)
        if ultimo and not prueba:
            try:
                if datetime.now(timezone.utc) - datetime.fromisoformat(ultimo) < timedelta(hours=REPETIR_H.get(clave, 12)):
                    continue
            except ValueError:
                pass
        enviar(texto, prio, prueba)
        estado["alertas"][clave] = datetime.now(timezone.utc).isoformat()
        enviadas += 1

    # 6. Resumen del día (07:00–07:29 Bogotá, una vez)
    hoy = t.strftime("%Y-%m-%d")
    if forzar_resumen or (t.hour == 7 and t.minute < 30 and estado.get("resumen_dia") != hoy):
        partes = [f"Periodo {competente}."]
        # Tablero del ciclo de vida (orquestación): cuántos casos en cada estado
        try:
            from app.services.auth import create_access_token
            tok = create_access_token({"sub": "12"})
            tb = requests.get("http://127.0.0.1:3008/api/ans/wa/tablero", params={"periodo": competente},
                              headers={"Authorization": f"Bearer {tok}"}, timeout=60).json()
            if isinstance(tb, list):
                cnt = {}
                for c in tb:
                    cnt[c["estado"]] = cnt.get(c["estado"], 0) + 1
                partes.append("Tablero: " + ", ".join(f"{cnt.get(k, 0)} {l}" for k, l in (
                    ("nuevo", "nuevos"), ("recolectando", "recolectando"), ("analizado", "analizados (esperan tu decisión)"),
                    ("decidido_sin_soporte", "decididos SIN soporte"), ("verificado", "verificados"))) + ".")
        except Exception as e:  # noqa: BLE001
            partes.append(f"(tablero no disponible: {str(e)[:60]})")
        partes.append(f"Esperan tu decisión: {propuestos} propuesta(s) en Auditoría IA, {correos_nuevos} correo(s) por revisar, "
                      f"{respuestas_24h} respuesta(s) de WhatsApp en 24 h.")
        if sin_analizar:
            partes.append(f"{sin_analizar} caso(s) por auditar sin análisis (el ciclo de las 4:30 los corre; si persiste, pulsa Analizar pendientes).")
        if fallidos:
            partes.append(f"{fallidos} análisis con error (IA).")
        partes.append(f"Índice de media: {backlog_indice} archivo(s) pendientes. Disco: {libre_gb:.0f} GB libres. "
                      f"Agente de correos: {'ok' if horas < 3 else f'{horas:.0f} h sin reportar'}. Bot: {st or '?'}.")
        enviar("Buenos días. " + " ".join(partes), "default", prueba)
        estado["resumen_dia"] = hoy
        enviadas += 1

    if not prueba:
        guardar_estado(estado)
    print(f"{t:%Y-%m-%d %H:%M} Bogotá · alertas detectadas {len(alertas)} · enviadas {enviadas} · "
          f"agente {horas:.1f} h · bot {st} ({h_bot:.1f} h) · ia fallos 24h {fallos_ia} · disco {libre_gb:.1f} GB · "
          f"propuestos {propuestos} · sin analizar {sin_analizar} · índice pendiente {backlog_indice}")


if __name__ == "__main__":
    asyncio.run(main())
