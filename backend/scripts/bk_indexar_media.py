"""ÍNDICE de la media de WhatsApp — extrae de cada video/imagen lo que la máquina puede sacar
SIN IA, una sola vez, y lo deja en `wa_media_meta` (2026-09-03: todo lo que
llega al número se guarda, y los análisis automáticos deben partir de datos, no de bytes).

Por archivo:
  · ffprobe → duración, ancho, alto
  · 3 frames (inicio / mitad / final) a 640 px en data/whatsapp_meta/<msg_id>/f1..f3.jpg
  · OCR (tesseract, local) del letrero de la cámara en la esquina inferior derecha —
    fecha/hora REALES, GPS, lugar, código de la orden (PDL R… / DF…) y #líder — leído en
    los 3 frames y consolidado (gana el que más campos trae)
  · transcripción del audio con faster-whisper (local; --sin-audio la salta)

Uso (en el VPS, desde backend/):
  venv/bin/python scripts/indexar_media.py                 # pendientes, más nuevos primero
  venv/bin/python scripts/indexar_media.py --limite 20     # una muestra
  venv/bin/python scripts/indexar_media.py --sin-audio     # solo ffprobe + frames + OCR (rápido)
  venv/bin/python scripts/indexar_media.py --max-min 90    # tope de tiempo (para el cron)
  venv/bin/python scripts/indexar_media.py --solo 8136     # un mensaje concreto (reprocesa)
  venv/bin/python scripts/indexar_media.py --reintentar    # incluye los que quedaron en error (<3 intentos)

Idempotente: cada archivo se procesa una vez (fila listo). Nunca borra nada.
"""
import argparse
import asyncio
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sqlalchemy import select  # noqa: E402
from PIL import Image, ImageFilter  # noqa: E402

from app.db.database import AsyncSessionLocal, engine  # noqa: E402
from app.models.models import Base, WaMensaje, WaMediaMeta  # noqa: E402
from app.services import letrero  # noqa: E402

META_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "whatsapp_meta"))


# ── ffprobe / frames ──────────────────────────────────────────────────────────────
def probe(ruta: str):
    try:
        out = subprocess.run(["ffprobe", "-v", "quiet", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height:format=duration", "-of", "csv=p=0", ruta],
                             capture_output=True, text=True, timeout=60).stdout.strip().splitlines()
    except Exception:
        return None, None, None
    w = h = dur = None
    for ln in out:
        partes = ln.split(",")
        if len(partes) >= 2 and partes[0].isdigit():
            w, h = int(partes[0]), int(partes[1])
        elif len(partes) == 1:
            try:
                dur = float(partes[0])
            except ValueError:
                pass
    return dur, w, h


def frame_nativo(ruta: str, segundo: float, destino_png: str) -> bool:
    r = subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-ss", f"{max(segundo, 0):.2f}", "-i", ruta,
                        "-frames:v", "1", "-q:v", "2", destino_png], capture_output=True, timeout=120)
    return r.returncode == 0 and os.path.exists(destino_png)


# ── OCR del letrero ───────────────────────────────────────────────────────────────
def _tesseract(im: Image.Image, png: str, umbral: int, escala: int) -> str:
    c = im.resize((im.width * escala, im.height * escala), Image.LANCZOS)
    bw = c.point(lambda p: 0 if p > umbral else 255).filter(ImageFilter.MedianFilter(3))
    tmp = png + ".ocr.png"
    bw.save(tmp)
    try:
        out = subprocess.run(["tesseract", tmp, "-", "-l", "spa+eng", "--psm", "6"],
                             capture_output=True, text=True, timeout=60).stdout
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return "\n".join(ln.strip() for ln in out.splitlines() if ln.strip())


def _valor_lectura(txt: str) -> int:
    """Cuánto sirve una lectura: código (4) > fecha (2) > líder (1) > algo de texto (0)."""
    d = parsear_letrero(txt)
    return (4 if d.get("codigo") else 0) + (2 if d.get("fecha") else 0) + (1 if d.get("lider") else 0)


