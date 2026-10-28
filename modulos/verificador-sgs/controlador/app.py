"""
app.py — Verificador y Balance Final SGS (módulo SGO7)
======================================================
Registra dos módulos:
  / (root)       → Balance Final SGS  (balance_proy.blueprint)
  /verificador/  → Verificador SGS    (verificador_proy.blueprint)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask
from balance_proy.blueprint import bp as bp_balance
from verificador_proy.blueprint import bp as bp_verificador

app = Flask(__name__, template_folder="templates")

# ── SGO7: trazabilidad — todo archivo subido, proceso o descarga queda registrado en sabogal ──
try:
    import sgo7_registro
    sgo7_registro.instalar(app, modulo="verificador", acciones={
            r"/verificador/api/auto/export": "control_excel",
            r"/verificador/api/auto": "control_mes",
            r"/verificador/api/correo/[^/]+/outlook": "control_abrir_correo_outlook",
            r"/verificador/api/validado-correos/excel": "control_validado_correos_excel",
            r"/verificador/api/validado-correos": "control_validado_correos",
            r"/verificador/api/plantilla-mat": "control_plantilla_mat",
            r"/verificador/api/cruce": "control_cruce_excel",
            r"/verificador/api/verificar": "control_verificar",
            r"/api/": "balance_final_sgs"
    })
except Exception as _e_sgo7:
    print("sgo7_registro no instalado:", _e_sgo7)
app.secret_key = os.environ.get("VERIFICADOR_SECRET", "")

app.register_blueprint(bp_balance)
app.register_blueprint(bp_verificador)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3009))
    app.run(host="0.0.0.0", port=port, debug=False)
