import { useState, useMemo } from 'react'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  PieChart, Pie, Cell, LineChart, Line, ResponsiveContainer,
} from 'recharts'
import api from '../../utils/api'
import toast from 'react-hot-toast'
import { Calendar, RotateCcw, X, Search, Info, Package, CheckCircle, Clock, Ban, FileDown } from 'lucide-react'

const PALETTE = ['#3b82f6', '#22c55e', '#f59e0b', '#8b5cf6', '#ec4899', '#14b8a6', '#ef4444', '#64748b', '#0ea5e9', '#a855f7', '#84cc16', '#f97316']
const colorFor = (i) => PALETTE[i % PALETTE.length]

const F_VACIO = { tipo: '', zona: '', estado: '', supervisor: '', municipio: '', q: '', fecha_inicio: '', fecha_fin: '' }
const FILTRO_LABELS = { tipo: 'Tipo', zona: 'Zona', estado: 'Estado', supervisor: 'Supervisor', municipio: 'Municipio', q: 'Búsqueda', fecha_inicio: 'Desde', fecha_fin: 'Hasta' }

function MiniSelect({ value, onChange, options, placeholder }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}
      className={`px-2 py-1.5 text-xs border rounded-lg bg-white focus:outline-none focus:ring-1 focus:ring-blue-400 ${value ? 'border-blue-300 text-blue-800 font-semibold' : 'border-gray-200 text-gray-600'}`}>
      <option value="">{placeholder}</option>
      {options.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  )
}

function Card({ icon: Icon, label, value, color = 'gray' }) {
  const c = { gray: 'bg-gray-50 text-gray-700', blue: 'bg-blue-50 text-blue-700', green: 'bg-green-50 text-green-700', orange: 'bg-orange-50 text-orange-700', red: 'bg-red-50 text-red-700' }
  return (
    <div className="bg-white rounded-xl border shadow-sm p-4 flex items-center justify-between">
      <div>
        <p className="text-xs text-gray-500 font-medium">{label}</p>
        <p className="text-2xl font-bold text-gray-800">{value}</p>
      </div>
      <div className={`p-2.5 rounded-lg ${c[color]}`}><Icon size={20} /></div>
    </div>
  )
}

function ChartCard({ title, children, onInfo }) {
  return (
    <div className="bg-white rounded-xl border shadow-sm p-4">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold text-gray-700">{title}</h3>
        <span className="flex items-center gap-1 text-[10px] text-blue-500"><Info size={11} /> clic para ver casos</span>
      </div>
      {children}
    </div>
  )
}

