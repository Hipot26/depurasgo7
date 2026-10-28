// Órdenes SIN zona de los periodos abiertos (solo el desarrollador, 2026-09-29).
// Sin zona, el reporte manda la orden a UBATE y el 3.2 B no sabe a qué profesional de apoyo
// preguntarle (llegó a pedirse a la zona equivocada el correo de una orden). Aquí se ve,
// orden por orden, qué zona le daría el directorio de hoy o el CONSENSO de señales (nombre
// parecido, celular del supervisor, historial del líder, LCL/PDL en la programación), y se
// aplica con un clic. Solo se pone donde al menos dos señales coinciden y ninguna contradice.
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../utils/api'
import toast from 'react-hot-toast'
import { MapPin, X, Loader2, CheckCircle2 } from 'lucide-react'

const SENAL = {
  palabras: 'nombre parecido',
  celular: 'celular',
  lider: 'líder',
  lcl: 'LCL',
  pdl: 'PDL',
}

export default function ZonasFaltantes({ apiBase }) {
  const qc = useQueryClient()
  const [abierto, setAbierto] = useState(false)
  const q = useQuery({
    queryKey: ['zonas-sin-zona', apiBase],
    queryFn: async () => (await api.get('/zonas/sin-zona', { baseURL: apiBase })).data,
    staleTime: 60_000,
  })
  const filas = q.data?.filas || []
  const deducibles = filas.filter((f) => f.zona).length
  const aplicar = useMutation({
    mutationFn: async () => (await api.post('/zonas/deducir', {}, { baseURL: apiBase })).data,
    onSuccess: (d) => {
      const quedan = (d.filas?.length || 0) - (d.resueltas || 0)
      toast.success(`${d.resueltas} orden(es) con zona. ${quedan} siguen sin zona y van al Paso 1.`, { duration: 8000 })
      setAbierto(false)
      qc.invalidateQueries()
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo aplicar'),
  })

  if (!filas.length) return null
  return (
    <>
      <button onClick={() => setAbierto(true)}
        title="Órdenes sin zona de los periodos abiertos: el reporte las cuenta en UBATE y el 3.2 B no sabe a quién preguntar"
        className="flex items-center gap-2 px-3 py-2 text-sm bg-amber-50 border border-amber-300 text-amber-800 rounded-lg hover:bg-amber-100">
        <MapPin size={15} /> Sin zona · {filas.length}
      </button>
      {abierto && (
        <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={() => setAbierto(false)}>
          <div className="bg-white rounded-xl shadow-xl w-full max-w-5xl max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-4 px-5 py-4 border-b">
              <div>
                <h3 className="font-semibold">Órdenes sin zona · periodos {(q.data?.periodos || []).join(', ')}</h3>
                <p className="text-xs text-gray-600 mt-1">
                  Se pone la zona solo donde el directorio de hoy la sabe o donde <b>al menos dos señales coinciden y ninguna contradice</b>.
                  Las demás siguen sin zona y van al Paso 1: preguntar a programación.
                </p>
              </div>
              <button onClick={() => setAbierto(false)} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
            </div>
            <div className="overflow-auto px-5 py-3">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-gray-500 border-b">
                    <th className="py-1.5 pr-2">Orden</th>
                    <th className="py-1.5 pr-2">Supervisor en el CD10</th>
                    <th className="py-1.5 pr-2">Señales</th>
                    <th className="py-1.5">Zona</th>
                  </tr>
                </thead>
                <tbody>
                  {filas.map((f) => (
                    <tr key={f.reg_id} className="border-b last:border-0 align-top">
                      <td className="py-1.5 pr-2 font-mono whitespace-nowrap">
                        {f.pdl}
                        <div className="text-gray-400">{f.periodo}{f.se_ejecuto === 'NO' ? ' · anulada' : ''}</div>
                      </td>
                      <td className="py-1.5 pr-2">{f.supervisor || '—'}</td>
                      <td className="py-1.5 pr-2">
                        {f.metodo === 'directorio'
                          ? <span className="text-gray-600">el directorio ya lo conoce</span>
                          : Object.keys(f.votos || {}).length
                            ? <div className="flex flex-wrap gap-1">
                                {Object.entries(f.votos).map(([k, v]) => (
                                  <span key={k} title={v.por} className="px-1.5 py-0.5 rounded bg-gray-100 text-gray-700">
                                    {SENAL[k] || k}: <b>{v.zona}</b>
                                  </span>
                                ))}
                              </div>
                            : <span className="text-gray-400">ninguna señal</span>}
                      </td>
                      <td className="py-1.5 whitespace-nowrap">
                        {f.zona
                          ? <span className="px-2 py-0.5 rounded bg-emerald-100 text-emerald-800 font-semibold">{f.zona}</span>
                          : <span className="px-2 py-0.5 rounded bg-amber-100 text-amber-800">Paso 1</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-end gap-2 px-5 py-3 border-t">
              <button onClick={() => setAbierto(false)} className="px-3 py-2 text-sm border rounded-lg text-gray-600 hover:bg-gray-50">Cerrar</button>
              <button onClick={() => aplicar.mutate()} disabled={!deducibles || aplicar.isPending}
                className="flex items-center gap-2 px-4 py-2 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">
                {aplicar.isPending ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />}
                Poner la zona en {deducibles} orden(es)
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
