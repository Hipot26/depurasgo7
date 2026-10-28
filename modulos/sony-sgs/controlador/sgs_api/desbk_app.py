#!/usr/bin/env python3
"""
Balance SGS - App Flask independiente.
Lee proyectos de SGS (Odoo) y genera un Excel con bandejas y materiales.
"""
import os
import json
from datetime import datetime

import uuid
from flask import Flask, render_template, request, send_file, redirect, url_for
import io

from sgs_client import SGSClient
from generar_balance import generar_excel, generar_reversion

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))

# ── Módulo de liquidación ──────────────────────────────────────────────────────
from liquidacion import liq
app.register_blueprint(liq)

# Almacena temporalmente los Excel generados en memoria
_archivos = {}   # token -> {"bytes": ..., "filename": ...}

# ── Cliente SGS reutilizable (sesion persistente entre requests) ──────────────
_cliente_cache: SGSClient = None

def _cfg():
    url      = os.environ.get("SGS_URL")
    db       = os.environ.get("SGS_DB")
    login    = os.environ.get("SGS_LOGIN")
    password = os.environ.get("SGS_PASSWORD")

    if not all([url, login, password]):
        cfg_path = os.path.join(os.path.dirname(__file__), "config_sgs.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            url      = url      or cfg.get("url", "")
            db       = db       or cfg.get("db", "")
            login    = login    or cfg.get("login", "")
            password = password or cfg.get("password", "")

    return url, db, login, password


def _cliente():
    global _cliente_cache

    url, db, login, password = _cfg()
    if not all([url, login, password]):
        raise RuntimeError(
            "Faltan credenciales SGS. "
            "Configura SGS_URL, SGS_LOGIN, SGS_PASSWORD como variables de entorno "
            "o rellena config_sgs.json."
        )

    # Detectar DB una sola vez y guardarla en config para proximas ejecuciones
    if not db:
        db = SGSClient.detectar_db(url)
        if not db:
            raise RuntimeError("No se pudo detectar la base de datos SGS. Configura SGS_DB.")
        _guardar_db_config(db)

    # Reutilizar cliente si la sesion sigue activa
    if _cliente_cache and _cliente_cache.uid:
        try:
            _cliente_cache._call_kw("res.users", "read", [[_cliente_cache.uid]], {"fields": ["id"]})
            return _cliente_cache
        except Exception:
            pass  # sesion expirada, re-autenticar

    cliente = SGSClient(url, db, login, password)
    cliente.autenticar()
    _cliente_cache = cliente
    return cliente


def _guardar_db_config(db: str):
    """Persiste el nombre de DB en config_sgs.json para evitar detectar_db en proximas ejecuciones."""
    cfg_path = os.path.join(os.path.dirname(__file__), "config_sgs.json")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            if not cfg.get("db"):
                cfg["db"] = db
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=4)
        except Exception:
            pass


# ── Rutas ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generar", methods=["POST"])
def generar():
    proyecto_id = request.form.get("proyecto_id", "").strip()
    if not proyecto_id:
        return render_template("index.html", error="Ingresa un ID o codigo de proyecto.")

    try:
        cliente = _cliente()
        cliente.autenticar()

        # Resolver ID numerico o codigo PRO-XXXXXX
        if proyecto_id.isdigit():
            parent_id = int(proyecto_id)
        else:
            proyecto = cliente.buscar_por_codigo(proyecto_id)
            if not proyecto:
                return render_template(
                    "index.html",
                    error=f"Proyecto '{proyecto_id}' no encontrado en SGS.",
                    proyecto_id=proyecto_id,
                )
            parent_id = proyecto["id"]

        data = cliente.balance_completo(parent_id)

        padre    = data["padre"]
        odm      = padre.get("order_number") or padre.get("odm") or parent_id
        ts       = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"balance_sgs_{odm}_{ts}.xlsx"

        excel_bytes = generar_excel(data)

        # Guardar en memoria y redirigir a pagina de resultado
        token = str(uuid.uuid4())
        _archivos[token] = {"bytes": excel_bytes, "filename": filename}

        nodos  = data["nodos"]
        resumen = [
            {"pro": n.get("code",""), "nodo": n.get("nodo",""),
             "status": n.get("status",["",""])[1] if isinstance(n.get("status"), list) else "",
             "bandejas": len(n.get("bandejas",[])),
             "materiales": len(n.get("liq_materiales",[]))}
            for n in nodos
        ]

        return render_template("resultado.html",
                               filename=filename,
                               token=token,
                               padre=padre.get("name",""),
                               odm=odm,
                               resumen=resumen)

    except RuntimeError as e:
        return render_template("index.html", error=str(e), proyecto_id=proyecto_id)
    except Exception as e:
        return render_template("index.html",
                               error=f"Error inesperado: {e}", proyecto_id=proyecto_id)


@app.route("/descargar/<token>")
def descargar(token):
    archivo = _archivos.get(token)
    if not archivo:
        return "Archivo no encontrado o expirado.", 404
    return send_file(
        io.BytesIO(archivo["bytes"]),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=archivo["filename"],
    )


@app.route("/reversion", methods=["POST"])
def reversion():
    proyecto_id = request.form.get("proyecto_id", "").strip()
    if not proyecto_id:
        return render_template("index.html", error="Ingresa un ID o codigo de proyecto.")

    try:
        cliente = _cliente()
        cliente.autenticar()

        if proyecto_id.isdigit():
            parent_id = int(proyecto_id)
        else:
            proyecto = cliente.buscar_por_codigo(proyecto_id)
            if not proyecto:
                return render_template(
                    "index.html",
                    error=f"Proyecto '{proyecto_id}' no encontrado en SGS.",
                    proyecto_id=proyecto_id,
                )
            parent_id = proyecto["id"]

        data    = cliente.balance_completo(parent_id)
        padre   = data["padre"]
        odm     = padre.get("order_number") or padre.get("odm") or parent_id
        ts      = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"reversion_{odm}_{ts}.xlsx"

        excel_bytes, n_filas = generar_reversion(data)

        if n_filas == 0:
            return render_template(
                "index.html",
                error="No se encontraron materiales sobre-liquidados (Situacion B) en este proyecto.",
                proyecto_id=proyecto_id,
            )

        return send_file(
            io.BytesIO(excel_bytes),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )

    except RuntimeError as e:
        return render_template("index.html", error=str(e), proyecto_id=proyecto_id)
    except Exception as e:
        return render_template("index.html", error=f"Error inesperado: {e}", proyecto_id=proyecto_id)


@app.route("/health")
def health():
    return {"ok": True, "service": "balance-sgs"}


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
