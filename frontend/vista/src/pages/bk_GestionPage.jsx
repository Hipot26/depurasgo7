import { Fragment, useEffect, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import toast from 'react-hot-toast'
import api from '../utils/api'
import { useAuthStore } from '../store/authStore'
import { Workflow, RefreshCw, Search, X, Mail, Database, Boxes, Activity, AlertTriangle, ExternalLink, ChevronRight, Info, Pencil, Check, Banknote, FileText, Receipt, Copy, Download, ShieldCheck, HelpCircle, Undo2 } from 'lucide-react'

// Gestión General (2026-10-22): un caso por proyecto (LCL, DF o PDL) con el flujo FL-PS-E-03 de Dominion
// (39 pasos en 3 pilares) como columna vertebral. El paso actual, el pilar y las alertas los calcula el
// backend a partir de cuatro fuentes: correo (agente del PC, interpretado con las reglas del Centro MO), SGS
// (lectura en vivo por el Centro MO: MO liquidada por familia y cada liquidación), Sonny
// (Balance Integrado + Sonny Bot) y los Registros de SGO7. Aquí el supervisor ve en qué va cada proyecto.

const TZ = 'America/Bogota'
// Una fecha sin zona (la franja del dinero y los cortes de los exportes vienen en hora Colombia, «2026-10-17» o «2026-07-23T19:39:45»)
// se lee como hora Colombia (sin horario de verano: −05:00), no como UTC ni como la hora del navegador.
function aFecha(iso) {
  const s = String(iso)
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return new Date(`${s}T12:00:00-05:00`)
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s)) return new Date(`${s}-05:00`)
  return new Date(s)
}
function fechaHora(iso) {
  if (!iso) return '—'
  try { return aFecha(iso).toLocaleString('es-CO', { timeZone: TZ, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) } catch { return iso }
}
function fecha(iso) {
  if (!iso) return '—'
  try { return aFecha(iso).toLocaleDateString('es-CO', { timeZone: TZ, day: '2-digit', month: 'short', year: '2-digit' }) } catch { return iso }
}
function hace(iso) {
  if (!iso) return ''
  const d = (Date.now() - new Date(iso).getTime()) / 86400000
  if (d < 1) return 'hoy'
  if (d < 2) return 'ayer'
  if (d < 30) return `hace ${Math.floor(d)} d`
  return `hace ${Math.floor(d / 30)} mes${d >= 60 ? 'es' : ''}`
}
const PILAR = {
  1: { label: 'Levantamiento y aprobación Enel', corto: 'Levantamiento', bar: 'bg-sky-500', soft: 'bg-sky-200', text: 'text-sky-800', chip: 'bg-sky-100 text-sky-800' },
  2: { label: 'Ejecución', corto: 'Ejecución', bar: 'bg-emerald-500', soft: 'bg-emerald-200', text: 'text-emerald-800', chip: 'bg-emerald-100 text-emerald-800' },
  3: { label: 'Facturación', corto: 'Facturación', bar: 'bg-violet-500', soft: 'bg-violet-200', text: 'text-violet-800', chip: 'bg-violet-100 text-violet-800' },
}
const FUENTE = {
  correo: { label: 'Correo', cls: 'bg-blue-100 text-blue-800', Icon: Mail },
  sgs:    { label: 'SGS', cls: 'bg-emerald-100 text-emerald-800', Icon: Database },
  sonny:  { label: 'Sonny', cls: 'bg-violet-100 text-violet-800', Icon: Boxes },
  sgo7:   { label: 'SGO7', cls: 'bg-gray-200 text-gray-700', Icon: Activity },
  pms:    { label: 'PMS', cls: 'bg-amber-100 text-amber-800', Icon: FileText },
  scm:    { label: 'SCM', cls: 'bg-cyan-100 text-cyan-800', Icon: Receipt },
}
const ALERTA = {
  sin_liquidar: 'Sin liquidar', sonny_pendiente: 'Almacén en Sonny', rechazo_abierto: 'Rechazo abierto', plan_sin_validar: 'Plan sin validar',
  cms_sin_aprobar: 'CMS sin aprobar', ejecutado_sin_planilla: 'Ejecutado sin planilla', sin_actividad: 'Sin actividad',
  // (CHANGELOG 2026-10-23) el hilo del dinero
  aprobado_sin_libreto: 'Planilla aprobada sin libreto', libreto_devuelto: 'Libreto devuelto', libreto_en_enel: 'Libreto en Enel',
  liquidado_sin_cobro: 'Liquidado sin planilla ni libreto', mat_cobrado_sin_consumir: 'Material SCM > SGS',
  // (CHANGELOG 2026-10-23) SAP (MB51): todas de material
  scm_sin_sap: 'SCM sin descarga en SAP', sap_mas_que_scm: 'SAP descargó más que SCM', sgs_sin_sap: 'SGS sin descarga en SAP',
  // (CHANGELOG 2026-10-25) material por liquidar y proyección contra PMS (material; la de por liquidar es informativa, en ámbar)
  mat_excede_pms: 'Material excede PMS', mat_por_liquidar: 'Material por liquidar',
  // (CHANGELOG 2026-10-25) Sonny por etapa
  nodo_sin_planilla: 'Nodo sin planilla', planilla_en_curso_quieta: 'Planilla en curso quieta', planilla_en_firmas: 'Planilla en firmas',
  pms_finalizado_sin_planilla: 'PMS finalizado sin planilla',
  // (CHANGELOG 2026-10-25) el cierre en SCM (LCL CONCLUSA)
  lcl_concluida_libreto_abierto: 'LCL concluida con libreto abierto', lcl_sin_libreto_vence: 'LCL sin libreto vence', importe_lcl_vs_mo: 'Importe LCL ≠ MO libretos',
  // (CHANGELOG 2026-10-25) el cobro en DRAPE (portal de facturación del cliente)
  lcl_concluida_sin_factura: 'LCL concluida sin factura', factura_vencida_sin_pago: 'Factura vencida sin pago', factura_rechazada: 'Factura rechazada',
  em_sin_facturar: 'EM sin facturar',
}
// (CHANGELOG 2026-10-25) el estado del cobro del caso en DRAPE (por LCL: «Num. Pedido» de la factura)
const COBRO = {
  sin_factura: { t: 'sin factura', cls: 'text-gray-500', chip: 'bg-gray-100 text-gray-600' },
  borrador: { t: 'factura en borrador', cls: 'text-gray-500', chip: 'bg-gray-100 text-gray-600' },
  rechazada: { t: 'factura rechazada', cls: 'text-red-700', chip: 'bg-red-100 text-red-700' },
  enviada: { t: 'enviada a Enel', cls: 'text-sky-800', chip: 'bg-sky-50 text-sky-800' },
  facturado: { t: 'facturado', cls: 'text-violet-800', chip: 'bg-violet-100 text-violet-800' },
  pago_parcial: { t: 'pago parcial', cls: 'text-emerald-800', chip: 'bg-emerald-50 text-emerald-800' },
  pagado: { t: 'pagado', cls: 'text-emerald-700', chip: 'bg-emerald-100 text-emerald-800' },
}
// (CHANGELOG 2026-10-25) cómo queda el material proyectado de SGS (liquidado + por liquidar) contra lo presupuestado en PMS
const PROY = {
  cuadra: { t: 'Cuadra', cls: 'text-green-700', chip: 'bg-green-50 text-green-700' },
  excede: { t: 'Excede', cls: 'text-red-700', chip: 'bg-red-100 text-red-700' },
  por_debajo: { t: 'Por debajo', cls: 'text-gray-500', chip: 'bg-gray-100 text-gray-600' },
  sin_material_pms: { t: 'PMS sin material', cls: 'text-gray-400', chip: 'bg-gray-50 text-gray-500' },
  sin_pms: { t: 'sin presupuesto PMS', cls: 'text-gray-400' },
  sin_sgs: { t: 'SGS sin leer', cls: 'text-gray-400' },
  sin_bandejas: { t: 'sin bandejas de SGS', cls: 'text-gray-400' },
}
function pctTxt(p) { return p === null || p === undefined ? '' : `${Math.abs(Number(p)).toLocaleString('es-CO', { maximumFractionDigits: 1 })} %` }
function proyTxt(pr) {      // «excede 12 %» · «cuadra (+3 %)» · «por debajo (−20 %)»
  if (!pr || !PROY[pr.estado]) return ''
  if (pr.estado === 'excede') return `excede ${pctTxt(pr.pct)}`
  if (pr.estado === 'cuadra' || pr.estado === 'por_debajo') return `${PROY[pr.estado].t.toLowerCase()} (${signo(pr.pct)}${pctTxt(pr.pct)})`
  return PROY[pr.estado].t
}
const SAP_ESTADO = { confirmada: 'confirmada por SAP', no_ve: 'SAP aún no lo ve', parcial: 'SAP confirma una parte' }
const ORIGEN = { correo: 'Correo', sonny: 'Sonny', pms: 'PMS', scm: 'SCM' }
const TIPO = { programado: 'Programado', mayor_impacto: 'Mayor impacto', linea_viva: 'Línea viva' }
const ACTOR = { PA: 'Prof. de apoyo', ST: 'Supervisor', Enel: 'Enel', F: 'Facturación', P: 'Programación', L: 'Almacén', GT: 'Cuadrilla', AUXSGS: 'Auxiliar SGS', C: 'Cumplimentador', COORD: 'Coordinador', CMS: 'CMS', SONNY: 'Sonny Bot', SGS: 'SGS', SGO7: 'SGO7', CORES: 'Centro de control' }
const pilarDe = (n) => (!n ? 0 : n <= 7 ? 1 : n <= 22 ? 2 : 3)
function millones(v) {
  const n = Number(v || 0)
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toLocaleString('es-CO', { maximumFractionDigits: 1 })} M`
  return `$${Math.round(n).toLocaleString('es-CO')}`
}

// ── (CHANGELOG 2026-10-23) La franja del dinero: PMS → SGS → Sonny → SCM → SAP ──────────────────────────────────────────
// La MO de SGS se muestra en gris, sin color ni alerta: la valida el liquidador y aquí solo se muestra. El material se valora al costo de SGS (provisional).
// (CHANGELOG 2026-10-25) la última caja es «Facturado (DRAPE)» (el cobro por LCL en el portal de facturación del cliente; el material del MB51 sigue en ella); es
// punteada solo mientras no haya datos de DRAPE cargados. Rojo para el material y para la factura vencida sin pago.
const DELTA_CLS = { verde: 'text-green-700', ambar: 'text-amber-700', gris: 'text-gray-400' }
function signo(n) { return n > 0 ? '+' : n < 0 ? '−' : '' }
function deltaTxt(x) {
  if (!x) return null
  const v = Math.abs(Number(x.valor || 0))
  const pct = x.pct === null || x.pct === undefined ? '' : ` · ${signo(x.pct)}${Math.abs(x.pct).toLocaleString('es-CO', { maximumFractionDigits: 1 })} %`
  return `${signo(x.valor)}${millones(v)}${pct}`
}
function deltaCorto(x) {      // en la flecha entre cajas: el porcentaje (el valor en pesos sale al pasar el ratón)
  if (!x) return null
  if (x.pct === null || x.pct === undefined) return `${signo(x.valor)}${millones(Math.abs(Number(x.valor || 0)))}`
  return `${signo(x.pct)}${Math.abs(x.pct).toLocaleString('es-CO', { maximumFractionDigits: 1 })} %`
}
// (CHANGELOG 2026-10-23) cómo se valoró el material de una caja: precio de Enel (SAP) donde el MB51 trae el código; si no, costo de SGS (provisional)
function precioTxt(c) {
  if (c.mat === null || c.mat === undefined) return null
  if (c.mat_precio === 'sap') return <span className="text-emerald-700" title="A precio de Enel (SAP)"> Enel</span>
  if (c.mat_precio === 'mixto') return <span className="text-gray-400" title={`${millones(c.mat_sap)} a precio de Enel (SAP); el resto a costo de SGS (provisional)`}> Enel+prov.</span>
  return c.mat_provisional ? <span className="text-gray-400" title="A costo de SGS (provisional)"> prov.</span> : null
}
function diaMes(iso) {     // «09/10»: las fechas de SAP son días (hora Colombia); es-CO en Chromium escribe «9/10» aunque se pida 2 dígitos
  if (!iso) return ''
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (m) return `${m[3]}/${m[2]}`
  try { const p = Object.fromEntries(new Intl.DateTimeFormat('es-CO', { timeZone: TZ, day: '2-digit', month: '2-digit' }).formatToParts(aFecha(iso)).map((x) => [x.type, x.value])); return `${String(p.day).padStart(2, '0')}/${String(p.month).padStart(2, '0')}` } catch { return iso }
}
// (CHANGELOG 2026-10-25) el estado de las LCL del caso en el exporte de LCL de SCM. CONCLUSA = concluida según SCM (SCM no fecha el cambio: la fecha es la del
// exporte en que ya figuraba así); si eso equivale a pagado está por confirmar con FBL1N.
function ChipLcl({ l }) {
  if (!l?.estado) return null
  const concl = l.estado === 'concluida'
  const ab = [...new Set((l.abiertas || []).map((x) => x.estado || 'sin fila en SCM'))]
  return <span data-chip="scm-lcl" data-estado={l.estado} title={concl ? `Todas las LCL del caso (${l.n}) figuran CONCLUSA en el exporte de LCL de SCM: concluida según SCM. SCM no fecha el cambio de estado: es el exporte en que ya figuraba así. Si eso equivale a pagado está por confirmar con FBL1N.` : `Estado de la LCL en el exporte de LCL de SCM${(l.abiertas || []).length ? ': ' + l.abiertas.map((x) => `${x.lcl} ${x.estado || 'sin fila'}`).join(' · ') : ''}`}
    className={`inline-flex items-center text-[10px] px-1.5 py-0.5 rounded border whitespace-nowrap ${concl ? 'bg-emerald-50 text-emerald-800 border-emerald-200' : 'bg-cyan-50 text-cyan-800 border-cyan-200'}`}>
    {concl ? `LCL concluida ${diaMes(l.cerrado_en || l.corte)}` : `LCL abierta · ${ab.join(', ')}`}</span>
}
const NOMBRE_LIB = { INV: 'Enviado', RIF: 'Rechazado', LAV: 'En Elaboración', CHI: 'Cerrado (sin enviar a Enel)', SAP: 'SAP', ACQ: 'Adquirido', ANN: 'Anulado', BOC: 'Borrador Consuntivo', CHC: 'Cerrado Consuntivo', RIL: 'Lanzado', BOZ: 'Borrador', BOZZA: 'Borrador' }
function ChipSap({ corte }) {
  if (!corte) return null
  return <span data-chip="sap-corte" title="Última fecha de consumo (clase 261) del MB51 cargado: lo autocertificado después aún no está en SAP (no es un 0)" className="inline-flex items-center text-[10px] px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-800 border border-indigo-200 whitespace-nowrap">SAP al {diaMes(corte)}</span>
}
// (CHANGELOG 2026-10-25) la caja «Facturado (DRAPE)»: el estado del cobro de las LCL del caso en el portal de facturación del cliente (factura, registro, vencimiento,
// pago) y el importe de la LCL por las posiciones de cada factura; sin posiciones leídas no se reparte («en DCxxxx (N LCL, total $)»).
// (CHANGELOG 2026-10-25) con el portal oculto, la caja es la facturación del informe de deuda de facturación de Dominion
function DeudaCaja({ c }) {
  if (!c.hay) return <div className="text-xs text-gray-400 mt-1" data-deuda="sin-dato">sin dato en el informe de deuda</div>
  const g = c.deuda?.por_grupo || {}
  return (
    <div data-deuda="si">
      <div className="text-[13px] font-bold leading-tight mt-0.5 whitespace-nowrap text-gray-800" data-deuda-mo={Math.round(Number(c.mo || 0))}>En fact. {millones(c.mo)}</div>
      <div className="text-[10px] text-gray-500 leading-tight">informe {diaMes(c.deuda?.corte)} · en conciliación {millones(g.conciliacion?.mo)}</div>
    </div>
  )
}
function CobroCaja({ c }) {
  if (c.cobro_oculto) return <DeudaCaja c={c} />
  const co = c.cobro
  if (!co?.hay) return <div className="text-xs text-gray-400 mt-1" data-cobro="sin-dato">factura: sin datos de DRAPE</div>
  const e = COBRO[co.estado] || { t: co.estado || '—', cls: 'text-gray-600' }
  const u = co.ultima
  const sp = (co.sin_pos || [])[0]
  const cifra = co.estado === 'pagado' ? co.pagado : co.estado === 'enviada' ? co.enviado : co.facturado
  const rot = co.estado === 'pagado' ? 'Pagado' : co.estado === 'enviada' ? 'Env.' : 'Fact.'
  const vence = co.proximo_vence
  return (
    <div data-cobro={co.estado} data-vencida={co.vencida ? 'si' : 'no'}>
      {co.n > 0 && Number(cifra || 0) >= 1
        ? <div className={`text-[13px] font-bold leading-tight mt-0.5 whitespace-nowrap ${co.vencida ? 'text-red-700' : e.cls}`} data-cobro-importe={Math.round(Number(cifra || 0))}>{rot} {millones(cifra)}</div>
        : <div className={`text-xs font-semibold mt-1 ${e.cls}`}>{e.t}</div>}
      {co.n > 0 && <div className="text-[11px] text-gray-700 leading-tight" data-cobro-factura>{e.t}{u ? ` · ${u.factura} ${diaMes(u.f_factura)}` : ''}{co.n > 1 ? ` (+${co.n - 1})` : ''}{u?.f_registro ? ` · reg. ${diaMes(u.f_registro)}` : ''}</div>}
      {sp && <div className="text-[10px] text-gray-500 leading-tight" data-cobro-sin-pos title="Sin el detalle de posiciones el importe de la LCL no se reparte">en {sp.factura} ({sp.n_lcl} LCL, {millones(sp.total)}){(co.sin_pos || []).length > 1 ? ` +${co.sin_pos.length - 1}` : ''}</div>}
      {co.vencida ? <div className="text-[10px] text-red-700 font-semibold leading-tight" data-cobro-vencida>vencida · {co.n_vencidas} fact. · {co.dias_atraso} d háb. de atraso</div>
        : co.pago?.fecha ? <div className="text-[10px] text-emerald-800 leading-tight">pagado {diaMes(co.pago.fecha)}{co.pago.docs?.length ? ` · doc. ${co.pago.docs[0]}` : ''}</div>
        : vence ? <div className="text-[10px] text-gray-500 leading-tight" title={vence.exacta ? 'Vencimiento del detalle de la factura' : `Estimado: fecha de factura + ${co.vence_dias} días (no se leyó el detalle)`}>vence {vence.exacta ? '' : '~'}{diaMes(vence.f_vence)}</div> : null}
      {co.n_em > 0 && <div className="text-[10px] text-gray-400 leading-tight" title="EM (HES) de sus facturas: son de la factura entera, DRAPE no las reparte por LCL">{co.n_em} EM</div>}
    </div>
  )
}
function Caja({ c, activa, onClick, onEvidencia, malas }) {
  const sap = c.k === 'sap'
  const sinCobro = sap && (c.cobro_oculto ? !c.hay : !c.cobro?.hay)
  const base = sinCobro ? 'border-dashed border-gray-300 bg-gray-50' : sap ? (c.cobro?.vencida ? 'border-red-300 bg-white' : c.hay ? 'border-gray-200 bg-white' : 'border-gray-200 bg-gray-50') : c.hay ? 'border-gray-200 bg-white' : 'border-gray-200 bg-gray-50'
  const mal = (CAJA_TARJETA[c.k] || []).some((t) => (malas || []).includes(t))
  return (
    <div className="flex-1 min-w-[92px] flex flex-col">
    <button type="button" onClick={onClick} data-caja={c.k} title={sap ? (c.cobro_oculto ? (c.fuente || '') : c.cobro?.hay ? 'Fuente: DRAPE (portal de facturación del cliente). La llave es la LCL («Num. Pedido»); la orden de SAP no aparece en DRAPE.' : `Factura: sin datos de DRAPE. Faltan los datos de DRAPE (o ${c.falta || 'el documento de SAP'})`) : 'Ver el detalle y filtrar la línea de tiempo por esta fuente'}
      className={`text-left w-full flex-1 rounded-lg border px-2 py-1.5 ${base} ${mal ? 'border-red-400' : ''} ${activa ? 'ring-2 ring-blue-500' : 'hover:border-blue-300'}`}>
      <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">{c.titulo}</div>
      <div className="text-[10px] text-gray-400">{c.sistema}</div>
      {sap ? <>
          <CobroCaja c={c} />
          {c.mat_hay && <div className="text-[11px] text-gray-600 leading-tight mt-0.5" data-sap-mat>{c.ve_lcl ? <>MAT desc. {millones(c.mat)}<span className="text-emerald-700"> Enel</span></> : <span className="text-gray-400">MAT: SAP no lo ve</span>}</div>}
          {c.mat_hay && <div className="mt-0.5"><ChipSap corte={c.corte_mb51} /></div>}
          {c.scm_lcl === 'concluida' && <div className="text-[10px] text-gray-500 leading-tight mt-0.5" data-sap-scm title={c.nota_scm || ''}>SCM: LCL concluida{c.cobro?.hay ? '' : ' · ¿pagado? por confirmar'}</div>}
          {mal && <div className="text-[10px] text-red-700 font-semibold leading-tight mt-0.5">la suma no reproduce la cifra</div>}
        </>
        : !c.hay ? <><div className="text-xs text-gray-400 mt-1">no llegó</div>{c.k === 'scm' && c.lcl?.estado && <div className="mt-0.5"><ChipLcl l={c.lcl} /></div>}</>
        : <>
            <div className={`text-[13px] font-bold leading-tight mt-0.5 whitespace-nowrap ${c.mo_gris ? 'text-gray-500' : 'text-gray-800'}`}>MO {millones(c.mo)}</div>
            {c.k === 'sgs' && c.mat_bandejas ? (
              <div className="text-[11px] text-gray-600 leading-tight" data-sgs-mat="tres">
                <div title="Consumido en SGS (liquidado)">MAT liq. {millones(c.mat)}{precioTxt(c)}</div>
                <div className={Number(c.mat_por_liquidar || 0) >= 1 ? 'text-amber-800' : 'text-gray-400'} title="Despachado − devuelto − consumido (bandejas aprobadas de SGS)" data-sgs-mat="por-liquidar">por liq. {millones(c.mat_por_liquidar)}</div>
                <div className="font-semibold" title="Liquidado + por liquidar" data-sgs-mat="proyectado">proy. {millones(c.mat_proyectado)}</div>
              </div>
            ) : (
            <div className="text-[11px] text-gray-600 leading-tight">
              MAT {c.mat === null || c.mat === undefined ? (c.mat_codigos ? `${c.mat_codigos} cód.` : '—') : millones(c.mat)}
              {precioTxt(c)}
              {c.k === 'scm' && c.mat_codigos && c.mat !== null && c.mat !== undefined ? <span className="text-gray-400"> · {c.mat_codigos} cód.</span> : null}
            </div>)}
            {c.k === 'scm' && c.lcl?.estado && <div className="mt-0.5"><ChipLcl l={c.lcl} /></div>}
            {c.k === 'sonny' && c.mo_plan !== null && c.mo_plan !== undefined && (
              <div className="text-[10px] text-gray-500 leading-tight" data-sonny-plan title={`Valor_mo_plan de las planillas (la valoración de PMS que trae Sonny): se muestra al lado del real y no lo reemplaza${c.plan_atipicas?.length ? ` · sin los planes atípicos de ${c.plan_atipicas.join(', ')}` : ''}`}>plan MO {millones(c.mo_plan)}{c.plan_atipicas?.length ? '*' : ''}</div>)}
            {c.k === 'pms' && c.proyeccion && PROY[c.proyeccion.estado] && c.proyeccion.proyectado !== null && c.proyeccion.proyectado !== undefined && (
              <div className="text-[10px] leading-tight mt-0.5" data-pms-proy={c.proyeccion.estado} title={`Material proyectado de SGS (liquidado + por liquidar) contra el presupuestado en PMS: cuadra con ±${c.proyeccion.umbral} %`}>
                <span className="text-gray-500">proy. {millones(c.proyeccion.proyectado)} · </span><span className={`font-semibold ${PROY[c.proyeccion.estado].cls}`}>{proyTxt(c.proyeccion)}</span>
              </div>)}
            <div className="text-[10px] text-gray-400 mt-0.5">{c.fecha ? `últ. ${fecha(c.fecha)}` : ''}</div>
            {mal && <div className="text-[10px] text-red-700 font-semibold leading-tight mt-0.5">la suma no reproduce la cifra</div>}
          </>}
    </button>
    {onEvidencia && <button type="button" data-evidencia={c.k} onClick={onEvidencia} className="mt-0.5 text-[10px] text-blue-700 hover:underline text-left inline-flex items-center gap-0.5"><HelpCircle size={10} />¿De dónde sale?</button>}
    </div>
  )
}
function Flecha({ d }) {
  return (
    <div className="flex flex-col items-center justify-center w-[46px] shrink-0 text-[10px] leading-tight text-center">
      <ChevronRight size={14} className="text-gray-300" />
      {d?.mo && <span className={DELTA_CLS[d.mo.color] || 'text-gray-400'} title={`MO ${deltaTxt(d.mo)}${d.base_lcl ? ' · contra el LCL completo (con el otro padre de SGS)' : ''}`}>{deltaCorto(d.mo)}</span>}
      {d?.mat && <span className="text-gray-400" title={`MAT ${deltaTxt(d.mat)} (a precio de Enel donde SAP lo trae; si no, costo SGS provisional)`}>MAT {deltaCorto(d.mat)}</span>}
    </div>
  )
}
// (CHANGELOG 2026-10-25) el detalle del cobro en DRAPE: cada factura de las LCL del caso con su estado, registro, vencimiento y pago, y lo de esta LCL por posiciones
function CobroDetalle({ co, onEvidencia, dr }) {
  const fs = co.facturas || []
  const cg = dr?.cargas?.[0]
  return (
    <div className="space-y-1" data-cobro-detalle>
      <div><b>Cobro en DRAPE</b> (portal de facturación del cliente{co.corte ? `, corte ${fechaHora(co.corte)}` : ''}): <span className={(COBRO[co.estado] || {}).cls}>{(COBRO[co.estado] || {}).t || co.estado}</span>
        {co.vencida ? <span className="text-red-700 font-semibold"> · {co.n_vencidas} factura(s) vencida(s) sin pago, {co.dias_atraso} días hábiles de atraso</span> : null}</div>
      {fs.length > 0 ? (
        <table className="w-full text-[11px]" data-cobro-tabla>
          <thead className="text-gray-500 text-left"><tr><th className="px-1">Factura</th><th className="px-1">Estado</th><th className="px-1">Fecha</th><th className="px-1">Registro</th><th className="px-1">Vence</th><th className="px-1">Pago</th><th className="px-1 text-right">De esta LCL</th><th className="px-1 text-right">LCL · EM</th></tr></thead>
          <tbody className="divide-y divide-gray-100">
            {fs.map((x) => (
              <tr key={x.drape_id} data-cobro-fila={x.factura} data-estado={x.estado} className={x.vencida ? 'bg-red-50' : x.estado === 'rechazada' ? 'text-gray-400' : ''}>
                <td className="px-1 py-0.5 font-mono"><button type="button" className="hover:text-blue-700" title={`DRAPE ID ${x.drape_id}: copiar`} onClick={() => copiar(x.factura)}>{x.factura}</button></td>
                <td className="px-1 py-0.5" title={x.que}>{x.que.split(' (')[0]}</td>
                <td className="px-1 py-0.5 whitespace-nowrap">{fecha(x.f_factura)}</td>
                <td className="px-1 py-0.5 whitespace-nowrap">{x.n_registro ? `${x.n_registro} · ${fecha(x.f_registro)}` : '—'}</td>
                <td className={`px-1 py-0.5 whitespace-nowrap ${x.vencida ? 'text-red-700 font-semibold' : ''}`} title={x.vence_exacta ? 'Del detalle de la factura' : `Estimado: fecha de factura + ${co.vence_dias} días`}>{x.f_vence ? `${x.vence_exacta ? '' : '~'}${fecha(x.f_vence)}` : '—'}{x.vencida ? ` (+${x.dias_atraso} d háb.)` : ''}</td>
                <td className="px-1 py-0.5 whitespace-nowrap">{x.f_pago ? `${fecha(x.f_pago)}${x.doc_pago ? ` · ${x.doc_pago}` : ''}` : '—'}</td>
                <td className="px-1 py-0.5 text-right whitespace-nowrap">{x.completo && x.importe !== null && x.importe !== undefined ? millones(x.importe) : <span className="text-gray-500" title="Sin el detalle de posiciones no se reparte">en la factura ({x.n_lcl} LCL, {millones(x.total)})</span>}</td>
                <td className="px-1 py-0.5 text-right whitespace-nowrap" title={(x.ems || []).slice(0, 40).join(', ')}>{x.n_lcl} · {x.n_em}</td>
              </tr>))}
          </tbody>
        </table>
      ) : <div className="text-gray-500">DRAPE no tiene ninguna factura con {co.lcls?.length > 1 ? 'las LCL' : 'la LCL'} del caso ({(co.lcls || []).join(', ')}).</div>}
      {co.n > 0 && <div data-cobro-sumas>Facturado {millones(co.facturado)} (registradas + pagadas, por posiciones) · pagado {millones(co.pagado)} · pendiente {millones(co.pendiente)}{Number(co.enviado || 0) >= 1 ? ` · enviado a Enel ${millones(co.enviado)}` : ''}{Number(co.vencido || 0) >= 1 ? <span className="text-red-700"> · vencido {millones(co.vencido)}</span> : null}
        {onEvidencia && <button type="button" onClick={() => onEvidencia('drape')} className="ml-1 text-blue-700 underline" data-evidencia-fila="drape">¿De dónde sale?</button>}</div>}
      {co.n_em > 0 && <div className="text-gray-600">{co.n_em} EM (HES) en sus facturas: {(co.ems || []).slice(0, 8).join(', ')}{co.n_em > 8 ? '…' : ''} · son de la factura entera (DRAPE no las reparte por LCL).</div>}
      <div className="text-[11px] text-gray-500">Fuente: DRAPE (portal de facturación del cliente){cg ? ` · ${cg.archivo} · cargado ${fechaHora(cg.cargado)}` : ''}. La llave es la LCL («Num. Pedido»); la orden de SAP y el libreto no aparecen en DRAPE. Vencimiento del detalle de la factura o, si no se leyó, estimado (~): fecha de factura + {co.vence_dias} días. Sin el detalle de posiciones no se reparte el importe por LCL.</div>
    </div>
  )
}
// (CHANGELOG 2026-10-23) la caja muestra la tarjeta de fuentes donde consta el MB51 cargado (solo material); (CHANGELOG 2026-10-25) arriba, el cobro en DRAPE
function DetalleSap({ c, d, onEvidencia }) {
  const { data: fu } = useQuery({ queryKey: ['gestion-fuentes'], queryFn: () => api.get('/gestion/fuentes').then((r) => r.data), staleTime: 120000 })
  const s = (fu?.sistemas || []).find((x) => x.k === 'sap')
  const dr = (fu?.sistemas || []).find((x) => x.k === 'drape')
  const cg = s?.cargas?.[0]
  const co = d?.cobro || c.cobro
  return (
    <div className="text-xs text-gray-700 space-y-1.5" data-detalle="sap">
      {co?.hay ? <CobroDetalle co={co} onEvidencia={onEvidencia} dr={dr} />
        : c.cobro_oculto ? <div data-deuda-detalle><b>En facturación según el informe de deuda de facturación de Dominion{c.deuda?.corte ? ` (${fecha(c.deuda.corte)})` : ''}:</b>{' '}
            {c.hay ? <>{millones(c.mo)} en liberación de Enel o lista para pago · {millones(c.deuda?.por_grupo?.conciliacion?.mo)} en conciliación ({c.deuda?.n} filas del informe).
              {onEvidencia && <button type="button" onClick={() => onEvidencia('deuda')} className="ml-1 text-blue-700 underline" data-evidencia-fila="deuda">¿De dónde sale?</button>}</> : 'sin dato en el informe de deuda.'}
            {' '}El informe no trae facturas ni pagos: el pago queda «fuente por definir».</div>
        : <div><b>Factura: sin datos de DRAPE.</b> Faltan los datos de DRAPE (portal de facturación del cliente: factura, registro y pago por LCL); en SAP faltaría {c.falta || 'FBL1N (partidas de Dominion como proveedor), HES (ESSR / ME2S) e historial de pedido (EKBE)'}. Sin eso no se afirma «cobrado».</div>}
      {c.nota_scm && <div className="text-gray-600" data-nota-scm>{c.scm_lcl === 'concluida' ? <b>La LCL figura CONCLUSA en SCM. </b> : null}{co?.hay ? 'CONCLUSA no quiere decir pagado: el cobro es el de DRAPE, arriba.' : c.nota_scm}</div>}
      <div className="rounded border border-indigo-200 bg-indigo-50/60 px-2 py-1.5" data-fuente-sap>
        <div className="text-[10px] font-semibold uppercase tracking-wide text-indigo-800">Fuentes · SAP (MB51)</div>
        {cg ? <div className="flex flex-wrap gap-x-2 items-baseline">
            <span className="font-mono text-gray-800">{cg.archivo}</span>
            <span>exporte {fechaHora(cg.exporte)}</span>
            <span>cargado {fechaHora(cg.cargado)}{cg.usuario ? ` por ${cg.usuario}` : ''}</span>
            <span>{Number(cg.filas || 0).toLocaleString('es-CO')} filas</span>
            <button type="button" className="font-mono text-gray-400 hover:text-blue-700" title="Huella sha256 del archivo: copiar" onClick={() => copiar(cg.sha256)}>sha256 {String(cg.sha256 || '').slice(0, 12)}…</button>
          </div> : <div className="text-amber-700">{s?.sin_registro || 'No hay un MB51 cargado.'}</div>}
        {s?.nota && <div className="text-gray-600 mt-0.5">{s.nota}</div>}
      </div>
      {c.mat_hay && <div>Material descargado contra este {c.ve_lcl ? `código: ${millones(c.mat)} a precio de Enel en ${c.n_filas} fila(s), la última el ${fecha(c.fecha)}` : 'código: SAP no tiene descargas (no se lee como 0)'}.
        {onEvidencia && <button type="button" onClick={() => onEvidencia('sap_mat')} className="ml-1 text-blue-700 underline">¿De dónde sale?</button>}</div>}
    </div>
  )
}
function DetalleCaja({ c, d, onEvidencia }) {
  if (!c) return null
  if (c.k === 'sap') return <DetalleSap c={c} d={d} onEvidencia={onEvidencia} />
  if (!c.hay) return <div className="text-xs text-gray-500">{c.sistema} no trae nada para este caso en el último exporte cargado.</div>
  if (c.k === 'pms') return (
    <div className="text-xs space-y-1">
      <ul className="space-y-0.5 font-mono">{(c.detalle || []).map((x) => <li key={x.request_code}>{x.request_code} · {x.status} · MO {millones(x.services)} · MAT {millones(x.materials)} · {fecha(x.fecha_estado)}{x.compartida?.length ? <span className="text-gray-400"> · también nombra {x.compartida.join(', ')}</span> : null}</li>)}</ul>
      <Proyeccion pr={d.proyeccion} onEvidencia={onEvidencia} />
    </div>
  )
  if (c.k === 'sgs') return (
    <div className="text-xs text-gray-600 space-y-0.5">
      <div>{(c.codes || []).join(' + ')} · {c.n_lotes} liquidaciones · MO <span className="text-gray-500">{millones(c.mo)}</span> (la valida el liquidador; aquí solo se muestra)</div>
      <div>Material de Enel · {c.mat_precio === 'sap' ? 'a precio de Enel (SAP)' : c.mat_precio === 'mixto' ? `${millones(c.mat_sap)} a precio de Enel (SAP) y el resto a costo de SGS (provisional): SAP no trae esos códigos` : 'a costo de SGS (provisional): SAP no trae precio para estos códigos'}</div>
      {c.mat_bandejas ? (
        <div data-detalle="sgs-mat">
          <b>Liquidado</b> {millones(c.mat)} + <b>Por liquidar</b> <span className="text-amber-800">{millones(c.mat_por_liquidar)}</span> ({c.mat_n_por_liquidar} código(s): salió del almacén y aún no está consumido) = <b>Proyectado</b> {millones(c.mat_proyectado)}
          {Number(c.mat_sobre || 0) >= 1 && <span className="text-gray-500"> · consumido de más que lo despachado, aparte: {millones(c.mat_sobre)} en {c.mat_n_sobre} código(s)</span>}
          {onEvidencia && <button type="button" onClick={() => onEvidencia('sgs_mat')} className="ml-1 text-blue-700 underline">¿De dónde sale?</button>}
        </div>
      ) : <div className="text-gray-500">Liquidado {c.mat === null ? 'sin leer' : millones(c.mat)} · por liquidar: {c.mat_bandejas_error ? `el Centro MO no pudo leer las bandejas (${c.mat_bandejas_error})` : 'falta leer las bandejas de SGS (recalcular el caso)'}</div>}
      {c.mat_lcl_proyectado !== null && c.mat_lcl_proyectado !== undefined && <div className="text-amber-800">LCL completo: liquidado {millones(c.mat_lcl)} + por liquidar {millones(c.mat_lcl_por_liquidar)} = proyectado {millones(c.mat_lcl_proyectado)}. Contra esto se compara PMS.</div>}
      {c.mo_lcl !== null && c.mo_lcl !== undefined && <div className="text-amber-800">LCL completo (con {(d.compartido_con || []).join(', ')}): MO {millones(c.mo_lcl)}{c.mat_lcl !== null && c.mat_lcl !== undefined ? ` · MAT ${millones(c.mat_lcl)}` : ''}. Los deltas se miden contra esto.</div>}
    </div>
  )
  if (c.k === 'sonny') return (
    <div className="text-xs space-y-1">
      <div className="flex flex-wrap gap-1">{Object.entries(c.por_estado || {}).map(([e, v]) => <Chip key={e} cls="bg-violet-50 text-violet-800">{e} · {v.n} · {millones(v.mo)}</Chip>)}</div>
      <ul className="font-mono space-y-0.5 max-h-40 overflow-auto">{(c.detalle || []).map((x) => <li key={x.id_planilla}>{x.id_planilla} · {x.estado} · MO {millones(x.mo_real)} · MAT {millones(x.mat_real)}{x.libretos?.length ? ` · libreto ${x.libretos.join(', ')}` : ' · sin libreto enlazado'}</li>)}</ul>
    </div>
  )
  return (
    <div className="text-xs space-y-1">
      <div className="flex flex-wrap gap-1">{Object.entries(c.por_estado || {}).map(([e, v]) => <Chip key={e} cls={['ANN', 'RIF'].includes(e) ? 'bg-gray-100 text-gray-500' : 'bg-cyan-50 text-cyan-800'} title={`${v.nombre || NOMBRE_LIB[e] || e}: ${v.que} · lo tiene ${v.quien}`}>{e} «{v.nombre || NOMBRE_LIB[e] || e}» · {v.n} · {millones(v.mo)}{v.en_enel ? ' · en Enel' : ''}</Chip>)}</div>
      <div className="text-[11px] text-gray-500">Solo cuentan los libretos vivos (sin ANN «Anulado» ni RIF «Rechazado»). RIL «Lanzado» y SAP «SAP» están en Enel: su orden no se puede afirmar con los exportes y no se leen como «facturado». El cierre definitivo es la LCL en CONCLUSA. Exporte del {fecha(c.corte)}.</div>
      <ul className="font-mono space-y-0.5 max-h-40 overflow-auto">{(c.detalle || []).map((x) => <li key={`${x.lcl}-${x.libreto}`}>{x.libreto} · {x.estado_libreto} · MO {millones(x.mo_pesos)} · {x.planilla ? `planilla ${x.planilla} (${(x.regla || '').split(':')[0]})` : 'sin planilla'} · {fecha(x.fecha_autocert)}</li>)}</ul>
    </div>
  )
}
// ── (CHANGELOG 2026-10-25) Proyección del material: liquidado + por liquidar (SGS) contra lo presupuestado en PMS. Una línea, calmada; el rojo solo si excede.
function Proyeccion({ pr, onEvidencia, linea }) {
  if (!pr) return null
  const e = PROY[pr.estado] || {}
  const conCifras = ['cuadra', 'excede', 'por_debajo', 'sin_material_pms'].includes(pr.estado)
  if (linea && !conCifras) return null
  return (
    <div className={`${linea ? 'mt-1.5 text-[11px]' : 'text-xs'} text-gray-700 flex flex-wrap items-baseline gap-x-1.5`} data-proyeccion={pr.estado}>
      <b>Material proyectado</b>
      {conCifras ? <>
        <span>liquidado {millones(pr.liquidado)} + por liquidar <span className="text-amber-800">{millones(pr.por_liquidar)}</span> = <b>{millones(pr.proyectado)}</b></span>
        <span className="text-gray-400">·</span>
        <span>presupuestado (PMS) {pr.estado === 'sin_material_pms' ? '$0 (la solicitud no trae material)' : millones(pr.pms)}</span>
        {pr.delta !== null && pr.delta !== undefined && <span className="text-gray-500">· {signo(pr.delta)}{millones(Math.abs(pr.delta))}</span>}
        <span className={`font-semibold ${e.cls || ''}`} data-chip="proy-estado">{e.t}{pr.pct !== null && pr.pct !== undefined ? ` ${signo(pr.pct)}${pctTxt(pr.pct)}` : ''}</span>
        <span className="text-gray-400">(cuadra con ±{pr.umbral} %{pr.base_lcl ? ' · LCL completo' : ''}{pr.provisional ? ' · parte a costo SGS provisional' : ''})</span>
        {pr.negadas > 0 && <span className="text-gray-400" title="Una solicitud negada no es presupuesto">· sin {pr.negadas} solicitud(es) negada(s) ({millones(pr.negadas_mat)})</span>}
        {onEvidencia && <button type="button" onClick={() => onEvidencia('sgs_mat')} className="text-blue-700 underline">¿De dónde sale?</button>}
      </> : <span className="text-gray-500">{e.t || pr.estado}</span>}
    </div>
  )
}
// ── (CHANGELOG 2026-10-25) Sonny por etapa, debajo de la franja: sin planilla (nodos) → en curso (Dominion) → en Enel → libretos. Calmada por defecto (una
// línea); al abrirla, cada etapa con su cuenta, MO real y plan, los días de cada planilla en curso en su estado y los nodos sin planilla. Lo que
// falta de PMS va en gris como «techo», nunca como deuda. La cifra de la caja Sonny no cambia.
function EtapasFila({ et, onEvidencia }) {
  const [todos, setTodos] = useState(false)
  if (!et?.hay) return null
  const sp = et.sin_planilla || {}
  const t = Object.fromEntries((et.tramos || []).map((x) => [x.k, x]))
  const curso = t.en_curso?.planillas || []
  const enel = t.en_enel?.planillas || []
  const nodos = todos ? (sp.nodos || []) : (sp.nodos || []).slice(0, 15)
  const dias = et.dias || {}
  const caja = (k, titulo, x, cls, extra) => (
    <div className={`rounded border px-2 py-1 ${cls}`} data-etapa-caja={k}>
      <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">{titulo}</div>
      {extra || <>
        <div className="text-[12px] font-bold text-gray-800">{x?.n || 0} planilla(s) · {millones(x?.mo_real)}</div>
        <div className="text-[10px] text-gray-500">plan {millones(x?.mo_plan)} · MAT {millones(x?.mat_real)}</div>
        <div className="text-[10px] text-gray-500">{Object.entries(x?.por_estado || {}).map(([e, v]) => `${e} ${v.n}`).join(' · ')}</div>
      </>}
    </div>
  )
  return (
    <details className="mt-2 rounded border border-violet-200 bg-violet-50/40 px-2 py-1.5 text-xs" data-etapas="sonny">
      <summary className="cursor-pointer text-gray-700 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <b>Sonny por etapa</b>
        <span data-etapa="sin_planilla">{et.nodos_cargados ? <>sin planilla <b>{sp.n || 0}</b> nodo(s){sp.n ? <span className="text-gray-500"> ({sp.asignados} asignado(s) · {sp.finalizados} finalizado(s))</span> : null}</> : <span className="text-gray-400">sin planilla: falta la Lista Cruzada de Nodos</span>}
          {Number(sp.techo?.mo || 0) >= 1 && <span className="text-gray-400" title="Lo valorado en PMS (services) que aún no está en planillas de Sonny, por solicitud: puede ser trabajo sin planilla o diferencia de valoración. No es deuda."> · techo PMS {millones(sp.techo.mo)}</span>}</span>
        <span className="text-gray-300">→</span>
        <span data-etapa="en_curso">en curso <b>{t.en_curso?.n || 0}</b> · {millones(t.en_curso?.mo_real)}{t.en_curso?.n ? <span className="text-gray-500"> (plan {millones(t.en_curso?.mo_plan)})</span> : null}</span>
        <span className="text-gray-300">→</span>
        <span data-etapa="en_enel">en Enel <b>{t.en_enel?.n || 0}</b> · {millones(t.en_enel?.mo_real)}</span>
        <span className="text-gray-300">→</span>
        <span data-etapa="libretos">libretos <b>{t.libretos?.n || 0}</b> · {millones(t.libretos?.mo_real)}</span>
        {et.corte_nodos && <span className="text-[10px] px-1.5 py-0.5 rounded bg-violet-50 text-violet-800 border border-violet-200 whitespace-nowrap" title="Fecha del exporte de la Lista Cruzada de Nodos">nodos al {diaMes(et.corte_nodos)}</span>}
      </summary>
      <div className="mt-1.5 grid grid-cols-2 md:grid-cols-4 gap-1.5">
        {caja('sin_planilla', 'Sin planilla (nodos)', null, 'border-gray-200 bg-white', <>
          <div className="text-[12px] font-bold text-gray-800">{sp.n || 0} nodo(s) de {sp.n_nodos_caso || 0}</div>
          <div className="text-[10px] text-gray-500">{sp.asignados || 0} asignado(s) · {sp.finalizados || 0} finalizado(s) · {sp.pendientes || 0} pendiente(s)</div>
          <div className="text-[10px] text-gray-500">almacén: {Object.entries(sp.material || {}).map(([e, n]) => `${e} ${n}`).join(' · ') || '—'}</div>
          <div className="text-[10px] text-gray-400" title="Sonny no da valor por nodo. Lo valorado en PMS que aún no está en planillas es un techo, no deuda.">techo PMS (gris) {millones(sp.techo?.mo)}</div>
        </>)}
        {caja('en_curso', 'En curso (Dominion)', t.en_curso, 'border-gray-200 bg-white')}
        {caja('en_enel', 'En Enel', t.en_enel, 'border-gray-200 bg-white')}
        {caja('libretos', 'Libretos', t.libretos, 'border-gray-200 bg-white')}
      </div>
      {(curso.length > 0 || enel.length > 0) && (
        <table className="mt-1.5 w-full text-[11px]" data-etapas-tabla="planillas">
          <thead className="text-gray-500 text-left"><tr><th className="px-1">Planilla</th><th className="px-1">Estado</th><th className="px-1">En el estado desde</th><th className="px-1 text-right">Días</th><th className="px-1">Último cambio</th><th className="px-1 text-right">MO real</th><th className="px-1 text-right">MO plan</th></tr></thead>
          <tbody className="divide-y divide-gray-100">
            {[...curso, ...enel].map((x) => {
              const quieta = x.etapa === 'en_curso' && !['En firmas', 'Firmada', 'Rechazada'].includes(x.estado) && Number(x.dias_sin_cambio || 0) >= (dias.quieta || 15)
              const firmas = ['En firmas', 'Firmada', 'Rechazada'].includes(x.estado) && Number(x.dias_habiles || 0) >= (dias.firmas || 5)
              return (
                <tr key={x.id_planilla} data-etapa-planilla={x.id_planilla} data-etapa-de={x.etapa} className={quieta || firmas ? 'bg-amber-50/80' : ''}>
                  <td className="px-1 py-0.5 font-mono">{x.id_planilla}</td>
                  <td className="px-1 py-0.5">{x.estado}<span className="text-gray-400"> · {x.etapa === 'en_curso' ? 'Dominion' : 'Enel'}</span></td>
                  <td className="px-1 py-0.5 whitespace-nowrap" title={x.desde_regla}>{fecha(x.desde)}{x.desde_exacta ? '' : <span className="text-gray-400"> (último cambio)</span>}</td>
                  <td className="px-1 py-0.5 text-right">{x.dias ?? '—'}</td>
                  <td className={`px-1 py-0.5 whitespace-nowrap ${quieta ? 'text-amber-800 font-semibold' : ''}`} title={x.que_ultimo}>{fecha(x.ultimo_cambio)} · {x.dias_sin_cambio ?? '—'} d sin cambios</td>
                  <td className="px-1 py-0.5 text-right whitespace-nowrap">{millones(x.mo_real)}</td>
                  <td className="px-1 py-0.5 text-right whitespace-nowrap text-gray-500">{x.plan_atipico ? <span title={`Plan atípico ${pesos(x.plan_atipico)}: no suma`}>atípico</span> : millones(x.mo_plan)}</td>
                </tr>)
            })}
          </tbody>
        </table>
      )}
      {nodos.length > 0 && (
        <div className="mt-1.5" data-etapas-tabla="nodos">
          <div className="text-[11px] font-semibold text-gray-600">Nodos sin planilla</div>
          <table className="w-full text-[11px]">
            <thead className="text-gray-500 text-left"><tr><th className="px-1">Nodo</th><th className="px-1">Estado</th><th className="px-1">Desde</th><th className="px-1 text-right">Días hábiles</th><th className="px-1">Material en almacén</th><th className="px-1">Solicitud (QR)</th></tr></thead>
            <tbody className="divide-y divide-gray-100">
              {nodos.map((x) => (
                <tr key={x.id_nodo} data-nodo={x.id_nodo} className={['Asignado', 'Finalizado'].includes(x.estado) && Number(x.dias_habiles || 0) >= (dias.nodo || 15) ? 'bg-amber-50/80' : ''}>
                  <td className="px-1 py-0.5 font-mono" title={`Id_nodo ${x.id_nodo}`}>{x.nodo}</td><td className="px-1 py-0.5">{x.estado || '—'}</td><td className="px-1 py-0.5 whitespace-nowrap">{fecha(x.desde)}</td>
                  <td className="px-1 py-0.5 text-right">{x.dias_habiles ?? '—'}</td><td className="px-1 py-0.5">{x.material || '—'}{x.movimiento ? <span className="text-gray-400"> · {x.movimiento}</span> : null}</td>
                  <td className="px-1 py-0.5 font-mono text-gray-500 truncate max-w-[200px]" title={x.qr}>{x.qr}</td>
                </tr>))}
            </tbody>
          </table>
          {(sp.nodos || []).length > 15 && <button type="button" onClick={() => setTodos(!todos)} className="mt-1 text-blue-700 underline">{todos ? 'ver menos' : `ver los ${(sp.nodos || []).length}`}</button>}
        </div>
      )}
      <div className="mt-1 text-[11px] text-gray-500">Sin planilla = nodos de la Lista Cruzada de Nodos de Sonny sin Id_planilla, con la fecha de su estado. En curso: días desde la creación, el cambio de estado o la primera asignación a un firmante (Historial de Actividades); «último cambio» si Sonny no lo trae. Ámbar = pasa el umbral de su alerta ({dias.nodo || 15} días hábiles nodo · {dias.quieta || 15} días sin cambios · {dias.firmas || 5} días hábiles en firmas). Sonny no da valor por nodo; el techo de PMS es gris: no es deuda.
        {onEvidencia && <button type="button" onClick={() => onEvidencia('sonny_etapa')} className="ml-1 text-blue-700 underline">¿De dónde sale?</button>}</div>
    </details>
  )
}
// ── (CHANGELOG 2026-10-25) SCM · cierre, debajo de la franja: el estado de cada LCL del caso en el exporte de LCL de SCM (CONCLUSA = concluida según SCM; si eso
// equivale a pagado está por confirmar con FBL1N) y los libretos del caso por estado, con el nombre que muestra la pantalla. Calmada por defecto
// (una línea). RIL «Lanzado» y SAP «SAP» son «en Enel», nunca «facturado».
function ScmLclFila({ s, c, onEvidencia }) {
  const pe = Object.entries(c?.por_estado || {})
  if (!s?.hay && !pe.length) return null
  const lcls = s?.lcls || []
  const dias = (x) => (x.dias_fin === null || x.dias_fin === undefined ? '' : x.dias_fin >= 0 ? ` (en ${x.dias_fin} d)` : ` (hace ${-x.dias_fin} d)`)
  return (
    <details className="mt-2 rounded border border-cyan-200 bg-cyan-50/40 px-2 py-1.5 text-xs" data-scm-lcl="fila">
      <summary className="cursor-pointer text-gray-700 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <b>SCM · cierre</b>
        {s?.hay ? <ChipLcl l={c?.lcl} /> : <span className="text-gray-400">la LCL no está en el exporte de LCL de SCM</span>}
        {s?.hay && <span data-scm-lcl="importe">{lcls.length} LCL · Importe {millones(s.importe)} · MO de sus libretos {millones(s.mo_libretos)}</span>}
        {pe.length > 0 && <span className="text-gray-500" data-scm-lcl="estados">libretos del caso: {pe.map(([e, v]) => `${e} ${v.n}`).join(' · ')}</span>}
        {s?.corte && <span className="text-[10px] px-1.5 py-0.5 rounded bg-cyan-50 text-cyan-800 border border-cyan-200 whitespace-nowrap" title="Fecha del exporte de LCL de SCM cargado">LCL al {diaMes(s.corte)}</span>}
      </summary>
      {lcls.length > 0 && (
        <table className="mt-1.5 w-full text-[11px]" data-scm-tabla="lcl">
          <thead className="text-gray-500 text-left"><tr><th className="px-1">LCL</th><th className="px-1">Estado</th><th className="px-1">En el estado desde</th><th className="px-1">Fin de validez</th><th className="px-1 text-right">Órdenes</th><th className="px-1 text-right">Importe</th><th className="px-1 text-right">MO libretos vivos</th><th className="px-1">¿Cuadra?</th><th className="px-1 text-right">Libretos</th></tr></thead>
          <tbody className="divide-y divide-gray-100">
            {lcls.map((x) => (
              <tr key={x.lcl} data-scm-lcl-fila={x.lcl} data-estado={x.estado}>
                <td className="px-1 py-0.5 font-mono">{x.lcl}{x.acumulativa ? <span className="text-gray-400" title="Acumulativa (según SCM)"> · acum.</span> : null}</td>
                <td className="px-1 py-0.5" title={x.que}><span className={x.concluida ? 'text-emerald-800 font-semibold' : 'text-gray-800'}>{x.estado}</span> <span className="text-gray-500">{x.que}</span></td>
                <td className="px-1 py-0.5 whitespace-nowrap" title="SCM no fecha el cambio de estado: es el primer exporte cargado en que la LCL figura así">{fecha(x.estado_desde)} (exporte)</td>
                <td className="px-1 py-0.5 whitespace-nowrap">{fecha(x.fin_validez)}{dias(x)}</td>
                <td className="px-1 py-0.5 text-right" title={(x.trabajos || []).join(', ')}>{x.n_trabajos}</td>
                <td className="px-1 py-0.5 text-right whitespace-nowrap">{millones(x.importe)}</td>
                <td className="px-1 py-0.5 text-right whitespace-nowrap">{millones(x.mo_libretos)}</td>
                <td className="px-1 py-0.5 whitespace-nowrap">{x.cuadra === true ? <span className="text-green-700">sí</span> : x.cuadra === false ? <span className="text-red-700" title={`MO − Importe ${millones(x.delta)}`}>no ({x.pct === null ? 'Importe $0' : `${signo(x.pct)}${pctTxt(x.pct)}`})</span> : <span className="text-gray-400" title="Solo se compara en las CONCLUSA">—</span>}</td>
                <td className="px-1 py-0.5 text-right">{x.n_libretos}</td>
              </tr>))}
          </tbody>
        </table>
      )}
      {(s?.sin_scm || []).length > 0 && <div className="mt-1 text-gray-500">LCL del caso sin fila en el exporte de LCL de SCM: {s.sin_scm.join(', ')}</div>}
      {pe.length > 0 && (
        <table className="mt-1.5 w-full text-[11px]" data-scm-tabla="libretos">
          <thead className="text-gray-500 text-left"><tr><th className="px-1">Estado del libreto</th><th className="px-1">Nombre en pantalla</th><th className="px-1">Significado (glosario de facturación)</th><th className="px-1 text-right">Libretos</th><th className="px-1 text-right">MO</th><th className="px-1">¿Suma en la caja?</th></tr></thead>
          <tbody className="divide-y divide-gray-100">
            {pe.map(([e, v]) => (
              <tr key={e} data-lib-estado={e}>
                <td className="px-1 py-0.5 font-mono">{e}{v.en_enel ? <span className="ml-1 text-[10px] px-1 rounded bg-indigo-50 text-indigo-800 font-sans" data-chip="en-enel">en Enel</span> : null}</td>
                <td className="px-1 py-0.5">{v.nombre || NOMBRE_LIB[e] || e}</td>
                <td className="px-1 py-0.5 text-gray-600">{v.que}{v.quien && v.quien !== '—' ? <span className="text-gray-400"> · lo tiene {v.quien}</span> : null}</td>
                <td className="px-1 py-0.5 text-right">{v.n}</td>
                <td className="px-1 py-0.5 text-right whitespace-nowrap">{millones(v.mo)}</td>
                <td className="px-1 py-0.5">{v.suma === false ? <span className="text-gray-400">no (anulado o devuelto)</span> : 'sí'}</td>
              </tr>))}
          </tbody>
        </table>
      )}
      <div className="mt-1 text-[11px] text-gray-500">Cerrado = todas las LCL del caso en CONCLUSA (concluida según SCM; si eso equivale a pagado está por confirmar con FBL1N). CHI «Cerrado» es «cerrado por el contratista, sin enviar»: no es un cierre definitivo. RIL y SAP están en Enel y su orden no se puede afirmar; no son «facturado». Trabajo = orden de SAP.
        {onEvidencia && <>{s?.hay && <button type="button" onClick={() => onEvidencia('scm_lcl')} className="ml-1 text-blue-700 underline" data-evidencia-fila="scm_lcl">¿De dónde sale?</button>}{pe.length > 0 && <button type="button" onClick={() => onEvidencia('scm_estado')} className="ml-1 text-blue-700 underline" data-evidencia-fila="scm_estado">libretos por estado</button>}</>}</div>
    </details>
  )
}
// ── (CHANGELOG 2026-10-23) Material por código, debajo de la franja: SGS despachado → SGS consumido → SCM posado → SAP descargado. Calmado por defecto
// (una línea); al abrirlo, la diferencia resaltada donde no cuadra (±1 %). Lo que SAP aún no ve se dice, no se pinta como 0.
function MaterialFila({ m, sap, onEvidencia }) {
  const [todos, setTodos] = useState(false)
  if (!m || !m.n) return null
  const filas = todos ? m.filas : m.filas.slice(0, 25)
  const celda = (v, rojo, extra) => <td className={`px-1 py-0.5 text-right whitespace-nowrap ${rojo ? 'bg-red-50 text-red-700 font-semibold' : ''}`}>{v === null || v === undefined ? '—' : numero(v)}{extra}</td>
  const recSap = (m.rec || []).reduce((a, x) => a + Number(x.sap || 0), 0)
  const recScm = (m.rec || []).reduce((a, x) => a + Number(x.scm || 0), 0)
  return (
    <details className="mt-2 rounded border border-gray-200 bg-gray-50/60 px-2 py-1.5 text-xs" data-material="codigos">
      <summary className="cursor-pointer text-gray-700 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <b>Material por código</b>
        <span>{m.n} código(s) · <span className="text-green-700">{m.n_cuadran} cuadran</span>{m.n_difieren ? <> · <span className="text-red-700">{m.n_difieren} con diferencia</span></> : null}{m.n_no_ve ? <> · <span className="text-gray-500">{m.n_no_ve} que SAP aún no ve</span></> : null}{m.n_por_liquidar ? <> · <span className="text-amber-800" data-material="por-liquidar">{m.n_por_liquidar} con material por liquidar ({millones(m.v_por_liquidar)})</span></> : null}</span>
        {m.sap_hay ? <ChipSap corte={m.corte_sap} /> : <span className="text-gray-400">sin MB51 de SAP</span>}
        {m.sap_hay && !m.sap_ve_lcl && <span className="text-gray-500">SAP no tiene descargas de este código: no se lee como 0</span>}
      </summary>
      <div className="mt-1.5 max-h-[50vh] overflow-auto">
        <table className="w-full text-[11px]">
          <thead className="text-gray-500 text-left sticky top-0 bg-gray-50"><tr>
            <th className="px-1">Código</th><th className="px-1">Material</th><th className="px-1 text-right">SGS despachado</th><th className="px-1 text-right">SGS devuelto</th><th className="px-1 text-right">SGS consumido</th>
            <th className="px-1 text-right" title="Despachado − devuelto − consumido (bandejas aprobadas de SGS)">Por liquidar</th>
            <th className="px-1 text-right">SCM posado</th><th className="px-1 text-right">SAP descargado</th><th className="px-1 text-right">Precio</th><th className="px-1">¿Cuadra?</th>
          </tr></thead>
          <tbody className="divide-y divide-gray-100">
            {filas.map((x) => (
              <tr key={x.material} data-material-fila={x.material} data-cuadra={x.cuadra ? 'si' : 'no'} data-por-liquidar={Number(x.por_liquidar || 0) > 0 ? 'si' : 'no'} className={Number(x.por_liquidar || 0) > 0 ? 'bg-amber-50/70' : ''}>
                <td className="px-1 py-0.5 font-mono">{x.material}</td>
                <td className="px-1 py-0.5 max-w-[200px] truncate" title={x.nombre}>{x.nombre} <span className="text-gray-400">{x.unidad}</span></td>
                {celda(x.sgs_desp, false)}
                {celda(x.sgs_dev, false)}
                {celda(x.sgs_cons, x.marcas?.sgs_scm || x.marcas?.sgs_sap)}
                <td data-col="por-liquidar" className={`px-1 py-0.5 text-right whitespace-nowrap ${Number(x.por_liquidar || 0) > 0 ? 'text-amber-800 font-semibold' : 'text-gray-400'}`} title={Number(x.por_liquidar || 0) > 0 ? `${pesos(x.v_por_liquidar)} · última salida ${fecha(x.f_despacho)}${x.bandeja ? ` (${x.bandeja})` : ''}` : ''}>{x.por_liquidar === null || x.por_liquidar === undefined ? '—' : numero(x.por_liquidar)}{Number(x.sobre || 0) > 0 ? <span className="ml-1 text-[10px] text-gray-500" title="Consumido por encima de lo despachado neto (dato raro de SGS)">+{numero(x.sobre)} de más</span> : null}</td>
                {celda(x.scm, x.marcas?.sgs_scm || x.marcas?.scm_sap)}
                {x.estado_sap === 'no_ve' ? <td className="px-1 py-0.5 text-right text-gray-400 whitespace-nowrap" data-col="sap" title={`El libreto es del ${fecha(x.fecha_libreto)} y el MB51 llega al ${fecha(m.corte_sap)}`}>aún no lo ve</td>
                  : x.estado_sap === 'fuera' || x.sap === null || x.sap === undefined ? <td className="px-1 py-0.5 text-right text-gray-400" data-col="sap" title="SAP no maneja este código (material propio de Dominion o sin MB51)">—</td>
                  : <td data-col="sap" className={`px-1 py-0.5 text-right whitespace-nowrap ${x.marcas?.scm_sap || x.marcas?.sgs_sap ? 'bg-red-50 text-red-700 font-semibold' : ''}`}>{numero(x.sap)}{x.manual ? <span className="ml-1 text-[10px] px-1 rounded bg-amber-100 text-amber-800 font-normal" title="Descarga manual «Orden de trabajo en flujo de facturación»">manual {diaMes(x.f_manual)}</span> : null}</td>}
                <td className="px-1 py-0.5 text-right whitespace-nowrap" title={x.precio_fuente === 'sap' ? 'precio de Enel (SAP)' : 'costo de SGS (provisional)'}>{x.precio ? pesos(x.precio) : '—'}{x.precio_fuente === 'sgs' ? <span className="text-gray-400"> prov.</span> : ''}</td>
                <td className="px-1 py-0.5 whitespace-nowrap">{x.cuadra ? <span className="text-green-700">sí</span> : <span className="text-red-700">no</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {m.filas.length > 25 && <button type="button" onClick={() => setTodos(!todos)} className="mt-1 text-blue-700 underline">{todos ? 'ver menos' : `ver los ${m.filas.length}`}</button>}
      </div>
      {m.rec_n > 0 && <div className="mt-1 text-gray-600" data-material="recuperado">Recuperado (KG), aparte: {m.rec_n} código(s) · SCM {numero(recScm)} kg · SAP {m.sap_hay ? `${numero(recSap)} kg` : '—'} · {(m.rec || []).filter((x) => x.cuadra).length} cuadran</div>}
      <div className="mt-1 text-[11px] text-gray-500">Cuadra = iguales con ±1 %. Rojo = la diferencia. Ámbar = material por liquidar (despachado − devuelto − consumido, bandejas aprobadas de SGS). SGS es el material de cliente {m.compartido_con?.length ? 'del LCL completo' : 'del LCL'}; SCM, los libretos vivos; SAP, lo descargado contra el LCL (261 − 262 − 281).
        {onEvidencia && <><button type="button" onClick={() => onEvidencia('material')} className="ml-1 text-blue-700 underline">¿De dónde sale?</button>{m.sap_hay && <button type="button" onClick={() => onEvidencia('sap_mat')} className="ml-1 text-blue-700 underline">filas de SAP</button>}</>}</div>
    </details>
  )
}
function Franja({ d, onFuente, onEvidencia, sgsLeido }) {
  const [abierta, setAbierta] = useState(null)
  const { data: fu } = useQuery({ queryKey: ['gestion-fuentes'], queryFn: () => api.get('/gestion/fuentes').then((r) => r.data), staleTime: 120000 })
  if (!d?.cajas) return <div className="bg-white border border-gray-200 rounded-lg p-3 text-sm text-gray-400">La franja del dinero aparece al recalcular el caso con los exportes de PMS y SCM cargados.</div>
  const caja = d.cajas.find((c) => c.k === abierta)
  const ev = d.evidencia || {}
  const nEv = Object.values(ev.cajas || {}).filter((x) => x.ok !== null && x.ok !== undefined)
  const exporte = (k) => { const s = (fu?.sistemas || []).find((x) => x.k === k); const c = s?.cargas?.[0]; return c ? fecha(c.exporte) : (s?.sin_registro ? 'sin registro' : '—') }
  return (
    <div className="bg-white border border-gray-200 rounded-lg p-3" data-franja="dinero">
      <div className="flex flex-wrap items-baseline justify-between gap-2 mb-2">
        <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide flex items-center gap-1"><Banknote size={14} />El dinero · de lo presupuestado a lo cobrado</div>
        <div className="flex flex-wrap items-baseline gap-2">
          {d.compartido_con?.length > 0 && <span className="text-[11px] text-amber-800">PMS, Sonny, SCM y SAP van por LCL: compartido con {d.compartido_con.join(', ')}{d.alertas_lcl === false ? ' · sus alertas de libreto y material salen en ese caso' : ''}</span>}
          {d.sap?.hay && <ChipSap corte={d.sap.corte} />}
        </div>
      </div>
      <div className="flex items-stretch gap-0.5 overflow-x-auto">
        {d.cajas.map((c, i) => (
          <Fragment key={c.k}>
            {i > 0 && <Flecha d={d.deltas?.[i - 1]} />}
            <Caja c={c} activa={abierta === c.k} malas={ev.malas} onEvidencia={onEvidencia && (c.hay || (c.k === 'sap' && (c.mat_hay || c.cobro?.hay))) ? () => onEvidencia(c.k === 'sap' && !c.cobro?.hay ? 'sap_mat' : CAJA_TARJETA[c.k]?.[0]) : null}
              onClick={() => { setAbierta(abierta === c.k ? null : c.k); if (c.k !== 'sap' && onFuente) onFuente(c.k, abierta !== c.k) }} />
          </Fragment>
        ))}
      </div>
      <Proyeccion pr={d.proyeccion} onEvidencia={onEvidencia} linea />
      <EtapasFila et={d.etapas} onEvidencia={onEvidencia} />
      <ScmLclFila s={d.scm_lcl} c={d.cajas.find((x) => x.k === 'scm')} onEvidencia={onEvidencia} />
      <div className="text-[11px] text-gray-400 mt-1">MO de SGS en gris: la valida el liquidador, no se compara. Material a precio de Enel (SAP) donde el MB51 trae el código; si no, a costo de SGS (provisional). Toca una caja para ver su detalle y filtrar la línea de tiempo; «¿De dónde sale?» abre las filas que la suman.</div>
      <div className="text-[11px] mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-gray-500" data-resumen="fuentes">
        <span>Fuentes: PMS {exporte('pms')} · Sonny {exporte('sonny_lista')} · nodos {exporte('sonny_nodos')} · SCM {exporte('scm')} · LCL SCM {exporte('scm_lcl')} · SAP {exporte('sap')} · informe de deuda {exporte('deuda')}{(fu?.sistemas || []).some((x) => x.k === 'drape') ? ` · facturas ${exporte('drape')}` : ''} · SGS en vivo {sgsLeido ? fechaHora(sgsLeido) : '—'}</span>
        {nEv.length > 0 && <span className={ev.ok === false ? 'text-red-700 font-semibold' : 'text-green-700'}>{nEv.filter((x) => x.ok).length} de {nEv.length} cifras se reproducen con sus filas{ev.malas?.length ? ` · no cuadran: ${ev.malas.join(', ')}` : ''}</span>}
        {ev.error && <span className="text-red-700">evidencia con error: {ev.error}</span>}
        {fu?.rojos > 0 && <span className="text-red-700">{fu.rojos} control(es) en rojo en «Fuentes y controles»</span>}
      </div>
      {d.mat_dif_n > 0 && (
        <details className="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1.5 text-xs text-red-800">
          <summary className="cursor-pointer font-semibold">Material: SCM tiene posado más de lo que SGS muestra consumido en {d.mat_dif_n} código(s) · {millones(d.resumen?.mat_rojo_valor)}{(d.mat_dif || []).every((x) => x.precio_fuente === 'sap') ? ' a precio de Enel (SAP)' : (d.mat_dif || []).some((x) => x.precio_fuente === 'sap') ? ' (precio Enel y costo SGS provisional)' : ' a costo SGS (provisional)'}</summary>
          <table className="mt-1 w-full">
            <thead><tr className="text-left text-red-700"><th>material</th><th className="text-right">SCM posado</th><th className="text-right">SGS consumido</th><th className="text-right">SGS despachado</th><th className="text-right">SAP descargado</th><th className="text-right">valor</th></tr></thead>
            <tbody>{(d.mat_dif || []).slice(0, 15).map((x) => <tr key={x.material}><td><span className="font-mono">{x.material}</span> {x.nombre}</td><td className="text-right">{numero(x.scm)} {x.unidad}</td><td className="text-right">{numero(x.sgs_consumido)}</td><td className="text-right">{numero(x.sgs_despachado)}</td><td className="text-right" title={x.sap_estado === 'no_ve' ? 'SAP aún no lo ve: el libreto es posterior al corte del MB51' : ''}>{x.sap === null || x.sap === undefined ? '—' : x.sap_estado === 'no_ve' ? <span className="text-gray-400">aún no lo ve</span> : numero(x.sap)}</td><td className="text-right">{millones(x.valor)}{x.precio_fuente === 'sgs' ? <span className="text-red-400"> prov.</span> : ''}</td></tr>)}</tbody>
          </table>
          <div className="mt-1 text-red-700">Solo material de cliente que SGS reconoce en el LCL y con monto, ordenado por monto. Revisar y corregir desde <Link to="/modulos/control" className="underline">Control Mensual → «Validado por correos»</Link>.
            {onEvidencia && <button type="button" onClick={() => onEvidencia('alerta:mat_cobrado_sin_consumir')} className="ml-1 underline">¿De dónde sale?</button>}</div>
        </details>
      )}
      {d.mat_menores_n > 0 && <div className="mt-1 text-[11px] text-gray-600" data-mat-menores>{d.mat_menores_n} diferencia(s) de material SCM &gt; SGS por {pesos(d.mat_menores_valor)} en todo el caso: menos de {pesos(d.mat_minimo)}, no son alerta; se ven en «¿De dónde sale?» de la caja SCM{onEvidencia && <button type="button" onClick={() => onEvidencia('scm_mat')} className="ml-1 underline">(ver)</button>}.</div>}
      {d.mat_revisar_n > 0 && <div className="mt-1 text-[11px] text-sky-800">{d.mat_revisar_n} código(s) de SCM que SGS no reconoce en este LCL o no les tiene costo: no generan alerta; se revisan en «¿De dónde sale?» de la caja SCM{onEvidencia && <button type="button" onClick={() => onEvidencia('scm_mat')} className="ml-1 underline">(ver)</button>}.</div>}
      <MaterialFila m={d.material} sap={d.sap} onEvidencia={onEvidencia} />
      {d.deuda && <div className="mt-2 text-xs text-gray-600">Informe de deuda de facturación (corte {fecha(d.deuda.corte)}): {d.deuda.figura ? <>figura con MO {millones(d.deuda.mo)} · MAT {millones(d.deuda.mat)} · {d.deuda.estados.join(', ')}{d.deuda.responsables?.length ? ` · pendiente de ${d.deuda.responsables.join(' / ')}` : ''}</> : 'no figura'}</div>}
      {caja && <div className="mt-2 border-t border-gray-100 pt-2"><DetalleCaja c={caja} d={d} onEvidencia={onEvidencia} /></div>}
    </div>
  )
}
// ── (CHANGELOG 2026-10-23) «¿De dónde sale?»: cada cifra con sus filas, su fórmula, su fuente y la confianza de cada cruce ─────────────
// La suma al pie la compara el servidor con la cifra de la caja: si no la reproduce al peso, se dice en rojo (nunca se esconde).
const ALERTAS_DINERO = ['mat_cobrado_sin_consumir', 'aprobado_sin_libreto', 'libreto_en_enel', 'libreto_devuelto', 'liquidado_sin_cobro', 'scm_sin_sap', 'sap_mas_que_scm', 'sgs_sin_sap', 'mat_excede_pms', 'mat_por_liquidar',
  'nodo_sin_planilla', 'planilla_en_curso_quieta', 'planilla_en_firmas', 'pms_finalizado_sin_planilla', 'lcl_concluida_libreto_abierto', 'lcl_sin_libreto_vence', 'importe_lcl_vs_mo',
  'lcl_concluida_sin_factura', 'factura_vencida_sin_pago', 'factura_rechazada', 'em_sin_facturar']
// (CHANGELOG 2026-10-25) la planilla rechazada tiene tarjeta (el motivo de Enel) cuando viene de Sonny; el rechazo visto solo por correo, no
const conTarjeta = (a) => ALERTAS_DINERO.includes(a.tipo) || (a.tipo === 'rechazo_abierto' && (a.planillas || []).length > 0)
const CAJA_TARJETA = { pms: ['pms'], sgs: ['sgs_mo', 'sgs_mat'], sonny: ['sonny', 'sonny_etapa'], scm: ['scm_mo', 'scm_mat', 'scm_estado', 'scm_lcl'], sap: ['drape', 'deuda', 'sap_mat', 'material'] }
const CONF_CLS = { alta: 'bg-green-100 text-green-800', confirmada: 'bg-blue-100 text-blue-800', media: 'bg-amber-100 text-amber-800', ambigua: 'bg-red-100 text-red-700', 'sin planilla': 'bg-gray-100 text-gray-500' }
const CTL_CLS = { verde: 'bg-green-500', rojo: 'bg-red-500', ambar: 'bg-amber-400', gris: 'bg-gray-300' }
function pesos(v) { return v === null || v === undefined || v === '' ? '' : `$${Math.round(Number(v)).toLocaleString('es-CO')}` }
function numero(v) { return v === null || v === undefined || v === '' ? '' : Number(v).toLocaleString('es-CO', { maximumFractionDigits: 3 }) }
function copiar(t) {
  if (!t) return
  try { navigator.clipboard.writeText(String(t)).then(() => toast.success(`Copiado: ${t}`), () => toast(`Id: ${t}`)) } catch { toast(`Id: ${t}`) }
}
function Celda({ c, f }) {
  const v = f[c.k]
  if (c.tipo === 'pesos') return <span className="whitespace-nowrap">{pesos(v)}</span>
  if (c.tipo === 'num') return <span className="whitespace-nowrap">{numero(v)}</span>
  if (c.tipo === 'fecha') return <span className="whitespace-nowrap">{v ? fecha(v) : ''}</span>
  if (c.tipo === 'conf') return v ? <Chip cls={CONF_CLS[v] || 'bg-gray-100 text-gray-600'} title={f.regla || ''}>{v}</Chip> : null
  return <span>{v === null || v === undefined ? '' : String(v)}</span>
}
function BotonesDecision({ acciones, pid, puede, onDecidir }) {
  if (!acciones?.length) return null
  return (
    <div className="flex flex-col gap-1">
      {acciones.map((a) => (
        <div key={`${a.libreto}-${a.id_planilla}`} className="flex flex-wrap items-center gap-1 text-[11px]">
          <span className={a.del_caso ? 'text-gray-700' : 'text-gray-400'}>{a.etiqueta}</span>
          {a.decision ? (
            <>
              <Chip cls={a.decision.decision === 'si' ? 'bg-blue-100 text-blue-800' : 'bg-gray-200 text-gray-600'}>{a.decision.decision === 'si' ? 'confirmada' : 'descartada'} por {(a.decision.usuario || '').split('@')[0]} el {fecha(a.decision.fecha)}</Chip>
              {puede && <button type="button" data-accion="deshacer" onClick={() => onDecidir({ deshacer: a.decision.id })} className="inline-flex items-center gap-0.5 text-gray-600 hover:text-blue-700"><Undo2 size={11} />Deshacer</button>}
            </>
          ) : puede ? (
            <>
              <button type="button" data-accion="es-este" onClick={() => onDecidir({ proyecto_id: pid, lcl: a.lcl, libreto: a.libreto, id_planilla: a.id_planilla, decision: 'si' })} className="px-1.5 py-0.5 rounded border border-green-600 text-green-700 hover:bg-green-50">Es este</button>
              <button type="button" data-accion="no-es" onClick={() => onDecidir({ proyecto_id: pid, lcl: a.lcl, libreto: a.libreto, id_planilla: a.id_planilla, decision: 'no' })} className="px-1.5 py-0.5 rounded border border-gray-400 text-gray-600 hover:bg-gray-50">No es</button>
            </>
          ) : <span className="text-gray-400">(pendiente de confirmar)</span>}
        </div>
      ))}
    </div>
  )
}
function TablaFilas({ columnas, filas, pid, puede, onDecidir, pie }) {
  const conAcciones = filas.some((f) => f.acciones?.length)
  return (
    <div className="max-h-[46vh] overflow-auto border border-gray-200 rounded">
      <table className="w-full text-[11px]">
        <thead className="bg-gray-50 text-gray-500 sticky top-0 z-10">
          <tr>
            {columnas.map((c) => <th key={c.k} className={`px-1.5 py-1 font-semibold ${['pesos', 'num'].includes(c.tipo) ? 'text-right' : 'text-left'}`}>{c.t}</th>)}
            {conAcciones && <th className="px-1.5 py-1 text-left font-semibold">Confirmar</th>}
            <th className="px-1.5 py-1" />
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {filas.map((f, i) => (
            <tr key={i} data-destacar={f.destacar ? 'si' : undefined} className={f._cab ? 'bg-cyan-50/60 font-semibold' : f.anterior ? 'bg-amber-50/50' : f.destacar ? 'bg-amber-50/80' : ''}>
              {f._cab ? (
                <td colSpan={columnas.length} className="px-1.5 py-1">
                  <span className="font-mono">{f.libreto}</span> · {f.estado} · planilla {f.planilla} <Chip cls={CONF_CLS[f.confianza] || 'bg-gray-100 text-gray-600'} title={f.regla}>{f.confianza}</Chip>
                  <span className="font-normal text-gray-500"> · MO del libreto {pesos(f.mo_libreto)}{f.ok === false ? '' : f.ok ? ' · sus prestaciones suman lo mismo' : ''}</span>
                  {f.ok === false && <span className="text-red-700 font-normal"> · sus prestaciones suman {pesos(f.subtotal)}: no cuadra</span>}
                  {f.anterior && <span className="font-normal text-amber-700"> · de un exporte anterior</span>}
                  {f.descripcion && <div className="font-normal text-gray-500 truncate max-w-[560px]" title={f.descripcion}>{f.descripcion}</div>}
                </td>
              ) : columnas.map((c) => <td key={c.k} className={`px-1.5 py-1 align-top ${['pesos', 'num'].includes(c.tipo) ? 'text-right' : ''} ${c.k === 'descripcion' ? 'max-w-[220px] truncate' : ''}`} title={c.k === 'descripcion' ? f.descripcion : (c.k === 'pesos' && f.aviso ? f.aviso : undefined)}><Celda c={c} f={f} />{c.k === 'pesos' && f.aviso ? <div className="text-[10px] text-amber-700">{f.aviso}</div> : null}</td>)}
              {conAcciones && <td className="px-1.5 py-1 align-top"><BotonesDecision acciones={f.acciones} pid={pid} puede={puede} onDecidir={onDecidir} /></td>}
              <td className="px-1.5 py-1 align-top text-right">{f.copiar ? <button type="button" title={`Copiar ${f.copiar} para buscarlo en el sistema`} onClick={() => copiar(f.copiar)} className="text-gray-400 hover:text-blue-700 inline-flex items-center gap-0.5"><Copy size={11} />Copiar id</button> : null}</td>
            </tr>
          ))}
          {filas.length === 0 && <tr><td colSpan={columnas.length + 2} className="px-2 py-3 text-center text-gray-400">Sin filas: el sistema no trae nada para este caso.</td></tr>}
        </tbody>
        {pie}
      </table>
    </div>
  )
}
function FuenteBloque({ f }) {
  if (!f) return null
  if (f.vivo) return <div className="text-xs text-gray-700"><b>{f.sistema}</b> · {f.nota} · {f.consultado ? fechaHora(f.consultado) : 'sin lectura'}</div>
  return (
    <div className="text-xs text-gray-700 space-y-0.5">
      <div><b>{f.sistema}</b></div>
      {(f.cargas || []).map((c) => (
        <div key={c.id} className="flex flex-wrap gap-x-2 items-baseline">
          <span className="font-mono text-gray-800">{c.archivo}</span>
          <span>exporte {fechaHora(c.exporte)}</span>
          <span>cargado {fechaHora(c.cargado)}{c.usuario ? ` por ${c.usuario}` : ''}{c.equipo ? ` (${c.equipo})` : ''}</span>
          <span>{Number(c.filas || 0).toLocaleString('es-CO')} filas</span>
          <button type="button" className="font-mono text-gray-400 hover:text-blue-700" title="Huella sha256 del archivo: copiar" onClick={() => copiar(c.sha256)}>sha256 {String(c.sha256 || '').slice(0, 12)}…</button>
          {c.ultima === false && <span className="text-amber-700">no es el último exporte cargado</span>}
        </div>
      ))}
      {f.sin_registro && <div className="text-amber-700">{f.sin_registro}</div>}
      {f.corte && <div className="text-gray-500">Consumo (261) hasta el {fecha(f.corte)}: lo que se autocertificó después aún no está en SAP.</div>}
      {f.precios?.sha256 && <div className="text-gray-500">Precios QP: {f.precios.archivo} · {f.precios.codigos} códigos · del {fecha(f.precios.modificado)} · huella {f.precios.sha256.slice(0, 8)}</div>}
      {f.precios_enel?.n > 0 && <div className="text-gray-500" data-precios-enel>Precio de Enel: {f.precios_enel.regla} · {f.precios_enel.n} códigos en {f.precios_enel.tabla}{f.precios_enel.carga ? ` · de ${f.precios_enel.carga.archivo} (sha256 ${String(f.precios_enel.carga.sha256 || '').slice(0, 12)}…, cargado ${fechaHora(f.precios_enel.carga.cargado)})` : ''}{f.precios_enel.fecha ? ` · SAP al ${fecha(f.precios_enel.fecha)}` : ''}. Donde SAP no trae el código, costo de SGS (provisional).</div>}
    </div>
  )
}
function Lista({ titulo, filas, cls, children }) {
  if (!filas?.length) return null
  return (
    <div className={`rounded border px-2 py-1.5 text-xs ${cls}`}>
      <div className="font-semibold mb-1">{titulo}</div>
      {children}
    </div>
  )
}
export function EvidenciaPanel({ pid, inicial, onClose }) {
  const qc = useQueryClient()
  const { data: ev, isLoading, isError } = useQuery({ queryKey: ['gestion-evidencia', pid], queryFn: () => api.get(`/gestion/proyectos/${pid}/evidencia`).then((r) => r.data), enabled: !!pid })
  const [k, setK] = useState(inicial)
  useEffect(() => { setK(inicial) }, [inicial])
  const decidir = useMutation({
    mutationFn: (b) => (b.deshacer ? api.delete(`/gestion/decisiones/${b.deshacer}`) : api.post('/gestion/decisiones', b)),
    onSuccess: (r, b) => {
      ['gestion-evidencia', 'gestion-proyecto', 'gestion-proyectos', 'gestion-fuentes'].forEach((q) => qc.invalidateQueries({ queryKey: [q] }))
      toast.success(b.deshacer ? 'Decisión deshecha · recalculado' : `${b.decision === 'si' ? 'Confirmado' : 'Descartado'} · ${r.data.casos_recalculados} caso(s) recalculado(s)`)
    },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo guardar la decisión'),
  })
  const [bajando, setBajando] = useState(false)
  const excel = async () => {
    setBajando(true)
    try {
      const r = await api.get(`/gestion/proyectos/${pid}/evidencia/excel`, { responseType: 'blob' })
      const nombre = (r.headers?.['content-disposition'] || '').match(/filename="([^"]+)"/)?.[1] || `evidencia_${pid}.xlsx`
      const url = URL.createObjectURL(r.data)
      const a = document.createElement('a'); a.href = url; a.download = nombre; document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 2000)
      toast.success('Excel de evidencia descargado (queda en Registros)')
    } catch { toast.error('No se pudo generar el Excel') } finally { setBajando(false) }
  }
  const t = ev?.tarjetas?.[k] || ev?.tarjetas?.[ev?.orden?.[0]]
  const puede = !!ev?.puede_decidir
  const onDecidir = (b) => decidir.mutate(b)
  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={(e) => { e.stopPropagation(); onClose() }}>
      <div className="absolute inset-0 bg-black/20" />
      <aside data-panel="evidencia" className="relative bg-white w-full max-w-4xl h-full overflow-y-auto shadow-2xl border-l border-gray-200" onClick={(e) => e.stopPropagation()}>
        <div className="sticky top-0 z-20 bg-white border-b border-gray-200 px-4 py-2.5 flex items-center gap-2">
          <HelpCircle size={18} className="text-blue-700" />
          <div className="min-w-0 flex-1">
            <div className="font-semibold text-gray-800">¿De dónde sale?</div>
            <div className="text-[11px] text-gray-500 truncate">{ev?.caso ? `${ev.caso.codigo || ''} · ${ev.caso.clave} · franja calculada ${fechaHora(ev.calculado_en)}` : ''}</div>
          </div>
          <button type="button" onClick={excel} disabled={bajando || !ev?.orden?.length} data-accion="excel" className="text-xs px-2 py-1 rounded border border-gray-300 hover:border-blue-400 text-blue-700 inline-flex items-center gap-1 disabled:opacity-50"><Download size={13} />{bajando ? 'Generando…' : 'Excel de evidencia'}</button>
          <button type="button" onClick={onClose} className="p-1 rounded hover:bg-gray-100"><X size={18} /></button>
        </div>
        {isLoading ? <div className="p-6 text-sm text-gray-500">Cargando…</div> : isError ? <div className="p-6 text-sm text-red-700">No se pudo leer la evidencia de este caso.</div> : !ev?.orden?.length ? (
          <div className="p-6 text-sm text-gray-500">El caso aún no tiene franja del dinero: recalcularlo.</div>
        ) : (
          <div className="p-4 space-y-3">
            <div className="flex flex-wrap gap-1">
              {ev.orden.map((x) => { const tx = ev.tarjetas[x]; return (
                <button type="button" key={x} data-tarjeta={x} onClick={() => setK(x)} className={`text-[11px] px-2 py-1 rounded border inline-flex items-center gap-1 ${x === t?.k ? 'border-blue-600 bg-blue-50 text-blue-800' : 'border-gray-200 text-gray-600 hover:border-blue-300'}`}>
                  <span className={`w-2 h-2 rounded-full ${tx.ok === false ? 'bg-red-500' : tx.ok ? 'bg-green-500' : 'bg-gray-300'}`} />{tx.titulo}
                </button>) })}
            </div>
            {t && (
              <div className="space-y-3" data-tarjeta-abierta={t.k}>
                <div className="flex flex-wrap items-baseline gap-2">
                  <h3 className="text-base font-bold text-gray-800">{t.titulo}</h3>
                  {t.ok === true && <Chip cls="bg-green-100 text-green-800"><ShieldCheck size={11} />La suma de las filas reproduce la cifra</Chip>}
                  {t.ok === false && <Chip cls="bg-red-100 text-red-700"><AlertTriangle size={11} />La suma no reproduce la cifra</Chip>}
                  {t.ok === null && <Chip cls="bg-gray-100 text-gray-500">sin cifra que comparar</Chip>}
                  {t.gris && <Chip cls="bg-gray-100 text-gray-500">MO de SGS: la valida el liquidador, solo se muestra</Chip>}
                  {t.precio_enel && <Chip cls="bg-emerald-50 text-emerald-800">precio Enel (SAP)</Chip>}
                  {t.provisional && <Chip cls="bg-gray-100 text-gray-500">{t.precio_enel ? 'y costo de SGS (provisional) donde SAP no trae el código' : 'a costo de SGS (provisional)'}</Chip>}
                </div>
                <div className="rounded border border-gray-200 bg-gray-50 px-2.5 py-2 space-y-1.5">
                  <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Fuente</div>
                  <FuenteBloque f={t.fuente} />
                  <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-500 pt-1">Fórmula</div>
                  <div className="text-xs text-gray-800">{t.formula}</div>
                </div>
                <div>
                  <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-500 mb-1">Filas que suman · {t.n_filas}</div>
                  <TablaFilas columnas={t.columnas} filas={t.filas} pid={pid} puede={puede} onDecidir={onDecidir} />
                  <div className="mt-1.5 space-y-0.5" data-suma-pie>
                    {(t.sumas || []).map((s) => (
                      <div key={s.k} className={`text-xs flex flex-wrap gap-x-3 ${s.ok === false ? 'text-red-700 font-semibold' : 'text-gray-700'}`}>
                        <span>{s.etiqueta}: suma de las filas <b>{s.unidad === 'n' ? numero(s.suma) : pesos(s.suma)}</b></span>
                        <span>cifra de la caja <b>{s.cifra === null ? '—' : s.unidad === 'n' ? numero(s.cifra) : pesos(s.cifra)}</b></span>
                        <span>{s.ok === true ? '✓ cuadra al peso' : s.ok === false ? `✗ no cuadra (diferencia ${pesos(s.diferencia)})` : 'sin cifra'}</span>
                      </div>
                    ))}
                  </div>
                </div>
                <Lista titulo={`Pendientes de confirmar · no suman en la caja · ${pesos((t.pendientes || []).reduce((a, x) => a + Number(x.mo || 0), 0))}`} filas={t.pendientes} cls="border-amber-300 bg-amber-50 text-amber-900">
                  <ul className="space-y-1">{t.pendientes?.map((x) => <li key={x.libreto} className="flex flex-wrap gap-x-2 items-start"><span className="font-mono">{x.libreto}</span><span>{x.estado}</span><span>sumaría {pesos(x.mo)}</span><span className="text-gray-500 truncate max-w-[260px]" title={x.descripcion}>{x.descripcion}</span><BotonesDecision acciones={x.acciones} pid={pid} puede={puede} onDecidir={onDecidir} /></li>)}</ul>
                </Lista>
                <Lista titulo="Descartadas por una persona («No es»)" filas={t.descartadas} cls="border-gray-200 bg-gray-50 text-gray-700">
                  <ul className="space-y-1">{t.descartadas?.map((x) => <li key={`${x.libreto}-${x.planilla}`} className="flex flex-wrap gap-x-2 items-start"><span className="font-mono">{x.libreto}</span><span>no es la planilla {x.planilla}</span><span>{x.quien}</span><span>{pesos(x.mo)}{x.sigue_en_caja ? ' (el libreto sigue en la caja por su LCL)' : ''}</span><BotonesDecision acciones={x.acciones} pid={pid} puede={puede} onDecidir={onDecidir} /></li>)}</ul>
                </Lista>
                <Lista titulo="No suman (anulados o devueltos por Enel)" filas={t.no_suman} cls="border-gray-200 bg-white text-gray-600">
                  <ul className="space-y-0.5">{t.no_suman?.map((x) => <li key={x.libreto}><span className="font-mono">{x.libreto}</span> · {x.estado} · {pesos(x.mo)} · {x.motivo}</li>)}</ul>
                </Lista>
                <Lista titulo="Códigos de SCM que SGS no reconoce en este LCL (revisar) · no suman ni generan alerta" filas={t.revisar} cls="border-sky-200 bg-sky-50 text-sky-900">
                  <ul className="space-y-0.5 font-mono">{t.revisar?.map((x, i) => <li key={i}>{x.libreto ? `${x.libreto} · ` : ''}{x.codigo || x.material} · {numero(x.posada ?? x.scm)} {x.unidad || ''} · {x.motivo}</li>)}</ul>
                </Lista>
                <Lista titulo={`Diferencias menores · ${pesos(t.menores_suma)} en todo el caso, menos de ${pesos(t.menores_minimo)}: no son alerta`} filas={t.menores} cls="border-gray-200 bg-gray-50 text-gray-700">
                  <ul className="space-y-0.5" data-lista="menores">{t.menores?.map((x) => <li key={x.codigo}><span className="font-mono">{x.codigo}</span> {x.nombre} · {numero(x.scm)} {x.unidad} en SCM, {numero(x.consumido)} consumidos en SGS · diferencia {numero(x.diferencia)} × {pesos(x.costo)} = <b>{pesos(x.valor)}</b></li>)}</ul>
                  <div className="mt-1 font-semibold" data-menores-suma>Suma de las diferencias menores: {pesos(t.menores_suma)}</div>
                </Lista>
                {(t.extra || []).map((x, i) => (
                  <div key={i} className="text-xs">
                    <div className="font-semibold text-gray-600 mb-1">{x.titulo}</div>
                    <TablaFilas columnas={x.columnas} filas={x.filas} pid={pid} puede={false} onDecidir={onDecidir} />
                    <div className="mt-1 text-gray-600">Suma {pesos(x.suma)} · cifra {x.cifra === null || x.cifra === undefined ? '—' : pesos(x.cifra)}</div>
                  </div>
                ))}
                {(t.notas || []).length > 0 && <ul className="text-[11px] text-gray-500 list-disc pl-4 space-y-0.5">{t.notas.map((n, i) => <li key={i}>{n}</li>)}</ul>}
                <div className="text-[11px] text-gray-500 border-t border-gray-100 pt-2">
                  <b>Confianza del cruce:</b> {Object.entries(ev.confianza || {}).map(([c, d]) => <span key={c} className="mr-2"><Chip cls={CONF_CLS[c]}>{c}</Chip> {d}.</span>)}
                  {!puede && ' Confirmar los cruces lo hace el desarrollador (y facturación cuando se le abra el módulo).'}
                </div>
              </div>
            )}
          </div>
        )}
      </aside>
    </div>
  )
}

// ── (CHANGELOG 2026-10-23) Panel «Fuentes y controles» (cabecera de /gestion; resumido en cada caso) ──────────────────────────────────
// (CHANGELOG 2026-10-25) «Descargar» el archivo de una carga (queda en Registros)
function BotonDescargar({ c }) {
  const [bajando, setBajando] = useState(false)
  const bajar = async () => {
    setBajando(true)
    try {
      const r = await api.get(`/gestion/cargas/${c.id}/archivo`, { responseType: 'blob' })
      const cd = r.headers?.['content-disposition'] || ''
      const n = (cd.match(/filename\*=UTF-8''([^;]+)/) || [])[1]
      const nombre = n ? decodeURIComponent(n) : (cd.match(/filename="([^"]+)"/) || [])[1] || c.archivo_nombre || `fuente_${c.id}`
      const url = URL.createObjectURL(r.data)
      const a = document.createElement('a'); a.href = url; a.download = nombre; document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 2000)
      toast.success('Archivo descargado (queda en Registros)')
    } catch { toast.error('No se pudo descargar el archivo') } finally { setBajando(false) }
  }
  return <button type="button" onClick={bajar} disabled={bajando} data-descargar={c.id} className="text-blue-700 hover:underline inline-flex items-center gap-1 disabled:opacity-50"><Download size={11} />{bajando ? '…' : 'Descargar'}</button>
}

function FuentesPanel() {
  const { data } = useQuery({ queryKey: ['gestion-fuentes'], queryFn: () => api.get('/gestion/fuentes').then((r) => r.data), refetchInterval: 300000 })
  const [abierto, setAbierto] = useState(false)
  if (!data) return null
  const ambar = (data.controles || []).filter((c) => c.estado === 'ambar').length
  return (
    <div className="bg-white border border-gray-200 rounded-lg p-3" data-panel="fuentes">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => setAbierto(!abierto)} className="text-xs font-semibold text-gray-600 uppercase tracking-wide inline-flex items-center gap-1 hover:text-blue-700"><ShieldCheck size={14} />Fuentes y controles</button>
        <span className="text-xs text-gray-500">{data.verdes} en verde{ambar ? ` · ${ambar} pendientes de una persona` : ''}{data.rojos ? ` · ${data.rojos} en rojo` : ''} · {fechaHora(data.calculado_en)}</span>
        <button type="button" onClick={() => setAbierto(!abierto)} className="ml-auto text-xs text-blue-700 hover:underline">{abierto ? 'ocultar' : 'ver exportes y controles'}</button>
      </div>
      <div className="flex flex-wrap gap-1.5 mt-2">
        {(data.controles || []).map((c) => <span key={c.k} title={c.detalle} data-control={c.k} data-estado={c.estado} className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded border border-gray-200 bg-gray-50"><span className={`w-2 h-2 rounded-full ${CTL_CLS[c.estado] || 'bg-gray-300'}`} />{c.titulo}: <b>{c.valor}</b></span>)}
      </div>
      {abierto && (
        <div className="mt-2 space-y-2">
          <table className="w-full text-[11px]">
            <thead className="text-gray-500 text-left"><tr><th className="py-1">Sistema</th><th>Archivo</th><th>Exporte</th><th>Carga</th><th className="text-right">Filas del archivo</th><th className="text-right">Filas en SGO7</th><th className="text-right">Descargar</th></tr></thead>
            <tbody className="divide-y divide-gray-100">
              {(data.sistemas || []).map((s) => s.vivo ? (
                <tr key={s.k}><td className="py-1 font-semibold">{s.titulo}</td><td colSpan={3}>lectura en vivo, solo lectura · {s.casos} casos · de {fechaHora(s.desde)} a {fechaHora(s.hasta)}</td><td /><td /><td /></tr>
              ) : s.k === 'correo' ? (
                <tr key={s.k}><td className="py-1 font-semibold">{s.titulo}</td><td colSpan={3}>{s.agente || 'sin agente'} · leído hasta {fechaHora(s.leido_hasta)} · último contacto {fechaHora(s.ultimo_contacto)}</td><td className="text-right">{Number(s.correos || 0).toLocaleString('es-CO')} correos</td><td /><td /></tr>
              ) : (s.cargas?.length ? s.cargas : [null]).map((c, i) => (
                <tr key={`${s.k}-${i}`} data-sistema={s.k}>
                  <td className="py-1 font-semibold align-top">{i === 0 ? s.titulo : ''}{i === 0 && s.nota ? <div className="font-normal text-[10px] text-gray-500 max-w-[260px]" data-nota={s.k}>{s.nota}</div> : null}</td>
                  <td className="font-mono">{c ? c.archivo : <span className="text-amber-700 font-sans">{s.sin_registro}</span>}</td>
                  <td>{c ? `${s.tipo === 'pantalla' ? 'consulta directa ' : ''}${fechaHora(c.exporte)}` : ''}</td>
                  <td>{c ? `${fechaHora(c.cargado)}${c.usuario ? ` · ${c.usuario}` : ''}` : ''}</td>
                  <td className="text-right">{c ? Number(c.filas || 0).toLocaleString('es-CO') : ''}</td>
                  <td className="text-right">{i === 0 ? Number(s.filas_tabla || 0).toLocaleString('es-CO') : ''}</td>
                  <td className="text-right">{c?.tiene_archivo ? <BotonDescargar c={c} /> : c ? <span className="text-gray-400">sin archivo</span> : ''}</td>
                </tr>
              )))}
            </tbody>
          </table>
          <ul className="text-[11px] text-gray-600 space-y-0.5">{(data.controles || []).map((c) => <li key={c.k}><span className={`inline-block w-2 h-2 rounded-full mr-1 ${CTL_CLS[c.estado] || 'bg-gray-300'}`} /><b>{c.titulo}</b>: {c.valor} · {c.detalle}{c.casos?.length ? ` · ${c.casos.slice(0, 8).map((x) => x.codigo).join(', ')}` : ''}</li>)}</ul>
        </div>
      )}
    </div>
  )
}

function DineroFila({ r }) {
  if (!r || !r.fuentes?.length) return <span className="text-[11px] text-gray-300">—</span>
  const m = (v) => (v === null || v === undefined ? '—' : (Number(v) / 1e6).toLocaleString('es-CO', { maximumFractionDigits: 1 }))
  return (
    <div className="text-[11px] leading-tight">
      <div className="font-mono text-gray-700 whitespace-nowrap" title="MO en millones: PMS · SGS (gris) · Sonny · SCM">{m(r.pms_mo)} · <span className="text-gray-400">{m(r.sgs_mo)}</span> · {m(r.sonny_mo)} · {m(r.scm_mo)}</div>
      <div className="flex flex-wrap gap-1 mt-0.5">
        {r.delta && <Chip cls={r.delta.color === 'verde' ? 'bg-green-50 text-green-700' : 'bg-amber-50 text-amber-800'} title="MO de la planilla Sonny contra la del libreto SCM">Sonny→SCM {deltaTxt(r.delta)}</Chip>}
        {r.mat_proy && PROY[r.mat_proy.estado]?.chip && <Chip cls={PROY[r.mat_proy.estado].chip} title={`Material proyectado de SGS ${millones(r.mat_proy.proyectado)} (liquidado + por liquidar) contra ${millones(r.mat_proy.pms)} en PMS (${signo(r.mat_proy.pct)}${pctTxt(r.mat_proy.pct)})`}><span data-chip="mat-proy" data-estado={r.mat_proy.estado}>MAT vs PMS · {r.mat_proy.estado === 'excede' ? `excede ${pctTxt(r.mat_proy.pct)}` : PROY[r.mat_proy.estado].t.toLowerCase()}</span></Chip>}
        {r.mat_rojo > 0 && <Chip cls="bg-red-100 text-red-700" title="Material posado en SCM que SGS no muestra consumido">MAT SCM&gt;SGS · {r.mat_rojo} cód.</Chip>}
        {r.sap_alertas?.length > 0 && <Chip cls="bg-indigo-100 text-indigo-800" title={r.sap_alertas.map((t) => ALERTA[t] || t).join(' · ')}>MAT SAP · {r.sap_alertas.length}</Chip>}
        {r.cobro?.vencida ? <Chip cls="bg-red-100 text-red-700" title={`DRAPE: factura registrada vencida sin pago · ${millones(r.cobro.vencido)} de esta LCL · ${r.cobro.dias_atraso} días hábiles de atraso`}><span data-chip="cobro" data-estado="vencida">vencida · {millones(r.cobro.vencido)}</span></Chip>
          : ['facturado', 'pagado', 'pago_parcial', 'enviada'].includes(r.cobro?.estado) && <Chip cls={COBRO[r.cobro.estado].chip} title={`DRAPE: ${COBRO[r.cobro.estado].t}${r.cobro.ultima ? ` · ${r.cobro.ultima}` : ''} · facturado ${millones(r.cobro.facturado)} · pagado ${millones(r.cobro.pagado)}`}><span data-chip="cobro" data-estado={r.cobro.estado}>{COBRO[r.cobro.estado].t}</span></Chip>}
        {(Number(r.etapas?.en_curso_n || 0) + Number(r.etapas?.en_enel_n || 0)) > 0 && <Chip cls="bg-violet-50 text-violet-800" title={`Sonny: ${r.etapas.en_curso_n} planilla(s) en curso (${millones(r.etapas.en_curso_mo)}) y ${r.etapas.en_enel_n} en Enel (${millones(r.etapas.en_enel_mo)})${r.etapas.libretos_n ? `, además de ${r.etapas.libretos_n} en libretos` : ''}${r.etapas.sin_planilla ? ` · ${r.etapas.sin_planilla} nodo(s) sin planilla` : ''}`}><span data-chip="sonny-en-curso">en curso {millones(Number(r.etapas.en_curso_mo || 0) + Number(r.etapas.en_enel_mo || 0))}</span></Chip>}
        {Number(r.etapas?.sin_planilla || 0) > 0 && !(Number(r.etapas?.en_curso_n || 0) + Number(r.etapas?.en_enel_n || 0)) && <Chip cls="bg-violet-50 text-violet-700" title="Nodos de Sonny sin planilla (Lista Cruzada de Nodos)"><span data-chip="sonny-sin-planilla">{r.etapas.sin_planilla} nodo(s) sin planilla</span></Chip>}
        {r.evidencia_ok === false && <Chip cls="bg-red-600 text-white" title={`La suma de las filas no reproduce la cifra: ${(r.evidencia_malas || []).join(', ')}`}>cifra sin reproducir</Chip>}
      </div>
    </div>
  )
}

// (CHANGELOG 2026-10-25) los totales del cobro en DRAPE de las facturas que nombran una LCL con caso en Gestión (el total de cada factura entera: DRAPE no lo reparte
// por LCL sin el detalle) y los casos por estado del cobro
function CobroResumen({ c, onVencidas, activo }) {
  if (!c?.hay) return null
  const ca = c.casos || {}
  const n = (v) => Number(v || 0).toLocaleString('es-CO')
  return (
    <div className="bg-white border border-gray-200 rounded-lg px-3 py-2 text-xs text-gray-700 flex flex-wrap gap-x-3 gap-y-1 items-baseline" data-resumen="cobro" title={c.regla || ''}>
      <span className="font-semibold uppercase tracking-wide text-gray-500 flex items-center gap-1"><Receipt size={13} />Cobro · DRAPE</span>
      <span className="text-gray-400">corte {fechaHora(c.corte)} · facturas con LCL de Gestión (total de cada factura)</span>
      <span data-cobro-res="facturado">facturado 2026 <b>{millones(c.facturado_2026)}</b> ({n(c.n_facturado_2026)})</span>
      <span data-cobro-res="pagado">pagado 2026 <b className="text-emerald-700">{millones(c.pagado_2026)}</b> ({n(c.n_pagado_2026)})</span>
      <span data-cobro-res="pendiente">pendiente de pago <b className="text-violet-800">{millones(c.pendiente)}</b> ({n(c.n_pendiente)})</span>
      <button type="button" onClick={onVencidas} data-cobro-res="vencido" className={`rounded px-1 ${activo ? 'bg-red-100 ring-1 ring-red-300' : 'hover:bg-red-50'}`} title={`Registradas sin Fecha de Pago y con el vencimiento pasado (del detalle o estimado: factura + ${c.vence_dias} días). Clic: casos con la alerta.`}>vencido <b className="text-red-700">{millones(c.vencido)}</b> ({n(c.n_vencido)})</button>
      <span data-cobro-res="rechazado">rechazado 2026 <b>{millones(c.rechazado_2026)}</b> ({n(c.n_rechazado_2026)})</span>
      {Number(c.n_enviado || 0) > 0 && <span data-cobro-res="enviado">enviado a Enel {millones(c.enviado)} ({n(c.n_enviado)})</span>}
      <span className="text-gray-500" data-cobro-res="casos">casos: {['facturado', 'pago_parcial', 'pagado', 'enviada', 'sin_factura'].filter((k) => ca[k]).map((k) => `${COBRO[k].t} ${n(ca[k])}`).join(' · ')}{ca.vencida ? ` · con factura vencida ${n(ca.vencida)}` : ''}</span>
    </div>
  )
}

function Kpi({ label, valor, sub, cls, onClick, activo }) {
  return (
    <button type="button" onClick={onClick} className={`text-left bg-white border rounded-lg p-3 ${activo ? 'border-blue-500 ring-1 ring-blue-300' : 'border-gray-200'} ${onClick ? 'hover:border-blue-300' : 'cursor-default'}`}>
      <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide">{label}</div>
      <div className={`text-2xl font-bold ${cls || 'text-gray-800'}`}>{valor}</div>
      {sub && <div className="text-xs text-gray-500">{sub}</div>}
    </button>
  )
}

// Rótulo que aparece al instante al pasar el ratón por un cuadrito (el title del navegador tarda y a veces no sale).
function Rotulo({ n, info, extra }) {
  return (
    <span className="pointer-events-none hidden group-hover:flex group-focus-visible:flex absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 z-30 flex-col items-start whitespace-nowrap bg-gray-900 text-white text-[11px] leading-tight px-2 py-1.5 rounded shadow-lg">
      <span><b>Paso {n}</b>{info?.actor ? <span className="text-gray-300"> · {ACTOR[info.actor] || info.actor}</span> : null}</span>
      <span>{info?.corto || ''}</span>
      {extra ? <span className="text-gray-300">{extra}</span> : null}
    </span>
  )
}

function Barra({ p, pasos, alto = 'h-2.5' }) {
  // 39 segmentos en 3 grupos. Relleno suave hasta el paso más lejano alcanzado; el paso actual en color fuerte.
  const grupos = [[1, 7], [8, 22], [23, 39]]
  return (
    <div className="flex gap-1 w-full min-w-[180px]">
      {grupos.map(([a, b], gi) => (
        <div key={gi} className="flex gap-px flex-1" style={{ flexGrow: b - a + 1 }}>
          {Array.from({ length: b - a + 1 }, (_, i) => a + i).map((n) => {
            const alcanzado = p.max_paso && n <= p.max_paso
            const actual = p.paso_actual === n
            const info = pasos?.[n]
            return (
              <div key={n} className="group relative flex-1">
                <div className={`${alto} w-full rounded-[2px] ${actual ? PILAR[gi + 1].bar + ' ring-2 ring-offset-1 ring-gray-700' : alcanzado ? PILAR[gi + 1].soft : 'bg-gray-100'}`} />
                <Rotulo n={n} info={info} extra={actual ? 'paso actual' : alcanzado ? 'alcanzado' : 'sin llegar'} />
              </div>
            )
          })}
        </div>
      ))}
    </div>
  )
}

function Chip({ cls, children, title }) {
  return <span title={title} className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] font-medium whitespace-nowrap ${cls}`}>{children}</span>
}

