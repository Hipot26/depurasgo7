#!/usr/bin/env python3
"""
Descargador de Actas - SIPWEBORA
Uso: python descargar_acta.py <idplanilla>
Ejemplo: python descargar_acta.py 17079

Genera un archivo Excel (.xls) con todos los datos exactos del sistema.
"""

import sys
import os
import re
import urllib.request
import urllib.error
from datetime import datetime

BASE_URL = "https://sipwebora.com/sonny/html_excel/"


def descargar_acta(id_planilla: str) -> str:
    url = f"{BASE_URL}acta.php?idplanilla={id_planilla}"
    print(f"[*] Descargando planilla {id_planilla}...")
    print(f"    URL: {url}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            html_bytes = response.read()
    except urllib.error.URLError as e:
        print(f"[ERROR] No se pudo conectar: {e}")
        sys.exit(1)

    # Detectar encoding del HTML
    encoding = "utf-8"
    try:
        html = html_bytes.decode(encoding)
    except UnicodeDecodeError:
        html = html_bytes.decode("latin-1")

    # Verificar que el HTML contiene datos reales
    if "idplanilla" not in html.lower() and "planilla" not in html.lower():
        print("[ADVERTENCIA] La respuesta no parece ser una planilla válida.")

    # Reescribir URLs relativas a absolutas para que Excel cargue imágenes y CSS
    # CSS externo
    html = re.sub(
        r'href=(["\']?)stylesheet\.css',
        f'href=\\1{BASE_URL}stylesheet.css',
        html
    )
    # Imágenes relativas (src="image001.png" → src="https://...")
    html = re.sub(
        r'src=(["\']?)(?!http)([^"\'\s>]+\.(png|jpg|jpeg|gif|svg|ico))',
        lambda m: f'src={m.group(1)}{BASE_URL}{m.group(2)}',
        html,
        flags=re.IGNORECASE
    )
    # Cualquier src relativo que apunte a un .php (qrcode, diagrama, etc.)
    html = re.sub(
        r'src=(["\']?)(?!http)([a-zA-Z0-9_\-]+\.php[^"\'\s>]*)',
        lambda m: f'src={m.group(1)}{BASE_URL}{m.group(2)}',
        html
    )
    # Favicon e iconos en <link href="...">
    html = re.sub(
        r'href=(["\']?)(?!http)(\.\.\/[^"\'\s>]+)',
        lambda m: f'href={m.group(1)}https://sipwebora.com/sonny/{m.group(2)[3:]}',
        html
    )

    # Carpeta por ID: DATA_DIR / 17082 / acta_17082.xls
    base_dir    = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
    carpeta_id  = os.path.join(base_dir, id_planilla)
    os.makedirs(carpeta_id, exist_ok=True)

    nombre_archivo = f"acta_{id_planilla}.xls"
    ruta_salida    = os.path.join(carpeta_id, nombre_archivo)

    # Si el archivo está bloqueado (abierto en Excel), agregar timestamp
    try:
        with open(ruta_salida, "w", encoding="utf-8") as f:
            f.write(html)
    except PermissionError:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre_archivo = f"acta_{id_planilla}_{timestamp}.xls"
        ruta_salida    = os.path.join(carpeta_id, nombre_archivo)
        print(f"[!] Archivo anterior en uso. Guardando como: {nombre_archivo}")
        with open(ruta_salida, "w", encoding="utf-8") as f:
            f.write(html)

    print(f"[OK] Archivo guardado: {ruta_salida}")
    print(f"     Tamaño: {os.path.getsize(ruta_salida):,} bytes")
    print()
    print("  Abre el archivo con Microsoft Excel.")
    print("  Si Excel pregunta por el formato, confirmar 'Sí, mantener formato'.")
    print()
    return ruta_salida


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("ERROR: Debes proporcionar el ID de la planilla.")
        print("Ejemplo: python descargar_acta.py 17079")
        sys.exit(1)

    id_planilla = sys.argv[1].strip()

    if not id_planilla.isdigit():
        print(f"ERROR: El ID debe ser un número. Recibido: '{id_planilla}'")
        sys.exit(1)

    descargar_acta(id_planilla)


if __name__ == "__main__":
    main()
