import { useState, useMemo } from 'react'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import api from '../../utils/api'
import { formatCOP } from '../../utils/format'
import { Download, Info, X, Search, AlertTriangle, ChevronLeft, ChevronRight, ChevronDown } from 'lucide-react'

// Matriz por PROYECTO (requisito 8 oct 2026; forma de matriz 9 oct 2026):
//   var 1 = PROYECTO / ANTECEDENTE → fila cabecera · var 2 = TIPO → sub-fila · var 3 = FECHA → columnas (días del mes)
//   Celda = cuadrillas (líderes distintos) de ese tipo en ese proyecto ese día. Al final de la fila: cuadrillas del mes,
//   cantidad (filas cuadrilla·día), MO proyectada y MO real (n/a cuando ninguna fila del grupo la tiene).
// Solo lectura: todo sale de la Programación. Clic en una celda → las filas que la sustentan.

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
const na = (v) => (v == null ? 'n/a' : formatCOP(v))
const naCls = (v, cls) => (v == null ? 'text-gray-300 italic' : cls)
const dd = (n) => String(n).padStart(2, '0')

const CAPAS = [
  { id: 'cuadrillas', label: 'Cuadrillas' },
  { id: 'proyeccion', label: 'MO Proy.' },
  { id: 'ejecucion', label: 'MO Real' },
]
const ORDENES = [
  { id: 'n', label: 'Cantidad' },
  { id: 'proy', label: 'MO Proy.' },
  { id: 'real', label: 'MO Real' },
  { id: 'nombre', label: 'A-Z' },
]

