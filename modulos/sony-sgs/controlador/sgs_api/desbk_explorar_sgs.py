#!/usr/bin/env python3
"""
Explorador SGS - API Odoo JSON-RPC
Uso: python explorar_sgs.py <id_proyecto>
Ejemplo: python explorar_sgs.py 49884

Solo lee datos. No modifica nada en SGS.
"""

import sys
import json
import os
import urllib.request
import urllib.error
from datetime import datetime

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config_sgs.json")
OUTPUT_DIR  = os.path.join(os.path.dirname(__file__), "output")


def detectar_db(url_base):
    """Intenta obtener el nombre de la DB desde el endpoint publico de Odoo."""
    try:
        payload = json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 0,
            "params": {}
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{url_base}/web/database/list",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            dbs = data.get("result", [])
            if dbs:
                print(f"[SGS] Bases de datos encontradas: {dbs}")
                return dbs[0]
    except Exception:
        pass
    return None


def cargar_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    for campo in ("url", "login", "password"):
        if not cfg.get(campo):
            print(f"[ERROR] Falta '{campo}' en config_sgs.json")
            sys.exit(1)

    # Si db no esta configurada, intentar detectarla automaticamente
    if not cfg.get("db"):
        print("[SGS] Campo 'db' vacio — intentando deteccion automatica...")
        db = detectar_db(cfg["url"])
        if db:
            cfg["db"] = db
            print(f"[SGS] DB detectada: '{db}'")
        else:
            print("[ERROR] No se pudo detectar la DB. Rellena 'db' en config_sgs.json manualmente.")
            sys.exit(1)
    return cfg


def _post(url, payload, cookies=None):
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    if cookies:
        headers["Cookie"] = cookies

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw     = resp.read().decode("utf-8")
        cookie  = resp.headers.get("Set-Cookie", "")
        return json.loads(raw), cookie


def autenticar(cfg):
    print(f"[SGS] Autenticando como '{cfg['login']}'...")
    url     = f"{cfg['url']}/web/session/authenticate"
    payload = {
        "jsonrpc": "2.0",
        "method":  "call",
        "id":      1,
        "params": {
            "db":       cfg["db"],
            "login":    cfg["login"],
            "password": cfg["password"],
        },
    }
    result, cookie = _post(url, payload)
    if result.get("result", {}).get("uid"):
        uid = result["result"]["uid"]
        print(f"[SGS] Login OK — uid={uid}")
        return cookie
    else:
        error = result.get("error") or result.get("result")
        print(f"[ERROR] Login fallido: {error}")
        sys.exit(1)


TIPOS_SEGUROS = {"char", "text", "integer", "float", "monetary", "date",
                 "datetime", "boolean", "selection", "many2one"}
MODELOS_BLOQUEADOS = {"account.move", "account.move.line", "account.journal",
                      "account.account", "account.tax"}


def obtener_campos_seguros(cfg, cookie):
    """Obtiene los campos de project.dominion filtrando los que requieren permisos contables."""
    url     = f"{cfg['url']}/web/dataset/call_kw"
    payload = {
        "jsonrpc": "2.0", "method": "call", "id": 10,
        "params": {
            "model":  "project.dominion",
            "method": "fields_get",
            "args":   [],
            "kwargs": {"attributes": ["string", "type", "relation"]},
        },
    }
    result, _ = _post(url, payload, cookies=cookie)
    if "error" in result:
        print(f"[WARN] No se pudo obtener campos: {result['error']}")
        return None

    todos = result.get("result", {})
    seguros = []
    omitidos = []
    for nombre, info in todos.items():
        tipo     = info.get("type", "")
        relacion = info.get("relation", "")
        if tipo in TIPOS_SEGUROS and relacion not in MODELOS_BLOQUEADOS:
            seguros.append(nombre)
        else:
            omitidos.append((nombre, tipo, relacion))

    print(f"[SGS] Campos totales: {len(todos)} | Seguros: {len(seguros)} | Omitidos: {len(omitidos)}")
    print("[SGS] Campos omitidos (relaciones):")
    for nombre, tipo, rel in omitidos:
        print(f"       {nombre} ({tipo}) -> {rel}")
    return seguros, omitidos


def leer_bandeja(cfg, cookie, modelo_bandeja, id_proyecto):
    """Lee las bandejas de un proyecto dado el modelo detectado."""
    url     = f"{cfg['url']}/web/dataset/call_kw"
    payload = {
        "jsonrpc": "2.0", "method": "call", "id": 20,
        "params": {
            "model":  modelo_bandeja,
            "method": "search_read",
            "args":   [[["project_id", "=", int(id_proyecto)]]],
            "kwargs": {"fields": [], "limit": 200},
        },
    }
    result, _ = _post(url, payload, cookies=cookie)
    if "error" in result:
        # Intentar con campo dominion_id en vez de project_id
        payload["params"]["args"] = [[["dominion_id", "=", int(id_proyecto)]]]
        result, _ = _post(url, payload, cookies=cookie)
    if "error" in result:
        return None
    return result.get("result", [])


