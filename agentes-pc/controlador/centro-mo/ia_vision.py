# -*- coding: utf-8 -*-
"""
ia_vision.py — "Ojos" del Centro MO: lee codigos LCL/DF/PDL en imagenes de
correos. Proveedor principal: Cloudflare Workers AI (mismo montaje que el ANS
de SG07). MiniMax queda como respaldo conmutable.

Contrato con el resto del proyecto (NO cambia):
    leer_codigos_en_imagen(ruta) -> ({"lcl","df","pdl"}, None) | (None, motivo)
Si algo falla la funcion devuelve (None, motivo) y el correo cae al carril
semiautomatico. La IA NUNCA es bloqueante.

--------------------------------------------------------------------------
Por que tres capas y no una sola llamada (leccion de SG07, 2026-09-07)
--------------------------------------------------------------------------
Pedirle a un modelo "extrae el codigo" mezcla dos trabajos distintos: LEER y
DECIDIR. El modelo lee bien y decide regular, asi que se separan:

  1. TRANSCRIPCION (vision, literal).  El modelo de vision solo copia lo que
     ve. No interpreta, no concluye, no elige. Es su trabajo fuerte.
  2. CAPA MECANICA (regex, determinista).  Sobre ese texto corren las MISMAS
     expresiones regulares que usa el resto del proyecto. Lo que cae aqui es
     exacto y no depende del humor del modelo. Esta capa manda.
  3. CAPA INTERPRETATIVA (modelo de texto + json_schema).  Solo para lo que la
     regex no encontro: codigos partidos en dos lineas, con ruido de OCR, o
     etiquetados de forma rara. Va con esquema JSON forzado.

Si la capa 2 y la capa 3 se contradicen, gana la 2 y queda anotado. Todo lo
que sale se valida de forma antes de devolverse: un codigo con forma invalida
se descarta, nunca se inventa.

--------------------------------------------------------------------------
Configuracion — config_ia.json, junto a este script, NUNCA en el codigo
--------------------------------------------------------------------------
    {
      "proveedor": "auto",            // cloudflare | minimax | auto
      "cf_account_id": "...",
      "cf_ai_token": "...",           // token con permiso Workers AI
      "cf_gateway": "cazador-mo",     // opcional: AI Gateway (registro + costo)
      "cf_model_vision": "@cf/meta/llama-3.2-11b-vision-instruct",
      "cf_model_texto":  "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
      "cf_timeout_s": 90
    }
Las llaves se ponen con:  python configurar_cloudflare.py <ACCOUNT_ID> <TOKEN>
Prueba:                   python probar_ia.py <imagen.png>

Gotchas de Workers AI verificados en produccion (SG07):
  - El endpoint compatible con OpenAI RECHAZA imagenes en llama-3.2-vision
    ("Unable to add image..."). Por eso la vision va por el endpoint NATIVO.
  - Ese endpoint exige que TODOS los mensajes lleven el mismo tipo de content.
    Como el system es string, el user va como UN solo string.
  - response_format recibe el esquema JSON CRUDO, sin envoltorio name/strict.
"""
import os
import io
import re
import json
import base64
import urllib.error
import urllib.request

DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
CONFIG_IA = os.path.join(DIR_SCRIPT, "config_ia.json")

_DEFAULTS = {
    # -- proveedor --------------------------------------------------------
    "proveedor": "auto",          # cloudflare | minimax | auto (cf y si falla minimax)
    # -- Cloudflare Workers AI --------------------------------------------
    "cf_account_id": "",
    "cf_ai_token": "",
    "cf_gateway": "",             # vacio = pegarle directo a la API de Cloudflare
    "cf_model_vision": "@cf/meta/llama-3.2-11b-vision-instruct",
    "cf_model_texto": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "cf_timeout_s": 90,
    # El modelo de vision se atasca repitiendo bloques en formularios densos
    # (medido 2026-09-10 con un plan de trabajo real: 37 lineas repetidas de 90,
    # 33 s). Con 1.1 bajan a 2 de 53 y a 28 s, sin perder ningun codigo.
    "cf_repeticion_penalty": 1.1,
    "cf_max_repes": 2,            # veces que se tolera una linea antes de recortarla
    # -- MiniMax (legado, respaldo) ---------------------------------------
    "api_key": "",
    "endpoint": "https://api.minimax.io/v1/text/chatcompletion_v2",
    "modelo": "MiniMax-M3",
    "timeout_s": 120,
    # -- imagen ------------------------------------------------------------
    "lado_max_px": 1600,          # 0 = no redimensionar
}


