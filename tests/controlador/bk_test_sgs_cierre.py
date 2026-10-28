"""Regla de cierre mensual del ERP: detección del rango en el mensaje del ERP (devuelve las fechas como texto ISO)."""
import sgs_cierre


def test_detectar_rango_en_el_mensaje_del_erp():
    msg = ("La fecha de avance esta fuera del rango de produccion en curso: 2026-10-03 - 2026-11-03")
    assert sgs_cierre.detectar(msg) == ("2026-10-03", "2026-11-03")


def test_detectar_tolera_tilde_y_sin_dos_puntos():
    msg = "fuera del rango de producción en curso 2026-10-03 - 2026-11-03"
    assert sgs_cierre.detectar(msg) == ("2026-10-03", "2026-11-03")


def test_detectar_devuelve_none_para_otros_errores():
    assert sgs_cierre.detectar("No puedes consumir mas material del proyectado") is None
    assert sgs_cierre.detectar("") is None
    assert sgs_cierre.detectar(None) is None