def leer_proyecto(cfg, cookie, id_proyecto, fields=None):
    """Lee un registro de project.dominion por ID."""
    if fields is None:
        fields, _ = obtener_campos_seguros(cfg, cookie)
        fields = fields or []

    url     = f"{cfg['url']}/web/dataset/call_kw"
    payload = {
        "jsonrpc": "2.0",
        "method":  "call",
        "id":      2,
        "params": {
            "model":  "project.dominion",
            "method": "read",
            "args":   [[int(id_proyecto)]],
            "kwargs": {"fields": fields},
        },
    }
    result, _ = _post(url, payload, cookies=cookie)
    if "error" in result:
        print(f"[ERROR] {result['error']}")
        return None
    registros = result.get("result", [])
    return registros[0] if registros else None


def leer_hijos(cfg, cookie, id_padre):
    """Busca proyectos hijo de un proyecto padre."""
    url     = f"{cfg['url']}/web/dataset/call_kw"
    payload = {
        "jsonrpc": "2.0",
        "method":  "call",
        "id":      3,
        "params": {
            "model":  "project.dominion",
            "method": "search_read",
            "args":   [[["parent_id", "=", int(id_padre)]]],
            "kwargs": {
                "fields": ["id", "name", "state", "parent_id"],
                "limit":  100,
            },
        },
    }
    result, _ = _post(url, payload, cookies=cookie)
    if "error" in result:
        print(f"[ERROR] {result['error']}")
        return []
    return result.get("result", [])


def guardar_json(nombre, datos):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    archivo = os.path.join(OUTPUT_DIR, f"{nombre}_{ts}.json")
    with open(archivo, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2, default=str)
    print(f"[SGS] Guardado: {archivo}")
    return archivo


def main():
    if len(sys.argv) < 2:
        print("Uso: python explorar_sgs.py <id_proyecto>")
        print("Ejemplo: python explorar_sgs.py 49884")
        sys.exit(1)

    id_proyecto = sys.argv[1]
    cfg    = cargar_config()
    cookie = autenticar(cfg)

    # ── Mapear campos omitidos para encontrar modelo de bandejas ──
    print(f"\n[SGS] Mapeando campos de project.dominion...")
    campos_seguros, omitidos = obtener_campos_seguros(cfg, cookie)

    # ── Leer el proyecto ──
    print(f"\n[SGS] Leyendo proyecto ID={id_proyecto}...")
    datos = leer_proyecto(cfg, cookie, id_proyecto, fields=campos_seguros)

    if not datos:
        print("[ERROR] No se encontro el proyecto.")
        sys.exit(1)

    print(f"[SGS] Proyecto: {datos.get('name', '???')} | tray_count={datos.get('tray_count',0)}")
    guardar_json(f"proyecto_{id_proyecto}", datos)

    # ── Buscar bandejas en los modelos one2many detectados ──
    candidatos_bandeja = [
        nombre for nombre, _, rel in omitidos
        if "tray" in nombre.lower() or "tray" in rel.lower()
        or "bandeja" in rel.lower() or "bandeja" in nombre.lower()
    ]
    print(f"\n[SGS] Modelos candidatos de bandejas: {candidatos_bandeja}")

    # Extraer modelos unicos de los omitidos con "tray"
    modelos_tray = list({rel for nombre, _, rel in omitidos
                         if "tray" in rel.lower() or "tray" in nombre.lower() and rel})
    print(f"[SGS] Modelos a probar: {modelos_tray}")

    for modelo in modelos_tray:
        if not modelo:
            continue
        print(f"\n[SGS] Probando modelo '{modelo}' para proyecto {id_proyecto}...")
        bandejas = leer_bandeja(cfg, cookie, modelo, id_proyecto)
        if bandejas is not None:
            print(f"[SGS] {len(bandejas)} bandeja(s) encontradas en '{modelo}'")
            if bandejas:
                print("[SGS] Campos de bandeja disponibles:")
                for k, v in bandejas[0].items():
                    print(f"       {k}: {str(v)[:80]}")
                guardar_json(f"bandejas_{id_proyecto}", bandejas)
        else:
            print(f"[SGS] Modelo '{modelo}' no accesible o sin bandejas.")

    # ── Si tiene hijos, listarlos ──
    print(f"\n[SGS] Buscando proyectos hijo de ID={id_proyecto}...")
    hijos = leer_hijos(cfg, cookie, id_proyecto)
    if hijos:
        print(f"[SGS] {len(hijos)} hijo(s) encontrados:")
        for h in hijos:
            print(f"       ID={h['id']} | {h.get('name','?')} | estado={h.get('state','?')}")
        guardar_json(f"hijos_{id_proyecto}", hijos)
    else:
        print("[SGS] Sin hijos (o es un nodo hoja).")


if __name__ == "__main__":
    main()
