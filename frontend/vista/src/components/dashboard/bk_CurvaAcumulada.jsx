import { useMemo, useState, Fragment } from 'react'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, LabelList,
} from 'recharts'
import { TrendingUp, Table2, ChevronDown, ChevronUp } from 'lucide-react'
import api from '../../utils/api'
import { formatCOP } from '../../utils/format'

// Curva acumulada (requisito del coordinador de programados, 2026-09-21).
// Es la tabla dinámica de su PROGRAMACIÓN_GUAVIO (hoja REPORTE) hecha en la
// plataforma: por día suma los valores, a la derecha los acumula y grafica el
// acumulado. Los días sin trabajo dejan la curva plana, igual que en su Excel.

// Orden FIJO de colores categóricos (validado para daltonismo: peor par
// adyacente ΔE 9,1). Nunca se ciclan ni se reasignan al filtrar zonas.
const SLOTS = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100']

const SERIES = [
  { key: 'valor_dia',     label: 'Proyección',   origen: 'VALOR DÍA',     color: SLOTS[0] },
  { key: 'mo_proyectada', label: 'Programación', origen: 'MO PROYECTADA', color: SLOTS[1] },
  { key: 'mo_real',       label: 'Real',         origen: 'MO REAL',       color: SLOTS[2] },
  { key: 'mo_facturada',  label: 'Facturada',    origen: 'MO FACTURADA',  color: SLOTS[3] },
]

// Color por zona: fijo por entidad (no por su posición en el filtro).
const ZONAS_ORDEN = ['ALMEIDAS', 'GUAVIO', 'LV&TLC', 'UBATE']
const colorZona = (z) => SLOTS[((ZONAS_ORDEN.indexOf(z) + 4) % 4)]

const fmtDia   = (f) => (f ? f.slice(8) : '')
const fmtFecha = (f) => (f ? f.slice(0, 10).split('-').reverse().join('/') : '')
const fmtM     = (v) => Math.round((v || 0) / 1e6) + 'M'

// Etiqueta directa al final de la línea: punto de color + texto en tinta neutra
// (así la identidad de cada zona no depende solo del color).
const etiquetaFinal = (texto, color, ultimo) => (props) => {
  const { x, y, index } = props
  if (index !== ultimo || x == null || y == null) return null
  return (
    <g>
      <circle cx={x + 7} cy={y} r={3.5} fill={color} />
      <text x={x + 14} y={y} dy={4} fontSize={11} fill="#475569">{texto}</text>
    </g>
  )
}