def cargar_config() -> dict:
    cfg = dict(_DEFAULTS)
    try:
        with open(CONFIG_IA, encoding="utf-8") as f:
            cfg.update({k: v for k, v in (json.load(f) or {}).items() if v not in (None, "")})
    except Exception:
        pass
    return cfg


def configurado_cf(cfg=None) -> bool:
    cfg = cfg or cargar_config()
    return bool(cfg.get("cf_account_id") and cfg.get("cf_ai_token"))


def configurado_minimax(cfg=None) -> bool:
    cfg = cfg or cargar_config()
    return bool(cfg.get("api_key"))


# ============================================================== CAPA MECANICA
# Espejo de balance_mo_bridge.RE_* — se duplican a proposito para que este
# modulo no arrastre win32com (asi se puede probar sin Outlook). Si alli
# cambian las formas, cambiarlas aqui tambien.
RE_DF = re.compile(r"(?<![A-Za-z0-9])DF[\s_\-]?(\d{10,13})(?!\d)", re.IGNORECASE)
RE_LCL = re.compile(r"(?<![A-Za-z0-9])LCL[\s_\-]?(\d{8,12})(?!\d)", re.IGNORECASE)
RE_LCL_PELADA = re.compile(r"(?<![0-9])(630\d{7})(?!\d)")
RE_PDL = re.compile(r"(?<![A-Za-z0-9])(CDOPP[A-Z0-9]{6,14})(?![A-Z0-9])", re.IGNORECASE)

# El modelo de vision confunde glifos con digitos al leer letreros y capturas.
# Mismo arreglo que el OCR del ANS, aplicado SOLO dentro de un codigo ya
# detectado, nunca sobre el texto libre.
_GLIFOS = {"O": "0", "o": "0", "Q": "0", "D": "0",
           "I": "1", "l": "1", "|": "1",
           "S": "5", "s": "5", "B": "8", "G": "6", "Z": "2"}


def _solo_digitos(s: str) -> str:
    return "".join(_GLIFOS.get(c, c) for c in s if c.isalnum())


def _norm_lcl(bruto: str) -> str:
    """LCL valido = 'LCL' + 8..12 digitos. Devuelve '' si no valida."""
    d = _solo_digitos(re.sub(r"(?i)^lcl", "", (bruto or "").strip()))
    d = "".join(c for c in d if c.isdigit())
    return f"LCL{d}" if 8 <= len(d) <= 12 else ""


def _norm_df(bruto: str) -> str:
    """DF valido = 'DF' + 10..13 digitos."""
    d = _solo_digitos(re.sub(r"(?i)^df", "", (bruto or "").strip()))
    d = "".join(c for c in d if c.isdigit())
    return f"DF{d}" if 10 <= len(d) <= 13 else ""


def _norm_pdl(bruto: str) -> str:
    """PDL valido = 'CDOPP' + 6..14 alfanumericos."""
    s = (bruto or "").strip().upper().replace(" ", "").replace("-", "")
    m = RE_PDL.search(s)
    if m:
        return m.group(1).upper()
    return ""


def codigos_por_regex(texto: str) -> dict:
    """CAPA 2 — determinista. Extrae codigos del texto transcrito.
    Es la fuente mas confiable: no interpreta, solo reconoce formas."""
    t = texto or ""
    out = {"lcl": "", "df": "", "pdl": ""}
    m = RE_LCL.search(t)
    if m:
        out["lcl"] = _norm_lcl(m.group(1))
    if not out["lcl"]:
        m = RE_LCL_PELADA.search(t)
        if m:
            out["lcl"] = _norm_lcl(m.group(1))
    m = RE_DF.search(t)
    if m:
        out["df"] = _norm_df(m.group(1))
    m = RE_PDL.search(t)
    if m:
        out["pdl"] = _norm_pdl(m.group(1))
    return out


