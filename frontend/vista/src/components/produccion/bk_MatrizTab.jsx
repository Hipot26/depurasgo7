import { useState, useMemo } from 'react'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import api from '../../utils/api'
import { formatCOP } from '../../utils/format'
import { Download, TrendingUp, TrendingDown, ChevronLeft, ChevronRight, Info, X } from 'lucide-react'

const MESES = ['', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']

const compact = (v) => {
  if (!v) return ''
  const a = Math.abs(v)
  if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + 'M'
  if (a >= 1e3) return Math.round(v / 1e3) + 'k'
  return String(Math.round(v))
}
const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`)
const cumplColor = (c) =>
  c == null ? 'bg-gray-100 text-gray-400'
    : c >= 0.95 ? 'bg-green-100 text-green-700'
    : c >= 0.75 ? 'bg-yellow-100 text-yellow-700'
    : 'bg-red-100 text-red-700'
const heat = (r) =>
  r == null ? '' : r >= 1 ? 'bg-green-50 text-green-700' : r >= 0.7 ? 'bg-yellow-50 text-yellow-700' : 'bg-red-50 text-red-600'

const CAPAS = [
  { id: 'proyeccion', label: 'Proyección' },
  { id: 'ejecucion', label: 'Ejecución' },
  { id: 'comparativa', label: 'Comparativa' },
]

export default function MatrizTab({ unidad, cfg, opciones }) {
  const now = new Date()
  const [anio, setAnio] = useState(now.getFullYear())
  const [mes, setMes] = useState(now.getMonth() + 1)
  const [zona, setZona] = useState('')
  const [tipo, setTipo] = useState('')
  const [capa, setCapa] = useState('proyeccion')
  const [cell, setCell] = useState(null)   // celda clickeada → popup de justificación

  const params = useMemo(() => {
    const p = { unidad, anio, mes }
    if (zona) p.zona = zona
    if (tipo) p.tipo_grupo = tipo
    return p
  }, [unidad, anio, mes, zona, tipo])

  const { data, isLoading } = useQuery({
    queryKey: ['matriz', params],
    queryFn: () => api.get('/kpis/matriz', { params }).then((r) => r.data),
    placeholderData: keepPreviousData,
  })

  const dias = data?.dias || []
  const cuadrillas = data?.cuadrillas || []
  const totDia = data?.totales_dia || {}
  const zonas = opciones?.zonas?.length ? opciones.zonas : cfg.zonas
  const tipos = opciones?.tipos?.length ? opciones.tipos : cfg.tipoOptions.filter(Boolean)

  const cambiarMes = (delta) => {
    let m = mes + delta, a = anio
    if (m < 1) { m = 12; a-- } else if (m > 12) { m = 1; a++ }
    setMes(m); setAnio(a)
  }

  const celda = (d) => {
    if (!d) return { txt: '', cls: 'text-gray-300' }
    if (capa === 'proyeccion') return { txt: d.proy ? compact(d.proy) : (d.n ? '·' : ''), cls: 'text-blue-700' }
    if (capa === 'comparativa') {
      const r = d.proy ? d.real / d.proy : null
      return { txt: r == null ? (d.n ? '·' : '') : pct(r), cls: `font-medium ${heat(r)}` }
    }
    return { txt: d.real ? compact(d.real) : (d.n ? '·' : ''), cls: d.real ? 'text-gray-800 font-medium' : 'text-gray-300' }
  }

  const exportCSV = () => {
    const head = ['ZONA', 'CUADRILLA', 'TIPO', ...dias.map((d) => d.dia), 'TOTAL', '%CUMPL']
    const lines = [head.join(';')]
    cuadrillas.forEach((c) => {
      const fila = [c.zona, c.lider, c.tipo_grupo]
      dias.forEach((d) => {
        const cd = c.dias[String(d.dia)]
        fila.push(capa === 'proyeccion' ? (cd?.proy || '') : capa === 'comparativa' ? (cd && cd.proy ? Math.round((cd.real / cd.proy) * 100) + '%' : '') : (cd?.real || ''))
      })
      fila.push(Math.round(capa === 'proyeccion' ? c.total_proy : c.total_real))
      fila.push(c.cumplimiento != null ? Math.round(c.cumplimiento * 100) + '%' : '')
      lines.push(fila.join(';'))
    })
    const blob = new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8;' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `matriz_produccion_${unidad}_${anio}-${String(mes).padStart(2, '0')}.csv`
    a.click()
  }

  const filasConGrupos = []
  let zonaActual = null
  cuadrillas.forEach((c) => {
    if (c.zona !== zonaActual) { zonaActual = c.zona; filasConGrupos.push({ grupo: c.zona }) }
    filasConGrupos.push({ cuad: c })
  })
  const valTotal = capa === 'proyeccion' ? data?.total_proy : data?.total_real

  return (
    <div className="space-y-4">
      {/* Controles */}
      <div className="bg-white rounded-xl border shadow-sm p-3 flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-1 bg-gray-50 border rounded-lg">
          <button onClick={() => cambiarMes(-1)} className="px-1.5 py-1.5 text-gray-500 hover:text-gray-800"><ChevronLeft size={15} /></button>
          <span className="text-xs font-semibold text-gray-700 min-w-[110px] text-center">{MESES[mes]} {anio}{isLoading && ' …'}</span>
          <button onClick={() => cambiarMes(1)} className="px-1.5 py-1.5 text-gray-500 hover:text-gray-800"><ChevronRight size={15} /></button>
        </div>
        <select value={zona} onChange={(e) => setZona(e.target.value)} className={`px-2 py-1.5 text-xs border rounded-lg ${zona ? 'border-blue-300 text-blue-800 font-semibold' : 'border-gray-200 text-gray-600'}`}>
          <option value="">Todas las zonas</option>
          {zonas.map((z) => <option key={z} value={z}>{z}</option>)}
        </select>
        <select value={tipo} onChange={(e) => setTipo(e.target.value)} className={`px-2 py-1.5 text-xs border rounded-lg ${tipo ? 'border-blue-300 text-blue-800 font-semibold' : 'border-gray-200 text-gray-600'}`}>
          <option value="">Todos los tipos</option>
          {tipos.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <div className="flex-1" />
        <div className="flex gap-0.5 bg-gray-100 p-0.5 rounded-lg">
          {CAPAS.map((c) => (
            <button key={c.id} onClick={() => setCapa(c.id)}
              className={`px-2.5 py-1 text-xs rounded-md transition ${capa === c.id ? 'bg-white text-blue-700 font-semibold shadow-sm' : 'text-gray-500 hover:text-gray-800'}`}>
              {c.label}
            </button>
          ))}
        </div>
        <button onClick={exportCSV} className="flex items-center gap-1.5 text-xs border px-2.5 py-1.5 rounded-lg text-gray-600 hover:bg-gray-50">
          <Download size={13} /> CSV
        </button>
      </div>

      {/* Resumen */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <Card label="Cuadrillas activas" value={cuadrillas.length} />
        <Card label="MO Proyectada" value={formatCOP(data?.total_proy)} color="blue" />
        <Card label="MO Real" value={formatCOP(data?.total_real)} color="green" />
        <Card label="% Cumplimiento" value={pct(data?.cumplimiento)} color={data?.cumplimiento >= 0.95 ? 'green' : data?.cumplimiento >= 0.75 ? 'orange' : 'red'} />
        <Card label="Días del mes" value={dias.length} />
      </div>

      {data?.por_tipo?.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] text-gray-400 font-medium">Activas por tipo:</span>
          {data.por_tipo.map((t) => (
            <span key={t.grupo} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] bg-blue-50 text-blue-700 border border-blue-200">
              <b>{t.grupo}</b> {t.cuadrillas} · {pct(t.cumplimiento)}
            </span>
          ))}
        </div>
      )}

      {/* Nota: de dónde salen los datos */}
      <div className="flex items-start gap-2 text-[12px] text-gray-600 bg-blue-50/60 border border-blue-100 rounded-lg px-3 py-2">
        <Info size={15} className="text-blue-500 shrink-0 mt-0.5" />
        <span>
          Estos datos <b>no se cargan aquí</b>: salen de la <b>Programación</b>. Cada celda es la suma de la
          MO {capa === 'proyeccion' ? 'proyectada' : capa === 'ejecucion' ? 'real' : '(real ÷ proyectada)'} de
          las cuadrillas de ese día. <b>Haz clic en cualquier dato</b> para ver de qué proyectos sale.
        </span>
      </div>

      {/* Matriz */}
      <div className="bg-white rounded-xl border shadow-sm overflow-auto" style={{ maxHeight: '68vh' }}>
        {cuadrillas.length === 0 ? (
          <div className="p-10 text-center text-gray-400 text-sm">Sin datos de programación en {MESES[mes]} {anio}.</div>
        ) : (
          <table className="text-[11px] border-collapse">
            <thead className="sticky top-0 z-20">
              <tr className="bg-gray-50">
                <th className="sticky left-0 z-30 bg-gray-50 text-left px-3 py-2 font-semibold text-gray-600 border-b border-r min-w-[190px]">Cuadrilla</th>
                {dias.map((d) => (
                  <th key={d.dia} className={`px-1 py-1 text-center font-medium border-b ${d.finde ? 'bg-gray-200 text-gray-500' : 'text-gray-600'}`} style={{ minWidth: 34 }}>
                    <div className="leading-none">{d.dia}</div>
                    <div className="text-[9px] text-gray-400 leading-none">{d.dow}</div>
                  </th>
                ))}
                <th className="px-2 py-2 text-right font-semibold text-gray-600 border-b border-l bg-gray-50 min-w-[80px]">Total</th>
                <th className="px-2 py-2 text-center font-semibold text-gray-600 border-b min-w-[60px]">Cumpl.</th>
              </tr>
            </thead>
            <tbody>
              {filasConGrupos.map((fila, idx) => fila.grupo !== undefined ? (
                <tr key={`g${idx}`} className="bg-blue-50/60">
                  <td className="sticky left-0 z-10 bg-blue-50 px-3 py-1 font-bold text-blue-800 border-b border-r">{fila.grupo || '—'}</td>
                  <td className="border-b" colSpan={dias.length + 2} />
                </tr>
              ) : (
                <RowCuadrilla key={`c${idx}`} c={fila.cuad} dias={dias} capa={capa} celda={celda}
                  onCell={(c, d) => setCell({ lider: c.lider, tipo_grupo: c.tipo_grupo, zona: c.zona, dia: d.dia })} />
              ))}
            </tbody>
            <tfoot className="sticky bottom-0 z-20">
              <tr className="bg-gray-100 font-semibold">
                <td className="sticky left-0 z-30 bg-gray-100 px-3 py-2 text-gray-700 border-t border-r">TOTAL DÍA</td>
                {dias.map((d) => {
                  const t = totDia[String(d.dia)]
                  const v = capa === 'proyeccion' ? t?.proy : t?.real
                  return <td key={d.dia} className={`px-1 py-2 text-center text-gray-600 border-t ${d.finde ? 'bg-gray-200' : ''}`}>{compact(v)}</td>
                })}
                <td className="px-2 py-2 text-right text-gray-800 border-t border-l">{compact(valTotal)}</td>
                <td className={`px-2 py-2 text-center border-t ${cumplColor(data?.cumplimiento)}`}>{pct(data?.cumplimiento)}</td>
              </tr>
            </tfoot>
          </table>
        )}
      </div>

      <p className="text-[11px] text-gray-400">
        Pivote de solo lectura de la programación. Celda · = cuadrilla programada sin valor ese día. Fines de semana sombreados.
      </p>

      {cell && <CeldaDetalle cell={cell} unidad={unidad} anio={anio} mes={mes} onClose={() => setCell(null)} />}
    </div>
  )
}

function RowCuadrilla({ c, dias, capa, celda, onCell }) {
  const total = capa === 'proyeccion' ? c.total_proy : c.total_real
  return (
    <tr className="hover:bg-blue-50/40 border-b">
      <td className="sticky left-0 z-10 bg-white px-3 py-1.5 border-r">
        <div className="font-medium text-gray-800 truncate max-w-[180px]" title={c.lider}>{c.lider}</div>
        <div className="text-[9px] text-gray-400">{c.tipo_grupo} · {c.dias_trab} días</div>
      </td>
      {dias.map((d) => {
        const cd = c.dias[String(d.dia)]
        const { txt, cls } = celda(cd)
        const clickable = !!cd?.n
        return (
          <td key={d.dia}
            onClick={clickable ? () => onCell(c, d) : undefined}
            title={clickable ? 'Ver de dónde sale este dato' : undefined}
            className={`px-1 py-1.5 text-center ${d.finde ? 'bg-gray-50' : ''} ${cls} ${clickable ? 'cursor-pointer hover:ring-2 hover:ring-blue-400 hover:ring-inset' : ''}`}>
            {txt}
          </td>
        )
      })}
      <td className="px-2 py-1.5 text-right font-semibold text-gray-700 border-l whitespace-nowrap">{formatCOP(total)}</td>
      <td className="px-1 py-1.5 text-center">
        <span className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-semibold ${cumplColor(c.cumplimiento)}`}>
          {c.cumplimiento != null && (c.cumplimiento >= 0.95 ? <TrendingUp size={9} /> : c.cumplimiento < 0.75 ? <TrendingDown size={9} /> : null)}
          {pct(c.cumplimiento)}
        </span>
      </td>
    </tr>
  )
}

