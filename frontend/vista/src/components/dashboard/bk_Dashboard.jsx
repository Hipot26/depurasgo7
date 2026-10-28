import { useState, useMemo, useEffect, Fragment } from 'react'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  PieChart, Pie, Cell, LineChart, Line, ResponsiveContainer
} from 'recharts'
import api from '../../utils/api'
import { formatCOP } from '../../utils/format'
import { TrendingUp, Users, CheckCircle, AlertCircle, FileCheck, ChevronRight, Info, X, Building2, Filter, Calendar, RotateCcw, SlidersHorizontal, Search, MapPin } from 'lucide-react'
import { useAuthStore } from '../../store/authStore'
import { esRolGlobal, ORDEN_UNIDADES, UNIDADES, unidadCfg } from '../../config/unidades'
import CurvaAcumulada from './CurvaAcumulada'

// ── Períodos (presets) ────────────────────────────────────────────────────────
const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
const finDeMes = (y, m) => new Date(y, m + 1, 0)

const PRESETS = [
  { id: 'mes',    label: 'Este mes' },
  { id: 'mesAnt', label: 'Mes anterior' },
  { id: 'trim',   label: 'Últ. 3 meses' },
  { id: 'anio',   label: 'Este año' },
  { id: 'todo',   label: 'Todo' },
  { id: 'custom', label: 'Personalizado' },
]

function calcPeriodo(preset, rango) {
  const h = new Date()
  const y = h.getFullYear(), m = h.getMonth()
  switch (preset) {
    case 'mes':    return { fecha_inicio: ymd(new Date(y, m, 1)),     fecha_fin: ymd(finDeMes(y, m)) }
    case 'mesAnt': return { fecha_inicio: ymd(new Date(y, m - 1, 1)), fecha_fin: ymd(finDeMes(y, m - 1)) }
    case 'trim':   return { fecha_inicio: ymd(new Date(y, m - 2, 1)), fecha_fin: ymd(finDeMes(y, m)) }
    case 'anio':   return { fecha_inicio: `${y}-01-01`,               fecha_fin: `${y}-12-31` }
    case 'todo':   return {}
    case 'custom': return { fecha_inicio: rango.inicio || undefined, fecha_fin: rango.fin || undefined }
    default:       return {}
  }
}

const FILTRO_LABELS = {
  zona: 'Zona', lider: 'Líder', tipo_grupo: 'Tipo', proceso: 'Proceso',
  subproceso: 'Subproceso', municipio: 'Municipio', estado: 'Estado', estado_pdt: 'Estado PDT',
}

const COLORES_ZONA = {
  ALMEIDAS: '#8b5cf6',
  GUAVIO: '#f97316',
  UBATE: '#14b8a6',
  'LV&TLC': '#ec4899',
}

const PIE_COLORS = ['#22c55e','#f59e0b','#ef4444','#3b82f6','#8b5cf6']
const MESES = ['','Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic']

// Colores por estado (texto libre, vocabulario real de los datos)
const ESTADO_COLORS = {
  'EN GESTION': '#f59e0b', 'EN PROGRESO': '#8b5cf6', 'APROBADO': '#22c55e', 'VALIDADO': '#3b82f6',
  'EJECUTADO': '#16a34a', 'CANCELADO': '#ef4444', 'RECHAZADO': '#dc2626', 'DISPONIBILIDAD': '#14b8a6',
  'PENDIENTE': '#eab308', 'PROGRAMADO': '#3b82f6', 'PARCIAL': '#f59e0b',
}
const PALETTE = ['#3b82f6','#22c55e','#f59e0b','#8b5cf6','#ec4899','#14b8a6','#ef4444','#64748b','#0ea5e9','#a855f7']
const colorFor = (label, i) => ESTADO_COLORS[label] || PALETTE[i % PALETTE.length]

// Orden lógico del flujo de facturación/SGS (de inicio a fin del pipeline)
const PIPELINE_ORDEN = [
  'PENDIENTE','DISPONIBILIDAD','EN GESTION','EN PROGRESO','APROBADO','VALIDADO',
  'EJECUTADO','ENTREGADO_FACTURACION','LIQUIDADO_SGS','RECHAZADO','CANCELADO',
]
const ordenarPipeline = (dist) => {
  const idx = (e) => { const i = PIPELINE_ORDEN.indexOf(e); return i === -1 ? 999 : i }
  return [...(dist || [])].sort((a, b) => idx(a.estado) - idx(b.estado) || b.total - a.total)
}

