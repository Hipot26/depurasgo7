#!/bin/bash
cd /var/www/sgo7/sgs-ui
set -a; . /var/www/sgo7/sgs-ui/.env; set +a   # secretos: .env del módulo (600, fuera de git)
export SECRET_KEY="$(cat /var/www/sgo7/sgs-ui/.secret_key)"
mkdir -p logs
# SGO7 modo servidor: Chromium sin ventana y sesión SGS automática (ver blueprint liquidador2)
export PW_HEADLESS=1
export PW_AUTO_SESION=1
# Xvfb: algunos flujos abren Chromium "visible"; en el servidor corre en una pantalla virtual.
exec xvfb-run -a -s "-screen 0 1600x900x24" /var/www/sgo7/sgs-ui/venv/bin/gunicorn -w 1 --threads 8 -b 127.0.0.1:5001 --timeout 1800 app:app
