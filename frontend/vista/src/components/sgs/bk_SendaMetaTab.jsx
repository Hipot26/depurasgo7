import { useState, useMemo, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ReferenceLine, ResponsiveContainer,
} from 'recharts'
import toast from 'react-hot-toast'
import { Loader2, Target, Pencil, Plus, Trash2, Save, X, ChevronDown, ChevronRight, CheckCircle2, AlertTriangle, XCircle, Clock } from 'lucide-react'
import api from '../../utils/api'
import { formatCOP } from '../../utils/format'

// ── Senda de meta ────────────────────────────────────────────────────────────
// Avance de la MO liquidada en SGS frente a la meta mensual por senda
// (UBATE TP / LV / MOE), con cortes el día 10, el 20 y el último del mes.
// Las metas son variables por mes (se editan aquí, se guardan en `sgs_metas`).
// Senda esperada: lineal por día calendario. Todo lo calcula el backend
// (`GET /sgs/senda`); este componente solo lo muestra y edita las metas.

const MESES = ['', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']
// Paleta por senda (orden fijo, validada CVD/contraste): teal · azul · rosa · ámbar · gris
const SENDA_HEX = ['#0D9488', '#2563EB', '#DB2777', '#D97706', '#64748B']
const SENDA_LINEAL_HEX = '#94A3B8'

const ESTADO = {
  cumplido:  { label: 'Cumplida',  cls: 'bg-green-100 text-green-700',  Icon: CheckCircle2 },
  en_senda:  { label: 'En senda',  cls: 'bg-green-100 text-green-700',  Icon: CheckCircle2 },
  ajustado:  { label: 'Ajustada',  cls: 'bg-amber-100 text-amber-700',  Icon: AlertTriangle },
  rezago:    { label: 'Rezago',    cls: 'bg-red-100 text-red-700',      Icon: XCircle },
  pendiente: { label: 'Pendiente', cls: 'bg-gray-100 text-gray-500',    Icon: Clock },
}
const CUADRILLAS_LABEL = { todas: 'todas las cuadrillas', moe: 'solo cuadrillas MOE', sin_moe: 'sin cuadrillas MOE' }

const fmtM = (v) => (v == null ? '—' : `$${(v / 1e6).toLocaleString('es-CO', { minimumFractionDigits: 1, maximumFractionDigits: 1 })} M`)
const fmtPct = (v) => (v == null ? '—' : `${v.toLocaleString('es-CO', { maximumFractionDigits: 1 })} %`)
const fmtFecha = (v) => (v ? `${v.slice(8, 10)}/${v.slice(5, 7)}` : '—')
const cleanName = (n) => (n || '').split(/\s+/).slice(0, 2).join(' ')

const EstadoBadge = ({ estado, size = 'xs' }) => {
  const e = ESTADO[estado] || ESTADO.pendiente
  return (
    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded font-semibold whitespace-nowrap ${e.cls} ${size === 'xs' ? 'text-[10px]' : 'text-xs'}`}>
      <e.Icon size={size === 'xs' ? 10 : 12} /> {e.label}
    </span>
  )
}

const filtroTexto = (s) => [
  s.zonas?.length ? s.zonas.join(' + ') : 'todas las zonas',
  s.procesos?.length ? s.procesos.join('/') : null,
  CUADRILLAS_LABEL[s.cuadrillas] || s.cuadrillas,
].filter(Boolean).join(' · ')

// ── Barra de avance con la marca de lo esperado hoy ─────────────────────────
const BarraSenda = ({ pct, esperado, color }) => (
  <div className="relative h-2.5 bg-gray-100 rounded-full overflow-visible" title={`Avance ${fmtPct(pct)} · esperado hoy ${fmtPct(esperado)}`}>
    <div className="h-full rounded-full transition-all" style={{ width: `${Math.min(pct || 0, 100)}%`, background: color }} />
    {esperado != null && (
      <div className="absolute -top-1 w-0.5 h-[18px] bg-gray-700 rounded" style={{ left: `calc(${Math.min(esperado, 100)}% - 1px)` }} />
    )}
  </div>
)

// ── Tarjeta por senda ───────────────────────────────────────────────────────
const SendaCard = ({ s, color, mesEnCurso }) => {
  const [verLideres, setVerLideres] = useState(false)
  const h = s.hoy
  return (
    <div className="bg-white rounded-xl border p-4 space-y-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="inline-block w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: color }} />
            <h3 className="text-sm font-bold text-gray-800 truncate">{s.nombre}</h3>
          </div>
          <div className="text-[11px] text-gray-400 mt-0.5" title={filtroTexto(s)}>{filtroTexto(s)}</div>
        </div>
        <div className="text-right shrink-0">
          <div className="text-[10px] uppercase tracking-wide text-gray-400">Meta</div>
          <div className="text-sm font-bold text-gray-800">{fmtM(s.meta)}</div>
        </div>
      </div>

      <div>
        <div className="flex items-baseline justify-between mb-1.5">
          <span className="text-2xl font-bold text-gray-800">{fmtPct(s.pct_total)}</span>
          <span className="text-xs text-gray-500">{fmtM(s.total)} liquidado · {s.n_prestaciones} prest.</span>
        </div>
        <BarraSenda pct={s.pct_total} esperado={h?.esperado_pct} color={color} />
        {h && (
          <div className="flex items-center justify-between mt-1.5 text-[11px] text-gray-500">
            <span>Esperado al día {h.dia}: <b className="text-gray-700">{fmtPct(h.esperado_pct)}</b></span>
            <EstadoBadge estado={h.estado} />
          </div>
        )}
      </div>

      {/* Cortes cada 10 días */}
      <div className="grid grid-cols-3 gap-1.5">
        {s.cortes.map(c => (
          <div key={c.dia} className={`rounded-lg border px-2 py-1.5 ${c.estado === 'pendiente' ? 'bg-gray-50/60 border-dashed' : 'bg-gray-50'}`}>
            <div className="text-[10px] uppercase tracking-wide text-gray-400">Día {c.dia}{c.es_fin_mes ? ' · cierre' : ''}</div>
            <div className={`text-base font-bold ${c.estado === 'pendiente' ? 'text-gray-300' : 'text-gray-800'}`}>{c.pct == null ? '—' : fmtPct(c.pct)}</div>
            <div className="text-[10px] text-gray-400">de {fmtPct(c.esperado_pct)} esp.</div>
            <div className="mt-1"><EstadoBadge estado={c.estado} /></div>
            {c.acumulado != null && <div className="text-[10px] text-gray-500 mt-0.5">{fmtM(c.acumulado)}</div>}
          </div>
        ))}
      </div>

      {/* Situación a hoy: ritmo, proyección y lo que exige la meta */}
      {h && (
        <div className="rounded-lg bg-blue-50/60 border border-blue-100 px-3 py-2 text-[11px] text-gray-600 space-y-0.5">
          <div className="font-semibold text-gray-700">{h.cerrado ? 'Cierre del mes' : `Al día ${h.dia} (${fmtFecha(h.fecha)})`}</div>
          <div>Ritmo: <b>{fmtM(h.ritmo_dia)}</b>/día → proyección de cierre <b className={h.proyeccion_pct >= 100 ? 'text-green-700' : 'text-red-600'}>{fmtPct(h.proyeccion_pct)}</b> ({fmtM(h.proyeccion)})</div>
          {!h.cerrado && h.faltante > 0 && (
            <div>Faltan <b>{fmtM(h.faltante)}</b>: {h.requerido_habil != null
              ? <>exige <b>{fmtM(h.requerido_habil)}</b>/día hábil en los {h.habiles_restantes} hábiles que quedan</>
              : <>sin días restantes en el mes</>}</div>
          )}
          {!h.cerrado && h.faltante === 0 && <div className="text-green-700 font-semibold">Meta cumplida.</div>}
          {h.cerrado && h.faltante > 0 && <div>Quedaron <b>{fmtM(h.faltante)}</b> por debajo de la meta.</div>}
        </div>
      )}

      {/* Quiénes suman (auditable) */}
      <button type="button" onClick={() => setVerLideres(v => !v)}
        className="flex items-center gap-1 text-[11px] text-blue-700 hover:underline">
        {verLideres ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        {s.lideres.length} líder{s.lideres.length === 1 ? '' : 'es'} suman en esta senda
      </button>
      {verLideres && (
        <div className="border rounded-lg overflow-hidden">
          <table className="w-full text-[11px]">
            <tbody className="divide-y">
              {s.lideres.map(l => (
                <tr key={l.lider} className="hover:bg-gray-50">
                  <td className="px-2 py-1 text-gray-700" title={l.lider}>
                    {cleanName(l.lider)}
                    {l.cuadrilla_moe && <span className="ml-1 px-1 rounded bg-blue-100 text-blue-700 text-[9px] font-semibold" title={`Cuadrilla MOE en la programación: ${l.cuadrilla_moe}`}>MOE</span>}
                  </td>
                  <td className="px-2 py-1 text-gray-400">{l.zonas.join(', ')}</td>
                  <td className="px-2 py-1 text-right text-gray-400">{l.n}</td>
                  <td className={`px-2 py-1 text-right font-semibold ${l.mo < 0 ? 'text-red-600' : 'text-gray-700'}`}>{formatCOP(l.mo)}</td>
                </tr>
              ))}
              {!s.lideres.length && <tr><td className="px-2 py-3 text-center text-gray-400">Nada liquidado todavía en esta senda.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Editor de metas (las variables) ─────────────────────────────────────────
const nuevaFila = () => ({ clave: `senda_${Date.now().toString(36)}`, nombre: '', metaM: '', zonas: [], procesos: [], cuadrillas: 'todas' })

const EditorMetas = ({ data, mes, anio, onClose }) => {
  const qc = useQueryClient()
  const [filas, setFilas] = useState(() => data.sendas.map(s => ({
    clave: s.clave, nombre: s.nombre, metaM: String(s.meta / 1e6), zonas: s.zonas, procesos: s.procesos, cuadrillas: s.cuadrillas,
  })))

  const set = (i, patch) => setFilas(f => f.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const toggle = (i, campo, val) => setFilas(f => f.map((r, j) => {
    if (j !== i) return r
    const arr = r[campo].includes(val) ? r[campo].filter(x => x !== val) : [...r[campo], val]
    return { ...r, [campo]: arr }
  }))

  const guardar = useMutation({
    mutationFn: async () => {
      const metas = filas.map(r => ({
        clave: r.clave, nombre: r.nombre.trim(), meta: Math.round(parseFloat(String(r.metaM).replace(',', '.')) * 1e6),
        zonas: r.zonas, procesos: r.procesos, cuadrillas: r.cuadrillas,
      }))
      if (metas.some(m => !m.nombre || Number.isNaN(m.meta) || m.meta < 0)) throw new Error('Toda senda necesita nombre y una meta en millones (≥ 0).')
      return (await api.put('/sgs/senda/metas', { mes, anio, metas })).data
    },
    onSuccess: () => {
      toast.success(`Metas de ${MESES[mes]} ${anio} guardadas`)
      qc.invalidateQueries({ queryKey: ['sgs-senda', mes, anio] })
      onClose()
    },
    onError: (e) => toast.error(e?.response?.data?.detail || e.message || 'No se pudieron guardar las metas'),
  })

  return (
    <div className="bg-white rounded-xl border-2 border-blue-200 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-bold text-gray-800">Metas de {MESES[mes]} {anio}</h3>
          <p className="text-[11px] text-gray-400">Meta en millones de pesos. Zonas vacías = todas. Cuadrillas MOE = líderes con tipo MOE/MOE2 en la programación del mes.</p>
        </div>
        <button type="button" onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={16} /></button>
      </div>

      <div className="space-y-2">
        {filas.map((r, i) => (
          <div key={r.clave} className="grid grid-cols-1 md:grid-cols-[1.2fr_0.7fr_2fr_1fr_1.2fr_auto] gap-2 items-center border rounded-lg p-2 bg-gray-50/50">
            <input value={r.nombre} onChange={e => set(i, { nombre: e.target.value })} placeholder="Nombre (UBATE TP)"
              className="px-2 py-1.5 text-xs border rounded-lg bg-white focus:outline-none focus:ring-1 focus:ring-blue-400" />
            <div className="relative">
              <input value={r.metaM} onChange={e => set(i, { metaM: e.target.value })} inputMode="decimal" placeholder="520"
                className="w-full pl-2 pr-6 py-1.5 text-xs border rounded-lg bg-white text-right font-semibold focus:outline-none focus:ring-1 focus:ring-blue-400" />
              <span className="absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-gray-400">M</span>
            </div>
            <div className="flex flex-wrap gap-1">
              {data.zonas_disponibles.map(z => (
                <button key={z} type="button" onClick={() => toggle(i, 'zonas', z)}
                  className={`px-1.5 py-0.5 rounded text-[10px] font-semibold border transition-colors ${
                    r.zonas.includes(z) ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-gray-500 border-gray-200 hover:bg-gray-50'}`}>
                  {z}
                </button>
              ))}
            </div>
            <div className="flex gap-1">
              {['MI', 'PG'].map(p => (
                <button key={p} type="button" onClick={() => toggle(i, 'procesos', p)}
                  title={r.procesos.length ? 'Solo los procesos marcados' : 'Todos los procesos'}
                  className={`px-1.5 py-0.5 rounded text-[10px] font-bold border transition-colors ${
                    r.procesos.includes(p) ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-gray-500 border-gray-200 hover:bg-gray-50'}`}>
                  {p}
                </button>
              ))}
            </div>
            <select value={r.cuadrillas} onChange={e => set(i, { cuadrillas: e.target.value })}
              className="px-2 py-1.5 text-xs border rounded-lg bg-white text-gray-700">
              {data.cuadrillas_opciones.map(o => <option key={o} value={o}>{CUADRILLAS_LABEL[o]}</option>)}
            </select>
            <button type="button" onClick={() => setFilas(f => f.filter((_, j) => j !== i))} disabled={filas.length <= 1}
              title="Quitar senda" className="text-gray-400 hover:text-red-600 disabled:opacity-30"><Trash2 size={14} /></button>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-2">
        <button type="button" onClick={() => setFilas(f => [...f, nuevaFila()])}
          className="flex items-center gap-1 text-xs border px-2 py-1.5 rounded-lg bg-white text-gray-600 hover:bg-gray-50">
          <Plus size={12} /> Añadir senda
        </button>
        <div className="ml-auto flex items-center gap-1.5">
          <button type="button" onClick={onClose} className="text-xs border px-3 py-1.5 rounded-lg bg-white text-gray-600 hover:bg-gray-50">Cancelar</button>
          <button type="button" onClick={() => guardar.mutate()} disabled={guardar.isPending}
            className="flex items-center gap-1.5 text-xs bg-blue-700 text-white px-3 py-1.5 rounded-lg hover:bg-blue-800 disabled:opacity-50">
            {guardar.isPending ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />} Guardar metas
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Tab ─────────────────────────────────────────────────────────────────────
export default function SendaMetaTab({ mes, anio }) {
  const [editando, setEditando] = useState(false)
  const [verCuadrillas, setVerCuadrillas] = useState(false)
  useEffect(() => { setEditando(false) }, [mes, anio])

  const { data, isLoading, isError, error, isFetching } = useQuery({
    queryKey: ['sgs-senda', mes, anio],
    queryFn: async () => (await api.get('/sgs/senda', { params: { mes, anio } })).data,
    refetchOnWindowFocus: false,
    retry: false,
    staleTime: 5 * 60 * 1000,
  })

  const chartData = useMemo(() => {
    if (!data) return []
    const n = data.dias_mes
    return Array.from({ length: n }, (_, i) => {
      const row = { dia: i + 1, senda: Math.round((i + 1) / n * 1000) / 10 }
      data.sendas.forEach(s => { row[s.clave] = s.serie[i]?.pct ?? null })
      return row
    })
  }, [data])

  if (isLoading) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-gray-500 bg-white rounded-xl border">
        <Loader2 size={28} className="animate-spin mb-2 text-blue-500" />
        Midiendo la senda…
      </div>
    )
  }
  if (isError) {
    return <div className="bg-red-50 border border-red-200 text-red-700 rounded-xl p-4 text-sm">{error?.response?.data?.detail || 'No se pudo calcular la senda.'}</div>
  }
  if (!data) return null

  const hoyDia = data.sendas[0]?.hoy?.dia
  const esperadoHoy = data.sendas[0]?.hoy?.esperado_pct
  const origen = data.metas_origen === 'guardada' ? `Metas guardadas para ${MESES[mes]} ${anio}`
    : data.metas_origen === 'heredada' ? `Metas heredadas de ${data.metas_heredadas_de} (no guardadas aún para este mes)`
    : 'Metas por defecto (no guardadas aún para este mes)'
  const cuadrillasCasadas = data.cuadrillas_moe.filter(c => c.en_sgs.length).length

  return (
    <div className="space-y-3">
      {/* Origen de las metas + corte de datos */}
      <div className="flex items-center gap-2 flex-wrap text-[11px] text-gray-500 bg-white border rounded-xl px-3 py-2">
        <Target size={13} className="text-blue-600" />
        <span className={data.metas_origen === 'guardada' ? 'text-gray-600' : 'text-amber-700 font-semibold'}>{origen}</span>
        <span className="text-gray-300">·</span>
        <span>Último dato SGS: <b className="text-gray-700">{fmtFecha(data.ultima_fecha_sgs)}</b></span>
        <span className="text-gray-300">·</span>
        <span>Senda lineal por día calendario · cortes día {data.cortes_dias.join(', ')}</span>
        {isFetching && <Loader2 size={12} className="animate-spin text-blue-500" />}
        {!editando && (
          <button type="button" onClick={() => setEditando(true)}
            className="ml-auto flex items-center gap-1 text-xs border border-blue-600 text-blue-700 px-2 py-1 rounded-lg hover:bg-blue-50">
            <Pencil size={12} /> Editar metas
          </button>
        )}
      </div>

      {editando && <EditorMetas data={data} mes={mes} anio={anio} onClose={() => setEditando(false)} />}

      {/* Resumen */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        <div className="bg-white border rounded-xl px-3 py-2.5">
          <div className="text-[10px] uppercase tracking-wide text-gray-400">Meta total</div>
          <div className="text-lg font-bold text-gray-800">{fmtM(data.meta_total)}</div>
          <div className="text-[11px] text-gray-400">{data.sendas.length} sendas</div>
        </div>
        <div className="bg-white border rounded-xl px-3 py-2.5">
          <div className="text-[10px] uppercase tracking-wide text-gray-400">Liquidado en sendas</div>
          <div className="text-lg font-bold text-blue-700">{fmtPct(data.pct_total)}</div>
          <div className="text-[11px] text-gray-400">{fmtM(data.total_sendas)}{esperadoHoy != null ? ` · esperado ${fmtPct(esperadoHoy)}` : ''}</div>
        </div>
        <div className="bg-white border rounded-xl px-3 py-2.5">
          <div className="text-[10px] uppercase tracking-wide text-gray-400">Fuera de las sendas</div>
          <div className="text-lg font-bold text-gray-600">{fmtM(data.fuera_de_senda)}</div>
          <div className="text-[11px] text-gray-400" title="MO de SGS que ninguna senda cuenta (otras zonas, OTROS…)">de {fmtM(data.total_sgs)} en SGS</div>
        </div>
        <div className="bg-white border rounded-xl px-3 py-2.5">
          <div className="text-[10px] uppercase tracking-wide text-gray-400">{hoyDia ? `Día ${hoyDia} de ${data.dias_mes}` : 'Mes futuro'}</div>
          <div className="text-lg font-bold text-gray-800">{hoyDia ? fmtPct(Math.round(hoyDia / data.dias_mes * 1000) / 10) : '—'}</div>
          <div className="text-[11px] text-gray-400">del mes transcurrido</div>
        </div>
      </div>

      {/* Tarjetas por senda */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {data.sendas.map((s, i) => <SendaCard key={s.clave} s={s} color={SENDA_HEX[i % SENDA_HEX.length]} />)}
      </div>

      {/* Gráfica: % de la meta por día vs senda lineal */}
      <div className="bg-white rounded-xl border p-4">
        <h3 className="text-xs font-bold text-gray-700 uppercase tracking-wide mb-1">Avance acumulado vs senda (% de la meta)</h3>
        <p className="text-[11px] text-gray-400 mb-3">La línea gris punteada es la senda lineal. Las verticales marcan los cortes.</p>
        <ResponsiveContainer width="100%" height={260}>
          <LineChart data={chartData} margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#f1f5f9" />
            <XAxis dataKey="dia" tick={{ fontSize: 10, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
            <YAxis domain={[0, (max) => Math.max(100, Math.ceil((max || 0) / 10) * 10)]} tickFormatter={v => `${v}%`} tick={{ fontSize: 10, fill: '#94a3b8' }} axisLine={false} tickLine={false} width={40} />
            <Tooltip formatter={(v, name) => [fmtPct(v), name]} labelFormatter={d => `Día ${d}`} contentStyle={{ fontSize: 11 }} />
            <Legend iconSize={10} wrapperStyle={{ fontSize: 11 }} />
            {data.cortes_dias.map(d => <ReferenceLine key={d} x={d} stroke="#cbd5e1" strokeDasharray="2 4" />)}
            <ReferenceLine y={100} stroke="#cbd5e1" />
            <Line type="monotone" dataKey="senda" name="Senda lineal" stroke={SENDA_LINEAL_HEX} strokeWidth={1.5} strokeDasharray="5 4" dot={false} isAnimationActive={false} />
            {data.sendas.map((s, i) => (
              <Line key={s.clave} type="monotone" dataKey={s.clave} name={s.nombre} stroke={SENDA_HEX[i % SENDA_HEX.length]}
                strokeWidth={2} dot={false} activeDot={{ r: 4 }} connectNulls={false} isAnimationActive={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* Cuadrillas MOE detectadas en la programación */}
      <div className="bg-white rounded-xl border">
        <button type="button" onClick={() => setVerCuadrillas(v => !v)}
          className="w-full flex items-center gap-2 px-4 py-2.5 text-xs text-gray-700 hover:bg-gray-50 rounded-xl">
          {verCuadrillas ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          <span className="font-semibold">Cuadrillas MOE en la programación de {MESES[mes]}</span>
          <span className="text-gray-400">{data.cuadrillas_moe.length} cuadrillas · {cuadrillasCasadas} con liquidación en SGS</span>
        </button>
        {verCuadrillas && (
          <div className="border-t overflow-x-auto">
            <table className="w-full text-[11px]">
              <thead>
                <tr className="bg-gray-50 text-gray-500 text-[10px] uppercase tracking-wide">
                  <th className="text-left px-3 py-1.5">Líder (programación)</th>
                  <th className="text-left px-3 py-1.5">Tipo</th>
                  <th className="text-left px-3 py-1.5">Zona prog.</th>
                  <th className="text-right px-3 py-1.5">Días</th>
                  <th className="text-right px-3 py-1.5">MO proyectada</th>
                  <th className="text-left px-3 py-1.5">Líder en SGS</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {data.cuadrillas_moe.map(c => (
                  <tr key={`${c.lider}-${c.zona}-${c.tipo_grupo}`} className="hover:bg-gray-50">
                    <td className="px-3 py-1.5 text-gray-700">{c.lider}</td>
                    <td className="px-3 py-1.5 text-gray-500">{c.tipo_grupo}</td>
                    <td className="px-3 py-1.5 text-gray-500">{c.zona || '—'}</td>
                    <td className="px-3 py-1.5 text-right text-gray-500">{c.n}</td>
                    <td className="px-3 py-1.5 text-right text-gray-700">{formatCOP(c.mo_proyectada)}</td>
                    <td className="px-3 py-1.5 text-gray-500">{c.en_sgs.length ? c.en_sgs.join(' / ') : <span className="text-gray-300">sin liquidación este mes</span>}</td>
                  </tr>
                ))}
                {!data.cuadrillas_moe.length && <tr><td colSpan={6} className="px-3 py-4 text-center text-gray-400">La programación del mes no tiene cuadrillas de tipo MOE.</td></tr>}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
