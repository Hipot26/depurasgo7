# -*- coding: utf-8 -*-
"""
analizador_correo.py — Análisis completo de un correo "Orden Pendiente".

Dado un correo de Outlook, extrae todo lo que el flujo de balance MO necesita
y arma la lista de faltantes. Orden de fuentes (de más a menos confiable):

  1. Texto (asunto + cuerpo, incluida la cadena de reenvíos)
  2. Excel adjunto (metadatos Ticket/PDL-OdM-LCL, filas QP, columna líder)
  3. Imágenes adjuntas → visión IA (MiniMax) — la LCL muchas veces viene en
     captura de pantalla; el PDL en casos raros

Reglas de negocio (2026-08-11):
  - MI (código DF...): NO lleva PDL. Requiere: código DF + planilla + líder.
  - Programados (código LCL...): requiere LCL + PDL + planilla + líder.
  - Si la planilla no está adjunta pero hay link SharePoint/Drive → anotarlo.
"""
import os
import re
import uuid
import tempfile

from balance_mo_bridge import (RE_DF, RE_LCL, RE_LCL_PELADA, _normalizar_match,
                               extraer_de_excel, validar_codigo_sgs,
                               tipo_planilla_valida, extraer_planilla_nativa,
                               clave_nodo_cmp, nodos_sgs_proyecto)
import ia_vision
from instruccion_mat import instruccion_material   # ¿el correo deja liquidar material?

# Corta el cuerpo en "mensaje reciente" vs "cadena de reenvíos vieja" — la
# trazabilidad de la cadena suele traer códigos de OTROS proyectos.
RE_CORTE_CADENA = re.compile(r"\n\s*(?:De|From|-{3,}\s*Mensaje original)\s*:", re.IGNORECASE)


def _candidatos_codigo(asunto: str, cuerpo: str, excel_metas: list,
                       excel_metas_rg11: list = None) -> list:
    """Todos los DF/LCL con su fuente, en orden de confianza:
    planilla del SISTEMA (ese Excel sí sabe a qué proyecto pertenece) > asunto
    > cuerpo reciente > cadena vieja > encabezado de Excel genérico (RG11/
    PRESUPUESTO — los supervisores NO actualizan esos encabezados, arrastran la
    LCL del PI viejo; regla 2026-08-24). La imagen (IA) se agrega
    aparte si hace falta."""
    m = RE_CORTE_CADENA.search(cuerpo or "")
    arriba = cuerpo[:m.start()] if m else (cuerpo or "")
    cadena = cuerpo[m.start():] if m else ""
    fuentes = [
        ("planilla", " ".join(excel_metas)),
        ("asunto", asunto or ""),
        ("cuerpo", arriba),
        ("cadena", cadena),
        ("excel adjunto", " ".join(excel_metas_rg11 or [])),
    ]
    cands, vistos = [], set()
    for fuente, texto in fuentes:
        for rex, pref in ((RE_DF, "DF"), (RE_LCL, "LCL")):
            for mm in rex.finditer(texto or ""):
                cod = f"{pref}{mm.group(1)}"
                if cod not in vistos:
                    vistos.add(cod)
                    cands.append({"codigo": cod, "fuente": fuente})
        # LCL pelada (número 630xxxxxxx suelto, típico de tablas HTML donde el
        # rótulo quedó en otra fila) — al final de cada fuente: menos confiable,
        # pero el árbitro SGS descarta las que no existan.
        for mm in RE_LCL_PELADA.finditer(texto or ""):
            cod = f"LCL{mm.group(1)}"
            if cod not in vistos:
                vistos.add(cod)
                cands.append({"codigo": cod, "fuente": f"{fuente} (tabla)"})
    return cands

RE_PDL = re.compile(r"\bCDOPP[A-Z0-9]{6,14}\b", re.IGNORECASE)
RE_LINK_EXTERNO = re.compile(r"sharepoint|onedrive|drive\.google|docs\.google", re.IGNORECASE)
RE_FECHA = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})")


_PALABRAS_NO_NOMBRE = {
    "CORDIAL", "SALUDO", "SALUDOS", "GRACIAS", "ATENTO", "ATENTA", "BUEN",
    "BUENA", "BUENOS", "BUENAS", "DIA", "DÍA", "DIAS", "DÍAS", "TARDE",
    "TARDES", "SUPERVISOR", "TECNICO", "TÉCNICO", "INGENIERO", "DOMINION",
    "ADJUNTO", "ANEXO", "EJECUTADO", "EJECUTADA", "LIQUIDAR", "INFORMACION",
    "INFORMACIÓN", "PROYECTO", "SERVICES", "MULTITECHNICAL", "FIELD",
    "FACTURACION", "FACTURACIÓN", "URGENTE", "CIERRE",
}


RE_RANGO_NODOS = re.compile(
    r"(?:del\s+)?nodos?\s+(\d{1,3})\s*(?:al|a|hasta)\s*(?:el\s+)?(?:nodo\s+)?(\d{1,3})\b",
    re.IGNORECASE)
RE_LISTA_NODOS = re.compile(
    r"\bnodos?\s+(\d{1,3}[A-Z]?(?:\s*(?:,|-|y)\s*\d{1,3}[A-Z]?)*)\b", re.IGNORECASE)


def _alcance_declarado(texto: str) -> set:
    """Nodos INDIVIDUALES que el mensaje declara ejecutados: 'del nodo 1 al
    24' → {'1'..'24'} · 'nodos 11-12-13' → {'11','12','13'} · 'Nodo 57' →
    {'57'}. Los tramos ('cable N21-N23', 'el nodo N1-N21') no entran: los
    supervisores los nombran con aproximación y no sirven para comparar."""
    out, t = set(), texto or ""
    for m in RE_RANGO_NODOS.finditer(t):
        a, b = int(m.group(1)), int(m.group(2))
        if a < b <= a + 300:
            out.update(str(i) for i in range(a, b + 1))
    for m in RE_LISTA_NODOS.finditer(RE_RANGO_NODOS.sub(" ", t)):
        out.update(tok.upper() for tok in re.findall(r"\d{1,3}[A-Z]?", m.group(1), re.IGNORECASE))
    return out


