"""Gestión General: funciones puras del clasificador de correos y del hilo del dinero (sin base ni red)."""
from datetime import datetime

import pytest

from app.services import gestion_flujo as gf


# ── Códigos en el texto ──────────────────────────────────────────────────────────

def test_candidatos_codigo_prioriza_asunto_sobre_cuerpo_y_cadena():
    asunto = "Solicitud MO LCL 6301000001"
    cuerpo = "Buen día, adjunto planilla del DF202600000002.\n\nDe: alguien\nEnviado: ayer\nLCL6301000003 viejo"
    cands = gf.candidatos_codigo(asunto, cuerpo)
    assert cands[0] == {"codigo": "LCL6301000001", "fuente": "asunto"}
    fuentes = {c["codigo"]: c["fuente"] for c in cands}
    assert fuentes["DF202600000002"] == "cuerpo"
    assert fuentes["LCL6301000003"] == "cadena"


def test_partir_cuerpo_separa_el_mensaje_reciente_de_la_cadena():
    arriba, cadena = gf.partir_cuerpo("texto nuevo\n\nDe: Remitente\nmensaje viejo")
    assert arriba.strip() == "texto nuevo"
    assert cadena.lstrip().startswith("De:")
    assert gf.partir_cuerpo("sin cadena") == ("sin cadena", "")


def test_es_correo_lista_con_cuatro_o_mas_codigos():
    muchos = "Órdenes a liquidar: DF202600000001, DF202600000002, DF202600000003 y DF202600000004"
    assert gf.es_correo_lista("Reporte", muchos)
    assert not gf.es_correo_lista("Reporte", "Solo DF202600000001 y DF202600000002")


def test_codigo_propio_lee_el_texto_no_el_asunto():
    assert gf.codigo_propio("favor liquidar el DF 202600000009") == "DF202600000009"
    assert gf.codigo_propio("el LCL 6301000005 queda listo") == "LCL6301000005"
    assert gf.codigo_propio("tabla 6301000006 sin prefijo") == "LCL6301000006"
    assert gf.codigo_propio("sin códigos") is None


def test_clave_de_quita_el_prefijo_lcl():
    assert gf.clave_de("LCL6301000001") == "6301000001"
    assert gf.clave_de("lcl_6301000001") == "6301000001"
    assert gf.clave_de("DF202600000001") == "DF202600000001"


# ── Supervisores y padres ─────────────────────────────────────────────────────────

def test_tokens_nombre_sin_tildes_ni_palabras_cortas():
    assert gf._tokens_nombre("Pérez Gómez de la Hoz") == {"PEREZ", "GOMEZ", "HOZ"}


def test_es_del_supervisor_exige_dos_palabras_en_comun():
    assert gf.es_del_supervisor("APELLIDO1 APELLIDO2 NOMBRE", "NOMBRE APELLIDO1 APELLIDO2")
    assert not gf.es_del_supervisor("APELLIDO1 OTRO", "NOMBRE APELLIDO1 APELLIDO2")
    assert not gf.es_del_supervisor("", "NOMBRE APELLIDO1")


def test_agrupar_padres_funde_los_del_mismo_supervisor_y_zona():
    padres = [
        {"code": "PRO-000002", "name": "6301000001.", "supervisor": "SUP UNO", "zona": "UBATE"},
        {"code": "PRO-000001", "name": "6301000001", "supervisor": "SUP UNO", "zona": "UBATE"},
        {"code": "PRO-000003", "name": "6301000001 LV", "supervisor": "SUP DOS", "zona": "LV&TLC"},
    ]
    out = gf.agrupar_padres(padres)
    assert [p["code"] for p in out] == ["PRO-000001", "PRO-000003"]
    assert out[0]["codes"] == ["PRO-000001", "PRO-000002"]
    assert " + " in out[0]["name"]
    assert out[1]["codes"] == ["PRO-000003"]


def test_hilo_normalizado_quita_prefijos_y_codigos():
    a = gf.hilo_normalizado("RE: RV: Solicitud MO LCL 6301000001 - Cambio de poste")
    b = gf.hilo_normalizado("Solicitud MO LCL 6301000009 - Cambio de poste")
    assert a == b
    assert "6301000001" not in a and "re" != a[:2]


