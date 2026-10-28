import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import toast from 'react-hot-toast'
import api from '../utils/api'
import { useAuthStore } from '../store/authStore'
import { Zap, ExternalLink, FileSpreadsheet, ChevronRight, ChevronDown, RefreshCw, AlertTriangle, CheckCircle2, XCircle, Clock, KeyRound, Inbox } from 'lucide-react'

// Liquidador Automático dentro de SGO7: panel de seguimiento construido sobre los Registros
// (cada planilla subida, cada liquidación con su resultado ítem por ítem, ajustes de fecha).
// La operación (subir, verificar, liquidar) sigue en el módulo embebido (/modulos/liquidador).

const TZ = 'America/Bogota'
function fechaHora(iso) {
  try { return new Date(iso).toLocaleString('es-CO', { timeZone: TZ, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' }) }
  catch { return iso }
}
function hora(iso) {
  try { return new Date(iso).toLocaleString('es-CO', { timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit' }) }
  catch { return iso }
}
function mesClave(iso) {
  try { return new Date(iso).toLocaleDateString('en-CA', { timeZone: TZ }).slice(0, 7) } catch { return '' }
}
function fechaCorta(ymd) {
  if (!ymd) return '—'
  const [y, m, d] = String(ymd).split('-'); return d && m ? `${d}/${m}/${y}` : ymd
}
const ESTADO = {
  listo:     { label: 'Terminada',  cls: 'bg-green-100 text-green-700' },
  cancelado: { label: 'Cancelada',  cls: 'bg-amber-100 text-amber-700' },
  error:     { label: 'Error',      cls: 'bg-red-100 text-red-700' },
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

function DetalleLiquidacion({ r, eventos }) {
  const d = r.detalle || {}
  const items = d.items || []
  const [soloErrores, setSoloErrores] = useState(false)
  const lista = soloErrores ? items.filter((i) => !i.ok) : items
  return (
    <div className="bg-gray-50 border-t border-gray-200 px-4 py-3 text-sm">
      <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2 lg:grid-cols-4 text-xs text-gray-700 mb-3">
        <div><span className="text-gray-500">Sesión:</span> <code>{r.sesion}</code></div>
        <div><span className="text-gray-500">Fecha de planilla:</span> {fechaCorta(d.fecha_planilla)}</div>
        <div><span className="text-gray-500">PDL:</span> {d.pdl || '—'} · <span className="text-gray-500">LCL:</span> {d.lcl || '—'} · <span className="text-gray-500">PRO:</span> {d.pro_padre || '—'}</div>
        <div><span className="text-gray-500">Cierre:</span> {d.cierre_modo || '—'}</div>
        {d.clases_error && Object.keys(d.clases_error).length > 0 && (
          <div className="sm:col-span-2"><span className="text-gray-500">Tipos de error:</span> {Object.entries(d.clases_error).map(([k, v]) => `${k} (${v})`).join(' · ')}</div>
        )}
        {d.error_general && <div className="sm:col-span-2 text-red-700">Error general: {d.error_general}</div>}
        {d.diagnostico && <div className="sm:col-span-2"><span className="text-gray-500">Diagnóstico guardado:</span> <code className="break-all">{d.diagnostico}</code></div>}
      </div>
      {eventos.length > 0 && (
        <div className="mb-3">
          <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">Línea de tiempo de la sesión</div>
          <ul className="text-xs text-gray-700 space-y-0.5">
            {eventos.map((e) => (
              <li key={e.id} className="flex gap-2"><Clock className="w-3.5 h-3.5 text-gray-400 shrink-0 mt-0.5" />
                <span className="text-gray-500 whitespace-nowrap">{fechaHora(e.ts)}</span>
                <span><b>{e.accion.replace('liquidador_', '').replace(/_/g, ' ')}</b>{e.usuario ? ` · ${e.usuario}` : ''}
                  {e.accion === 'liquidador_fecha_ajustada' && e.detalle ? ` · ${fechaCorta(e.detalle.fecha_original)} → ${fechaCorta(e.detalle.fecha_aplicada)} (cierre SGS)` : ''}
                  {e.accion === 'liquidador_planilla' && e.detalle ? ` · ${e.archivo || ''} · MO ${e.detalle.filas_mo ?? '?'} · MAT ${e.detalle.filas_mat ?? '?'}` : ''}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="flex items-center gap-3 mb-1">
        <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Ítems enviados a liquidar ({items.length})</div>
        <label className="text-xs text-gray-600 flex items-center gap-1"><input type="checkbox" checked={soloErrores} onChange={(e) => setSoloErrores(e.target.checked)} /> solo fallidos</label>
      </div>
      <div className="overflow-auto max-h-80 bg-white border border-gray-200 rounded">
        <table className="w-full text-xs">
          <thead className="bg-gray-100 text-gray-600"><tr>
            <th className="text-left px-2 py-1">Tipo</th><th className="text-left px-2 py-1">Código</th><th className="text-left px-2 py-1">Nodo / PRO</th>
            <th className="text-right px-2 py-1">Cant.</th><th className="text-left px-2 py-1">Líder</th><th className="text-left px-2 py-1">Resultado</th>
          </tr></thead>
          <tbody>
            {lista.map((i, idx) => (
              <tr key={idx} className="border-t border-gray-100">
                <td className="px-2 py-1"><span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${i.tipo === 'MO' ? 'bg-green-100 text-green-700' : 'bg-blue-100 text-blue-700'}`}>{i.tipo || '?'}</span></td>
                <td className="px-2 py-1 font-semibold">{i.codigo || '—'}</td>
                <td className="px-2 py-1">{i.nodo || i.pro || '—'}</td>
                <td className="px-2 py-1 text-right">{i.cantidad ?? i.qty ?? '—'}</td>
                <td className="px-2 py-1 max-w-[220px] truncate" title={i.lider || i.partner_nombre || ''}>{i.lider || i.partner_nombre || i.partner || '—'}</td>
                <td className="px-2 py-1">{i.ok
                  ? <span className="text-green-700 inline-flex items-center gap-1"><CheckCircle2 className="w-3.5 h-3.5" />{i.nueva ? 'OK (nueva)' : i.omitido ? 'Omitido' : 'OK'}</span>
                  : <span className="text-red-700 inline-flex items-start gap-1" title={i.error || ''}><XCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" /><span className="line-clamp-2 max-w-[360px]">{i.error || i.estado || 'Error'}</span></span>}
                </td>
              </tr>
            ))}
            {lista.length === 0 && <tr><td colSpan={6} className="px-2 py-3 text-center text-gray-400">Sin ítems{soloErrores ? ' fallidos' : ''}.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function MiCuentaSGS() {
  const qc = useQueryClient()
  const { data: cuenta } = useQuery({ queryKey: ['sgo7-sgs-cuenta'], queryFn: async () => (await api.get('/sgo7/sgs-cuenta')).data })
  const [editar, setEditar] = useState(false)
  const [login, setLogin] = useState('')
  const [pass, setPass] = useState('')
  const guardar = useMutation({
    mutationFn: async () => (await api.put('/sgo7/sgs-cuenta', { sgs_login: login.trim(), sgs_password: pass })).data,
    onSuccess: () => { toast.success('Cuenta SGS guardada. El Liquidador la usará en tu próxima subida.'); setEditar(false); setPass(''); qc.invalidateQueries({ queryKey: ['sgo7-sgs-cuenta'] }) },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo guardar'),
  })
  const borrar = useMutation({
    mutationFn: async () => (await api.delete('/sgo7/sgs-cuenta')).data,
    onSuccess: () => { toast.success('Cuenta SGS retirada'); qc.invalidateQueries({ queryKey: ['sgo7-sgs-cuenta'] }) },
  })
  const registrada = !!cuenta?.registrada
  return (
    <div className={`border rounded-xl p-4 mb-5 ${registrada ? 'bg-white border-gray-200' : 'bg-amber-50 border-amber-200'}`}>
      <div className="flex items-start gap-3 flex-wrap">
        <KeyRound className={`w-5 h-5 mt-0.5 ${registrada ? 'text-blue-700' : 'text-amber-700'}`} />
        <div className="flex-1 min-w-[260px]">
          <div className="font-semibold text-gray-800">Mi cuenta SGS</div>
          {registrada ? (
            <p className="text-sm text-gray-600">Liquidas en SGS con <b>{cuenta.sgs_login}</b>. Se guarda cifrada y el Liquidador abre tu sesión al subir una planilla; si SGS la cierra, la reabre solo.
              {cuenta.ultimo_ok && <span className="text-green-700"> · Última sesión OK: {fechaHora(cuenta.ultimo_ok)}</span>}
              {cuenta.ultimo_error && <span className="text-red-700"> · Último error: {cuenta.ultimo_error}</span>}
            </p>
          ) : (
            <p className="text-sm text-amber-800">Registra tu usuario y contraseña de SGS <b>una sola vez</b>: cada persona liquida con su propia cuenta. Sin esto el Liquidador no puede escribir en SGS a tu nombre.</p>
          )}
          {(editar || !registrada) && (
            <form className="mt-2 flex flex-wrap gap-2 items-center" onSubmit={(e) => { e.preventDefault(); guardar.mutate() }}>
              <input value={login} onChange={(e) => setLogin(e.target.value)} placeholder="Usuario SGS (cédula o correo)" className="border border-gray-300 rounded px-2 py-1.5 text-sm w-56" required />
              <input type="password" value={pass} onChange={(e) => setPass(e.target.value)} placeholder="Contraseña SGS" className="border border-gray-300 rounded px-2 py-1.5 text-sm w-48" required autoComplete="new-password" />
              <button type="submit" disabled={guardar.isPending} className="bg-blue-700 hover:bg-blue-800 text-white text-sm font-semibold px-3 py-1.5 rounded">{guardar.isPending ? 'Guardando…' : 'Guardar'}</button>
              {registrada && <button type="button" onClick={() => setEditar(false)} className="text-sm text-gray-600 hover:underline">Cancelar</button>}
            </form>
          )}
        </div>
        {registrada && !editar && (
          <div className="flex gap-3 text-sm">
            <button onClick={() => { setLogin(cuenta.sgs_login || ''); setEditar(true) }} className="text-blue-700 hover:underline">Cambiar</button>
            <button onClick={() => { if (confirm('¿Retirar tu cuenta SGS de SGO7?')) borrar.mutate() }} className="text-red-700 hover:underline">Retirar</button>
          </div>
        )}
      </div>
    </div>
  )
}

// SGO7 línea de facturación (2026-10-10): lo que facturación revisó en el Balance Integrado y mandó a liquidar.
// «Liquidar» abre el módulo con la orden: carga su Excel, verifica contra SGS y, al terminar, el resultado
// vuelve solo al caso del facturador.
const ORDEN = {
  pendiente:      { label: 'Pendiente',       cls: 'bg-amber-100 text-amber-800' },
  en_liquidacion: { label: 'En liquidación',  cls: 'bg-blue-100 text-blue-700' },
  liquidada:      { label: 'Liquidada',       cls: 'bg-green-100 text-green-700' },
  con_fallos:     { label: 'Con fallos',      cls: 'bg-red-100 text-red-700' },
  cancelada:      { label: 'Cancelada',       cls: 'bg-gray-100 text-gray-500' },
}
function OrdenesFacturacion() {
  const qc = useQueryClient()
  const [verCerradas, setVerCerradas] = useState(false)
  const { data } = useQuery({
    queryKey: ['sgo7-ordenes'],
    queryFn: async () => (await api.get('/sgo7/ordenes', { params: { limit: 100 } })).data,
    refetchInterval: 60000,
  })
  const ordenes = data || []
  const activas = ordenes.filter((o) => ['pendiente', 'en_liquidacion', 'con_fallos'].includes(o.orden_estado))
  const cerradas = ordenes.filter((o) => !['pendiente', 'en_liquidacion', 'con_fallos'].includes(o.orden_estado)).slice(0, 10)
  const cancelar = useMutation({
    mutationFn: async (id) => (await api.post(`/sgo7/casos/${id}/orden/cancelar`)).data,
    onSuccess: () => { toast.success('Orden cancelada'); qc.invalidateQueries({ queryKey: ['sgo7-ordenes'] }) },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo cancelar'),
  })
  const lista = verCerradas ? [...activas, ...cerradas] : activas
  const sem = (s) => s === 'verde' ? 'bg-green-500' : s === 'rojo' ? 'bg-red-500' : 'bg-amber-400'
  return (
    <div className="mb-6">
      <div className="flex items-center gap-2 mb-1 flex-wrap">
        <Inbox className="w-5 h-5 text-blue-700" />
        <h2 className="text-lg font-semibold text-gray-800">Órdenes de facturación</h2>
        {activas.length > 0 && <span className="text-xs font-bold bg-amber-100 text-amber-800 px-2 py-0.5 rounded-full">{activas.length} por atender</span>}
        <button onClick={() => setVerCerradas(!verCerradas)} className="ml-auto text-xs text-blue-700 hover:underline">{verCerradas ? 'Ocultar terminadas' : 'Ver terminadas'}</button>
      </div>
      <p className="text-xs text-gray-500 mb-2">Balances que facturación revisó y mandó a liquidar desde el Balance Integrado. «Liquidar» carga el Excel de la orden en el módulo (verifica contra SGS como siempre); el resultado vuelve solo al caso del facturador.</p>
      <div className="bg-white border border-gray-200 rounded-xl overflow-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500"><tr>
            <th className="text-left px-3 py-2">Enviada</th><th className="text-left px-3 py-2">Por</th><th className="text-left px-3 py-2">Planilla</th><th className="text-left px-3 py-2">Proyecto SGS</th>
            <th className="text-left px-3 py-2">LCL</th><th className="text-left px-3 py-2">Fecha ejec.</th><th className="text-left px-3 py-2">Líderes</th><th className="text-left px-3 py-2">Semáforo</th>
            <th className="text-left px-3 py-2">Observación</th><th className="text-left px-3 py-2">Estado</th><th></th>
          </tr></thead>
          <tbody>
            {lista.map((o) => {
              const est = ORDEN[o.orden_estado] || { label: o.orden_estado, cls: 'bg-gray-100 text-gray-600' }
              const ruta = encodeURIComponent(`/liquidador2/orden/${o.id}`)
              return (
                <tr key={o.id} className="border-t border-gray-100">
                  <td className="px-3 py-2 whitespace-nowrap text-gray-700">{fechaHora(o.orden_enviada_en)}</td>
                  <td className="px-3 py-2 text-gray-800">{o.orden_enviada_por || '—'}</td>
                  <td className="px-3 py-2 font-semibold text-gray-800 whitespace-nowrap">{o.id_planilla}</td>
                  <td className="px-3 py-2 max-w-[220px] truncate text-gray-700" title={`${o.proyecto_nombre || ''} ${o.pro_code || ''}`}>{o.proyecto_nombre || '—'}{o.pro_code ? <span className="text-gray-400"> · {o.pro_code}</span> : null}</td>
                  <td className="px-3 py-2 text-gray-700 whitespace-nowrap">{o.lcl || o.odm || '—'}</td>
                  <td className="px-3 py-2 text-gray-700 whitespace-nowrap">{fechaCorta(o.fecha_ejecucion)}</td>
                  <td className="px-3 py-2 text-xs text-gray-700 max-w-[200px] truncate" title={(o.lideres || []).map((l) => `${l.nombre} ${l.pct}%`).join(' · ')}>{(o.lideres || []).map((l) => `${l.nombre} ${l.pct}%`).join(' · ') || '—'}</td>
                  <td className="px-3 py-2 text-xs whitespace-nowrap"><span className={`inline-block w-2.5 h-2.5 rounded-full mr-1 ${sem(o.semaforo)}`} />{o.ok ?? 0} OK · <span className={o.rojos ? 'text-red-700 font-semibold' : ''}>{o.rojos ?? 0} rojos</span>{o.alertas ? ` · ${o.alertas} alertas` : ''}</td>
                  <td className="px-3 py-2 text-xs text-gray-700 max-w-[220px] truncate" title={o.orden_nota || ''}>{o.orden_nota || '—'}</td>
                  <td className="px-3 py-2"><span className={`text-xs font-semibold px-2 py-0.5 rounded-full whitespace-nowrap ${est.cls}`}>{est.label}</span>{o.orden_resultado ? <div className="text-[11px] text-gray-500 mt-0.5">{o.orden_resultado.ok ?? 0} OK / {o.orden_resultado.fallidos ?? 0} fallidos</div> : null}</td>
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    {['pendiente', 'con_fallos'].includes(o.orden_estado) && <Link to={`/modulos/liquidador?ruta=${ruta}`} className="bg-green-700 hover:bg-green-800 text-white text-xs font-semibold px-3 py-1 rounded inline-flex items-center gap-1"><Zap className="w-3.5 h-3.5" /> Liquidar</Link>}
                    {o.orden_estado === 'en_liquidacion' && o.orden_sesion && <a href={`/liquidador2/verificar/${o.orden_sesion}`} target="_blank" rel="noopener noreferrer" className="text-blue-700 hover:underline text-xs inline-flex items-center gap-1"><ExternalLink className="w-3.5 h-3.5" /> Sesión</a>}
                    {o.orden_estado === 'en_liquidacion' && <Link to={`/modulos/liquidador?ruta=${ruta}`} className="ml-2 text-blue-700 hover:underline text-xs">Volver a cargar</Link>}
                    {['liquidada', 'con_fallos'].includes(o.orden_estado) && o.orden_sesion && <a href={`/liquidador2/resultado/${o.orden_sesion}`} target="_blank" rel="noopener noreferrer" className="ml-2 text-blue-700 hover:underline text-xs inline-flex items-center gap-1"><ExternalLink className="w-3.5 h-3.5" /> Resultado</a>}
                    {['pendiente', 'en_liquidacion', 'con_fallos'].includes(o.orden_estado) && <button onClick={() => { if (confirm(`¿Cancelar la orden del caso #${o.id} (planilla ${o.id_planilla})?`)) cancelar.mutate(o.id) }} className="ml-2 text-red-700 hover:underline text-xs">Cancelar</button>}
                  </td>
                </tr>
              )
            })}
            {lista.length === 0 && <tr><td colSpan={11} className="px-3 py-6 text-center text-gray-400">No hay órdenes de facturación pendientes.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function LiquidadorPage() {
  const { usuario } = useAuthStore()
  const esDev = usuario?.rol === 'desarrollador'
  const [abierta, setAbierta] = useState(null)
  const { data, isFetching, refetch } = useQuery({
    queryKey: ['sgo7-liquidador'],
    queryFn: async () => (await api.get('/sgo7/registros', { params: { modulo: 'liquidador', limit: 1000 } })).data,
  })
  const regs = data || []
  const { resultados, planillas, porSesion, mesActual } = useMemo(() => {
    const resultados = regs.filter((r) => r.accion === 'liquidador_resultado')
    const planillas = regs.filter((r) => r.accion === 'liquidador_planilla' || r.accion === 'liquidador_subir_excel')
    const porSesion = {}
    regs.forEach((r) => { if (r.sesion) (porSesion[r.sesion] = porSesion[r.sesion] || []).push(r) })
    Object.values(porSesion).forEach((l) => l.sort((a, b) => a.ts.localeCompare(b.ts)))
    return { resultados, planillas, porSesion, mesActual: mesClave(new Date().toISOString()) }
  }, [regs])
  const delMes = resultados.filter((r) => mesClave(r.ts) === mesActual)
  const okMes = delMes.reduce((s, r) => s + (Number(r.detalle?.ok) || 0), 0)
  const fallMes = delMes.reduce((s, r) => s + (Number(r.detalle?.fallidos) || 0), 0)
  const planMes = planillas.filter((r) => mesClave(r.ts) === mesActual)
  const hashVistos = {}
  const mesLabel = new Date().toLocaleDateString('es-CO', { timeZone: TZ, month: 'long', year: 'numeric' })

  const archivoDe = (r) => r.archivo || (porSesion[r.sesion] || []).find((e) => e.accion === 'liquidador_planilla' || e.accion === 'liquidador_subir_excel')?.archivo || '—'

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex items-center gap-3 mb-1 flex-wrap">
        <Zap className="w-6 h-6 text-blue-700" />
        <h1 className="text-2xl font-bold text-gray-800">Liquidador Automático</h1>
        <div className="ml-auto flex items-center gap-3">
          <button onClick={() => refetch()} className="text-sm text-blue-700 hover:underline flex items-center gap-1"><RefreshCw className={`w-4 h-4 ${isFetching ? 'animate-spin' : ''}`} /> Actualizar</button>
          <Link to="/modulos/liquidador" className="bg-blue-700 hover:bg-blue-800 text-white text-sm font-semibold px-4 py-2 rounded-lg inline-flex items-center gap-2"><FileSpreadsheet className="w-4 h-4" /> Nueva liquidación</Link>
        </div>
      </div>
      <p className="text-sm text-gray-500 mb-4">{esDev
        ? 'Seguimiento de todo lo que se sube y se liquida en SGS: quién, cuándo (hora de Colombia) y qué pasó con cada ítem. Cada subida es un registro distinto, aunque sea el mismo archivo.'
        : 'Lo que TU cuenta sube y liquida en SGS: cuándo (hora de Colombia) y qué pasó con cada ítem. Solo ves tus propias subidas y liquidaciones; cada subida es un registro distinto, aunque sea el mismo archivo.'}</p>

      <MiCuentaSGS />

      <OrdenesFacturacion />

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 mb-5">
        <Kpi label={`Liquidaciones · ${mesLabel}`} valor={delMes.length} sub={`${resultados.length} en total registradas`} />
        <Kpi label="Ítems liquidados OK este mes" valor={okMes} cls="text-green-700" />
        <Kpi label="Ítems fallidos este mes" valor={fallMes} cls={fallMes ? 'text-red-700' : 'text-gray-800'} />
        <Kpi label="Planillas subidas este mes" valor={planMes.length} sub={`${planillas.length} en total`} />
      </div>

      <h2 className="text-lg font-semibold text-gray-800 mb-2">Liquidaciones</h2>
      <div className="bg-white border border-gray-200 rounded-xl overflow-auto mb-6">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500"><tr>
            <th className="w-6"></th><th className="text-left px-3 py-2">Fecha · hora</th><th className="text-left px-3 py-2">Usuario</th><th className="text-left px-3 py-2">Planilla</th>
            <th className="text-left px-3 py-2">PDL / LCL</th><th className="text-left px-3 py-2">Fecha planilla</th><th className="text-right px-3 py-2">Ítems</th><th className="text-right px-3 py-2">OK</th><th className="text-right px-3 py-2">Fallidos</th><th className="text-left px-3 py-2">Estado</th><th></th>
          </tr></thead>
          <tbody>
            {resultados.map((r) => {
              const d = r.detalle || {}; const est = ESTADO[r.estado] || { label: r.estado || '—', cls: 'bg-gray-100 text-gray-600' }
              const open = abierta === r.id
              return (
                <>
                  <tr key={r.id} className="border-t border-gray-100 hover:bg-blue-50/40 cursor-pointer" onClick={() => setAbierta(open ? null : r.id)}>
                    <td className="px-3 py-2 text-gray-400">{open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}</td>
                    <td className="px-3 py-2 whitespace-nowrap text-gray-700">{fechaHora(r.ts)}</td>
                    <td className="px-3 py-2 text-gray-800">{r.usuario || '—'}</td>
                    <td className="px-3 py-2 max-w-[260px] truncate text-gray-700" title={archivoDe(r)}>{archivoDe(r)}</td>
                    <td className="px-3 py-2 text-gray-700 whitespace-nowrap">{d.pdl || '—'}{d.lcl ? ` · ${d.lcl}` : ''}</td>
                    <td className="px-3 py-2 text-gray-700 whitespace-nowrap">{fechaCorta(d.fecha_planilla)}</td>
                    <td className="px-3 py-2 text-right">{d.total ?? '—'}</td>
                    <td className="px-3 py-2 text-right text-green-700 font-semibold">{d.ok ?? '—'}</td>
                    <td className={`px-3 py-2 text-right font-semibold ${d.fallidos ? 'text-red-700' : 'text-gray-400'}`}>{d.fallidos ?? '—'}</td>
                    <td className="px-3 py-2"><span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${est.cls}`}>{est.label}</span></td>
                    <td className="px-3 py-2 text-right">{r.sesion && <a href={`/liquidador2/resultado/${r.sesion}`} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()} className="text-blue-700 hover:underline text-xs inline-flex items-center gap-1" title="Abrir en el módulo"><ExternalLink className="w-3.5 h-3.5" /></a>}</td>
                  </tr>
                  {open && <tr key={`${r.id}-d`}><td colSpan={11} className="p-0"><DetalleLiquidacion r={r} eventos={(porSesion[r.sesion] || []).filter((e) => e.id !== r.id)} /></td></tr>}
                </>
              )
            })}
            {resultados.length === 0 && <tr><td colSpan={11} className="px-3 py-8 text-center text-gray-400">Todavía no hay liquidaciones registradas.</td></tr>}
          </tbody>
        </table>
      </div>

      <h2 className="text-lg font-semibold text-gray-800 mb-2">Planillas subidas</h2>
      <div className="bg-white border border-gray-200 rounded-xl overflow-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500"><tr>
            <th className="text-left px-3 py-2">Fecha · hora</th><th className="text-left px-3 py-2">Usuario</th><th className="text-left px-3 py-2">Archivo</th>
            <th className="text-right px-3 py-2">MO</th><th className="text-right px-3 py-2">MAT</th><th className="text-left px-3 py-2">PDL</th><th className="text-left px-3 py-2">Fecha planilla</th><th className="text-left px-3 py-2">Huella</th><th className="text-left px-3 py-2">Resultado</th>
          </tr></thead>
          <tbody>
            {planillas.map((p) => {
              const d = p.detalle || {}; const sha = d.sha256 || p.archivo_hash || ''
              const repetida = sha && hashVistos[sha]; if (sha && !repetida) hashVistos[sha] = p.ts
              const res = (porSesion[p.sesion] || []).find((e) => e.accion === 'liquidador_resultado')
              const ajuste = (porSesion[p.sesion] || []).find((e) => e.accion === 'liquidador_fecha_ajustada')
              return (
                <tr key={p.id} className="border-t border-gray-100">
                  <td className="px-3 py-2 whitespace-nowrap text-gray-700">{fechaHora(p.ts)}</td>
                  <td className="px-3 py-2 text-gray-800">{p.usuario || '—'}</td>
                  <td className="px-3 py-2 max-w-[280px] truncate text-gray-700" title={p.archivo || ''}>{p.archivo || '—'}</td>
                  <td className="px-3 py-2 text-right">{d.filas_mo ?? '—'}</td>
                  <td className="px-3 py-2 text-right">{d.filas_mat ?? '—'}</td>
                  <td className="px-3 py-2 text-gray-700">{d.pdl_detectado || '—'}</td>
                  <td className="px-3 py-2 text-gray-700 whitespace-nowrap">{fechaCorta(d.fecha_planilla)}{ajuste && <span className="ml-1 text-amber-700" title={`Ajustada por cierre SGS: ${fechaCorta(ajuste.detalle?.fecha_original)} → ${fechaCorta(ajuste.detalle?.fecha_aplicada)}`}><AlertTriangle className="w-3.5 h-3.5 inline" /></span>}</td>
                  <td className="px-3 py-2 text-xs"><code title={sha}>{sha ? sha.slice(0, 10) : '—'}</code>{repetida && <span className="ml-1 text-amber-700" title={`Mismo archivo subido antes a las ${hora(repetida)}`}>repetida</span>}</td>
                  <td className="px-3 py-2 text-xs">{res ? <button onClick={() => setAbierta(res.id)} className="text-blue-700 hover:underline">{(ESTADO[res.estado] || {}).label || res.estado} · {res.detalle?.ok ?? 0} OK / {res.detalle?.fallidos ?? 0} fallidos</button> : <span className="text-gray-400">sin liquidar</span>}</td>
                </tr>
              )
            })}
            {planillas.length === 0 && <tr><td colSpan={9} className="px-3 py-8 text-center text-gray-400">Todavía no hay planillas subidas.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
