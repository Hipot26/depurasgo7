#!/bin/bash
cd /var/www/sgo7/materiales-sgs
# Secretos y conexión a SGS (SGS_URL, SGS_LOGIN, SGS_PASSWORD, WEB_USER, WEB_PASS, APP_SECRET): .env del
# módulo, con permisos 600 y fuera del control de versiones.
set -a; . /var/www/sgo7/materiales-sgs/.env; set +a

# ── Capacidades que desbloquea la cuenta de lectura ampliada ──
export USAR_FECHA_ALMACEN="1"
export USAR_COSTOS="1"

exec ./venv/bin/gunicorn -w 2 --threads 4 -b 127.0.0.1:3004 --timeout 900 app:app