// Pipeline de etapas (flujo con flechas, de inicio a fin)
function PipelineStages({ title, sub, dist, onClick }) {
  const ordered = ordenarPipeline(dist)
  const total = ordered.reduce((s, d) => s + d.total, 0)
  const max = Math.max(1, ...ordered.map((d) => d.total))
  return (
    <div className={`bg-white rounded-xl p-5 shadow-sm border group ${onClick ? 'cursor-pointer hover:shadow-md hover:border-blue-300 transition' : ''}`} onClick={onClick}>
      <div className="flex items-baseline justify-between mb-4">
        <h3 className="text-sm font-semibold text-gray-700">{title}</h3>
        {onClick ? <InfoHint /> : (sub && <span className="text-[11px] text-gray-400">{sub}</span>)}
      </div>
      {total === 0 ? (
        <div className="h-[120px] flex items-center justify-center text-gray-400 text-xs">Sin datos</div>
      ) : (
        <div className="flex items-stretch gap-1 overflow-x-auto pb-1">
          {ordered.map((d, i) => {
            const color = colorFor(d.estado, i)
            const pct = total ? Math.round((d.total / total) * 100) : 0
            return (
              <Fragment key={d.estado}>
                <div className="flex-1 min-w-[110px] rounded-lg border bg-white overflow-hidden">
                  <div className="h-1.5" style={{ backgroundColor: color }} />
                  <div className="px-3 py-2">
                    <p className="text-[11px] text-gray-500 font-medium truncate" title={d.estado}>{d.estado}</p>
                    <p className="text-2xl font-bold text-gray-800 leading-tight">{d.total}</p>
                    <p className="text-[10px] text-gray-400">{pct}%{d.mo_real > 0 ? ` · ${formatCOP(d.mo_real)}` : ''}</p>
                    {/* mini barra proporcional */}
                    <div className="h-1 bg-gray-100 rounded-full mt-1.5 overflow-hidden">
                      <div className="h-full rounded-full" style={{ width: `${Math.round((d.total / max) * 100)}%`, backgroundColor: color }} />
                    </div>
                  </div>
                </div>
                {i < ordered.length - 1 && (
                  <div className="flex items-center shrink-0"><ChevronRight size={16} className="text-gray-300" /></div>
                )}
              </Fragment>
            )
          })}
        </div>
      )}
    </div>
  )
}

// Donut de distribución de un estado (dist = [{estado, total, mo_real}])
function EstadoDonut({ title, sub, dist, sin, onClick }) {
  const data = (dist || []).map((d) => ({ name: d.estado, value: d.total, mo_real: d.mo_real }))
  const con = data.reduce((s, d) => s + d.value, 0)
  return (
    <div className={`bg-white rounded-xl p-5 shadow-sm border group ${onClick ? 'cursor-pointer hover:shadow-md hover:border-blue-300 transition' : ''}`} onClick={onClick}>
      <div className="flex items-baseline justify-between mb-3">
        <h3 className="text-sm font-semibold text-gray-700">{title}</h3>
        {onClick ? <InfoHint /> : (sub && <span className="text-[11px] text-gray-400">{sub}</span>)}
      </div>
      {con === 0 ? (
        <div className="h-[200px] flex items-center justify-center text-gray-400 text-xs">Sin datos en este periodo</div>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <PieChart>
            <Pie data={data} cx="50%" cy="50%" innerRadius={48} outerRadius={78} dataKey="value" paddingAngle={2}
              label={({ name, percent }) => `${name} ${(percent*100).toFixed(0)}%`} labelLine={false} fontSize={9}>
              {data.map((d, i) => <Cell key={i} fill={colorFor(d.name, i)} />)}
            </Pie>
            <Tooltip formatter={(v, n, p) => [`${v} registro(s)`, p.payload.name]} />
          </PieChart>
        </ResponsiveContainer>
      )}
      <p className="text-[11px] text-gray-400 text-center mt-1">
        {con} con estado{typeof sin === 'number' ? ` · ${sin} sin diligenciar` : ''}
      </p>
    </div>
  )
}