def _validar(d: dict) -> dict:
    """Normaliza y descarta lo que no tiene forma valida."""
    return {
        "lcl": _norm_lcl(str((d or {}).get("lcl") or "")),
        "df": _norm_df(str((d or {}).get("df") or "")),
        "pdl": _norm_pdl(str((d or {}).get("pdl") or "")),
    }


# ================================================================== TRANSPORTE
def _post(url: str, cuerpo: dict, token: str, timeout: float):
    """POST JSON con urllib (sin dependencias). -> (dict, None) | (None, motivo)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(cuerpo).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace")), None
    except urllib.error.HTTPError as e:
        detalle = ""
        try:
            detalle = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return None, f"HTTP {e.code} {detalle}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _leer_imagen_data_url(ruta: str):
    """Lee la imagen, la reduce si es enorme (menos base64 = mas rapido y
    menos tokens) y devuelve (data_url, None) | (None, motivo)."""
    cfg = cargar_config()
    ext = os.path.splitext(ruta)[1].lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    try:
        with open(ruta, "rb") as f:
            crudo = f.read()
    except OSError as e:
        return None, f"No se pudo leer la imagen: {e}"

    lado = int(cfg.get("lado_max_px") or 0)
    if lado > 0:
        try:                                   # PIL es opcional: si no esta, se manda tal cual
            from PIL import Image
            im = Image.open(io.BytesIO(crudo))
            if max(im.size) > lado:
                im.thumbnail((lado, lado), Image.LANCZOS)
                buf = io.BytesIO()
                im.convert("RGB").save(buf, format="JPEG", quality=88)
                crudo, ext = buf.getvalue(), "jpeg"
        except Exception:
            pass

    b64 = base64.b64encode(crudo).decode("ascii")
    return f"data:image/{ext};base64,{b64}", None


# ========================================================= CLOUDFLARE: VISION
_INSTR_TRANSCRIBIR = (
    "Eres un transcriptor, no un analista. Copia en espanol TODO el texto legible de esta "
    "imagen, linea por linea, tal cual aparece: codigos, numeros, fechas, horas, nombres, "
    "encabezados, celdas de tabla, asunto y cuerpo de correo. Respeta cada caracter de los "
    "codigos; no corrijas ni completes nada. Si un texto esta borroso, escribelo igual y "
    "marca (ilegible) al lado. No resumas, no interpretes, no saques conclusiones."
)


def _urls_vision(cfg: dict) -> list:
    """Endpoint NATIVO (el compatible con OpenAI rechaza imagenes en este modelo).
    Primero por el AI Gateway si esta configurado, si falla directo."""
    acc, modelo = cfg["cf_account_id"], cfg["cf_model_vision"]
    urls = []
    gw = (cfg.get("cf_gateway") or "").strip()
    if gw:
        urls.append(f"https://gateway.ai.cloudflare.com/v1/{acc}/{gw}/workers-ai/{modelo}")
    urls.append(f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/{modelo}")
    return urls


def _recortar_repeticion(texto: str, max_repes: int = 2) -> str:
    """Red de seguridad contra el bucle del modelo de vision: si una linea ya
    salio `max_repes` veces, las siguientes se tiran. Conserva el ORDEN y la
    primera aparicion de todo, que es donde estan los codigos."""
    vistas, salida = {}, []
    for linea in (texto or "").splitlines():
        clave = linea.strip()
        if not clave:
            salida.append(linea)
            continue
        vistas[clave] = vistas.get(clave, 0) + 1
        if vistas[clave] <= max_repes:
            salida.append(linea)
    return "\n".join(salida)


def transcribir_imagen(ruta: str, cfg=None):
    """CAPA 1 — el modelo de vision copia la imagen a texto literal.
    -> (texto, None) | (None, motivo)."""
    cfg = cfg or cargar_config()
    if not configurado_cf(cfg):
        return None, "Cloudflare no configurado (falta cf_account_id / cf_ai_token)"
    data_url, err = _leer_imagen_data_url(ruta)
    if err:
        return None, err

    cuerpo = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": _INSTR_TRANSCRIBIR},
        {"type": "image_url", "image_url": {"url": data_url}}]}],
        "max_tokens": 900, "temperature": 0.0,
        "repetition_penalty": float(cfg.get("cf_repeticion_penalty") or 1.1)}

    ultimo = ""
    for url in _urls_vision(cfg):
        d, err = _post(url, cuerpo, cfg["cf_ai_token"], float(cfg["cf_timeout_s"]))
        if err:
            ultimo = err
            continue
        texto = None
        if isinstance(d.get("result"), dict):
            texto = d["result"].get("response")
        if texto is None and isinstance(d.get("choices"), list) and d["choices"]:
            texto = (d["choices"][0].get("message") or {}).get("content")
        if isinstance(texto, str) and texto.strip():
            return _recortar_repeticion(texto, int(cfg.get("cf_max_repes") or 2)), None
        ultimo = f"respuesta sin texto: {str(d)[:200]}"
    return None, f"Vision Cloudflare fallo ({ultimo})"


# ==================================================== CLOUDFLARE: INTERPRETAR
_SCHEMA_CODIGOS = {
    "type": "object",
    "properties": {
        "lcl": {"type": "string"},
        "df": {"type": "string"},
        "pdl": {"type": "string"},
    },
    "required": ["lcl", "df", "pdl"],
    "additionalProperties": False,
}

_SYSTEM_CODIGOS = (
    "Eres un extractor de identificadores de proyectos electricos en Colombia. Te dan la "
    "TRANSCRIPCION literal de una captura de pantalla. Busca solo estos identificadores:\n"
    "- lcl: 8 a 12 digitos, etiquetado LCL, pedido, solicitud o similar (ej: LCL6300996351).\n"
    "- df: empieza por DF seguido de 10 a 13 digitos (ej: DF202664572348).\n"
    "- pdl: empieza por CDOPP (ej: CDOPP2026R06431), a veces etiquetado PDL o planilla.\n"
    "Reglas duras: copia el codigo EXACTO de la transcripcion, sin completar ni corregir "
    "digitos. Si un identificador no aparece, devuelve cadena vacia. No inventes: ante la "
    "duda, vacio. Responde UNICAMENTE con el JSON pedido, sin texto alrededor."
)


def _url_texto(cfg: dict) -> str:
    acc = cfg["cf_account_id"]
    gw = (cfg.get("cf_gateway") or "").strip()
    if gw:
        return f"https://gateway.ai.cloudflare.com/v1/{acc}/{gw}/workers-ai/v1/chat/completions"
    return f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/v1/chat/completions"


def _extraer_json(texto) -> dict:
    if isinstance(texto, dict):
        return texto
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (texto or "").strip(), flags=re.S)
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except ValueError:
            return {}


def codigos_por_modelo(transcripcion: str, cfg=None):
    """CAPA 3 — el modelo de texto interpreta la transcripcion con esquema
    forzado. -> (dict, None) | (None, motivo)."""
    cfg = cfg or cargar_config()
    cuerpo = {
        "model": cfg["cf_model_texto"],
        # Workers AI exige el MISMO tipo de content en todos los mensajes:
        # system es string, asi que el user va como UN solo string.
        "messages": [{"role": "system", "content": _SYSTEM_CODIGOS},
                     {"role": "user", "content": f"TRANSCRIPCION:\n{transcripcion}"}],
        "response_format": {"type": "json_schema", "json_schema": _SCHEMA_CODIGOS},
        "max_tokens": 300,
        "temperature": 0.0,
    }
    d, err = _post(_url_texto(cfg), cuerpo, cfg["cf_ai_token"], float(cfg["cf_timeout_s"]))
    if err:
        return None, f"Texto Cloudflare fallo ({err})"
    texto = None
    if isinstance(d.get("choices"), list) and d["choices"]:
        texto = (d["choices"][0].get("message") or {}).get("content")
    elif isinstance(d.get("result"), dict):
        texto = d["result"].get("response")
    js = _extraer_json(texto)
    if not js:
        return None, "Cloudflare sin JSON usable"
    return js, None


def _leer_con_cloudflare(ruta: str, cfg: dict):
    """Las tres capas en orden. -> (dict, None) | (None, motivo)."""
    transcripcion, err = transcribir_imagen(ruta, cfg)
    if err:
        return None, err

    mecanico = codigos_por_regex(transcripcion)
    faltan = [k for k in ("lcl", "df", "pdl") if not mecanico[k]]

    final = dict(mecanico)
    via = {k: ("regex" if mecanico[k] else "") for k in final}
    traza = []

    if faltan:
        interpretado, err2 = codigos_por_modelo(transcripcion, cfg)
        if err2:
            traza.append(f"capa interpretativa no disponible ({err2})")
        else:
            valido = _validar(interpretado)
            for k in ("lcl", "df", "pdl"):
                if mecanico[k]:
                    # La regex ya lo tenia: manda ella. Solo se anota el choque.
                    if valido[k] and valido[k] != mecanico[k]:
                        traza.append(f"{k.upper()}: el modelo propuso {valido[k]}, "
                                     f"se conserva {mecanico[k]} (texto literal)")
                elif valido[k]:
                    final[k] = valido[k]
                    via[k] = "modelo"

    final["_via"] = via
    final["_traza"] = traza
    final["_transcripcion"] = transcripcion
    return final, None


# ==================================================== MINIMAX (legado/respaldo)
_PROMPT_MINIMAX = (
    "Eres un asistente que lee capturas de pantalla de sistemas de gestion de "
    "proyectos electricos en Colombia. En la imagen busca estos identificadores:\n"
    "- LCL: numero de 10 digitos, a veces precedido por 'LCL'\n"
    "- DF: codigo que empieza por 'DF' seguido de 10-13 digitos\n"
    "- PDL: codigo que empieza por 'CDOPP' o etiquetado como PDL/planilla\n"
    "Responde SOLO un JSON: {\"lcl\": \"...\", \"df\": \"...\", \"pdl\": \"...\"} "
    "con cadena vacia en lo que no aparezca. No inventes: si no lo ves claro, vacio."
)


def _leer_con_minimax(ruta: str, cfg: dict):
    if not configurado_minimax(cfg):
        return None, "Sin clave de MiniMax en config_ia.json"
    data_url, err = _leer_imagen_data_url(ruta)
    if err:
        return None, err
    cuerpo = {
        "model": cfg["modelo"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _PROMPT_MINIMAX},
            {"type": "image_url", "image_url": {"url": data_url}}]}],
        "temperature": 0.1,
        "max_tokens": 2000,
    }
    d, err = _post(cfg["endpoint"], cuerpo, cfg["api_key"], float(cfg["timeout_s"]))
    if err:
        return None, f"Error llamando a MiniMax: {err}"
    try:
        contenido = d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        base = (d or {}).get("base_resp") or {}
        return None, f"Respuesta inesperada de MiniMax: {base.get('status_msg') or str(d)[:200]}"
    final = _validar(_extraer_json(contenido))
    final["_via"] = {k: ("minimax" if final[k] else "") for k in ("lcl", "df", "pdl")}
    final["_traza"] = []
    return final, None


# ==================================================================== FACHADA
def leer_codigos_en_imagen(ruta: str):
    """Manda una imagen al proveedor configurado y devuelve
    ({lcl, df, pdl, ...}, None) o (None, motivo). Nunca lanza."""
    cfg = cargar_config()
    prov = (cfg.get("proveedor") or "auto").strip().lower()

    if prov == "minimax":
        orden = ["minimax"]
    elif prov == "cloudflare":
        orden = ["cloudflare"]
    else:                                  # auto: Cloudflare y, si falla, MiniMax
        orden = ["cloudflare", "minimax"]

    motivos = []
    for p in orden:
        try:
            if p == "cloudflare":
                if not configurado_cf(cfg):
                    motivos.append("Cloudflare no configurado")
                    continue
                res, err = _leer_con_cloudflare(ruta, cfg)
            else:
                if not configurado_minimax(cfg):
                    motivos.append("MiniMax no configurado")
                    continue
                res, err = _leer_con_minimax(ruta, cfg)
        except Exception as e:             # noqa: BLE001 — jamas romper el flujo
            motivos.append(f"{p}: {type(e).__name__}: {e}")
            continue
        if err:
            motivos.append(f"{p}: {err}")
            continue
        return res, None

    return None, " | ".join(motivos) or "Sin proveedor de IA configurado"
