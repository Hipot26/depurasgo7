"""
leer_proyectos_mes.py — Lee prestaciones del mes desde la API de SGS
=====================================================================
Modelo: project.activities.details
  - date           : Fecha De Avance
  - project_id     : Many2one a project.dominion
  - create_uid     : quien creó el avance
  - advanced_price : valor monetario del avance

Flujo mejorado (sin filtro por uid):
  1. Consultar TODOS los avances del mes (sin restricción de usuario)
  2. Agrupar por project_id
  3. Leer project.dominion → supervisor, pdl, lider, etc.
  4. Filtrar: solo proyectos cuyo supervisor está en SUPERVISORES_EQUIPO
  5. Consolidar nodos del mismo DF
  6. Devolver consolidado + detalle individual de cada prestación
"""

import sys
import os
import re
from collections import defaultdict
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SGS UI"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SGS + SONY", "sgs_api"))

from sgs_readonly_client import SGSReadOnlyClient
import config as cfg

CAMPOS_DOMINION_BUSQUEDA = [
    "id", "name", "code", "display_name",
    "nodo", "pdl", "odm", "order_number", "circuito",
    "responsible_id", "approve_id", "partner_id", "department_id",
    "parent_id", "create_date", "write_date",
]

CAMPOS_AVANCE = [
    "id", "date", "project_id", "project_activities_id",
    "partner_id", "amount", "advanced_price",
    "production_type", "state", "create_uid", "name",
]


def _conectar():
    db = SGSReadOnlyClient.detectar_db(cfg.SGS_URL)
    c  = SGSReadOnlyClient(cfg.SGS_URL, db, cfg.SGS_LOGIN, cfg.SGS_PASSWORD)
    c.autenticar()
    return c


def _rango_mes(año: int, mes: int):
    inicio = datetime(año, mes, 1)
    fin    = datetime(año + 1, 1, 1) if mes == 12 else datetime(año, mes + 1, 1)
    return inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")


