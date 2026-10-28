"""Normalización de nodos, cables y códigos de caso para el cruce planilla ↔ ERP."""
import pytest

from balance_liquidar import _nodo_endpoint_key, _normalizar_nodo_sip
from integracion import (
    _extraer_codigos_caso,
    _nodo_key_sgs,
    _normalizar_codigo_caso,
    _normalizar_desc,
)


@pytest.mark.parametrize("texto, esperado", [
    ("1", "1"),
    (" 1a ", "1A"),
    ("NODO 4", "4"),
    ("N4AL6", "4AL6"),
    ("4AL6", "4AL6"),
    ("N1_N2", "1AL2"),
    ("1 al 2", "1AL2"),
    ("4-23", "4AL23"),
    ("23-4", "4AL23"),          # mismo cable en cualquier sentido
    ("N23 AL N4", "4AL23"),
    ("6 - 1.1", "611"),         # compacto cuando no es un tramo
])
def test_normalizar_nodo_sip(texto, esperado):
    assert _normalizar_nodo_sip(texto) == esperado


@pytest.mark.parametrize("texto", ["", None, "SIN NODO", "—"])
def test_normalizar_nodo_sip_sin_digitos(texto):
    assert _normalizar_nodo_sip(texto) == ""


def test_orden_natural_de_extremos():
    claves = sorted(["23A", "4", "23", "100"], key=_nodo_endpoint_key)
    assert claves == ["4", "23", "23A", "100"]


@pytest.mark.parametrize("nombre, nodo, esperado", [
    ("6301000001 NODO 4-23", "23", "4AL23"),
    ("6301000001 NODO 4 AL 23", "23", "4AL23"),
    ("6301000001 NODO 4- NODO 23", "23", "4AL23"),
    ("6301000001 NODO 4 NODO 23", "4", "4AL23"),
    ("6301000001 NODO 7", "7", "7"),
    ("TRAMO RED N2 AL N5", "", "2AL5"),
    ("6301000001 N10_N23", "", "10AL23"),
])
def test_nodo_key_sgs(nombre, nodo, esperado):
    assert _nodo_key_sgs({"name": nombre, "nodo": nodo}) == esperado


@pytest.mark.parametrize("valor, esperado", [
    ("6301000001", "6301000001"),
    (" lcl-6301000001 ", "LCL6301000001"),
    ("0", ""),
    ("000", ""),
    ("—", ""),
    (None, ""),
    (False, ""),
])
def test_normalizar_codigo_caso(valor, esperado):
    assert _normalizar_codigo_caso(valor) == esperado


def test_extraer_codigos_caso_encuentra_lcl_y_df():
    codigos = _extraer_codigos_caso("Orden LCL 6301000001 / DF202600000001")
    assert "6301000001" in codigos
    assert "DF202600000001" in codigos
    assert _extraer_codigos_caso("") == set()


def test_normalizar_desc_quita_tildes_y_puntuacion():
    assert _normalizar_desc("  Instalación DE Poste; BT (12 m) ") == "instalacion de poste bt 12 m"
    assert _normalizar_desc("") == ""
