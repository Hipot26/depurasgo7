#!/bin/bash
cd /var/www/sgo7/verificador-sgs
# Secretos y conexión a SGS: .env del módulo, con permisos 600 y fuera del control de versiones.
set -a; . /var/www/sgo7/verificador-sgs/.env; set +a
exec ./venv/bin/gunicorn -w 2 -b 127.0.0.1:3009 --timeout 900 app:app
