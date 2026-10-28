import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '../utils/api'
import toast from 'react-hot-toast'
import {
  History, Search, X, Download, ChevronRight, ChevronDown,
  Plus, Pencil, Trash2, Layers, List,
} from 'lucide-react'

const ACCIONES = { crear: 'Creó', editar: 'Editó', borrar: 'Borró' }
const ACCION_STYLE = {
  crear:  'bg-green-100 text-green-700',
  editar: 'bg-amber-100 text-amber-700',
  borrar: 'bg-red-100 text-red-700',
}
const ACCION_ICON = { crear: Plus, editar: Pencil, borrar: Trash2 }

const fmtTs = (v) => {
  if (!v) return '—'
  const d = new Date(v)
  return d.toLocaleString('es-CO', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}
const corta = (s, n = 60) => (s && String(s).length > n ? String(s).slice(0, n) + '…' : (s ?? ''))

function Chip({ accion }) {
  const Icon = ACCION_ICON[accion] || Pencil
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-semibold ${ACCION_STYLE[accion] || 'bg-gray-100 text-gray-700'}`}>
      <Icon size={12} /> {ACCIONES[accion] || accion}
    </span>
  )
}

// ── Fila de operación (agrupada por request_id), expandible al detalle ──────────
function OperacionRow({ op }) {
  const [open, setOpen] = useState(false)
  const { data: detalle } = useQuery({
    queryKey: ['audit-op', op.request_id],
    queryFn: async () => (await api.get(`/auditoria/operacion/${op.request_id}`)).data,
    enabled: open && !!op.request_id,
  })
  const masiva = op.filas > 1
  return (
    <>
      <tr className="border-b hover:bg-gray-50 cursor-pointer" onClick={() => setOpen((v) => !v)}>
        <td className="px-3 py-2 text-gray-400 w-6">{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</td>
        <td className="px-3 py-2 whitespace-nowrap text-gray-600">{fmtTs(op.inicio)}</td>
        <td className="px-3 py-2 font-medium">{op.usuario || '—'} <span className="text-gray-400 text-xs">({op.rol || '—'})</span></td>
        <td className="px-3 py-2">{op.modulo || '—'}</td>
        <td className="px-3 py-2"><Chip accion={op.accion} /></td>
        <td className="px-3 py-2">
          {masiva
            ? <span className="inline-flex items-center gap-1 text-blue-700 font-semibold"><Layers size={13} /> {op.filas} filas</span>
            : <span className="text-gray-500">1 registro</span>}
        </td>
        <td className="px-3 py-2 text-gray-400 text-xs truncate max-w-[180px]">{op.ruta}</td>
      </tr>
      {open && (
        <tr className="bg-gray-50/60">
          <td></td>
          <td colSpan={6} className="px-3 py-2">
            <div className="max-h-72 overflow-auto rounded border bg-white">
              <table className="w-full text-xs">
                <thead className="bg-gray-100 text-gray-500 sticky top-0">
                  <tr>
                    <th className="px-2 py-1 text-left">Registro</th>
                    <th className="px-2 py-1 text-left">Acción</th>
                    <th className="px-2 py-1 text-left">Campo</th>
                    <th className="px-2 py-1 text-left">Antes</th>
                    <th className="px-2 py-1 text-left">Después</th>
                  </tr>
                </thead>
                <tbody>
                  {(detalle || []).map((d) => (
                    <tr key={d.id} className="border-t">
                      <td className="px-2 py-1 text-gray-500">#{d.registro_id ?? '—'}</td>
                      <td className="px-2 py-1"><Chip accion={d.accion} /></td>
                      <td className="px-2 py-1 font-mono">{d.campo || (d.accion !== 'editar' ? '(registro)' : '—')}</td>
                      <td className="px-2 py-1 text-red-600" title={d.valor_anterior || d.datos || ''}>{corta(d.valor_anterior ?? (d.accion === 'borrar' ? '(ver snapshot)' : ''))}</td>
                      <td className="px-2 py-1 text-green-700" title={d.valor_nuevo || d.datos || ''}>{corta(d.valor_nuevo ?? (d.accion === 'crear' ? '(ver snapshot)' : ''))}</td>
                    </tr>
                  ))}
                  {detalle && detalle.length === 0 && (
                    <tr><td colSpan={5} className="px-2 py-3 text-center text-gray-400">Sin detalle</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

export default function AuditoriaPage() {
  const [vista, setVista] = useState('operaciones')   // 'operaciones' | 'detalle'
  const [usuarioId, setUsuarioId] = useState('')
  const [modulo, setModulo] = useState('')
  const [accion, setAccion] = useState('')
  const [desde, setDesde] = useState('')
  const [hasta, setHasta] = useState('')
  const [q, setQ] = useState('')

  const params = {
    ...(usuarioId && { usuario_id: usuarioId }),
    ...(modulo && { modulo }),
    ...(accion && { accion }),
    ...(desde && { desde }),
    ...(hasta && { hasta }),
    ...(q && { q }),
  }

  const { data: usuarios = [] } = useQuery({
    queryKey: ['audit-usuarios'],
    queryFn: async () => (await api.get('/auditoria/usuarios')).data,
  })
  const { data: modulos = [] } = useQuery({
    queryKey: ['audit-modulos'],
    queryFn: async () => (await api.get('/auditoria/modulos')).data,
  })
  const { data: ops = [], isFetching: fOps } = useQuery({
    queryKey: ['audit-ops', params],
    queryFn: async () => (await api.get('/auditoria/operaciones', { params })).data,
    enabled: vista === 'operaciones',
  })
  const { data: lista, isFetching: fLista } = useQuery({
    queryKey: ['audit-lista', params],
    queryFn: async () => (await api.get('/auditoria', { params: { ...params, limite: 500 } })).data,
    enabled: vista === 'detalle',
  })

  const limpiar = () => { setUsuarioId(''); setModulo(''); setAccion(''); setDesde(''); setHasta(''); setQ('') }

  const exportar = async () => {
    try {
      const res = await api.get('/auditoria/export', { params, responseType: 'blob' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([res.data]))
      a.download = `Auditoria_${new Date().toISOString().slice(0, 10)}.xlsx`
      a.click()
      URL.revokeObjectURL(a.href)
    } catch (e) {
      toast.error('No se pudo exportar')
    }
  }

  return (
    <div className="p-4 md:p-6 max-w-[1400px] mx-auto">
      <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <History className="text-blue-800" size={22} />
          <div>
            <h1 className="text-xl font-bold text-gray-800">Auditoría</h1>
            <p className="text-xs text-gray-500">Historial de toda acción de todos los usuarios — respaldo y trazabilidad</p>
          </div>
        </div>
        <button onClick={exportar} className="flex items-center gap-2 px-3 py-2 bg-blue-800 text-white rounded-lg text-sm hover:bg-blue-900">
          <Download size={15} /> Exportar respaldo
        </button>
      </div>

      {/* Filtros */}
      <div className="bg-white rounded-xl border p-3 mb-4 flex flex-wrap items-end gap-3">
        <label className="text-xs text-gray-500">Usuario
          <select value={usuarioId} onChange={(e) => setUsuarioId(e.target.value)} className="block mt-0.5 border rounded-lg px-2 py-1.5 text-sm min-w-[160px]">
            <option value="">Todos</option>
            {usuarios.map((u) => <option key={u.usuario_id} value={u.usuario_id}>{u.nombre} ({u.acciones})</option>)}
          </select>
        </label>
        <label className="text-xs text-gray-500">Módulo
          <select value={modulo} onChange={(e) => setModulo(e.target.value)} className="block mt-0.5 border rounded-lg px-2 py-1.5 text-sm min-w-[130px]">
            <option value="">Todos</option>
            {modulos.map((m) => <option key={m.modulo} value={m.modulo}>{m.modulo}</option>)}
          </select>
        </label>
        <label className="text-xs text-gray-500">Acción
          <select value={accion} onChange={(e) => setAccion(e.target.value)} className="block mt-0.5 border rounded-lg px-2 py-1.5 text-sm">
            <option value="">Todas</option>
            <option value="crear">Creó</option>
            <option value="editar">Editó</option>
            <option value="borrar">Borró</option>
          </select>
        </label>
        <label className="text-xs text-gray-500">Desde
          <input type="date" value={desde} onChange={(e) => setDesde(e.target.value)} className="block mt-0.5 border rounded-lg px-2 py-1.5 text-sm" />
        </label>
        <label className="text-xs text-gray-500">Hasta
          <input type="date" value={hasta} onChange={(e) => setHasta(e.target.value)} className="block mt-0.5 border rounded-lg px-2 py-1.5 text-sm" />
        </label>
        <label className="text-xs text-gray-500 flex-1 min-w-[160px]">Buscar
          <div className="relative mt-0.5">
            <Search size={14} className="absolute left-2 top-2.5 text-gray-400" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="campo o valor…" className="w-full border rounded-lg pl-7 pr-2 py-1.5 text-sm" />
          </div>
        </label>
        <button onClick={limpiar} className="flex items-center gap-1 px-2 py-1.5 text-gray-500 hover:text-gray-800 text-sm">
          <X size={14} /> Limpiar
        </button>
      </div>

      {/* Switch de vista */}
      <div className="flex gap-1 mb-3">
        <button onClick={() => setVista('operaciones')} className={`flex items-center gap-1 px-3 py-1.5 rounded-lg text-sm ${vista === 'operaciones' ? 'bg-blue-800 text-white' : 'bg-white border text-gray-600'}`}>
          <Layers size={14} /> Por operación
        </button>
        <button onClick={() => setVista('detalle')} className={`flex items-center gap-1 px-3 py-1.5 rounded-lg text-sm ${vista === 'detalle' ? 'bg-blue-800 text-white' : 'bg-white border text-gray-600'}`}>
          <List size={14} /> Detalle (cambio a cambio)
        </button>
      </div>

      {/* Vista por operación */}
      {vista === 'operaciones' && (
        <div className="bg-white rounded-xl border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-500 text-xs uppercase">
              <tr>
                <th className="px-3 py-2 w-6"></th>
                <th className="px-3 py-2 text-left">Fecha</th>
                <th className="px-3 py-2 text-left">Usuario</th>
                <th className="px-3 py-2 text-left">Módulo</th>
                <th className="px-3 py-2 text-left">Acción</th>
                <th className="px-3 py-2 text-left">Alcance</th>
                <th className="px-3 py-2 text-left">Ruta</th>
              </tr>
            </thead>
            <tbody>
              {ops.map((op) => <OperacionRow key={op.request_id} op={op} />)}
              {!fOps && ops.length === 0 && (
                <tr><td colSpan={7} className="px-3 py-8 text-center text-gray-400">Sin actividad registrada con esos filtros</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Vista detalle (plano) */}
      {vista === 'detalle' && (
        <div className="bg-white rounded-xl border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-500 text-xs uppercase">
              <tr>
                <th className="px-3 py-2 text-left">Fecha</th>
                <th className="px-3 py-2 text-left">Usuario</th>
                <th className="px-3 py-2 text-left">Módulo</th>
                <th className="px-3 py-2 text-left">Acción</th>
                <th className="px-3 py-2 text-left">Registro</th>
                <th className="px-3 py-2 text-left">Campo</th>
                <th className="px-3 py-2 text-left">Antes</th>
                <th className="px-3 py-2 text-left">Después</th>
              </tr>
            </thead>
            <tbody>
              {(lista?.items || []).map((d) => (
                <tr key={d.id} className="border-b hover:bg-gray-50">
                  <td className="px-3 py-2 whitespace-nowrap text-gray-600">{fmtTs(d.creado_en)}</td>
                  <td className="px-3 py-2">{d.usuario_nombre || '—'}</td>
                  <td className="px-3 py-2">{d.modulo || '—'}</td>
                  <td className="px-3 py-2"><Chip accion={d.accion} /></td>
                  <td className="px-3 py-2 text-gray-500">{d.tabla} #{d.registro_id ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-xs">{d.campo || (d.accion !== 'editar' ? '(registro)' : '—')}</td>
                  <td className="px-3 py-2 text-red-600" title={d.valor_anterior || d.datos || ''}>{corta(d.valor_anterior ?? (d.accion === 'borrar' ? '(snapshot)' : ''))}</td>
                  <td className="px-3 py-2 text-green-700" title={d.valor_nuevo || d.datos || ''}>{corta(d.valor_nuevo ?? (d.accion === 'crear' ? '(snapshot)' : ''))}</td>
                </tr>
              ))}
              {!fLista && lista && lista.items.length === 0 && (
                <tr><td colSpan={8} className="px-3 py-8 text-center text-gray-400">Sin cambios con esos filtros</td></tr>
              )}
            </tbody>
          </table>
          {lista && lista.total > lista.items.length && (
            <div className="px-3 py-2 text-xs text-gray-400 border-t">Mostrando {lista.items.length} de {lista.total}. Afina los filtros o exporta para ver todo.</div>
          )}
        </div>
      )}
    </div>
  )
}
