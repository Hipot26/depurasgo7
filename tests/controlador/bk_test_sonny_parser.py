"""Parser del acta de Sonny (HTML) con una planilla mínima inventada."""
from balance_liquidar import extraer_datos


def _td(texto, colspan=1):
    return f'<td colspan="{colspan}">{texto}</td>'


def _fila(*celdas):
    return "<tr>" + "".join(celdas) + "</tr>"


def _html_planilla():
    filas = [
        _fila(_td("Empresa Colaboradora", 10), _td("Contratista de prueba", 20)),
        _fila(_td("OdM / Grafo", 31), _td("", 55), _td("LCL", 30)),
        # valores: pos 1 (cs 31) OdM · pos 87 (cs 30) LCL · pos 119 COD CTO · pos 125 municipio
        _fila(_td("", 1), _td("DF202600000001", 31), _td("", 55), _td("6301000001", 30),
              _td("", 2), _td("CTO-1", 6), _td("Municipio X", 10)),
        _fila(_td("Ticket", 10), _td("PDL", 10)),
        # valores: pos 45 (cs 24) ticket · pos 71 (cs 45) zona · pos 118 código matriz
        _fila(_td("", 45), _td("CDOPP2026R00001", 24), _td("", 2), _td("Zona de prueba", 45), _td("", 2), _td("E1-77", 5)),
        _fila(_td("Fecha de ejecución 15/09/2026", 40)),
        # cabecera de mano de obra
        _fila(_td("Nodo", 7), _td("Descripción de la Actividad", 60), _td("Prestación", 11)),
        # filas MO: nodo (cs 7) · prestación (cs 11) · descripción (cs 42) · unidad (cs 6) · cantidad (cs 6)
        _fila(_td("N1", 7), _td("QP0001", 11), _td("CAMBIO DE POSTE", 42), _td("C/U", 6), _td("2", 6)),
        _fila(_td("1 al 2", 7), _td("QP0002", 11), _td("TENDIDO DE RED", 42), _td("m", 6), _td("12,5", 6)),
        _fila(_td("VALOR MANO OBRA", 60)),
        # cabecera de materiales
        _fila(_td("Cod. Material", 10), _td("I N", 4), _td("I RZ", 4)),
        # fila MAT: nodo (cs 7, pos 0) · código (pos 48, cs 10) · I N (pos 58) · unidad (pos 78, cs 9) · desc (pos 99)
        _fila(_td("N1", 7), _td("", 41), _td("900001", 10), _td("3", 4), _td("", 16), _td("UND", 9),
              _td("", 12), _td("POSTE 12 m", 52)),
        _fila(_td("CONVENCIONES MATERIALES", 60)),
        _fila(_td("TOTAL TRABAJO $1,000.00 $250.50", 60)),
    ]
    return "<html><head><title>Acta de prueba</title></head><body><table>" + "".join(filas) + "</table></body></html>"


def test_metadatos():
    d = extraer_datos(_html_planilla())
    m = d["metadata"]
    assert m["empresa"] == "Contratista de prueba"
    assert m["odm"] == "DF202600000001"
    assert m["lcl"] == "6301000001"
    assert m["cod_cto"] == "CTO-1"
    assert m["municipio"] == "Municipio X"
    assert m["ticket"] == "CDOPP2026R00001"
    assert m["zona_nombre"] == "Zona de prueba"
    assert m["codigo_matriz"] == "E1-77"
    assert m["fecha_ejecucion"] == "15/09/2026"
    assert m["titulo"] == "Acta de prueba"
    assert m["total_mo"] == "$1,000.00"
    assert m["total_mat"] == "$250.50"


def test_mano_de_obra_normaliza_nodos_y_cantidades():
    mo = extraer_datos(_html_planilla())["mo"]
    assert [(f["nodo"], f["codigo"], f["cantidad"]) for f in mo] == [
        ("1", "QP0001", 2.0),
        ("1AL2", "QP0002", 12.5),
    ]
    assert mo[0]["desc"] == "CAMBIO DE POSTE"
    assert mo[0]["unidad"] == "C/U"


def test_materiales():
    mat = extraer_datos(_html_planilla())["mat"]
    assert len(mat) == 1
    assert mat[0]["nodo"] == "1"
    assert mat[0]["codigo"] == "900001"
    assert mat[0]["i_n"] == 3.0
    assert mat[0]["unidad"] == "UND"
    assert mat[0]["desc"] == "POSTE 12 m"


def test_planilla_sin_secciones_no_falla(capsys):
    d = extraer_datos("<html><body><table><tr><td>vacía</td></tr></table></body></html>")
    assert d["mo"] == [] and d["mat"] == []
    assert "Mano de Obra" in capsys.readouterr().out
