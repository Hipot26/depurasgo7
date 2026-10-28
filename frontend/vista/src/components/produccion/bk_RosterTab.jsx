import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../utils/api'
import { formatCOP } from '../../utils/format'
import toast from 'react-hot-toast'
import { Plus, Pencil, Trash2, X, UserCheck, UserX, Lock } from 'lucide-react'

const CARGOS = ['Jefe de cuadrilla', 'Electricista experto', 'Electricista', 'Ayudante', 'Liniero', 'Conductor', 'Operario', 'Supervisor']
const VACIO = { zona: '', tipo_grupo: '', codigo_cuadrilla: '', cargo: '', nombre: '', estado: 'activo', salario: '', bono_mov: '', aux_transporte: '', no_facturable: false, causa_vacante: '', observacion: '' }
const inp = 'w-full border rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400'
const lbl = 'block text-[11px] font-medium text-gray-500 mb-0.5'

export default function RosterTab({ unidad, cfg, puedeEditar }) {
  const qc = useQueryClient()
  const [form, setForm] = useState(null)  // null = cerrado; objeto = editando/creando

  const { data: items = [] } = useQuery({
    queryKey: ['integrantes', unidad],
    queryFn: () => api.get('/produccion/integrantes', { params: { unidad } }).then((r) => r.data),
  })

  const saveMut = useMutation({
    mutationFn: (f) => {
      const payload = { ...f, unidad, salario: num(f.salario), bono_mov: num(f.bono_mov), aux_transporte: num(f.aux_transporte) }
      return f.id ? api.patch(`/produccion/integrantes/${f.id}`, payload) : api.post('/produccion/integrantes', payload)
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['integrantes', unidad] }); setForm(null); toast.success('Guardado') },
    onError: (e) => toast.error(e.response?.data?.detail || 'Error'),
  })
  const delMut = useMutation({
    mutationFn: (id) => api.delete(`/produccion/integrantes/${id}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['integrantes', unidad] }); toast.success('Eliminado') },
  })

  const num = (v) => (v === '' || v == null ? null : Number(v))
  const activos = items.filter((i) => i.estado === 'activo').length
  const vacantes = items.filter((i) => i.estado === 'vacante').length
  const costoMes = items.reduce((s, i) => s + (i.salario || 0) + (i.bono_mov || 0) + (i.aux_transporte || 0), 0)

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex flex-wrap gap-2">
          <Chip icon={UserCheck} label="Activos" value={activos} color="green" />
          <Chip icon={UserX} label="Vacantes" value={vacantes} color="red" />
          <Chip label="Integrantes" value={items.length} />
          <Chip label="Costo mensual" value={formatCOP(costoMes)} color="blue" />
        </div>
        {puedeEditar ? (
          <button onClick={() => setForm({ ...VACIO })} className="flex items-center gap-1.5 text-xs bg-blue-700 text-white px-3 py-2 rounded-lg hover:bg-blue-800">
            <Plus size={14} /> Agregar integrante
          </button>
        ) : <span className="flex items-center gap-1 text-[11px] text-gray-400"><Lock size={11} /> solo lectura</span>}
      </div>

      <div className="bg-white rounded-xl border shadow-sm overflow-auto">
        <table className="w-full text-[11px]">
          <thead className="bg-gray-50 text-gray-600">
            <tr>
              {['Zona', 'Tipo', 'Cuadrilla', 'Cargo', 'Nombre', 'Estado', 'Salario', 'Bono', 'Aux', 'NoFact', ''].map((h, i) => (
                <th key={i} className={`px-2 py-2 font-semibold ${['Salario', 'Bono', 'Aux'].includes(h) ? 'text-right' : 'text-left'}`}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {items.length === 0 ? (
              <tr><td colSpan={11} className="px-3 py-8 text-center text-gray-400">Aún no hay integrantes. {puedeEditar && 'Usa "Agregar integrante" para empezar a rellenar.'}</td></tr>
            ) : items.map((it) => (
              <tr key={it.id} className="border-t hover:bg-gray-50">
                <td className="px-2 py-1.5">{it.zona || '—'}</td>
                <td className="px-2 py-1.5">{it.tipo_grupo || '—'}</td>
                <td className="px-2 py-1.5 font-medium text-gray-700">{it.codigo_cuadrilla || '—'}</td>
                <td className="px-2 py-1.5">{it.cargo || '—'}</td>
                <td className="px-2 py-1.5 font-medium text-gray-800">{it.nombre || (it.estado === 'vacante' ? <span className="text-red-500">VACANTE</span> : '—')}</td>
                <td className="px-2 py-1.5">
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${it.estado === 'activo' ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>{it.estado}</span>
                </td>
                <td className="px-2 py-1.5 text-right text-gray-600">{it.salario ? formatCOP(it.salario) : '—'}</td>
                <td className="px-2 py-1.5 text-right text-gray-500">{it.bono_mov ? formatCOP(it.bono_mov) : '—'}</td>
                <td className="px-2 py-1.5 text-right text-gray-500">{it.aux_transporte ? formatCOP(it.aux_transporte) : '—'}</td>
                <td className="px-2 py-1.5 text-center">{it.no_facturable ? '✓' : ''}</td>
                <td className="px-2 py-1.5 text-right whitespace-nowrap">
                  {puedeEditar && <>
                    <button onClick={() => setForm({ ...it, salario: it.salario ?? '', bono_mov: it.bono_mov ?? '', aux_transporte: it.aux_transporte ?? '' })} className="text-gray-400 hover:text-blue-600 px-1"><Pencil size={13} /></button>
                    <button onClick={() => { if (confirm('¿Eliminar este integrante?')) delMut.mutate(it.id) }} className="text-gray-400 hover:text-red-600 px-1"><Trash2 size={13} /></button>
                  </>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {form && (
        <FormModal form={form} setForm={setForm} cfg={cfg} onSave={() => saveMut.mutate(form)} saving={saveMut.isPending} />
      )}
    </div>
  )
}

function FormModal({ form, setForm, cfg, onSave, saving }) {
  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-2xl max-h-[92vh] flex flex-col">
        <div className="flex items-center justify-between px-5 py-3 border-b">
          <h2 className="font-semibold text-gray-800">{form.id ? 'Editar integrante' : 'Nuevo integrante'}</h2>
          <button onClick={() => setForm(null)}><X size={18} /></button>
        </div>
        <div className="px-5 py-4 grid grid-cols-2 sm:grid-cols-3 gap-3 overflow-y-auto">
          <div><label className={lbl}>Zona</label>
            <select value={form.zona || ''} onChange={(e) => set('zona', e.target.value)} className={inp}>
              <option value="">—</option>{cfg.zonas.map((z) => <option key={z}>{z}</option>)}
            </select>
          </div>
          <div><label className={lbl}>Tipo</label>
            <select value={form.tipo_grupo || ''} onChange={(e) => set('tipo_grupo', e.target.value)} className={inp}>
              <option value="">—</option>{cfg.tipoOptions.filter(Boolean).map((t) => <option key={t}>{t}</option>)}
            </select>
          </div>
          <div><label className={lbl}>Cuadrilla (código)</label>
            <input value={form.codigo_cuadrilla || ''} onChange={(e) => set('codigo_cuadrilla', e.target.value)} placeholder="ej. MOE2_1" className={inp} />
          </div>
          <div><label className={lbl}>Cargo</label>
            <input list="cargos-list" value={form.cargo || ''} onChange={(e) => set('cargo', e.target.value)} className={inp} />
            <datalist id="cargos-list">{CARGOS.map((c) => <option key={c} value={c} />)}</datalist>
          </div>
          <div className="col-span-2"><label className={lbl}>Nombre</label>
            <input value={form.nombre || ''} onChange={(e) => set('nombre', e.target.value)} className={inp} />
          </div>
          <div><label className={lbl}>Estado</label>
            <select value={form.estado} onChange={(e) => set('estado', e.target.value)} className={inp}>
              <option value="activo">Activo</option><option value="vacante">Vacante</option>
            </select>
          </div>
          <div><label className={lbl}>Salario</label>
            <input type="number" value={form.salario} onChange={(e) => set('salario', e.target.value)} className={inp} />
          </div>
          <div><label className={lbl}>Bono mov.</label>
            <input type="number" value={form.bono_mov} onChange={(e) => set('bono_mov', e.target.value)} className={inp} />
          </div>
          <div><label className={lbl}>Aux. transporte</label>
            <input type="number" value={form.aux_transporte} onChange={(e) => set('aux_transporte', e.target.value)} className={inp} />
          </div>
          <div className="flex items-end pb-2">
            <label className="flex items-center gap-2 text-sm text-gray-600">
              <input type="checkbox" checked={!!form.no_facturable} onChange={(e) => set('no_facturable', e.target.checked)} /> No facturable
            </label>
          </div>
          {form.estado === 'vacante' && (
            <div className="col-span-2 sm:col-span-3"><label className={lbl}>Causa de la vacante</label>
              <input value={form.causa_vacante || ''} onChange={(e) => set('causa_vacante', e.target.value)} className={inp} />
            </div>
          )}
          <div className="col-span-2 sm:col-span-3"><label className={lbl}>Observación</label>
            <input value={form.observacion || ''} onChange={(e) => set('observacion', e.target.value)} className={inp} />
          </div>
        </div>
        <div className="flex justify-end gap-3 px-5 py-3 border-t bg-gray-50 rounded-b-xl">
          <button onClick={() => setForm(null)} className="px-4 py-2 text-sm border rounded-lg hover:bg-gray-100">Cancelar</button>
          <button onClick={onSave} disabled={saving} className="px-4 py-2 text-sm bg-blue-700 text-white rounded-lg hover:bg-blue-800 disabled:opacity-50">
            {saving ? 'Guardando…' : 'Guardar'}
          </button>
        </div>
      </div>
    </div>
  )
}

function Chip({ icon: Icon, label, value, color = 'gray' }) {
  const c = { gray: 'text-gray-700', green: 'text-green-700', red: 'text-red-600', blue: 'text-blue-700' }
  return (
    <div className="bg-white rounded-lg border px-3 py-1.5 flex items-center gap-2">
      {Icon && <Icon size={14} className={c[color]} />}
      <span className="text-[11px] text-gray-500">{label}</span>
      <span className={`text-sm font-bold ${c[color]}`}>{value}</span>
    </div>
  )
}
