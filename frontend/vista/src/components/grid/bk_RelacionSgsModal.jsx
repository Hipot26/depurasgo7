import { useState, useMemo, useEffect } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import api from '../../utils/api'
import { formatCOP } from '../../utils/format'
import toast from 'react-hot-toast'
import { X, Link2, AlertTriangle, Check, Search, Loader2, ArrowRight } from 'lucide-react'

const MESES = ['','Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre']
const ZONAS = ['UBATE', 'GUAVIO', 'LV&TLC', 'ALMEIDAS']

const fmtFecha = (v) => {
  if (!v) return '—'
  const [y, m, d] = String(v).slice(0, 10).split('-')
  return (y && m && d) ? `${d}/${m}` : String(v)
}
const cleanName = (n) => (n || '').split(/\s+/).slice(0, 2).join(' ')
const cleanLiq  = (n) => (n || '').split(/\s+/).slice(0, 2).join(' ')

const ZONA_COLORS = {
  ALMEIDAS: 'bg-purple-100 text-purple-700', GUAVIO: 'bg-orange-100 text-orange-700',
  UBATE: 'bg-teal-100 text-teal-700', 'LV&TLC': 'bg-pink-100 text-pink-700',
}
const ZonaPill = ({ z }) => z ? (
  <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${ZONA_COLORS[z] || 'bg-gray-100 text-gray-600'}`}>{z}</span>
) : null

const Card = ({ label, value, color, active, onClick }) => (
  <button type="button" onClick={onClick} disabled={!onClick}
    className={`text-left bg-white border rounded-lg px-3 py-2 flex-1 min-w-[110px] transition-all ${
      onClick ? 'cursor-pointer hover:border-blue-300' : 'cursor-default'} ${active ? 'ring-2 ring-blue-400 border-blue-300' : ''}`}>
    <div className="text-[10px] uppercase tracking-wide text-gray-400">{label}</div>
    <div className={`text-lg font-bold ${color || 'text-gray-800'}`}>{value}</div>
  </button>
)

// Etiqueta de una fila de proyección para el desplegable
const filaLabel = (f) => {
  const code = (f.lcl && f.lcl !== 'N/A') ? `LCL ${f.lcl}` : (f.num_pdt || '—')
  const ya = (f.mo_real_actual && f.mo_real_actual !== 0) ? ` ⚠ ya: ${formatCOP(f.mo_real_actual)}` : ''
  return `#${f.id} · ${cleanName(f.lider)} · ${fmtFecha(f.fecha)} · ${code}${ya}`
}

