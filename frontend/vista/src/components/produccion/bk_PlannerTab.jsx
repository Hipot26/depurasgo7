import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '../../utils/api'
import { formatCOP, ZONA_COLORS } from '../../utils/format'
import { AlertTriangle, X, Users } from 'lucide-react'

// ── Planner de recursos ────────────────────────────────────────────────────────
// Resumen visual de la programación: por zona y día, semáforo de cuadrillas
// programadas vs capacidad (Dimensionamiento), MO proyectada diaria con sus
// responsables y lectura del estado PDT.
// Regla: un líder repetido en varios trabajos el mismo día cuenta UNA sola vez.

const MESES_LABEL = ['','Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre']
const TIPO_ORDER  = ['CMAP5','CMAP3','ARRIME','GRUA','ESTACA','LVL3','MIE','OTROS']

// Colores del estado PDT (mismo lenguaje del resto de la plataforma)
const EPDT_COLOR = {
  PROGRAMADO:    '#3b82f6',
  'EN EJECUCION':'#06b6d4',
  EJECUTADO:     '#16a34a',
  PARCIAL:       '#eab308',
  REPROGRAMADO:  '#f59e0b',
  CANCELADO:     '#ef4444',
  ANULADO:       '#6b7280',
}
const epdtColor = (e) => EPDT_COLOR[e] || '#9ca3af'

// $ compacto para la fila de MO diaria
const moCorto = (v) => {
  if (!v) return '—'
  if (v >= 1e6) return `${(v / 1e6).toFixed(1).replace('.', ',')}M`
  if (v >= 1e3) return `${Math.round(v / 1e3)}k`
  return String(Math.round(v))
}

function CeldaSemaforo({ prog, cap, noLaborable, onClick, selected }) {
  let cls, txt
  if (!cap) {
    // Sin capacidad definida (p. ej. Línea Viva pendiente): conteo neutro
    cls = prog > 0 ? 'bg-blue-100 text-blue-800' : 'text-gray-300'
    txt = prog > 0 ? String(prog) : '·'
  } else if (noLaborable && !prog) {
    cls = 'text-gray-300'
    txt = '·'
  } else if (prog >= cap) {
    cls = 'bg-green-500 text-white'
    txt = `${prog}/${cap}`
  } else if (prog > 0) {
    cls = 'bg-amber-400 text-amber-950'
    txt = `${prog}/${cap}`
  } else {
    cls = 'bg-red-500 text-white'
    txt = `0/${cap}`
  }
  return (
    <button onClick={onClick}
      className={`w-full min-w-[34px] px-0.5 py-1 rounded text-[10px] font-semibold leading-none transition
        ${cls} ${selected ? 'ring-2 ring-blue-600 ring-offset-1' : 'hover:opacity-80'}`}>
      {txt}
    </button>
  )
}

function BarraEstadoPdt({ conteos }) {
  const total = Object.values(conteos || {}).reduce((s, n) => s + n, 0)
  if (!total) return <div className="h-1.5 rounded bg-gray-100" />
  const title = Object.entries(conteos).map(([e, n]) => `${e}: ${n}`).join(' · ')
  return (
    <div className="h-1.5 rounded overflow-hidden flex" title={title}>
      {Object.entries(conteos).sort((a, b) => b[1] - a[1]).map(([e, n]) => (
        <div key={e} style={{ width: `${(n / total) * 100}%`, backgroundColor: epdtColor(e) }} />
      ))}
    </div>
  )
}

