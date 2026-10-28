import { useState, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../utils/api'
import toast from 'react-hot-toast'
import {
  Boxes, Upload, FileDown, Table2, History, FolderOpen, BarChart3, RefreshCw,
  Search as SearchIcon, X, Download, AlertTriangle, Copy, Lock,
} from 'lucide-react'

const M = '/api/moes'   // baseURL del módulo

const fmtFecha = (iso) => {
  if (!iso) return ''
  try { return new Date(iso).toLocaleDateString('es-CO', { day: '2-digit', month: 'short', year: '2-digit' }) }
  catch { return iso }
}
const fmtDT = (iso) => {
  if (!iso) return ''
  try { return new Date(iso).toLocaleString('es-CO', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) }
  catch { return iso }
}
const money = (v) => (v == null ? '' : `$${Number(v).toLocaleString('es-CO', { maximumFractionDigits: 0 })}`)
const pct = (v) => `${(Number(v || 0) * 100).toFixed(1)}%`

const estadoColor = (e) => {
  const s = (e || '').toUpperCase()
  if (/FINALIZ|EJECUT/.test(s)) return 'bg-green-100 text-green-700'
  if (/PARCIAL/.test(s)) return 'bg-amber-100 text-amber-700'
  if (/NO EJEC|RECHAZ|ANULAD/.test(s)) return 'bg-red-100 text-red-700'
  if (/REPROG/.test(s)) return 'bg-orange-100 text-orange-700'
  if (/PROGRAM/.test(s)) return 'bg-blue-100 text-blue-700'
  return 'bg-gray-100 text-gray-600'
}

// Celda editable (campos amarillos que llena la unidad CHOCONTA)
function EditCell({ value, onSave, type = 'text', width = 'w-28' }) {
  const [v, setV] = useState(value ?? '')
  return (
    <input
      className={`${width} px-1.5 py-1 text-xs border border-amber-200 bg-amber-50 rounded focus:bg-white focus:border-amber-400 outline-none`}
      type={type} value={v}
      onChange={(e) => setV(e.target.value)}
      onBlur={() => { if ((value ?? '') !== v) onSave(v === '' ? null : v) }}
    />
  )
}

export default function BolsasMoesPage() {
  const qc = useQueryClient()
  const [tab, setTab] = useState('datos')
  const [busq, setBusq] = useState('')
  const [fEstado, setFEstado] = useState('')
  const [soloDup, setSoloDup] = useState(false)
  const [soloAus, setSoloAus] = useState(false)
  const [soloInco, setSoloInco] = useState(false)
  const [verTimeline, setVerTimeline] = useState(null)   // {clave, antecedente}
  const [recibo, setRecibo] = useState(null)             // resumen del último cruce
  const [fechaVersion, setFechaVersion] = useState(() => new Date().toISOString().slice(0, 10))
  const fileRef = useRef()
  const lastFileRef = useRef(null)                       // último archivo subido (para 'forzar')

  const { data, isLoading } = useQuery({
    queryKey: ['moes', busq, fEstado, soloDup, soloAus],
    queryFn: async () => (await api.get('', {
      baseURL: M,
      params: {
        q: busq || undefined, estado: fEstado || undefined,
        solo_duplicados: soloDup || undefined, solo_ausentes: soloAus || undefined,
      },
    })).data,
  })
  const items = data?.items || []

  const { data: resumen } = useQuery({
    queryKey: ['moes-resumen'],
    queryFn: async () => (await api.get('/resumen', { baseURL: M })).data,
  })
  const { data: opciones } = useQuery({
    queryKey: ['moes-opciones'],
    queryFn: async () => (await api.get('/opciones', { baseURL: M })).data,
  })
  const { data: imports } = useQuery({
    queryKey: ['moes-imports'],
    queryFn: async () => (await api.get('/importaciones', { baseURL: M })).data,
    enabled: tab === 'importaciones',
  })
  const { data: cambios } = useQuery({
    queryKey: ['moes-cambios', soloInco],
    queryFn: async () => (await api.get('/cambios', { baseURL: M, params: { solo_inconsistencias: soloInco || undefined } })).data,
    enabled: tab === 'cambios',
  })
  const { data: ind } = useQuery({
    queryKey: ['moes-ind'],
    queryFn: async () => (await api.get('/indicadores', { baseURL: M })).data,
    enabled: tab === 'indicadores',
  })

  const refrescar = () => {
    ['moes', 'moes-resumen', 'moes-opciones', 'moes-imports', 'moes-cambios', 'moes-ind']
      .forEach((k) => qc.invalidateQueries({ queryKey: [k] }))
  }

  const importar = useMutation({
    mutationFn: async ({ file, password, forzar }) => {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('fecha_version', fechaVersion)
      if (password) fd.append('password', password)
      if (forzar) fd.append('forzar', 'true')
      return (await api.post('/import', fd, { baseURL: M })).data
    },
    onSuccess: (r) => {
      setRecibo(r)   // recibo del cruce, en pantalla (no toasts efímeros)
      if (r.aplicado === false) toast('Versión más antigua: no se actualizó el estado', { icon: '⚠', duration: 7000, style: { background: '#fffbeb', color: '#92400e' } })
      else toast.success('Excel procesado')
      if (r.cifrado) toast('Archivo cifrado desbloqueado', { duration: 6000 })
      refrescar()
    },
    onError: async (e, vars) => {
      const detail = e.response?.data?.detail || ''
      if (/cifrad|contrase/i.test(detail) && !vars.password) {
        const pw = window.prompt('El Excel está cifrado. Escribe la contraseña de apertura:')
        if (pw != null) { importar.mutate({ ...vars, password: pw }); return }
      }
      toast.error(detail || 'Error al importar')
    },
  })
  const forzarUltimo = () => { if (lastFileRef.current) importar.mutate({ file: lastFileRef.current, forzar: true }) }

  const patch = useMutation({
    mutationFn: async ({ id, campo, valor }) => (await api.patch(`/${id}`, { [campo]: valor }, { baseURL: M })).data,
    onSuccess: (row) => {
      qc.setQueryData(['moes', busq, fEstado, soloDup, soloAus], (old) =>
        old ? { ...old, items: old.items.map((it) => (it.id === row.id ? row : it)) } : old)
      qc.invalidateQueries({ queryKey: ['moes-cambios'] })
    },
    onError: () => toast.error('No se pudo guardar'),
  })
  const save = (id, campo) => (valor) => patch.mutate({ id, campo, valor })

  const onFile = () => {
    const f = fileRef.current?.files?.[0]
    if (f) { lastFileRef.current = f; importar.mutate({ file: f }) }
    if (fileRef.current) fileRef.current.value = ''
  }

  const exportar = async () => {
    try {
      const res = await api.get('/export', { baseURL: M, responseType: 'blob' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([res.data]))
      a.download = `bolsas_moes_${new Date().toISOString().slice(0, 10)}.xlsx`
      a.click(); URL.revokeObjectURL(a.href)
    } catch { toast.error('Error al exportar') }
  }

  const descargarImport = async (i) => {
    const res = await api.get(`/importaciones/${i.id}/descargar`, { baseURL: M, responseType: 'blob' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([res.data]))
    a.download = i.nombre; a.click(); URL.revokeObjectURL(a.href)
  }

  return (
    <div className="p-4 md:p-6 max-w-[1700px] mx-auto">
      {/* Cabecera */}
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <Boxes className="text-blue-700" size={22} />
        <h1 className="text-xl font-bold text-gray-800">Bolsas MOES</h1>
        <span className="text-xs bg-blue-100 text-blue-700 px-2 py-0.5 rounded-full">Pisos Móviles · Chocontá</span>
        <div className="flex flex-wrap items-center gap-2 ml-auto">
          <input ref={fileRef} type="file" accept=".xlsx,.xlsm,.xls" hidden onChange={onFile} />
          <label className="text-xs text-gray-500">Fecha de la versión</label>
          <input type="date" value={fechaVersion} onChange={(e) => setFechaVersion(e.target.value)}
            title="Fecha de ESTA bolsa. El sistema usa esta fecha (no el orden de carga) para saber cuál es la más reciente."
            className="px-2 py-1.5 text-sm border rounded-lg text-gray-700" />
          <button onClick={() => fileRef.current?.click()} disabled={importar.isLoading}
            className="flex items-center gap-2 px-3 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50">
            <Upload size={15} /> {importar.isLoading ? 'Procesando…' : 'Subir Excel de Enel'}
          </button>
          <button onClick={exportar}
            className="flex items-center gap-2 px-3 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700">
            <FileDown size={15} /> Exportar
          </button>
          <button onClick={refrescar} className="p-2 text-gray-500 hover:text-blue-700" title="Refrescar"><RefreshCw size={16} /></button>
        </div>
      </div>

      {/* KPIs rápidos */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <KPI label="Tickets" value={resumen?.total ?? 0} />
        <KPI label="Duplicados" value={resumen?.duplicados ?? 0} tone={resumen?.duplicados ? 'amber' : 'gray'}
          hint="Mismo ticket repetido con el mismo estado · clic para verlos"
          onClick={() => { setTab('datos'); setSoloDup(true); setSoloAus(false) }} />
        <KPI label="Excel subidos" value={resumen?.imports ?? 0} onClick={() => setTab('importaciones')} />
        <KPI label="Estados" value={(resumen?.por_estado || []).length} onClick={() => setTab('indicadores')} />
      </div>

      {/* Recibo del último cruce: qué hizo el sistema, en lenguaje claro */}
      {recibo && <ReciboCruce r={recibo} onClose={() => setRecibo(null)} onVerCambios={() => setTab('cambios')} onForzar={forzarUltimo} />}

      {/* Tabs */}
      <div className="flex gap-1 border-b mb-3 flex-wrap">
        {[['datos', 'Datos actuales', Table2], ['cambios', 'Línea de tiempo', History],
          ['importaciones', 'Excel subidos', FolderOpen], ['indicadores', 'Indicadores', BarChart3]].map(([id, label, Icon]) => (
          <button key={id} onClick={() => setTab(id)}
            className={`flex items-center gap-1.5 px-4 py-2 text-sm font-medium -mb-px border-b-2 ${
              tab === id ? 'border-blue-600 text-blue-700' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
            <Icon size={15} /> {label}
          </button>
        ))}
      </div>

      {/* ── DATOS ACTUALES ── */}
      {tab === 'datos' && (
        <>
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <div className="relative">
              <SearchIcon size={14} className="absolute left-2 top-2.5 text-gray-400" />
              <input value={busq} onChange={(e) => setBusq(e.target.value)} placeholder="Antecedente, LCL, ODM, municipio…"
                className="pl-7 pr-2 py-1.5 text-sm border rounded-lg w-72" />
              {busq && <button onClick={() => setBusq('')} className="absolute right-1.5 top-2 text-gray-400 hover:text-gray-700"><X size={14} /></button>}
            </div>
            <select value={fEstado} onChange={(e) => setFEstado(e.target.value)} className="px-2 py-1.5 text-sm border rounded-lg bg-white">
              <option value="">Estado: todos</option>
              {(opciones?.estados || []).map((e) => <option key={e} value={e}>{e}</option>)}
            </select>
            <label className="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer">
              <input type="checkbox" checked={soloDup} onChange={(e) => setSoloDup(e.target.checked)} /> Solo duplicados
            </label>
            <label className="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer">
              <input type="checkbox" checked={soloAus} onChange={(e) => setSoloAus(e.target.checked)} /> No en última versión
            </label>
            <span className="ml-auto text-xs text-gray-400">{items.length} de {data?.total ?? 0}</span>
          </div>
          <p className="text-[11px] text-gray-500 mb-2">
            Estado consolidado por ticket (clave = <b>Antecedente + LCL</b>). Las celdas <span className="bg-amber-50 border border-amber-200 px-1 rounded">amarillas</span> las editas tú; clic en una fila para ver su línea de tiempo.
          </p>
          {isLoading ? <p className="p-6 text-gray-400 text-sm">Cargando…</p>
          : items.length === 0 ? <p className="p-6 text-gray-400 text-sm border rounded-lg bg-white">Sin tickets. Sube el Excel de Enel para empezar.</p>
          : (
            <div className="bg-white border rounded-lg overflow-auto">
              <table className="text-xs w-full whitespace-nowrap">
                <thead className="bg-gray-50 text-left text-gray-600 sticky top-0">
                  <tr>
                    {['', 'Estado', 'Antecedente', 'LCL', 'Municipio', 'CTO', 'Valor MO', 'F. máx', 'F. ejec', 'SE EJEC', '% ejec', 'PDL/TCK', 'Observaciones'].map((h) => (
                      <th key={h} className="px-2 py-2 font-semibold">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {items.map((b) => (
                    <tr key={b.id} className="border-t hover:bg-blue-50/40">
                      <td className="px-1.5">
                        {b.duplicado
                          ? <span title="Ticket repetido en alguna carga"><Copy size={13} className="text-amber-500" /></span>
                          : b.ausente && <span title="No vino en la última versión cargada"><AlertTriangle size={13} className="text-orange-400" /></span>}
                      </td>
                      <td className="px-2 py-1 cursor-pointer" onClick={() => setVerTimeline({ clave: b.clave, antecedente: b.antecedente })}>
                        <span className={`px-1.5 py-0.5 rounded ${estadoColor(b.estado)}`}>{b.estado || '—'}</span>
                      </td>
                      <td className="px-2 py-1 font-mono max-w-[230px] truncate cursor-pointer" title={b.antecedente}
                        onClick={() => setVerTimeline({ clave: b.clave, antecedente: b.antecedente })}>{b.antecedente || '—'}</td>
                      <td className="px-2 py-1 font-mono">{b.lcl}</td>
                      <td className="px-2 py-1">{b.municipio}</td>
                      <td className="px-2 py-1">{b.cto}</td>
                      <td className="px-2 py-1 text-right">{money(b.valor_mo)}</td>
                      <td className="px-2 py-1">{fmtFecha(b.fecha_max)}</td>
                      <td className="px-2 py-1"><EditCell value={b.fecha_ejecucion} type="date" width="w-32" onSave={save(b.id, 'fecha_ejecucion')} /></td>
                      <td className="px-2 py-1"><EditCell value={b.se_ejecuto} width="w-16" onSave={save(b.id, 'se_ejecuto')} /></td>
                      <td className="px-2 py-1"><EditCell value={b.pct_ejecutado} type="number" width="w-16" onSave={save(b.id, 'pct_ejecutado')} /></td>
                      <td className="px-2 py-1"><EditCell value={b.pdl_tck} width="w-36" onSave={save(b.id, 'pdl_tck')} /></td>
                      <td className="px-2 py-1"><EditCell value={b.observaciones} width="w-52" onSave={save(b.id, 'observaciones')} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {/* ── LÍNEA DE TIEMPO ── */}
      {tab === 'cambios' && (
        <>
          <div className="flex items-center gap-3 mb-3">
            <label className="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer">
              <input type="checkbox" checked={soloInco} onChange={(e) => setSoloInco(e.target.checked)} />
              Solo inconsistencias
            </label>
            <span className="text-[11px] text-gray-500">Cada cambio entre versiones del Excel (alta / modificación) y tus ediciones. Rojo = inconsistencia (cambió un dato de Enel que no debería).</span>
          </div>
          <CambiosTabla cambios={cambios} />
        </>
      )}

      {/* ── EXCEL SUBIDOS ── */}
      {tab === 'importaciones' && (
        <div className="bg-white border rounded-lg overflow-hidden">
          <div className="px-4 py-2 bg-gray-50 border-b text-xs text-gray-500">
            Cada Excel de Enel cargado, con su cruce contra la base. Descargables como respaldo de procedencia.
          </div>
          {(!imports || imports.length === 0) ? <p className="p-6 text-gray-400 text-sm">Sin cargas todavía.</p> : (
            <table className="text-xs w-full">
              <thead className="bg-gray-50 text-left text-gray-600">
                <tr>{['Archivo', 'Versión', 'Hoja', 'Filas', 'Nuevas', 'Modif.', 'Sin cambio', 'Dup.', 'Subido', ''].map((h) => <th key={h} className="px-3 py-2 font-semibold">{h}</th>)}</tr>
              </thead>
              <tbody>
                {imports.map((i) => (
                  <tr key={i.id} className={`border-t hover:bg-gray-50 ${i.aplicado ? '' : 'opacity-60'}`}>
                    <td className="px-3 py-1.5 font-mono max-w-[280px] truncate" title={i.nombre}>
                      {i.cifrado && <Lock size={11} className="inline mr-1 text-amber-500" />}{i.nombre}
                      {i.columnas_nuevas?.length > 0 && <span className="ml-1 text-[10px] bg-purple-100 text-purple-700 px-1 rounded">+{i.columnas_nuevas.length} col</span>}
                    </td>
                    <td className="px-3 py-1.5 whitespace-nowrap">
                      <span className="font-medium">{fmtFecha(i.fecha_version)}</span>
                      {!i.aplicado && <span className="ml-1 text-[10px] bg-gray-200 text-gray-600 px-1 rounded" title="Versión más antigua: no actualizó el estado actual">archivada</span>}
                    </td>
                    <td className="px-3 py-1.5 text-gray-500">{i.hoja}</td>
                    <td className="px-3 py-1.5">{i.filas_total}</td>
                    <td className="px-3 py-1.5 text-green-700">{i.filas_alta}</td>
                    <td className="px-3 py-1.5 text-blue-700">{i.filas_mod}</td>
                    <td className="px-3 py-1.5 text-gray-500">{i.filas_igual}</td>
                    <td className="px-3 py-1.5 text-amber-700">{i.duplicados || ''}</td>
                    <td className="px-3 py-1.5 text-gray-500 whitespace-nowrap">{fmtDT(i.creado_en)}<br /><span className="text-gray-400">{i.subido_por}</span></td>
                    <td className="px-3 py-1.5 text-right">
                      <button onClick={() => descargarImport(i)} className="inline-flex items-center gap-1 text-blue-600 hover:text-blue-800"><Download size={14} /></button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* ── INDICADORES ── */}
      {tab === 'indicadores' && ind && (
        <div className="space-y-5">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <KPI label="Tickets" value={ind.total} />
            <KPI label="Cumplimiento (finalizados)" value={pct(ind.cumplimiento)} tone="green" />
            <KPI label="Valor presupuestado" value={money(ind.economico?.presupuestado)} />
            <KPI label="Valor ejecutado" value={`${money(ind.economico?.ejecutado)} · ${pct(ind.economico?.pct)}`} tone="blue" />
          </div>

          <Card title="Cumplimiento por estado y mes (proyección vs ejecución)">
            <table className="text-xs">
              <thead>
                <tr className="text-gray-500">
                  <th className="px-2 py-1 text-left">Estado</th>
                  {ind.pivote.meses.map((m) => <th key={m} className="px-2 py-1 text-center">{m}</th>)}
                  <th className="px-2 py-1 text-center font-semibold">Total</th>
                </tr>
              </thead>
              <tbody>
                {ind.pivote.estados.map((e) => {
                  const tot = ind.pivote.meses.reduce((s, m) => s + (ind.pivote.datos[m]?.[e] || 0), 0)
                  return (
                    <tr key={e} className="border-t">
                      <td className="px-2 py-1"><span className={`px-1.5 py-0.5 rounded ${estadoColor(e)}`}>{e}</span></td>
                      {ind.pivote.meses.map((m) => <td key={m} className="px-2 py-1 text-center">{ind.pivote.datos[m]?.[e] || ''}</td>)}
                      <td className="px-2 py-1 text-center font-semibold">{tot}</td>
                    </tr>
                  )
                })}
                <tr className="border-t bg-gray-50 font-semibold">
                  <td className="px-2 py-1">Total</td>
                  {ind.pivote.meses.map((m) => <td key={m} className="px-2 py-1 text-center">{ind.pivote.totales_mes[m]}</td>)}
                  <td className="px-2 py-1 text-center">{ind.total}</td>
                </tr>
              </tbody>
            </table>
          </Card>

          <div className="grid md:grid-cols-2 gap-4">
            <Card title="Por estado">
              {ind.por_estado.map((e) => (
                <div key={e.estado} className="flex items-center justify-between py-0.5 text-xs">
                  <span className={`px-1.5 py-0.5 rounded ${estadoColor(e.estado)}`}>{e.estado}</span>
                  <b>{e.total}</b>
                </div>
              ))}
            </Card>
            <Card title="Por municipio (top 15)">
              {ind.por_municipio.map((m) => (
                <div key={m.municipio} className="flex items-center justify-between py-0.5 text-xs">
                  <span>{m.municipio}</span><b>{m.total}</b>
                </div>
              ))}
            </Card>
          </div>
        </div>
      )}

      {verTimeline && <TimelineModal info={verTimeline} onClose={() => setVerTimeline(null)} />}
    </div>
  )
}

function KPI({ label, value, tone = 'blue', onClick, hint }) {
  const tones = { blue: 'text-blue-700', green: 'text-green-700', amber: 'text-amber-600', gray: 'text-gray-700' }
  return (
    <div onClick={onClick} title={hint}
      className={`bg-white border rounded-lg p-3 ${onClick ? 'cursor-pointer hover:border-blue-300 hover:shadow-sm' : ''}`}>
      <p className="text-xs text-gray-500">{label}</p>
      <p className={`text-2xl font-bold ${tones[tone]}`}>{value}</p>
    </div>
  )
}

// Recibo del cruce: traduce lo que hizo el sistema a lenguaje del coordinador.
function ReciboCruce({ r, onClose, onVerCambios, onForzar }) {
  const noAplicado = r.aplicado === false
  const fila = (label, val, cls = 'text-gray-800') => (
    <div className="flex items-baseline gap-2">
      <span className={`text-lg font-bold ${cls}`}>{val}</span>
      <span className="text-xs text-gray-600">{label}</span>
    </div>
  )
  return (
    <div className={`mb-4 border rounded-lg bg-white overflow-hidden ${noAplicado ? 'border-amber-300' : ''}`}>
      <div className={`flex items-center gap-2 px-4 py-2 border-b ${noAplicado ? 'bg-amber-50' : 'bg-blue-50'}`}>
        <FileDown size={15} className="text-blue-700" />
        <span className="text-sm font-semibold text-gray-800">Resultado del cruce · {r.archivo}</span>
        <span className="text-[11px] text-gray-500">hoja "{r.hoja}"{r.fecha_version ? ` · versión ${r.fecha_version}` : ''}{r.cifrado ? ' · desbloqueado' : ''}</span>
        <button onClick={onClose} className="ml-auto text-gray-400 hover:text-gray-700"><X size={16} /></button>
      </div>
      {r.aviso && (
        <div className="px-4 py-2 bg-amber-50 border-b border-amber-200 text-xs text-amber-900 flex items-start gap-2">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{r.aviso}</span>
          {noAplicado && onForzar && (
            <button onClick={onForzar} className="ml-auto shrink-0 px-2 py-1 bg-amber-600 text-white rounded hover:bg-amber-700 whitespace-nowrap">
              Forzar actualización
            </button>
          )}
        </div>
      )}
      {noAplicado ? (
        <div className="px-4 py-3 text-xs text-gray-600">El estado actual NO se modificó. El archivo quedó guardado en "Excel subidos" como respaldo.</div>
      ) : (
      <>
      <div className="px-4 py-3 grid grid-cols-2 md:grid-cols-5 gap-3">
        {fila('tickets nuevos', `+${r.alta}`, 'text-green-700')}
        {fila('cambiaron de estado', r.cambios_estado, 'text-blue-700')}
        {fila('valores a revisar', r.valores_a_revisar, r.valores_a_revisar ? 'text-red-600' : 'text-gray-700')}
        {fila('duplicados reales', r.duplicados, r.duplicados ? 'text-amber-600' : 'text-gray-700')}
        {fila('no vinieron en esta versión', r.ausentes, 'text-orange-600')}
      </div>
      <div className="px-4 pb-3 flex flex-wrap items-center gap-3 text-[11px] text-gray-500">
        <span>{r.filas_total} filas leídas · {r.modificadas} actualizadas · {r.sin_cambio} sin cambio</span>
        {r.columnas_nuevas?.length > 0 && (
          <span className="bg-purple-50 text-purple-700 border border-purple-200 rounded px-1.5 py-0.5">
            Columnas nuevas: {r.columnas_nuevas.join(', ')}
          </span>
        )}
        <button onClick={onVerCambios} className="ml-auto text-blue-600 hover:text-blue-800 font-medium">Ver la línea de tiempo →</button>
      </div>
      </>
      )}
    </div>
  )
}

function Card({ title, children }) {
  return (
    <div className="bg-white border rounded-lg p-4">
      <h3 className="text-sm font-semibold text-gray-700 mb-3">{title}</h3>
      <div className="overflow-auto">{children}</div>
    </div>
  )
}

function CambiosTabla({ cambios, compact = false }) {
  if (!cambios) return <p className="text-gray-400 text-sm p-4">Cargando…</p>
  if (!cambios.length) return <p className="text-gray-400 text-sm p-4 border rounded-lg bg-white">Sin cambios registrados.</p>
  const tipoBadge = (t) => t === 'alta' ? 'bg-green-100 text-green-700'
    : t === 'edicion' ? 'bg-purple-100 text-purple-700'
    : t === 'baja' ? 'bg-red-100 text-red-700' : 'bg-blue-100 text-blue-700'
  // Valor vacío legible (en vez del críptico ∅)
  const V = (v) => (v == null || v === '' ? <span className="text-gray-400 italic">(vacío)</span> : v)
  const tipoTxt = (t) => t === 'alta' ? 'creado' : t === 'edicion' ? 'edición' : t === 'baja' ? 'baja' : 'cambio'
  return (
    <div className="bg-white border rounded-lg overflow-auto">
      <table className="text-xs w-full">
        <thead className="bg-gray-50 sticky top-0 text-left text-gray-600">
          <tr>{['Fecha', 'Tipo', 'Excel', ...(compact ? [] : ['Antecedente']), 'Campo', 'Antes', '→', 'Después', ''].map((h, i) => <th key={i} className="px-2 py-1.5 font-semibold">{h}</th>)}</tr>
        </thead>
        <tbody>
          {cambios.map((c) => (
            <tr key={c.id} className={`border-t hover:bg-gray-50 ${c.inconsistencia ? 'bg-red-50/60' : ''}`}>
              <td className="px-2 py-1 whitespace-nowrap text-gray-500">{fmtDT(c.creado_en)}</td>
              <td className="px-2 py-1"><span className={`px-1.5 rounded ${tipoBadge(c.tipo)}`}>{tipoTxt(c.tipo)}</span></td>
              <td className={`px-2 py-1 text-gray-500 ${compact ? 'break-all' : 'max-w-[150px] truncate'}`} title={c.import_nombre || 'edición manual'}>{c.import_nombre || '✎ manual'}</td>
              {!compact && <td className="px-2 py-1 font-mono max-w-[180px] truncate" title={c.antecedente}>{c.antecedente}</td>}
              <td className="px-2 py-1 text-gray-700">{c.campo || '—'}</td>
              <td className="px-2 py-1 text-red-600 max-w-[150px] truncate">{V(c.valor_anterior)}</td>
              <td className="px-1 text-gray-400">→</td>
              <td className="px-2 py-1 text-green-700 max-w-[150px] truncate">{V(c.valor_nuevo)}</td>
              <td className="px-2 py-1">{c.inconsistencia && <span title="Inconsistencia: un dato de Enel cambió de un valor a otro" className="inline-flex"><AlertTriangle size={13} className="text-red-500" /></span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function TimelineModal({ info, onClose }) {
  const { data: cambios } = useQuery({
    queryKey: ['moes-cambios-clave', info.clave],
    queryFn: async () => (await api.get('/cambios', { baseURL: M, params: { clave: info.clave } })).data,
  })
  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-2xl max-w-4xl w-full max-h-[82vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-4 py-3 border-b">
          <div className="flex items-center gap-2">
            <History size={16} className="text-blue-700" />
            <h3 className="font-semibold text-sm text-gray-800 font-mono">{info.antecedente}</h3>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
        </div>
        {Array.isArray(cambios) && cambios.length > 0 && (() => {
          const alta = cambios.find((c) => c.tipo === 'alta')
          const mods = cambios.filter((c) => c.tipo === 'modificacion')
          const inco = cambios.filter((c) => c.inconsistencia).length
          const versiones = new Set(cambios.map((c) => c.import_nombre).filter(Boolean)).size
          return (
            <div className="px-4 py-2 bg-blue-50/60 border-b text-xs text-gray-700">
              {alta && <>Creado como <b>{alta.valor_nuevo}</b>. </>}
              {mods.length} cambio(s) en {versiones} versión(es) del Excel.
              {inco > 0 && <span className="text-red-600"> · {inco} a revisar (inconsistencia).</span>}
            </div>
          )
        })()}
        <div className="overflow-auto p-3"><CambiosTabla cambios={cambios} compact /></div>
      </div>
    </div>
  )
}