export default function RelacionSgsModal({ mes: mesInicial, anio: anioInicial, unidad, onClose, onAplicado }) {
  const [zona, setZona] = useState(null)
  const [mes, setMes]   = useState(mesInicial)
  const [anio, setAnio] = useState(anioInicial)
  const anios = useMemo(() => {
    const hasta = Math.max(new Date().getFullYear() + 1, anioInicial)
    return Array.from({ length: hasta - 2025 + 1 }, (_, i) => 2025 + i)
  }, [anioInicial])

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['sgs-relacionar', unidad, mes, anio, zona],
    queryFn: async () => {
      const { data } = await api.post('/sgs/relacionar', { mes, anio, unidad, zona })
      return data
    },
    refetchOnWindowFocus: false,
    retry: false,
  })

  const [assign, setAssign]   = useState({})   // pro_id -> prog_id (fila elegida)
  const [incl, setIncl]       = useState({})   // pro_id -> bool (incluir en aplicar)
  const [manual, setManual]   = useState({})   // dudoso pro_id -> prog_id
  const [pickFor, setPickFor] = useState(null)
  const [pickQ, setPickQ]     = useState('')
  const [stateFilter, setStateFilter] = useState(null) // 'conflicto' | 'dudoso' | null
  const [liqFilter, setLiqFilter]     = useState('')
  const [search, setSearch]           = useState('')

  // Mapa fila_id -> fila (para detectar conflictos y mostrar info)
  const filaById = useMemo(() => {
    const m = {}
    ;(data?.grupos || []).forEach(g => g.filas.forEach(f => { m[f.id] = f }))
    return m
  }, [data])

  // Inicializar asignación/inclusión al cargar: usar la sugerencia por fecha;
  // desmarcar las que caen en una fila que YA tiene MO real (conflicto).
  useEffect(() => {
    if (!data?.grupos) return
    const a = {}, inc = {}
    for (const g of data.grupos) {
      for (const s of g.sgs) {
        a[s.pro_id] = s.sugerido_prog_id ?? null
        const f = g.filas.find(x => x.id === s.sugerido_prog_id)
        inc[s.pro_id] = !!s.sugerido_prog_id && !(f && f.mo_real_actual && f.mo_real_actual !== 0)
      }
    }
    setAssign(a); setIncl(inc)
  }, [data])

  useEffect(() => { setStateFilter(null); setLiqFilter(''); setSearch('') }, [zona, mes, anio])
  // Los vínculos manuales apuntan a filas del mes consultado: al cambiar de periodo dejan de ser válidos.
  useEffect(() => { setManual({}); setPickFor(null) }, [mes, anio])

  const aplicar = useMutation({
    mutationFn: (cambios) => api.post('/sgs/aplicar', { cambios }),
    onSuccess: (res) => {
      toast.success(`${res.data.actualizadas} fila(s) actualizada(s) con datos de SGS`)
      onAplicado?.(); onClose()
    },
    onError: (err) => toast.error(err.response?.data?.detail || 'Error al aplicar'),
  })

  // ── Construir cambios (sumando por fila si varias liquidaciones van a la misma) ──
  const cambios = useMemo(() => {
    if (!data) return []
    const byRow = {}
    const add = (pid, mo, pro, fecha) => {
      if (!pid) return
      byRow[pid] = byRow[pid] || { mo: 0, pros: [], fecha: null }
      byRow[pid].mo += mo
      if (pro) byRow[pid].pros.push(pro)
      if (fecha && (!byRow[pid].fecha || fecha > byRow[pid].fecha)) byRow[pid].fecha = fecha
    }
    for (const g of data.grupos) for (const s of g.sgs) {
      if (!incl[s.pro_id]) continue
      add(assign[s.pro_id] ?? s.sugerido_prog_id, s.man_obra, s.pro_code, s.fecha_liq)
    }
    for (const n of data.sgs_sin_relacion || []) {
      if (manual[n.pro_id]) add(manual[n.pro_id], n.man_obra, n.pro_code, n.fecha_liq)
    }
    return Object.entries(byRow).map(([pid, v]) => ({
      prog_id: +pid, mo_real: Math.round(v.mo * 100) / 100,
      num_proliquidado: v.pros.join('+'), fecha_liquidacion_sgs: v.fecha,
    }))
  }, [data, assign, incl, manual])

  // ── Filtros ───────────────────────────────────────────────────────────────
  const liquidadores = useMemo(() => {
    const m = new Map()
    if (data) {
      data.grupos.forEach(g => (g.liquidadores || []).forEach(l => m.set(l, cleanLiq(l))))
      ;(data.sgs_sin_relacion || []).forEach(n => (n.liquidadores || []).forEach(l => m.set(l, cleanLiq(l))))
    }
    return [...m.entries()].sort((a, b) => a[1].localeCompare(b[1]))
  }, [data])

  const matchSearch = (hay) => {
    const q = search.toLowerCase().trim()
    return !q || q.split(/\s+/).every(t => hay.toLowerCase().includes(t))
  }

  // Una liquidación SGS está en conflicto si su fila destino actual ya tiene MO real.
  const sgsConflict = (g, s) => {
    const f = g.filas.find(x => x.id === (assign[s.pro_id] ?? s.sugerido_prog_id))
    return !!(f && f.mo_real_actual && f.mo_real_actual !== 0)
  }

  const gruposView = useMemo(() => {
    if (!data) return []
    if (stateFilter === 'dudoso') return []
    return data.grupos.filter(g => {
      if (liqFilter && !(g.liquidadores || []).includes(liqFilter)) return false
      if (stateFilter === 'conflicto' && !g.sgs.some(s => sgsConflict(g, s))) return false
      const hay = `${g.etiqueta} ${g.zona} ` +
        g.sgs.map(s => `${s.pro_code} ${s.lcl} ${s.pdl_code} ${s.liquidado_por} ${s.lider_sgs || ''}`).join(' ') + ' ' +
        g.filas.map(f => `${f.id} ${f.lider || ''} ${f.lcl || ''} ${f.num_pdt || ''}`).join(' ')
      return matchSearch(hay)
    })
  }, [data, stateFilter, liqFilter, search, assign])

  const dudososView = useMemo(() => {
    if (!data || (stateFilter && stateFilter !== 'dudoso')) return []
    return (data.sgs_sin_relacion || []).filter(n => {
      if (liqFilter && !(n.liquidadores || []).includes(liqFilter)) return false
      return matchSearch(`${n.pro_code} ${n.lider || ''} ${n.lcl || ''} ${n.pdl_code || ''} ${n.liquidado_por || ''} ${n.zona}`)
    })
  }, [data, stateFilter, liqFilter, search])

  const libres = data?.prog_sin_relacion || []
  const libresFiltradas = useMemo(() => {
    const node = (data?.sgs_sin_relacion || []).find(n => n.pro_id === pickFor)
    const q = (pickQ || (node ? `${node.lider || ''} ${node.lcl !== 'N/A' ? node.lcl : ''}` : '')).toLowerCase().trim()
    const tokens = q.split(/\s+/).filter(Boolean)
    const ya = new Set(Object.values(manual))
    const arr = libres.filter(r => !ya.has(r.id) || r.id === manual[pickFor])
    if (!tokens.length) return arr.slice(0, 40)
    return arr.filter(r => tokens.some(t => `${r.id} ${r.lider || ''} ${r.lcl || ''} ${r.num_pdt || ''} ${r.fecha || ''}`.toLowerCase().includes(t))).slice(0, 40)
  }, [libres, pickFor, pickQ, manual, data])

  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-2 sm:p-4" onClick={onClose}>
      <div className="bg-gray-50 rounded-xl shadow-2xl w-full max-w-6xl max-h-[92vh] flex flex-col" onClick={e => e.stopPropagation()}>

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b bg-white rounded-t-xl shrink-0">
          <div className="flex items-center gap-2">
            <Link2 size={18} className="text-blue-600" />
            <div>
              <h2 className="text-sm font-bold text-gray-800">Relación con SGS</h2>
              <p className="text-xs text-gray-400">{MESES[mes]} {anio} · {unidad} · agrupado por proyecto / PDL</p>
            </div>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={20} /></button>
        </div>

        {/* Zona + periodo */}
        <div className="flex items-center gap-1.5 px-4 py-2 border-b bg-white shrink-0 overflow-x-auto">
          <span className="text-[11px] text-gray-400 mr-1">Zona:</span>
          {[null, ...ZONAS].map((z) => (
            <button key={z || 'todas'} onClick={() => setZona(z)}
              className={`px-2.5 py-1 rounded-full text-xs font-medium whitespace-nowrap border transition-colors ${
                zona === z ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'}`}>
              {z || 'Todas'}
            </button>
          ))}
          <div className="ml-auto flex items-center gap-1.5 pl-3">
            <span className="text-[11px] text-gray-400">Mes:</span>
            <select value={mes} onChange={e => setMes(+e.target.value)}
              className="border rounded-lg px-2 py-1 text-xs text-gray-700 bg-white">
              {MESES.slice(1).map((m, i) => <option key={i + 1} value={i + 1}>{m}</option>)}
            </select>
            <select value={anio} onChange={e => setAnio(+e.target.value)}
              className="border rounded-lg px-2 py-1 text-xs text-gray-700 bg-white">
              {anios.map(a => <option key={a} value={a}>{a}</option>)}
            </select>
            {(mes !== mesInicial || anio !== anioInicial) && (
              <button onClick={() => { setMes(mesInicial); setAnio(anioInicial) }}
                title="Volver al mes del grid"
                className="text-[11px] text-gray-400 hover:text-blue-600 whitespace-nowrap">↩ {MESES[mesInicial]}</button>
            )}
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {isLoading && (
            <div className="flex flex-col items-center justify-center py-16 text-gray-500">
              <Loader2 size={32} className="animate-spin mb-3 text-blue-500" />
              Consultando SGS y agrupando por proyecto…
              <span className="text-xs text-gray-400 mt-1">Puede tardar unos segundos</span>
            </div>
          )}
          {isError && (
            <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-4 text-sm">
              {error?.response?.data?.detail || 'No se pudo consultar SGS.'}
            </div>
          )}

          {data && (
            <>
              {/* Resumen */}
              <div className="flex flex-wrap gap-2">
                <Card label="Total SGS mes" value={formatCOP(data.sgs_total)} color="text-blue-700" />
                <Card label="Proyectos (grupos)" value={data.resumen.n_grupos} color="text-gray-800" />
                <Card label="Liquidaciones" value={data.resumen.n_sgs} color="text-green-600" />
                <Card label="Conflictos" value={data.resumen.n_conflicto} color="text-red-600"
                  active={stateFilter === 'conflicto'} onClick={() => setStateFilter(f => f === 'conflicto' ? null : 'conflicto')} />
                <Card label="SGS sin grupo" value={data.resumen.n_sgs_dudoso} color="text-gray-600"
                  active={stateFilter === 'dudoso'} onClick={() => setStateFilter(f => f === 'dudoso' ? null : 'dudoso')} />
              </div>

              {/* Filtros */}
              <div className="flex items-center gap-2 flex-wrap">
                <div className="relative flex-1 min-w-[180px]">
                  <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
                  <input value={search} onChange={e => setSearch(e.target.value)}
                    placeholder="Buscar por LCL, PDL, PRO, líder, # PDT…"
                    className="w-full pl-7 pr-2 py-1.5 text-xs border rounded-lg focus:outline-none focus:ring-1 focus:ring-blue-400" />
                </div>
                <select value={liqFilter} onChange={e => setLiqFilter(e.target.value)}
                  className="border rounded-lg px-2 py-1.5 text-xs text-gray-700 max-w-[220px]">
                  <option value="">Liquidador: todos</option>
                  {liquidadores.map(([full, label]) => <option key={full} value={full}>{label}</option>)}
                </select>
                {(stateFilter || liqFilter || search) && (
                  <button onClick={() => { setStateFilter(null); setLiqFilter(''); setSearch('') }}
                    className="flex items-center gap-1 text-xs text-gray-500 border px-2 py-1.5 rounded-lg hover:bg-gray-50">
                    <X size={12} /> Limpiar
                  </button>
                )}
              </div>
              <p className="text-[11px] text-gray-400 -mt-2">
                Cada proyecto/PDL muestra sus liquidaciones SGS y a qué fila va cada monto (sugerido <b>por fecha</b>, el líder no influye).
                Cambia la fila en el desplegable si hace falta y revisa que el total SGS cuadre con lo proyectado.
              </p>

              {/* ── Grupos ─────────────────────────────────────────────────── */}
              {stateFilter !== 'dudoso' && gruposView.map(g => (
                <div key={g.grupo_id} className="rounded-lg border border-gray-200 bg-white">
                  <div className="flex items-center gap-2 flex-wrap px-3 py-2 border-b bg-gray-50/70 rounded-t-lg">
                    <ZonaPill z={g.zona} />
                    <b className="text-xs text-gray-800">{g.etiqueta}</b>
                    <span className="text-[11px] text-gray-400">{g.n_sgs} liq · {g.n_filas} filas</span>
                    <span className="ml-auto text-xs">
                      <span className="text-gray-400">SGS </span><b className="text-green-700">{formatCOP(g.sgs_total)}</b>
                      <span className="text-gray-300"> · </span>
                      <span className="text-gray-400">proy. </span><span className="text-gray-600">{formatCOP(g.mo_proy_total)}</span>
                    </span>
                  </div>
                  <div className="divide-y">
                    {(stateFilter === 'conflicto' ? g.sgs.filter(s => sgsConflict(g, s)) : g.sgs).map(s => {
                      const pid = assign[s.pro_id] ?? s.sugerido_prog_id
                      const f = pid ? g.filas.find(x => x.id === pid) : null
                      const conflict = f && f.mo_real_actual && f.mo_real_actual !== 0
                      return (
                        <div key={s.pro_id} className={`flex items-start gap-2 px-3 py-2 ${conflict ? 'bg-red-50/40' : ''}`}>
                          <input type="checkbox" className="mt-1 accent-blue-600"
                            checked={!!incl[s.pro_id]}
                            onChange={e => setIncl(m => ({ ...m, [s.pro_id]: e.target.checked }))} />
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-1.5 flex-wrap text-xs">
                              <b className="text-blue-700">{s.pro_code}</b>
                              <b className="text-green-700">{formatCOP(s.man_obra)}</b>
                              <span className="text-gray-400">· {s.proceso} · {fmtFecha(s.fecha_liq)} · liq. {s.liquidado_por || '—'}</span>
                              {s.lider_sgs && <span className="text-gray-300">· ejec. {cleanName(s.lider_sgs)}</span>}
                            </div>
                            <div className="flex items-center gap-1.5 mt-1">
                              <ArrowRight size={13} className="text-gray-400 shrink-0" />
                              <select value={pid || ''}
                                onChange={e => setAssign(m => ({ ...m, [s.pro_id]: e.target.value ? +e.target.value : null }))}
                                className={`text-xs border rounded px-1 py-0.5 max-w-[420px] flex-1 ${conflict ? 'border-red-300' : ''}`}>
                                <option value="">— sin asignar —</option>
                                {g.filas.map(ff => <option key={ff.id} value={ff.id}>{filaLabel(ff)}</option>)}
                              </select>
                            </div>
                            {conflict && (
                              <div className="flex items-center gap-1 mt-1 text-[11px] text-red-600">
                                <AlertTriangle size={12} />
                                La fila #{f.id} ya tenía {formatCOP(f.mo_real_actual)} → se sobrescribe con {formatCOP(s.man_obra)} solo si la dejas marcada.
                              </div>
                            )}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              ))}
              {stateFilter !== 'dudoso' && !gruposView.length && (
                <div className="text-xs text-gray-400 py-2">Sin proyectos que coincidan con el filtro.</div>
              )}

              {/* ── SGS sin relación (dudosos) ────────────────────────────── */}
              {dudososView.length > 0 && (
                <div>
                  <h3 className="text-xs font-bold text-gray-700 uppercase tracking-wide mb-1">
                    SGS sin grupo — vincular a mano ({dudososView.length})
                  </h3>
                  <p className="text-[11px] text-gray-400 mb-2 leading-snug">
                    ⓘ No comparten PRO/LCL/DF con ninguna fila del mes (trabajo liquidado en otro mes o no programado). Vincúlalos a una fila a mano si corresponde.
                  </p>
                  <div className="space-y-1.5">
                    {dudososView.map(n => {
                      const linkedId = manual[n.pro_id]
                      const linked = linkedId && libres.find(r => r.id === linkedId)
                      return (
                        <div key={n.pro_id} className="rounded-lg border border-gray-200 bg-white px-2.5 py-2">
                          <div className="flex items-center gap-2 flex-wrap text-xs">
                            <ZonaPill z={n.zona} />
                            <b className="text-blue-700">{n.pro_code}</b>
                            <span className="text-gray-500">{n.proceso} · {n.lcl !== 'N/A' ? `LCL ${n.lcl}` : n.pdl_code} · {fmtFecha(n.fecha_liq)} · liq. {n.liquidado_por || '—'}</span>
                            <b className="text-green-700">{formatCOP(n.man_obra)}</b>
                            <div className="ml-auto flex items-center gap-2">
                              {linked && <span className="text-[11px] text-green-700 flex items-center gap-1"><Check size={12} /> #{linked.id} {cleanName(linked.lider)}</span>}
                              <button onClick={() => { setPickFor(pickFor === n.pro_id ? null : n.pro_id); setPickQ('') }}
                                className="text-[11px] border border-blue-200 text-blue-600 rounded px-2 py-0.5 hover:bg-blue-50">
                                {linked ? 'Cambiar' : 'Relacionar…'}
                              </button>
                              {linked && <button onClick={() => setManual(m => { const x = { ...m }; delete x[n.pro_id]; return x })}
                                className="text-[11px] text-gray-400 hover:text-red-600">quitar</button>}
                            </div>
                          </div>
                          {pickFor === n.pro_id && (
                            <div className="mt-2 border-t pt-2">
                              <div className="relative mb-1.5">
                                <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400" />
                                <input autoFocus value={pickQ} onChange={e => setPickQ(e.target.value)}
                                  placeholder="Buscar fila por líder, LCL, # PDT, fecha…"
                                  className="w-full pl-7 pr-2 py-1 text-xs border rounded" />
                              </div>
                              <div className="max-h-44 overflow-y-auto divide-y">
                                {libresFiltradas.map(r => (
                                  <button key={r.id} onClick={() => { setManual(m => ({ ...m, [n.pro_id]: r.id })); setPickFor(null) }}
                                    className="w-full text-left px-2 py-1 text-xs hover:bg-blue-50">
                                    {filaLabel(r)}{r.mo_proyectada ? <span className="text-gray-400"> · proy. {formatCOP(r.mo_proyectada)}</span> : null}
                                  </button>
                                ))}
                                {!libresFiltradas.length && <div className="text-xs text-gray-400 px-2 py-2">Sin coincidencias.</div>}
                              </div>
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between gap-2 px-4 py-3 border-t bg-white rounded-b-xl shrink-0">
          <span className="text-xs text-gray-500">{cambios.length} fila(s) recibirán MO real</span>
          <div className="flex items-center gap-2">
            <button onClick={onClose} className="text-xs text-gray-600 px-3 py-1.5 rounded border hover:bg-gray-50">Cancelar</button>
            <button onClick={() => aplicar.mutate(cambios)} disabled={!cambios.length || aplicar.isPending}
              className="flex items-center gap-1 text-xs bg-blue-700 text-white px-4 py-1.5 rounded hover:bg-blue-800 disabled:opacity-50">
              {aplicar.isPending ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
              Aplicar {cambios.length || ''} fila(s)
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
