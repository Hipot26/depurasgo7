import { X, Info, FileDown, CheckCircle, AlertTriangle } from 'lucide-react'
import { formatCOP } from '../../utils/format'

// Celda de # PDT: muestra el texto y, si hay desglose Mantilla, un botón de detalle
export function PdtCell(p) {
  const v = p.value ?? ''
  if (!p.data?.detalle_mantilla) return v
  return (
    <span className="flex items-center gap-1 w-full">
      <span className="truncate flex-1">{v}</span>
      <button
        onMouseDown={(e) => { e.stopPropagation() }}
        onClick={(e) => { e.stopPropagation(); p.context?.onVerMantilla?.(p.data) }}
        title="Ver desglose de la plantilla"
        className="shrink-0 text-blue-600 hover:text-blue-800"
      >
        <Info size={13} />
      </button>
    </span>
  )
}

// Popup con el desglose por actividad de una celda # PDT
export function MantillaDetalleModal({ row, onClose }) {
  let det = []
  try { det = JSON.parse(row.detalle_mantilla || '[]') } catch { det = [] }
  const moReal = det.slice(0, 5).reduce((s, d) => s + (d.mdo || 0), 0)
  const inop = det.slice(5, 7).reduce((s, d) => s + (d.mdo || 0), 0)
  const f = row.fecha ? String(row.fecha).slice(0, 10).split('-').reverse().join('/') : ''

  return (
    <div className="fixed inset-0 z-[60] bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b">
          <div>
            <h2 className="font-semibold text-gray-800 text-sm">{row.lider} · {f}</h2>
            <p className="text-xs text-gray-500">Desglose de pisos (plantilla Mantilla)</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
        </div>
        <div className="px-5 py-4 space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="bg-blue-50 rounded-lg p-2.5 text-center">
              <p className="text-[10px] text-blue-600">MO REAL (5 operativas)</p>
              <p className="text-lg font-bold text-blue-700">{formatCOP(moReal)}</p>
            </div>
            <div className="bg-amber-50 rounded-lg p-2.5 text-center">
              <p className="text-[10px] text-amber-600">INOPERATIVIDAD (2)</p>
              <p className="text-lg font-bold text-amber-700">{formatCOP(inop)}</p>
            </div>
          </div>
          <table className="w-full text-xs">
            <thead className="bg-gray-50 text-gray-600">
              <tr>
                <th className="text-left px-2 py-1.5 font-semibold">Actividad</th>
                <th className="text-right px-2 py-1.5 font-semibold">Cant.</th>
                <th className="text-right px-2 py-1.5 font-semibold">MDO</th>
                <th className="text-left px-2 py-1.5 font-semibold">Ticket / ID</th>
              </tr>
            </thead>
            <tbody>
              {det.map((d, i) => (
                <tr key={i} className={`border-t ${i >= 5 ? 'bg-amber-50/40' : ''}`}>
                  <td className="px-2 py-1 text-gray-700">{d.actividad}</td>
                  <td className="px-2 py-1 text-right text-gray-600">{d.cantidad || ''}</td>
                  <td className="px-2 py-1 text-right text-gray-700">{d.mdo ? formatCOP(d.mdo) : '—'}</td>
                  <td className="px-2 py-1 text-gray-800">{d.ticket || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-[11px] text-gray-400">Las primeras 5 actividades suman a MO REAL; Inoperativo y Tiempo ocioso suman a INOPERATIVIDAD.</p>
        </div>
      </div>
    </div>
  )
}

// Modal de resultado de la importación + descarga del Excel de errores
export function MantillaResultadoModal({ res, onClose }) {
  const descargarInforme = () => {
    const bin = atob(res.informe_b64)
    const arr = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i)
    const blob = new Blob([arr], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = res.informe_nombre || 'mantilla_errores.xlsx'
    a.click()
  }
  return (
    <div className="fixed inset-0 z-[60] bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-md" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b">
          <h2 className="font-semibold text-gray-800 text-sm">Importación de plantilla Mantilla</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
        </div>
        <div className="px-5 py-4 space-y-3">
          <div className="flex items-center gap-2 text-green-700">
            <CheckCircle size={18} />
            <span className="text-sm"><b>{res.actualizadas}</b> cuadrilla-días actualizados (# PDT, MO Real, Inoperatividad).</span>
          </div>
          {res.sin_match > 0 ? (
            <>
              <div className="flex items-center gap-2 text-amber-700">
                <AlertTriangle size={18} />
                <span className="text-sm"><b>{res.sin_match}</b> sin cruce. Motivos:</span>
              </div>
              {/* Resumen por motivo */}
              <div className="space-y-1">
                {(res.motivos || []).map((m, i) => (
                  <div key={i} className="flex items-center justify-between text-[11px] bg-amber-50 border border-amber-200 rounded px-2 py-1">
                    <span className="text-amber-800">{m.motivo}</span>
                    <b className="text-amber-700 shrink-0 ml-2">{m.total}</b>
                  </div>
                ))}
              </div>
              {/* Detalle fila a fila con su motivo */}
              <div className="max-h-32 overflow-y-auto bg-gray-50 rounded-lg p-2 text-[11px] text-gray-600 space-y-0.5">
                {res.sin_match_detalle.map((s, i) => (
                  <div key={i}>
                    <b className="text-gray-700">{s.codigo}</b> · {s.fecha}
                    <span className="text-gray-400"> — {s.motivo}</span>
                  </div>
                ))}
              </div>
              <button onClick={descargarInforme} className="w-full flex items-center justify-center gap-2 bg-red-600 text-white py-2 rounded-lg text-sm hover:bg-red-700">
                <FileDown size={15} /> Descargar Excel con errores (rojo + motivo en comentario)
              </button>
            </>
          ) : (
            <p className="text-sm text-gray-500">Todo cruzó correctamente.</p>
          )}
        </div>
      </div>
    </div>
  )
}