def ocr_letrero(png: str) -> str:
    """Letrero de la cámara. PASADAS de respaldo (verificación 2026-09-07 contra videos ya
    validados: la esquina 30 %/umbral 200 perdía el «PDL R…» en varios): 1) esquina inferior
    derecha 55 %×30 %, umbral 200; si no sale el código, 2) esquina 60 %×42 %, umbral 170;
    si tampoco, 3) el cuadro completo (plantillas y letreros en otra esquina). Se queda con
    la lectura que más campos entrega."""
    im = Image.open(png).convert("L")
    w, h = im.size
    mejor, valor = "", -1
    pasadas = [
        (im.crop((int(w * 0.45), int(h * 0.70), w, h)), 200, 4),
        (im.crop((int(w * 0.40), int(h * 0.58), w, h)), 170, 4),
        (im, 190, 2),
    ]
    for i, (crop, umbral, escala) in enumerate(pasadas):
        txt = _tesseract(crop, png, umbral, escala)
        v = _valor_lectura(txt)
        if v > valor:
            mejor, valor = txt, v
        if valor >= 6:                    # código + fecha: no hace falta seguir
            break
    return mejor


_GLIFOS = str.maketrans({"O": "0", "o": "0", "A": "4", "a": "4", "I": "1", "l": "1", "S": "5", "s": "5",
                         "G": "6", "g": "6", "B": "8", "b": "8", "/": "7"})


def _digitos_ocr(s: str) -> str:
    """«O6/05» → «06705»: glifos que el OCR confunde con dígitos, en contexto de código."""
    return re.sub(r"\D", "", s.translate(_GLIFOS))


