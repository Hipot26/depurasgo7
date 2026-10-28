import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../utils/api'
import toast from 'react-hot-toast'
import { Plus, X } from 'lucide-react'

const ROLES = ['supervisor','ingeniero_zona','programador','facturacion','consulta','directivo','cliente','cumplimentador','desarrollador']
const ZONAS = ['','ALMEIDAS','GUAVIO','UBATE','LV&TLC']
const UNIDADES = [['SABOGAL','Sabogal'],['CHOCONTA','Chocontá · No programados']]
const ROL_LABELS = {supervisor:'Jefe de Zona',ingeniero_zona:'Ingeniero Zona',programador:'Programador',facturacion:'Facturación',consulta:'Consulta (solo lectura)',directivo:'Directivo (ve todo)',cliente:'Cliente (solo Bolsas)',cumplimentador:'Cumplimentador (solo ANS)',desarrollador:'Desarrollador'}

export default function AdminPage() {
  const qc = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ nombre: '', email: '', password: '', rol: 'ingeniero_zona', zona: '', unidad: 'SABOGAL' })
  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))

  const { data: usuarios = [] } = useQuery({
    queryKey: ['usuarios'],
    queryFn: () => api.get('/auth/usuarios').then((r) => r.data),
  })

  const createMutation = useMutation({
    mutationFn: (data) => api.post('/auth/usuarios', data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['usuarios'] }); setShowForm(false); toast.success('Usuario creado') },
    onError: (e) => toast.error(e.response?.data?.detail || 'Error'),
  })

  const ROL_COLORS = {
    supervisor: 'bg-red-100 text-red-800',
    ingeniero_zona: 'bg-blue-100 text-blue-800',
    programador: 'bg-green-100 text-green-800',
    facturacion: 'bg-purple-100 text-purple-800',
    cumplimentador: 'bg-sky-100 text-sky-800',
  }

  return (
    <div className="p-6 max-w-3xl">
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Administración</h1>
          <p className="text-sm text-gray-500">Gestión de usuarios y permisos</p>
        </div>
        <button onClick={() => setShowForm(true)} className="flex items-center gap-2 bg-blue-700 text-white px-4 py-2 rounded-lg text-sm hover:bg-blue-800">
          <Plus size={15} /> Nuevo usuario
        </button>
      </div>

      {/* Tabla usuarios */}
      <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-blue-900 text-white text-xs">
            <tr>
              <th className="text-left px-4 py-3">Nombre</th>
              <th className="text-left px-4 py-3">Usuario</th>
              <th className="text-left px-4 py-3">Rol</th>
              <th className="text-left px-4 py-3">Unidad</th>
              <th className="text-left px-4 py-3">Zona</th>
              <th className="text-left px-4 py-3">Estado</th>
            </tr>
          </thead>
          <tbody>
            {usuarios.map((u) => (
              <tr key={u.id} className="border-b hover:bg-gray-50">
                <td className="px-4 py-2.5 font-medium text-gray-800">{u.nombre}</td>
                <td className="px-4 py-2.5 text-gray-500 text-xs">{u.email}</td>
                <td className="px-4 py-2.5">
                  <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${ROL_COLORS[u.rol] || ''}`}>{ROL_LABELS[u.rol] || u.rol}</span>
                </td>
                <td className="px-4 py-2.5 text-gray-600 text-xs">{u.unidad === 'CHOCONTA' ? 'Chocontá' : (u.unidad || 'Sabogal')}</td>
                <td className="px-4 py-2.5 text-gray-600 text-xs">{u.zona || '—'}</td>
                <td className="px-4 py-2.5">
                  <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${u.activo ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>{u.activo ? 'Activo' : 'Inactivo'}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Modal crear usuario */}
      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-xl shadow-2xl w-full max-w-md">
            <div className="flex items-center justify-between px-6 py-4 border-b">
              <h2 className="font-semibold">Nuevo usuario</h2>
              <button onClick={() => setShowForm(false)}><X size={18} /></button>
            </div>
            <div className="px-6 py-4 space-y-4">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Nombre completo</label>
                <input value={form.nombre} onChange={(e) => set('nombre', e.target.value)} className="w-full border rounded-lg px-3 py-2 text-sm" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Usuario (login)</label>
                <input type="text" value={form.email} onChange={(e) => set('email', e.target.value)} className="w-full border rounded-lg px-3 py-2 text-sm" placeholder="ej. almeidas" autoComplete="off" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Contraseña</label>
                <input type="password" value={form.password} onChange={(e) => set('password', e.target.value)} className="w-full border rounded-lg px-3 py-2 text-sm" />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Rol</label>
                  <select value={form.rol} onChange={(e) => set('rol', e.target.value)} className="w-full border rounded-lg px-2 py-2 text-sm">
                    {ROLES.map((r) => <option key={r} value={r}>{ROL_LABELS[r]}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Zona</label>
                  <select value={form.zona} onChange={(e) => set('zona', e.target.value)} className="w-full border rounded-lg px-2 py-2 text-sm">
                    {ZONAS.map((z) => <option key={z} value={z}>{z || 'Sin zona'}</option>)}
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Unidad operativa</label>
                <select value={form.unidad} onChange={(e) => set('unidad', e.target.value)} className="w-full border rounded-lg px-2 py-2 text-sm">
                  {UNIDADES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
                <p className="text-[11px] text-gray-400 mt-1">
                  {form.rol === 'directivo'
                    ? 'El directivo ve todas las unidades (la unidad solo marca su pertenencia).'
                    : 'El usuario solo verá los datos de esta unidad.'}
                </p>
              </div>
            </div>
            <div className="flex justify-end gap-3 px-6 py-4 border-t bg-gray-50 rounded-b-xl">
              <button onClick={() => setShowForm(false)} className="px-4 py-2 text-sm border rounded-lg hover:bg-gray-100">Cancelar</button>
              <button
                onClick={() => createMutation.mutate({ ...form, zona: form.zona || null, unidad: form.unidad || 'SABOGAL' })}
                disabled={!form.nombre || !form.email || !form.password || createMutation.isPending}
                className="px-4 py-2 text-sm bg-blue-700 text-white rounded-lg hover:bg-blue-800 disabled:opacity-50"
              >
                {createMutation.isPending ? 'Guardando...' : 'Crear usuario'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
