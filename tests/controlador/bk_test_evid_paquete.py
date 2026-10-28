"""Nombres y orden de los archivos del paquete de evidencias."""
from types import SimpleNamespace

import pytest

from app.services import evid_paquete as ep


def _ev(id, ruta="", nombre="", mime="", hora=None, hora_fin=None, fecha=None, que_muestra=None, nota=None):
    return SimpleNamespace(id=id, ruta=ruta, nombre=nombre, mime=mime, hora=hora, hora_fin=hora_fin,
                           fecha=fecha, que_muestra=que_muestra, nota=nota, en_paquete=None)


@pytest.mark.parametrize("valor, esperado", [
    ("16:52:41", "16:52"), ("7:05", "07:05"), ("16:52", "16:52"), ("24:00", None), ("7:5", None), ("x", None), (None, None),
])
def test_hhmm(valor, esperado):
    assert ep.hhmm(valor) == esperado


@pytest.mark.parametrize("valor, esperado", [
    ("2026-09-24 15:03:14", "2026-09-24"), ("22/09/2026 08:00", "2026-09-22"), ("sin fecha", None), (None, None),
])
def test_fecha_iso(valor, esperado):
    assert ep.fecha_iso(valor) == esperado


def test_sumar_min():
    assert ep.sumar_min("16:50", 3) == "16:53"
    assert ep.sumar_min("23:50", 20) == "00:10"
    assert ep.sumar_min(None, 3) is None
    assert ep.sumar_min("16:50", None) is None


def test_tipo_por_mime_y_extension():
    assert ep.tipo_de(_ev(1, ruta="a.mp4")) == "Video"
    assert ep.tipo_de(_ev(2, mime="image/jpeg")) == "Imagen"
    assert ep.tipo_de(_ev(3, ruta="nota.ogg")) == "Audio"
    assert ep.tipo_de(_ev(4, ruta="correo.msg")) == "Correo"
    assert ep.tipo_de(_ev(5, mime="application/pdf")) == "PDF"
    assert ep.tipo_de(_ev(6, ruta="x.bin")) == "Documento"


def test_palabra_para_nombre_de_archivo():
    assert ep._palabra("Lluvia sin lograr encargue", 32) == "Lluvia_sin_lograr_encargue"
    assert ep._palabra("Vía cerrada: árbol caído", 32) == "Via_cerrada_arbol_caido"
    assert len(ep._palabra("palabra " * 20, 32)) <= 32


def test_nombre_paquete_video_con_rango_y_etiqueta():
    e = _ev(1, ruta="v.mp4", hora="16:50", hora_fin="16:53", que_muestra="Lluvia sin lograr encargue")
    assert ep.nombre_paquete(e, 1) == "Evidencia_1_Video_16-50_a_16-53_Lluvia_sin_lograr_encargue.mp4"


def test_nombre_paquete_correo_lleva_fecha():
    e = _ev(2, ruta="c.msg", fecha="2026-09-22")
    assert ep.nombre_paquete(e, 3, ancho=2) == "Evidencia_03_Correo_22-09-2026.msg"


def test_nombre_paquete_respeta_largo_maximo():
    e = _ev(3, ruta="v.mp4", hora="08:00", que_muestra="x" * 200)
    assert len(ep.nombre_paquete(e, 1)) <= ep.MAX_NOMBRE


def test_numerar_ordena_por_hora_y_comparte_numero_video_imagen():
    evs = [
        _ev(10, ruta="b.jpg", hora="16:50", fecha="2026-09-16"),
        _ev(11, ruta="a.mp4", hora="16:50", fecha="2026-09-16"),
        _ev(12, ruta="c.mp4", hora="07:43", fecha="2026-09-16"),
        _ev(13, ruta="d.msg"),                       # sin hora: al final
        _ev(14, ruta="e.mp4", hora="12:10"),         # sin fecha: toma la fecha más común
    ]
    filas = ep.numerar(evs)
    orden = [(f["e"].id, f["n"]) for f in filas]
    assert orden == [(12, 1), (14, 2), (11, 3), (10, 3), (13, 4)]
    assert filas[2]["tipo"] == "Video" and filas[3]["tipo"] == "Imagen"


def test_numerar_desambigua_dos_imagenes_de_la_misma_hora():
    evs = [_ev(1, ruta="a.jpg", hora="10:00", fecha="2026-09-01"), _ev(2, ruta="b.jpg", hora="10:00", fecha="2026-09-01")]
    nombres = [f["nombre"] for f in ep.numerar(evs)]
    assert nombres[0] == "Evidencia_1_Imagen_10-00.jpg"
    assert nombres[1] == "Evidencia_1_Imagen_10-00_b.jpg"


def test_duplicados_por_sha256(tmp_path):
    a = tmp_path / "a.bin"; b = tmp_path / "b.bin"; c = tmp_path / "c.bin"
    a.write_bytes(b"igual"); b.write_bytes(b"igual"); c.write_bytes(b"distinto")
    evs = [_ev(1, ruta=str(a)), _ev(2, ruta=str(b)), _ev(3, ruta=str(c)), _ev(4, ruta=str(tmp_path / "no_existe"))]
    assert ep.duplicados(evs) == {2: 1}
    assert ep.sha256_archivo(str(a)) == ep.sha256_archivo(str(b))
    assert ep.sha256_archivo(str(tmp_path / "no_existe")) is None


def test_defaults_nueva_deja_fuera_los_audios():
    audio = _ev(1, ruta="nota.ogg")
    video = _ev(2, ruta="v.mp4")
    ep.defaults_nueva(audio); ep.defaults_nueva(video, fecha_txt="2026-09-20 10:00")
    assert audio.en_paquete is False
    assert video.en_paquete is True and video.fecha == "2026-09-20"
