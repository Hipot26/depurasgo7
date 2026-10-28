import { useState, useMemo, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  PieChart, Pie, Cell, ResponsiveContainer,
} from 'recharts'
import api from '../utils/api'
import { formatCOP } from '../utils/format'
import toast from 'react-hot-toast'
import { FileSpreadsheet, Search, Loader2, X, Download, Table2, ListTree, GitBranch, BarChart3, Filter, ChevronDown, Target } from 'lucide-react'
import SendaMetaTab from '../components/sgs/SendaMetaTab'

const MESES = ['','Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre']
const ZONAS = ['UBATE', 'GUAVIO', 'LV&TLC', 'ALMEIDAS', 'OTROS']

const ZONA_COLORS = {
  ALMEIDAS: 'bg-purple-100 text-purple-700', GUAVIO: 'bg-orange-100 text-orange-700',
  UBATE: 'bg-teal-100 text-teal-700', 'LV&TLC': 'bg-pink-100 text-pink-700',
  OTROS: 'bg-gray-200 text-gray-600',
}
// Paleta de gráficas por zona (validada CVD/contraste) — misma identidad que los pills
const ZONA_HEX = { UBATE: '#0D9488', GUAVIO: '#EA580C', 'LV&TLC': '#DB2777', ALMEIDAS: '#9333EA', OTROS: '#64748B' }
const MI_HEX = '#2563EB', PG_HEX = '#16A34A'

const ZonaPill = ({ z }) => z ? (
  <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold whitespace-nowrap ${ZONA_COLORS[z] || 'bg-gray-100 text-gray-600'}`}>{z}</span>
) : null

const ProcBadge = ({ p }) => (
  <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${p === 'MI' ? 'bg-blue-100 text-blue-700' : 'bg-green-100 text-green-700'}`}>{p}</span>
)

const EstadoBadge = ({ p }) => p.estado === 'anulado'
  ? <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-gray-200 text-gray-600 whitespace-nowrap" title="Liquidado y reversado dentro del mes: queda en $0">anulado</span>
  : p.tiene_reversiones
  ? <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-700 whitespace-nowrap">⚠ parcial</span>
  : <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-green-100 text-green-700">✓ ok</span>

const fmtFecha = (v) => {
  if (!v) return '—'
  const [y, m, d] = String(v).slice(0, 10).split('-')
  return (y && m && d) ? `${d}/${m}` : String(v)
}
const rangoFechas = (p) => {
  const f1 = p.fecha_primera, f2 = p.fecha_liq
  if (!f1 || f1 === f2) return fmtFecha(f2)
  return `${fmtFecha(f1)} → ${fmtFecha(f2)}`
}
const cleanName = (n) => (n || '').split(/\s+/).slice(0, 2).join(' ')
const moCls = (v) => v < 0 ? 'text-red-600' : 'text-green-700'

// Badges de liquidadores con su MO en el tooltip (estilo Verificador)
const LiqBadges = ({ liqs, liquidado_por }) => {
  if (liqs?.length) {
    return liqs.map(l => (
      <span key={l.nombre} title={`${l.nombre} — ${formatCOP(l.mo)}`}
        className="inline-block px-1.5 py-0.5 m-px rounded text-[10px] font-semibold bg-sky-100 text-sky-800 whitespace-nowrap">
        {l.nombre.split(' ')[0]}
      </span>
    ))
  }
  return <span className="text-gray-500 text-[11px]">{liquidado_por || '—'}</span>
}

