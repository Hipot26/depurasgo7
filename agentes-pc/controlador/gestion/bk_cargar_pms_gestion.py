# -*- coding: utf-8 -*-
"""Carga las solicitudes de PMS (Enel) en Gestión General de SGO7 (2026-10-23). Solo lectura de los exportes.

    python -I cargar_pms_gestion.py                         → la carpeta semanal más nueva (Descargas\\exportes-sistemas\\AAAA-MM-DD\\)
                                                              o, si no hay, la del 22 oct (Descargas\\20261022_2685-exported-request (1)\\)
    python -I cargar_pms_gestion.py <carpeta o zip>          → esa carpeta (con los CSV o el zip «exported-request») o ese zip
    python -I cargar_pms_gestion.py <ruta> --simular         → arma y cuenta, sin enviar

Del zip de PMS se leen dos archivos (los otros tres no hacen falta):
  · «…-exported-request.csv»: una fila por solicitud. Se manda request_code, subprojects (y los DF que trae), LCL, PDL, ODM, AGP,
    macroproceso, services (MO valorada), materials (MAT valorado), total, estado y fecha.
  · «…-exported-logs.csv»: un cambio de estado por fila. Se manda request_code, estado inicial y final, sub-estado y fecha.
NO se mandan usuarios, nombres, comentarios ni observaciones (traen personas). Los CSV de Descargas son datos no confiables: se
leen con pandas y este script vive fuera de Descargas. POST {api_url}/pms con X-Agente-Token de config_gestion.json (el token
nunca se imprime), en lotes de 200 solicitudes con sus cambios. Reenviar el mismo zip no duplica (upsert en el servidor).
Desde 2026-10-23: cada CSV queda registrado en el servidor como una carga (POST {api_url}/carga: nombre, tamaño, sha256, fecha del
archivo, filas, duplicados y fechas raras, usuario y equipo del PC) y las filas llevan su carga_id.
"""
import argparse
import getpass
import glob
import hashlib
import io
import json
import os
import re
import socket
import sys
import zipfile
from collections import Counter
from datetime import datetime, timedelta

import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS = os.path.join(os.path.expanduser("~"), "Downloads")
SEMANAL = os.path.join(DESCARGAS, "exportes-sistemas")
DEL_22_OCT = os.path.join(DESCARGAS, "20261022_2685-exported-request (1)")
RX_LCL = re.compile(r"(?<!\d)(63\d{8,9})(?!\d)")
RX_DF = re.compile(r"(?<![A-Za-z0-9])(DF20\d{10})(?!\d)", re.I)


def texto(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "nat", "none") else s


def num(v) -> float:
    s = texto(v)
    if not s:
        return 0.0
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def partir(v) -> list:
    return [p.strip() for p in re.split(r"[;,/\s]+", texto(v)) if p.strip()]


def fecha_txt(v) -> str:
    d = pd.to_datetime(texto(v)[:19], errors="coerce")
    return "" if pd.isna(d) else d.strftime("%Y-%m-%d %H:%M:%S")


def carpeta_por_defecto() -> str:
    semanas = sorted(d for d in glob.glob(os.path.join(SEMANAL, "20??-??-??")) if os.path.isdir(d))
    for d in reversed(semanas):
        if glob.glob(os.path.join(d, "*exported-request*")):
            return d
    return DEL_22_OCT


