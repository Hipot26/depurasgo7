"""Registro de trazabilidad de los módulos: huella de archivos y filtrado de campos sensibles."""
import hashlib
import io

import sgo7_registro
from flask import Flask


def _app():
    app = Flask("prueba")
    app.config["TESTING"] = True
    return app


def test_form_quita_campos_que_parecen_contrasena():
    app = _app()
    with app.test_request_context("/accion", method="POST",
                                  data={"usuario": "x", "password": "s", "clave_sgs": "s", "token": "t",
                                        "cookie_x": "c", "observacion": "o" * 500}):
        campos = sgo7_registro._form()
    assert "password" not in campos and "clave_sgs" not in campos and "token" not in campos and "cookie_x" not in campos
    assert campos["usuario"] == "x"
    assert len(campos["observacion"]) == 300           # recorte


def test_form_lee_json_y_filtra_sensibles():
    app = _app()
    with app.test_request_context("/accion", method="POST", json={"a": 1, "secret": "x", "b": None, "c": "t" * 400}):
        campos = sgo7_registro._form()
    assert campos == {"a": 1, "b": None, "c": "t" * 300}


def test_archivos_calcula_sha256_y_no_copia(monkeypatch):
    app = _app()
    contenido = b"planilla de prueba " * 100
    monkeypatch.setattr(sgo7_registro, "_guardar_copia", lambda fs, sha, nombre: None)
    with app.test_request_context("/subir", method="POST",
                                  data={"archivo": (io.BytesIO(contenido), "planilla.xlsx")}):
        archivos = sgo7_registro._archivos()
    assert len(archivos) == 1
    nombre, n, sha, copia = archivos[0]
    assert nombre == "planilla.xlsx"
    assert n == len(contenido)
    assert sha == hashlib.sha256(contenido).hexdigest()
    assert copia is None


def test_regex_de_sesion_reconoce_uuid_y_marca_de_tiempo():
    assert sgo7_registro._RE_SESION.search("/liquidador2/verificar/123e4567-e89b-12d3-a456-426614174000")
    assert sgo7_registro._RE_SESION.search("/devoluciones/sesion/20261001-093015")
    assert not sgo7_registro._RE_SESION.search("/liquidador2/subir")


def test_patron_sensible():
    for k in ("password", "PASS", "clave", "secret", "token", "contrasena", "cookie"):
        assert sgo7_registro._SENSIBLE.search(k)
    assert not sgo7_registro._SENSIBLE.search("usuario")
