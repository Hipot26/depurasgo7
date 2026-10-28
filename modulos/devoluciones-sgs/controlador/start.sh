#!/bin/bash
cd /var/www/sgo7/devoluciones-sgs
# Secretos y parámetros de la instalación (SGS_URL, SGS_DB, SGS_LOGIN/SGS_PASSWORD, ANTHROPIC_API_KEY,
# KIMI_API_KEY, DEV_PUBLIC_URL, DEV_USERS, DEV_SUPER, DEV_ADIC_TODOS, DEV_SUPERVISORES_ADICIONALES,
# SGS_LOGIN_<ZONA>/SGS_PASSWORD_<ZONA>…): .env del módulo, con permisos 600 y fuera del control de versiones.
set -a; . /var/www/sgo7/devoluciones-sgs/.env; set +a

# ── Proveedor de OCR ──────────────────────────────────────────────────────────
export KIMI_MODEL="kimi-k2.7-code-highspeed"
export OCR_BACKEND="claude"
export MODELO_OCR="claude-sonnet-5"
export OCR_EFFORT="low"
export OCR_MODO="compacto"

# ── Runtime ───────────────────────────────────────────────────────────────────
export PORT="3040"
export DEV_REQUIRE_AUTH='1'
# Zonas: una zona sin SGS_LOGIN_<ZONA>/SGS_PASSWORD_<ZONA> en el .env es solo observadora
# (nada se escribe en SGS desde sus sesiones).

mkdir -p logs devoluciones_sesiones
# 1 worker (jobs OCR en hilos del propio proceso) + hilos para concurrencia
exec ./venv/bin/gunicorn -w 1 --threads 8 -b 127.0.0.1:3040 --timeout 900 devoluciones_sgs:app