function Card({ label, value, color = 'gray' }) {
  const colors = { gray: 'text-gray-800', blue: 'text-blue-700', green: 'text-green-700', orange: 'text-orange-600', red: 'text-red-600' }
  return (
    <div className="bg-white rounded-xl border shadow-sm p-3">
      <p className="text-[11px] text-gray-500 font-medium">{label}</p>
      <p className={`text-xl font-bold ${colors[color]}`}>{value}</p>
    </div>
  )
}

// Popup: justifica de dónde sale el valor de una celda (las programaciones reales)
function CeldaDetalle({ cell, unidad, anio, mes, onClose }) {
  const fecha = `${anio}-${String(mes).padStart(2, '0')}-${String(cell.dia).padStart(2, '0')}`
  const { data, isLoading } = useQuery({
    queryKey: ['matriz-detalle', unidad, fecha, cell.lider, cell.zona, cell.tipo_grupo],
    queryFn: () => api.get('/kpis/matriz-detalle', {
      params: { unidad, fecha, lider: cell.lider, zona: cell.zona, tipo_grupo: cell.tipo_grupo },
    }).then((r) => r.data),
  })
  const items = data?.items || []
  const proy = data?.total_proy || 0
  const real = data?.total_real || 0
  const cumpl = proy > 0 ? Math.round((real / proy) * 100) : null
  const fechaTxt = `${String(cell.dia).padStart(2, '0')}/${String(mes).padStart(2, '0')}/${anio}`

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-2xl max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b shrink-0">
          <div>
            <h2 className="font-semibold text-gray-800 text-sm">{cell.lider}</h2>
            <p className="text-xs text-gray-500">{cell.tipo_grupo || '—'} · {cell.zona || '—'} · {fechaTxt}</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
        </div>

        <div className="px-5 py-4 overflow-y-auto space-y-4">
          <div className="grid grid-cols-3 gap-3">
            <Mini label="Proyectado" value={formatCOP(proy)} color="blue" />
            <Mini label="Real" value={formatCOP(real)} color="green" />
            <Mini label="Cumplimiento" value={cumpl == null ? '—' : `${cumpl}%`} color={cumpl >= 95 ? 'green' : cumpl >= 75 ? 'orange' : 'red'} />
          </div>

          <p className="text-[12px] text-gray-500">
            Este valor es la <b>suma</b> de la MO de {items.length} programación{items.length !== 1 ? 'es' : ''} de esta cuadrilla en esta fecha:
          </p>

          {isLoading ? (
            <div className="py-8 text-center text-gray-400 text-sm">Cargando…</div>
          ) : items.length === 0 ? (
            <div className="py-8 text-center text-gray-400 text-sm">Sin programaciones registradas ese día.</div>
          ) : (
            <div className="overflow-x-auto border rounded-lg">
              <table className="w-full text-xs">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="text-left px-3 py-2 font-semibold text-gray-600">Proyecto / PDT</th>
                    <th className="text-left px-3 py-2 font-semibold text-gray-600">Proceso</th>
                    <th className="text-right px-3 py-2 font-semibold text-gray-600">MO Proy.</th>
                    <th className="text-right px-3 py-2 font-semibold text-gray-600">MO Real</th>
                    <th className="text-left px-3 py-2 font-semibold text-gray-600">Estado</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((it) => (
                    <tr key={it.id} className="border-t hover:bg-gray-50">
                      <td className="px-3 py-1.5">
                        <div className="text-gray-800">{it.proyecto || '—'}</div>
                        {it.num_pdt && <div className="text-[10px] text-gray-400">PDT {it.num_pdt}{it.municipio ? ` · ${it.municipio}` : ''}</div>}
                      </td>
                      <td className="px-3 py-1.5 text-gray-600">{[it.proceso, it.subproceso].filter(Boolean).join(' · ') || '—'}</td>
                      <td className="px-3 py-1.5 text-right text-blue-700">{it.mo_proyectada ? formatCOP(it.mo_proyectada) : '—'}</td>
                      <td className="px-3 py-1.5 text-right text-green-700">{it.mo_real ? formatCOP(it.mo_real) : '—'}</td>
                      <td className="px-3 py-1.5">{it.estado || '—'}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="bg-gray-50 font-semibold border-t">
                    <td className="px-3 py-2" colSpan={2}>Total</td>
                    <td className="px-3 py-2 text-right text-blue-700">{formatCOP(proy)}</td>
                    <td className="px-3 py-2 text-right text-green-700">{formatCOP(real)}</td>
                    <td className="px-3 py-2" />
                  </tr>
                </tfoot>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function Mini({ label, value, color }) {
  const c = { blue: 'text-blue-700', green: 'text-green-700', orange: 'text-orange-600', red: 'text-red-600' }
  return (
    <div className="bg-gray-50 rounded-lg p-2.5 text-center">
      <p className="text-[10px] text-gray-500">{label}</p>
      <p className={`text-base font-bold ${c[color] || 'text-gray-800'}`}>{value}</p>
    </div>
  )
}
