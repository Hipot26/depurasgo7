import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import toast from 'react-hot-toast'
import api from '../utils/api'
import { useAuthStore } from '../store/authStore'
import { Scale, RefreshCw, Search, ChevronRight, ChevronDown, Clock, Download, Send, PauseCircle, PlayCircle, Archive, RotateCcw, FileSpreadsheet, ExternalLink, XCircle } from 'lucide-react'

// Línea de facturación SGO7 (2026-10-10): cada consulta del Balance Integrado queda guardada como un CASO
// (planilla SIPWEBORA + proyecto SGS) con su semáforo. El facturador ve aquí en qué va cada caso, lo deja
// en espera de almacén (el sistema lo reconsulta solo cada 2 h), lo reconsulta a mano, y cuando cuadra y ya
// tiene Excel lo manda a liquidar: la orden aparece en el panel del Liquidador Automático.

const TZ = 'America/Bogota'
function fechaHora(iso) {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleString('es-CO', { timeZone: TZ, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) } catch { return iso }
}
function fechaCorta(ymd) {
  if (!ymd) return '—'
  const [y, m, d] = String(ymd).split('-'); return d && m ? `${d}/${m}/${y}` : ymd
}
function mesClave(iso) {
  try { return new Date(iso).toLocaleDateString('en-CA', { timeZone: TZ }).slice(0, 7) } catch { return '' }
}

const ESTADOS = {
  consultado:     { label: 'Consultado',            cls: 'bg-gray-100 text-gray-700' },
  descuadre:      { label: 'Con descuadre',         cls: 'bg-red-100 text-red-700' },
  espera_almacen: { label: 'Espera de almacén',     cls: 'bg-amber-100 text-amber-800' },
  cuadra:         { label: 'Cuadra',                cls: 'bg-green-100 text-green-700' },
  enviado:        { label: 'Enviado a liquidar',    cls: 'bg-blue-100 text-blue-700' },
  en_liquidacion: { label: 'En liquidación',        cls: 'bg-indigo-100 text-indigo-700' },
  liquidado:      { label: 'Liquidado',             cls: 'bg-emerald-100 text-emerald-800' },
  con_fallos:     { label: 'Liquidado con fallos',  cls: 'bg-orange-100 text-orange-800' },
  archivado:      { label: 'Archivado',             cls: 'bg-gray-100 text-gray-500' },
}
const FILTROS = [
  ['', 'Todos'],
  ['descuadre,espera_almacen', 'Pendientes de almacén'],
  ['cuadra', 'Listos para liquidar'],
  ['enviado,en_liquidacion', 'En el Liquidador'],
  ['liquidado,con_fallos', 'Liquidados'],
  ['archivado', 'Archivados'],
]
const SEM = { verde: 'bg-green-500', ambar: 'bg-amber-400', rojo: 'bg-red-500' }
const EVENTO = {
  consulta: 'Consulta', cuadro: 'Ya cuadra', estado_manual: 'Cambio de estado', excel_generado: 'Excel generado',
  orden_enviada: 'Mandado a liquidar', orden_en_liquidacion: 'El Liquidador lo tomó', orden_liquidada: 'Liquidado en SGS',
  orden_con_fallos: 'Liquidación con fallos', orden_cancelada: 'Orden cancelada', reconsulta_error: 'Error al reconsultar',
}

function rutaReconsulta(c) {
  const q = `id_planilla=${encodeURIComponent(String(c.id_planilla || '').replace(/\+/g, ','))}&proyecto_id=${encodeURIComponent(c.proyecto_id || c.parent_id || '')}&auto=1`
  return `/modulos/balance?ruta=${encodeURIComponent(`/balance/?${q}`)}`
}

function Kpi({ label, valor, sub, cls }) {
  return (
    <div className="bg-white border border-gray-200 rounded-lg p-3">
      <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide">{label}</div>
      <div className={`text-2xl font-bold ${cls || 'text-gray-800'}`}>{valor}</div>
      {sub && <div className="text-xs text-gray-500">{sub}</div>}
    </div>
  )
}