function ChipBtn({ cls, children, title, onClick, activo }) {
  return <button type="button" title={title} onClick={onClick} className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] font-medium whitespace-nowrap ${cls} ${activo ? 'ring-2 ring-gray-700' : 'hover:ring-1 hover:ring-gray-400'}`}>{children}</button>
}

function BarraFiltro({ p, pasos, porPaso, pasoActivo, onPaso }) {
  // La misma barra de 39 pasos, pero cada paso es un botón: color pleno si tiene eventos, suave si solo se alcanzó, gris si no.
  // Al pasar el ratón sale un rótulo encima del cuadrito y la línea de abajo dice siempre qué paso es.
  const [hov, setHov] = useState(null)
  const grupos = [[1, 7], [8, 22], [23, 39]]
  const mostrado = hov || pasoActivo || p.paso_actual
  const infoM = mostrado ? pasos?.[mostrado] : null
  return (
    <div>
      <div className="flex gap-1 w-full">
        {grupos.map(([a, b], gi) => (
          <div key={gi} className="flex gap-px flex-1" style={{ flexGrow: b - a + 1 }}>
            {Array.from({ length: b - a + 1 }, (_, i) => a + i).map((n) => {
              const eventos = porPaso[n] || 0
              const alcanzado = p.max_paso && n <= p.max_paso
              const actual = p.paso_actual === n
              const activo = pasoActivo === n
              const info = pasos?.[n]
              return (
                <button type="button" key={n} onClick={() => eventos && onPaso(n)} onMouseEnter={() => setHov(n)} onMouseLeave={() => setHov(null)} onFocus={() => setHov(n)} onBlur={() => setHov(null)}
                  aria-label={`Paso ${n} · ${info?.corto || ''}`}
                  className={`group relative h-5 flex-1 rounded-[2px] ${eventos ? PILAR[gi + 1].bar : alcanzado ? PILAR[gi + 1].soft : 'bg-gray-100'} ${activo ? 'ring-2 ring-offset-1 ring-blue-700' : actual ? 'ring-2 ring-offset-1 ring-gray-700' : ''} ${eventos ? 'cursor-pointer hover:opacity-80' : 'cursor-default'}`}>
                  <Rotulo n={n} info={info} extra={eventos ? `${eventos} evento(s) · toca para filtrar` : actual ? 'paso actual' : alcanzado ? 'alcanzado, sin eventos' : 'sin llegar'} />
                </button>
              )
            })}
          </div>
        ))}
      </div>
      <div className="mt-1.5 text-xs text-gray-700 min-h-[18px]">
        {mostrado ? <><b>Paso {mostrado}</b> · {infoM?.corto}{infoM?.actor ? <span className="text-gray-500"> · {ACTOR[infoM.actor] || infoM.actor}</span> : null}<span className="text-gray-400"> · {porPaso[mostrado] ? `${porPaso[mostrado]} evento(s)` : 'sin eventos'}{hov ? '' : ' · pasa el ratón por la barra para ver cada paso'}</span></> : null}
      </div>
    </div>
  )
}

function Alertas({ alertas, max = 3, cerrado }) {
  // (CHANGELOG 2026-10-25) un caso cerrado en SCM (todas sus LCL en CONCLUSA) sigue mostrando sus alertas, con el rótulo para que la persona responsable decida
  const rot = cerrado ? <span data-rotulo="cerrado-scm"><Chip cls="bg-emerald-50 text-emerald-800 border border-emerald-200" title="Todas sus LCL figuran CONCLUSA en SCM (concluida según SCM; si eso equivale a pagado está por confirmar con FBL1N). Sus alertas siguen para que decidas.">caso cerrado en SCM</Chip></span> : null
  if (!alertas?.length) return rot || <span className="text-xs text-gray-400">—</span>
  return (
    <div className="flex flex-wrap gap-1">
      {rot}
      {alertas.slice(0, max).map((a, i) => <Chip key={i} cls={a.nivel === 'info' ? 'bg-amber-100 text-amber-800' : 'bg-red-100 text-red-700'} title={a.texto}><AlertTriangle size={11} />{ALERTA[a.tipo] || a.tipo}{a.dias ? ` · ${a.dias} d` : ''}</Chip>)}
      {alertas.length > max && <Chip cls="bg-red-50 text-red-600">+{alertas.length - max}</Chip>}
    </div>
  )
}

function CorreoModal({ eventoId, onClose }) {
  const { data, isLoading } = useQuery({ queryKey: ['gestion-correo', eventoId], queryFn: () => api.get(`/gestion/eventos/${eventoId}/correo`).then((r) => r.data), enabled: !!eventoId })
  if (!eventoId) return null
  const doc = data ? `<!doctype html><html><head><meta charset="utf-8"><style>body{font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#1b1f24;margin:0;padding:14px}img{max-width:100%}</style></head><body>${data.html || `<pre style="white-space:pre-wrap">${(data.texto || '').replace(/</g, '&lt;')}</pre>`}</body></html>` : ''
  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-start justify-center p-4 overflow-auto" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-xl w-full max-w-4xl" onClick={(e) => e.stopPropagation()}>
        <div className="px-4 py-3 border-b border-gray-200 flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="font-semibold text-gray-800 truncate">{data?.asunto || 'Correo'}</div>
            {data && <div className="text-xs text-gray-500 mt-0.5"><b>De:</b> {data.de}{data.de_correo ? ` <${data.de_correo}>` : ''} · <b>Para:</b> {data.para || '—'} · {fechaHora(data.fecha)} · {data.carpeta}</div>}
            {data?.adjuntos?.length > 0 && <div className="text-xs text-gray-500 mt-0.5"><b>Adjuntos:</b> {data.adjuntos.join(' · ')}</div>}
          </div>
          <button type="button" onClick={onClose} className="p-1 rounded hover:bg-gray-100"><X size={18} /></button>
        </div>
        {isLoading ? <div className="p-6 text-sm text-gray-500">Cargando…</div>
          : <iframe title="Correo" sandbox="" srcDoc={doc} className="w-full bg-white rounded-b-lg" style={{ height: '70vh' }} />}
      </div>
    </div>
  )
}

