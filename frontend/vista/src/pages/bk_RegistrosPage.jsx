import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '../utils/api'
import { ClipboardList, ChevronDown, ChevronRight, FileText, Download, RefreshCw } from 'lucide-react'

// Trazabilidad de los módulos SGO7: todo archivo subido, proceso ejecutado o descarga
// generada queda aquí con el usuario SGO7 que lo hizo. Solo desarrollador (ver App.jsx).
const MODULOS = [
  { id: '',             label: 'Todos los módulos' },
  { id: 'balance',      label: 'Balance Integrado' },
  { id: 'materiales',   label: 'Tablero de Materiales' },
  { id: 'verificador',  label: 'Control Mensual' },
  { id: 'devoluciones', label: 'Registro de Devoluciones' },
  { id: 'liquidador',   label: 'Liquidador Automático' },
  { id: 'centro-mo',    label: 'Centro MO' },
]
const MOD_LABEL = Object.fromEntries(MODULOS.map((m) => [m.id, m.label]))

function fecha(iso) {
  try { return new Date(iso).toLocaleString('es-CO', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' }) }
  catch { return iso }
}
function bytes(n) {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
function estadoCls(e) {
  if (!e) return 'bg-gray-100 text-gray-600'
  if (e === 'ok' || /^2|^3/.test(e)) return 'bg-green-100 text-green-700'
  if (/^4/.test(e)) return 'bg-amber-100 text-amber-700'
  return 'bg-red-100 text-red-700'
}

function Fila({ r }) {
  const [open, setOpen] = useState(false)
  const tieneDetalle = (r.detalle && Object.keys(r.detalle).length) || r.archivo_hash || r.archivo_copia
  return (
    <>
      <tr className="border-t border-gray-100 hover:bg-blue-50/40 cursor-pointer" onClick={() => tieneDetalle && setOpen(!open)}>
        <td className="px-3 py-2 text-gray-400 w-6">{tieneDetalle ? (open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />) : null}</td>
        <td className="px-3 py-2 whitespace-nowrap text-gray-600">{fecha(r.ts)}</td>
        <td className="px-3 py-2 whitespace-nowrap"><span className="text-xs font-semibold text-blue-800 bg-blue-50 px-2 py-0.5 rounded-full">{MOD_LABEL[r.modulo] || r.modulo}</span></td>
        <td className="px-3 py-2 text-gray-800">{r.usuario || <span className="text-gray-400">—</span>}</td>
        <td className="px-3 py-2 font-medium text-gray-800">{r.accion}</td>
        <td className="px-3 py-2 text-gray-700 max-w-[280px] truncate" title={r.archivo || ''}>
          {r.archivo ? (<span className="inline-flex items-center gap-1">{r.accion === 'descargar' ? <Download className="w-3.5 h-3.5 text-gray-400" /> : <FileText className="w-3.5 h-3.5 text-gray-400" />}{r.archivo}{r.archivo_bytes ? <span className="text-gray-400 text-xs"> · {bytes(r.archivo_bytes)}</span> : null}</span>) : null}
        </td>
        <td className="px-3 py-2"><span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${estadoCls(r.estado)}`}>{r.estado || '—'}</span></td>
        <td className="px-3 py-2 text-gray-500 text-xs whitespace-nowrap">{r.duracion_ms != null ? `${(r.duracion_ms / 1000).toFixed(1)} s` : ''}</td>
      </tr>
      {open && (
        <tr className="bg-gray-50"><td></td><td colSpan={7} className="px-3 py-2 text-xs text-gray-700">
          <div className="grid gap-1 sm:grid-cols-2">
            <div><span className="text-gray-500">Ruta:</span> <code>{r.metodo} {r.ruta}</code></div>
            {r.ip && <div><span className="text-gray-500">IP:</span> {r.ip}</div>}
            {r.sesion && <div><span className="text-gray-500">Sesión del módulo:</span> <code>{r.sesion}</code></div>}
            {r.archivo_hash && <div className="sm:col-span-2"><span className="text-gray-500">SHA-256:</span> <code className="break-all">{r.archivo_hash}</code></div>}
            {r.archivo_copia && <div className="sm:col-span-2"><span className="text-gray-500">Copia guardada:</span> <code className="break-all">{r.archivo_copia}</code></div>}
          </div>
          {r.detalle && Object.keys(r.detalle).length > 0 && (
            <pre className="mt-2 bg-white border border-gray-200 rounded p-2 overflow-auto max-h-64">{JSON.stringify(r.detalle, null, 2)}</pre>
          )}
        </td></tr>
      )}
    </>
  )
}

export default function RegistrosPage() {
  const [modulo, setModulo] = useState('')
  const [usuario, setUsuario] = useState('')
  const [q, setQ] = useState('')
  const [desde, setDesde] = useState('')
  const [hasta, setHasta] = useState('')
  const params = { modulo: modulo || undefined, usuario: usuario || undefined, q: q || undefined,
    desde: desde ? `${desde}T00:00:00` : undefined, hasta: hasta ? `${hasta}T23:59:59` : undefined, limit: 300 }
  const { data, isFetching, refetch } = useQuery({ queryKey: ['sgo7-registros', params], queryFn: async () => (await api.get('/sgo7/registros', { params })).data })
  const { data: resumen } = useQuery({ queryKey: ['sgo7-registros-resumen'], queryFn: async () => (await api.get('/sgo7/registros/resumen')).data })
  const filas = data || []
  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex items-center gap-3 mb-1">
        <ClipboardList className="w-6 h-6 text-blue-700" />
        <h1 className="text-2xl font-bold text-gray-800">Registros SGO7</h1>
        <button onClick={() => refetch()} className="ml-auto text-sm text-blue-700 hover:underline flex items-center gap-1"><RefreshCw className={`w-4 h-4 ${isFetching ? 'animate-spin' : ''}`} /> Actualizar</button>
      </div>
      <p className="text-sm text-gray-500 mb-4">Todo archivo subido, proceso ejecutado o descarga generada en los módulos, con quién lo hizo y cuándo. Las copias de los archivos quedan en el servidor.</p>
      {resumen?.modulos?.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5 mb-4">
          {resumen.modulos.map((m) => (
            <div key={m.modulo} className="bg-white border border-gray-200 rounded-lg p-3">
              <div className="text-xs font-semibold text-blue-800">{MOD_LABEL[m.modulo] || m.modulo}</div>
              <div className="text-2xl font-bold text-gray-800">{m.total}</div>
              <div className="text-xs text-gray-500">{m.ultimos_7d} en 7 días · último {m.ultimo ? fecha(m.ultimo) : '—'}</div>
            </div>
          ))}
        </div>
      )}
      <div className="flex flex-wrap gap-2 mb-3 items-end">
        <select value={modulo} onChange={(e) => setModulo(e.target.value)} className="border border-gray-300 rounded px-2 py-1.5 text-sm bg-white">
          {MODULOS.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
        </select>
        <input value={usuario} onChange={(e) => setUsuario(e.target.value)} placeholder="Usuario" className="border border-gray-300 rounded px-2 py-1.5 text-sm w-44" />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Archivo o ruta" className="border border-gray-300 rounded px-2 py-1.5 text-sm w-52" />
        <label className="text-xs text-gray-500">Desde <input type="date" value={desde} onChange={(e) => setDesde(e.target.value)} className="border border-gray-300 rounded px-2 py-1 text-sm ml-1" /></label>
        <label className="text-xs text-gray-500">Hasta <input type="date" value={hasta} onChange={(e) => setHasta(e.target.value)} className="border border-gray-300 rounded px-2 py-1 text-sm ml-1" /></label>
        <span className="text-xs text-gray-400 ml-auto">{filas.length} registros{filas.length >= 300 ? ' (máx. 300; afina el filtro)' : ''}</span>
      </div>
      <div className="bg-white border border-gray-200 rounded-xl overflow-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500">
            <tr><th className="w-6"></th><th className="text-left px-3 py-2">Fecha</th><th className="text-left px-3 py-2">Módulo</th><th className="text-left px-3 py-2">Usuario</th><th className="text-left px-3 py-2">Acción</th><th className="text-left px-3 py-2">Archivo</th><th className="text-left px-3 py-2">Estado</th><th className="text-left px-3 py-2">Duración</th></tr>
          </thead>
          <tbody>
            {filas.map((r) => <Fila key={r.id} r={r} />)}
            {filas.length === 0 && <tr><td colSpan={8} className="px-3 py-8 text-center text-gray-400">Sin registros con ese filtro.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
