"""
app.py — Servidor del módulo MATERIALES (Zona VII)
==================================================
Independiente del verificador. Se sirve bajo /materiales detrás del proxy
nginx de la plataforma (gunicorn en 127.0.0.1).

Local:  python app.py            → http://127.0.0.1:5052/materiales/
VPS:    gunicorn app:app         (ver start.sh)
"""

import sys
import os

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# La carpeta del bundle debe estar en sys.path para resolver:
#   config, sgs_readonly_client y (de forma plana) sgs_client.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from flask import Flask
import config as cfg

from materiales_proy.blueprint import bp as bp_materiales

app = Flask(__name__)

# ── SGO7: trazabilidad — todo archivo subido, proceso o descarga queda registrado en sabogal ──
try:
    import sgo7_registro
    sgo7_registro.instalar(app, modulo="materiales", acciones={
            r"/materiales/api/analizar": "tablero_analizar",
            r"/materiales/api/export": "tablero_excel"
    })
except Exception as _e_sgo7:
    print("sgo7_registro no instalado:", _e_sgo7)
app.secret_key = cfg.SECRET_KEY
app.register_blueprint(bp_materiales)


@app.route("/healthz")
def _healthz():
    return {"ok": True, "module": "materiales"}


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  MATERIALES — Zona VII (módulo independiente)")
    print(f"  SGS login: {cfg.SGS_LOGIN}  |  puerto 5052")
    print("  Abre: http://127.0.0.1:5052/materiales/")
    print("=" * 60 + "\n")
    app.run(debug=False, port=5052, host="127.0.0.1")