function Editable({ valor, onGuardar, placeholder }) {
  const [edit, setEdit] = useState(false)
  const [v, setV] = useState(valor || '')
  if (!edit) return <span className="inline-flex items-center gap-1">{valor || <span className="text-gray-400">{placeholder}</span>}<button type="button" className="text-gray-400 hover:text-blue-600" onClick={() => { setV(valor || ''); setEdit(true) }}><Pencil size={12} /></button></span>
  return (
    <span className="inline-flex items-center gap-1">
      <input className="border border-gray-300 rounded px-1 py-0.5 text-sm w-44" value={v} onChange={(e) => setV(e.target.value)} />
      <button type="button" className="text-green-600" onClick={() => { onGuardar(v); setEdit(false) }}><Check size={14} /></button>
      <button type="button" className="text-gray-400" onClick={() => setEdit(false)}><X size={14} /></button>
    </span>
  )
}

function Detalle({ id, pasos, onClose, onAbrir }) {
  const qc = useQueryClient()
  const { data: p, isLoading } = useQuery({ queryKey: ['gestion-proyecto', id], queryFn: () => api.get(`/gestion/proyectos/${id}`).then((r) => r.data), enabled: !!id })
  // Todo lo que se ve es filtro: pilar, paso de la barra, fuente, actor y quién escribió. Se tocan para activar y de nuevo para quitar.
  const [filtro, setFiltro] = useState({ fuente: '', pilar: 0, paso: null, actor: null, de: null })
  const toggle = (k, v) => setFiltro((f) => ({ ...f, [k]: f[k] === v ? (k === 'pilar' ? 0 : k === 'fuente' ? '' : null) : v }))
  const limpiar = () => setFiltro({ fuente: '', pilar: 0, paso: null, actor: null, de: null })
  const fuente = filtro.fuente
  const [correo, setCorreo] = useState(null)
  const [evid, setEvid] = useState(null)              // (CHANGELOG 2026-10-23) tarjeta de evidencia abierta («¿De dónde sale?»)
  const [verTodo, setVerTodo] = useState(false)
  const editar = useMutation({
    mutationFn: (body) => api.patch(`/gestion/proyectos/${id}`, body),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['gestion-proyecto', id] }); qc.invalidateQueries({ queryKey: ['gestion-proyectos'] }); toast.success('Guardado') },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo guardar'),
  })
  const base = useMemo(() => (p ? p.eventos.filter((e) => verTodo || e.paso || e.directo) : []), [p, verTodo])
  const eventos = useMemo(() => base.filter((e) =>
    (!filtro.fuente || e.fuente === filtro.fuente) && (!filtro.pilar || e.pilar === filtro.pilar) && (!filtro.paso || e.paso === filtro.paso)
    && (!filtro.actor || e.actor === filtro.actor) && (!filtro.de || e.de === filtro.de)), [base, filtro])
  const porPilar = useMemo(() => {
    const g = { 1: [], 2: [], 3: [], 0: [] }
    eventos.forEach((e) => g[e.pilar || 0].push(e))
    return g
  }, [eventos])
  const porPaso = useMemo(() => { const m = {}; base.forEach((e) => { if (e.paso) m[e.paso] = (m[e.paso] || 0) + 1 }); return m }, [base])
  const conteoPilar = useMemo(() => { const m = { 1: 0, 2: 0, 3: 0 }; base.forEach((e) => { if (e.pilar) m[e.pilar] += 1 }); return m }, [base])
  const hayFiltro = !!(filtro.fuente || filtro.pilar || filtro.paso || filtro.actor || filtro.de)
  return (
    <div className="fixed inset-0 z-40 flex justify-end" onClick={onClose}>
      <div className="absolute inset-0 bg-black/30" />
      <aside className="relative bg-gray-50 w-full max-w-3xl h-full overflow-y-auto shadow-2xl" onClick={(e) => e.stopPropagation()}>
        {isLoading || !p ? <div className="p-6 text-sm text-gray-500">Cargando…</div> : (
          <div className="p-4 space-y-4">
            <div className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <div className="text-xs font-mono text-gray-500 flex flex-wrap gap-x-2 items-baseline">
                  <span className="font-semibold text-gray-800 text-sm">{p.codigo?.startsWith('DF') ? p.codigo : `LCL ${p.codigo || ''}`}</span>
                  <span className="text-gray-600">{p.padre?.codes?.length > 1 ? p.padre.codes.join(' + ') : p.clave}</span>
                  {p.padre?.codes?.length > 1 && <Chip cls="bg-sky-100 text-sky-800" title="SGS partió este trabajo en varios proyectos padre del mismo supervisor; aquí se ven juntos">{p.padre.codes.length} padres en SGS</Chip>}
                  {p.padre?.name && <span className="text-gray-400 truncate max-w-[280px]" title={p.padre.name}>{p.padre.name}</span>}
                </div>
                <h2 className="text-lg font-bold text-gray-800 leading-tight mt-0.5">{p.nombre}</h2>
                <div className="text-xs font-mono text-gray-400 flex flex-wrap gap-x-2 mt-0.5">
                  {p.codigos?.pdl?.slice(0, 6).map((c) => <span key={c}>{c}</span>)}
                  {p.codigos?.pdl?.length > 6 && <span>+{p.codigos.pdl.length - 6} planes</span>}
                </div>
                <div className="text-sm text-gray-600 mt-1 flex flex-wrap gap-x-4 gap-y-1 items-center">
                  <Chip cls="bg-gray-200 text-gray-700">{TIPO[p.tipo] || p.tipo}</Chip>
                  {p.origen && p.origen !== 'correo' && <Chip cls="bg-amber-50 text-amber-800" title="El caso no nació de un correo: lo creó la carga de este sistema porque el código existe en SGS">nació de {ORIGEN[p.origen] || p.origen}</Chip>}
                  <span>Zona: <Editable valor={p.zona} placeholder="sin zona" onGuardar={(v) => editar.mutate({ zona: v })} /></span>
                  <span>Supervisor: <Editable valor={p.supervisor} placeholder="sin supervisor" onGuardar={(v) => editar.mutate({ supervisor: v })} /></span>
                  {p.padre?.supervisor && <span className="text-xs text-gray-400">(en SGS: {p.padre.supervisor})</span>}
                </div>
                {p.hermanos?.length > 0 && (
                  <div className="mt-2 text-xs bg-amber-50 border border-amber-200 rounded px-2 py-1.5 text-amber-900">
                    <b>Otro proyecto con el mismo LCL en SGS</b> (otro supervisor; no se mezclan):
                    {p.hermanos.map((h) => (
                      <button type="button" key={h.id} onClick={() => onAbrir && onAbrir(h.id)} className="ml-2 underline hover:text-amber-700">
                        {h.clave} · {TIPO[h.tipo] || h.tipo} · {h.supervisor || 'sin supervisor'}{h.paso_actual ? ` · paso ${h.paso_actual}` : ''}{h.total_mo ? ` · ${millones(h.total_mo)}` : ''}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <Link to={`/gestion/${p.id}`} className="text-xs text-blue-700 hover:underline whitespace-nowrap mt-1" data-enlace="trabajo">Ver como trabajo</Link>
              <button type="button" onClick={onClose} className="p-1 rounded hover:bg-gray-200"><X size={20} /></button>
            </div>

            <div className="bg-white border border-gray-200 rounded-lg p-3">
              <div className="flex items-center justify-between text-xs mb-1">
                {[1, 2, 3].map((pi) => (
                  <button type="button" key={pi} onClick={() => toggle('pilar', pi)} title="Filtrar la línea de tiempo por este pilar"
                    className={`font-semibold px-1.5 py-0.5 rounded ${PILAR[pi].text} ${filtro.pilar === pi ? 'ring-2 ring-gray-700 bg-white' : 'hover:bg-white'}`}>
                    {pi} · {PILAR[pi].corto} <span className="font-normal text-gray-400">{conteoPilar[pi]}</span>
                  </button>
                ))}
              </div>
              <BarraFiltro p={p} pasos={pasos} porPaso={porPaso} pasoActivo={filtro.paso} onPaso={(n) => toggle('paso', n)} />
              <div className="text-[11px] text-gray-400 mt-0.5">Toca un pilar o un paso con eventos para filtrar la línea de tiempo. Color pleno = tiene eventos; suave = solo se alcanzó.</div>
              <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="text-sm"><b>Va en el paso {p.paso_actual ?? '—'}</b>{p.estado_texto ? ` · ${p.estado_texto}` : ''}</span>
                {p.paso_fecha && <span className="text-xs text-gray-500">{fechaHora(p.paso_fecha)} · {hace(p.paso_fecha)} · fuente {FUENTE[p.fuente_paso]?.label || p.fuente_paso}</span>}
                {p.max_paso && p.max_paso !== p.paso_actual && <span className="text-xs text-gray-500">Alcanzó hasta el paso {p.max_paso}</span>}
              </div>
              {p.cerrado === true && <div className="mt-2 text-xs rounded border border-emerald-200 bg-emerald-50 px-2 py-1 text-emerald-900" data-cerrado-scm>
                <b>Caso cerrado en SCM:</b> todas sus LCL figuran CONCLUSA (exporte del {fecha(p.cerrado_en)}). Concluida según SCM; si eso equivale a pagado está por confirmar con FBL1N.{p.alertas?.length ? ' Sus alertas siguen abajo para que decidas.' : ''}</div>}
              {p.alertas?.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {p.alertas.map((a, i) => <li key={i} className={`text-sm flex items-start gap-1.5 ${a.nivel === 'info' ? 'text-amber-800' : 'text-red-700'}`} data-alerta={a.tipo} data-sap={a.sap || ''} data-nivel={a.nivel || ''}><AlertTriangle size={14} className="mt-0.5 shrink-0" /><span>{a.texto}{a.sap && <span className={`ml-1.5 text-[10px] px-1.5 py-0.5 rounded whitespace-nowrap ${a.sap === 'confirmada' ? 'bg-red-100 text-red-800' : 'bg-gray-100 text-gray-600'}`} data-chip="sap-estado">{SAP_ESTADO[a.sap] || a.sap}</span>}{conTarjeta(a) && p.dinero?.cajas && <button type="button" data-evidencia={`alerta:${a.tipo}`} onClick={() => setEvid(`alerta:${a.tipo}`)} className="ml-1.5 text-xs text-blue-700 hover:underline whitespace-nowrap">¿De dónde sale?</button>}</span></li>)}
                </ul>
              )}
            </div>

            <Franja d={p.dinero} sgsLeido={p.sgs?.consultado_en} onEvidencia={(k) => setEvid(k)} onFuente={(k, activar) => setFiltro((f) => ({ ...f, fuente: activar ? k : '' }))} />

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div className="bg-white border border-gray-200 rounded-lg p-3">
                <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">SGS · MO liquidada (lectura en vivo)</div>
                {p.sgs?.padre ? (
                  <div className="text-sm space-y-2">
                    <div className="rounded border border-emerald-300 bg-emerald-50/50 px-2 py-1.5">
                      <div className="flex flex-wrap items-baseline gap-x-2">
                        <span className="font-mono text-xs text-gray-600">{(p.sgs.padre.codes || [p.sgs.padre.code]).join(' + ')}</span>
                        {p.sgs.padre.zona && <Chip cls="bg-gray-100 text-gray-600">{p.sgs.padre.zona}</Chip>}
                        <b className="ml-auto text-emerald-800">{millones(p.sgs.total_mo)}</b>
                      </div>
                      <div className="text-xs text-gray-600">{p.sgs.padre.supervisor || 'sin supervisor en SGS'} · {p.sgs.nodos} nodos · {p.sgs.n_lotes} liquidaciones{p.sgs.ultima_liq ? ` · última ${fecha(p.sgs.ultima_liq.ts)} (${millones(p.sgs.ultima_liq.mo)})` : ' · sin MO liquidada'}</div>
                      {p.sgs.padre.detalle?.length > 1 && <div className="text-xs text-gray-600 font-mono">{p.sgs.padre.detalle.map((d) => `${d.code} ${millones(d.mo_total)} · ${d.nodos} nodos`).join('  ·  ')}</div>}
                      <div className="text-[11px] text-gray-400 mt-0.5">MO acumulada: suma de todas las liquidaciones de la familia de este padre. No es una sola planilla.</div>
                    </div>
                    {p.sgs_lotes?.length > 0 && (
                      <details className="text-xs">
                        <summary className="cursor-pointer text-blue-700">Ver las {p.sgs_lotes.length} liquidaciones</summary>
                        <ul className="mt-1 max-h-48 overflow-auto font-mono space-y-0.5">
                          {p.sgs_lotes.map((l, i) => <li key={i} className="text-gray-700">{fecha(l.ts)} · {l.padre} · {millones(l.mo)} · {l.n} avances · {(l.usuarios || []).join(', ')}</li>)}
                        </ul>
                      </details>
                    )}
                  </div>
                ) : <div className="text-sm text-gray-400">El código no existe como proyecto en SGS o aún no se ha leído.</div>}
              </div>
              <div className="bg-white border border-gray-200 rounded-lg p-3">
                <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Sonny · Balance de Proyectos</div>
                {p.facturacion?.length ? (
                  <ul className="space-y-1">
                    {p.facturacion.map((c) => <li key={c.caso_id} className="text-sm flex items-center gap-2"><span className={`w-2.5 h-2.5 rounded-full ${c.semaforo === 'verde' ? 'bg-green-500' : c.semaforo === 'ambar' ? 'bg-amber-400' : c.semaforo === 'rojo' ? 'bg-red-500' : 'bg-gray-300'}`} />Caso {c.caso_id} · planilla {c.id_planilla} · <b>{c.estado}</b>{c.orden_estado ? ` · orden ${c.orden_estado}` : ''}<Link to="/facturacion" className="text-blue-600 hover:underline inline-flex items-center gap-0.5 text-xs">abrir<ExternalLink size={11} /></Link></li>)}
                  </ul>
                ) : <div className="text-sm text-gray-400">Sin casos en el Balance Integrado.</div>}
              </div>
            </div>

            <div className="flex flex-wrap gap-2 items-center text-xs">
              <span className="text-gray-500">Fuentes:</span>
              {Object.entries(FUENTE).map(([k, f]) => <button type="button" key={k} onClick={() => toggle('fuente', k)} className={`px-2 py-0.5 rounded border ${fuente === k ? 'border-gray-700 ' + f.cls : 'border-gray-200 bg-white text-gray-600'}`}>{f.label} {p.fuentes?.[k] || 0}</button>)}
              <label className="ml-auto inline-flex items-center gap-1 text-gray-600"><input type="checkbox" checked={verTodo} onChange={(e) => setVerTodo(e.target.checked)} />mostrar citados sin paso</label>
            </div>
            {hayFiltro && (
              <div className="flex flex-wrap gap-1.5 items-center text-xs bg-white border border-gray-300 rounded-lg px-2.5 py-1.5">
                <span className="text-gray-500 font-semibold">Filtros activos:</span>
                {filtro.pilar ? <ChipBtn cls={PILAR[filtro.pilar].chip} onClick={() => toggle('pilar', filtro.pilar)}>Pilar {filtro.pilar} · {PILAR[filtro.pilar].corto} <X size={11} /></ChipBtn> : null}
                {filtro.paso ? <ChipBtn cls={PILAR[pilarDe(filtro.paso)].chip} onClick={() => toggle('paso', filtro.paso)}>Paso {filtro.paso} · {pasos?.[filtro.paso]?.corto} <X size={11} /></ChipBtn> : null}
                {filtro.fuente ? <ChipBtn cls={FUENTE[filtro.fuente].cls} onClick={() => toggle('fuente', filtro.fuente)}>{FUENTE[filtro.fuente].label} <X size={11} /></ChipBtn> : null}
                {filtro.actor ? <ChipBtn cls="bg-gray-200 text-gray-700" onClick={() => toggle('actor', filtro.actor)}>{ACTOR[filtro.actor] || filtro.actor} <X size={11} /></ChipBtn> : null}
                {filtro.de ? <ChipBtn cls="bg-gray-200 text-gray-700" onClick={() => toggle('de', filtro.de)}>{filtro.de} <X size={11} /></ChipBtn> : null}
                <span className="text-gray-500 ml-auto">{eventos.length} de {base.length} eventos</span>
                <button type="button" onClick={limpiar} className="text-blue-700 hover:underline">quitar todos</button>
              </div>
            )}
            {hayFiltro && eventos.length === 0 && <div className="text-sm text-gray-500 bg-white border border-gray-200 rounded-lg p-3">Ningún evento cumple esos filtros.</div>}

            {[1, 2, 3].map((pi) => porPilar[pi].length > 0 && (
              <div key={pi}>
                <button type="button" onClick={() => toggle('pilar', pi)} title="Filtrar por este pilar"
                  className={`text-xs font-semibold uppercase tracking-wide mb-1 px-1 rounded ${PILAR[pi].text} ${filtro.pilar === pi ? 'ring-2 ring-gray-700 bg-white' : 'hover:bg-white'}`}>
                  Pilar {pi} · {PILAR[pi].label} · {porPilar[pi].length}
                </button>
                <ol className="space-y-1.5">
                  {porPilar[pi].map((e) => {
                    const f = FUENTE[e.fuente] || FUENTE.sgo7
                    return (
                      <li key={e.id} className={`bg-white border rounded-lg p-2.5 ${e.paso === p.paso_actual && e.id === p.eventos.filter((x) => x.paso).slice(-1)[0]?.id ? 'border-gray-700' : 'border-gray-200'}`}>
                        <div className="flex flex-wrap items-center gap-1.5 text-xs">
                          <ChipBtn cls={f.cls} activo={filtro.fuente === e.fuente} onClick={() => toggle('fuente', e.fuente)} title="Filtrar por fuente"><f.Icon size={11} />{f.label}{e.fuente === 'correo' && !e.directo ? ' · citado' : ''}</ChipBtn>
                          {e.paso && <ChipBtn cls={PILAR[e.pilar].chip} activo={filtro.paso === e.paso} onClick={() => toggle('paso', e.paso)} title="Filtrar por este paso">{e.paso} · {pasos?.[e.paso]?.corto}</ChipBtn>}
                          {e.cond && <Chip cls="bg-amber-100 text-amber-800">{e.cond}</Chip>}
                          <ChipBtn cls="bg-gray-100 text-gray-600" activo={filtro.actor === e.actor} onClick={() => toggle('actor', e.actor)} title="Filtrar por actor">{ACTOR[e.actor] || e.actor}</ChipBtn>
                          <span className="text-gray-400 font-mono ml-auto">{fechaHora(e.ts)}</span>
                        </div>
                        <div className="text-sm font-medium text-gray-800 mt-1">{e.prueba}</div>
                        <div className="text-xs text-gray-500 mt-0.5 truncate">
                          {e.de ? <button type="button" onClick={() => toggle('de', e.de)} title="Filtrar por quien escribió" className={`font-semibold hover:underline ${filtro.de === e.de ? 'text-blue-700' : 'text-gray-700'}`}>{e.de}</button> : null}
                          {e.para ? ` → ${e.para}` : ''}{e.asunto ? ` · ${e.asunto}` : ''}
                        </div>
                        {e.texto && <div className="text-xs text-gray-600 mt-1 line-clamp-2">{e.texto}</div>}
                        {(e.tiene_html || (e.fuente === 'correo' && e.texto)) && <button type="button" onClick={() => setCorreo(e.id)} className="mt-1 text-xs text-blue-600 hover:underline inline-flex items-center gap-1"><Mail size={11} />Ver correo</button>}
                        {e.adjuntos?.length > 0 && <div className="text-[11px] text-gray-500 mt-0.5 font-mono truncate">Adjuntos: {e.adjuntos.filter((a) => !/^image\d+|^Outlook-/i.test(a)).join(' · ')}</div>}
                      </li>
                    )
                  })}
                </ol>
              </div>
            ))}
            {porPilar[0].length > 0 && verTodo && (
              <div>
                <div className="text-xs font-semibold uppercase tracking-wide mb-1 text-gray-500">Correos del hilo sin paso reconocido · {porPilar[0].length}</div>
                <ol className="space-y-1">
                  {porPilar[0].map((e) => <li key={e.id} className="bg-white border border-gray-200 rounded p-2 text-xs"><span className="font-mono text-gray-400">{fechaHora(e.ts)}</span> · <b>{e.de}</b> · {e.asunto} {e.tiene_html && <button type="button" className="text-blue-600 hover:underline ml-1" onClick={() => setCorreo(e.id)}>ver</button>}</li>)}
                </ol>
              </div>
            )}

            <div className="bg-white border border-gray-200 rounded-lg p-3">
              <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Abrir en los módulos</div>
              <div className="flex flex-wrap gap-2">
                {p.enlaces.map((l) => <Link key={l.url} to={l.url} className="text-sm px-2 py-1 rounded border border-gray-200 hover:border-blue-400 text-blue-700 inline-flex items-center gap-1">{l.label}<ExternalLink size={12} /></Link>)}
              </div>
            </div>
            <div className="text-xs text-gray-400">Creado {fechaHora(p.creado_en)} · {p.n_eventos} eventos · {p.n_correos} correos directos</div>
          </div>
        )}
      </aside>
      <CorreoModal eventoId={correo} onClose={() => setCorreo(null)} />
      {evid && <EvidenciaPanel pid={id} inicial={evid} onClose={() => setEvid(null)} />}
    </div>
  )
}

export default function GestionPage() {
  // (CHANGELOG 2026-10-25) la vista de operador vive en /gestion/lista; el caso abierto va en la ruta (/gestion/lista/:id) y el filtro de alerta
  // puede llegar por ?alerta= (desde «Lo que frena el cobro» de la zona). La vista de directivos está en /gestion.
  const usuario = useAuthStore((s) => s.usuario)
  const qc = useQueryClient()
  const navegar = useNavigate()
  const { id: idRuta } = useParams()
  const [buscarParams] = useSearchParams()
  const [q, setQ] = useState('')
  const [zona, setZona] = useState('')
  const [sup, setSup] = useState('')
  const [pilar, setPilar] = useState(0)
  const [alerta, setAlerta] = useState(() => buscarParams.get('alerta') || '')
  const [tipo, setTipo] = useState('')
  const [origen, setOrigen] = useState('')
  const [cierre, setCierre] = useState('')          // (CHANGELOG 2026-10-25) '' todos · 'no' abiertos · 'si' cerrados en SCM · 'sin' sin LCL en SCM
  const [rechazo, setRechazo] = useState(() => buscarParams.get('rechazo') || '')     // (CHANGELOG 2026-10-25) categoría del motivo del rechazo (?rechazo= desde la zona)
  const sel = idRuta ? Number(idRuta) : null
  const setSel = (x) => navegar(x ? `/gestion/lista/${x}${window.location.search}` : `/gestion/lista${window.location.search}`)
  const [verFlujo, setVerFlujo] = useState(false)
  const [verN, setVerN] = useState(200)
  const { data: meta } = useQuery({ queryKey: ['gestion-pasos'], queryFn: () => api.get('/gestion/pasos').then((r) => r.data), staleTime: 3600000 })
  const pasos = useMemo(() => Object.fromEntries((meta?.pasos || []).map((p) => [p.n, p])), [meta])
  const { data: res, refetch: refRes } = useQuery({ queryKey: ['gestion-resumen'], queryFn: () => api.get('/gestion/resumen').then((r) => r.data), refetchInterval: 120000 })
  const { data: lista = [], isLoading, refetch } = useQuery({
    queryKey: ['gestion-proyectos', zona, sup, pilar, alerta, tipo, origen, cierre, rechazo],
    queryFn: () => api.get('/gestion/proyectos', { params: { zona: zona || undefined, supervisor: sup || undefined, pilar: pilar || undefined, alerta: alerta || undefined, tipo: tipo || undefined, origen: origen || undefined, cerrado: cierre || undefined, rechazo: rechazo || undefined, limit: 5000 } }).then((r) => r.data),
    refetchInterval: 120000,
  })
  const recalcular = useMutation({ mutationFn: () => api.post('/gestion/recalcular', null, { params: { fondo: true } }), onSuccess: (r) => { toast.success(`Recalculando ${r.data.proyectos} proyectos en segundo plano (tarda unos minutos)`); setTimeout(() => { refetch(); refRes() }, 60000) }, onError: () => toast.error('No se pudo recalcular') })
  const filtrada = useMemo(() => {
    const t = q.trim().toLowerCase()
    if (!t) return lista
    return lista.filter((p) => [p.clave, p.codigo, p.nombre, p.supervisor, p.zona, JSON.stringify(p.codigos || {})].join(' ').toLowerCase().includes(t))
  }, [lista, q])
  useEffect(() => { setVerN(200) }, [q, zona, sup, pilar, alerta, tipo, origen, cierre, rechazo])
  const visibles = filtrada.slice(0, verN)
  const agente = res?.agentes?.[0]
  const esDev = usuario?.rol === 'desarrollador'

  return (
    <div className="p-4 md:p-6 space-y-4">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-bold text-gray-800 flex items-center gap-2"><Workflow size={22} className="text-blue-700" />Gestión General</h1>
          <p className="text-sm text-gray-600 mt-0.5">Cada proyecto con su paso en el flujo de Dominion (FL-03: levantamiento, ejecución y facturación), leído del correo, SGS, Sonny, PMS, SCM y los módulos de SGO7, y su dinero de lo presupuestado a lo cobrado. Las alertas dicen qué está frenado y hace cuánto.</p>
          <p className="text-xs mt-1"><Link to="/gestion" className="text-blue-700 hover:underline" data-enlace="zona">Vista para directivos (Zona)</Link> · esta es la vista de operador: la lista completa, los filtros y la operación.</p>
          {res?.cargas && <p className="text-[11px] text-gray-400 mt-0.5">Exportes cargados: PMS {res.cargas.pms ? fecha(res.cargas.pms) : '—'} · Sonny {res.cargas.sonny ? fecha(res.cargas.sonny) : '—'} · SCM {res.cargas.scm ? fecha(res.cargas.scm) : '—'} · LCL de SCM {res.cargas.scm_lcl ? fecha(res.cargas.scm_lcl) : '—'} · deuda de facturación {res.cargas.deuda ? fecha(res.cargas.deuda) : '—'}{'drape' in res.cargas ? ` · facturas ${res.cargas.drape ? fecha(res.cargas.drape) : '—'}` : ''}</p>}
        </div>
        <div className="flex items-center gap-2 text-xs text-gray-500">
          {agente ? <span title={`cursor ${fechaHora(agente.cursor)}`}>Correo leído hasta {fechaHora(agente.cursor)} · último contacto {hace(agente.ultimo_contacto)}</span> : <span className="text-amber-700">Agente de correo sin conexión aún</span>}
          <button type="button" onClick={() => { refetch(); refRes() }} className="p-1.5 rounded border border-gray-200 bg-white hover:bg-gray-50" title="Actualizar"><RefreshCw size={14} /></button>
          {esDev && <button type="button" onClick={() => recalcular.mutate()} disabled={recalcular.isPending} className="px-2 py-1.5 rounded border border-gray-200 bg-white hover:bg-gray-50">Recalcular</button>}
          <button type="button" onClick={() => setVerFlujo(!verFlujo)} className="px-2 py-1.5 rounded border border-gray-200 bg-white hover:bg-gray-50 inline-flex items-center gap-1"><Info size={13} />FL-03</button>
        </div>
      </div>

      <FuentesPanel />

      {verFlujo && meta && (
        <div className="bg-white border border-gray-200 rounded-lg p-3 grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
          {[1, 2, 3].map((pi) => (
            <div key={pi}>
              <div className={`font-semibold uppercase tracking-wide mb-1 ${PILAR[pi].text}`}>Pilar {pi} · {PILAR[pi].label}</div>
              <ol className="space-y-0.5">{meta.pasos.filter((p) => pilarDe(p.n) === pi).map((p) => <li key={p.n} className="flex gap-2"><span className="font-mono text-gray-400 w-5 text-right">{p.n}</span><span className="text-gray-700">{p.corto}</span><span className="text-gray-400 ml-auto">{meta.actores[p.actor] || p.actor}</span></li>)}</ol>
            </div>
          ))}
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
        <Kpi label="Proyectos activos" valor={res?.total ?? '—'} sub={`${res?.terminados || 0} con libretos aprobados${res?.sin_correo ? ` · ${res.sin_correo} sin correo` : ''}${res?.cierre ? ` · ${res.cierre.cerrados} cerrados en SCM` : ''}`} onClick={() => { setPilar(0); setAlerta(''); setOrigen(''); setCierre(''); setRechazo('') }} activo={!pilar && !alerta && !origen && !cierre && !rechazo} />
        <Kpi label="En levantamiento" valor={res?.por_pilar?.[1] ?? '—'} cls={PILAR[1].text} onClick={() => setPilar(pilar === 1 ? 0 : 1)} activo={pilar === 1} />
        <Kpi label="En ejecución" valor={res?.por_pilar?.[2] ?? '—'} cls={PILAR[2].text} onClick={() => setPilar(pilar === 2 ? 0 : 2)} activo={pilar === 2} />
        <Kpi label="En facturación" valor={res?.por_pilar?.[3] ?? '—'} cls={PILAR[3].text} onClick={() => setPilar(pilar === 3 ? 0 : 3)} activo={pilar === 3} />
        <Kpi label="Con alertas" valor={res?.con_alerta ?? '—'} cls="text-red-700" sub={res?.alertas?.sin_liquidar ? `${res.alertas.sin_liquidar} sin liquidar` : undefined} onClick={() => setAlerta(alerta ? '' : 'sin_liquidar')} activo={!!alerta} />
        <Kpi label="Zonas" valor={Object.keys(res?.zonas || {}).filter((z) => z !== '—').length || '—'} sub={Object.entries(res?.zonas || {}).map(([z, n]) => `${z} ${n}`).join(' · ')} />
      </div>
      <CobroResumen c={res?.cobro} onVencidas={() => setAlerta(alerta === 'factura_vencida_sin_pago' ? '' : 'factura_vencida_sin_pago')} activo={alerta === 'factura_vencida_sin_pago'} />

      <div className="flex flex-wrap gap-2 items-center">
        <div className="relative"><Search size={14} className="absolute left-2 top-2.5 text-gray-400" /><input value={q} onChange={(e) => setQ(e.target.value)} placeholder="LCL, PDL, DF, nombre, supervisor…" className="pl-7 pr-2 py-1.5 border border-gray-300 rounded text-sm w-72" /></div>
        <select value={zona} onChange={(e) => setZona(e.target.value)} className="border border-gray-300 rounded text-sm py-1.5 px-2"><option value="">Todas las zonas</option>{Object.keys(res?.zonas || {}).filter((z) => z !== '—').sort().map((z) => <option key={z} value={z}>{z}</option>)}</select>
        <select value={sup} onChange={(e) => setSup(e.target.value)} className="border border-gray-300 rounded text-sm py-1.5 px-2 max-w-[260px]"><option value="">Todos los supervisores</option>{(res?.supervisores || []).map(([s, n]) => <option key={s} value={s}>{s} ({n})</option>)}</select>
        <select value={tipo} onChange={(e) => setTipo(e.target.value)} className="border border-gray-300 rounded text-sm py-1.5 px-2"><option value="">Todos los tipos</option>{Object.entries(TIPO).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <select value={alerta} onChange={(e) => setAlerta(e.target.value)} className="border border-gray-300 rounded text-sm py-1.5 px-2"><option value="">Todas las alertas</option>{Object.entries(ALERTA).map(([k, v]) => <option key={k} value={k}>{v}{res?.alertas?.[k] ? ` (${res.alertas[k]})` : ''}</option>)}</select>
        <select value={origen} onChange={(e) => setOrigen(e.target.value)} aria-label="Origen del caso" className="border border-gray-300 rounded text-sm py-1.5 px-2">
          <option value="">Todos los orígenes</option>
          <option value="correo">Con correo{res?.por_origen?.correo ? ` (${res.por_origen.correo})` : ''}</option>
          <option value="sin_correo">Sin correo{res?.sin_correo ? ` (${res.sin_correo})` : ''}</option>
          {['sonny', 'pms', 'scm'].map((k) => <option key={k} value={k}>Nació de {ORIGEN[k]}{res?.por_origen?.[k] ? ` (${res.por_origen[k]})` : ''}</option>)}
        </select>
        <select value={cierre} onChange={(e) => setCierre(e.target.value)} aria-label="Cierre en SCM" data-filtro="cierre" className="border border-gray-300 rounded text-sm py-1.5 px-2" title="Cerrado = todas las LCL del caso en CONCLUSA en SCM (concluida según SCM)">
          <option value="">Abiertos y cerrados</option>
          <option value="no">Abiertos en SCM{res?.cierre ? ` (${res.cierre.abiertos})` : ''}</option>
          <option value="si">Cerrados en SCM{res?.cierre ? ` (${res.cierre.cerrados})` : ''}</option>
          <option value="sin">Sin LCL en SCM{res?.cierre ? ` (${res.cierre.sin_scm})` : ''}</option>
        </select>
        <select value={rechazo} onChange={(e) => setRechazo(e.target.value)} aria-label="Motivo del rechazo" data-filtro="rechazo" className="border border-gray-300 rounded text-sm py-1.5 px-2 max-w-[260px]" title="Motivo del rechazo de Enel (texto de Sonny clasificado por reglas; SCM no exporta el motivo de la devolución)">
          <option value="">Todos los motivos de rechazo</option>
          {rechazo && !(res?.motivos_rechazo || []).some((m) => m.k === rechazo) && <option value={rechazo}>{rechazo}</option>}
          {(res?.motivos_rechazo || []).filter((m) => res?.rechazos?.[m.k] || m.k === rechazo).map((m) => <option key={m.k} value={m.k}>{m.t}{res?.rechazos?.[m.k] ? ` (${res.rechazos[m.k]})` : ''}</option>)}
        </select>
        {usuario?.nombre && <button type="button" onClick={() => setSup(sup ? '' : usuario.nombre.split(' ')[0])} className={`text-sm px-2 py-1.5 rounded border ${sup && usuario.nombre.startsWith(sup) ? 'border-blue-500 bg-blue-50 text-blue-700' : 'border-gray-300 bg-white'}`}>Mis proyectos</button>}
        <span className="text-xs text-gray-500 ml-auto">{filtrada.length} proyecto(s)</span>
      </div>

      <div className="bg-white border border-gray-200 rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide">
            <tr>
              <th className="text-left px-3 py-2">Proyecto</th>
              <th className="text-left px-3 py-2">Supervisor · zona</th>
              <th className="text-left px-3 py-2 w-[240px]">Avance (39 pasos)</th>
              <th className="text-left px-3 py-2">Paso actual</th>
              <th className="text-left px-3 py-2" title="MO en millones: PMS · SGS (gris) · Sonny · SCM">Dinero (MO, M$) · SGS</th>
              <th className="text-left px-3 py-2">Alertas</th>
              <th className="text-right px-3 py-2">Actividad</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {isLoading && <tr><td colSpan={7} className="px-3 py-6 text-center text-gray-500">Cargando…</td></tr>}
            {!isLoading && filtrada.length === 0 && <tr><td colSpan={7} className="px-3 py-8 text-center text-gray-500">Sin proyectos con ese filtro. {res?.total === 0 && 'Cuando el agente de correo del PC haga su primera pasada, los proyectos aparecen aquí solos.'}</td></tr>}
            {visibles.map((p) => (
              <tr key={p.id} className="hover:bg-blue-50/40 cursor-pointer" onClick={() => setSel(p.id)}>
                <td className="px-3 py-2 max-w-[320px]">
                  <div className="font-mono text-xs text-gray-500 flex items-center gap-1.5 flex-wrap">
                    <span className="text-gray-700">{p.codigo || p.clave}</span><span className="text-gray-400">{p.codigo ? p.clave : ''}</span>
                    <Chip cls="bg-gray-100 text-gray-600">{TIPO[p.tipo] || p.tipo}</Chip>
                    {p.hermanos_n > 0 && <Chip cls="bg-amber-100 text-amber-800" title="Hay otro proyecto padre en SGS con el mismo LCL, de otro supervisor">mismo LCL ×{p.hermanos_n + 1}</Chip>}
                    {p.origen && p.origen !== 'correo' && <Chip cls="bg-amber-50 text-amber-800" title="Nació de esta carga, no de un correo">sin correo · {ORIGEN[p.origen] || p.origen}</Chip>}
                  </div>
                  <div className="font-medium text-gray-800 truncate">{p.nombre}</div>
                </td>
                <td className="px-3 py-2 text-gray-700"><div className="truncate max-w-[200px]">{p.supervisor || <span className="text-gray-400">—</span>}</div><div className="text-xs text-gray-500">{p.zona || ''}</div></td>
                <td className="px-3 py-2"><Barra p={p} pasos={pasos} /></td>
                <td className="px-3 py-2">
                  {p.paso_actual ? <><Chip cls={PILAR[p.pilar]?.chip}>{p.paso_actual} · {PILAR[p.pilar]?.corto}</Chip><div className="text-xs text-gray-700 mt-0.5 max-w-[240px] truncate" title={p.estado_texto}>{p.estado_texto}</div><div className="text-[11px] text-gray-400">{hace(p.paso_fecha)} · {FUENTE[p.fuente_paso]?.label}</div></> : <span className="text-gray-400 text-xs">Sin paso</span>}
                </td>
                <td className="px-3 py-2 text-xs">
                  {p.dinero_resumen?.fuentes?.length
                    ? <DineroFila r={p.dinero_resumen} />
                    : p.sgs?.padre ? <div className="text-gray-500" title="MO acumulada de todas las liquidaciones de este padre en SGS (no es una sola planilla)">MO acumulada {millones(p.sgs.total_mo)}</div>
                    : <div className="text-gray-400">No está en SGS</div>}
                  {p.sgs?.padre && <div className="text-[11px] text-gray-400 whitespace-nowrap">{p.sgs.n_lotes} liquidaciones{p.sgs.ultima_liq ? ` · últ. ${fecha(p.sgs.ultima_liq.ts)}` : ''}</div>}
                  {p.facturacion?.length > 0 && <div className="text-violet-800">{p.facturacion.length} caso(s) Balance · {p.facturacion[p.facturacion.length - 1].estado}</div>}
                </td>
                <td className="px-3 py-2"><Alertas alertas={p.alertas} cerrado={p.cerrado === true} /></td>
                <td className="px-3 py-2 text-right text-xs text-gray-500 whitespace-nowrap">{fecha(p.ultima_actividad)}<div className="text-gray-400">{p.n_correos} correos · {p.n_eventos} ev.</div></td>
              </tr>
            ))}
          </tbody>
        </table>
        {filtrada.length > visibles.length && (
          <div className="px-3 py-2 text-center border-t border-gray-100">
            <button type="button" onClick={() => setVerN(verN + 200)} className="text-sm text-blue-700 hover:underline">Mostrar 200 más ({visibles.length} de {filtrada.length})</button>
          </div>
        )}
      </div>
      {sel && <Detalle id={sel} pasos={pasos} onClose={() => setSel(null)} onAbrir={(id) => setSel(id)} />}
    </div>
  )
}