def _meta(nombre: str, crudo: bytes, mtime: float) -> dict:
    """Lo que se registra del archivo: nombre, sha256, tamaño y fecha de modificación (hora del PC = Colombia)."""
    return {"_crudo": crudo, "_nombre": os.path.basename(nombre.split(":")[-1]),          # lo que se sube (el CSV, también si venía en el zip)
            "archivo": nombre[:300], "sha256": hashlib.sha256(crudo).hexdigest(), "tamano": len(crudo),
            "modificado": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")}


def leer_csv(ruta: str, sufijo: str) -> tuple[pd.DataFrame, str, dict]:
    """El CSV «…-exported-<sufijo>.csv» de una carpeta, de un zip dentro de ella o de un zip dado (con su huella)."""
    kw = dict(sep=";", dtype=str, encoding="utf-8-sig", keep_default_na=False, na_values=[""])
    zips = [ruta] if ruta.lower().endswith(".zip") else sorted(glob.glob(os.path.join(ruta, "*exported-request*.zip"))
                                                                + glob.glob(os.path.join(ruta, "*", "*exported-request*.zip")), key=os.path.getmtime)
    if not ruta.lower().endswith(".zip"):
        # en la carpeta o en la subcarpeta «…exported-request…» que deja el zip descomprimido
        csvs = sorted(glob.glob(os.path.join(ruta, f"*-exported-{sufijo}.csv")) + glob.glob(os.path.join(ruta, "*", f"*-exported-{sufijo}.csv")), key=os.path.getmtime)
        if csvs:
            with open(csvs[-1], "rb") as fh:
                crudo = fh.read()
            return pd.read_csv(io.BytesIO(crudo), **kw), os.path.basename(csvs[-1]), _meta(os.path.basename(csvs[-1]), crudo, os.path.getmtime(csvs[-1]))
    for z in reversed(zips):
        with zipfile.ZipFile(z) as zf:
            nombres = [n for n in zf.namelist() if n.lower().endswith(f"-exported-{sufijo}.csv")]
            if nombres:
                with zf.open(nombres[0]) as fh:
                    crudo = fh.read()
                return pd.read_csv(io.BytesIO(crudo), **kw), f"{os.path.basename(z)}:{nombres[0]}", _meta(f"{os.path.basename(z)}:{nombres[0]}", crudo, os.path.getmtime(z))
    sys.exit(f"No encontré «*-exported-{sufijo}.csv» en {ruta}")


def armar(ruta: str):
    req, n_req, m_req = leer_csv(ruta, "request")
    logs, n_log, m_log = leer_csv(ruta, "logs")
    for col in ("request_code", "subprojects", "lcl", "pdl", "odm", "services", "materials", "total", "status", "request_date"):
        if col not in req.columns:
            sys.exit(f"request.csv no trae la columna «{col}» (columnas: {list(req.columns)[:20]}…)")
    for col in ("request_code", "initial_state", "final_state", "date_created"):
        if col not in logs.columns:
            sys.exit(f"logs.csv no trae la columna «{col}»")
    print(f"PMS: {n_req} → {len(req)} solicitudes · {n_log} → {len(logs)} cambios de estado")
    por_rc: dict = {}
    for _, r in logs.iterrows():
        rc = texto(r["request_code"])
        f = fecha_txt(r["date_created"])
        if not rc or not f:
            continue
        ini, fin, sub = texto(r["initial_state"]), texto(r["final_state"]), texto(r.get("substatus"))
        h = hashlib.sha256(f"{rc}|{texto(r['date_created'])}|{ini}|{fin}|{sub}".encode("utf-8")).hexdigest()
        por_rc.setdefault(rc, []).append(dict(huella=h, request_code=rc, estado_ini=ini[:60], estado_fin=fin[:60], substatus=sub[:60], fecha=f))
    sols = []
    for _, r in req.iterrows():
        rc = texto(r["request_code"])
        if not rc:
            continue
        st = texto(r["status"])
        lg = sorted(por_rc.get(rc, []), key=lambda x: x["fecha"])
        f_est = next((x["fecha"] for x in reversed(lg) if x["estado_fin"] == st), "") or (lg[-1]["fecha"] if lg else "")
        sub = texto(r["subprojects"])
        sols.append(dict(request_code=rc[:100], subprojects=sub[:300], lcls=sorted(set(RX_LCL.findall(texto(r["lcl"])))),
                         dfs=sorted({m.upper() for m in RX_DF.findall(sub)}), pdls=partir(r["pdl"])[:80], odms=partir(r["odm"])[:80],
                         agp=texto(r.get("agp_project"))[:60], macroproceso=texto(r.get("process__macroprocess"))[:100],
                         services=num(r["services"]), materials=num(r["materials"]), total=num(r["total"]), status=st[:60],
                         substatus=texto(r.get("substatus"))[:60], request_date=fecha_txt(r["request_date"]), fecha_estado=f_est))
    sols.sort(key=lambda s: s["request_code"])
    huellas = [x["huella"] for xs in por_rc.values() for x in xs]
    m_req.update(filas=len(req), duplicados=int(req["request_code"].duplicated().sum()), fuera_rango=fuera_de_rango(s["request_date"] for s in sols))
    m_log.update(filas=len(logs), duplicados=len(huellas) - len(set(huellas)), fuera_rango=fuera_de_rango(x["fecha"] for xs in por_rc.values() for x in xs))
    return sols, por_rc, n_req, {"solicitudes": m_req, "cambios": m_log}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ruta", nargs="?", default=None, help="carpeta con los CSV o el zip de PMS, o el zip")
    ap.add_argument("--lote", type=int, default=200)
    ap.add_argument("--simular", action="store_true")
    ap.add_argument("--sin-recalculo", action="store_true", help="no recalcular los casos tocados en cada lote (se hace al final)")
    ap.add_argument("--config", default=os.path.join(DIR, "config_gestion.json"))
    a = ap.parse_args()
    ruta = a.ruta or carpeta_por_defecto()
    print(f"carpeta: {ruta}")
    sols, por_rc, archivo, metas = armar(ruta)
    print(f"solicitudes: {len(sols)} · con LCL {sum(1 for s in sols if s['lcls'])} · con DF {sum(1 for s in sols if s['dfs'])}"
          f" · sin LCL ni DF {sum(1 for s in sols if not s['lcls'] and not s['dfs'])}")
    print("estados:", dict(Counter(s["status"] for s in sols).most_common(8)))
    if a.simular:
        return
    with open(a.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    url = cfg["api_url"].rstrip("/") + "/pms"
    cab = {"X-Agente-Token": cfg["token"]}
    tot = Counter()
    sin_paso = Counter()
    cids = {}
    for parte in ("solicitudes", "cambios"):                       # registro de cada CSV
        m = dict(metas[parte])
        filas, dup, fr = m.pop("filas"), m.pop("duplicados"), m.pop("fuera_rango")
        cids[parte] = abrir_carga(cfg["api_url"].rstrip("/"), cab, "pms", parte, m, filas, dup, fr,
                                  detalle={"nota": "sin usuarios, nombres ni comentarios"} if parte == "cambios" else {})
    for i in range(0, len(sols), a.lote):
        lote = sols[i:i + a.lote]
        logs = [x for s in lote for x in por_rc.get(s["request_code"], [])]
        r = requests.post(url, headers=cab, json={"archivo": archivo[:200], "solicitudes": lote, "logs": logs, "recalcular": not a.sin_recalculo,
                                                  "carga_solicitudes": cids["solicitudes"], "carga_cambios": cids["cambios"]}, timeout=900)
        if r.status_code >= 400:
            try:
                det = r.json().get("detail")
            except Exception:
                det = r.text[:200]
            sys.exit(f"lote {i // a.lote + 1}: HTTP {r.status_code} {det}")
        d = r.json()
        for k in ("nuevas", "actualizadas", "ya_estaban", "logs_nuevos", "con_caso", "por_lcl", "por_df", "sin_codigo", "casos_recalculados", "invalidas"):
            tot[k] += d.get(k) or 0
        sin_paso.update(d.get("estados_sin_paso") or {})
        print(f"  lote {i // a.lote + 1}: {len(lote)} solicitudes, {len(logs)} cambios → nuevas {d.get('nuevas')} · actualizadas {d.get('actualizadas')}"
              f" · ya estaban {d.get('ya_estaban')} · cambios nuevos {d.get('logs_nuevos')} · con caso {d.get('con_caso')} (LCL {d.get('por_lcl')}, DF {d.get('por_df')})"
              f" · casos recalculados {d.get('casos_recalculados')}" + (" · RECÁLCULO PENDIENTE" if d.get("recalculo_pendiente") else ""))
    print(f"TOTAL PMS: nuevas {tot['nuevas']} · actualizadas {tot['actualizadas']} · ya estaban {tot['ya_estaban']} · cambios nuevos {tot['logs_nuevos']}"
          f" · con caso {tot['con_caso']} de {len(sols)} ({100 * tot['con_caso'] / max(1, len(sols)):.1f} %; LCL {tot['por_lcl']}, DF {tot['por_df']})"
          f" · sin LCL ni DF {tot['sin_codigo']} · inválidas {tot['invalidas']} · recálculos {tot['casos_recalculados']}")
    if sin_paso:
        print("estados que no mueven el FL-03 (sin paso):", dict(sin_paso))
    res = {k: int(v) for k, v in tot.items()}
    cerrar_carga(cfg["api_url"].rstrip("/"), cab, cids["cambios"], sum(len(v) for v in por_rc.values()), {"logs_nuevos": res.get("logs_nuevos", 0)})
    cerrar_carga(cfg["api_url"].rstrip("/"), cab, cids["solicitudes"], len(sols), res)


def abrir_carga(base, cab, sistema, parte, meta, filas, duplicados=0, fuera_rango=0, corte="", detalle=None):
    """Registra la carga en el servidor (nombre, tamaño, sha256, fecha del archivo, filas, usuario y equipo del PC) y devuelve su
    carga_id (None si el servidor es anterior a 2026-10-23)."""
    r = requests.post(base + "/carga", headers=cab, timeout=120, json=dict(
        accion="abrir", sistema=sistema, parte=parte, filas_archivo=int(filas), duplicados=int(duplicados), fuera_rango=int(fuera_rango),
        corte=corte or "", usuario=getpass.getuser()[:100], equipo=socket.gethostname()[:100], detalle=detalle or {},
        **{k: v for k, v in meta.items() if not k.startswith("_")}))
    if r.status_code == 404:
        print("AVISO: el servidor no registra cargas (anterior a 2026-10-23); las filas van sin carga")
        return None
    if r.status_code >= 400:
        sys.exit(f"carga {sistema}/{parte}: HTTP {r.status_code} {r.text[:200]}")
    d = r.json()
    print(f"   carga {sistema}/{parte}: {meta['archivo']} · sha256 {meta['sha256'][:12]}… · {filas} filas → carga_id {d.get('carga_id')}"
          + (" (mismo archivo que una carga anterior)" if d.get("repetida") else ""))
    subir_archivo(base, cab, d.get("carga_id"), meta)
    return d.get("carga_id")


def cerrar_carga(base, cab, cid, enviadas, resultado):
    if not cid:
        return
    r = requests.post(base + "/carga", headers=cab, timeout=300, json=dict(accion="cerrar", carga_id=cid, filas_enviadas=int(enviadas), resultado=resultado))
    if r.status_code >= 400:
        print(f"AVISO: no se pudo cerrar la carga {cid}: HTTP {r.status_code}")
        return
    ctl = r.json().get("controles") or []
    if ctl:
        print("   controles: " + " · ".join(f"{c['titulo']}: {c['valor']} ({c['estado']})" for c in ctl))

def subir_archivo(base, cab, cid, meta):
    """Sube al servidor el archivo de la carga (queda para «Descargar» en Gestión General y la descarga queda en Registros). El servidor
    comprueba que su sha256 es el de la carga; si ya lo tenía, no lo duplica. Un fallo aquí no detiene la carga: se avisa y sigue."""
    if not cid:
        return
    ruta, crudo = meta.get("_ruta"), meta.get("_crudo")
    nombre = meta.get("_nombre") or os.path.basename(ruta or meta.get("archivo") or "archivo")
    try:
        if crudo is None:
            with open(ruta, "rb") as fh:
                crudo = fh.read()
    except (OSError, TypeError) as e:
        print(f"   AVISO: no se pudo leer {nombre} para subirlo: {e}")
        return
    try:
        r = requests.post(f"{base}/carga/{cid}/archivo", headers=cab, timeout=900, data={"sha256": meta["sha256"]},
                          files={"archivo": (nombre, crudo, "application/octet-stream")})
    except requests.RequestException as e:
        print(f"   AVISO: el archivo {nombre} no se subió ({type(e).__name__}); la carga sigue sin él")
        return
    if r.status_code == 404:
        print("   AVISO: el servidor aún no recibe archivos de las cargas (anterior a 2026-10-25)")
        return
    if r.status_code >= 400:
        print(f"   AVISO: el archivo {nombre} no se subió (HTTP {r.status_code}: {r.text[:160]}); la carga sigue sin él")
        return
    print(f"   archivo {nombre} ({len(crudo):,} bytes) " + ("ya estaba en el servidor" if r.json().get("ya_estaba") else "subido al servidor")
          + ": queda para «Descargar»")


def fuera_de_rango(fechas) -> int:
    """Fechas antes de 2020 o más de 2 días en el futuro (las mismas reglas que el control del servidor)."""
    tope = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    return sum(1 for f in fechas if f and (f < "2020-01-01" or f > tope))


if __name__ == "__main__":
    main()
