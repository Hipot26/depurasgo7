import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import { UserPlus, Save, Trash2, MapPin, AlertTriangle, Info } from 'lucide-react'
import api from '../../utils/api'

// Tab «Líderes»: gestiona el directorio de líderes/supervisores → zona de forma PROACTIVA, para
// que los PRÓXIMOS imports resuelvan la zona solos. Complementa el auto-aprendizaje de la grilla.
// Lo que se toca aquí NO modifica ninguna fila del ANS ya cargada — solo el directorio (futuro).

const ZONAS = ['GUAVIO', 'ALMEIDAS', 'UBATE', 'LV']
const ROLES = ['supervisor', 'lider', 'apoyo', 'otro']
const K = (...p) => ['ans', ...p]

export default function LideresTab({ apiBase, periodo, readOnly }) {
  const qc = useQueryClient()

  const dir = useQuery({
    queryKey: K('directorio'),
    queryFn: async () => (await api.get('/contactos', { baseURL: apiBase })).data,
  })
  const sinZona = useQuery({
    queryKey: K('sin-zona', periodo),
    queryFn: async () => (await api.get('/responsables-sin-zona', { baseURL: apiBase, params: { periodo } })).data,
  })

  const guardar = useMutation({
    mutationFn: async (payload) => (await api.post('/contactos', payload, { baseURL: apiBase })).data,
    onSuccess: (d) => {
      qc.invalidateQueries({ queryKey: K('directorio') })
      qc.invalidateQueries({ queryKey: K('sin-zona') })
      const n = d?.zonas_resueltas || 0
      toast.success(n
        ? `Guardado en el directorio. ${n} orden${n > 1 ? 'es' : ''} sin zona de los periodos abiertos ya la tiene${n > 1 ? 'n' : ''}.`
        : 'Guardado en el directorio. Aplica a los próximos imports.')
      if (n) qc.invalidateQueries()
    },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo guardar'),
  })

  const borrar = useMutation({
    mutationFn: async (id) => (await api.delete(`/contactos/${id}`, { baseURL: apiBase })).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: K('directorio') }),
  })

  const filas = dir.data || []

  return (
    <div className="space-y-4">
      {/* Regla de oro del directorio */}
      <div className="flex items-start gap-2 bg-sky-50 border border-sky-200 rounded-lg p-3 text-xs text-sky-900">
        <Info size={16} className="text-sky-600 shrink-0 mt-0.5" />
        <p>
          Lo que definas aquí se usa para deducir la <b>zona</b> a partir del <b>responsable</b> en los
          <b> próximos imports</b>. <b>No modifica lo ya cargado</b>: los meses anteriores quedan tal como
          se reportaron. Para corregir una fila vieja, edítala directamente en la pestaña Registros.
        </p>
      </div>

      {/* Responsables que aparecen en el ANS sin zona → asignar rápido */}
      {sinZona.data?.responsables?.length > 0 && (
        <div className="border border-amber-200 rounded-lg overflow-hidden">
          <div className="bg-amber-50 px-3 py-2 flex items-center gap-2 text-xs font-semibold text-amber-800">
            <AlertTriangle size={14} /> Responsables sin zona ({sinZona.data.total_filas} fila{sinZona.data.total_filas !== 1 ? 's' : ''})
            <span className="font-normal text-amber-600">— asígnales su zona para futuros imports</span>
          </div>
          <div className="divide-y">
            {sinZona.data.responsables.map((r) => (
              <div key={r.nombre} className="flex items-center gap-2 px-3 py-1.5 text-xs">
                <span className="flex-1 text-gray-800">{r.nombre}
                  <span className="ml-1 text-gray-400">· {r.filas} fila{r.filas !== 1 ? 's' : ''}</span>
                </span>
                {!readOnly && ZONAS.map((z) => (
                  <button key={z} onClick={() => guardar.mutate({ nombre: r.nombre, rol: 'supervisor', zona: z })}
                    className="px-1.5 py-0.5 rounded border border-gray-300 text-gray-600 hover:bg-teal-50 hover:border-teal-400 hover:text-teal-800">
                    {z}
                  </button>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Directorio completo, editable */}
      <div className="border rounded-lg overflow-hidden">
        <div className="bg-gray-50 px-3 py-2 flex items-center gap-2 text-xs font-semibold text-gray-700">
          <MapPin size={14} /> Directorio de líderes y supervisores
          <span className="font-normal text-gray-400">— la cédula ayuda a distinguir dos nombres parecidos</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="bg-gray-50 text-gray-500 text-left">
              <tr>
                <th className="px-3 py-1.5 font-medium">Nombre</th>
                <th className="px-3 py-1.5 font-medium">Cédula</th>
                <th className="px-3 py-1.5 font-medium">Rol</th>
                <th className="px-3 py-1.5 font-medium">Zona</th>
                <th className="px-3 py-1.5 font-medium">Teléfono</th>
                <th className="px-3 py-1.5"></th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {!readOnly && <FilaNueva onGuardar={(p) => guardar.mutate(p)} />}
              {filas.map((c) => (
                <FilaContacto key={c.id} c={c} readOnly={readOnly}
                  onGuardar={(p) => guardar.mutate({ nombre: c.nombre, ...p })}
                  onBorrar={() => borrar.mutate(c.id)} />
              ))}
              {filas.length === 0 && (
                <tr><td colSpan={6} className="px-3 py-4 text-center text-gray-400">
                  Directorio vacío. Agrega arriba, o asígnale zona a los responsables sin zona.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function FilaContacto({ c, onGuardar, onBorrar, readOnly }) {
  const [ed, setEd] = useState({ cedula: c.cedula || '', rol: c.rol || 'supervisor', zona: c.zona || '', telefono: c.telefono || '' })
  const cambiado = ed.cedula !== (c.cedula || '') || ed.rol !== (c.rol || 'supervisor') ||
    ed.zona !== (c.zona || '') || ed.telefono !== (c.telefono || '')
  const set = (k) => (e) => setEd((v) => ({ ...v, [k]: e.target.value }))
  return (
    <tr className="hover:bg-gray-50">
      <td className="px-3 py-1 text-gray-800">{c.nombre}</td>
      <td className="px-2 py-1"><input value={ed.cedula} onChange={set('cedula')} disabled={readOnly}
        placeholder="—" className="w-24 border border-gray-200 rounded px-1 py-0.5" /></td>
      <td className="px-2 py-1">
        <select value={ed.rol} onChange={set('rol')} disabled={readOnly} className="border border-gray-200 rounded px-1 py-0.5">
          {ROLES.map((r) => <option key={r}>{r}</option>)}
        </select>
      </td>
      <td className="px-2 py-1">
        <select value={ed.zona} onChange={set('zona')} disabled={readOnly} className="border border-gray-200 rounded px-1 py-0.5">
          <option value="">—</option>
          {ZONAS.map((z) => <option key={z}>{z}</option>)}
        </select>
      </td>
      <td className="px-2 py-1"><input value={ed.telefono} onChange={set('telefono')} disabled={readOnly}
        placeholder="—" className="w-28 border border-gray-200 rounded px-1 py-0.5" /></td>
      <td className="px-2 py-1 whitespace-nowrap">
        {!readOnly && (
          <>
            <button onClick={() => onGuardar(ed)} disabled={!cambiado}
              className="text-gray-300 hover:text-teal-700 disabled:opacity-30 mr-1" title="Guardar"><Save size={13} /></button>
            <button onClick={onBorrar} className="text-gray-300 hover:text-red-600" title="Quitar"><Trash2 size={13} /></button>
          </>
        )}
      </td>
    </tr>
  )
}

function FilaNueva({ onGuardar }) {
  const [n, setN] = useState({ nombre: '', cedula: '', rol: 'supervisor', zona: '', telefono: '' })
  const set = (k) => (e) => setN((v) => ({ ...v, [k]: e.target.value }))
  const puede = n.nombre.trim() && (n.zona || n.telefono.trim())
  const guardar = () => { onGuardar({ ...n }); setN({ nombre: '', cedula: '', rol: 'supervisor', zona: '', telefono: '' }) }
  return (
    <tr className="bg-teal-50/40">
      <td className="px-2 py-1"><input value={n.nombre} onChange={set('nombre')} placeholder="Nombre nuevo…"
        onKeyDown={(e) => { if (e.key === 'Enter' && puede) guardar() }}
        className="w-full border border-gray-300 rounded px-1 py-0.5" /></td>
      <td className="px-2 py-1"><input value={n.cedula} onChange={set('cedula')} placeholder="cédula"
        className="w-24 border border-gray-300 rounded px-1 py-0.5" /></td>
      <td className="px-2 py-1">
        <select value={n.rol} onChange={set('rol')} className="border border-gray-300 rounded px-1 py-0.5">
          {ROLES.map((r) => <option key={r}>{r}</option>)}
        </select>
      </td>
      <td className="px-2 py-1">
        <select value={n.zona} onChange={set('zona')} className="border border-gray-300 rounded px-1 py-0.5">
          <option value="">—</option>
          {ZONAS.map((z) => <option key={z}>{z}</option>)}
        </select>
      </td>
      <td className="px-2 py-1"><input value={n.telefono} onChange={set('telefono')} placeholder="teléfono"
        className="w-28 border border-gray-300 rounded px-1 py-0.5" /></td>
      <td className="px-2 py-1">
        <button onClick={guardar} disabled={!puede}
          className="flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded bg-teal-700 text-white hover:bg-teal-800 disabled:opacity-40">
          <UserPlus size={12} /> Agregar
        </button>
      </td>
    </tr>
  )
}