// Filtro de líderes estilo Excel: búsqueda + checkboxes + (Seleccionar todo) + Aceptar/Cancelar
const LiderExcelFilter = ({ lideres, sel, onApply }) => {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(() => new Set())
  const [q, setQ] = useState('')
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return
    const h = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [open])

  const abrir = () => {
    setDraft(new Set(sel.length ? sel : lideres))
    setQ('')
    setOpen(o => !o)
  }

  const ql = q.toLowerCase().trim()
  const visibles = lideres.filter(l => !ql || l.toLowerCase().includes(ql))
  const todosVisibles = visibles.length > 0 && visibles.every(l => draft.has(l))

  const toggle = l => setDraft(d => {
    const n = new Set(d)
    n.has(l) ? n.delete(l) : n.add(l)
    return n
  })
  const toggleTodos = () => setDraft(d => {
    const n = new Set(d)
    visibles.forEach(l => todosVisibles ? n.delete(l) : n.add(l))
    return n
  })

  const aceptar = () => {
    onApply(draft.size >= lideres.length ? [] : [...draft])
    setOpen(false)
  }

  const label = !sel.length ? 'Líder: todos'
    : sel.length === 1 ? `Líder: ${cleanName(sel[0])}`
    : `Líder: ${sel.length} selecc.`

  return (
    <div className="relative" ref={ref}>
      <button type="button" onClick={abrir}
        className={`flex items-center gap-1 border rounded-lg px-2 py-1.5 text-xs bg-white transition-colors ${
          sel.length ? 'border-blue-400 text-blue-700 font-semibold' : 'text-gray-700'}`}>
        <Filter size={11} /> {label} <ChevronDown size={12} className="text-gray-400" />
      </button>
      {open && (
        <div className="absolute z-30 mt-1 left-0 w-72 bg-white border rounded-xl shadow-lg p-2 space-y-2">
          <input autoFocus value={q} onChange={e => setQ(e.target.value)} placeholder="Buscar…"
            className="w-full px-2 py-1.5 text-xs border rounded-lg focus:outline-none focus:ring-1 focus:ring-blue-400" />
          <div className="max-h-56 overflow-y-auto border rounded-lg">
            <label className="flex items-center gap-2 px-2 py-1.5 text-xs font-semibold text-gray-700 hover:bg-gray-50 cursor-pointer sticky top-0 bg-white border-b">
              <input type="checkbox" checked={todosVisibles} onChange={toggleTodos} className="accent-blue-600" />
              (Seleccionar todo)
            </label>
            {visibles.map(l => (
              <label key={l} className="flex items-center gap-2 px-2 py-1.5 text-xs text-gray-700 hover:bg-blue-50 cursor-pointer">
                <input type="checkbox" checked={draft.has(l)} onChange={() => toggle(l)} className="accent-blue-600" />
                <span className="truncate" title={l}>{l}</span>
              </label>
            ))}
            {!visibles.length && (
              <div className="px-2 py-3 text-center text-xs text-gray-400">Sin coincidencias</div>
            )}
          </div>
          <div className="flex justify-end gap-1.5">
            <button type="button" onClick={aceptar} disabled={!draft.size}
              className="px-3 py-1 text-xs rounded-lg bg-blue-700 text-white hover:bg-blue-800 disabled:opacity-50">
              Aceptar
            </button>
            <button type="button" onClick={() => setOpen(false)}
              className="px-3 py-1 text-xs rounded-lg border text-gray-600 hover:bg-gray-50">
              Cancelar
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

const VISTAS = [
  { id: 'consolidado', icon: Table2,    label: 'Consolidado' },
  { id: 'detalle',     icon: ListTree,  label: 'Prestaciones' },
  { id: 'pdl',         icon: GitBranch, label: 'Análisis PDL' },
  { id: 'graficas',    icon: BarChart3, label: 'Gráficas' },
  { id: 'senda',       icon: Target,    label: 'Senda de meta' },   // avance vs meta mensual, cortes cada 10 días (2026-09-17)
]

export default function SgsExportePage() {
  const hoy = new Date()
  const [mes, setMes]   = useState(hoy.getMonth() + 1)
  const [anio, setAnio] = useState(hoy.getFullYear())
  const [vista, setVista] = useState('consolidado')
  const [zona, setZona]   = useState(null)
  const [search, setSearch]         = useState('')
  const [procFilter, setProcFilter] = useState('')
  const [liderSel, setLiderSel] = useState([])   // multi-selección estilo Excel; [] = todos
  const [liqFilter, setLiqFilter]     = useState(null)
  const [descargando, setDescargando] = useState(null)

  const anios = useMemo(() => {
    const hasta = hoy.getFullYear() + 1
    return Array.from({ length: hasta - 2025 + 1 }, (_, i) => 2025 + i)
  }, [])

  const { data, isLoading, isError, error, isFetching } = useQuery({
    queryKey: ['sgs-exporte', mes, anio],
    queryFn: async () => (await api.post('/sgs/exporte', { mes, anio })).data,
    refetchOnWindowFocus: false,
    retry: false,
    staleTime: 5 * 60 * 1000,
  })

  useEffect(() => { setZona(null); setSearch(''); setProcFilter(''); setLiderSel([]); setLiqFilter(null) }, [mes, anio])

  // ── Liquidadores (pills con MO total, sobre el consolidado completo) ────────
  const liquidadores = useMemo(() => {
    const m = new Map()
    ;(data?.consolidado || []).forEach(p => (p.liquidadores_det || []).forEach(l => {
      m.set(l.nombre, (m.get(l.nombre) || 0) + l.mo)
    }))
    return [...m.entries()].sort((a, b) => b[1] - a[1])
  }, [data])

  const lideres = useMemo(() => {
    const s = new Set()
    ;(data?.consolidado || []).forEach(p => (p.lideres_list || []).forEach(l => s.add(l.nombre)))
    return [...s].sort()
  }, [data])

  const matchSearch = (hay) => {
    const q = search.toLowerCase().trim()
    return !q || q.split(/\s+/).every(t => hay.toLowerCase().includes(t))
  }

  // ── Filtrado por vista ──────────────────────────────────────────────────────
  const consolidadoView = useMemo(() => (data?.consolidado || []).filter(p => {
    if (zona && p.zona !== zona) return false
    if (procFilter && p.proceso !== procFilter) return false
    if (liderSel.length && !(p.lideres_list || []).some(l => liderSel.includes(l.nombre))) return false
    if (liqFilter && !(p.liquidadores || []).includes(liqFilter)) return false
    return matchSearch(`${p.pro_code} ${p.pdl_code} ${p.lcl} ${p.lider || ''} ${p.liquidado_por || ''} ${p.nombre_pro || ''} ${p.zona}`)
  }), [data, zona, procFilter, liderSel, liqFilter, search])

  const detalleView = useMemo(() => (data?.detalle || []).filter(d => {
    if (zona && d.zona !== zona) return false
    if (procFilter && d.proceso !== procFilter) return false
    if (liderSel.length && !liderSel.includes(d.lider)) return false
    if (liqFilter && d.liquidador !== liqFilter) return false
    return matchSearch(`${d.pro_code} ${d.pdl_code} ${d.lcl} ${d.lider || ''} ${d.liquidador || ''} ${d.nombre_pro || ''} ${d.zona}`)
  }), [data, zona, procFilter, liderSel, liqFilter, search])

  const esDetalle = vista === 'detalle'
  const listaView = esDetalle ? detalleView : consolidadoView
  const listaFull = esDetalle ? (data?.detalle || []) : (data?.consolidado || [])
  const totalView = useMemo(() => listaView.reduce((s, x) => s + (x.man_obra || 0), 0), [listaView])
  const hayFiltros = zona || search || procFilter || liderSel.length > 0 || liqFilter

  const descargarExcel = async (z) => {
    setDescargando(z ?? 'todas')
    try {
      const res = await api.post('/sgs/exporte/excel', { mes, anio, zona: z || null }, { responseType: 'blob' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([res.data]))
      const disp = res.headers['content-disposition'] || ''
      a.download = disp.match(/filename="(.+)"/)?.[1] || `SGS_${mes}_${anio}.xlsx`
      a.click()
      toast.success('Excel descargado')
    } catch {
      toast.error('No se pudo generar el Excel')
    } finally {
      setDescargando(null)
    }
  }

  const totZona = (z) => (data?.totales_zona || []).find(t => t.zona === z)

  // ── Datos de gráficas (respetan los filtros activos) ────────────────────────
  const chartZona = useMemo(() => ZONAS.map(z => ({
    zona: z, mo: consolidadoView.filter(p => p.zona === z).reduce((s, p) => s + p.man_obra, 0),
  })).filter(d => d.mo !== 0), [consolidadoView])

  const chartProc = useMemo(() => [
    { name: 'Mayor Impacto (MI)', value: consolidadoView.filter(p => p.proceso === 'MI').reduce((s, p) => s + p.man_obra, 0), fill: MI_HEX },
    { name: 'Programado (PG)',    value: consolidadoView.filter(p => p.proceso === 'PG').reduce((s, p) => s + p.man_obra, 0), fill: PG_HEX },
  ].filter(d => d.value > 0), [consolidadoView])

  const chartLideres = useMemo(() => {
    const m = {}
    consolidadoView.forEach(p => (p.lideres_list || []).forEach(l => { m[l.nombre] = (m[l.nombre] || 0) + l.mo }))
    return Object.entries(m).sort((a, b) => b[1] - a[1]).slice(0, 12)
      .map(([nombre, mo]) => ({ nombre: cleanName(nombre), mo }))
  }, [consolidadoView])

  const chartFechas = useMemo(() => {
    const m = {}
    detalleView.forEach(d => { if (d.fecha) m[d.fecha] = (m[d.fecha] || 0) + d.man_obra })
    return Object.entries(m).sort((a, b) => a[0].localeCompare(b[0]))
      .map(([fecha, mo]) => ({ fecha: fmtFecha(fecha), mo }))
  }, [detalleView])

  const fmtM = (v) => `$${(v / 1e6).toFixed(1)}M`

  return (
    <div className="p-4 md:p-6 max-w-7xl mx-auto space-y-4">

      {/* Header */}
      <div className="flex items-center gap-2">
        <FileSpreadsheet size={22} className="text-green-700" />
        <div>
          <h1 className="text-lg font-bold text-gray-800">SGS Exporte</h1>
          <p className="text-xs text-gray-400">Todo lo liquidado en SGS (mismas cifras que el exporte SGS, sin filtro de liquidador) · OTROS = deptos fuera de zona · solo consulta</p>
        </div>
      </div>

      {/* Selector de periodo + exportes */}
      <div className="bg-white rounded-xl border p-3 flex items-center gap-2 flex-wrap">
        <span className="text-xs text-gray-500">Periodo:</span>
        <select value={mes} onChange={e => setMes(+e.target.value)}
          className="border rounded-lg px-2 py-1.5 text-sm text-gray-700 bg-white">
          {MESES.slice(1).map((m, i) => <option key={i + 1} value={i + 1}>{m}</option>)}
        </select>
        <select value={anio} onChange={e => setAnio(+e.target.value)}
          className="border rounded-lg px-2 py-1.5 text-sm text-gray-700 bg-white">
          {anios.map(a => <option key={a} value={a}>{a}</option>)}
        </select>
        {isFetching && !isLoading && <Loader2 size={15} className="animate-spin text-blue-500" />}

        <div className="ml-auto flex items-center gap-1.5 flex-wrap">
          <button onClick={() => descargarExcel(null)} disabled={!!descargando || isLoading}
            title="Un archivo con RESUMEN + hoja por zona + Prestaciones + Análisis PDL"
            className="flex items-center gap-1.5 text-xs bg-green-700 text-white px-3 py-1.5 rounded-lg hover:bg-green-800 disabled:opacity-50">
            {descargando === 'todas' ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
            Excel completo
          </button>
          {ZONAS.map(z => (
            <button key={z} onClick={() => descargarExcel(z)} disabled={!!descargando || isLoading}
              title={`Excel solo de ${z} (consolidado + prestaciones + PDL)`}
              className="flex items-center gap-1 text-xs border border-green-700 text-green-700 px-2 py-1.5 rounded-lg hover:bg-green-50 disabled:opacity-50">
              {descargando === z ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
              {z}
            </button>
          ))}
        </div>
      </div>

      {isLoading && (
        <div className="flex flex-col items-center justify-center py-20 text-gray-500 bg-white rounded-xl border">
          <Loader2 size={32} className="animate-spin mb-3 text-blue-500" />
          Consultando SGS…
          <span className="text-xs text-gray-400 mt-1">Puede tardar unos segundos</span>
        </div>
      )}
      {isError && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-xl p-4 text-sm">
          {error?.response?.data?.detail || 'No se pudo consultar SGS.'}
        </div>
      )}

      {data && (
        <>
          {/* Totales por zona (clic = filtrar) */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2">
            <button type="button" onClick={() => setZona(null)}
              className={`text-left bg-white border rounded-xl px-3 py-2.5 transition-all hover:border-blue-300 ${!zona ? 'ring-2 ring-blue-400 border-blue-300' : ''}`}>
              <div className="text-[10px] uppercase tracking-wide text-gray-400">Total {MESES[mes]}</div>
              <div className={`text-base md:text-lg font-bold ${data.total < 0 ? 'text-red-600' : 'text-blue-700'}`}>{formatCOP(data.total)}</div>
              <div className="text-[11px] text-gray-400">{data.n} proy. · {data.n_prestaciones} prestaciones</div>
            </button>
            {ZONAS.map(z => {
              const t = totZona(z)
              return (
                <button key={z} type="button" onClick={() => setZona(v => v === z ? null : z)}
                  className={`text-left bg-white border rounded-xl px-3 py-2.5 transition-all hover:border-blue-300 ${zona === z ? 'ring-2 ring-blue-400 border-blue-300' : ''}`}>
                  <div className="flex items-center gap-1.5"><ZonaPill z={z} /></div>
                  <div className={`text-base md:text-lg font-bold mt-0.5 ${(t?.total || 0) < 0 ? 'text-red-600' : 'text-gray-800'}`}>{formatCOP(t?.total || 0)}</div>
                  <div className="text-[11px] text-gray-400">{t?.n || 0} proy. · {t?.n_prestaciones || 0} prest.</div>
                </button>
              )
            })}
          </div>

          {/* Tabs de vista */}
          <div className="flex items-center gap-1 bg-white border rounded-xl p-1 w-fit max-w-full overflow-x-auto">
            {VISTAS.map(({ id, icon: Icon, label }) => (
              <button key={id} onClick={() => setVista(id)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition-colors ${
                  vista === id ? 'bg-blue-700 text-white' : 'text-gray-600 hover:bg-gray-100'}`}>
                <Icon size={13} /> {label}
              </button>
            ))}
          </div>

          {/* Filtros */}
          {vista !== 'graficas' && vista !== 'senda' && (
            <div className="space-y-2">
              <div className="flex items-center gap-2 flex-wrap">
                <div className="relative flex-1 min-w-[180px]">
                  <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
                  <input value={search} onChange={e => setSearch(e.target.value)}
                    placeholder="Buscar por PRO, LCL, PDL, líder, liquidador, nombre…"
                    className="w-full pl-7 pr-2 py-1.5 text-xs border rounded-lg bg-white focus:outline-none focus:ring-1 focus:ring-blue-400" />
                </div>
                <select value={procFilter} onChange={e => setProcFilter(e.target.value)}
                  className="border rounded-lg px-2 py-1.5 text-xs text-gray-700 bg-white">
                  <option value="">Proceso: todos</option>
                  <option value="MI">MI — Mayor Impacto (DF)</option>
                  <option value="PG">PG — Programado (LCL)</option>
                </select>
                <LiderExcelFilter lideres={lideres} sel={liderSel} onApply={setLiderSel} />
                {hayFiltros && (
                  <button onClick={() => { setZona(null); setSearch(''); setProcFilter(''); setLiderSel([]); setLiqFilter(null) }}
                    className="flex items-center gap-1 text-xs text-gray-500 border px-2 py-1.5 rounded-lg bg-white hover:bg-gray-50">
                    <X size={12} /> Limpiar
                  </button>
                )}
                <span className="text-xs text-gray-500 ml-auto whitespace-nowrap">
                  {listaView.length !== listaFull.length ? `${listaView.length} de ${listaFull.length}` : listaView.length}
                  {esDetalle ? ' prest.' : ' proy.'} · <b className={moCls(totalView)}>{formatCOP(totalView)}</b>
                </span>
              </div>

              {/* Pills de liquidador */}
              {liquidadores.length > 0 && (
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="text-[11px] text-gray-400">Liquidador:</span>
                  <button onClick={() => setLiqFilter(null)}
                    className={`px-2 py-0.5 rounded-full text-[11px] font-medium border transition-colors ${
                      !liqFilter ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'}`}>
                    Todos
                  </button>
                  {liquidadores.map(([nombre, mo]) => (
                    <button key={nombre} onClick={() => setLiqFilter(f => f === nombre ? null : nombre)}
                      title={`${nombre} — ${formatCOP(mo)}`}
                      className={`px-2 py-0.5 rounded-full text-[11px] font-medium border transition-colors whitespace-nowrap ${
                        liqFilter === nombre ? 'bg-sky-600 text-white border-sky-600' : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'}`}>
                      {cleanName(nombre)}
                    </button>
                  ))}
                  {liqFilter && (
                    <span className="text-[11px] text-sky-700 font-semibold ml-1">
                      Total {cleanName(liqFilter)}: {formatCOP(totalView)}
                    </span>
                  )}
                </div>
              )}
            </div>
          )}

          {/* ── Vista: Consolidado ─────────────────────────────────────── */}
          {vista === 'consolidado' && (
            <div className="bg-white rounded-xl border overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-gray-50 text-gray-500 text-[10px] uppercase tracking-wide">
                    <th className="text-left px-3 py-2 whitespace-nowrap">Fecha</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap">PDL / OT</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap">Líder</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap">LCL</th>
                    <th className="text-left px-3 py-2">Proc</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap"># Cod ERP</th>
                    <th className="text-right px-3 py-2 whitespace-nowrap">$ Man Obra</th>
                    <th className="text-left px-3 py-2">Zona</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap">Liquidado por</th>
                    <th className="text-left px-3 py-2">Estado</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {consolidadoView.map(p => (
                    <tr key={`${p.pro_id}-${p.pdl_code}`} className={p.proceso === 'MI' ? 'bg-blue-50/40 hover:bg-blue-50' : 'bg-green-50/30 hover:bg-green-50/60'}>
                      <td className="px-3 py-1.5 whitespace-nowrap text-gray-600">{rangoFechas(p)}</td>
                      <td className="px-3 py-1.5 max-w-[190px] truncate text-gray-700" title={`${p.pdl_code}\n${p.nombre_pro || ''}`}>{p.pdl_code || '—'}</td>
                      <td className="px-3 py-1.5 whitespace-nowrap text-gray-700" title={p.lider}>{cleanName(p.lider) || '—'}</td>
                      <td className="px-3 py-1.5 whitespace-nowrap text-gray-600 font-mono text-[11px]">{p.lcl !== 'N/A' ? p.lcl : '—'}</td>
                      <td className="px-3 py-1.5"><ProcBadge p={p.proceso} /></td>
                      <td className="px-3 py-1.5 whitespace-nowrap font-mono text-[11px] font-semibold text-blue-700">
                        {p.pro_code}
                        {(p.nodos_grupo?.length || 0) > 1 && (
                          <span className="ml-1 text-[10px] text-gray-400 font-sans"
                            title={p.nodos_grupo.map(n => `${n.pro_code} · ${formatCOP(n.mo)}`).join('\n')}>
                            ×{p.nodos_grupo.length} nodos
                          </span>
                        )}
                      </td>
                      <td className={`px-3 py-1.5 text-right font-semibold whitespace-nowrap ${moCls(p.man_obra)}`}>{formatCOP(p.man_obra)}</td>
                      <td className="px-3 py-1.5" title={p.nota_zona || p.departamento}><ZonaPill z={p.zona} /></td>
                      <td className="px-3 py-1.5"><LiqBadges liqs={p.liquidadores_det} liquidado_por={p.liquidado_por} /></td>
                      <td className="px-3 py-1.5"><EstadoBadge p={p} /></td>
                    </tr>
                  ))}
                  {!consolidadoView.length && (
                    <tr><td colSpan={10} className="px-3 py-8 text-center text-gray-400">Sin proyectos que coincidan con el filtro.</td></tr>
                  )}
                </tbody>
                {consolidadoView.length > 0 && (
                  <tfoot>
                    <tr className="bg-gray-50 font-bold">
                      <td colSpan={6} className="px-3 py-2 text-right text-gray-600">TOTAL</td>
                      <td className={`px-3 py-2 text-right whitespace-nowrap ${moCls(totalView)}`}>{formatCOP(totalView)}</td>
                      <td colSpan={3} />
                    </tr>
                  </tfoot>
                )}
              </table>
            </div>
          )}

          {/* ── Vista: Prestaciones individuales ───────────────────────── */}
          {vista === 'detalle' && (
            <div className="bg-white rounded-xl border overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-gray-50 text-gray-500 text-[10px] uppercase tracking-wide">
                    <th className="text-left px-3 py-2 whitespace-nowrap">Fecha</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap"># Cod ERP</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap">PDL / OT</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap">Nombre proyecto</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap">Líder</th>
                    <th className="text-left px-3 py-2">Proc</th>
                    <th className="text-left px-3 py-2">Zona</th>
                    <th className="text-right px-3 py-2 whitespace-nowrap">$ Prestación</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap">Liquidado por</th>
                    <th className="text-left px-3 py-2">Estado</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {detalleView.map((d, i) => (
                    <tr key={`${d.pro_id}-${i}`} className={d.proceso === 'MI' ? 'bg-blue-50/40 hover:bg-blue-50' : 'bg-green-50/30 hover:bg-green-50/60'}>
                      <td className="px-3 py-1.5 whitespace-nowrap text-gray-600">{fmtFecha(d.fecha)}</td>
                      <td className="px-3 py-1.5 whitespace-nowrap font-mono text-[11px] font-semibold text-blue-700">{d.pro_code}</td>
                      <td className="px-3 py-1.5 max-w-[160px] truncate text-gray-700" title={d.pdl_code}>{d.pdl_code || '—'}</td>
                      <td className="px-3 py-1.5 max-w-[200px] truncate text-gray-500 text-[11px]" title={d.nombre_pro}>{d.nombre_pro || '—'}</td>
                      <td className="px-3 py-1.5 whitespace-nowrap text-gray-700" title={d.lider}>{cleanName(d.lider) || '—'}</td>
                      <td className="px-3 py-1.5"><ProcBadge p={d.proceso} /></td>
                      <td className="px-3 py-1.5" title={d.nota_zona || d.departamento}><ZonaPill z={d.zona} /></td>
                      <td className={`px-3 py-1.5 text-right font-semibold whitespace-nowrap ${moCls(d.man_obra)}`}>{formatCOP(d.man_obra)}</td>
                      <td className="px-3 py-1.5 whitespace-nowrap text-gray-600" title={d.liquidador}>{cleanName(d.liquidador) || '—'}</td>
                      <td className="px-3 py-1.5 text-gray-500 text-[11px]">{d.state || '—'}</td>
                    </tr>
                  ))}
                  {!detalleView.length && (
                    <tr><td colSpan={10} className="px-3 py-8 text-center text-gray-400">Sin prestaciones que coincidan con el filtro.</td></tr>
                  )}
                </tbody>
                {detalleView.length > 0 && (
                  <tfoot>
                    <tr className="bg-gray-50 font-bold">
                      <td colSpan={7} className="px-3 py-2 text-right text-gray-600">TOTAL</td>
                      <td className={`px-3 py-2 text-right whitespace-nowrap ${moCls(totalView)}`}>{formatCOP(totalView)}</td>
                      <td colSpan={2} />
                    </tr>
                  </tfoot>
                )}
              </table>
            </div>
          )}

          {/* ── Vista: Análisis PDL ────────────────────────────────────── */}
          {vista === 'pdl' && (
            <div className="bg-white rounded-xl border overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-gray-50 text-gray-500 text-[10px] uppercase tracking-wide">
                    <th className="text-left px-3 py-2 whitespace-nowrap">Fechas</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap">PDL / OT</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap">LCL</th>
                    <th className="text-left px-3 py-2">Proc</th>
                    <th className="text-left px-3 py-2">Zona</th>
                    <th className="text-center px-3 py-2 whitespace-nowrap"># Nodos</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap">Clasificación</th>
                    <th className="text-right px-3 py-2 whitespace-nowrap">$ Man Obra</th>
                    <th className="text-left px-3 py-2 whitespace-nowrap">Detalle nodos</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {consolidadoView.map(p => {
                    const cls = p.clasificacion_pdl || ''
                    const clsColor = cls === 'Nodo único' ? 'bg-green-100 text-green-700'
                      : cls.startsWith('Multi-nodo simultáneo') ? 'bg-amber-100 text-amber-700'
                      : 'bg-red-100 text-red-700'
                    return (
                      <tr key={`${p.pro_id}-${p.pdl_code}`} className="hover:bg-blue-50/40">
                        <td className="px-3 py-1.5 whitespace-nowrap text-gray-600">{rangoFechas(p)}</td>
                        <td className="px-3 py-1.5 max-w-[180px] truncate text-gray-700" title={p.pdl_code}>{p.pdl_code || '—'}</td>
                        <td className="px-3 py-1.5 whitespace-nowrap font-mono text-[11px] text-gray-600">{p.lcl !== 'N/A' ? p.lcl : '—'}</td>
                        <td className="px-3 py-1.5"><ProcBadge p={p.proceso} /></td>
                        <td className="px-3 py-1.5" title={p.nota_zona || p.departamento}><ZonaPill z={p.zona} /></td>
                        <td className="px-3 py-1.5 text-center font-bold text-gray-700">{p.nodos_grupo?.length || 1}</td>
                        <td className="px-3 py-1.5 whitespace-nowrap">
                          <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${clsColor}`}>{cls}</span>
                        </td>
                        <td className={`px-3 py-1.5 text-right font-semibold whitespace-nowrap ${moCls(p.man_obra)}`}>{formatCOP(p.man_obra)}</td>
                        <td className="px-3 py-1.5 min-w-[220px]">
                          {(p.nodos_grupo || []).map(n => (
                            <div key={n.pro_code + (n.nodo_id || '')} className="text-[11px] text-gray-500 leading-snug">
                              <span className="font-mono text-gray-700">{n.pro_code}</span>
                              {n.nodo_id ? <span> · N{n.nodo_id}</span> : null}
                              <span className={`ml-1.5 ${moCls(n.mo)}`}>{formatCOP(n.mo)}</span>
                              {n.fecha_primera && <span className="ml-1.5">{fmtFecha(n.fecha_primera)}</span>}
                            </div>
                          ))}
                        </td>
                      </tr>
                    )
                  })}
                  {!consolidadoView.length && (
                    <tr><td colSpan={9} className="px-3 py-8 text-center text-gray-400">Sin contratos que coincidan con el filtro.</td></tr>
                  )}
                </tbody>
              </table>
              {consolidadoView.length > 0 && (
                <div className="flex items-center gap-4 px-3 py-2 border-t text-[11px] text-gray-400 flex-wrap">
                  <span><span className="inline-block w-2.5 h-2.5 rounded-sm bg-green-200 mr-1 align-middle" />Nodo único</span>
                  <span><span className="inline-block w-2.5 h-2.5 rounded-sm bg-amber-200 mr-1 align-middle" />Multi-nodo simultáneo (≤1 día)</span>
                  <span><span className="inline-block w-2.5 h-2.5 rounded-sm bg-red-200 mr-1 align-middle" />Multi-nodo secuencial (+N días)</span>
                </div>
              )}
            </div>
          )}

          {/* ── Vista: Gráficas ────────────────────────────────────────── */}
          {vista === 'graficas' && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <div className="bg-white rounded-xl border p-4">
                <h3 className="text-xs font-bold text-gray-700 uppercase tracking-wide mb-3">MO por zona</h3>
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={chartZona} layout="vertical" margin={{ top: 4, right: 24, left: 8, bottom: 4 }}>
                    <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="#f1f5f9" />
                    <XAxis type="number" tickFormatter={fmtM} tick={{ fontSize: 10, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
                    <YAxis type="category" dataKey="zona" width={70} tick={{ fontSize: 11, fill: '#475569' }} axisLine={false} tickLine={false} />
                    <Tooltip formatter={(v) => [formatCOP(v), 'MO']} />
                    <Bar dataKey="mo" radius={[0, 4, 4, 0]} barSize={20}>
                      {chartZona.map(d => <Cell key={d.zona} fill={ZONA_HEX[d.zona] || '#64748b'} />)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>

              <div className="bg-white rounded-xl border p-4">
                <h3 className="text-xs font-bold text-gray-700 uppercase tracking-wide mb-3">MI vs PG</h3>
                <ResponsiveContainer width="100%" height={220}>
                  <PieChart>
                    <Pie data={chartProc} dataKey="value" nameKey="name" innerRadius={50} outerRadius={80} paddingAngle={2}>
                      {chartProc.map(d => <Cell key={d.name} fill={d.fill} stroke="#fff" strokeWidth={2} />)}
                    </Pie>
                    <Tooltip formatter={(v) => formatCOP(v)} />
                    <Legend iconSize={10} wrapperStyle={{ fontSize: 11 }} />
                  </PieChart>
                </ResponsiveContainer>
              </div>

              <div className="bg-white rounded-xl border p-4">
                <h3 className="text-xs font-bold text-gray-700 uppercase tracking-wide mb-3">Top líderes (MO)</h3>
                <ResponsiveContainer width="100%" height={Math.max(220, chartLideres.length * 24)}>
                  <BarChart data={chartLideres} layout="vertical" margin={{ top: 4, right: 24, left: 8, bottom: 4 }}>
                    <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="#f1f5f9" />
                    <XAxis type="number" tickFormatter={fmtM} tick={{ fontSize: 10, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
                    <YAxis type="category" dataKey="nombre" width={120} tick={{ fontSize: 10, fill: '#475569' }} axisLine={false} tickLine={false} />
                    <Tooltip formatter={(v) => [formatCOP(v), 'MO']} />
                    <Bar dataKey="mo" fill={MI_HEX} radius={[0, 4, 4, 0]} barSize={14} />
                  </BarChart>
                </ResponsiveContainer>
              </div>

              <div className="bg-white rounded-xl border p-4">
                <h3 className="text-xs font-bold text-gray-700 uppercase tracking-wide mb-3">Evolución por fecha</h3>
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={chartFechas} margin={{ top: 4, right: 8, left: 8, bottom: 4 }}>
                    <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#f1f5f9" />
                    <XAxis dataKey="fecha" tick={{ fontSize: 9, fill: '#94a3b8' }} angle={-45} textAnchor="end" height={40} axisLine={false} tickLine={false} />
                    <YAxis tickFormatter={fmtM} tick={{ fontSize: 10, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
                    <Tooltip formatter={(v) => [formatCOP(v), 'MO']} />
                    <Bar dataKey="mo" fill={PG_HEX} radius={[3, 3, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}

          {/* ── Vista: Senda de meta ───────────────────────────────────── */}
          {vista === 'senda' && <SendaMetaTab mes={mes} anio={anio} />}
        </>
      )}
    </div>
  )
}