def test_tipo_de_por_codigos_e_hilo():
    assert gf.tipo_de({"df": ["DF1"], "lcl": []}, "x") == "mayor_impacto"
    assert gf.tipo_de({"df": [], "lcl": ["630"]}, "AMP Vereda LV") == "linea_viva"


def test_elegir_caso_sonny_prefiere_el_supervisor_y_luego_el_que_no_es_linea_viva():
    casos = [{"id": 2, "tipo": "linea_viva", "supervisor": "PEREZ GOMEZ"},
             {"id": 1, "tipo": "programado", "supervisor": "LOPEZ DIAZ"}]
    assert gf.elegir_caso_sonny("GOMEZ PEREZ", casos) == 2
    assert gf.elegir_caso_sonny("", casos) == 1
    assert gf.elegir_caso_sonny("", []) is None
    assert gf.elegir_caso_sonny("", casos[:1]) == 2


# ── Sonny, PDL y fechas ──────────────────────────────────────────────────────────

def test_paso_sonny_estado_conocido_y_desconocido():
    paso, _cond, actor, conocido = gf.paso_sonny("Libretos Aprobados")
    assert conocido and paso == 39
    paso2, cond2, _actor2, conocido2 = gf.paso_sonny("Estado inventado")
    assert (paso2, cond2, conocido2) == (28, None, False)
    assert isinstance(actor, str) and actor


@pytest.mark.parametrize("entrada, esperado", [
    ("CDOPP2026R009056", "CDOPP2026R09056"),
    ("cdopp2026r09056", "CDOPP2026R09056"),
    ("CDOPPI2026R00123", "CDOPPI2026R00123"),
    ("DF202600000001", "DF202600000001"),
    ("", ""),
])
def test_pdl_canon(entrada, esperado):
    assert gf.pdl_canon(entrada) == esperado


def test_pdl_norm_y_pdls_de_quitan_sufijos():
    assert gf.pdl_norm("CDOPP2026R009056_1") == "CDOPP2026R09056"
    assert gf.pdl_norm("sin pdl") is None
    assert gf.pdls_de("CDOPP2026R00001_LV y DF202600000002") == ["CDOPP2026R00001", "DF202600000002"]


def test_cuadra_con_tolerancia():
    assert gf.cuadra(71_969_636, 71_969_637)
    assert gf.cuadra(100_000, 100_900)
    assert not gf.cuadra(100_000, 103_000)
    assert gf.cuadra(0, 0)


def test_dias_habiles_no_cuenta_fines_de_semana():
    a = datetime(2026, 10, 2, 9)      # viernes
    b = datetime(2026, 10, 6, 9)      # martes
    assert gf.dias_habiles(a, b) == 2
    assert gf.dias_habiles(b, a) == 0
    assert gf.dias_habiles(None, b) == 0


# ── Motivo del rechazo ───────────────────────────────────────────────────────────

def test_texto_motivo_desescapa_y_normaliza():
    assert gf.texto_motivo("&amp;quot;Hola&amp;quot;  mundo\r\n\r\n\r\nfin") == '"Hola" mundo\n\nfin'
    assert gf.texto_motivo(None) == ""


def test_motivo_rechazo_sin_texto():
    m = gf.motivo_rechazo("")
    assert m["categoria"] == "sin_texto" and m["puntos"] == [] and m["n_puntos"] == 0


def test_motivo_rechazo_clasifica_puntos_y_elige_la_categoria_mayoritaria():
    texto = ("Los soportes del caso fueron rechazados por el siguiente motivo: "
             "1. Corregir el código de matriz en el RG10. "
             "2. La cantidad de poste no coincide con lo instalado. "
             "3. Falta la fotografía del antes.")
    m = gf.motivo_rechazo(texto)
    assert m["n_puntos"] == 3
    assert len(m["puntos"]) == 3
    assert m["categoria"] in m["categorias"]
    assert m["frase_corta"].endswith("y 2 puntos más")
    assert all(p["categoria"] != "sin_texto" for p in m["puntos"])
    assert m["cortado"] is False


def test_motivo_corto_solo_lleva_el_resumen():
    m = gf.motivo_rechazo("Falta la foto del después")
    c = gf.motivo_corto(m)
    assert set(c) == {"categoria", "etiqueta", "subcategoria", "frase_corta", "n_puntos", "categorias", "cortado"}
    assert gf.motivo_corto(None) is None
