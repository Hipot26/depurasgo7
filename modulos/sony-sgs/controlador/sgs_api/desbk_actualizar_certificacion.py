#!/usr/bin/env python3
"""
Actualiza SOLO el campo 'certification_date' de un proyecto en SGS.

Uso:
    python actualizar_certificacion.py PRO-052365 2026-04-09

Seguridad:
    - Lee el registro antes de modificar y muestra el estado actual.
    - Escribe UNICAMENTE el campo certification_date.
    - Verifica el resultado leyendo el registro nuevamente.
    - No toca ningun otro campo ni modelo.
"""

import sys
import os
import json

# Agregar carpeta raiz de sgs_api al path
sys.path.insert(0, os.path.dirname(__file__))
from sgs_client import SGSClient

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config_sgs.json")


def cargar_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("db"):
        print("[SGS] Campo 'db' vacio — detectando automaticamente...")
        db = SGSClient.detectar_db(cfg["url"])
        if db:
            cfg["db"] = db
            print(f"[SGS] DB detectada: '{db}'")
        else:
            print("[ERROR] No se pudo detectar la DB.")
            sys.exit(1)
    return cfg


def main():
    if len(sys.argv) < 3:
        print("Uso: python actualizar_certificacion.py <codigo> <fecha>")
        print("Ejemplo: python actualizar_certificacion.py PRO-052365 2026-04-09")
        sys.exit(1)

    codigo = sys.argv[1].strip()
    fecha  = sys.argv[2].strip()

    # Validar formato de fecha basico
    partes = fecha.split("-")
    if len(partes) != 3 or not all(p.isdigit() for p in partes):
        print(f"[ERROR] Formato de fecha invalido: '{fecha}'. Usar YYYY-MM-DD.")
        sys.exit(1)

    cfg    = cargar_config()
    client = SGSClient(cfg["url"], cfg["db"], cfg["login"], cfg["password"])

    print(f"\n[SGS] Autenticando...")
    uid = client.autenticar()
    print(f"[SGS] Sesion iniciada — uid={uid}")

    # ── Paso 1: Buscar el registro ────────────────────────────────────────────
    print(f"\n[SGS] Buscando '{codigo}'...")
    proyecto = client.buscar_por_codigo(codigo)

    if not proyecto:
        print(f"[ERROR] No se encontro el proyecto '{codigo}'.")
        sys.exit(1)

    pid  = proyecto["id"]
    name = proyecto.get("name", "?")
    cert_actual = proyecto.get("certification_date", False)

    print(f"[SGS] Encontrado: ID={pid} | {name}")
    print(f"[SGS] Fecha de certificacion ACTUAL: {cert_actual or '(sin valor)'}")

    # ── Paso 2: Confirmar antes de escribir ───────────────────────────────────
    print(f"\n[SGS] >> Se actualizara 'certification_date' a '{fecha}' en ID={pid}")
    print("[SGS] >> SOLO este campo sera modificado.")
    resp = input("[SGS] Confirmar operacion? (s/n): ").strip().lower()
    if resp != "s":
        print("[SGS] Operacion cancelada.")
        sys.exit(0)

    # ── Paso 3: Escribir ──────────────────────────────────────────────────────
    print(f"\n[SGS] Ejecutando write en project.dominion ID={pid}...")
    ok = client.write("project.dominion", pid, {"certification_date": fecha})
    print(f"[SGS] Respuesta del servidor: {ok}")

    # ── Paso 4: Verificar leyendo de nuevo ────────────────────────────────────
    print(f"\n[SGS] Verificando resultado...")
    verificacion = client.read(
        "project.dominion", [pid], ["id", "code", "name", "certification_date"]
    )
    if verificacion:
        cert_nuevo = verificacion[0].get("certification_date", "?")
        print(f"[SGS] Fecha de certificacion NUEVA: {cert_nuevo}")
        if str(cert_nuevo) == fecha:
            print("[SGS] OK — El campo fue actualizado correctamente.")
        else:
            print(f"[WARN] El valor guardado ({cert_nuevo}) no coincide con '{fecha}'.")
    else:
        print("[WARN] No se pudo releer el registro para verificar.")


if __name__ == "__main__":
    main()
