"""AUDITORÍA FRÍA de los análisis de la máquina (2026-09-22): tras detectar un análisis con un
fallo evidente, se revisan todos contra lo que el sistema puede probar sin IA.

Por cada caso 3.2 A del periodo toma su análisis vigente y lo cruza, SIN IA, contra lo que
el sistema puede probar:
  · el código del letrero de cada video (OCR crudo con el lector tolerante de letrero.py);
  · lo que dicta el audio (¿nombra este PDL u OTRO PDL del periodo?);
  · los amarres deterministas;
  · los videos del caso (letrero = orden) que existen en la base y NO entraron al análisis;
  · la cobertura de tiempos recalculada con las ventanas lógicas por gap.

Marcas:
  SI_AJENO          la IA dijo SI a un video cuyo letrero es de otra orden          ← grave
  SI_AUDIO_OTRO     SI a un video sin código cuyo audio dicta otro PDL              ← grave
  SI_SIN_SUSTENTO   SI sin código, sin amarre fuerte y sin que el audio dicte el PDL
  NO_PROPIO         la IA descartó un video cuyo letrero ES de la orden             ← evidencia perdida
  PROPIO_FUERA      hay videos de la orden en la base que no entraron al análisis   ← evidencia perdida
  PROPIO_SIN_VEREDICTO  un video de la orden entró y la IA no lo listó (el analista ya lo fuerza a SI)
  COBERTURA_CAMBIA  con las ventanas lógicas el veredicto de cobertura cambia

Uso (en el VPS, desde backend/):
  venv/bin/python scripts/auditar_analisis.py 2026-09
  venv/bin/python scripts/auditar_analisis.py 2026-09 --reanalizar     # re-corre los marcados que siguen «propuesto»
  venv/bin/python scripts/auditar_analisis.py 2026-09 --reanalizar --todos   # re-corre TODOS los 3.2 A propuestos
Nunca toca un análisis aplicado: esos solo se reportan (la decisión de reabrirlos es humana).
"""
import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sqlalchemy import select  # noqa: E402

from app.db.database import AsyncSessionLocal  # noqa: E402
from app.models.models import AnsRegistro, WaAnalisis, WaMensaje, WaMediaMeta  # noqa: E402
from app.services import letrero, matcher, wa_analista  # noqa: E402

UTC = timedelta(hours=5)


def _ids_en(texto: str) -> set:
    return {int(x) for x in re.findall(r"msg[_ ]?id[= ]?(\d+)", texto or "", re.I)}