def _parse_fecha(raw) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    for fmt, largo in [("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)]:
        try:
            return datetime.strptime(s[:largo], fmt)
        except ValueError:
            continue
    return None


def _limpio(val) -> str:
    """Devuelve el valor solo si no es un marcador vacío como N/A, NA, False, None.
    Descarta números cortos (nodos internos como '1', '2') que no son códigos PDL/LCL."""
    if not val:
        return ""
    s = str(val).strip()
    su = s.upper()
    if su in ("N/A", "NA", "FALSE", "NONE", "-", "0"):
        return ""
    # Números puros < 1000 son nodos internos de SGS, no códigos PDL/LCL válidos
    if s.isdigit() and int(s) < 1000:
        return ""
    return s


def _split_pdls(raw) -> list:
    """Lista de PDL individuales de la casilla PDL de SGS. La casilla ACUMULA el
    historial separado por ';' (el nodo copia los del padre y se le agregan los
    nuevos): el ÚLTIMO de la lista es el PDL vigente. Limpia el '/' final
    ('CDOPP2026R00326/') y deduplica conservando el orden."""
    if not raw:
        return []
    out = []
    # ';' es el separador oficial; ':' y ',' aparecen por errores de digitación en SGS
    for parte in str(raw).replace(":", ";").replace(",", ";").split(";"):
        s = parte.strip().strip("/").strip()
        if not s or s.upper() in ("N/A", "NA", "-", "—", "N//A"):
            continue
        if s not in out:
            out.append(s)
    return out


def _extraer_codigo(display_name: str) -> str:
    if not display_name:
        return ""
    m = re.match(r"\[([^\]]+)\]", display_name)
    return m.group(1).strip() if m else display_name.strip()


def _es_responsable_liquidador(responsible_raw) -> bool:
    """True si responsible_id es el liquidador (dato incorrecto: el liquidador asignado como responsable).
    Un liquidador que además es supervisor no cuenta aquí: su responsible_id es correcto."""
    uid_liq = getattr(cfg, "SGS_UID_LIQUIDADOR", None)
    if not uid_liq or not responsible_raw:
        return False
    if isinstance(responsible_raw, (list, tuple)):
        return responsible_raw[0] == uid_liq
    return False


def _resolver_supervisor(responsible_raw) -> tuple[str, str]:
    """Devuelve (supervisor_equipo, nombre_sgs_raw).
    Si responsible_id es el liquidador → devuelve ('?_LIQUIDADOR', nombre)
    para que el caller use MAPA_PROYECTO_SUPERVISOR.
    """
    if not responsible_raw:
        return "?", ""
    if isinstance(responsible_raw, (list, tuple)) and len(responsible_raw) > 1:
        nombre_sgs = str(responsible_raw[1]).upper().strip()
    else:
        nombre_sgs = str(responsible_raw).upper().strip()

    # Si responsible_id es el liquidador mismo → datos incorrectos en SGS
    if _es_responsable_liquidador(responsible_raw):
        return "?_LIQUIDADOR", nombre_sgs
    # Responsable que NO es supervisor de campo (p. ej. un profesional de apoyo que figura
    # como responsable de nodos liquidados por el equipo: antes se descartaban como
    # "supervisor fuera del equipo"). Mismo camino que cuando el responsable es el
    # liquidador: el supervisor se infiere por approve_id / padre / líder.
    if any(f.upper() in nombre_sgs for f in getattr(cfg, "RESPONSABLES_NO_SUPERVISOR", [])):
        return "?_LIQUIDADOR", nombre_sgs

    palabras_sgs = set(nombre_sgs.split())
    # 1) Coincidencia exacta o todas las palabras del supervisor presentes
    for sup in cfg.SUPERVISORES_EQUIPO:
        palabras_sup = sup.upper().split()
        if sup.upper() == nombre_sgs or all(p in palabras_sgs for p in palabras_sup):
            return sup, nombre_sgs
    # 2) Mapa explícito de fragmentos SGS → supervisor
    for clave, valor in cfg.MAPA_SUPERVISOR.items():
        if clave.upper() in nombre_sgs:
            return valor, nombre_sgs
    # 3) Fallback: cualquier palabra del supervisor que aparezca en el nombre SGS
    for sup in cfg.SUPERVISORES_EQUIPO:
        for palabra_sup in sup.upper().split():
            if palabra_sup in palabras_sgs:
                return sup, nombre_sgs
    return nombre_sgs, nombre_sgs


def _leer_dominion_individual(c, pro_id: int) -> dict:
    try:
        recs = c.read("project.dominion", [pro_id], CAMPOS_DOMINION_BUSQUEDA)
        return recs[0] if recs else {}
    except Exception:
        return {"id": pro_id}


def _leer_dominions_lote(c, pro_ids: list, tam_lote: int = 100,
                         progress_cb=None) -> dict:
    """Lee project.dominion en lotes de `tam_lote` ids por llamada.
    Una llamada por proyecto (el modo anterior) tardaba ~3 min con el mes
    completo y disparaba el timeout de nginx (300s). Si un lote falla
    (p.ej. un id inaccesible invalida el read completo), cae al modo
    individual solo para ese lote.
    `progress_cb(fraccion, msg)` recibe el avance del lote (0.0 a 1.0)."""
    doms = {}
    n = len(pro_ids)
    for i in range(0, n, tam_lote):
        lote = pro_ids[i:i + tam_lote]
        if progress_cb:
            try:
                progress_cb(i / n, f"Leyendo proyectos {i + 1}–{min(i + tam_lote, n)} de {n}…")
            except Exception:
                pass
        try:
            for rec in c.read("project.dominion", lote, CAMPOS_DOMINION_BUSQUEDA):
                doms[rec["id"]] = rec
        except Exception:
            for pid in lote:
                d = _leer_dominion_individual(c, pid)
                if d:
                    doms[pid] = d
    return doms


def _depto_norm(dom: dict) -> str:
    dep_raw  = dom.get("department_id")
    dep_name = dep_raw[1] if isinstance(dep_raw, (list, tuple)) and len(dep_raw) > 1 else str(dep_raw or "")
    return dep_name.upper().replace("Á", "A").replace("É", "E").replace("\xa0", " ").strip()


def _padre_del_equipo(c, dom: dict, cache: dict, deptos: list) -> bool:
    """True si el PRO padre del nodo está en un departamento del equipo."""
    parent_raw = dom.get("parent_id")
    parent_id  = parent_raw[0] if isinstance(parent_raw, (list, tuple)) and parent_raw else None
    if not parent_id:
        return False
    if parent_id not in cache:
        cache[parent_id] = _leer_dominion_individual(c, parent_id)
    dep = _depto_norm(cache[parent_id] or {})
    return any(d.upper() in dep for d in deptos)


def _liquidado_por_equipo(avances: list) -> bool:
    """True si algún avance lo creó un liquidador del equipo (cfg.LIQUIDADORES_EQUIPO)."""
    frags = [x.upper() for x in getattr(cfg, "LIQUIDADORES_EQUIPO", [])]
    for av in avances:
        u = av.get("create_uid")
        if isinstance(u, (list, tuple)) and len(u) > 1:
            n = " ".join(str(u[1]).split()).upper()
            if any(f in n for f in frags):
                return True
    return False


def leer_proyectos_mes(año: int = None, mes: int = None, verbose: bool = True,
                       progress_cb=None) -> dict:
    """
    Devuelve un dict con dos secciones:
      {
        "consolidado": [...],   # un dict por proyecto (compatible con generar_excel)
        "detalle":     [...],   # un dict por prestación individual
      }
    `progress_cb(pct, msg)` — callback opcional de avance (pct 0-100) para
    mostrar barra de progreso en el frontend.
    """
    def _prog(pct, msg):
        if progress_cb:
            try:
                progress_cb(pct, msg)
            except Exception:
                pass

    hoy = datetime.now()
    if año is None: año = hoy.year
    if mes  is None: mes  = hoy.month

    inicio_str, fin_str = _rango_mes(año, mes)
    MESES_ES = {1:"ENERO",2:"FEBRERO",3:"MARZO",4:"ABRIL",5:"MAYO",
                6:"JUNIO",7:"JULIO",8:"AGOSTO",9:"SEPTIEMBRE",
                10:"OCTUBRE",11:"NOVIEMBRE",12:"DICIEMBRE"}

    if verbose:
        print(f"\n[SGS] Consultando prestaciones — {MESES_ES[mes]} {año}")
        print(f"      Rango Fecha De Avance: {inicio_str} a {fin_str}")

    _prog(3, "Conectando con la plataforma…")
    c = _conectar()
    if verbose:
        print(f"      Autenticado: uid={c.uid}")
    _prog(8, "Autenticado. Consultando prestaciones del mes…")

    # ── PASO 1: Todos los avances del mes (se filtra por depto UBATE en paso 3) ─
    avances = c.search_read(
        "project.activities.details",
        [["date", ">=", inicio_str],
         ["date", "<",  fin_str],
         ["advanced_price", "!=", 0]],
        CAMPOS_AVANCE,
        limit=10000,
    )

    if not avances:
        if verbose:
            print("      Sin avances en este mes.")
        _prog(100, "Sin prestaciones en este mes.")
        return {"consolidado": [], "detalle": []}

    if verbose:
        print(f"      Avances totales en el mes: {len(avances)}")
    _prog(18, f"{len(avances)} prestaciones encontradas. Agrupando por proyecto…")

    # ── PASO 2: Agrupar por project_id ────────────────────────────────────────
    fechas_por_pro      = defaultdict(list)
    lider_por_pro       = {}
    mo_mes_por_pro      = defaultdict(float)
    n_avances_pro       = defaultdict(int)
    avances_por_pro     = defaultdict(list)
    lideres_mo_por_pro  = defaultdict(dict)   # {pro_id: {nombre_lider: mo_total}}

    for av in avances:
        pid_raw = av.get("project_id")
        if not pid_raw:
            continue
        pro_id = pid_raw[0] if isinstance(pid_raw, (list, tuple)) else int(pid_raw)

        f = _parse_fecha(av.get("date"))
        if f:
            fechas_por_pro[pro_id].append(f)

        mo_av = float(av.get("advanced_price") or 0)
        mo_mes_por_pro[pro_id] += mo_av

        partner_raw = av.get("partner_id")
        if partner_raw:
            if isinstance(partner_raw, (list, tuple)) and len(partner_raw) > 1:
                nombre_lider = str(partner_raw[1]).upper().strip()
            elif isinstance(partner_raw, str):
                nombre_lider = partner_raw.upper().strip()
            else:
                nombre_lider = ""
            if nombre_lider:
                lideres_mo_por_pro[pro_id][nombre_lider] = (
                    lideres_mo_por_pro[pro_id].get(nombre_lider, 0.0) + mo_av
                )
                if pro_id not in lider_por_pro:
                    lider_por_pro[pro_id] = nombre_lider

        n_avances_pro[pro_id] += 1
        avances_por_pro[pro_id].append(av)

    pro_ids = list(fechas_por_pro.keys())
    if verbose:
        print(f"      Proyectos únicos en el mes: {len(pro_ids)}")

    # ── PASO 3: Leer datos de cada proyecto y filtrar por equipo ──────────────
    if verbose:
        print("      Leyendo datos de proyectos y filtrando por equipo...")

    proyectos = []
    detalle_items = []

    # 20% → 85% del progreso corresponde a la lectura por lotes
    doms_por_id = _leer_dominions_lote(
        c, pro_ids,
        progress_cb=lambda frac, msg: _prog(20 + int(65 * frac), msg))
    _prog(88, "Filtrando proyectos del equipo…")
    padres_cache: dict = dict(doms_por_id)

    for pro_id in pro_ids:
        dom = doms_por_id.get(pro_id) or _leer_dominion_individual(c, pro_id)
        if not dom:
            continue

        # Filtro por departamento: solo Ubaté Mayor Impacto y Ubaté TP
        dep_raw  = dom.get("department_id")
        dep_name = dep_raw[1] if isinstance(dep_raw, (list, tuple)) and len(dep_raw) > 1 else str(dep_raw or "")
        dep_norm = dep_name.upper().replace("Á","A").replace("É","E").replace("\xa0", " ").strip()
        # Departamentos del equipo (cfg.DEPARTAMENTOS_EQUIPO): Ubaté MI/TP y, en el
        # contrato 2026, "NZ7 - Telecontrol" — también lo liquida el equipo.
        deptos = getattr(cfg, "DEPARTAMENTOS_EQUIPO", [])
        if not any(d.upper() in dep_norm for d in deptos):
            # Nodo archivado en otro departamento dentro de una familia del equipo
            # (caso real PRO-064594, "NZ7 - Choconta TP" en la familia Ubaté
            # PRO-053113: $6,3 M de sept 2026 desaparecían de la fila sin aviso).
            # Cuenta si su padre es del equipo Y lo liquidó el equipo; lo que
            # liquidó otro grupo (PRO-058688, Línea Viva) sigue fuera.
            padre_eq = _padre_del_equipo(c, dom, padres_cache, deptos)
            liq_eq   = _liquidado_por_equipo(avances_por_pro.get(pro_id, []))
            if not (padre_eq and liq_eq):
                if padre_eq and verbose:
                    print(f"      SKIP {dom.get('code')}: depto '{dep_name.strip()}' dentro de "
                          f"familia del equipo, pero no lo liquidó el equipo")
                continue
            if verbose:
                print(f"      INFO {dom.get('code')}: depto '{dep_name.strip()}' — incluido por "
                      f"ser de una familia del equipo liquidada por el equipo")
        # Departamento compartido con otro grupo (p. ej. Telecontrol ↔ Línea Viva):
        # solo cuenta lo que liquidó el equipo (cfg.LIQUIDADORES_EQUIPO).
        compartidos = getattr(cfg, "DEPARTAMENTOS_COMPARTIDOS", [])
        es_compartido = any(d.upper() in dep_norm for d in compartidos)
        if es_compartido:
            liq_frags = [x.upper() for x in getattr(cfg, "LIQUIDADORES_EQUIPO", [])]
            nombres_liq = []
            for av in avances_por_pro.get(pro_id, []):
                u = av.get("create_uid")
                if isinstance(u, (list, tuple)) and len(u) > 1:
                    nombres_liq.append(" ".join(str(u[1]).split()).upper())
            if not any(f in n for f in liq_frags for n in nombres_liq):
                if verbose:
                    print(f"      SKIP {dom.get('code')}: depto compartido, liquidado por {sorted(set(nombres_liq))}")
                continue

        # pro_code se extrae PRIMERO (se necesita antes de resolver supervisor)
        pro_code = dom.get("code") or _extraer_codigo(
            dom.get("display_name") or dom.get("name", ""))

        supervisor, supervisor_raw = _resolver_supervisor(dom.get("responsible_id"))
        # En departamentos compartidos el responsible suele ser el creador de Línea Viva
        # (y puede casar por nombre con un supervisor del equipo); el supervisor real es la casilla
        # "Supervisor/Coordinador" (approve_id) — se usa si resuelve a alguien del equipo.
        if es_compartido and dom.get("approve_id"):
            sup_ap, raw_ap = _resolver_supervisor(dom.get("approve_id"))
            if sup_ap in cfg.SUPERVISORES_EQUIPO:
                supervisor, supervisor_raw = sup_ap, raw_ap

        # Si responsible_id es el liquidador, buscar supervisor correcto
        if supervisor == "?_LIQUIDADOR":
            mapa_proy = getattr(cfg, "MAPA_PROYECTO_SUPERVISOR", {})
            # 1) MAPA explícito por código propio
            supervisor = mapa_proy.get(pro_code, "")
            # 1b) Casilla "Supervisor/Coordinador" (approve_id) de SGS, si es alguien del
            #     equipo: es el dato real cuando el responsable es el liquidador o un apoyo.
            #     Va antes del padre y del líder; solo cede ante el mapa manual por código.
            if not supervisor and dom.get("approve_id"):
                parent_raw_ap = dom.get("parent_id")
                p_code_ap = (_extraer_codigo(str(parent_raw_ap[1]))
                             if isinstance(parent_raw_ap, (list, tuple)) and len(parent_raw_ap) > 1 else "")
                if not mapa_proy.get(p_code_ap):
                    sup_ap, _ = _resolver_supervisor(dom.get("approve_id"))
                    if sup_ap in cfg.SUPERVISORES_EQUIPO:
                        supervisor = sup_ap
                        if verbose:
                            print(f"      INFO {pro_code}: supervisor por casilla Supervisor/Coordinador → {supervisor}")
            # 2) MAPA por código del proyecto padre
            if not supervisor:
                parent_raw2 = dom.get("parent_id")
                if isinstance(parent_raw2, (list, tuple)) and len(parent_raw2) > 1:
                    p_code = _extraer_codigo(str(parent_raw2[1]))
                    supervisor = mapa_proy.get(p_code, "")
                    # 3) Leer responsible_id del padre directamente desde SGS
                    if not supervisor:
                        parent_id_val = parent_raw2[0]
                        parent_dom = _leer_dominion_individual(c, parent_id_val)
                        if parent_dom:
                            sup_parent, _ = _resolver_supervisor(parent_dom.get("responsible_id"))
                            if (sup_parent not in ("?", "?_LIQUIDADOR")
                                    and sup_parent in cfg.SUPERVISORES_EQUIPO):
                                supervisor = sup_parent
                                if verbose:
                                    print(f"      INFO {pro_code}: supervisor heredado del padre → {supervisor}")
            # 4) Inferir por líder del proyecto (MAPA_LIDER_SUPERVISOR)
            if not supervisor:
                lider_raw = lider_por_pro.get(pro_id, "")
                mapa_lider = getattr(cfg, "MAPA_LIDER_SUPERVISOR", {})
                for frag, sup_inf in mapa_lider.items():
                    if frag.upper() in lider_raw.upper():
                        supervisor = sup_inf
                        if verbose:
                            print(f"      INFO {pro_code}: supervisor inferido por líder '{lider_raw}' → {supervisor}")
                        break
            if not supervisor:
                if verbose:
                    print(f"      AVISO {pro_code}: sin supervisor resoluble → '?'")
                supervisor = "?"

        # Solo incluir proyectos de supervisores del equipo (o '?' cuando no hay mapeo)
        if supervisor not in cfg.SUPERVISORES_EQUIPO and supervisor != "?":
            if verbose:
                print(f"      SKIP {pro_code}: supervisor '{supervisor}' fuera del equipo")
            continue

        pdl_code = (
            _limpio(dom.get("pdl")) or
            _limpio(dom.get("odm")) or
            _limpio(dom.get("order_number")) or
            _limpio(dom.get("nodo")) or ""
        )
        nombre = dom.get("name") or ""
        if nombre and not pdl_code:
            partes = nombre.split("//")
            if partes:
                pdl_code = partes[0].strip()
        if not pdl_code:
            pdl_code = pro_code
        # Historial completo de PDL del nodo y el vigente (último de la casilla)
        pdl_list   = _split_pdls(pdl_code)
        pdl_ultimo = pdl_list[-1] if pdl_list else pdl_code

        # Construir lista de líderes ordenada por MO descendente
        lideres_dict_pro = lideres_mo_por_pro.get(pro_id, {})
        if not lideres_dict_pro:
            # Fallback: lider del dom
            partner_raw = dom.get("partner_id")
            if isinstance(partner_raw, (list, tuple)) and len(partner_raw) > 1:
                n = str(partner_raw[1]).upper().strip()
            elif isinstance(partner_raw, str):
                n = partner_raw.upper().strip()
            else:
                n = ""
            if n:
                lideres_dict_pro = {n: mo_mes_por_pro.get(pro_id, 0.0)}
        lideres_list = sorted(
            [{"nombre": n, "mo": m} for n, m in lideres_dict_pro.items()],
            key=lambda x: x["mo"], reverse=True,
        )
        lider = lideres_list[0]["nombre"] if lideres_list else lider_por_pro.get(pro_id, "")

        pdl_upper = str(pdl_code or "").upper()
        if pdl_upper.startswith("DF"):
            proceso = "MI"
            lcl = "N/A"
        else:
            proceso = "PG"
            # LCL: buscar 6300... en todos los campos disponibles del proyecto
            lcl_raw = (_limpio(dom.get("odm")) or _limpio(dom.get("order_number")) or "").strip()
            display_name_str = str(dom.get("display_name") or "")
            m_lcl = (re.search(r'6300\d+', lcl_raw) or
                     re.search(r'6300\d+', pdl_code) or
                     re.search(r'6300\d+', nombre) or
                     re.search(r'6300\d+', display_name_str))
            lcl = m_lcl.group(0) if m_lcl else (lcl_raw or "N/A")

        man_obra = mo_mes_por_pro.get(pro_id, 0.0)
        if abs(man_obra) < 1.0:
            continue

        fechas = sorted(fechas_por_pro.get(pro_id, []))
        fecha  = fechas[-1] if fechas else None
        fecha_primera = fechas[0] if fechas else None
        nodo_id = _limpio(dom.get("nodo", ""))

        avances_pro = avances_por_pro[pro_id]
        valores = [float(av.get("advanced_price") or 0) for av in avances_pro]
        tiene_reversiones = any(v < 0 for v in valores)

        # Proyecto padre (para consolidar nodos del mismo DF)
        parent_raw = dom.get("parent_id")
        parent_code = None
        if isinstance(parent_raw, (list, tuple)) and len(parent_raw) > 1:
            parent_code = _extraer_codigo(str(parent_raw[1]))

        # Liquidadores de este proyecto
        liqs_dict = {}
        for av in avances_pro:
            uid_raw = av.get("create_uid")
            if not uid_raw:
                continue
            uid    = uid_raw[0] if isinstance(uid_raw, (list, tuple)) else uid_raw
            nombre_uid = uid_raw[1] if isinstance(uid_raw, (list, tuple)) and len(uid_raw) > 1 else str(uid)
            nombre_uid = " ".join(nombre_uid.split()).upper()
            mo_av = float(av.get("advanced_price") or 0)
            if uid not in liqs_dict:
                liqs_dict[uid] = {"nombre": nombre_uid, "mo": 0.0}
            liqs_dict[uid]["mo"] += mo_av

        liqs_sorted = sorted(liqs_dict.values(), key=lambda x: x["mo"], reverse=True)
        liquidado_por = " / ".join(l["nombre"].split()[0] for l in liqs_sorted)

        proyectos.append({
            "pro_code":          pro_code,
            "pro_id":            pro_id,
            "parent_code":       parent_code,
            "fecha":             fecha,
            "fecha_primera":     fecha_primera,
            "pdl_code":          pdl_code,
            "pdl_list":          pdl_list,
            "pdl_ultimo":        pdl_ultimo,
            "nodo_id":           nodo_id,
            "lider":             lider,
            "lideres_list":      lideres_list,
            "lcl":               lcl,
            "proceso":           proceso,
            "supervisor":        supervisor,
            "man_obra":          man_obra,
            "supervisor_raw":    supervisor_raw,
            "supervisor_manual": bool(getattr(cfg, "LIQUIDADOR_NOMBRE_SGS", "")) and supervisor_raw == cfg.LIQUIDADOR_NOMBRE_SGS,
            "estado":            "parcial" if tiene_reversiones else "ok",
            "fuente_mo":         "advanced_price_mes",
            "n_avances":         n_avances_pro.get(pro_id, 0),
            "tiene_reversiones": tiene_reversiones,
            "liquidadores":      liqs_sorted,
            "liquidado_por":     liquidado_por,
        })

        # ── Detalle individual: una fila por prestación ───────────────────────
        for av in avances_pro:
            f_av = _parse_fecha(av.get("date"))
            uid_raw = av.get("create_uid")
            uid_nombre = ""
            if isinstance(uid_raw, (list, tuple)) and len(uid_raw) > 1:
                uid_nombre = " ".join(str(uid_raw[1]).split()).upper()
            mo_av = float(av.get("advanced_price") or 0)

            detalle_items.append({
                "fecha":       f_av,
                "pro_code":    pro_code,
                "pro_id":      pro_id,
                "pdl_code":    pdl_code,
                "pdl_ultimo":  pdl_ultimo,
                "nombre_pro":  nombre,
                "proceso":     proceso,
                "supervisor":  supervisor,
                "lider":       lider,
                "man_obra":    mo_av,
                "liquidador":  uid_nombre,
                "state":       av.get("state", ""),
                "av_id":       av.get("id"),
            })

    # ── Consolidar nodos del mismo DF o mismo LCL ─────────────────────────────
    # MI  → clave = DF code  (varios PRO-nodos del mismo DF → una fila)
    # PG  → clave = LCL 6300... (varios PRO-nodos del mismo LCL → una fila)
    def _clave_grupo(p):
        if p.get("proceso") == "MI":
            return ("DF",  p.get("pdl_code", p["pro_code"]))
        lcl = p.get("lcl", "N/A")
        if lcl != "N/A" and lcl:
            return ("LCL", lcl)
        return ("PRO", p["pro_code"])   # fallback: sin LCL → fila individual

    _prog(95, "Consolidando nodos por DF/LCL…")
    grupos_pdl: dict = defaultdict(list)
    for p in proyectos:
        grupos_pdl[_clave_grupo(p)].append(p)

    proyectos_merged = []
    for pdl, grupo in grupos_pdl.items():
        # Construir nodos_grupo para análisis PDL (siempre, incluso si es un solo nodo)
        nodos_grupo = [
            {
                "pro_code":     p["pro_code"],
                "nodo_id":      p.get("nodo_id", ""),
                "pdl_code":     p["pdl_code"],
                "pdl_ultimo":   p.get("pdl_ultimo", ""),
                "fecha_primera": p.get("fecha_primera"),
                "fecha_ultima": p.get("fecha"),
                "mo":           p["man_obra"],
                "n_avances":    p.get("n_avances", 0),
                "lider":        p.get("lider", ""),
            }
            for p in grupo
        ]

        if len(grupo) == 1:
            p0 = grupo[0].copy()
            p0["nodos_grupo"] = nodos_grupo
            proyectos_merged.append(p0)
        else:
            base = max(grupo, key=lambda p: abs(p["man_obra"]))
            merged = base.copy()
            merged["man_obra"]  = sum(p["man_obra"] for p in grupo)
            merged["n_avances"] = sum(p["n_avances"] for p in grupo)
            fechas_v = [p["fecha"] for p in grupo if p["fecha"]]
            merged["fecha"] = max(fechas_v) if fechas_v else None
            fechas_primeras = [p.get("fecha_primera") for p in grupo if p.get("fecha_primera")]
            merged["fecha_primera"] = min(fechas_primeras) if fechas_primeras else None
            merged["tiene_reversiones"] = any(p.get("tiene_reversiones") for p in grupo)
            merged["estado"] = "parcial" if merged["tiene_reversiones"] else "ok"
            # PDL: unión del historial de TODOS los nodos (no solo el dominante).
            # Antes la fila heredaba solo la casilla del nodo con más MO y el PDL con
            # el que se liquidó otro nodo desaparecía: nadie lo encontraba al buscar.
            # Vigente = último PDL del nodo liquidado más recientemente.
            grupo_ord = sorted(grupo, key=lambda p: (p.get("fecha") or p.get("fecha_primera") or datetime.min))
            union_pdl: list = []
            for p in grupo_ord:
                for x in (p.get("pdl_list") or _split_pdls(p.get("pdl_code", ""))):
                    if x not in union_pdl:
                        union_pdl.append(x)
            if union_pdl:
                merged["pdl_code"] = ";".join(union_pdl)
            merged["pdl_list"]   = union_pdl
            merged["pdl_ultimo"] = (grupo_ord[-1].get("pdl_ultimo")
                                    or (union_pdl[-1] if union_pdl else merged.get("pdl_code", "")))
            parent_codes = {p.get("parent_code") for p in grupo if p.get("parent_code")}
            if len(parent_codes) == 1:
                merged["pro_code"] = parent_codes.pop()
            # Usar el mejor LCL del grupo (el que empiece con 6300)
            if merged.get("proceso") == "PG":
                best_lcl = next(
                    (p["lcl"] for p in grupo
                     if p.get("lcl", "N/A") not in ("N/A", "")
                     and re.match(r'^6300\d+', str(p["lcl"]))),
                    merged.get("lcl", "N/A"),
                )
                merged["lcl"] = best_lcl
            # Fusionar liquidadores
            liqs_fusionados = {}
            for p in grupo:
                for l in p.get("liquidadores", []):
                    n = l["nombre"]
                    if n not in liqs_fusionados:
                        liqs_fusionados[n] = {"nombre": n, "mo": 0.0}
                    liqs_fusionados[n]["mo"] += l["mo"]
            merged["liquidadores"] = sorted(liqs_fusionados.values(), key=lambda x: x["mo"], reverse=True)
            merged["liquidado_por"] = " / ".join(l["nombre"].split()[0] for l in merged["liquidadores"])
            # Fusionar lideres_list
            lideres_fusionados = {}
            for p in grupo:
                for ll in p.get("lideres_list", []):
                    n = ll["nombre"]
                    lideres_fusionados[n] = lideres_fusionados.get(n, 0.0) + ll["mo"]
            merged["lideres_list"] = sorted(
                [{"nombre": n, "mo": m} for n, m in lideres_fusionados.items()],
                key=lambda x: x["mo"], reverse=True,
            )
            merged["lider"] = merged["lideres_list"][0]["nombre"] if merged["lideres_list"] else merged.get("lider", "")
            merged["nodos_grupo"] = nodos_grupo
            proyectos_merged.append(merged)

    proyectos_merged.sort(key=lambda p: (p["fecha"] or datetime.min))
    detalle_items.sort(key=lambda d: (d["fecha"] or datetime.min, d["pro_code"]))
    _prog(100, f"Listo: {len(proyectos_merged)} proyectos.")

    if verbose:
        total = sum(p["man_obra"] for p in proyectos_merged)
        print(f"\n      {'FECHA':<12}  {'COD SGS':<16}  {'PROC':<4}  {'SUPERVISOR':<14}  {'LIQUIDADO POR':<20}  {'$ MAN OBRA':>18}")
        print(f"      {'─'*100}")
        for p in proyectos_merged:
            f = p["fecha"].strftime("%Y-%m-%d") if p["fecha"] else "sin fecha"
            liqs = ", ".join(f"{l['nombre'].split()[0]} ${l['mo']:,.0f}" for l in p.get("liquidadores", []))
            print(f"      {f:<12}  {p['pro_code']:<16}  {p['proceso']:<4}  "
                  f"{p['supervisor']:<14}  {liqs[:20]:<20}  ${p['man_obra']:>17,.2f}")
        print(f"      {'─'*100}")
        print(f"      TOTAL: ${total:,.2f}  |  Proyectos: {len(proyectos_merged)}  |  Prestaciones: {len(detalle_items)}")

    return {"consolidado": proyectos_merged, "detalle": detalle_items}


if __name__ == "__main__":
    print("\n" + "="*60)
    print("  LECTOR DE PRESTACIONES — BALANCE FINAL SGS")
    print("="*60)
    resultado = leer_proyectos_mes(verbose=True)
    if not resultado["consolidado"]:
        print("\n  Sin datos. Verifica la conexión o el mes consultado.")