export default function BolsasReport() {
  const [f, setF] = useState({ ...F_VACIO })
  const [modal, setModal] = useState(null)  // { campo, valor, label }
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }))
  const limpiar = () => setF({ ...F_VACIO })

  const params = useMemo(() => Object.fromEntries(Object.entries(f).filter(([, v]) => v)), [f])

  const { data: kpi, isFetching } = useQuery({
    queryKey: ['bolsas-kpis', params],
    queryFn: () => api.get('/bolsas/kpis', { params }).then((r) => r.data),
    placeholderData: keepPreviousData,
  })
  const { data: opciones } = useQuery({
    queryKey: ['bolsas-opciones'],
    queryFn: () => api.get('/bolsas/opciones').then((r) => r.data),
  })

  const total = kpi?.total ?? 0
  const porEstado = kpi?.por_estado || []
  const bucket = (re) => porEstado.filter((e) => re.test(e.label)).reduce((s, e) => s + e.total, 0)
  const ejec = bucket(/EJECUT|SOLVED|CERR|FACTUR/)
  const pend = bucket(/PTE|PENDIENT|OPEN|VISITA|PROGRAM|GESTION/)
  const anul = bucket(/ANULAD|RECHAZ|CANCEL/)

  const dataTipo = (kpi?.por_tipo || []).map((d) => ({ name: d.label, value: d.total }))
  const dataEstado = (kpi?.por_estado || []).slice(0, 12).map((d) => ({ name: d.label, value: d.total }))
  const dataZona = (kpi?.por_zona || []).slice(0, 12).map((d) => ({ name: d.label, value: d.total }))
  const dataSup = (kpi?.por_supervisor || []).map((d) => ({ name: d.label, value: d.total }))
  const dataTL = (kpi?.timeline || []).map((d) => ({ name: d.periodo, value: d.total }))

  const abrir = (campo, valor, label) => valor && valor !== '—' && setModal({ campo, valor, label })
  const activos = Object.entries(f).filter(([, v]) => v)

  const [exporting, setExporting] = useState(false)
  const exportar = async () => {
    setExporting(true)
    try {
      const res = await api.get('/bolsas/export', { params, responseType: 'blob' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([res.data]))
      const disp = res.headers['content-disposition'] || ''
      a.download = disp.match(/filename="(.+)"/)?.[1] || 'reporte_bolsas.xlsx'
      a.click()
      toast.success('Reporte descargado')
    } catch {
      toast.error('Error al generar el Excel')
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="space-y-4">
      {/* Filtros */}
      <div className="bg-white rounded-xl border shadow-sm p-3 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold text-gray-500">Filtros</span>
          <MiniSelect value={f.tipo} onChange={(v) => set('tipo', v)} options={opciones?.tipos || []} placeholder="Todos los tipos" />
          <MiniSelect value={f.zona} onChange={(v) => set('zona', v)} options={opciones?.zonas || []} placeholder="Todas las zonas" />
          <MiniSelect value={f.estado} onChange={(v) => set('estado', v)} options={opciones?.estados || []} placeholder="Todo estado" />
          <MiniSelect value={f.supervisor} onChange={(v) => set('supervisor', v)} options={opciones?.supervisores || []} placeholder="Todo supervisor" />
          <div className="relative">
            <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400" />
            <input value={f.q} onChange={(e) => set('q', e.target.value)} placeholder="Buscar…"
              className="pl-6 pr-2 py-1.5 text-xs border rounded-lg w-32 focus:outline-none focus:ring-1 focus:ring-blue-400" />
          </div>
          <span className="flex items-center gap-1 text-[11px] text-gray-400"><Calendar size={12} /></span>
          <input type="date" value={f.fecha_inicio} onChange={(e) => set('fecha_inicio', e.target.value)} className="border rounded-lg px-2 py-1 text-xs" title="Fecha prog. desde" />
          <span className="text-gray-400 text-xs">→</span>
          <input type="date" value={f.fecha_fin} onChange={(e) => set('fecha_fin', e.target.value)} className="border rounded-lg px-2 py-1 text-xs" title="Fecha prog. hasta" />
          <span className="ml-auto text-xs text-gray-500">{total} casos{isFetching ? ' · …' : ''}</span>
          <button onClick={exportar} disabled={exporting}
            className="flex items-center gap-1.5 text-xs bg-green-600 text-white px-3 py-1.5 rounded-lg hover:bg-green-700 disabled:opacity-60">
            <FileDown size={14} /> {exporting ? 'Generando…' : 'Exportar Excel'}
          </button>
        </div>
        {activos.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 pt-1 border-t">
            {activos.map(([k, v]) => (
              <span key={k} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] bg-blue-50 text-blue-700 border border-blue-200">
                <b>{FILTRO_LABELS[k]}:</b> {v}
                <button onClick={() => set(k, '')}><X size={11} /></button>
              </span>
            ))}
            <button onClick={limpiar} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] text-red-600 hover:bg-red-50">
              <RotateCcw size={11} /> Limpiar
            </button>
          </div>
        )}
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Card icon={Package} label="Total casos" value={total} color="blue" />
        <Card icon={CheckCircle} label="Ejecutadas" value={ejec} color="green" />
        <Card icon={Clock} label="Pendientes" value={pend} color="orange" />
        <Card icon={Ban} label="Anuladas" value={anul} color="red" />
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Por tipo (donut) */}
        <ChartCard title="Casos por tipo de bolsa">
          <ResponsiveContainer width="100%" height={240}>
            <PieChart>
              <Pie data={dataTipo} cx="50%" cy="50%" innerRadius={55} outerRadius={88} dataKey="value" paddingAngle={2}
                onClick={(e) => abrir('tipo', e?.name, `Tipo ${e?.name}`)} className="cursor-pointer"
                label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`} labelLine={false} fontSize={10}>
                {dataTipo.map((d, i) => <Cell key={i} fill={colorFor(i)} />)}
              </Pie>
              <Tooltip formatter={(v, n, p) => [`${v} casos`, p.payload.name]} />
            </PieChart>
          </ResponsiveContainer>
        </ChartCard>

        {/* Por estado (barras) */}
        <ChartCard title="Casos por estado">
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={dataEstado} layout="vertical" margin={{ left: 10, right: 16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" horizontal={false} />
              <XAxis type="number" tick={{ fontSize: 10 }} allowDecimals={false} />
              <YAxis type="category" dataKey="name" tick={{ fontSize: 9 }} width={110} />
              <Tooltip formatter={(v) => [`${v} casos`, 'Total']} />
              <Bar dataKey="value" radius={[0, 3, 3, 0]} cursor="pointer" onClick={(e) => abrir('estado', e?.name, `Estado ${e?.name}`)}>
                {dataEstado.map((d, i) => <Cell key={i} fill={colorFor(i)} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        {/* Por zona */}
        <ChartCard title="Casos por zona">
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={dataZona} margin={{ left: 0, right: 16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis dataKey="name" tick={{ fontSize: 9 }} interval={0} angle={-20} textAnchor="end" height={50} />
              <YAxis tick={{ fontSize: 10 }} allowDecimals={false} />
              <Tooltip formatter={(v) => [`${v} casos`, 'Total']} />
              <Bar dataKey="value" radius={[3, 3, 0, 0]} cursor="pointer" onClick={(e) => abrir('zona', e?.name, `Zona ${e?.name}`)}>
                {dataZona.map((d, i) => <Cell key={i} fill={colorFor(i)} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        {/* Top supervisores */}
        <ChartCard title="Casos por supervisor (top 12)">
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={dataSup} layout="vertical" margin={{ left: 10, right: 16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" horizontal={false} />
              <XAxis type="number" tick={{ fontSize: 10 }} allowDecimals={false} />
              <YAxis type="category" dataKey="name" tick={{ fontSize: 9 }} width={110} />
              <Tooltip formatter={(v) => [`${v} casos`, 'Total']} />
              <Bar dataKey="value" radius={[0, 3, 3, 0]} fill="#3b82f6" cursor="pointer" onClick={(e) => abrir('supervisor', e?.name, `Supervisor ${e?.name}`)} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Timeline */}
      <div className="bg-white rounded-xl border shadow-sm p-4">
        <div className="flex items-baseline justify-between mb-2">
          <h3 className="text-sm font-semibold text-gray-700">Casos programados por mes (fecha de programación)</h3>
          <span className="text-[11px] text-gray-400">{kpi?.con_fecha_prog ?? 0} con fecha · {kpi?.sin_fecha_prog ?? 0} sin programar</span>
        </div>
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={dataTL} margin={{ left: 0, right: 16 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
            <XAxis dataKey="name" tick={{ fontSize: 10 }} />
            <YAxis tick={{ fontSize: 10 }} allowDecimals={false} />
            <Tooltip formatter={(v) => [`${v} casos`, 'Programados']} />
            <Line type="monotone" dataKey="value" stroke="#3b82f6" strokeWidth={2} dot={{ r: 3 }} />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {modal && <DetalleModal {...modal} baseParams={params} onClose={() => setModal(null)} />}
    </div>
  )
}

// Popup: los casos exactos detrás de una gráfica
function DetalleModal({ campo, valor, label, baseParams, onClose }) {
  const params = { ...baseParams, [campo]: valor, limit: 1000 }
  const { data, isLoading } = useQuery({
    queryKey: ['bolsas-detalle', params],
    queryFn: () => api.get('/bolsas', { params }).then((r) => r.data),
  })
  const items = data?.items || []
  const fdate = (v) => (v ? String(v).slice(0, 10).split('-').reverse().join('/') : '—')

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-4xl max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b shrink-0">
          <h2 className="font-semibold text-gray-800 text-sm">{label} · <b>{data?.total ?? 0}</b> casos</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
        </div>
        <div className="px-5 py-3 overflow-auto">
          {isLoading ? (
            <div className="py-10 text-center text-gray-400 text-sm">Cargando…</div>
          ) : (
            <table className="w-full text-[11px]">
              <thead className="bg-gray-50 sticky top-0">
                <tr className="text-gray-600">
                  {['Tipo', 'Zona', 'Caso', 'Municipio', 'Estado', 'PDL', 'Supervisor', 'F. Prog'].map((h) => (
                    <th key={h} className="text-left px-2 py-1.5 font-semibold whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((b) => (
                  <tr key={b.id} className="border-t hover:bg-gray-50">
                    <td className="px-2 py-1 font-medium text-gray-700">{b.tipo}</td>
                    <td className="px-2 py-1 text-gray-600">{b.zona || '—'}</td>
                    <td className="px-2 py-1 text-gray-800 max-w-[160px] truncate" title={b.caso}>{b.caso || '—'}</td>
                    <td className="px-2 py-1 text-gray-600">{b.municipio || '—'}</td>
                    <td className="px-2 py-1 text-gray-600">{b.estado || '—'}</td>
                    <td className="px-2 py-1 font-mono text-[10px] text-gray-500 max-w-[110px] truncate" title={b.pdl}>{b.pdl || '—'}</td>
                    <td className="px-2 py-1 text-gray-600">{b.supervisor || '—'}</td>
                    <td className="px-2 py-1 text-gray-600 whitespace-nowrap">{fdate(b.fecha_prog)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}