def _en_alcance(nodo, alcance: set) -> bool:
    """Nodo individual: está en el alcance. Tramo (N1-N21): sus DOS extremos
    están en el alcance."""
    k = clave_nodo_cmp(nodo)
    if "_" in k:
        partes = [p for p in k.split("_") if p]
        return bool(partes) and all(p in alcance for p in partes)
    return k in alcance


_PAL_GRUPO = re.compile(
    r"cuadrillas?\s+de(?:\s+los)?(?:\s+l[ií]deres)?|l[ií]der(?:es)?(?:\s+responsables?)?",
    re.IGNORECASE)
_STOP_NOMBRE = re.compile(
    r"\s+(?:del?|los|las|el|la|en|para|con|que|quien(?:es)?|nodos?|cables?|tramos?|"
    r"d[ií]as?|ejecut\w*|realiz\w*|trabaj\w*|se|por|hasta|desde|al|fecha|lcl|pdl|cc)\b"
    r"|[.;:\n\r(\d]", re.IGNORECASE)
_SEP_NOMBRES_TXT = re.compile(r"\s*[-–/+,]\s*|\s+[yYeE]\s+")
_PREFIJOS_NOMBRE = {"MOE", "MO", "EL", "LA", "SR", "SEÑOR", "SENOR", "DON", "ING", "INGENIERO"}
_SUFIJOS_NOMBRE = {"EL", "LA", "DE", "DEL", "DIA", "DÍA", "AYER", "HOY", "FECHA", "LCL", "PDL", "CC"}


def _nombres_de(segmento: str) -> list:
    """'Nombre Uno-Nombre Dos' → ['NOMBRE UNO', 'NOMBRE
    DOS']. Solo palabras con mayúscula inicial (los nombres propios);
    'estos trabajos' y demás relleno quedan fuera."""
    out = []
    for parte in _SEP_NOMBRES_TXT.split(segmento or ""):
        pal = [p for p in parte.strip().split() if p]
        while pal and pal[0].upper().strip(".") in _PREFIJOS_NOMBRE:
            pal.pop(0)
        while pal and pal[-1].upper() in _SUFIJOS_NOMBRE:
            pal.pop()
        if not 2 <= len(pal) <= 5:
            continue
        if not all(re.fullmatch(r"[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ]+", p) for p in pal):
            continue
        if any(p.upper() in _PALABRAS_NO_NOMBRE for p in pal):
            continue
        nombre = " ".join(p.upper() for p in pal)
        if nombre not in out:
            out.append(nombre)
    return out


def _grupos_lideres_texto(cuerpo: str):
    """Grupos de líderes del MENSAJE RECIENTE. Cada 'cuadrilla(s) de …' o
    'líder(es): …' es un grupo (ejemplo real, 2026-09-29: 'cuadrillas de
    Líder Uno-Líder Dos del nodo 1 al 24 … y el nodo N1-N21 cuadrillas de
    Líder Uno-Líder Dos-Líder Tres-Líder Cuatro'). También 'LIDERES' seguido
    de un nombre por renglón.
    Devuelve (grupos [[nombres]], frase literal para liquidador2)."""
    m = RE_CORTE_CADENA.search(cuerpo or "")
    arriba = cuerpo[:m.start()] if m else (cuerpo or "")
    grupos, lineas_fuente = [], []
    for mk in _PAL_GRUPO.finditer(arriba):
        resto = arriba[mk.end():]
        resto_linea = re.split(r"[\r\n]", resto, 1)[0]
        seg = re.sub(r"^\s*[:;]?\s*", "", resto_linea)
        if seg.strip():
            corte = _STOP_NOMBRE.search(seg)
            nombres = _nombres_de(seg[:corte.start()] if corte else seg)
        else:
            # "PARA LOS LIDERES" + un nombre por renglón
            nombres = []
            for ln in [l.strip() for l in resto.splitlines()[1:9]]:
                if not ln:
                    if nombres:
                        break
                    continue
                n = _nombres_de(ln)
                if len(n) != 1 or n[0] != ln.strip().upper():
                    break
                nombres.extend(x for x in n if x not in nombres)
        if nombres and (not grupos or grupos[-1] != nombres):
            grupos.append(nombres)
            ini = arriba.rfind("\n", 0, mk.start()) + 1
            fin = arriba.find("\n", mk.end())
            linea = arriba[ini:fin if fin != -1 else len(arriba)].strip()
            if linea and linea not in lineas_fuente:
                lineas_fuente.append(linea)
    return grupos, "\n".join(lineas_fuente)[:700]


