#!/usr/bin/env python3
"""reconsultar_casos.py — Reconsulta automática de los casos de facturación (cron, cada 30 min).

Toma del backend los casos que siguen con descuadre o en espera de almacén y cuya hora de
reconsulta ya llegó, corre el mismo cruce SIPWEBORA ↔ SGS que el Balance Integrado y manda el
semáforo nuevo. Si ya cuadra, el caso pasa solo a «Cuadra» y el facturador lo ve en su panel.

Horario: lunes a sábado, 06:00–19:59 hora de Colombia (fuera de eso no hace nada; `--forzar` lo salta).
Corre como usuario sgo7 con el venv y el entorno del Balance (ver /etc/cron.d/sgo7-casos).
"""
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import sgo7_casos  # noqa: E402

TZ_CO = timezone(timedelta(hours=-5))


def main():
    ahora = datetime.now(TZ_CO)
    forzar = "--forzar" in sys.argv
    print(f"[{ahora:%Y-%m-%d %H:%M}] reconsulta de casos")
    if not forzar and (ahora.weekday() == 6 or not (6 <= ahora.hour < 20)):
        print("   fuera de horario (L-S 06-20 CO); nada que hacer")
        return
    casos = sgo7_casos.pendientes_reconsulta(limite=int(os.environ.get("SGO7_RECONSULTA_MAX", "8")))
    if not casos:
        print("   sin casos pendientes de reconsulta")
        return
    from integracion import BalanceIntegrado, cfg_desde_archivo
    cliente = None
    for c in casos:
        t0 = time.time()
        try:
            if cliente is None:
                cliente = BalanceIntegrado(**cfg_desde_archivo())
            planilla = str(c.get("id_planilla") or "").replace("+", ",")
            proyecto = (c.get("proyecto_id") or "").strip() or (str(c["parent_id"]) if c.get("parent_id") else None)
            datos = cliente.preparar_cruce(planilla, proyecto_id_sgs=proyecto)
            base = cliente._generar_excel(datos["sip_datos"], datos["sgs_data"], datos["id_label"],
                                          advertencias=datos["advertencias"])
            r = sgo7_casos.registrar_consulta(datos, base, usuario="sistema", rol="auto", origen="auto", proyecto_id=proyecto)
            if r:
                print(f"   caso {c['id']} ({planilla}) → {r.get('estado')} · OK {r.get('ok')} · rojos {r.get('rojos')} · {time.time() - t0:.0f}s")
            else:
                print(f"   caso {c['id']} ({planilla}) → el backend no registró la consulta")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            sgo7_casos.reconsulta_error(c.get("id"), msg)
            print(f"   caso {c.get('id')} ERROR {msg[:300]} · {time.time() - t0:.0f}s")
            if forzar:
                traceback.print_exc()


if __name__ == "__main__":
    main()
