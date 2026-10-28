"""Guardián del cliente de solo lectura del ERP: toda escritura falla antes de salir de la máquina."""
import pytest

import sgs_readonly_client as ro


def _cliente():
    # Sin red: la autenticación nunca se llama. La URL es sintética.
    return ro.SGSReadOnlyClient("http://127.0.0.1:1", "db_prueba", "lectura", "x")


@pytest.mark.parametrize("metodo", ["create", "write", "unlink", "execute_kw"])
def test_metodos_de_escritura_lanzan(metodo):
    c = _cliente()
    with pytest.raises(ro.ReadOnlyViolationError):
        getattr(c, metodo)("project.material", [1], {"amount": 1})


@pytest.mark.parametrize("metodo", [
    "create", "write", "unlink", "delete", "action_confirm", "button_validate", "do_x", "post_y",
    "confirm", "validate", "cancel", "approve", "toggle_active", "set_state", "mark_done", "_apply", "copy",
    "metodo_desconocido",
])
def test_verificar_metodo_bloquea_todo_lo_que_no_es_lectura(metodo):
    c = _cliente()
    with pytest.raises(ro.ReadOnlyViolationError):
        c._verificar_metodo(metodo)


@pytest.mark.parametrize("metodo", sorted(ro._METODOS_LECTURA))
def test_verificar_metodo_deja_pasar_la_lista_blanca(metodo):
    _cliente()._verificar_metodo(metodo)     # no lanza


def test_atributo_desconocido_bloqueado():
    c = _cliente()
    with pytest.raises(ro.ReadOnlyViolationError):
        c.action_cualquier_cosa


def test_lista_blanca_no_contiene_escrituras():
    assert not ({"create", "write", "unlink", "execute_kw", "copy"} & set(ro._METODOS_LECTURA))