function DetalleDia({ zona, dia, datos, capZona, onClose }) {
  const tipos = TIPO_ORDER.filter((t) => datos?.tipos?.[t])
  const sitTxt  = { trabajo: null, descanso: 'descanso', vacio: 'sin programar' }
  const sitDot  = { trabajo: 'bg-green-500', descanso: 'bg-gray-400', vacio: 'bg-red-500' }
  return (
    <div className="border-t bg-blue-50/40 p-3 sm:p-4">
      <div className="flex items-center justify-between mb-2">
        <p className="text-sm font-semibold text-gray-800 flex items-center gap-2">
          <Users size={15} className="text-blue-700" />
          {zona} · {dia.dia} de {MESES_LABEL[+dia.fecha.slice(5, 7)].toLowerCase()}
          {dia.no_laborable && <span className="text-[10px] font-bold text-white bg-red-500 rounded px-1.5 py-0.5">DOMINGO / FESTIVO</span>}
        </p>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={16} /></button>
      </div>

      {!datos ? (
        <p className="text-xs text-gray-500">Sin programación este día.</p>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {tipos.map((t) => {
              const info = datos.tipos[t]
              const cap = capZona?.[t]
              return (
                <div key={t} className="bg-white rounded-lg border p-2.5">
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-xs font-bold text-gray-700">{t}</span>
                    <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded
                      ${!cap ? 'bg-gray-100 text-gray-500'
                        : info.programados >= cap ? 'bg-green-100 text-green-700'
                        : info.programados > 0 ? 'bg-amber-100 text-amber-700' : 'bg-red-100 text-red-700'}`}>
                      {cap ? `${info.programados}/${cap} programadas` : `${info.programados} programadas`}
                    </span>
                  </div>
                  <ul className="space-y-1">
                    {info.lideres.map((l) => (
                      <li key={l.lider} className="flex items-center gap-1.5 text-[11px] text-gray-700">
                        <span className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${sitDot[l.situacion]}`} />
                        <span className="truncate font-medium">{l.lider}</span>
                        {l.trabajos > 1 && <span className="text-[9px] text-gray-400 shrink-0">×{l.trabajos} trabajos · cuenta 1</span>}
                        {sitTxt[l.situacion] && <span className="text-[9px] text-gray-400 shrink-0 italic">{sitTxt[l.situacion]}</span>}
                        <span className="ml-auto text-[10px] text-gray-500 shrink-0">{l.mo ? formatCOP(l.mo) : ''}</span>
                        {l.estados.map((e) => (
                          <span key={e} className="shrink-0 text-[8px] font-bold text-white rounded px-1 py-px"
                            style={{ backgroundColor: epdtColor(e) }}>{e}</span>
                        ))}
                      </li>
                    ))}
                  </ul>
                </div>
              )
            })}
          </div>
          <div className="flex items-center gap-4 mt-3 text-xs text-gray-600 flex-wrap">
            <span>MO proyectada del día: <b className="text-gray-800">{formatCOP(datos.mo_total)}</b></span>
            {Object.entries(datos.estado_pdt || {}).map(([e, n]) => (
              <span key={e} className="flex items-center gap-1">
                <span className="inline-block w-2 h-2 rounded-sm" style={{ backgroundColor: epdtColor(e) }} />
                {e}: <b>{n}</b>
              </span>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function ZonaCard({ zona, dias, datosZona, capZona, sel, setSel }) {
  // Tipos a mostrar: los que tienen capacidad + los que aparecen en los datos
  const tipos = useMemo(() => {
    const set = new Set(Object.keys(capZona || {}))
    Object.values(datosZona || {}).forEach((d) => Object.keys(d.tipos || {}).forEach((t) => set.add(t)))
    return TIPO_ORDER.filter((t) => set.has(t))
  }, [datosZona, capZona])

  const sinCapacidad = !capZona || Object.keys(capZona).length === 0

  // Resumen del mes: MO total y días laborables completos
  const resumen = useMemo(() => {
    let mo = 0, completos = 0, laborables = 0
    dias.forEach((d) => {
      const dd = datosZona?.[d.fecha]
      mo += dd?.mo_total || 0
      if (d.no_laborable || sinCapacidad) return
      laborables++
      const ok = Object.entries(capZona).every(([t, cap]) => (dd?.tipos?.[t]?.programados || 0) >= cap)
      if (ok) completos++
    })
    return { mo, completos, laborables }
  }, [dias, datosZona, capZona, sinCapacidad])

  const diaSel = sel?.zona === zona ? sel : null

  return (
    <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
      {/* Cabecera de zona */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className={`px-2 py-0.5 rounded text-xs font-bold ${ZONA_COLORS[zona] || 'bg-gray-100 text-gray-700'}`}>{zona}</span>
          {!sinCapacidad && (
            <span className="text-[11px] text-gray-500">
              <b className={resumen.completos === resumen.laborables ? 'text-green-600' : 'text-gray-800'}>{resumen.completos}</b>
              /{resumen.laborables} días laborables con el recurso completo
            </span>
          )}
        </div>
        <span className="text-[11px] text-gray-500">MO proyectada del mes: <b className="text-gray-800">{formatCOP(resumen.mo)}</b></span>
      </div>

      {/* Aviso Línea Viva pendiente */}
      {sinCapacidad && (
        <div className="flex items-start gap-2 px-4 py-2.5 bg-amber-50 border-b border-amber-200 text-amber-800 text-xs">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            <b>Capacidad pendiente por confirmar con el cliente.</b> Cuando llegue el dato de
            Línea Viva, se llena en <b>Dimensionamiento</b> y este semáforo se activa solo.
            Mientras tanto se muestra el conteo programado sin alertas.
          </span>
        </div>
      )}

      {/* Matriz */}
      <div className="overflow-x-auto">
        <table className="border-collapse w-full">
          <thead>
            <tr>
              <th className="sticky left-0 bg-white text-left text-[10px] font-semibold text-gray-500 px-3 py-1.5 border-b min-w-[110px] z-10">
                Cuadrilla <span className="font-normal text-gray-400">(prog/cap)</span>
              </th>
              {dias.map((d) => (
                <th key={d.fecha} className={`px-0.5 py-1.5 border-b text-center text-[10px] font-bold min-w-[36px]
                  ${d.no_laborable ? 'text-white bg-red-500' : 'text-gray-600'}`}>
                  {d.dia}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tipos.map((t) => (
              <tr key={t} className="hover:bg-blue-50/30">
                <td className="sticky left-0 bg-white px-3 py-1 border-b text-xs font-semibold text-gray-700 whitespace-nowrap z-10">
                  {t}{capZona?.[t] ? <span className="text-gray-400 font-normal"> ×{capZona[t]}</span> : null}
                </td>
                {dias.map((d) => {
                  const info = datosZona?.[d.fecha]?.tipos?.[t]
                  return (
                    <td key={d.fecha} className="px-0.5 py-0.5 border-b text-center">
                      <CeldaSemaforo
                        prog={info?.programados || 0}
                        cap={capZona?.[t] || 0}
                        noLaborable={d.no_laborable}
                        selected={diaSel?.fecha === d.fecha}
                        onClick={() => setSel(diaSel?.fecha === d.fecha ? null : { zona, fecha: d.fecha })}
                      />
                    </td>
                  )
                })}
              </tr>
            ))}
            {/* MO proyectada por día */}
            <tr className="bg-gray-50">
              <td className="sticky left-0 bg-gray-50 px-3 py-1 text-[10px] font-semibold text-gray-500 whitespace-nowrap z-10">MO proyectada</td>
              {dias.map((d) => (
                <td key={d.fecha} className="px-0.5 py-1 text-center text-[9px] font-medium text-gray-600 whitespace-nowrap">
                  {moCorto(datosZona?.[d.fecha]?.mo_total)}
                </td>
              ))}
            </tr>
            {/* Estado PDT por día */}
            <tr>
              <td className="sticky left-0 bg-white px-3 py-1 text-[10px] font-semibold text-gray-500 whitespace-nowrap z-10">Estado PDT</td>
              {dias.map((d) => (
                <td key={d.fecha} className="px-0.5 py-1">
                  <BarraEstadoPdt conteos={datosZona?.[d.fecha]?.estado_pdt} />
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>

      {/* Detalle del día seleccionado */}
      {diaSel && (
        <DetalleDia
          zona={zona}
          dia={dias.find((d) => d.fecha === diaSel.fecha)}
          datos={datosZona?.[diaSel.fecha]}
          capZona={capZona}
          onClose={() => setSel(null)}
        />
      )}
    </div>
  )
}

export default function PlannerTab({ unidad, cfg }) {
  const now = new Date()
  const [mes, setMes]   = useState(now.getMonth() + 1)
  const [anio, setAnio] = useState(now.getFullYear())
  const [sel, setSel]   = useState(null)   // { zona, fecha }

  const { data, isLoading } = useQuery({
    queryKey: ['planner', unidad, mes, anio],
    queryFn: () => api.get('/produccion/planner', { params: { unidad, mes, anio } }).then((r) => r.data),
  })

  return (
    <div className="space-y-4">
      {/* Controles + leyenda */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <select value={mes} onChange={(e) => { setMes(+e.target.value); setSel(null) }}
            className="border rounded-lg px-2 py-1.5 text-sm">
            {MESES_LABEL.slice(1).map((m, i) => <option key={i + 1} value={i + 1}>{m}</option>)}
          </select>
          <select value={anio} onChange={(e) => { setAnio(+e.target.value); setSel(null) }}
            className="border rounded-lg px-2 py-1.5 text-sm">
            {[2025, 2026, 2027].map((y) => <option key={y}>{y}</option>)}
          </select>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-gray-500 flex-wrap">
          <span className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded bg-green-500" />completo</span>
          <span className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded bg-amber-400" />incompleto</span>
          <span className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded bg-red-500" />sin programar</span>
          <span className="flex items-center gap-1"><span className="inline-block w-3 h-3 rounded bg-red-500 text-white text-[8px] font-bold leading-3 text-center">D</span>domingo/festivo</span>
          <span className="text-gray-400">· líder repetido en el día cuenta 1 vez · clic en una celda para el detalle</span>
        </div>
      </div>

      {isLoading ? (
        <div className="text-center text-sm text-gray-400 py-16">Cargando planner…</div>
      ) : (
        cfg.zonas.map((z) => (
          <ZonaCard
            key={z}
            zona={z}
            dias={data?.dias || []}
            datosZona={data?.zonas?.[z]}
            capZona={data?.capacidad?.[z]}
            sel={sel}
            setSel={setSel}
          />
        ))
      )}
    </div>
  )
}