def _lider_en_texto(cuerpo: str):
    """Líder desde el texto del mensaje reciente (típico en respuestas del
    supervisor a la consulta). Dos patrones reales:
      A) "cuadrilla de Nombre Apellido" / "líder: Nombre Apellido"
      B) Línea en MAYÚSCULAS con pinta de nombre y cédula debajo:
           NOMBRE1 NOMBRE2 APELLIDO1 APELLIDO2
           1000000000
    Devuelve (nombre, cedula) o ("", "")."""
    m = RE_CORTE_CADENA.search(cuerpo or "")
    arriba = cuerpo[:m.start()] if m else (cuerpo or "")

    # Las palabras del nombre van separadas por espacios de LA MISMA línea —
    # con \s+ el nombre se tragaba el rótulo del renglón siguiente (ejemplo:
    # "Líder: Nombre Apellido\nFecha:25/08/2026" → "NOMBRE APELLIDO FECHA").
    ma = re.search(r"(?:cuadrilla\s+de|l[ií]der(?:\s+responsable)?\s*[:;]?)\s+"
                   r"([A-ZÁÉÍÓÚÑa-záéíóúñ]{2,}(?:[ \t]+[A-ZÁÉÍÓÚÑa-záéíóúñ]{2,}){1,3})",
                   arriba, re.IGNORECASE)
    if ma:
        palabras = ma.group(1).strip().upper().split()
        # Quitar prefijos de rol pegados al nombre ("líder MOE Nombre Apellido")
        while palabras and palabras[0] in {"MOE", "MO", "EL", "LA", "SR", "SEÑOR", "DON"}:
            palabras.pop(0)
        # ...y relleno pegado al final ("Nombre Apellido el día de ayer lunes...")
        _corte = {"EL", "LA", "DE", "DEL", "DIA", "DÍA", "AYER", "HOY", "LUNES",
                  "MARTES", "MIERCOLES", "MIÉRCOLES", "JUEVES", "VIERNES",
                  "SABADO", "SÁBADO", "DOMINGO", "FECHA", "LCL", "PDL", "CC"}
        while palabras and palabras[-1] in _corte:
            palabras.pop()
        candidato = " ".join(palabras)
        if candidato and not any(p in _PALABRAS_NO_NOMBRE for p in palabras):
            return candidato, ""

    lineas = [l.strip() for l in arriba.splitlines() if l.strip()][:12]
    for i, linea in enumerate(lineas):
        palabras = [p.strip(".,;:") for p in linea.split() if p.strip(".,;:")]
        if not (2 <= len(palabras) <= 6):
            continue
        if linea != linea.upper():
            continue
        if not all(re.fullmatch(r"[A-ZÁÉÍÓÚÑ]{2,}", p) for p in palabras):
            continue
        if any(p in _PALABRAS_NO_NOMBRE for p in palabras):
            continue
        cedula = ""
        if i + 1 < len(lineas):
            mc = re.fullmatch(r"\d{7,11}", lineas[i + 1].strip())
            if mc:
                cedula = mc.group(0)
        return " ".join(palabras), cedula
    return "", ""


_MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
          "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10,
          "noviembre": 11, "diciembre": 12}
RE_FECHA_PALABRAS = re.compile(
    r"(\d{1,2})\s+de\s+(" + "|".join(_MESES) + r")(?:\s+de[l]?\s+(\d{4}))?",
    re.IGNORECASE)


RE_DIAS_LISTA = re.compile(
    r"d[ií]as?\s+(\d{1,2}(?:\s*(?:-|,|y)\s*\d{1,2})+)(?!\d)(?!\s*[/-]\s*\d{2,4})", re.IGNORECASE)

RE_RANGO_FECHAS = re.compile(
    r"(?:del|desde(?:\s+el)?)\s+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s+(?:al|hasta(?:\s+el)?|y(?:\s+el)?)\s+"
    r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", re.IGNORECASE)

_COLORES_BASE = ("verde", "amarill", "roj", "azul", "naranja", "gris", "morad", "rosad")
RE_COLOR_INSTR = re.compile(
    r"(?:color(?:es)?|resaltad\w*|marcad\w*|sombread\w*|pintad\w*|rellen\w*)\W{1,25}?"
    r"(verde\w*|amarill\w*|roj[oa]s?|azul\w*|naranja\w*|gris\w*|morad\w*|rosad\w*)\b",
    re.IGNORECASE)


def _color_instruccion(texto: str) -> str:
    """Color que el supervisor pide en el mensaje ('los nodos que están en
    color verde') → 'verde' | 'amarillo' | ... | ''."""
    m = RE_COLOR_INSTR.search(texto or "")
    if not m:
        return ""
    pal = m.group(1).lower()
    for base in _COLORES_BASE:
        if pal.startswith(base):
            return {"amarill": "amarillo", "roj": "rojo", "morad": "morado",
                    "rosad": "rosado"}.get(base, base)
    return ""


def _nombre_color(rgb: str) -> str:
    """'00B050' → 'verde', 'FFFF00' → 'amarillo'… ('' sin relleno)."""
    if not rgb or len(rgb) < 6 or rgb == "theme":
        return "theme" if rgb == "theme" else ""
    try:
        r, g, b = int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)
    except ValueError:
        return ""
    if max(r, g, b) - min(r, g, b) < 25:
        return "" if max(r, g, b) >= 240 else "gris"
    if g >= 140 and g > r * 1.15 and g > b * 1.15:
        return "verde"
    if r >= 200 and g >= 180 and b < 140:
        return "amarillo"
    if r >= 200 and 90 <= g < 180 and b < 110:
        return "naranja"
    if r >= 170 and g < 120 and b < 120:
        return "rojo"
    if r >= 190 and b >= 150 and g < 170:
        return "rosado"
    if b >= 140 and b > r * 1.15 and b > g * 1.15:
        return "azul"
    if r >= 100 and b >= 140 and g < 120:
        return "morado"
    return "otro"