function Chip({ activo, children, onClick, color }) {
  return (
    <button
      onClick={onClick}
      className={'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] border transition ' +
        (activo ? 'bg-gray-900 text-white border-gray-900' : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50')}
    >
      {color && <span className="w-2 h-2 rounded-full" style={{ backgroundColor: color }} />}
      {children}
    </button>
  )
}

export default function CurvaAcumulada({ params }) {
  const [modo, setModo]                   = useState('total')          // total | zona
  const [metrica, setMetrica]             = useState('mo_proyectada')  // solo en modo zona
  const [ocultas, setOcultas]             = useState(() => new Set())  // series apagadas
  const [sinCancelados, setSinCancelados] = useState(true)   // requisito 22-09: fuera los cancelados
  const [verTabla, setVerTabla]           = useState(false)

  const qParams = useMemo(
    () => ({ ...params, excluir_cancelados: sinCancelados }),
    [params, sinCancelados],
  )

  const { data, isLoading } = useQuery({
    queryKey: ['kpis-curva', qParams],
    queryFn: () => api.get('/kpis/curva', { params: qParams }).then((r) => r.data),
    placeholderData: keepPreviousData,
  })

  const dias    = data?.dias || []
  const zonas   = data?.zonas || []
  const tot     = data?.totales || {}
  const activas = SERIES.filter((s) => !ocultas.has(s.key))
  const serieMetrica = SERIES.find((s) => s.key === metrica) || SERIES[1]

  // En modo zona: una línea por zona para la métrica elegida.
  const datos = useMemo(() => {
    if (modo === 'total') return dias
    return dias.map((d, i) => {
      const fila = { fecha: d.fecha }
      zonas.forEach((z) => { fila[z] = data?.por_zona?.[z]?.[i]?.['acum_' + metrica] ?? 0 })
      return fila
    })
  }, [modo, metrica, dias, zonas, data])

  const ultimo = datos.length - 1

  const toggleSerie = (k) => setOcultas((prev) => {
    const s = new Set(prev)
    if (s.has(k)) s.delete(k)
    else if (SERIES.length - s.size > 1) s.add(k)   // siempre queda una visible
    return s
  })

  const hayDatos = dias.length > 0

  return (
    <div className="bg-white rounded-xl p-5 shadow-sm border space-y-4">
      {/* Cabecera */}
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-gray-700 flex items-center gap-1.5">
            <TrendingUp size={15} className="text-gray-400" /> Curva acumulada del período
          </h3>
          <p className="text-[11px] text-gray-400 mt-0.5">
            {hayDatos
              ? fmtFecha(data.desde) + ' — ' + fmtFecha(data.hasta) + ' · sigue el período y los filtros de arriba'
              : 'sigue el período y los filtros de arriba'}
          </p>
        </div>
        <div className="flex items-center gap-1 bg-gray-100 rounded-lg p-0.5">
          {[['total', 'Total'], ['zona', 'Por zona']].map(([id, lbl]) => (
            <button
              key={id}
              onClick={() => setModo(id)}
              className={'px-2.5 py-1 text-xs rounded-md transition ' +
                (modo === id ? 'bg-white text-blue-700 font-semibold shadow-sm' : 'text-gray-500 hover:text-gray-800')}
            >
              {lbl}
            </button>
          ))}
        </div>
      </div>

      {/* Controles — una sola fila sobre la gráfica */}
      <div className="flex flex-wrap items-center gap-1.5">
        {modo === 'total'
          ? SERIES.map((s) => (
              <Chip key={s.key} activo={!ocultas.has(s.key)} color={s.color} onClick={() => toggleSerie(s.key)}>
                {s.label}
              </Chip>
            ))
          : SERIES.map((s) => (
              <Chip key={s.key} activo={metrica === s.key} onClick={() => setMetrica(s.key)}>
                {s.label}
              </Chip>
            ))}
        <span className="mx-1 h-4 w-px bg-gray-200" />
        <label className="inline-flex items-center gap-1.5 text-[11px] text-gray-500 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={sinCancelados}
            onChange={(e) => setSinCancelados(e.target.checked)}
            className="rounded border-gray-300"
          />
          Excluir cancelados y anulados
        </label>
        <button
          onClick={() => setVerTabla((v) => !v)}
          className="ml-auto inline-flex items-center gap-1 text-[11px] text-blue-600 hover:text-blue-800"
        >
          <Table2 size={12} /> {verTabla ? 'Ocultar tabla' : 'Ver tabla'}
          {verTabla ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        </button>
      </div>

      {/* Totales del período */}
      <div className="flex flex-wrap gap-x-6 gap-y-2">
        <div>
          <p className="text-[10px] text-gray-400 uppercase tracking-wide">Turnos</p>
          <p className="text-lg font-bold text-gray-800 leading-tight">{tot.turnos ?? 0}</p>
        </div>
        {SERIES.map((s) => (
          <div key={s.key}>
            <p className="text-[10px] text-gray-400 uppercase tracking-wide flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: s.color }} />
              {s.label}
            </p>
            <p className="text-lg font-bold text-gray-800 leading-tight">{formatCOP(tot[s.key] || 0)}</p>
          </div>
        ))}
      </div>

      {/* Curva */}
      {isLoading && !hayDatos ? (
        <div className="h-[280px] grid place-items-center text-gray-400 text-sm">Cargando curva…</div>
      ) : !hayDatos ? (
        <div className="h-[280px] grid place-items-center text-gray-400 text-sm">
          No hay programación cargada para este período.
        </div>
      ) : (
        <>
          <ResponsiveContainer width="100%" height={280}>
            <LineChart data={datos} margin={{ top: 8, right: modo === 'zona' ? 90 : 20, left: 12, bottom: 4 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis dataKey="fecha" tickFormatter={fmtDia} tick={{ fontSize: 10 }} minTickGap={10} />
              <YAxis tickFormatter={fmtM} tick={{ fontSize: 10 }} width={48} />
              <Tooltip
                formatter={(v, n) => [formatCOP(v), n]}
                labelFormatter={(l) => fmtFecha(l)}
                contentStyle={{ fontSize: 12, borderRadius: 8 }}
              />
              <Legend
                wrapperStyle={{ fontSize: 12 }}
                formatter={(v) => <span style={{ color: '#475569' }}>{v}</span>}
              />
              {modo === 'total'
                ? activas.map((s) => (
                    <Line
                      key={s.key} type="linear" dataKey={'acum_' + s.key} name={s.label}
                      stroke={s.color} strokeWidth={2} dot={false}
                      activeDot={{ r: 4, strokeWidth: 2, stroke: '#fff' }}
                    />
                  ))
                : zonas.map((z) => (
                    <Line
                      key={z} type="linear" dataKey={z} name={z}
                      stroke={colorZona(z)} strokeWidth={2} dot={false}
                      activeDot={{ r: 4, strokeWidth: 2, stroke: '#fff' }}
                    >
                      <LabelList dataKey={z} content={etiquetaFinal(z, colorZona(z), ultimo)} />
                    </Line>
                  ))}
            </LineChart>
          </ResponsiveContainer>

          {/* Turnos por día — gráfica aparte: nunca un segundo eje encima del dinero */}
          <div>
            <p className="text-xs font-semibold text-gray-600">Turnos por día</p>
            <p className="text-[11px] text-gray-400 mb-1">
              cuadrillas con VALOR DÍA diligenciado (su «Cuenta de VALOR DIA»)
            </p>
            <ResponsiveContainer width="100%" height={110}>
              <BarChart data={dias} margin={{ top: 4, right: modo === 'zona' ? 90 : 20, left: 12, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" vertical={false} />
                <XAxis dataKey="fecha" tickFormatter={fmtDia} tick={{ fontSize: 10 }} minTickGap={10} />
                <YAxis allowDecimals={false} tick={{ fontSize: 10 }} width={48} />
                <Tooltip
                  formatter={(v) => [v, 'Turnos']}
                  labelFormatter={(l) => fmtFecha(l)}
                  contentStyle={{ fontSize: 12, borderRadius: 8 }}
                />
                <Bar dataKey="turnos" fill="#64748b" radius={[4, 4, 0, 0]} maxBarSize={18} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </>
      )}

      {/* Vista de tabla — los mismos números de la gráfica */}
      {verTabla && hayDatos && (
        <div className="overflow-auto border rounded-lg max-h-[45vh]">
          <table className="w-full text-xs">
            <thead className="bg-gray-50 sticky top-0">
              <tr>
                <th className="px-2 py-2 text-left font-semibold text-gray-600">Fecha</th>
                <th className="px-2 py-2 text-right font-semibold text-gray-600">Turnos</th>
                {modo === 'total'
                  ? activas.map((s) => (
                      <th key={s.key} colSpan={2} className="px-2 py-2 text-center font-semibold text-gray-600 border-l">
                        {s.label}
                      </th>
                    ))
                  : zonas.map((z) => (
                      <th key={z} className="px-2 py-2 text-right font-semibold text-gray-600 border-l">{z}</th>
                    ))}
              </tr>
              <tr className="text-[10px] text-gray-400">
                <th />
                <th />
                {modo === 'total'
                  ? activas.map((s) => (
                      <Fragment key={s.key}>
                        <th className="px-2 pb-1 text-right font-normal border-l">del día</th>
                        <th className="px-2 pb-1 text-right font-normal">acumulado</th>
                      </Fragment>
                    ))
                  : zonas.map((z) => (
                      <th key={z} className="px-2 pb-1 text-right font-normal border-l">
                        acum. {serieMetrica.label.toLowerCase()}
                      </th>
                    ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {datos.map((d, i) => (
                <tr key={d.fecha} className="hover:bg-gray-50">
                  <td className="px-2 py-1.5 text-gray-700 whitespace-nowrap">{fmtFecha(d.fecha)}</td>
                  <td className="px-2 py-1.5 text-right text-gray-600">{dias[i]?.turnos || 0}</td>
                  {modo === 'total'
                    ? activas.map((s) => (
                        <Fragment key={s.key}>
                          <td className="px-2 py-1.5 text-right text-gray-500 border-l whitespace-nowrap">
                            {d[s.key] ? formatCOP(d[s.key]) : '—'}
                          </td>
                          <td className="px-2 py-1.5 text-right text-gray-800 whitespace-nowrap">
                            {formatCOP(d['acum_' + s.key] || 0)}
                          </td>
                        </Fragment>
                      ))
                    : zonas.map((z) => (
                        <td key={z} className="px-2 py-1.5 text-right text-gray-800 border-l whitespace-nowrap">
                          {formatCOP(d[z] || 0)}
                        </td>
                      ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-[11px] text-gray-400 leading-relaxed">
        <b>Proyección</b> = VALOR DÍA acumulado · <b>Programación</b> = MO PROYECTADA acumulada ·{' '}
        <b>Real</b> = MO REAL · <b>Facturada</b> = MO FACTURADA. Cada día suma lo del día al acumulado del
        anterior; los días sin trabajo dejan la línea plana. Por defecto deja fuera lo cancelado y lo anulado
        —mirando las dos columnas de estado—, para que la curva muestre solo lo que realmente se ejecuta.
      </p>
    </div>
  )
}