// Barras apiladas de un estado por zona
function EstadoPorZona({ title, sub, porZona, labels, onClick }) {
  const hasData = (porZona || []).some((z) => labels.some((l) => z[l] > 0))
  return (
    <div className={`bg-white rounded-xl p-5 shadow-sm border group ${onClick ? 'cursor-pointer hover:shadow-md hover:border-blue-300 transition' : ''}`} onClick={onClick}>
      <div className="flex items-baseline justify-between mb-3">
        <h3 className="text-sm font-semibold text-gray-700">{title}</h3>
        {onClick ? <InfoHint /> : (sub && <span className="text-[11px] text-gray-400">{sub}</span>)}
      </div>
      {!hasData ? (
        <div className="h-[200px] flex items-center justify-center text-gray-400 text-xs">Sin datos en este periodo</div>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={porZona} margin={{ top: 5, right: 16, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
            <XAxis dataKey="zona" tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
            <Tooltip />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {labels.map((l, i) => (
              <Bar key={l} dataKey={l} name={l} stackId="a" fill={colorFor(l, i)}
                radius={i === labels.length - 1 ? [3,3,0,0] : [0,0,0,0]} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}

// Pista visual de "clic para ver detalle" en la cabecera de cada gráfica
function InfoHint() {
  return (
    <span className="flex items-center gap-1 text-[10px] text-blue-500 opacity-70 group-hover:opacity-100">
      <Info size={12} /> detalle
    </span>
  )
}

// Lista los proyectos/registros exactos detrás de un campo de estado, filtrable por valor
function DetalleProyectos({ campo, params }) {
  const [filtro, setFiltro] = useState(null)
  const { data = [], isLoading } = useQuery({
    queryKey: ['kpi-detalle', campo, params],
    queryFn: () => api.get('/kpis/detalle', { params: { ...params, campo } }).then((r) => r.data),
  })
  const fdate = (s) => (s ? s.slice(0, 10).split('-').reverse().join('/') : '')
  const counts = {}
  data.forEach((d) => { counts[d.estado] = (counts[d.estado] || 0) + 1 })
  const estados = Object.keys(counts).sort((a, b) => counts[b] - counts[a])
  const filtered = filtro ? data.filter((d) => d.estado === filtro) : data
  const chip = (active) => `flex items-center gap-1 px-2 py-1 rounded-full text-[11px] border ${active ? 'bg-blue-700 text-white border-blue-700' : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50'}`

  if (isLoading) return <div className="py-10 text-center text-gray-400 text-sm">Cargando proyectos…</div>

  return (
    <div>
      {/* Filtros por estado */}
      <div className="flex flex-wrap gap-1.5 mb-3">
        <button onClick={() => setFiltro(null)} className={chip(!filtro)}>Todos <b>{data.length}</b></button>
        {estados.map((e, i) => (
          <button key={e} onClick={() => setFiltro(e)} className={chip(filtro === e)}>
            <span className="w-2 h-2 rounded-full" style={{ backgroundColor: colorFor(e, i) }} /> {e} <b>{counts[e]}</b>
          </button>
        ))}
      </div>

      <div className="overflow-auto border rounded-lg max-h-[55vh]">
        <table className="w-full text-xs">
          <thead className="bg-gray-50 sticky top-0">
            <tr>
              <th className="px-2 py-2 text-left font-semibold text-gray-600">Estado</th>
              <th className="px-2 py-2 text-left font-semibold text-gray-600">Zona</th>
              <th className="px-2 py-2 text-left font-semibold text-gray-600">Fecha</th>
              <th className="px-2 py-2 text-left font-semibold text-gray-600">Líder</th>
              <th className="px-2 py-2 text-left font-semibold text-gray-600">Proyecto / PMS</th>
              <th className="px-2 py-2 text-left font-semibold text-gray-600">Nº PDT</th>
              <th className="px-2 py-2 text-right font-semibold text-gray-600">MO real</th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr><td colSpan={7} className="px-2 py-4 text-center text-gray-400">Sin registros</td></tr>
            ) : filtered.map((d, i) => (
              <tr key={i} className="border-t hover:bg-gray-50">
                <td className="px-2 py-1.5">
                  <span className="inline-flex items-center gap-1">
                    <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: colorFor(d.estado, estados.indexOf(d.estado)) }} />
                    {d.estado}
                  </span>
                </td>
                <td className="px-2 py-1.5">{d.zona}</td>
                <td className="px-2 py-1.5 whitespace-nowrap">{fdate(d.fecha)}</td>
                <td className="px-2 py-1.5">{d.lider}</td>
                <td className="px-2 py-1.5 max-w-[220px] truncate" title={d.proyecto || ''}>{d.proyecto || '—'}</td>
                <td className="px-2 py-1.5 whitespace-nowrap">{d.num_pdt || '—'}</td>
                <td className="px-2 py-1.5 text-right text-gray-600 whitespace-nowrap">{d.mo_real > 0 ? formatCOP(d.mo_real) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-[11px] text-gray-400 mt-2">{filtered.length} de {data.length} registro(s){filtro ? ` · filtrando "${filtro}"` : ''}</p>
    </div>
  )
}

// Modal que justifica una gráfica: qué muestra, cómo se calcula y el detalle exacto
function ChartDetailModal({ info, params, onClose }) {
  if (!info) return null
  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className={`bg-white rounded-xl shadow-2xl w-full ${info.campo ? 'max-w-4xl' : 'max-w-2xl'} max-h-[85vh] flex flex-col`} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b shrink-0">
          <h2 className="font-semibold text-gray-800 text-sm">{info.title}</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
        </div>
        <div className="px-5 py-4 overflow-y-auto space-y-4">
          {info.descripcion && (
            <div>
              <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-1">¿Qué muestra?</p>
              <p className="text-sm text-gray-700">{info.descripcion}</p>
            </div>
          )}
          {info.campo ? (
            <DetalleProyectos campo={info.campo} params={params} />
          ) : info.calculo && (
            <div>
              <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-1">¿Cómo se calcula?</p>
              <p className="text-sm text-gray-700">{info.calculo}</p>
            </div>
          )}
          {!info.campo && info.columns && info.rows && (
            <div>
              <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-1">Detalle de los datos</p>
              <div className="overflow-x-auto border rounded-lg">
                <table className="w-full text-xs">
                  <thead className="bg-gray-50">
                    <tr>
                      {info.columns.map((c, i) => (
                        <th key={i} className={`px-3 py-2 font-semibold text-gray-600 ${i === 0 ? 'text-left' : 'text-right'}`}>{c}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {info.rows.length === 0 ? (
                      <tr><td colSpan={info.columns.length} className="px-3 py-3 text-center text-gray-400">Sin datos</td></tr>
                    ) : info.rows.map((r, ri) => (
                      <tr key={ri} className="border-t hover:bg-gray-50">
                        {r.map((cell, ci) => (
                          <td key={ci} className={`px-3 py-1.5 ${ci === 0 ? 'text-left font-medium text-gray-800' : 'text-right text-gray-600'}`}>{cell}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          {info.nota && <p className="text-[11px] text-gray-400 italic">{info.nota}</p>}
        </div>
      </div>
    </div>
  )
}

function KPICard({ icon: Icon, label, value, sub, color = 'blue' }) {
  const colors = {
    blue: 'bg-blue-50 text-blue-700',
    green: 'bg-green-50 text-green-700',
    orange: 'bg-orange-50 text-orange-700',
    red: 'bg-red-50 text-red-700',
  }
  return (
    <div className="bg-white rounded-xl p-5 shadow-sm border">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs text-gray-500 font-medium mb-1">{label}</p>
          <p className="text-2xl font-bold text-gray-800">{value}</p>
          {sub && <p className="text-xs text-gray-400 mt-0.5">{sub}</p>}
        </div>
        <div className={`p-2.5 rounded-lg ${colors[color]}`}>
          <Icon size={20} />
        </div>
      </div>
    </div>
  )
}

// Select compacto reutilizable para los filtros
function MiniSelect({ icon: Icon, value, onChange, options, placeholder }) {
  return (
    <div className="relative">
      {Icon && <Icon size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={`w-full ${Icon ? 'pl-7' : 'pl-2'} pr-6 py-1.5 text-xs border rounded-lg bg-white focus:outline-none focus:ring-1 focus:ring-blue-400 ${value ? 'border-blue-300 text-blue-800 font-semibold' : 'border-gray-200 text-gray-600'}`}
      >
        <option value="">{placeholder}</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </div>
  )
}

// Barra de filtros del dashboard — afecta a todas las gráficas
function FilterBar({ esGlobal, unidad, setUnidad, preset, setPreset, rango, setRango, f, setF, opciones, cfg, onLimpiar }) {
  const [expand, setExpand] = useState(false)
  const zonas       = opciones?.zonas?.length ? opciones.zonas : cfg.zonas
  const tipos       = opciones?.tipos?.length ? opciones.tipos : cfg.tipoOptions.filter(Boolean)
  const procesos    = opciones?.procesos?.length ? opciones.procesos : cfg.procesoOptions.filter(Boolean)
  const subprocesos = opciones?.subprocesos || []
  const estados     = opciones?.estados || []
  const estadosPdt  = opciones?.estados_pdt || []
  const lideres     = opciones?.lideres || []
  const municipios  = opciones?.municipios || []
  const setKey = (k, v) => setF((p) => ({ ...p, [k]: v }))

  const activos = Object.entries(f).filter(([, v]) => v)
  const hayFiltros = activos.length > 0 || preset !== 'mes'

  return (
    <div id="dash-filtros" className="bg-white rounded-xl border shadow-sm p-3 space-y-3 sticky top-0 z-20">
      {/* Fila 1: período + unidad */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-1.5 text-xs font-semibold text-gray-500 mr-1">
          <Calendar size={14} className="text-blue-600" /> Período
        </span>
        <div className="flex flex-wrap gap-1 bg-gray-100 p-0.5 rounded-lg">
          {PRESETS.map((p) => (
            <button
              key={p.id}
              onClick={() => setPreset(p.id)}
              className={`px-2.5 py-1 text-xs rounded-md transition ${preset === p.id ? 'bg-white text-blue-700 font-semibold shadow-sm' : 'text-gray-500 hover:text-gray-800'}`}
            >
              {p.label}
            </button>
          ))}
        </div>
        {preset === 'custom' && (
          <div className="flex items-center gap-1.5">
            <input type="date" value={rango.inicio} onChange={(e) => setRango((r) => ({ ...r, inicio: e.target.value }))} className="border rounded-lg px-2 py-1 text-xs" />
            <span className="text-gray-400 text-xs">→</span>
            <input type="date" value={rango.fin} onChange={(e) => setRango((r) => ({ ...r, fin: e.target.value }))} className="border rounded-lg px-2 py-1 text-xs" />
          </div>
        )}
        <div className="flex-1" />
        {esGlobal && (
          <div className="relative">
            <Building2 size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-blue-500 pointer-events-none" />
            <select id="dash-unidad" value={unidad} onChange={(e) => setUnidad(e.target.value)} title="Unidad operativa"
              className="pl-7 pr-2 py-1.5 text-xs font-semibold text-blue-800 bg-blue-50 border border-blue-200 rounded-lg">
              {ORDEN_UNIDADES.map((u) => <option key={u} value={u}>{UNIDADES[u].corto}</option>)}
            </select>
          </div>
        )}
        <button onClick={() => setExpand((v) => !v)}
          className={`flex items-center gap-1 px-2.5 py-1.5 text-xs border rounded-lg transition ${expand ? 'bg-blue-50 border-blue-200 text-blue-700' : 'text-gray-500 hover:bg-gray-50'}`}>
          <SlidersHorizontal size={13} /> Filtros
        </button>
      </div>

      {/* Fila 2: filtros de campo */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
        <MiniSelect icon={MapPin} value={f.zona} onChange={(v) => setKey('zona', v)} options={zonas} placeholder="Todas las zonas" />
        <div className="relative">
          <Search size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
          <input list="lideres-list" value={f.lider} onChange={(e) => setKey('lider', e.target.value)} placeholder="Líder…"
            className={`w-full pl-7 pr-2 py-1.5 text-xs border rounded-lg focus:outline-none focus:ring-1 focus:ring-blue-400 ${f.lider ? 'border-blue-300 text-blue-800 font-semibold' : 'border-gray-200 text-gray-600'}`} />
          <datalist id="lideres-list">{lideres.map((l) => <option key={l} value={l} />)}</datalist>
        </div>
        <MiniSelect value={f.tipo_grupo} onChange={(v) => setKey('tipo_grupo', v)} options={tipos} placeholder="Todos los tipos" />
        <MiniSelect value={f.proceso} onChange={(v) => setKey('proceso', v)} options={procesos} placeholder="Todo proceso" />
        <MiniSelect value={f.estado} onChange={(v) => setKey('estado', v)} options={estados} placeholder="Todo estado" />
        <MiniSelect value={f.estado_pdt} onChange={(v) => setKey('estado_pdt', v)} options={estadosPdt} placeholder="Todo estado PDT" />
        {expand && <>
          <MiniSelect value={f.subproceso} onChange={(v) => setKey('subproceso', v)} options={subprocesos} placeholder="Todo subproceso" />
          <MiniSelect icon={MapPin} value={f.municipio} onChange={(v) => setKey('municipio', v)} options={municipios} placeholder="Todo municipio" />
        </>}
      </div>

      {/* Chips de filtros activos */}
      {hayFiltros && (
        <div className="flex flex-wrap items-center gap-1.5 pt-1 border-t">
          <span className="text-[11px] text-gray-400 font-medium">Activos:</span>
          {preset !== 'mes' && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] bg-indigo-50 text-indigo-700 border border-indigo-200">
              {PRESETS.find((p) => p.id === preset)?.label}
              <button onClick={() => setPreset('mes')} className="hover:text-indigo-900"><X size={11} /></button>
            </span>
          )}
          {activos.map(([k, v]) => (
            <span key={k} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] bg-blue-50 text-blue-700 border border-blue-200">
              <b className="font-semibold">{FILTRO_LABELS[k]}:</b> {v}
              <button onClick={() => setKey(k, '')} className="hover:text-blue-900"><X size={11} /></button>
            </span>
          ))}
          <button onClick={onLimpiar} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] text-red-600 hover:bg-red-50">
            <RotateCcw size={11} /> Limpiar todo
          </button>
        </div>
      )}
    </div>
  )
}

const F_VACIO = { zona: '', lider: '', tipo_grupo: '', proceso: '', subproceso: '', municipio: '', estado: '', estado_pdt: '' }

export default function Dashboard() {
  const { usuario } = useAuthStore()
  const esGlobal = esRolGlobal(usuario?.rol)
  const [unidad, setUnidad] = useState(usuario?.unidad || 'SABOGAL')
  const cfg = unidadCfg(unidad)
  const [preset, setPreset] = useState('mes')
  const [rango, setRango] = useState({ inicio: '', fin: '' })
  const [f, setF] = useState({ ...F_VACIO })
  const [modal, setModal] = useState(null)

  const limpiar = () => { setPreset('mes'); setRango({ inicio: '', fin: '' }); setF({ ...F_VACIO }) }

  // Al cambiar de unidad, limpiar filtros de campo (los catálogos cambian)
  useEffect(() => { setF({ ...F_VACIO }) }, [unidad])

  const periodo = useMemo(() => calcPeriodo(preset, rango), [preset, rango])
  const params = useMemo(() => {
    const p = { unidad, ...periodo }
    for (const k of Object.keys(f)) if (f[k]) p[k] = f[k]
    return p
  }, [unidad, periodo, f])

  // Opciones de los filtros (valores reales en datos, por unidad)
  const { data: opciones } = useQuery({
    queryKey: ['kpi-opciones', unidad],
    queryFn: () => api.get('/kpis/opciones', { params: { unidad } }).then((r) => r.data),
  })

  const { data: kpi, isLoading } = useQuery({
    queryKey: ['kpis', params],
    queryFn: () => api.get('/kpis', { params }).then((r) => r.data),
    placeholderData: keepPreviousData,
  })
  const { data: timeline } = useQuery({
    queryKey: ['kpis-timeline', params],
    queryFn: () => api.get('/kpis/timeline', { params }).then((r) => r.data),
    placeholderData: keepPreviousData,
  })
  const { data: porLider } = useQuery({
    queryKey: ['kpis-lider', params],
    queryFn: () => api.get('/kpis/por-lider', { params }).then((r) => r.data),
    placeholderData: keepPreviousData,
  })
  const { data: estados } = useQuery({
    queryKey: ['kpis-estados', params],
    queryFn: () => api.get('/kpis/facturacion', { params }).then((r) => r.data),
    placeholderData: keepPreviousData,
  })
  const estadoProg = estados?.estado_prog
  const estadoPdt  = estados?.estado_pdt

  const pz = kpi?.por_zona || []
  const totalRegistros = pz.reduce((s, z) => s + z.total_registros, 0)
  const totalEjec = pz.reduce((s, z) => s + z.ejecutados, 0)

  // ── Helpers para el detalle de los popups ──────────────────────────────────
  const pctOf = (n, tot) => (tot ? `${Math.round((n / tot) * 100)}%` : '0%')
  const filasDist = (dist, tot, moKey) => (dist || []).map((d) => {
    const base = [d.estado, d.total, pctOf(d.total, tot)]
    if (moKey) base.push(formatCOP(d[moKey]))
    return base
  })
  const filasPorZona = (porZona, labels) => (porZona || []).map((z) => [
    z.zona, ...labels.map((l) => z[l] || 0), labels.reduce((s, l) => s + (z[l] || 0), 0),
  ])

  // Objetos de información para cada gráfica
  const infoEstadoProg = estadoProg && {
    title: 'Estado de programación — proyectos',
    descripcion: 'Lista de proyectos/días de cuadrilla según su etapa de programación. Usa los filtros para ver exactamente cuáles están EN GESTIÓN, APROBADO, VALIDADO, etc.',
    campo: 'estado',
    nota: `${estadoProg.con_estado} registros con estado · ${estadoProg.sin_estado} sin diligenciar · de ${estadoProg.total} totales.`,
  }
  const infoEstadoProgZona = estadoProg && {
    title: 'Estado de programación por zona',
    descripcion: 'Distribución de los estados de programación dentro de cada zona, para comparar en qué etapa va el trabajo de cada una.',
    calculo: 'Conteo de registros agrupado por ZONA y ESTADO.',
    columns: ['Zona', ...(estadoProg.labels || []), 'Total'],
    rows: filasPorZona(estadoProg.por_zona, estadoProg.labels || []),
  }
  const infoEstadoPdt = estadoPdt && {
    title: 'Facturación / SGS (PDT) — proyectos',
    descripcion: 'Lista de proyectos según su estado de facturación/SGS (PDT). Usa los filtros para ver exactamente cuáles están EJECUTADO, EN GESTIÓN, APROBADO, CANCELADO, etc.',
    campo: 'estado_pdt',
    nota: `${estadoPdt.con_estado} registros con estado PDT · ${estadoPdt.sin_estado} sin diligenciar · de ${estadoPdt.total} totales.`,
  }
  const infoEstadoPdtZona = estadoPdt && {
    title: 'Estado SGS/PDT por zona',
    descripcion: 'Distribución de los estados de facturación/SGS dentro de cada zona.',
    calculo: 'Conteo de registros agrupado por ZONA y ESTADO PDT.',
    columns: ['Zona', ...(estadoPdt.labels || []), 'Total'],
    rows: filasPorZona(estadoPdt.por_zona, estadoPdt.labels || []),
  }
  const infoMO = {
    title: 'MO Proyectada vs Real — por zona',
    descripcion: 'Compara el valor de mano de obra (MO) que se planificó contra el que realmente se ejecutó, en cada zona, para el período y filtros seleccionados.',
    calculo: 'Suma de MO PROYECTADA y MO REAL de todos los registros de cada zona. El % de cumplimiento es MO real ÷ MO proyectada.',
    columns: ['Zona', 'MO Proyectada', 'MO Real', '% Cumpl.'],
    rows: pz.map((z) => [z.zona, formatCOP(z.mo_proyectada), formatCOP(z.mo_real), `${z.pct_ejecucion}%`]),
  }
  const infoPctZona = {
    title: '% Ejecución por zona',
    descripcion: 'Qué porcentaje del valor de MO planificado se ha ejecutado en cada zona durante el período y filtros seleccionados.',
    calculo: '% Ejecución = MO real ÷ MO proyectada, por zona.',
    columns: ['Zona', 'MO Proyectada', 'MO Real', '% Ejec.'],
    rows: pz.map((z) => [z.zona, formatCOP(z.mo_proyectada), formatCOP(z.mo_real), `${z.pct_ejecucion}%`]),
  }
  const infoTimeline = {
    title: 'MO diaria del período',
    descripcion: 'Evolución día a día del valor de MO proyectada y real durante el período y filtros seleccionados.',
    calculo: 'Suma de MO proyectada y MO real de los registros de cada fecha.',
    columns: ['Fecha', 'MO Proyectada', 'MO Real'],
    rows: (timeline || []).map((t) => [t.fecha, formatCOP(t.mo_proyectada), formatCOP(t.mo_real)]),
  }
  const infoInoperatividad = {
    title: 'Inoperatividad por zona',
    descripcion: 'Valor de inoperatividad (inoperativo + tiempo ocioso) acumulado en cada zona, según el período y filtros seleccionados.',
    calculo: 'Suma de la columna INOPERATIVIDAD de todos los registros de cada zona.',
    columns: ['Zona', 'Inoperatividad', 'Registros'],
    rows: pz.map((z) => [z.zona, formatCOP(z.inoperatividad), z.total_registros]),
  }

  const rangoTxt = periodo.fecha_inicio
    ? `${periodo.fecha_inicio.split('-').reverse().join('/')} → ${(periodo.fecha_fin || '').split('-').reverse().join('/')}`
    : 'histórico completo'

  return (
    <div className="p-4 sm:p-6 space-y-5">
      {/* Título */}
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-lg font-bold text-gray-800">Dashboard de Programación</h1>
          <p className="text-sm text-gray-500">
            {UNIDADES[unidad]?.nombre || unidad} · <span className="text-gray-400">{rangoTxt}</span>
            {isLoading && <span className="ml-2 text-blue-500 animate-pulse">actualizando…</span>}
          </p>
        </div>
        <div className="text-right">
          <p className="text-2xl font-bold text-gray-800 leading-none">{totalRegistros}</p>
          <p className="text-[11px] text-gray-400 uppercase tracking-wide">registros filtrados</p>
        </div>
      </div>

      {/* Barra de filtros — afecta a todas las gráficas */}
      <FilterBar
        esGlobal={esGlobal} unidad={unidad} setUnidad={setUnidad}
        preset={preset} setPreset={setPreset} rango={rango} setRango={setRango}
        f={f} setF={setF} opciones={opciones} cfg={cfg} onLimpiar={limpiar}
      />

      {/* KPI Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KPICard icon={Users} label="Total registros" value={totalRegistros} color="blue" />
        <KPICard icon={CheckCircle} label="Ejecutados" value={totalEjec} sub={`${kpi?.pct_ejecucion_general?.toFixed(1)}% ejecución`} color="green" />
        <KPICard icon={TrendingUp} label="MO Proyectada" value={formatCOP(kpi?.total_mo_proyectada)} color="blue" />
        <KPICard icon={AlertCircle} label="V. Dejado Ejecutar" value={formatCOP(kpi?.total_valor_dejado)} color="orange" />
      </div>

      {/* Curva acumulada del período (requisito 2026-09-21) */}
      <CurvaAcumulada params={params} />

      {/* Charts row 1 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* MO por zona */}
        <div className="lg:col-span-2 bg-white rounded-xl p-5 shadow-sm border group cursor-pointer hover:shadow-md hover:border-blue-300 transition" onClick={() => setModal(infoMO)}>
          <div className="flex items-baseline justify-between mb-4">
            <h3 className="text-sm font-semibold text-gray-700">MO Proyectada vs Real — por zona</h3>
            <InfoHint />
          </div>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={pz} margin={{ top: 5, right: 20, left: 20, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis dataKey="zona" tick={{ fontSize: 11 }} />
              <YAxis tickFormatter={(v) => `${(v/1e6).toFixed(0)}M`} tick={{ fontSize: 11 }} />
              <Tooltip formatter={(v) => formatCOP(v)} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Bar dataKey="mo_proyectada" name="Proyectada" fill="#3b82f6" radius={[4,4,0,0]} />
              <Bar dataKey="mo_real" name="Real" fill="#22c55e" radius={[4,4,0,0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* Estado de programación — distribución real */}
        <EstadoDonut
          title="Estado de programación"
          dist={estadoProg?.dist}
          sin={estadoProg?.sin_estado}
          onClick={infoEstadoProg ? () => setModal(infoEstadoProg) : undefined}
        />
      </div>

      {/* % Ejecución por zona */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-white rounded-xl p-5 shadow-sm border group cursor-pointer hover:shadow-md hover:border-blue-300 transition" onClick={() => setModal(infoPctZona)}>
          <div className="flex items-baseline justify-between mb-4">
            <h3 className="text-sm font-semibold text-gray-700">% Ejecución por zona</h3>
            <InfoHint />
          </div>
          <div className="space-y-3">
            {pz.map((z) => (
              <div key={z.zona}>
                <div className="flex justify-between text-xs text-gray-600 mb-1">
                  <span className="font-medium">{z.zona}</span>
                  <span>{z.pct_ejecucion.toFixed(1)}%</span>
                </div>
                <div className="h-2.5 bg-gray-100 rounded-full overflow-hidden">
                  <div className="h-full rounded-full" style={{ width: `${Math.min(z.pct_ejecucion, 100)}%`, backgroundColor: COLORES_ZONA[z.zona] || '#3b82f6' }} />
                </div>
                <div className="flex justify-between text-xs text-gray-400 mt-0.5">
                  <span>{z.ejecutados} ejecutados / {z.total_registros} total</span>
                  <span>{formatCOP(z.mo_real)} real</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Timeline */}
        <div className="bg-white rounded-xl p-5 shadow-sm border group cursor-pointer hover:shadow-md hover:border-blue-300 transition" onClick={() => setModal(infoTimeline)}>
          <div className="flex items-baseline justify-between mb-4">
            <h3 className="text-sm font-semibold text-gray-700">MO diaria del período</h3>
            <InfoHint />
          </div>
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={timeline || []} margin={{ top: 5, right: 10, left: 10, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis dataKey="fecha" tick={{ fontSize: 10 }} tickFormatter={(v) => v ? v.slice(8) : ''} />
              <YAxis tickFormatter={(v) => `${(v/1e6).toFixed(0)}M`} tick={{ fontSize: 10 }} />
              <Tooltip formatter={(v) => formatCOP(v)} labelFormatter={(l) => `Día ${l}`} />
              <Line type="monotone" dataKey="mo_proyectada" name="Proyectada" stroke="#3b82f6" dot={false} strokeWidth={2} />
              <Line type="monotone" dataKey="mo_real" name="Real" stroke="#22c55e" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Inoperatividad por zona */}
      <div className="bg-white rounded-xl p-5 shadow-sm border group cursor-pointer hover:shadow-md hover:border-blue-300 transition" onClick={() => setModal(infoInoperatividad)}>
        <div className="flex items-baseline justify-between mb-4">
          <h3 className="text-sm font-semibold text-gray-700">Inoperatividad por zona</h3>
          <InfoHint />
        </div>
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={pz} margin={{ top: 5, right: 20, left: 20, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
            <XAxis dataKey="zona" tick={{ fontSize: 11 }} />
            <YAxis tickFormatter={(v) => `${(v / 1e6).toFixed(0)}M`} tick={{ fontSize: 11 }} />
            <Tooltip formatter={(v) => formatCOP(v)} />
            <Bar dataKey="inoperatividad" name="Inoperatividad" fill="#f59e0b" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* ── Estados de Facturación / SGS (PDT) ────────────────────────────── */}
      {estadoPdt && (
        <div className="space-y-4">
          <div className="flex items-center gap-2 flex-wrap">
            <FileCheck size={18} className="text-teal-700" />
            <h2 className="text-base font-bold text-gray-800">Estados de Facturación / SGS (PDT)</h2>
            <span className="text-xs text-teal-700 bg-teal-50 px-2 py-0.5 rounded-full font-medium">
              {estadoPdt.con_estado} con estado PDT
            </span>
          </div>

          {/* Tarjetas dinámicas por estado PDT */}
          {estadoPdt.dist?.length > 0 && (
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              {estadoPdt.dist.slice(0, 4).map((d, i) => (
                <div key={d.estado} className="bg-white rounded-xl p-4 shadow-sm border">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: colorFor(d.estado, i) }} />
                    <p className="text-xs text-gray-500 font-medium truncate">{d.estado}</p>
                  </div>
                  <p className="text-2xl font-bold text-gray-800">{d.total}</p>
                  {d.mo_real > 0 && <p className="text-xs text-gray-400 mt-0.5">{formatCOP(d.mo_real)}</p>}
                </div>
              ))}
            </div>
          )}

          {/* Pipeline del flujo SGS */}
          <PipelineStages title="Pipeline de Facturación & SGS" dist={estadoPdt.dist} onClick={() => setModal(infoEstadoPdt)} />

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <EstadoDonut title="Distribución estado SGS/PDT" dist={estadoPdt.dist} sin={estadoPdt.sin_estado} onClick={() => setModal(infoEstadoPdt)} />
            <EstadoPorZona title="Estado SGS/PDT por zona" porZona={estadoPdt.por_zona} labels={estadoPdt.labels || []} onClick={() => setModal(infoEstadoPdtZona)} />
          </div>
        </div>
      )}

      {/* ── Estado de programación: pipeline + por zona ───────────────────── */}
      {estadoProg && estadoProg.con_estado > 0 && (
        <div className="space-y-4">
          <PipelineStages
            title="Pipeline de programación"
            dist={estadoProg.dist}
            onClick={() => setModal(infoEstadoProg)}
          />
          <EstadoPorZona
            title="Estado de programación por zona"
            porZona={estadoProg.por_zona}
            labels={estadoProg.labels || []}
            onClick={() => setModal(infoEstadoProgZona)}
          />
        </div>
      )}

      {/* Tabla por líder */}
      {porLider && porLider.length > 0 && (
        <div className="bg-white rounded-xl p-5 shadow-sm border">
          <h3 className="text-sm font-semibold text-gray-700 mb-4">Resumen por líder de cuadrilla</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-gray-50 border-b">
                  <th className="text-left px-3 py-2 font-semibold text-gray-600">Líder</th>
                  <th className="text-left px-3 py-2 font-semibold text-gray-600">Zona</th>
                  <th className="text-left px-3 py-2 font-semibold text-gray-600">Tipo</th>
                  <th className="text-right px-3 py-2 font-semibold text-gray-600">Días</th>
                  <th className="text-right px-3 py-2 font-semibold text-gray-600">Ejecutados</th>
                  <th className="text-right px-3 py-2 font-semibold text-gray-600">% Ejec.</th>
                  <th className="text-right px-3 py-2 font-semibold text-gray-600">MO Proyectada</th>
                  <th className="text-right px-3 py-2 font-semibold text-gray-600">MO Real</th>
                </tr>
              </thead>
              <tbody>
                {porLider.slice(0, 30).map((r, i) => (
                  <tr key={i} className="border-b hover:bg-gray-50">
                    <td className="px-3 py-2 font-medium text-gray-800">{r.lider}</td>
                    <td className="px-3 py-2">{r.zona}</td>
                    <td className="px-3 py-2">{r.tipo_grupo}</td>
                    <td className="px-3 py-2 text-right">{r.total_dias}</td>
                    <td className="px-3 py-2 text-right text-green-700">{r.ejecutados}</td>
                    <td className="px-3 py-2 text-right">
                      <span className={`px-1.5 py-0.5 rounded text-xs ${r.pct_ejecucion >= 80 ? 'bg-green-100 text-green-700' : r.pct_ejecucion >= 50 ? 'bg-yellow-100 text-yellow-700' : 'bg-red-100 text-red-700'}`}>
                        {r.pct_ejecucion}%
                      </span>
                    </td>
                    <td className="px-3 py-2 text-right text-gray-600">{formatCOP(r.mo_proyectada)}</td>
                    <td className="px-3 py-2 text-right text-green-700">{formatCOP(r.mo_real)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Popup de detalle de gráfica */}
      <ChartDetailModal info={modal} params={params} onClose={() => setModal(null)} />
    </div>
  )
}