def _fecha_ejecucion_texto(texto: str, anio_defecto: int = 0) -> str:
    """Fecha real de ejecución del cuerpo: una fecha cerca de palabras de
    ejecución. Acepta numéricas ('29/07/2026') y en palabras ('10 de agosto
    [de 2026]' — caso real: 'ejecutado ... el día de ayer lunes 10 de
    agosto'). Fechas sueltas (plazos, vencimientos) se ignoran.
    Rango 'del 05/08/2026 al 26/08/2026' (mensaje reciente) → fecha FINAL."""
    if not anio_defecto:
        anio_defecto = __import__("datetime").datetime.now().year
    _mc = RE_CORTE_CADENA.search(texto or "")
    _reciente = texto[:_mc.start()] if _mc else (texto or "")
    mr = RE_RANGO_FECHAS.search(_reciente)
    if mr:
        fm = RE_FECHA.search(mr.group(2))
        if fm:
            d, mes, anio = int(fm.group(1)), int(fm.group(2)), int(fm.group(3))
            if anio < 100:
                anio += 2000
            if 1 <= d <= 31 and 1 <= mes <= 12 and 2020 <= anio <= 2100:
                return f"{anio:04d}-{mes:02d}-{d:02d}"
    # Días sueltos "los dias 18-19-20-22" + una fecha completa en el mismo
    # mensaje que da mes y año ("el día 21/09/2026") → la ÚLTIMA fecha de
    # ejecución (caso real tutela Susa 29-sep). Mismo criterio que el rango.
    from datetime import date as _date
    md = RE_DIAS_LISTA.search(_reciente)
    if md:
        completas = []
        for fm in RE_FECHA.finditer(_reciente):
            d, mes, anio = int(fm.group(1)), int(fm.group(2)), int(fm.group(3))
            anio += 2000 if anio < 100 else 0
            try:
                if 2020 <= anio <= 2100:
                    completas.append(_date(anio, mes, d))
            except ValueError:
                continue
        if completas:
            ref = max(completas)
            candidatas = list(completas)
            for dd in re.findall(r"\d{1,2}", md.group(1)):
                try:
                    candidatas.append(_date(ref.year, ref.month, int(dd)))
                except ValueError:
                    continue
            return max(candidatas).isoformat()
    # Solo el MENSAJE RECIENTE: la cadena trae fechas de OTRAS liquidaciones
    # del mismo hilo (caso real 2026-09-02: "Fecha;2/09/2026" de un correo
    # viejo se coló como fecha de una planilla que no traía fecha).
    texto = _reciente
    for m in re.finditer(r"ejecut\w+|realizad\w+|se trabaj\w+", texto or "", re.IGNORECASE):
        ini = max(0, m.start() - 60)
        fin = min(len(texto), m.end() + 110)
        ventana = texto[ini:fin]
        # Todas las coincidencias, no solo la primera: "18-19-20" parece fecha
        # (mes 19, inválida) y tapaba la fecha real que venía detrás.
        for fm in RE_FECHA.finditer(ventana):
            d, mes, anio = int(fm.group(1)), int(fm.group(2)), int(fm.group(3))
            if anio < 100:
                anio += 2000
            if 1 <= d <= 31 and 1 <= mes <= 12 and 2020 <= anio <= 2100:
                return f"{anio:04d}-{mes:02d}-{d:02d}"
        fp = RE_FECHA_PALABRAS.search(ventana)
        if fp:
            d = int(fp.group(1))
            mes = _MESES[fp.group(2).lower()]
            anio = int(fp.group(3)) if fp.group(3) else anio_defecto
            if 1 <= d <= 31 and 2020 <= anio <= 2100:
                return f"{anio:04d}-{mes:02d}-{d:02d}"

    # Respaldo: rótulo "Fecha:" del supervisor en el MENSAJE RECIENTE (p. ej.
    # "Líder: Nombre Apellido / Fecha:25/08/2026"). La cadena vieja se
    # descarta para no pescar fechas de correos anteriores del hilo.
    mc = RE_CORTE_CADENA.search(texto or "")
    reciente = texto[:mc.start()] if mc else (texto or "")
    for fm in re.finditer(
            r"(?im)^[ \t]*fecha(?:[ \t]+(?:de[ \t]+)?(?:ejecuci[oó]n|real)\w*)?"
            r"[ \t]*[:;]?[ \t]*(\S.{0,34})", reciente):
        resto = fm.group(1)
        fn = RE_FECHA.search(resto)
        if fn:
            d, mes, anio = int(fn.group(1)), int(fn.group(2)), int(fn.group(3))
            if anio < 100:
                anio += 2000
            if 1 <= d <= 31 and 1 <= mes <= 12 and 2020 <= anio <= 2100:
                return f"{anio:04d}-{mes:02d}-{d:02d}"
        fp = RE_FECHA_PALABRAS.search(resto)
        if fp:
            d = int(fp.group(1))
            mes = _MESES[fp.group(2).lower()]
            anio = int(fp.group(3)) if fp.group(3) else anio_defecto
            if 1 <= d <= 31 and 2020 <= anio <= 2100:
                return f"{anio:04d}-{mes:02d}-{d:02d}"
    return ""

EXT_IMAGEN = (".png", ".jpg", ".jpeg")
MIN_BYTES_IMAGEN = 15_000   # firmas y logos suelen pesar menos
MAX_IMAGENES_IA = 4


def _smtp_remitente(item) -> str:
    try:
        dir_ = item.SenderEmailAddress or ""
        if dir_.startswith("/"):   # dirección Exchange interna → resolver SMTP
            return item.Sender.GetExchangeUser().PrimarySmtpAddress or dir_
        return dir_
    except Exception:
        return ""


def _detectar_texto(texto: str) -> dict:
    """DF / LCL / PDL en texto plano."""
    out = {"df": "", "lcl": "", "pdl": ""}
    m = RE_DF.search(texto or "")
    if m:
        out["df"] = _normalizar_match(m, "DF")
    m = RE_LCL.search(texto or "")
    if m:
        out["lcl"] = _normalizar_match(m, "LCL")
    m = RE_PDL.search(texto or "")
    if m:
        out["pdl"] = m.group(0).upper()
    return out


def _analizar_imagenes(item, ya: dict, notas: list) -> dict:
    """Pasa las imágenes adjuntas por MiniMax solo si aún falta algo que la
    visión pueda aportar. Nunca lanza — anota errores y sigue."""
    hallado = {"lcl": "", "df": "", "pdl": ""}
    usadas = 0
    try:
        adjuntos = list(item.Attachments)
    except Exception:
        return hallado
    for adj in adjuntos:
        if usadas >= MAX_IMAGENES_IA:
            break
        fn = (adj.FileName or "").lower()
        if not fn.endswith(EXT_IMAGEN):
            continue
        try:
            if adj.Size < MIN_BYTES_IMAGEN:
                continue
        except Exception:
            pass
        ruta = os.path.join(tempfile.gettempdir(), f"centro_mo_{uuid.uuid4().hex}{os.path.splitext(fn)[1]}")
        try:
            adj.SaveAsFile(ruta)
            usadas += 1
            res, err = ia_vision.leer_codigos_en_imagen(ruta)
            if err:
                notas.append(f"IA ({adj.FileName}): {err}")
                continue
            for k in ("lcl", "df", "pdl"):
                if res.get(k) and not hallado[k] and not ya.get(k):
                    hallado[k] = res[k]
                    notas.append(f"IA leyó {k.upper()}={res[k]} en imagen {adj.FileName}")
        except Exception as e:
            notas.append(f"IA ({fn}): {e}")
        finally:
            try:
                os.remove(ruta)
            except OSError:
                pass
    return hallado