export default function ProyectosTab({ unidad, cfg, opciones }) {
  const now = new Date()
  const [anio, setAnio] = useState(now.getFullYear())
  const [mes, setMes] = useState(now.getMonth() + 1)
  const [zona, setZona] = useState('')
  const [tipo, setTipo] = useState('')
  const [buscar, setBuscar] = useState('')
  const [orden, setOrden] = useState('n')
  const [capa, setCapa] = useState('cuadrillas')
  const [cerrados, setCerrados] = useState({})        // proyecto → true (sub-filas ocultas)
  const [det, setDet] = useState(null)                  // {proyecto, tipo|null, dia|null, sin_proyecto}

  const params = useMemo(() => {
    const p = { unidad, anio, mes }
    if (zona) p.zona = zona
    if (tipo) p.tipo_grupo = tipo
    return p
  }, [unidad, anio, mes, zona, tipo])

  const { data, isLoading } = useQuery({
    queryKey: ['matriz-proyectos', params],
    queryFn: () => api.get('/kpis/matriz-proyectos', { params }).then((r) => r.data),
    placeholderData: keepPreviousData,
  })

  const dias = data?.dias || []
  const totDia = data?.totales_dia || {}
  const zonas = opciones?.zonas?.length ? opciones.zonas : cfg.zonas
  const tipos = opciones?.tipos?.length ? opciones.tipos : cfg.tipoOptions.filter(Boolean)
  const periodoTxt = `${MESES[mes]} ${anio}`

  const cambiarMes = (delta) => {
    let m = mes + delta, a = anio
    if (m < 1) { m = 12; a-- } else if (m > 12) { m = 1; a++ }
    setMes(m); setAnio(a)
  }

  const proyectos = useMemo(() => {
    let lista = data?.proyectos || []
    const q = buscar.trim().toLowerCase()
    if (q) lista = lista.filter((p) => (p.proyecto || '').toLowerCase().includes(q) || p.tipos.some((t) => (t.tipo || '').toLowerCase().includes(q)))
    const val = (x) => (orden === 'proy' ? (x.mo_proy ?? -1) : orden === 'real' ? (x.mo_real ?? -1) : x.n)
    const cmp = (a, b) => (orden === 'nombre' ? (a.proyecto || '').localeCompare(b.proyecto || '') : val(b) - val(a))
    return [...lista].sort((a, b) => (a.sin_proyecto - b.sin_proyecto) || cmp(a, b))   // «Sin proyecto» siempre al final
      .map((p) => ({ ...p, tipos: [...p.tipos].sort(cmp) }))
  }, [data, buscar, orden])

  // Valor de una celda (proyecto×día o tipo×día) según la capa
  const celda = (c) => {
    if (!c) return { txt: '', cls: '' }
    if (capa === 'cuadrillas') return { txt: String(c.cuadrillas || c.n), cls: 'text-gray-800 font-medium' }
    if (capa === 'proyeccion') return { txt: c.proy ? compact(c.proy) : '·', cls: c.proy ? 'text-blue-700' : 'text-gray-300' }
    return { txt: c.real ? compact(c.real) : '·', cls: c.real ? 'text-green-700 font-medium' : 'text-gray-300' }
  }
  const valorFila = (x) => (capa === 'proyeccion' ? na(x.mo_proy) : capa === 'ejecucion' ? na(x.mo_real) : x.n)

  const exportCSV = () => {
    const v = (x) => (x == null ? 'n/a' : Math.round(x))
    const nombre = (p) => `"${(p.sin_proyecto ? 'SIN PROYECTO' : p.proyecto).replace(/"/g, "'")}"`
    const diasDe = (x) => dias.map((d) => (x.dias[String(d.dia)] ? x.dias[String(d.dia)].cuadrillas : ''))
    const head = ['PROYECTO / ANTECEDENTE', 'TIPO', ...dias.map((d) => d.dia), 'CUADRILLAS MES', 'CANT (CUADRILLA·DIA)', 'MO PROYECTADA', 'MO REAL', '%CUMPL']
    const lines = [head.join(';')]
    proyectos.forEach((p) => {
      p.tipos.forEach((t) => lines.push([nombre(p), t.tipo || '—', ...diasDe(t), t.cuadrillas, t.n, v(t.mo_proy), v(t.mo_real), t.cumplimiento != null ? Math.round(t.cumplimiento * 100) + '%' : ''].join(';')))
      lines.push([nombre(p), 'SUBTOTAL', ...diasDe(p), p.cuadrillas, p.n, v(p.mo_proy), v(p.mo_real), p.cumplimiento != null ? Math.round(p.cumplimiento * 100) + '%' : ''].join(';'))
    })
    if (data) lines.push(['TOTAL', '', ...dias.map((d) => totDia[String(d.dia)]?.cuadrillas ?? ''), data.cuadrillas, data.n, v(data.mo_proy), v(data.mo_real), data.cumplimiento != null ? Math.round(data.cumplimiento * 100) + '%' : ''].join(';'))
    const blob = new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8;' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `matriz_proyectos_${unidad}_${anio}-${dd(mes)}.csv`
    a.click()
  }

  const selCls = (activo) => `px-2 py-1.5 text-xs border rounded-lg ${activo ? 'border-blue-300 text-blue-800 font-semibold' : 'border-gray-200 text-gray-600'}`
  const todosCerrados = proyectos.length > 0 && proyectos.every((p) => cerrados[p.proyecto])
  const plegarTodo = () => setCerrados(todosCerrados ? {} : Object.fromEntries(proyectos.map((p) => [p.proyecto, true])))

  return (
    <div className="space-y-4">
      {/* Controles */}
      <div className="bg-white rounded-xl border shadow-sm p-3 flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-1 bg-gray-50 border rounded-lg">
          <button onClick={() => cambiarMes(-1)} className="px-1.5 py-1.5 text-gray-500 hover:text-gray-800"><ChevronLeft size={15} /></button>
          <span className="text-xs font-semibold text-gray-700 min-w-[110px] text-center">{periodoTxt}{isLoading && ' …'}</span>
          <button onClick={() => cambiarMes(1)} className="px-1.5 py-1.5 text-gray-500 hover:text-gray-800"><ChevronRight size={15} /></button>
        </div>
        <select value={zona} onChange={(e) => setZona(e.target.value)} className={selCls(!!zona)}>
          <option value="">Todas las zonas</option>
          {zonas.map((z) => <option key={z} value={z}>{z}</option>)}
        </select>
        <select value={tipo} onChange={(e) => setTipo(e.target.value)} className={selCls(!!tipo)} title="var 2: TIPO">
          <option value="">Todos los tipos</option>
          {tipos.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <div className="relative">
          <Search size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
          <input value={buscar} onChange={(e) => setBuscar(e.target.value)} placeholder="Buscar proyecto…"
            className="pl-7 pr-2 py-1.5 text-xs border border-gray-200 rounded-lg w-44 focus:outline-none focus:border-blue-300" />
        </div>
        <div className="flex-1" />
        <div className="flex items-center gap-1">
          <span className="text-[11px] text-gray-400">Celda:</span>
          <div className="flex gap-0.5 bg-gray-100 p-0.5 rounded-lg">
            {CAPAS.map((c) => (
              <button key={c.id} onClick={() => setCapa(c.id)}
                className={`px-2 py-1 text-xs rounded-md transition ${capa === c.id ? 'bg-white text-blue-700 font-semibold shadow-sm' : 'text-gray-500 hover:text-gray-800'}`}>
                {c.label}
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-1">
          <span className="text-[11px] text-gray-400">Orden:</span>
          <div className="flex gap-0.5 bg-gray-100 p-0.5 rounded-lg">
            {ORDENES.map((o) => (
              <button key={o.id} onClick={() => setOrden(o.id)}
                className={`px-2 py-1 text-xs rounded-md transition ${orden === o.id ? 'bg-white text-blue-700 font-semibold shadow-sm' : 'text-gray-500 hover:text-gray-800'}`}>
                {o.label}
              </button>
            ))}
          </div>
        </div>
        <button onClick={plegarTodo} className="text-xs border px-2.5 py-1.5 rounded-lg text-gray-600 hover:bg-gray-50">
          {todosCerrados ? 'Desplegar tipos' : 'Plegar tipos'}
        </button>
        <button onClick={exportCSV} className="flex items-center gap-1.5 text-xs border px-2.5 py-1.5 rounded-lg text-gray-600 hover:bg-gray-50">
          <Download size={13} /> CSV
        </button>
      </div>

      {/* Resumen del mes */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <Card label="Proyectos" value={data?.proyectos_con_nombre ?? '—'} sub={data?.sin_proyecto_n ? `+ ${data.sin_proyecto_n} filas sin proyecto` : null} />
        <Card label="Cuadrillas" value={data?.cuadrillas ?? '—'} sub={data ? `${data.n} cuadrilla·día · ${data.dias_trab} días` : null} />
        <Card label="MO Proyectada" value={na(data?.mo_proy)} color="blue" />
        <Card label="MO Real" value={na(data?.mo_real)} color="green" />
        <Card label="% Cumplimiento" value={pct(data?.cumplimiento)} color={data?.cumplimiento >= 0.95 ? 'green' : data?.cumplimiento >= 0.75 ? 'orange' : 'red'} />
      </div>

      {data?.por_tipo?.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] text-gray-400 font-medium">Por tipo:</span>
          {data.por_tipo.map((t) => (
            <button key={t.tipo || '—'} onClick={() => setTipo(tipo === t.tipo ? '' : t.tipo)} title="Filtrar por este tipo"
              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] border ${tipo === t.tipo ? 'bg-blue-600 text-white border-blue-600' : 'bg-blue-50 text-blue-700 border-blue-200 hover:bg-blue-100'}`}>
              <b>{t.tipo || '—'}</b> {t.cuadrillas} cuad. · {t.n}
            </button>
          ))}
        </div>
      )}

      {/* De dónde sale */}
      <div className="flex items-start gap-2 text-[12px] text-gray-600 bg-blue-50/60 border border-blue-100 rounded-lg px-3 py-2">
        <Info size={15} className="text-blue-500 shrink-0 mt-0.5" />
        <span>
          Estos datos <b>no se cargan aquí</b>: salen de la <b>Programación</b>. Cada fila es un <b>PROYECTO / ANTECEDENTE</b> con
          sus <b>TIPOS</b> debajo; cada columna es un día del mes (<b>FECHA</b>). La celda dice{' '}
          {capa === 'cuadrillas' ? <><b>cuántas cuadrillas</b> de ese tipo tuvo el proyecto ese día</> : capa === 'proyeccion' ? <>la <b>MO proyectada</b> de ese día</> : <>la <b>MO real</b> de ese día</>}.
          Al final: cuadrillas del mes, cantidad (cuadrilla·día), MO proyectada y MO real; <b>n/a</b> = ninguna fila tiene esa MO.
          <b> Haz clic en una celda</b> para ver las filas que la sustentan.
        </span>
      </div>

      {/* Matriz */}
      <div className="bg-white rounded-xl border shadow-sm overflow-auto" style={{ maxHeight: '68vh' }}>
        {!isLoading && proyectos.length === 0 ? (
          <div className="p-10 text-center text-gray-400 text-sm">Sin datos de programación en {periodoTxt}{buscar ? ` que contengan «${buscar}»` : ''}.</div>
        ) : (
          <table className="text-[11px] border-collapse">
            <thead className="sticky top-0 z-20">
              <tr className="bg-gray-50 text-gray-600">
                <th className="sticky left-0 z-30 bg-gray-50 text-left px-3 py-2 font-semibold border-b border-r min-w-[250px] max-w-[250px]">Proyecto / Antecedente · Tipo</th>
                {dias.map((d) => (
                  <th key={d.dia} className={`px-1 py-1 text-center font-medium border-b ${d.finde ? 'bg-gray-200 text-gray-500' : ''}`} style={{ minWidth: 32 }}>
                    <div className="leading-none">{d.dia}</div>
                    <div className="text-[9px] text-gray-400 leading-none">{d.dow}</div>
                  </th>
                ))}
                <th className="px-2 py-2 text-right font-semibold border-b border-l bg-gray-50" title="Líderes distintos en el mes">Cuad.</th>
                <th className="px-2 py-2 text-right font-semibold border-b bg-gray-50" title="Filas de programación (cuadrilla·día)">Cant.</th>
                <th className="px-2 py-2 text-right font-semibold border-b bg-gray-50 min-w-[100px]">MO Proy.</th>
                <th className="px-2 py-2 text-right font-semibold border-b bg-gray-50 min-w-[100px]">MO Real</th>
                <th className="px-2 py-2 text-center font-semibold border-b bg-gray-50 min-w-[56px]">Cumpl.</th>
              </tr>
            </thead>
            <tbody>
              {proyectos.map((p) => (
                <GrupoProyecto key={p.proyecto || '__sin__'} p={p} dias={dias} celda={celda} cerrado={!!cerrados[p.proyecto]}
                  onToggle={() => setCerrados((s) => ({ ...s, [p.proyecto]: !s[p.proyecto] }))}
                  onCelda={(t, d) => setDet({ proyecto: p.proyecto, sin_proyecto: p.sin_proyecto, tipo: t ? t.tipo : null, dia: d ? d.dia : null })} />
              ))}
            </tbody>
            {data && (
              <tfoot className="sticky bottom-0 z-20">
                <tr className="bg-gray-100 font-semibold text-gray-800">
                  <td className="sticky left-0 z-30 bg-gray-100 px-3 py-2 border-t border-r">TOTAL DÍA · {periodoTxt}</td>
                  {dias.map((d) => {
                    const { txt } = celda(totDia[String(d.dia)])
                    return <td key={d.dia} className={`px-1 py-2 text-center text-gray-700 border-t ${d.finde ? 'bg-gray-200' : ''}`}>{txt}</td>
                  })}
                  <td className="px-2 py-2 border-t border-l text-right">{data.cuadrillas}</td>
                  <td className="px-2 py-2 border-t text-right">{data.n}</td>
                  <td className={`px-2 py-2 border-t text-right whitespace-nowrap ${naCls(data.mo_proy, 'text-blue-700')}`}>{na(data.mo_proy)}</td>
                  <td className={`px-2 py-2 border-t text-right whitespace-nowrap ${naCls(data.mo_real, 'text-green-700')}`}>{na(data.mo_real)}</td>
                  <td className={`px-2 py-2 border-t text-center ${cumplColor(data.cumplimiento)}`}>{pct(data.cumplimiento)}</td>
                </tr>
              </tfoot>
            )}
          </table>
        )}
      </div>

      <p className="text-[11px] text-gray-400">
        Pivote de solo lectura de la programación. Fines de semana sombreados. Las filas sin PROYECTO / ANTECEDENTE van al final
        para que se vean y se completen en la Programación. {capa !== 'cuadrillas' && 'Celda · = programación sin valor ese día.'}
      </p>

      {det && <DetalleProyecto det={det} params={params} periodoTxt={periodoTxt} onClose={() => setDet(null)} />}
    </div>
  )
}

function GrupoProyecto({ p, dias, celda, cerrado, onToggle, onCelda }) {
  const nombre = p.sin_proyecto ? 'SIN PROYECTO / ANTECEDENTE' : p.proyecto
  const hdr = p.sin_proyecto ? 'bg-amber-50 text-amber-900' : 'bg-blue-50/70 text-blue-900'
  const hdrSticky = p.sin_proyecto ? 'bg-amber-50' : 'bg-blue-50'
  const Chev = cerrado ? ChevronRight : ChevronDown
  return (
    <>
      <tr className={`${hdr} font-semibold`}>
        <td className={`sticky left-0 z-10 ${hdrSticky} px-2 py-1.5 border-b border-t border-r max-w-[250px]`} title={nombre}>
          <button onClick={onToggle} className="flex items-center gap-1 w-full text-left">
            <Chev size={12} className="shrink-0 opacity-60" />
            {p.sin_proyecto && <AlertTriangle size={12} className="shrink-0" />}
            <span className="truncate">{nombre}</span>
            <span className="ml-auto pl-1 text-[9px] font-normal opacity-60 whitespace-nowrap">{p.tipos.length} tipo{p.tipos.length !== 1 ? 's' : ''}</span>
          </button>
        </td>
        {dias.map((d) => {
          const c = p.dias[String(d.dia)]
          const { txt, cls } = celda(c)
          return (
            <td key={d.dia} onClick={c ? () => onCelda(null, d) : undefined} title={c ? 'Ver las filas de este proyecto ese día' : undefined}
              className={`px-1 py-1.5 text-center border-b border-t ${d.finde ? 'bg-black/5' : ''} ${cls} ${c ? 'cursor-pointer hover:ring-2 hover:ring-blue-400 hover:ring-inset' : ''}`}>
              {txt}
            </td>
          )
        })}
        <td className="px-2 py-1.5 border-b border-t border-l text-right">{p.cuadrillas}</td>
        <td className="px-2 py-1.5 border-b border-t text-right">{p.n}</td>
        <td className={`px-2 py-1.5 border-b border-t text-right whitespace-nowrap ${naCls(p.mo_proy, '')}`}>{na(p.mo_proy)}</td>
        <td className={`px-2 py-1.5 border-b border-t text-right whitespace-nowrap ${naCls(p.mo_real, '')}`}>{na(p.mo_real)}</td>
        <td className="px-2 py-1.5 border-b border-t text-center">
          <span className={`inline-block px-1.5 py-0.5 rounded-full text-[10px] ${cumplColor(p.cumplimiento)}`}>{pct(p.cumplimiento)}</span>
        </td>
      </tr>
      {!cerrado && p.tipos.map((t) => (
        <tr key={t.tipo || '—'} className="border-b hover:bg-blue-50/40">
          <td className="sticky left-0 z-10 bg-white px-3 py-1 border-r cursor-pointer" onClick={() => onCelda(t, null)} title="Ver todas las filas de este tipo en el mes">
            <span className="text-gray-400 mr-1">↳</span>
            <span className="font-medium text-gray-800">{t.tipo || <span className="text-gray-400 italic">sin tipo</span>}</span>
            <span className="text-[9px] text-gray-400 ml-1">{t.dias_trab} días</span>
          </td>
          {dias.map((d) => {
            const c = t.dias[String(d.dia)]
            const { txt, cls } = celda(c)
            return (
              <td key={d.dia} onClick={c ? () => onCelda(t, d) : undefined} title={c ? 'Ver de dónde sale este dato' : undefined}
                className={`px-1 py-1 text-center ${d.finde ? 'bg-gray-50' : ''} ${cls} ${c ? 'cursor-pointer hover:ring-2 hover:ring-blue-400 hover:ring-inset' : ''}`}>
                {txt}
              </td>
            )
          })}
          <td className="px-2 py-1 border-l text-right text-gray-700">{t.cuadrillas}</td>
          <td className="px-2 py-1 text-right text-gray-800 font-medium">{t.n}</td>
          <td className={`px-2 py-1 text-right whitespace-nowrap ${naCls(t.mo_proy, 'text-blue-700')}`}>{na(t.mo_proy)}</td>
          <td className={`px-2 py-1 text-right whitespace-nowrap ${naCls(t.mo_real, 'text-green-700 font-medium')}`}>{na(t.mo_real)}</td>
          <td className="px-2 py-1 text-center">
            <span className={`inline-block px-1.5 py-0.5 rounded-full text-[10px] ${cumplColor(t.cumplimiento)}`}>{pct(t.cumplimiento)}</span>
          </td>
        </tr>
      ))}
    </>
  )
}

function Card({ label, value, sub, color = 'gray' }) {
  const colors = { gray: 'text-gray-800', blue: 'text-blue-700', green: 'text-green-700', orange: 'text-orange-600', red: 'text-red-600' }
  return (
    <div className="bg-white rounded-xl border shadow-sm p-3">
      <p className="text-[11px] text-gray-500 font-medium">{label}</p>
      <p className={`text-xl font-bold ${colors[color]} truncate`} title={typeof value === 'string' ? value : undefined}>{value}</p>
      {sub && <p className="text-[10px] text-gray-400">{sub}</p>}
    </div>
  )
}

// Popup: las filas de programación detrás de una celda (proyecto × tipo × día) o de una fila (sin día)
function DetalleProyecto({ det, params, periodoTxt, onClose }) {
  const q = { unidad: params.unidad, anio: params.anio, mes: params.mes, proyecto: det.proyecto || '' }
  if (params.zona) q.zona = params.zona
  if (det.tipo != null) q.tipo = det.tipo
  if (det.dia) q.dia = det.dia
  const { data, isLoading } = useQuery({
    queryKey: ['matriz-proyectos-detalle', q],
    queryFn: () => api.get('/kpis/matriz-proyectos-detalle', { params: q }).then((r) => r.data),
  })
  const items = data?.items || []
  const nombre = det.sin_proyecto ? 'SIN PROYECTO / ANTECEDENTE' : det.proyecto
  const cuando = det.dia ? `${dd(det.dia)}/${dd(params.mes)}/${params.anio}` : periodoTxt
  const fmtF = (f) => (f ? `${f.slice(8, 10)}/${f.slice(5, 7)}/${f.slice(0, 4)}` : '—')

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-4xl max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-3 px-5 py-3 border-b shrink-0">
          <div className="min-w-0">
            <h2 className="font-semibold text-gray-800 text-sm break-words">{nombre}</h2>
            <p className="text-xs text-gray-500">{det.tipo != null ? `Tipo ${det.tipo || '—'}` : 'Todos los tipos'} · {cuando}</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700 shrink-0"><X size={18} /></button>
        </div>

        <div className="px-5 py-4 overflow-y-auto space-y-4">
          <div className="grid grid-cols-4 gap-3">
            <Mini label="Cuadrillas" value={data?.cuadrillas ?? '—'} />
            <Mini label="Filas (cuadrilla·día)" value={items.length} />
            <Mini label="MO Proyectada" value={na(data?.mo_proy)} color="blue" />
            <Mini label="MO Real" value={na(data?.mo_real)} color="green" />
          </div>

          {isLoading ? (
            <div className="py-8 text-center text-gray-400 text-sm">Cargando…</div>
          ) : items.length === 0 ? (
            <div className="py-8 text-center text-gray-400 text-sm">Sin filas de programación para esta celda.</div>
          ) : (
            <div className="overflow-x-auto border rounded-lg">
              <table className="w-full text-xs">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="text-left px-3 py-2 font-semibold text-gray-600">Fecha</th>
                    <th className="text-left px-3 py-2 font-semibold text-gray-600">Cuadrilla (líder)</th>
                    <th className="text-left px-3 py-2 font-semibold text-gray-600">Tipo</th>
                    <th className="text-left px-3 py-2 font-semibold text-gray-600">Zona</th>
                    <th className="text-left px-3 py-2 font-semibold text-gray-600">Proceso · # PDT</th>
                    <th className="text-right px-3 py-2 font-semibold text-gray-600">MO Proy.</th>
                    <th className="text-right px-3 py-2 font-semibold text-gray-600">MO Real</th>
                    <th className="text-left px-3 py-2 font-semibold text-gray-600">Estado</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((it) => (
                    <tr key={it.id} className="border-t hover:bg-gray-50">
                      <td className="px-3 py-1.5 whitespace-nowrap text-gray-700">{fmtF(it.fecha)}</td>
                      <td className="px-3 py-1.5 text-gray-800">{it.lider || '—'}</td>
                      <td className="px-3 py-1.5 text-gray-600">{it.tipo || '—'}</td>
                      <td className="px-3 py-1.5 text-gray-600">{it.zona || '—'}</td>
                      <td className="px-3 py-1.5 text-gray-600">
                        <div>{[it.proceso, it.subproceso].filter(Boolean).join(' · ') || '—'}</div>
                        {it.num_pdt && <div className="text-[10px] text-gray-400">PDT {it.num_pdt}{it.municipio ? ` · ${it.municipio}` : ''}</div>}
                      </td>
                      <td className={`px-3 py-1.5 text-right whitespace-nowrap ${naCls(it.mo_proyectada, 'text-blue-700')}`}>{na(it.mo_proyectada)}</td>
                      <td className={`px-3 py-1.5 text-right whitespace-nowrap ${naCls(it.mo_real, 'text-green-700')}`}>{na(it.mo_real)}</td>
                      <td className="px-3 py-1.5 text-gray-600">{it.estado || it.estado_pdt || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function Mini({ label, value, color }) {
  const c = { blue: 'text-blue-700', green: 'text-green-700' }
  return (
    <div className="bg-gray-50 rounded-lg p-2.5 text-center">
      <p className="text-[10px] text-gray-500">{label}</p>
      <p className={`text-base font-bold ${c[color] || 'text-gray-800'}`}>{value}</p>
    </div>
  )
}
