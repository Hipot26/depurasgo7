"""Reglas de acceso de la plataforma central leídas del entorno (listas de cuentas y elaboradores)."""
from app import config


class _Rol:
    def __init__(self, v):
        self.value = v


def test_listas_de_cuentas_vienen_del_entorno():
    assert config.USUARIOS_LIQUIDADOR == {"cuenta.liq"}
    assert config.USUARIOS_NODOS == {"cuenta.nodos", "cuenta.directivo"}      # espacios tolerados
    assert config.USUARIOS_AUDITORIA_WA == set()                               # vacío = nadie habilitado


def test_puede_liquidador_por_rol_y_por_cuenta():
    assert config.puede_liquidador(_Rol("desarrollador"), "cualquiera")
    assert config.puede_liquidador(_Rol("ingeniero_zona"), "cuenta.liq")
    assert not config.puede_liquidador(_Rol("ingeniero_zona"), "otra")


def test_elaboradores_desde_json():
    assert set(config.ELABORADORES) == {"cuenta.a", "cuenta.b"}
    assert config.es_elaborador("cuenta.a") and not config.es_elaborador("nadie")


def test_cuenta_de_sigla_por_tokens_completos_sin_tildes():
    assert config.cuenta_de_sigla("APELLIDOA NOMBREA (CO00000000)") == "cuenta.a"
    assert config.cuenta_de_sigla("apellidoá nombreá") == "cuenta.a"
    assert config.cuenta_de_sigla("APELLIDOB OTRO") == "cuenta.b"
    assert config.cuenta_de_sigla("APELLIDOAX NOMBREA") is None            # subcadena no cuenta
    assert config.cuenta_de_sigla("") is None and config.cuenta_de_sigla(None) is None


def test_rol_efectivo_en_el_ans():
    assert config.rol_pend_ans(_Rol("programador"), "x") == "elaborador"
    assert config.rol_pend_ans(_Rol("cumplimentador"), "x") == "elaborador"
    assert config.rol_pend_ans(_Rol("ingeniero_zona"), "cuenta.nodos") == "ingeniero_zona"
    assert config.rol_pend_ans(_Rol("ingeniero_zona"), "otra") == "ing_zona"