function Semaforo({ c }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      <span className={`inline-block w-2.5 h-2.5 rounded-full ${SEM[c.semaforo] || 'bg-gray-300'}`} />
      <span className="text-green-700 font-semibold">{c.ok ?? 0} OK</span>
      <span className={c.rojos ? 'text-red-700 font-semibold' : 'text-gray-400'}>{c.rojos ?? 0} rojos</span>
      {c.alertas > 0 && <span className="text-amber-700">{c.alertas} alertas</span>}
    </span>
  )
}

async function descargarExcel(c) {
  try {
    const r = await api.get(`/sgo7/casos/${c.id}/excel`, { responseType: 'blob' })
    const url = URL.createObjectURL(r.data)
    const a = document.createElement('a'); a.href = url; a.download = c.excel_archivo || `balance_caso_${c.id}.xlsx`; a.click()
    setTimeout(() => URL.revokeObjectURL(url), 2000)
  } catch { toast.error('El Excel de este caso no está disponible') }
}

function DetalleCaso({ c, puedeEditar, onCambio }) {
  const qc = useQueryClient()
  const { data } = useQuery({ queryKey: ['sgo7-caso', c.id], queryFn: async () => (await api.get(`/sgo7/casos/${c.id}`)).data })
  const [nota, setNota] = useState(c.nota || '')
  const [notaOrden, setNotaOrden] = useState('')
  const res = c.resumen || {}
  const rojas = res.filas_rojas || []
  const alertas = res.alertas || []
  const advert = res.advertencias || []
  const invalidar = () => { qc.invalidateQueries({ queryKey: ['sgo7-casos'] }); qc.invalidateQueries({ queryKey: ['sgo7-caso', c.id] }); onCambio && onCambio() }
  const guardarNota = useMutation({
    mutationFn: async () => (await api.patch(`/sgo7/casos/${c.id}`, { nota })).data,
    onSuccess: () => { toast.success('Nota guardada'); invalidar() },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo guardar'),
  })
  const mandar = useMutation({
    mutationFn: async () => (await api.post(`/sgo7/casos/${c.id}/orden`, { nota: notaOrden })).data,
    onSuccess: () => { toast.success('Orden enviada al Liquidador Automático'); setNotaOrden(''); invalidar() },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo enviar la orden'),
  })
  const puedeMandar = puedeEditar && c.excel_ruta && !['pendiente', 'en_liquidacion'].includes(c.orden_estado || '') && !['en_liquidacion', 'liquidado'].includes(c.estado)
  const eventos = data?.eventos || []
  return (
    <div className="bg-gray-50 border-t border-gray-200 px-4 py-3 text-sm">
      <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2 lg:grid-cols-4 text-xs text-gray-700 mb-3">
        <div><span className="text-gray-500">Proyecto SGS:</span> {c.proyecto_nombre || '—'} {c.pro_code ? `· ${c.pro_code}` : ''} {c.parent_id ? `· id ${c.parent_id}` : ''}</div>
        <div><span className="text-gray-500">LCL / OdM / Ticket:</span> {c.lcl || '—'} · {c.odm || '—'} · {c.ticket || '—'}</div>
        <div><span className="text-gray-500">Zona · empresa:</span> {c.zona || '—'} · {c.empresa || '—'}</div>
        <div><span className="text-gray-500">Nodos · MO · MAT:</span> {res.nodos ?? '—'} · {res.total_mo ?? '—'} · {res.total_mat ?? '—'}</div>
        <div><span className="text-gray-500">Creado:</span> {fechaHora(c.creado_en)} {c.creado_por ? `· ${c.creado_por}` : ''}</div>
        <div><span className="text-gray-500">Reconsulta automática:</span> {c.reconsulta_auto ? `sí · próxima ${fechaHora(c.proxima_reconsulta)}` : 'no'}</div>
        {c.ultimo_error && <div className="sm:col-span-2 text-red-700"><span className="text-gray-500">Último error al reconsultar:</span> {c.ultimo_error}</div>}
        {c.excel_archivo && <div className="sm:col-span-2"><span className="text-gray-500">Excel:</span> {c.excel_archivo} · {fechaHora(c.excel_generado_en)} {c.excel_generado_por ? `· ${c.excel_generado_por}` : ''} · fecha ejecución {fechaCorta(c.fecha_ejecucion)}
          {(c.lideres || []).length > 0 && <> · líderes: {c.lideres.map((l) => `${l.nombre} ${l.pct}%`).join(', ')}</>}
          <button onClick={() => descargarExcel(c)} className="ml-2 text-blue-700 hover:underline inline-flex items-center gap-1"><Download className="w-3.5 h-3.5" /> descargar</button></div>}
        {c.orden_estado && <div className="sm:col-span-2"><span className="text-gray-500">Orden:</span> <b>{c.orden_estado_label}</b> · enviada {fechaHora(c.orden_enviada_en)} por {c.orden_enviada_por || '—'}
          {c.orden_atendida_por ? ` · la tomó ${c.orden_atendida_por} ${fechaHora(c.orden_atendida_en)}` : ''}
          {c.orden_resultado ? ` · resultado: ${c.orden_resultado.ok ?? 0} OK / ${c.orden_resultado.fallidos ?? 0} fallidos` : ''}
          {c.orden_nota ? <> · <i>{c.orden_nota}</i></> : null}
          {c.orden_sesion && ['liquidada', 'con_fallos'].includes(c.orden_estado) && <a href={`/liquidador2/resultado/${c.orden_sesion}`} target="_blank" rel="noopener noreferrer" className="ml-2 text-blue-700 hover:underline inline-flex items-center gap-1"><ExternalLink className="w-3.5 h-3.5" /> resultado</a>}
        </div>}
      </div>

      {(advert.length > 0 || alertas.length > 0) && (
        <div className="mb-3 text-xs">
          {advert.map((a, i) => <div key={`a${i}`} className="text-amber-800 bg-amber-50 border border-amber-200 rounded px-2 py-1 mb-1"><b>{a.titulo || 'Advertencia'}:</b> {a.mensaje || ''}</div>)}
          {alertas.map((a, i) => <div key={`l${i}`} className={`rounded px-2 py-1 mb-1 border ${String(a).startsWith('CRITICO') ? 'text-red-800 bg-red-50 border-red-200' : 'text-amber-800 bg-amber-50 border-amber-200'}`}>{a}</div>)}
        </div>
      )}

      {rojas.length > 0 && (
        <div className="mb-3">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Filas en rojo ({rojas.length}{rojas.length >= 80 ? '+' : ''}) — lo que almacén / SIP deben corregir</div>
          <div className="overflow-auto max-h-64 bg-white border border-gray-200 rounded">
            <table className="w-full text-xs">
              <thead className="bg-gray-100 text-gray-600"><tr><th className="text-left px-2 py-1">Nodo</th><th className="text-left px-2 py-1">Código</th><th className="text-left px-2 py-1">Descripción</th><th className="text-right px-2 py-1">SIP</th><th className="text-right px-2 py-1">SGS</th><th className="text-right px-2 py-1">Dif.</th><th className="text-left px-2 py-1">Estado</th></tr></thead>
              <tbody>{rojas.map((f, i) => (
                <tr key={i} className="border-t border-gray-100"><td className="px-2 py-1">{f.nodo}</td><td className="px-2 py-1 font-semibold">{f.codigo}</td><td className="px-2 py-1 max-w-[260px] truncate" title={f.desc}>{f.desc}</td>
                  <td className="px-2 py-1 text-right">{f.sip ?? '—'}</td><td className="px-2 py-1 text-right">{f.sgs ?? '—'}</td><td className="px-2 py-1 text-right">{f.dif ?? '—'}</td><td className="px-2 py-1 text-red-700 font-semibold">{f.estado}</td></tr>))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {puedeEditar && (
        <div className="flex flex-wrap gap-2 items-start mb-3">
          <textarea value={nota} onChange={(e) => setNota(e.target.value)} rows={2} maxLength={600} placeholder="Nota del caso (ej. reportado a almacén por WhatsApp el 10/10, Juan dijo que lo carga mañana)" className="flex-1 min-w-[280px] border border-gray-300 rounded px-2 py-1.5 text-sm" />
          <button onClick={() => guardarNota.mutate()} disabled={guardarNota.isPending} className="bg-gray-700 hover:bg-gray-800 text-white text-xs font-semibold px-3 py-2 rounded">Guardar nota</button>
        </div>
      )}

      {puedeMandar && (
        <div className="bg-white border border-green-200 rounded-lg p-3 mb-3">
          <div className="font-semibold text-gray-800 text-sm mb-1 flex items-center gap-2"><Send className="w-4 h-4 text-green-700" /> Mandar a liquidar</div>
          <p className="text-xs text-gray-600 mb-2">Se envía el Excel generado ({c.excel_archivo}) al panel del Liquidador Automático.{c.semaforo !== 'verde' && <b className="text-red-700"> Este balance no cuadra o tiene alertas: la observación es obligatoria.</b>}</p>
          <div className="flex flex-wrap gap-2 items-start">
            <textarea value={notaOrden} onChange={(e) => setNotaOrden(e.target.value)} rows={2} maxLength={600} placeholder="Observación para quien liquida" className="flex-1 min-w-[280px] border border-gray-300 rounded px-2 py-1.5 text-sm" />
            <button onClick={() => { if (confirm(`¿Mandar el caso #${c.id} (planilla ${c.id_planilla}) al Liquidador Automático?`)) mandar.mutate() }} disabled={mandar.isPending || (c.semaforo !== 'verde' && !notaOrden.trim())} className="bg-green-700 hover:bg-green-800 disabled:opacity-50 text-white text-sm font-semibold px-4 py-2 rounded inline-flex items-center gap-2"><Send className="w-4 h-4" /> Mandar a liquidar</button>
          </div>
        </div>
      )}
      {puedeEditar && !c.excel_ruta && ['cuadra', 'descuadre', 'espera_almacen', 'consultado'].includes(c.estado) && (
        <p className="text-xs text-gray-600 mb-3">Para mandarlo a liquidar primero hay que generar el Excel (fecha + líderes) en el Balance: <Link to={rutaReconsulta(c)} className="text-blue-700 hover:underline">reconsultar y generar</Link>.</p>
      )}

      <div>
        <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Línea de tiempo</div>
        <ul className="text-xs text-gray-700 space-y-0.5 max-h-56 overflow-auto">
          {eventos.map((e) => {
            const d = e.detalle || {}
            let extra = ''
            if (e.tipo === 'consulta') extra = `${d.origen === 'auto' ? 'automática' : 'manual'} · ${d.ok ?? 0} OK / ${d.rojos ?? 0} rojos → ${(ESTADOS[d.estado] || {}).label || d.estado}`
            else if (e.tipo === 'estado_manual') extra = Object.entries(d).map(([k, v]) => `${k}: ${v === true ? 'sí' : v === false ? 'no' : (ESTADOS[v] || {}).label || v}`).join(' · ')
            else if (e.tipo === 'orden_enviada') extra = d.nota ? `«${d.nota}»` : ''
            else if (e.tipo === 'orden_liquidada' || e.tipo === 'orden_con_fallos') extra = `${d.ok ?? 0} OK / ${d.fallidos ?? 0} fallidos`
            else if (e.tipo === 'reconsulta_error') extra = d.error || ''
            else if (e.tipo === 'excel_generado') extra = `${d.archivo || ''} · fecha ${fechaCorta(d.fecha_ejecucion)}`
            return (
              <li key={e.id} className="flex gap-2"><Clock className="w-3.5 h-3.5 text-gray-400 shrink-0 mt-0.5" />
                <span className="text-gray-500 whitespace-nowrap">{fechaHora(e.ts)}</span>
                <span><b>{EVENTO[e.tipo] || e.tipo}</b>{e.usuario ? ` · ${e.usuario}` : ''}{extra ? ` · ${extra}` : ''}</span>
              </li>
            )
          })}
          {eventos.length === 0 && <li className="text-gray-400">Sin eventos todavía.</li>}
        </ul>
      </div>
    </div>
  )
}

export default function FacturacionPage() {
  const { usuario } = useAuthStore()
  const qc = useQueryClient()
  const esDev = usuario?.rol === 'desarrollador'
  const puedeEditar = !!usuario?.puede_facturacion
  const [filtro, setFiltro] = useState('')
  const [q, setQ] = useState('')
  const [abierto, setAbierto] = useState(null)
  const { data, isFetching, refetch } = useQuery({
    queryKey: ['sgo7-casos'],
    queryFn: async () => (await api.get('/sgo7/casos', { params: { limit: 1000 } })).data,
    refetchInterval: 60000,
  })
  const casos = data || []
  const cambiar = useMutation({
    mutationFn: async ({ id, body }) => (await api.patch(`/sgo7/casos/${id}`, body)).data,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['sgo7-casos'] }) },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo cambiar el estado'),
  })
  const cancelarOrden = useMutation({
    mutationFn: async (id) => (await api.post(`/sgo7/casos/${id}/orden/cancelar`)).data,
    onSuccess: () => { toast.success('Orden cancelada'); qc.invalidateQueries({ queryKey: ['sgo7-casos'] }) },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo cancelar'),
  })

  const lista = useMemo(() => {
    const est = filtro ? filtro.split(',') : null
    const qq = q.trim().toLowerCase()
    return casos.filter((c) => (!est || est.includes(c.estado)) &&
      (!qq || [c.id_planilla, c.proyecto_nombre, c.pro_code, c.lcl, c.odm, c.nota, c.ultima_consulta_por, String(c.id)].some((v) => String(v || '').toLowerCase().includes(qq))))
  }, [casos, filtro, q])

  const mes = mesClave(new Date().toISOString())
  const n = (fn) => casos.filter(fn).length
  const kPend = n((c) => ['descuadre', 'espera_almacen'].includes(c.estado))
  const kListos = n((c) => c.estado === 'cuadra')
  const kLiq = n((c) => ['enviado', 'en_liquidacion'].includes(c.estado))
  const kMes = n((c) => ['liquidado', 'con_fallos'].includes(c.estado) && mesClave(c.orden_terminada_en || c.actualizado_en) === mes)
  const kAuto = n((c) => c.reconsulta_auto && ['descuadre', 'espera_almacen'].includes(c.estado))

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex items-center gap-3 mb-1 flex-wrap">
        <Scale className="w-6 h-6 text-blue-700" />
        <h1 className="text-2xl font-bold text-gray-800">Balance de Proyectos</h1>
        <div className="ml-auto flex items-center gap-3">
          <button onClick={() => refetch()} className="text-sm text-blue-700 hover:underline flex items-center gap-1"><RefreshCw className={`w-4 h-4 ${isFetching ? 'animate-spin' : ''}`} /> Actualizar</button>
          <Link to="/modulos/balance" className="bg-blue-700 hover:bg-blue-800 text-white text-sm font-semibold px-4 py-2 rounded-lg inline-flex items-center gap-2"><FileSpreadsheet className="w-4 h-4" /> Nueva consulta</Link>
        </div>
      </div>
      <p className="text-sm text-gray-500 mb-4">
        Cada consulta del Balance Integrado queda guardada como un <b>caso</b> con su semáforo. Si no cuadra, déjalo en espera de almacén: el sistema lo
        reconsulta solo cada 2 horas (lunes a sábado, 6 a 20 h) y lo pasa a «Cuadra» cuando almacén corrige. Con el Excel generado, <b>Mandar a liquidar</b>
        lo pone en el panel del Liquidador Automático y el resultado vuelve aquí.{esDev ? ' Como desarrollador ves los casos de todo el equipo.' : ''}
      </p>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5 mb-5">
        <Kpi label="Pendientes de almacén" valor={kPend} sub={`${kAuto} con reconsulta automática`} cls={kPend ? 'text-red-700' : 'text-gray-800'} />
        <Kpi label="Listos para liquidar" valor={kListos} cls={kListos ? 'text-green-700' : 'text-gray-800'} />
        <Kpi label="En el Liquidador" valor={kLiq} cls="text-blue-700" />
        <Kpi label="Liquidados este mes" valor={kMes} cls="text-emerald-700" />
        <Kpi label="Casos en total" valor={casos.length} />
      </div>

      <div className="flex flex-wrap items-center gap-2 mb-3">
        {FILTROS.map(([v, l]) => (
          <button key={v} onClick={() => setFiltro(v)} className={`text-xs font-semibold px-3 py-1.5 rounded-full border ${filtro === v ? 'bg-blue-700 text-white border-blue-700' : 'bg-white text-gray-700 border-gray-300 hover:border-blue-400'}`}>{l}</button>
        ))}
        <div className="ml-auto relative">
          <Search className="w-4 h-4 text-gray-400 absolute left-2 top-2.5" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar planilla, proyecto, LCL, nota…" className="border border-gray-300 rounded-lg pl-8 pr-3 py-2 text-sm w-72" />
        </div>
      </div>

      <div className="bg-white border border-gray-200 rounded-xl overflow-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500"><tr>
            <th className="w-6"></th><th className="text-left px-3 py-2">Caso</th><th className="text-left px-3 py-2">Planilla</th><th className="text-left px-3 py-2">Proyecto SGS</th>
            <th className="text-left px-3 py-2">LCL</th><th className="text-left px-3 py-2">Estado</th><th className="text-left px-3 py-2">Semáforo</th>
            <th className="text-left px-3 py-2">Última consulta</th><th className="text-right px-3 py-2">Consultas</th><th className="text-left px-3 py-2">Excel</th><th className="text-right px-3 py-2">Acciones</th>
          </tr></thead>
          <tbody>
            {lista.map((c) => {
              const est = ESTADOS[c.estado] || { label: c.estado, cls: 'bg-gray-100 text-gray-600' }
              const open = abierto === c.id
              const enLiq = ['enviado', 'en_liquidacion'].includes(c.estado)
              return (
                <>
                  <tr key={c.id} className="border-t border-gray-100 hover:bg-blue-50/40 cursor-pointer" onClick={() => setAbierto(open ? null : c.id)}>
                    <td className="px-3 py-2 text-gray-400">{open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}</td>
                    <td className="px-3 py-2 font-semibold text-gray-800">#{c.id}</td>
                    <td className="px-3 py-2 font-semibold text-gray-800 whitespace-nowrap">{c.id_planilla}</td>
                    <td className="px-3 py-2 max-w-[240px] truncate text-gray-700" title={`${c.proyecto_nombre || ''} ${c.pro_code || ''}`}>{c.proyecto_nombre || '—'}{c.pro_code ? <span className="text-gray-400"> · {c.pro_code}</span> : null}</td>
                    <td className="px-3 py-2 text-gray-700 whitespace-nowrap">{c.lcl || c.odm || '—'}</td>
                    <td className="px-3 py-2"><span className={`text-xs font-semibold px-2 py-0.5 rounded-full whitespace-nowrap ${est.cls}`}>{est.label}</span>{c.orden_estado && ['liquidada', 'con_fallos'].includes(c.orden_estado) && c.orden_resultado ? <div className="text-[11px] text-gray-500 mt-0.5">{c.orden_resultado.ok ?? 0} OK / {c.orden_resultado.fallidos ?? 0} fallidos</div> : null}</td>
                    <td className="px-3 py-2 text-xs"><Semaforo c={c} /></td>
                    <td className="px-3 py-2 text-xs text-gray-700 whitespace-nowrap">{fechaHora(c.ultima_consulta_en)}<div className="text-gray-400">{c.ultima_consulta_por || '—'}{c.ultima_consulta_origen === 'auto' ? ' · automática' : ''}</div></td>
                    <td className="px-3 py-2 text-right">{c.consultas}{c.reconsulta_auto && ['descuadre', 'espera_almacen'].includes(c.estado) && <span className="ml-1 text-amber-700" title={`Reconsulta automática · próxima ${fechaHora(c.proxima_reconsulta)}`}><RotateCcw className="w-3.5 h-3.5 inline" /></span>}</td>
                    <td className="px-3 py-2 text-xs">{c.excel_ruta ? <button onClick={(e) => { e.stopPropagation(); descargarExcel(c) }} className="text-blue-700 hover:underline inline-flex items-center gap-1" title={c.excel_archivo}><Download className="w-3.5 h-3.5" /> {fechaCorta(c.fecha_ejecucion)}</button> : <span className="text-gray-400">sin generar</span>}</td>
                    <td className="px-3 py-2 text-right whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                      {!enLiq && c.estado !== 'liquidado' && <Link to={rutaReconsulta(c)} className="text-blue-700 hover:underline text-xs inline-flex items-center gap-1 mr-2" title="Vuelve a consultar SIPWEBORA y SGS con los mismos datos"><RotateCcw className="w-3.5 h-3.5" /> Reconsultar</Link>}
                      {puedeEditar && ['descuadre', 'consultado'].includes(c.estado) && <button onClick={() => cambiar.mutate({ id: c.id, body: { estado: 'espera_almacen' } })} className="text-amber-700 hover:underline text-xs inline-flex items-center gap-1 mr-2" title="Reportado a almacén; se reconsulta solo"><PauseCircle className="w-3.5 h-3.5" /> Espera almacén</button>}
                      {puedeEditar && c.estado === 'espera_almacen' && <button onClick={() => cambiar.mutate({ id: c.id, body: { estado: 'descuadre' } })} className="text-gray-700 hover:underline text-xs inline-flex items-center gap-1 mr-2"><PlayCircle className="w-3.5 h-3.5" /> Quitar espera</button>}
                      {puedeEditar && c.estado === 'cuadra' && c.excel_ruta && !['pendiente', 'en_liquidacion'].includes(c.orden_estado || '') && <button onClick={() => setAbierto(c.id)} className="bg-green-700 hover:bg-green-800 text-white text-xs font-semibold px-2.5 py-1 rounded inline-flex items-center gap-1 mr-2"><Send className="w-3.5 h-3.5" /> Mandar a liquidar</button>}
                      {puedeEditar && enLiq && ['pendiente', 'en_liquidacion'].includes(c.orden_estado || '') && <button onClick={() => { if (confirm(`¿Cancelar la orden de liquidación del caso #${c.id}?`)) cancelarOrden.mutate(c.id) }} className="text-red-700 hover:underline text-xs inline-flex items-center gap-1 mr-2"><XCircle className="w-3.5 h-3.5" /> Cancelar orden</button>}
                      {puedeEditar && !enLiq && c.estado !== 'archivado' && <button onClick={() => cambiar.mutate({ id: c.id, body: { estado: 'archivado' } })} className="text-gray-500 hover:underline text-xs inline-flex items-center gap-1" title="Sacar de la lista activa"><Archive className="w-3.5 h-3.5" /> Archivar</button>}
                      {puedeEditar && c.estado === 'archivado' && <button onClick={() => cambiar.mutate({ id: c.id, body: { estado: 'reabrir' } })} className="text-blue-700 hover:underline text-xs inline-flex items-center gap-1">Reabrir</button>}
                    </td>
                  </tr>
                  {open && <tr key={`${c.id}-d`}><td colSpan={11} className="p-0"><DetalleCaso c={c} puedeEditar={puedeEditar} /></td></tr>}
                </>
              )
            })}
            {lista.length === 0 && <tr><td colSpan={11} className="px-3 py-8 text-center text-gray-400">{casos.length === 0 ? 'Todavía no hay casos: la primera consulta en el Balance Integrado crea uno.' : 'Ningún caso con ese filtro.'}</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
