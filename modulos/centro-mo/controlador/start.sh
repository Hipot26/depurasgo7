#!/bin/bash
# start.sh — Centro MO (módulo SGO7) en 127.0.0.1:5088, usuario sgo7 (PM2 `centro-mo`).
# UN solo worker: el estado vive en memoria y se persiste en data/centro/*.json.
cd /var/www/sgo7/centro-mo
set -a; . /var/www/sgo7/centro-mo/.env; set +a          # SGO7_INTERNAL_TOKEN, CENTRO_MO_AGENTE_TOKEN
export NUEVO_DIR=/var/www/sgo7/sony-sgs/nuevo           # librería del Balance (sesiones compartidas con :3006)
export CENTRO_MO_PLANILLAS=/var/www/sgo7/registros/centro-mo
export CENTRO_MO_DATA=/var/www/sgo7/centro-mo/data/centro
export SGO7_MODULO=centro-mo
export LIQUIDADOR2_BASE=http://127.0.0.1:5001/liquidador2
export LIQUIDADOR2_PUBLIC=/liquidador2
export BALANCE_PUBLIC=/balance
exec /var/www/sgo7/centro-mo/venv/bin/gunicorn -w 1 --threads 8 -b 127.0.0.1:5088 --timeout 900 app:app
