#!/usr/bin/env python3
"""
Generación de observaciones RG03 con visión (proveedor de IA por API).

REGLA CLAVE (feedback del usuario): las fotos de un nodo son un FLUJO DE
SUCESOS coherente del mismo trabajo — nunca se analizan aisladas. Todas las
fotos del nodo van en UNA sola llamada multi-imagen, ordenadas, para que el
modelo mantenga coherencia (es la misma estructura en todas: si el poste
existente es de madera en la foto 1, sigue siendo de madera en la foto 2
aunque el ángulo cambie) y ubique cada foto en el antes → durante → después
usando el sello de fecha/hora impreso en la imagen.

La salida por foto sigue el formato exacto de entrenamiento:

    Observador: ... Contexto: ... OUTPUT FINAL: ...

Requiere ANTHROPIC_API_KEY en el entorno del proceso (PM2).
Modelo configurable con RG03_MODEL (default: claude-opus-4-8).
"""
import base64
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_BAREMO_PATH = os.path.join(_HERE, "assets", "elenco_baremo.json")
_FEWSHOT_PATH = os.path.join(_HERE, "assets", "rg03_fewshot.json")

_baremo_cache = None
_fewshot_cache = None
_MAX_ALCANCE = 900  # caracteres de alcance por QP en el prompt


def _baremo():
    global _baremo_cache
    if _baremo_cache is None:
        with open(_BAREMO_PATH, encoding="utf-8") as f:
            _baremo_cache = json.load(f)
    return _baremo_cache


def _fewshot():
    global _fewshot_cache
    if _fewshot_cache is None:
        with open(_FEWSHOT_PATH, encoding="utf-8") as f:
            _fewshot_cache = json.load(f)
    return _fewshot_cache


def hay_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _prompt_sistema(rg02: dict) -> str:
    fs = _fewshot()
    ejemplos = "\n\n".join(
        f"[Ejemplo — foto {e['foto']}, nodo {e['nodo']}]\n{e['observacion']}"
        for e in fs["ejemplos"]
    )
    ctx = fs["proyecto"]
    prests_ej = "\n".join(
        f"  {p['qp']} ×{p['cantidad']} {p['unidad']} — {p['desc']}"
        for p in ctx["prestaciones_nodo"]["4"]
    )
    return f"""Eres un ingeniero de DOMINION que documenta el registro fotográfico RG03 de obras \
eléctricas de distribución (Enel/Codensa, Colombia). Recibirás TODAS las fotos de un nodo en \
orden, junto con el contexto de planeación, y debes redactar la celda OBSERVACIONES de cada \
foto en el formato EXACTO de los ejemplos de entrenamiento.

LAS FOTOS SON UN FLUJO DE SUCESOS — LA REGLA MÁS IMPORTANTE:
Las fotos del nodo documentan UN MISMO trabajo sobre UNA MISMA estructura, en secuencia. \
Antes de escribir nada, reconstruye mentalmente la historia completa: cuál es la estructura \
existente y de qué material es, qué labores se ejecutan y cuál es el estado final. Luego \
redacta cada observación como parte de esa historia:
• La estructura existente es LA MISMA en todas las fotos donde aparezca: si es un poste de \
madera en una foto, ES DE MADERA en todas, aunque el ángulo, la luz o la distancia cambien. \
Nunca contradigas el material o la configuración entre fotos.
• Usa el sello de fecha/hora impreso en cada imagen para ubicarla en el flujo \
(antes → durante → después). Fotos con fechas anteriores pueden ser de visitas previas de la \
misma obra.
• Un elemento nuevo se distingue del existente por la historia: si en fotos posteriores al \
hincado del poste nuevo aparece un templete recién tensado con herrajes galvanizados \
brillantes, es el templete NUEVO instalado; los templetes de las fotos del estado inicial son \
los EXISTENTES a retirar.
• Sé consistente: dos fotos del mismo momento (sellos con segundos de diferencia) describen \
lo mismo desde ángulos distintos.

FORMATO OBLIGATORIO DE CADA OBSERVACIÓN (un solo párrafo, sin markdown ni saltos de línea):
Observador: [lo que se ve en la foto, en términos técnicos de red de distribución]. \
Contexto: En la planeación se evidencia que existe una relacion en la actividad para este nodo, \
lo cual se verifica con las prestaciones a cobrar: [códigos QP del nodo con su justificación \
según el alcance del baremo — qué es la actividad principal, qué está incluido en su alcance y \
qué se cobra aparte y por qué]. OUTPUT FINAL: [descripción corta y concreta de la foto].

REGLAS:
1. Usa ÚNICAMENTE los códigos QP listados para el nodo (vienen de la planilla RG02 real). \
Nunca inventes ni infieras códigos que no estén en la lista.
2. Justifica con el ALCANCE del baremo: si la acción visible está dentro del alcance de la \
actividad principal (ej. el retiro del poste, la re-excavación y la compactación están dentro \
del alcance del cambio de poste), dilo; si se cobra aparte (ej. tirantes, trabajos menores), \
explica por qué.
3. Describe solo lo que la foto muestra. Menciona templetes, acometidas, ramales, PAT o \
marcación solo si se ven o aplican.
4. Si una foto muestra la marcación del punto físico en pintura, verifica que coincida con el \
PF del nodo y menciónalo.
5. Español técnico, tono idéntico a los ejemplos. Sin viñetas ni comillas.

=== EJEMPLOS DE ENTRENAMIENTO (proyecto GAMA, nodo 4) ===
Descripción general del trabajo: {ctx['descripcion_general']}
Observaciones RG02: {ctx['observaciones_rg02']}
Prestaciones del nodo 4:
{prests_ej}

{ejemplos}
=== FIN DE EJEMPLOS ===

=== PROYECTO ACTUAL ===
Descripción general del trabajo (RG02): {rg02.get('descripcion_general') or '(no disponible)'}
Observaciones RG02: {rg02.get('observaciones') or '(ninguna)'}
{('Plan de trabajo (PDT): ' + rg02['pdt_texto']) if rg02.get('pdt_texto') else ''}"""


