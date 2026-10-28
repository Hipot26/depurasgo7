// Historial de cambios de la programación (2026-09-07) — mismo patrón que el HistorialModal
// de ANS: quién, cuándo, qué campo, antes → después, y botón Revertir para las ediciones.
// La fuente es la auditoría automática (GET /programaciones/historial), así que también
// aparecen filas creadas/borradas por importaciones, Mantilla, SGS y la mensualidad.
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import { History, X, Undo2, Search, Loader2 } from 'lucide-react'
import api from '../../utils/api'

const MESES = ['', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']
const ACCION = {
  crear:  ['Creada',  'bg-green-100 text-green-700'],
  editar: ['Editada', 'bg-blue-100 text-blue-700'],
  borrar: ['Borrada', 'bg-red-100 text-red-700'],
}
const ORIGEN = [
  ['/import/excel', 'Importación Excel'],
  ['/mantilla', 'Plantilla Mantilla'],
  ['/sgs', 'Relación SGS'],
  ['/generar-mes', 'Mensualidad'],
  ['/duplicar', 'Duplicar fila'],
  ['/revertir', 'Reversión'],
]

const fecha = (iso) => {
  try { return new Date(iso).toLocaleString('es-CO', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) } catch { return '' }
}
const val = (v) => (v === null || v === undefined || v === '' ? '∅' : String(v))
const origenDe = (c) => {
  if (c.resumen?.startsWith('Reversión')) return c.resumen
  const o = ORIGEN.find(([p]) => (c.ruta || '').includes(p))
  return o ? o[1] : ''
}

export default function HistorialProgramacionModal({
  unidad, unidadNombre, zona, mes, anio, registroId, filaSel, readOnly, onConfirmar, onClose,
}) {
  const qc = useQueryClient()
  const [q, setQ] = useState('')
  const [soloMes, setSoloMes] = useState(true)
  const [soloFila, setSoloFila] = useState(!!registroId)

  const params = { unidad, limite: 500 }
  if (soloFila && registroId) params.registro_id = registroId
  else {
    if (zona) params.zona = zona
    if (soloMes && mes && anio) { params.mes = mes; params.anio = anio }
  }
  if (q.trim()) params.q = q.trim()

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['prog-historial', params],
    queryFn: async () => (await api.get('/programaciones/historial', { params })).data,
    placeholderData: (prev) => prev,
  })
  const revertir = useMutation({
    mutationFn: async (id) => (await api.post(`/programaciones/historial/${id}/revertir`)).data,
    onSuccess: () => {
      toast.success('Cambio revertido')
      qc.invalidateQueries({ queryKey: ['programaciones'] })
      qc.invalidateQueries({ queryKey: ['prog-historial'] })
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo revertir'),
  })
  const onRevertir = (c) => {
    if (onConfirmar && !onConfirmar()) return
    if (window.confirm(`¿Devolver «${c.campo}» de la fila ${c.fila?.lider || ''} (${c.fila?.fecha || ''}) al valor anterior «${val(c.valor_anterior)}»?`))
      revertir.mutate(c.id)
  }

  const items = data?.items || []
  const alcance = soloFila && registroId
    ? `fila ${filaSel?.lider || registroId}${filaSel?.fecha ? ` · ${filaSel.fecha}` : ''}`
    : `${unidadNombre || unidad}${zona ? ` · ${zona}` : ''}${soloMes && mes && anio ? ` · ${MESES[mes]} ${anio}` : ' · todos los meses'}`

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-2xl max-w-6xl w-full max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-4 py-3 border-b gap-3">
          <div className="flex items-center gap-2 min-w-0">
            <History size={16} className="text-blue-700 shrink-0" />
            <h3 className="font-semibold text-sm text-gray-800 truncate">Historial de cambios · {alcance}</h3>
            {isFetching && <Loader2 size={13} className="animate-spin text-gray-400" />}
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
        </div>

        <div className="flex flex-wrap items-center gap-3 px-4 py-2 border-b bg-gray-50 text-xs">
          <div className="relative flex-1 min-w-[180px]">
            <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar por líder, usuario, campo o valor…"
              className="w-full pl-6 pr-2 py-1.5 border rounded-lg focus:outline-none focus:ring-1 focus:ring-blue-400" />
          </div>
          {registroId && (
            <label className="flex items-center gap-1.5 text-gray-700">
              <input type="checkbox" checked={soloFila} onChange={(e) => setSoloFila(e.target.checked)} />
              Solo la fila seleccionada
            </label>
          )}
          {!(soloFila && registroId) && mes && anio && (
            <label className="flex items-center gap-1.5 text-gray-700">
              <input type="checkbox" checked={soloMes} onChange={(e) => setSoloMes(e.target.checked)} />
              Solo {MESES[mes]} {anio}
            </label>
          )}
          <span className="text-gray-500">{data ? `${data.total} cambio${data.total === 1 ? '' : 's'}${data.total > items.length ? ` (mostrando ${items.length})` : ''}` : ''}</span>
        </div>

        <div className="overflow-auto p-3">
          {isLoading ? <p className="text-gray-400 text-sm p-4">Cargando…</p>
          : !items.length ? <p className="text-gray-400 text-sm p-4">Sin cambios registrados con estos filtros.</p>
          : (
            <table className="text-xs w-full">
              <thead className="bg-gray-50 sticky top-0">
                <tr className="text-left text-gray-600">
                  {['Fecha', 'Usuario', 'Fila (zona · día · líder)', 'Acción', 'Campo', 'Antes', '', 'Después', 'Origen', ''].map((h, i) => (
                    <th key={i} className="px-2 py-1.5 font-semibold whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((c) => {
                  const [txt, cls] = ACCION[c.accion] || [c.accion, 'bg-gray-100 text-gray-700']
                  const esRev = c.resumen?.startsWith('Reversión')
                  return (
                    <tr key={c.id} className="border-t hover:bg-gray-50 align-top">
                      <td className="px-2 py-1 whitespace-nowrap text-gray-500">{fecha(c.creado_en)}</td>
                      <td className="px-2 py-1 whitespace-nowrap max-w-[160px] truncate" title={c.usuario_rol || ''}>{c.usuario || '—'}</td>
                      <td className="px-2 py-1 whitespace-nowrap text-gray-700">
                        {c.fila?.zona || '—'} · {c.fila?.fecha || '—'} · <b>{c.fila?.lider || '—'}</b>
                        {c.fila && c.fila.existe === false && c.accion !== 'borrar' && <span className="ml-1 text-[10px] text-gray-400">(fila borrada)</span>}
                      </td>
                      <td className="px-2 py-1"><span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${cls}`}>{txt}</span></td>
                      <td className="px-2 py-1 whitespace-nowrap text-gray-700">{c.campo || (c.accion === 'crear' ? 'fila completa' : c.accion === 'borrar' ? 'fila completa' : '')}</td>
                      <td className="px-2 py-1 text-red-600 line-through max-w-[160px] truncate" title={val(c.valor_anterior)}>{c.accion === 'editar' ? val(c.valor_anterior) : ''}</td>
                      <td className="px-1 text-gray-400">{c.accion === 'editar' ? '→' : ''}</td>
                      <td className="px-2 py-1 text-green-700 max-w-[160px] truncate" title={val(c.valor_nuevo)}>{c.accion === 'editar' ? val(c.valor_nuevo) : ''}</td>
                      <td className="px-2 py-1 whitespace-nowrap">
                        {esRev
                          ? <span className="text-[10px] bg-purple-100 text-purple-700 px-1.5 rounded">{c.resumen}</span>
                          : <span className="text-[10px] text-gray-400">{origenDe(c)}</span>}
                      </td>
                      <td className="px-2 py-1 whitespace-nowrap">
                        {!readOnly && c.revertible && (
                          <button onClick={() => onRevertir(c)} disabled={revertir.isPending}
                            className="inline-flex items-center gap-1 text-blue-600 hover:text-blue-800 disabled:opacity-40">
                            <Undo2 size={13} /> Revertir
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}