async def auditar_caso(db, reg, a, conocidos) -> dict:
    res = json.loads(a.resultado or "{}")
    ia = res.get("ia") or {}
    marcas, detalle = [], []
    rnum, dfs = matcher._codigos_del_caso(reg)
    videos_ia = ia.get("videos") or []
    entraron = [v.get("msg_id") for v in (res.get("videos") or []) if v.get("msg_id")]
    ids = list(dict.fromkeys(entraron + [v.get("msg_id") for v in videos_ia if v.get("msg_id")]))
    metas = {x.msg_id: x for x in (await db.execute(select(WaMediaMeta).where(
        WaMediaMeta.msg_id.in_(ids)))).scalars().all()} if ids else {}
    amarres = await matcher.amarres_de(db, reg.id, minimo=75)
    trans_res = {v.get("msg_id"): v.get("transcripcion") for v in (res.get("videos") or [])}
    # Código que el analista leyó por VISIÓN cuando el OCR no dio nada (queda en res.videos)
    cod_vision = {v.get("msg_id"): v.get("codigo_letrero") for v in (res.get("videos") or [])
                  if v.get("codigo_letrero") and v.get("fuente_codigo") == "vision"}

    for v in videos_ia:
        mid = v.get("msg_id")
        meta = metas.get(mid)
        cod = matcher.codigo_letrero(meta) or cod_vision.get(mid)
        clase = letrero.clasificar(cod, rnum, dfs)
        trans = (meta.transcripcion if meta and meta.transcripcion is not None else trans_res.get(mid)) or ""
        otro = letrero.audio_menciona_otro(trans, rnum, conocidos)
        propio_audio = letrero.audio_menciona(trans, rnum)
        leyo = letrero.codigo_principal(f"{v.get('pdl_overlay') or ''} {v.get('overlay') or ''}") == rnum
        dc = v.get("del_caso")
        if dc == "SI":
            if clase == "ajeno":
                marcas.append("SI_AJENO")
                detalle.append(f"video {mid}: SI pero el letrero dice {cod}")
            elif clase == "sin_codigo" and otro and not propio_audio and not leyo:
                marcas.append("SI_AUDIO_OTRO")
                detalle.append(f"video {mid}: SI sin código y el audio dicta {otro}")
            elif clase == "sin_codigo" and mid not in amarres and not propio_audio and not leyo:
                marcas.append("SI_SIN_SUSTENTO")
                detalle.append(f"video {mid}: SI sin código, sin amarre y sin PDL en el audio")
        elif clase == "propio":
            marcas.append("NO_PROPIO")
            detalle.append(f"video {mid}: letrero {cod} = orden pero la IA lo dejó en {dc}")

    # Videos que entraron al análisis pero la IA no listó (sin veredicto): si son de la orden, es evidencia ignorada
    listados = {v.get("msg_id") for v in videos_ia}
    for mid in entraron:
        if mid not in listados and letrero.clasificar(matcher.codigo_letrero(metas.get(mid)), rnum, dfs) == "propio":
            marcas.append("PROPIO_SIN_VEREDICTO")
            detalle.append(f"video {mid}: letrero = orden, entró al análisis y la IA no lo listó")

    # Videos de la orden en la base (letrero = orden, día del caso ±1) que no entraron
    dia = wa_analista._parse_fecha(reg.fecha_fin) or wa_analista._parse_fecha(reg.fecha_inicio)
    if dia and rnum:
        r = await db.execute(select(WaMensaje, WaMediaMeta).join(WaMediaMeta, WaMediaMeta.msg_id == WaMensaje.id)
                             .where(WaMensaje.tipo == "video", WaMediaMeta.estado == "listo",
                                    WaMensaje.enviado_en >= dia - timedelta(days=1) + UTC,
                                    WaMensaje.enviado_en < dia + timedelta(days=2) + UTC))
        # Los grupos Exclusiones/Cuadrillas espejan el mismo video (mismo tamaño): el gemelo
        # del que sí entró no cuenta como «fuera».
        rm = await db.execute(select(WaMensaje.media_tamano).where(WaMensaje.id.in_(ids))) if ids else None
        tamanos = {t for (t,) in rm.all() if t} if rm is not None else set()
        fuera = [m.id for m, meta in r.all()
                 if matcher.codigo_letrero(meta) == rnum and m.id not in set(ids) and m.media_tamano not in tamanos]
        if fuera:
            marcas.append("PROPIO_FUERA")
            detalle.append(f"videos con letrero {rnum} que no entraron: {fuera}")

    # Cobertura con ventanas lógicas
    cov_old = ia.get("cobertura_calc") or {}
    if ia.get("tramos_justificados"):
        cov_new = wa_analista._calcular_cobertura(reg, ia)
        if cov_old and cov_old.get("cubierto") != cov_new.get("cubierto"):
            marcas.append("COBERTURA_CAMBIA")
            desc = sum(len(g.get("descartados") or []) for g in cov_new["gaps"].values())
            detalle.append(f"cobertura: antes cubierto={cov_old.get('cubierto')}, ahora {cov_new.get('cubierto')} "
                           f"({desc} tramo(s) fuera de la ventana lógica)")

    n_si = sum(1 for v in videos_ia if v.get("del_caso") == "SI")
    return {"analisis": a.id, "estado": a.estado, "reg_id": reg.id, "pdl": reg.pdl, "zona": reg.zona,
            "analista": ia.get("analista") or ("claude" if videos_ia else "-"),
            "sugerido": ia.get("excluible_sugerido"), "celda": reg.excluible, "videos": len(videos_ia),
            "si": n_si, "marcas": sorted(set(marcas)), "detalle": detalle}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("periodo", nargs="?", default=datetime.now().strftime("%Y-%m"))
    ap.add_argument("--reanalizar", action="store_true")
    ap.add_argument("--todos", action="store_true")
    ap.add_argument("--incluir-aplicados", action="store_true",
                    help="también re-corre los casos ya aplicados: crea una propuesta NUEVA (la celda no se toca)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    async with AsyncSessionLocal() as db:
        regs = (await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == a.periodo,
                                                            AnsRegistro.entorno == "real")
                                 .order_by(AnsRegistro.id))).scalars().all()
        conocidos = {x for x in (letrero.rnum(r.pdl) for r in regs) if x}
        ra = await db.execute(select(WaAnalisis).where(WaAnalisis.reg_id.in_([r.id for r in regs]),
                                                       WaAnalisis.estado != "descartado")
                              .order_by(WaAnalisis.id.desc()))
        vigente = {}
        for x in ra.scalars().all():
            vigente.setdefault(x.reg_id, x)
        filas = []
        for reg in regs:
            an = vigente.get(reg.id)
            if not an:
                continue
            res = json.loads(an.resultado or "{}")
            if res.get("tipo") != "3.2A":
                continue
            filas.append(await auditar_caso(db, reg, an, conocidos))

        if a.json:
            print(json.dumps(filas, ensure_ascii=False, indent=1))
        else:
            print(f"\nAUDITORÍA {a.periodo}: {len(filas)} análisis 3.2 A vigentes\n")
            print(f"{'PDL':17} {'zona':8} {'estado':10} {'analista':19} {'sug':4} {'celda':5} {'vid':>3} {'SI':>2}  marcas")
            for f in filas:
                print(f"{f['pdl']:17} {(f['zona'] or '-'):8} {f['estado']:10} {f['analista']:19} "
                      f"{str(f['sugerido'] or '-'):4} {str(f['celda'] or '-'):5} {f['videos']:>3} {f['si']:>2}  "
                      f"{', '.join(f['marcas']) or 'ok'}")
                for d in f["detalle"]:
                    print(f"{'':17}   · {d}")
            graves = [f for f in filas if any(m in f["marcas"] for m in ("SI_AJENO", "SI_AUDIO_OTRO"))]
            print(f"\ncon marcas: {sum(1 for f in filas if f['marcas'])} · graves (SI a video de otro PDL): {len(graves)} "
                  f"· aplicados con marcas: {sum(1 for f in filas if f['marcas'] and f['estado'] == 'aplicado')}")

        if a.reanalizar:
            objetivo = [f for f in filas if (f["estado"] == "propuesto" or a.incluir_aplicados)
                        and (a.todos or f["marcas"])]
            print(f"\nRE-ANÁLISIS de {len(objetivo)} caso(s)" + (" (todos)" if a.todos else " con marcas")
                  + (" incluyendo aplicados" if a.incluir_aplicados else " propuestos") + ":")
            for f in objetivo:
                async with AsyncSessionLocal() as db2:
                    reg = await db2.get(AnsRegistro, f["reg_id"])
                    try:
                        nuevo = await wa_analista.analizar_caso(db2, reg)
                        res = json.loads(nuevo.resultado or "{}")
                        ia = res.get("ia") or {}
                        n_si = sum(1 for v in (ia.get("videos") or []) if v.get("del_caso") == "SI")
                        cov = ia.get("cobertura_calc") or {}
                        print(f"  {reg.pdl}: #{f['analisis']} → #{nuevo.id} · analista {ia.get('analista') or 'claude'} · "
                              f"sugerido {f['sugerido']} → {ia.get('excluible_sugerido')} · videos SI {f['si']} → {n_si} · "
                              f"excluidos por letrero {len(res.get('videos_excluidos') or [])} · cubierto={cov.get('cubierto')}"
                              f"{' · REVISIÓN' if ia.get('revision') else ''}"
                              f"{' · error: ' + str(ia.get('error'))[:60] if ia.get('error') else ''}")
                        for t in ia.get("validacion") or []:
                            print(f"      ✎ {t}")
                    except Exception as e:  # noqa: BLE001
                        await db2.rollback()
                        print(f"  {reg.pdl}: ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