def _prestaciones_nodo(nodo: str, rg02: dict) -> str:
    prests = rg02.get("nodos", {}).get(str(nodo), [])
    baremo = _baremo()
    lineas = []
    for p in prests:
        info = baremo.get(p["qp"], {})
        alcance = (info.get("alcance") or "").replace("\n", " ")[:_MAX_ALCANCE]
        lineas.append(
            f"- {p['qp']} ×{p['cantidad']} {p['unidad']} — {p['desc']}\n"
            f"  ALCANCE (baremo): {alcance}"
        )
    return "\n".join(lineas) if lineas else (
        "(el nodo no tiene prestaciones en la planilla RG02 — indícalo con prudencia y describe solo lo visible)"
    )


def _bloque_imagen(jpg: bytes) -> dict:
    media_type = "image/png" if jpg[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(jpg).decode("ascii"),
        },
    }


_SCHEMA_SALIDA = {
    "type": "object",
    "properties": {
        "observaciones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "foto": {"type": "integer"},
                    "observacion": {"type": "string"},
                },
                "required": ["foto", "observacion"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["observaciones"],
    "additionalProperties": False,
}


def generar_nodo(bloques_nodo: list, rg02: dict, solo_foto: int = None) -> dict:
    """Genera las observaciones de TODAS las fotos de un nodo en una sola
    llamada multi-imagen (flujo de sucesos coherente).

    `bloques_nodo`: bloques del MISMO nodo, ordenados por nº de foto.
    `solo_foto`: si se indica, el modelo analiza toda la secuencia pero
    devuelve únicamente la observación de esa foto (para regenerar una).

    Retorna {nº de foto: observación}. Lanza RuntimeError con mensaje amigable.
    """
    if not hay_api_key():
        raise RuntimeError("Falta ANTHROPIC_API_KEY en el servidor — configúrala en PM2 y reinicia.")
    if not bloques_nodo:
        raise RuntimeError("No hay fotos para este nodo.")

    import anthropic

    nodo = bloques_nodo[0].get("nodo", "")
    contenido = [{
        "type": "text",
        "text": f"SECUENCIA COMPLETA DE FOTOS DEL NODO {nodo} — "
                f"PF {bloques_nodo[0].get('pf', '')} | CD/DME {bloques_nodo[0].get('cd', '')} | "
                f"Coordenadas {bloques_nodo[0].get('lat', '')}, {bloques_nodo[0].get('lon', '')}.\n"
                f"Cada imagen va precedida de su número de foto. El sello de fecha/hora está "
                f"impreso dentro de cada imagen — úsalo para ordenar el flujo de sucesos.",
    }]
    for b in bloques_nodo:
        if not b.get("jpg"):
            continue
        contenido.append({"type": "text", "text": f"FOTO {b['n']}:"})
        contenido.append(_bloque_imagen(b["jpg"]))

    pedido = (
        f"Devuelve únicamente la observación de la FOTO {solo_foto}, redactada con la "
        f"coherencia de toda la secuencia."
        if solo_foto is not None
        else "Devuelve la observación de CADA foto de la secuencia, todas coherentes entre sí."
    )
    contenido.append({
        "type": "text",
        "text": f"""Prestaciones a cobrar en el nodo {nodo} (planilla RG02):
{_prestaciones_nodo(nodo, rg02)}

Primero reconstruye la historia completa de la secuencia (estructura existente y su material,
labores, estado final). Después redacta. {pedido}""",
    })

    client = anthropic.Anthropic()
    modelo = os.environ.get("RG03_MODEL", "claude-opus-4-8")
    try:
        resp = client.messages.create(
            model=modelo,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA_SALIDA}},
            system=[{
                "type": "text",
                "text": _prompt_sistema(rg02),
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": contenido}],
        )
    except anthropic.AuthenticationError:
        raise RuntimeError("ANTHROPIC_API_KEY inválida o vencida — reemplázala en el servidor.")
    except anthropic.RateLimitError:
        raise RuntimeError("Límite de tasa de la API alcanzado — espera un momento y reintenta.")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Error de la API de IA ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        raise RuntimeError("Sin conexión con la API de IA — revisa la red del servidor.")

    if resp.stop_reason == "refusal":
        raise RuntimeError("La IA declinó procesar estas fotos — reintenta o redacta manualmente.")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Respuesta truncada — reintenta (o regenera el nodo en dos partes).")

    texto = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(texto)
        salida = {int(o["foto"]): " ".join(str(o["observacion"]).split())
                  for o in data.get("observaciones", []) if str(o.get("observacion", "")).strip()}
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("La IA devolvió un formato inesperado — reintenta.")

    if solo_foto is not None:
        if solo_foto not in salida:
            raise RuntimeError(f"La IA no devolvió la observación de la foto {solo_foto} — reintenta.")
        return {solo_foto: salida[solo_foto]}
    if not salida:
        raise RuntimeError("La IA no devolvió observaciones — reintenta.")
    return salida
