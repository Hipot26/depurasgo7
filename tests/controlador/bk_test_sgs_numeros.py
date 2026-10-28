"""Formato numérico del ERP (es_CO): lo que se teclea y lo que se lee."""
import pytest

from browser_client import PlaywrightClient, _cant_sgs

parse = PlaywrightClient._parse_sgs_number


@pytest.mark.parametrize("entrada, esperado", [
    (3, "3"),
    (3.0, "3"),
    ("3.0", "3"),
    (43.5, "43,5"),
    ("43,5", "43,5"),
    ("1.234,5", "1234,5"),
    ("1,234.5", "1234,5"),
    (0.25, "0,25"),
    (2.00004, "2"),           # redondeo a 4 decimales
    (" 7 ", "7"),
    ("12,3400", "12,34"),
])
def test_cant_sgs_teclea_en_es_co(entrada, esperado):
    assert _cant_sgs(entrada) == esperado


def test_cant_sgs_nunca_devuelve_punto_decimal():
    for v in (1.5, "1.5", "1,5", 1000.25):
        assert "." not in _cant_sgs(v)


@pytest.mark.parametrize("texto, esperado", [
    ("1,00", 1.0),
    ("1.00", 1.0),
    ("1.234,56", 1234.56),
    ("1,234.56", 1234.56),
    ("Pendiente: 12,5 UND", 12.5),
    ("-3,25", -3.25),
    ("\xa0 8 \xa0", 8.0),
])
def test_parse_sgs_number_acepta_ambos_formatos(texto, esperado):
    assert parse(texto) == pytest.approx(esperado)


@pytest.mark.parametrize("texto", [None, "", "sin número", "   "])
def test_parse_sgs_number_devuelve_none_sin_numero(texto):
    assert parse(texto) is None


def test_ida_y_vuelta():
    for v in (1.0, 43.5, 1234.25, 0.5):
        assert parse(_cant_sgs(v)) == pytest.approx(v)
