#!/bin/bash
cd /var/www/sgo7/sony-sgs/nuevo
set -a; . /var/www/sgo7/sony-sgs/nuevo.env; set +a
exec /var/www/sgo7/sony-sgs/venv/bin/gunicorn -w 2 -b 127.0.0.1:3006 --timeout 300 app:app