def analizar_item(item, carpeta_ruta: str = "") -> dict:
    """Analiza un MailItem de Outlook y devuelve el registro para el Centro MO."""
    asunto = item.Subject or ""
    try:
        cuerpo = item.Body or ""
    except Exception:
        cuerpo = ""
    try:
        fecha = item.ReceivedTime.strftime("%Y-%m-%d %H:%M")
    except Exception:
        fecha = ""
    try:
        conversacion = item.ConversationTopic or asunto
    except Exception:
        conversacion = asunto

    notas = []
    texto = asunto + "\n" + cuerpo
    d = _detectar_texto(texto)

    # ── Excel adjuntos ────────────────────────────────────────────────────────
    filas_mo, lideres, adjunto_usado = [], [], ""
    filas_mo_todas = []      # del Excel elegido, ignorando filtros (orden de Excel)
    excels_con_mo = []       # [(nombre, filas completas)] para comparar entre adjuntos
    instruccion_lideres = ""
    filas_mat, _mat_vistos = [], set()
    colores_mo, colores_mat = [], []   # relleno por fila, alineado (instrucción "en verde")
    lideres_det = []
    excel_metas = []
    excel_metas_rg11 = []   # códigos de encabezados de Excel genérico (poco confiables)
    fecha_ejecucion = ""
    hay_excel_adjunto = False
    try:
        adjuntos = list(item.Attachments)
    except Exception:
        adjuntos = []
    # Los adjuntos pueden venir directos (.xlsx/.xlsm) o dentro de un ZIP
    # (caso real: "FACTURACION WR-24532 CDOPP....zip" con el PRESUPUESTO
    # adentro). Del ZIP se extraen los Excel — PRESUPUESTO primero — y se
    # procesan igual. Solo lectura con openpyxl: las macros nunca se ejecutan.
    import zipfile
    pendientes_excel = []      # (nombre_para_notas, ruta_temporal)
    temporales = []
    for adj in adjuntos:
        fn = adj.FileName or ""
        low = fn.lower()
        if low.endswith((".xlsx", ".xlsm")):
            ruta = os.path.join(tempfile.gettempdir(), f"centro_mo_{uuid.uuid4().hex}.xlsx")
            try:
                adj.SaveAsFile(ruta)
                temporales.append(ruta)
                pendientes_excel.append((fn, ruta))
            except Exception as e:
                notas.append(f"Excel {fn}: {e}")
        elif low.endswith(".zip"):
            ruta_zip = os.path.join(tempfile.gettempdir(), f"centro_mo_{uuid.uuid4().hex}.zip")
            try:
                adj.SaveAsFile(ruta_zip)
                temporales.append(ruta_zip)
                with zipfile.ZipFile(ruta_zip) as zf:
                    miembros = [m for m in zf.namelist()
                                if m.lower().endswith((".xlsx", ".xlsm")) and not m.endswith("/")]
                    miembros.sort(key=lambda m: (0 if "PRESUPUESTO" in m.upper() else 1, m))
                    if not miembros:
                        notas.append(f"ZIP {fn}: no trae ningún Excel adentro")
                    for m in miembros[:10]:
                        if zf.getinfo(m).file_size > 80_000_000:
                            notas.append(f"ZIP {fn}: {os.path.basename(m)} demasiado grande — omitido")
                            continue
                        rm = os.path.join(tempfile.gettempdir(), f"centro_mo_{uuid.uuid4().hex}.xlsx")
                        with open(rm, "wb") as fh:
                            fh.write(zf.read(m))
                        temporales.append(rm)
                        pendientes_excel.append((f"{fn} → {os.path.basename(m)}", rm))
            except Exception as e:
                notas.append(f"ZIP {fn}: {e}")

    hay_excel_adjunto = bool(pendientes_excel)
    planilla_nativa = False
    excel_nativa_ruta = ""
    for fn, ruta in pendientes_excel:
        # Planilla del SISTEMA (nacida en el módulo Balance):
        # balance_sgs_manual (MO_MANUAL) o balance_integrado
        # (LIQUIDACION+SIP_MANO_OBRA). Se usa TAL CUAL como planilla final —
        # el archivo original se conserva para poder comparar.
        tipo_val = tipo_planilla_valida(ruta)
        if tipo_val:
            try:
                datos = (extraer_planilla_nativa(ruta) if tipo_val == "MO_MANUAL"
                         else extraer_de_excel(ruta))
            except Exception as e:
                notas.append(f"Planilla del sistema {fn}: {e}")
                continue
            planilla_nativa = True
            import shutil
            base = os.path.basename(fn.split("→")[-1].strip()) or "balance_sgs_manual.xlsx"
            destino_dir = os.path.join(os.path.expanduser("~"), "Downloads")
            destino = os.path.join(destino_dir, base)
            n = 2
            while os.path.exists(destino):
                raiz, ext = os.path.splitext(base)
                destino = os.path.join(destino_dir, f"{raiz} ({n}){ext}")
                n += 1
            try:
                shutil.copy2(ruta, destino)
                excel_nativa_ruta = destino
            except OSError as e:
                notas.append(f"Planilla nativa {fn}: no se pudo copiar ({e})")
            notas.append(f"Planilla del sistema detectada ({tipo_val}: {base}) — "
                         "se usa tal cual; liquidador2 decide sobre parciales/adiciones")
        else:
            try:
                datos = extraer_de_excel(ruta)
            except Exception as e:
                notas.append(f"Excel {fn}: {e}")
                continue
        for e in datos.get("errores", []):
            notas.append(f"Excel {fn}: {e}")
        for a in datos.get("avisos", []):
            notas.append(f"Excel {fn}: {a}")
        if datos.get("pdl") and not d["pdl"]:
            d["pdl"] = datos["pdl"]
        meta = (datos.get("codigo") or "").upper()
        if meta:
            if meta.startswith("CDOPP") and not d["pdl"]:
                d["pdl"] = meta          # puede ser lista "CDOPP...;CDOPP..."
            elif tipo_val:
                excel_metas.append(meta)        # planilla del sistema: confiable
            else:
                excel_metas_rg11.append(meta)   # encabezado RG11/PRESUPUESTO: viejo
        if datos.get("filas") and not filas_mo:
            filas_mo = [{"nodo": n, "codigo": c, "cantidad": q} for n, c, q in datos["filas"]]
            colores_mo = list(datos.get("colores") or [])
            filas_mo_todas = list(datos.get("filas_todas") or datos["filas"])
            adjunto_usado = fn
        if datos.get("filas") or datos.get("filas_todas"):
            excels_con_mo.append((fn, list(datos.get("filas_todas") or datos["filas"])))
        # Materiales FÍSICOS del supervisor — solo para el pre-análisis MAT
        from itertools import zip_longest as _zl
        for (n, c, q), colr in _zl(datos.get("filas_mat") or [], datos.get("colores_mat") or [], fillvalue=""):
            k = (n, c, q)
            if k not in _mat_vistos:
                _mat_vistos.add(k)
                filas_mat.append({"nodo": n, "codigo": c, "cantidad": q})
                colores_mat.append(colr or "")
        for lid in datos.get("lideres", []):
            if lid.upper() not in {x.upper() for x in lideres}:
                lideres.append(lid)
        for ld in datos.get("lideres_det", []):
            if ld["nombre"].upper() not in {x["nombre"].upper() for x in lideres_det}:
                lideres_det.append(ld)
        if datos.get("fecha_ejecucion") and not fecha_ejecucion:
            fecha_ejecucion = datos["fecha_ejecucion"]
    for _t in temporales:
        try:
            os.remove(_t)
        except OSError:
            pass

    _mc = RE_CORTE_CADENA.search(cuerpo or "")
    _arriba = cuerpo[:_mc.start()] if _mc else (cuerpo or "")

    # ── Filtro de Excel OLVIDADO (caso real tutela Susa 29-sep) ──────────────
    # "Lo visible es lo que se cobra" vale cuando el filtro ES la instrucción.
    # Si el correo declara un alcance ("del nodo 1 al 24") y el filtro deja
    # ver menos de la mitad de esos nodos, el filtro es un descuido del
    # supervisor (se quedó mirando dos nodos al guardar): se liquida el alcance
    # declarado. Tramos: entran si sus dos extremos están en el alcance.
    alcance = _alcance_declarado(_arriba)
    if alcance and filas_mo and len(filas_mo_todas) > len(filas_mo):
        indiv_todas = {clave_nodo_cmp(f[0]) for f in filas_mo_todas
                       if "_" not in clave_nodo_cmp(f[0])}
        declarados = alcance & indiv_todas
        visibles = {clave_nodo_cmp(f["nodo"]) for f in filas_mo} & declarados
        if declarados and len(visibles) < 0.5 * len(declarados):
            nuevas = [f for f in filas_mo_todas if _en_alcance(f[0], alcance)]
            if len(nuevas) > len(filas_mo):
                # la nota del bridge ("se liquidan solo las N visibles") ya no aplica
                notas[:] = [n for n in notas
                            if not (n.startswith(f"Excel {adjunto_usado}:") and "Filtro de Excel" in n)]
                fuera = sorted({str(f[0]) for f in filas_mo_todas if not _en_alcance(f[0], alcance)})
                notas.append(
                    f"FILTRO OLVIDADO: el Excel solo mostraba {len(filas_mo)} prestaciones "
                    f"(nodos {', '.join(sorted({str(f['nodo']) for f in filas_mo}))}) pero el correo "
                    f"declara {len(declarados)} nodos — se liquida el alcance del correo: "
                    f"{len(nuevas)} prestaciones"
                    + (f" (fuera del alcance, excluidos: {', '.join(fuera[:8])})" if fuera else ""))
                filas_mo = [{"nodo": n, "codigo": c, "cantidad": q} for n, c, q in nuevas]
                colores_mo = []

    # ── Dos Excel con el MISMO trabajo que no cuadran (RG11 vs PRESUPUESTO) ──
    if len(excels_con_mo) > 1 and adjunto_usado:
        def _mapa(filas):
            m = {}
            for n, c, q in filas:
                k = (clave_nodo_cmp(n), str(c).upper())
                m[k] = m.get(k, 0.0) + float(q or 0)
            return m
        base = _mapa(filas_mo_todas)
        etiqueta = {}
        for n, c, q in filas_mo_todas:
            etiqueta.setdefault((clave_nodo_cmp(n), str(c).upper()), str(n))
        for fn2, filas2 in excels_con_mo:
            if fn2 == adjunto_usado:
                continue
            otro = _mapa(filas2)
            comunes = set(base) & set(otro)
            if not base or len(comunes) < 0.8 * max(len(base), len(otro)):
                continue   # es otro trabajo, no una versión del mismo
            difs = [f"nodo {etiqueta.get(k, k[0])} {k[1]} {base[k]:g} vs {otro[k]:g}"
                    for k in comunes if abs(base[k] - otro[k]) > 0.001]
            solo_otro = len(set(otro) - set(base))
            solo_base = len(set(base) - set(otro))
            if difs or solo_otro or solo_base:
                extra = []
                if solo_base:
                    extra.append(f"{solo_base} solo en el usado")
                if solo_otro:
                    extra.append(f"{solo_otro} solo en el otro")
                notas.append(
                    f"⚠ {os.path.basename(adjunto_usado)} y {os.path.basename(fn2)} traen el mismo "
                    f"trabajo pero NO cuadran: {' · '.join(difs[:6])}"
                    + ("…" if len(difs) > 6 else "")
                    + (f" ({', '.join(extra)})" if extra else "")
                    + f" — se usó {os.path.basename(adjunto_usado)}; si vale el otro, súbelo con ")

    # ── Instrucción por COLOR (2026-09-02: "LOS nodos que están en color
    # verde"): el supervisor pinta en el Excel las filas a liquidar.
    # Si el mensaje reciente pide un color y la planilla trae filas de ese
    # color, se liquidan SOLO esas; si pide un color que no existe, se avisa.
    color_pedido = _color_instruccion(_arriba)
    if color_pedido and filas_mo and colores_mo:
        nombres = [_nombre_color(c) for c in colores_mo]
        sel = [i for i, nc in enumerate(nombres) if nc == color_pedido]
        conteo = {}
        for nc in nombres:
            conteo[nc or "sin color"] = conteo.get(nc or "sin color", 0) + 1
        desglose = ", ".join(f"{k}: {v}" for k, v in conteo.items())
        if sel:
            total = len(filas_mo)
            filas_mo = [filas_mo[i] for i in sel]
            colores_mo = [colores_mo[i] for i in sel]
            notas.append(f"Instrucción del correo: solo filas en color {color_pedido.upper()} "
                         f"→ {len(sel)} de {total} prestaciones ({desglose})")
            if filas_mat and colores_mat:
                nm = [_nombre_color(c) for c in colores_mat]
                selm = [i for i, nc in enumerate(nm) if nc == color_pedido]
                filas_mat = [filas_mat[i] for i in selm]
                colores_mat = [colores_mat[i] for i in selm]
        else:
            notas.append(f"⚠ El correo pide las filas en color {color_pedido.upper()} pero la "
                         f"planilla no tiene filas de ese color ({desglose}) — se dejan todas; "
                         "revisar a mano")
    elif color_pedido and filas_mo:
        notas.append(f"⚠ El correo pide filas en color {color_pedido.upper()} pero no pude "
                     "leer colores de la planilla — revisar a mano")

    # ── Varios PDL en el mensaje (p. ej. "PI CDOPP...- CDOPP...- CDOPP...") ──
    _pdls = list(dict.fromkeys(m.group(0).upper() for m in RE_PDL.finditer(_arriba)))
    if len(_pdls) > 1:
        notas.append(f"⚠ El correo menciona {len(_pdls)} PDL: {', '.join(_pdls)} — la tarjeta "
                     f"usa {d['pdl'] or _pdls[0]}; si son liquidaciones separadas, divide "
                     "con ✎ (solo nodos + PDL)")
    _mr = RE_RANGO_FECHAS.search(_arriba)
    if _mr:
        notas.append(f"El correo da un RANGO de ejecución {_mr.group(1)} → {_mr.group(2)}: "
                     "se usa la fecha final (ajústala en ✎ si prefieres otra)")

    # ── Resolución del código: SGS es el árbitro ─────────────────────────────
    # La cadena puede traer varios DF/LCL (trazabilidad sucia). Se juntan TODOS
    # los candidatos con su fuente y gana el primero que exista en SGS.
    candidatos = _candidatos_codigo(asunto, cuerpo, excel_metas, excel_metas_rg11)
    nodos_planilla = [f["nodo"] for f in filas_mo if f.get("nodo")]
    remitente_nombre = ""
    try:
        remitente_nombre = str(item.SenderName or "")
    except Exception:
        pass
    ganador, log_val, sgs_ok = (None, [], True)
    if candidatos:
        ganador, log_val, sgs_ok = validar_codigo_sgs(candidatos, nodos_planilla,
                                                      remitente_nombre)
        notas.extend(log_val)

    vision_hecha = False
    if not ganador:
        # Sin código confirmado → probar las imágenes (la LCL suele venir en captura)
        vis = _analizar_imagenes(item, d, notas)
        vision_hecha = True
        cands_img = []
        if vis.get("df"):
            cands_img.append({"codigo": vis["df"], "fuente": "imagen"})
        if vis.get("lcl"):
            lcl_img = vis["lcl"]
            if lcl_img.isdigit():
                lcl_img = "LCL" + lcl_img
            cands_img.append({"codigo": lcl_img, "fuente": "imagen"})
        if vis.get("pdl") and not d["pdl"]:
            d["pdl"] = vis["pdl"]
        if cands_img and sgs_ok:
            ganador, log2, sgs_ok = validar_codigo_sgs(cands_img, nodos_planilla,
                                                       remitente_nombre)
            notas.extend(log2)
        elif cands_img:
            candidatos = cands_img + candidatos

    codigo_validado = False
    if ganador:
        codigo = ganador["codigo"]
        codigo_validado = True
        # Trazabilidad sucia: si la planilla adjunta referencia un proyecto y el
        # asunto otro DISTINTO, alguien adjuntó la planilla equivocada (o
        # reutilizó el archivo de otro proyecto) — avisar siempre.
        plan = {c["codigo"] for c in candidatos if c["fuente"] == "planilla"}
        asun = {c["codigo"] for c in candidatos if c["fuente"] == "asunto"}
        if plan and asun and plan.isdisjoint(asun):
            notas.append(
                f"⚠ DISCREPANCIA: la planilla adjunta referencia {'/'.join(sorted(plan))} "
                f"pero el asunto habla de {'/'.join(sorted(asun))} — confirmar a qué proyecto "
                f"pertenece antes de liquidar")
    elif not sgs_ok and candidatos:
        # SGS caído: heurística vieja (mejor candidato disponible), sin validar
        codigo = candidatos[0]["codigo"]
        notas.append(f"Código {codigo} sin validar (SGS no disponible)")
    else:
        codigo = ""
        if candidatos:
            notas.append("Ningún código de la cadena existe en SGS — revisar a mano")
    tipo = "MI" if codigo.startswith("DF") else ("Programados" if codigo.startswith("LCL") else "")

    # ── Nodos de la planilla contra los nodos REALES del proyecto en SGS ────
    # (solo lectura). Avisa nodos que no existen (liquidador2 no los podrá
    # liquidar) y confirma el PDL real cuando el correo menciona varios.
    if codigo and codigo_validado and filas_mo:
        try:
            info_n, _err_n = nodos_sgs_proyecto(codigo)
        except Exception:
            info_n = None
        if info_n:
            etiquetas = {}
            for f in filas_mo:
                etiquetas.setdefault(clave_nodo_cmp(f["nodo"]), str(f["nodo"]))
            faltan = [etiquetas[k] for k in etiquetas if k and k not in info_n]
            if faltan and len(faltan) < len(etiquetas):
                notas.append(
                    f"⚠ Nodos de la planilla que NO existen en SGS: {', '.join(faltan[:10])} — "
                    "liquidador2 no los podrá liquidar (¿nodo sin crear o nombre distinto en SGS?)")
            pdls = {v["pdl"] for k, v in info_n.items() if k in etiquetas and v.get("pdl")}
            if len(pdls) == 1:
                pdl_sgs = next(iter(pdls))
                if tipo == "Programados" and not d["pdl"]:
                    d["pdl"] = pdl_sgs
                    notas.append(f"PDL tomado de SGS: {pdl_sgs} (todos los nodos de la planilla)")
                elif len(set(RE_PDL.findall(_arriba.upper()))) > 1:
                    notas.append(f"✓ SGS confirma: todos los nodos de la planilla están en {pdl_sgs}")
            elif len(pdls) > 1:
                por_pdl = {}
                for k, v in info_n.items():
                    if k in etiquetas and v.get("pdl"):
                        por_pdl.setdefault(v["pdl"], []).append(etiquetas[k])
                notas.append("⚠ En SGS los nodos de la planilla están en VARIOS PDL: "
                             + " · ".join(f"{p} → {', '.join(n[:8])}" for p, n in por_pdl.items())
                             + " — divide con ✎ (solo nodos + PDL)")

    # PDL para Programados: si aún falta, intentar las imágenes una sola vez
    if tipo == "Programados" and not d["pdl"] and not vision_hecha:
        vis = _analizar_imagenes(item, d, notas)
        if vis.get("pdl"):
            d["pdl"] = vis["pdl"]

    # URLs reales de SharePoint/OneDrive/Drive (del HTML, con entidades limpias)
    links_externos = []
    try:
        import html as _html
        html_body = item.HTMLBody or ""
        for href in re.findall(r'href="([^"]+)"', html_body):
            href = _html.unescape(href)
            if RE_LINK_EXTERNO.search(href) and href not in links_externos:
                links_externos.append(href)
    except Exception:
        pass
    link_externo = bool(links_externos) or bool(RE_LINK_EXTERNO.search(cuerpo))

    # Fecha real de ejecución: el Excel manda; si no, el cuerpo del correo
    if not fecha_ejecucion:
        fecha_ejecucion = _fecha_ejecucion_texto(cuerpo)

    # Líder desde el texto (respuestas del supervisor) — solo si el Excel no lo dio
    if not lideres:
        grupos, frase = _grupos_lideres_texto(cuerpo)
        distintos = []
        for g in grupos:
            if set(g) not in [set(x) for x in distintos]:
                distintos.append(g)
        if len(distintos) > 1:
            # Cuadrillas distintas por tramo: la planilla lleva por defecto a
            # los líderes COMUNES a todos los grupos (repartir a todos por %
            # le daría prestaciones a quien no trabajó ese tramo); el reparto
            # exacto se hace en liquidador2 → "Distribuir con IA".
            comunes = [n for n in distintos[0] if all(n in g for g in distintos)]
            union = []
            for g in distintos:
                union.extend(n for n in g if n not in union)
            lideres.extend(comunes or union)
            instruccion_lideres = frase
            notas.append(
                "El correo asigna CUADRILLAS DISTINTAS por tramo: "
                + " · ".join("[" + ", ".join(g) + "]" for g in distintos)
                + f" — la planilla lleva por defecto a {', '.join(comunes or union)}; para el "
                "reparto exacto usa 'Distribuir con IA' en liquidador2 con el texto de la tarjeta")
        elif distintos:
            lideres.extend(distintos[0])
            notas.append(f"Líderes tomados del texto del correo: {', '.join(distintos[0])}")
        else:
            lider_txt, ced_txt = _lider_en_texto(cuerpo)
            if lider_txt:
                lideres.append(lider_txt)
                if ced_txt:
                    lideres_det.append({"cedula": ced_txt, "nombre": lider_txt, "pct": 100.0})
                notas.append(f"Líder tomado del texto del correo: {lider_txt}"
                             + (f" (cédula {ced_txt})" if ced_txt else ""))

    # ── Faltantes ────────────────────────────────────────────────────────────
    faltantes = []
    if not codigo:
        faltantes.append("código (DF o LCL)")
    if not fecha_ejecucion:
        faltantes.append("fecha de ejecución")
    if tipo == "Programados" and not d["pdl"]:
        faltantes.append("PDL")
    if not filas_mo:
        if link_externo:
            faltantes.append("planilla (hay link externo SharePoint/Drive — descargarla y subirla)")
        elif hay_excel_adjunto:
            faltantes.append("planilla (el Excel adjunto no se pudo interpretar)")
        else:
            faltantes.append("planilla de prestaciones")
    if not lideres:
        faltantes.append("líder")

    try:
        store_id = item.Parent.StoreID
    except Exception:
        store_id = ""

    return {
        "entry_id": item.EntryID,
        "store_id": store_id,
        "id": uuid.uuid5(uuid.NAMESPACE_OID, item.EntryID).hex[:12],
        "asunto": asunto,
        "remitente": item.SenderName or "",
        "remitente_email": _smtp_remitente(item),
        "fecha": fecha,
        "carpeta": carpeta_ruta,
        "conversacion": conversacion,
        "tipo": tipo or "Desconocido",
        "codigo": codigo,
        "codigo_validado": codigo_validado,
        "codigo_fuente": (ganador or {}).get("fuente", ""),
        "codigo_coherencia": bool((ganador or {}).get("coherencia_nodos")),
        "lcl": d["lcl"],
        "pdl": "" if tipo == "MI" else d["pdl"],   # regla: MI no lleva PDL
        "filas_mo": filas_mo,
        "instruccion_lideres": instruccion_lideres,
        "filas_mat": filas_mat,
        "instruccion_mat": instruccion_material(asunto, cuerpo, fecha),
        "adjunto": adjunto_usado,
        "lideres": lideres,
        "lideres_det": lideres_det,
        "fecha_ejecucion": fecha_ejecucion,
        "link_externo": link_externo,
        "links_externos": links_externos[:5],
        "planilla_nativa": planilla_nativa,
        "excel_nativa_ruta": excel_nativa_ruta,
        "faltantes": faltantes,
        "carril": "super" if not faltantes else "semi",
        "notas": notas,
    }