def _sin_tildes(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


def parsear_letrero(txt: str) -> dict:
    """Campos del letrero a partir del OCR crudo (tolerante a ruido)."""
    t = _sin_tildes(txt)
    d = {}
    # 3 sept 2026 11:15:22 a.m.   ·   05/08/2026 8:23:03 a. m.  — la lectura vive en
    # app/services/letrero.py (la usa también el analista para el reloj de grabación)
    fh = letrero.fecha_hora(t)
    if fh:
        d["fecha"], d["hora"] = fh["fecha"], fh["hora"]
    # 4.82484N 73.69598W  ·  4,7483N 73,5556W
    m = re.search(r"(\d{1,2})[.,](\d{3,6})\s*([NS])\D{0,4}(\d{2,3})[.,](\d{3,6})\s*([WO])", t, re.I)
    if m:
        lat = float(f"{m.group(1)}.{m.group(2)}") * (-1 if m.group(3).upper() == "S" else 1)
        lon = -float(f"{m.group(4)}.{m.group(5)}")
        if -5 < lat < 14 and -80 < lon < -66:          # Colombia
            d["lat"], d["lon"] = round(lat, 6), round(lon, 6)
    # Código de la orden: PDL R06705 · pdlR09099 · CDOPP2026R06705 · DF202664648609. La lectura
    # vive en app/services/letrero.py (auditoría 2026-09-22: el regex de aquí exigía «PDL» en
    # mayúsculas y la app estampa «pdlR09099» — tres videos del R09099 acabaron anexados al
    # R09024). Tolera pdl/pdI/pdi, la O por cero y los glifos habituales del OCR.
    cod = letrero.codigo_principal(t)
    if cod:
        d["codigo"] = cod
    m = re.search(r"#\s*([A-Za-z][\w]{2,30})", t)
    if m:
        d["lider"] = m.group(1)
    else:
        # sin «#» legible: la ÚLTIMA línea del letrero con una sola palabra capitalizada de 4+
        # letras (la app pone el #líder al final) — se tolera un glifo pegado adelante (H/R/S)
        lineas = [ln.strip() for ln in t.splitlines() if ln.strip()]
        if lineas:
            mm = re.match(r"^[^A-Za-z]?[A-Z]?([A-Z][a-z]{3,20})$", lineas[-1])
            if mm and mm.group(1).lower() not in ("nodo", "ubala", "gacheta", "junin", "sueva"):
                d["lider"] = mm.group(1)
    # Lugar: la línea que no es fecha ni GPS ni código ni hashtag y tiene letras
    for ln in t.splitlines():
        s = ln.strip(" ·-•.:|>‘'\"")
        if len(s) < 4 or re.search(r"\d{4}|\d{1,2}[.,]\d{3,}", s) or "#" in s:
            continue
        if re.search(r"altit|altiu|alutud|veloc|indice|índice|msrm|km", s, re.I):
            continue
        if re.search(r"[A-Za-z]{4,}", s) and not re.search(r"DF\d|PDL", s, re.I):
            d["lugar"] = re.sub(r"[^\w\s\-,]", "", s).strip()[:200]
            break
    return d


def _guardar_fecha(c: dict, m: WaMensaje) -> None:
    """La cuadrilla graba y sube el mismo día: si la fecha leída se aleja más de 1 día de la
    fecha de envío del mensaje, es un error de OCR («9 ago» por «5 ago») → manda la de envío,
    conservando la hora del letrero."""
    if not m.enviado_en:
        return
    envio = (m.enviado_en - timedelta(hours=5)).date()
    f = c.get("fecha")
    try:
        leida = datetime.strptime(f, "%Y-%m-%d").date() if f else None
    except ValueError:
        leida = None
    if leida is None and c.get("hora"):
        c["fecha"] = envio.isoformat()
    elif leida is not None and abs((leida - envio).days) > 1:
        c["fecha"] = envio.isoformat()
        c["fecha_corregida"] = f


def consolidar(lecturas: list[dict]) -> dict:
    """Entre los 3 frames, por campo, el valor que más veces aparece (o el primero)."""
    out = {}
    for k in ("fecha", "hora", "lat", "lon", "lugar", "codigo", "lider"):
        vals = [l[k] for l in lecturas if l.get(k)]
        if not vals:
            continue
        if k == "hora":
            out[k] = next((l["hora"] for l in lecturas if l.get("fecha") and l.get("hora")), vals[0])
        else:
            out[k] = max(set(vals), key=vals.count)
    return out


# ── Un archivo ────────────────────────────────────────────────────────────────────
def procesar(m: WaMensaje, meta: WaMediaMeta, con_audio: bool) -> None:
    t0 = time.time()
    ruta = m.media_ruta
    if not ruta or not os.path.exists(ruta):
        raise FileNotFoundError("media no está en disco")
    carpeta = os.path.join(META_DIR, str(m.id))
    os.makedirs(carpeta, exist_ok=True)
    lecturas, crudo = [], []

    if m.tipo == "video":
        dur, w, h = probe(ruta)
        meta.duracion_s, meta.ancho, meta.alto = dur, w, h
        puntos = [1.0, (dur or 2) / 2, max((dur or 2) - 1.5, 0.5)]
        for i, seg in enumerate(puntos, 1):
            png = os.path.join(carpeta, f"nat{i}.png")
            if not frame_nativo(ruta, seg, png):
                continue
            # frame de muestra (640 px) para la interfaz / la IA
            Image.open(png).convert("RGB").resize((640, int(640 * Image.open(png).height / Image.open(png).width))).save(
                os.path.join(carpeta, f"f{i}.jpg"), quality=80)
            txt = ocr_letrero(png)
            crudo.append(f"[frame {i} @ {seg:.0f}s]\n{txt}")
            lecturas.append(parsear_letrero(txt))
            os.remove(png)
        # MÁS PUNTOS DEL LETRERO (16 oct 2026): el letrero puede quedar ilegible sobre follaje o cielo en los
        # 3 puntos fijos y verse nítido en otros (video 14033 del R09734: legible solo desde el segundo 18).
        # Si con ellos no salen dos lecturas que cuadren, hasta 6 puntos más con el mismo OCR local.
        if dur and dur > 6 and not letrero.reloj_grabacion(letrero.lecturas_reloj("\n\n".join(crudo)), dur):
            for k, frac in enumerate((0.75, 0.25, 0.65, 0.35, 0.85, 0.15), 4):
                seg = dur * frac
                png = os.path.join(carpeta, f"nat{k}.png")
                if not frame_nativo(ruta, seg, png):
                    continue
                txt = ocr_letrero(png)
                crudo.append(f"[frame {k} @ {seg:.1f}s]\n{txt}")
                lecturas.append(parsear_letrero(txt))
                os.remove(png)
                if letrero.reloj_grabacion(letrero.lecturas_reloj("\n\n".join(crudo)), dur):
                    break
        if con_audio and meta.transcripcion is None:
            from app.services.transcribe import transcribir_video
            meta.transcripcion = transcribir_video(ruta) or ""
    elif m.tipo == "imagen":
        im = Image.open(ruta)
        meta.ancho, meta.alto = im.size
        im.convert("RGB").resize((640, int(640 * im.height / im.width))).save(os.path.join(carpeta, "f1.jpg"), quality=80)
        png = os.path.join(carpeta, "nat1.png")
        im.convert("RGB").save(png)
        txt = ocr_letrero(png)
        crudo.append(f"[imagen]\n{txt}")
        lecturas.append(parsear_letrero(txt))
        os.remove(png)
    elif m.tipo == "audio" and con_audio:
        from app.services.transcribe import transcribir_video
        meta.transcripcion = transcribir_video(ruta) or ""
        dur, _, _ = probe(ruta)
        meta.duracion_s = dur
    else:
        dur, w, h = probe(ruta)
        meta.duracion_s, meta.ancho, meta.alto = dur, w, h

    c = consolidar(lecturas)
    _guardar_fecha(c, m)
    meta.frames_dir = carpeta if os.listdir(carpeta) else None
    meta.overlay_txt = "\n\n".join(crudo) or None
    meta.overlay_fecha, meta.overlay_hora = c.get("fecha"), c.get("hora")
    meta.overlay_lat, meta.overlay_lon = c.get("lat"), c.get("lon")
    meta.overlay_lugar, meta.overlay_codigo, meta.overlay_lider = c.get("lugar"), c.get("codigo"), c.get("lider")
    meta.tiempo_s = round(time.time() - t0, 1)
    meta.estado, meta.error = "listo", None
    meta.procesado_en = datetime.now()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=0)
    ap.add_argument("--max-min", type=float, default=0, help="tope de minutos de pared")
    ap.add_argument("--sin-audio", action="store_true")
    ap.add_argument("--solo", type=int, default=0, help="msg_id concreto (reprocesa)")
    ap.add_argument("--reintentar", action="store_true")
    ap.add_argument("--tipos", default="video,imagen,audio")
    ap.add_argument("--reocr-sin-codigo", action="store_true",
                    help="repite el OCR (pasadas de respaldo) en videos/imágenes ya indexados sin código; conserva la transcripción")
    ap.add_argument("--reparsear", action="store_true",
                    help="reinterpreta el OCR ya guardado (overlay_txt) con el parser actual, sin ffmpeg ni tesseract")
    a = ap.parse_args()
    tipos = [t.strip() for t in a.tipos.split(",") if t.strip()]

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[WaMediaMeta.__table__])

    inicio = time.time()
    hechos = errores = 0
    if a.reparsear:
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(WaMediaMeta, WaMensaje).join(WaMensaje, WaMensaje.id == WaMediaMeta.msg_id)
                                 .where(WaMediaMeta.estado == "listo", WaMediaMeta.overlay_txt.isnot(None)))
            filas = r.all()
            cambios = 0
            for meta, m in filas:
                bloques = [b.split("\n", 1)[1] if "\n" in b else "" for b in meta.overlay_txt.split("\n\n[") ]
                lecturas = [parsear_letrero(b) for b in bloques if b.strip()]
                c = consolidar(lecturas)
                _guardar_fecha(c, m)
                # Reloj validado (dos lecturas que cuadran, OCR o visión del analista): manda
                # sobre la lectura suelta de un frame
                rj = letrero.reloj_grabacion(letrero.lecturas_reloj(meta.overlay_txt), meta.duracion_s,
                                             (m.enviado_en - timedelta(hours=5)) if m.enviado_en else None)
                if rj and (not c.get("hora") or c.get("fecha") != rj["fecha"]
                           or abs(letrero._seg_dia(c["hora"]) - letrero._seg_dia(rj["inicio"])) > (meta.duracion_s or 0) + 60):
                    c["fecha"], c["hora"] = rj["fecha"], rj["inicio"]
                nuevo = (c.get("fecha"), c.get("hora"), c.get("codigo"), c.get("lider"), c.get("lugar"))
                viejo = (meta.overlay_fecha, meta.overlay_hora, meta.overlay_codigo, meta.overlay_lider, meta.overlay_lugar)
                if nuevo != viejo:
                    meta.overlay_fecha, meta.overlay_hora, meta.overlay_codigo, meta.overlay_lider, meta.overlay_lugar = nuevo
                    meta.overlay_lat, meta.overlay_lon = c.get("lat"), c.get("lon")
                    cambios += 1
            await db.commit()
            print(f"reparseo: {len(filas)} registros · {cambios} actualizados")
        return
    async with AsyncSessionLocal() as db:
        if a.solo:
            q = select(WaMensaje).where(WaMensaje.id == a.solo)
        elif a.reocr_sin_codigo:
            sub = select(WaMediaMeta.msg_id).where(WaMediaMeta.estado == "listo", WaMediaMeta.overlay_codigo.is_(None),
                                                   WaMediaMeta.tipo.in_(["video", "imagen"]))
            q = select(WaMensaje).where(WaMensaje.id.in_(sub), WaMensaje.media_ruta.isnot(None)).order_by(WaMensaje.id.desc())
            if a.limite:
                q = q.limit(a.limite)
            a.sin_audio = True            # la transcripción ya está: solo se rehace el letrero
        else:
            sub = select(WaMediaMeta.msg_id).where(WaMediaMeta.estado == "listo")
            if not a.reintentar:
                sub = select(WaMediaMeta.msg_id).where(
                    (WaMediaMeta.estado == "listo") | (WaMediaMeta.intentos >= 3))
            q = (select(WaMensaje).where(WaMensaje.media_ruta.isnot(None), WaMensaje.tipo.in_(tipos),
                                         WaMensaje.id.not_in(sub))
                 .order_by(WaMensaje.id.desc()))
            if a.limite:
                q = q.limit(a.limite)
        msgs = (await db.execute(q)).scalars().all()
        print(f"{len(msgs)} archivo(s) por indexar (audio={'no' if a.sin_audio else 'sí'})")
        for m in msgs:
            if a.max_min and (time.time() - inicio) / 60 > a.max_min:
                print("tope de tiempo alcanzado; el resto queda para la próxima pasada")
                break
            meta = (await db.execute(select(WaMediaMeta).where(WaMediaMeta.msg_id == m.id))).scalars().first()
            if not meta:
                meta = WaMediaMeta(msg_id=m.id, tipo=m.tipo)
                db.add(meta)
            meta.intentos = (meta.intentos or 0) + 1
            try:
                procesar(m, meta, con_audio=not a.sin_audio)
                hechos += 1
                print(f"  ✓ {m.id} {m.tipo} {meta.tiempo_s}s · {meta.overlay_fecha or '-'} {meta.overlay_hora or '-'} "
                      f"· {meta.overlay_codigo or '-'} · {meta.overlay_lugar or '-'} · {meta.overlay_lider or '-'}"
                      f"{' · audio ' + str(len(meta.transcripcion or '')) + ' car.' if meta.transcripcion else ''}")
            except Exception as e:  # noqa: BLE001
                meta.estado, meta.error = "error", str(e)[:400]
                errores += 1
                print(f"  ✗ {m.id} {m.tipo}: {e}")
            await db.commit()
    print(f"listo: {hechos} indexados, {errores} con error, {(time.time() - inicio) / 60:.1f} min")


if __name__ == "__main__":
    asyncio.run(main())
