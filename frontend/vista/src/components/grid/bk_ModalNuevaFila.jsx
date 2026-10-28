import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import api from '../../utils/api'
import toast from 'react-hot-toast'
import { X, Lock, Plus, Trash2, Users, ClipboardList } from 'lucide-react'
import { useAuthStore } from '../../store/authStore'
import { unidadCfg } from '../../config/unidades'

const TIPOS_PDT = ['', 'TP', 'PDL', 'DF']

const inp = 'w-full border rounded-lg px-2 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400'
const lbl = 'block text-xs font-medium text-gray-600 mb-1'

// Quita tildes y pasa a mayúsculas para comparar
const norm = (s) => String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toUpperCase()

// Buscador de líderes con dropdown propio: busca por nombre, tipo o zona.
// Es ayuda, no limitante — acepta cualquier texto libre.
function LiderInput({ value, onChange, onPick, catalogo, placeholder }) {
  const [open, setOpen] = useState(false)

  const q = norm(value)
  const matches = !q
    ? catalogo.slice(0, 8)
    : catalogo.filter((c) =>
        norm(c.nombre).includes(q) || norm(c.tipo_grupo).includes(q) || norm(c.zona).includes(q)
      ).slice(0, 8)

  return (
    <div className="relative flex-1">
      <input
        type="text"
        value={value}
        onChange={(e) => { onChange(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        className={inp}
        placeholder={placeholder}
        autoComplete="off"
      />
      {open && matches.length > 0 && (
        <div className="absolute z-20 left-0 right-0 top-full mt-1 bg-white border rounded-lg shadow-xl max-h-56 overflow-y-auto">
          {matches.map((c) => (
            <button
              key={c.id}
              type="button"
              onMouseDown={(e) => { e.preventDefault(); onPick(c); setOpen(false) }}
              className="w-full text-left px-3 py-1.5 hover:bg-blue-50 border-b last:border-b-0"
            >
              <span className="block text-xs font-medium text-gray-800 truncate">{c.nombre}</span>
              <span className="block text-[10px] text-gray-400">{[c.tipo_grupo, c.zona].filter(Boolean).join(' · ')}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export default function ModalNuevaFila({ zonaDefault, unidad, onClose, onCreated }) {
  const { usuario } = useAuthStore()
  const cfg = unidadCfg(unidad)
  const TIPOS = cfg.tipoOptions
  const zonaFija = usuario?.rol === 'ingeniero_zona' // zona bloqueada a la propia

  const [zona, setZona]   = useState(zonaDefault || cfg.zonas[0])
  const [fecha, setFecha] = useState(new Date().toISOString().split('T')[0])

  // Una fila nueva por líder
  const [lideres, setLideres] = useState([{ lider: '', tipo_grupo: TIPOS[0] }])

  // Datos comunes opcionales — se aplican a todas las filas creadas
  const [comunes, setComunes] = useState({
    proceso: '', subproceso: '', proyecto_antecedente: '', pms: '', circuito: '',
    lcl: '', tipo_pdt: '', municipio: '', vereda: '', alcance: '',
    gestor_cliente: '', supervisor: '',
  })
  const setC = (k, v) => setComunes((c) => ({ ...c, [k]: v }))

  // Catálogo de líderes de la unidad (ayuda, no limitante: acepta cualquier nombre)
  const { data: catalogo = [] } = useQuery({
    queryKey: ['lideres', unidad],
    queryFn: () => api.get('/catalogos/lideres', { params: { unidad } }).then((r) => r.data),
  })

  const setLider = (i, k, v) => {
    setLideres((ls) => {
      const next = ls.map((l, idx) => idx === i ? { ...l, [k]: v } : l)
      // Si el nombre coincide con el catálogo, autocompletar el tipo de grupo
      if (k === 'lider') {
        const match = catalogo.find((c) => c.nombre === v)
        if (match?.tipo_grupo && TIPOS.includes(match.tipo_grupo)) next[i].tipo_grupo = match.tipo_grupo
      }
      return next
    })
  }
  const addLider    = () => setLideres((ls) => [...ls, { lider: '', tipo_grupo: TIPOS[0] }])
  const removeLider = (i) => setLideres((ls) => ls.filter((_, idx) => idx !== i))

  const mutation = useMutation({
    mutationFn: async () => {
      const extras = Object.fromEntries(
        Object.entries(comunes).filter(([, v]) => v && String(v).trim() !== '')
      )
      const filas = lideres.filter((l) => l.lider.trim())
      await Promise.all(filas.map((l) =>
        api.post('/programaciones', { unidad, zona, fecha, lider: l.lider.trim(), tipo_grupo: l.tipo_grupo, ...extras })
      ))
      return filas.length
    },
    onSuccess: (n) => { toast.success(`${n} fila${n > 1 ? 's' : ''} creada${n > 1 ? 's' : ''}`); onCreated() },
    onError: (e) => toast.error(e.response?.data?.detail || 'Error al crear'),
  })

  const validos = lideres.filter((l) => l.lider.trim()).length

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-2xl max-h-[92vh] flex flex-col">
        <div className="flex items-center justify-between px-6 py-4 border-b shrink-0">
          <h2 className="font-semibold text-gray-800">Nueva programación</h2>
          <button onClick={onClose}><X size={18} /></button>
        </div>

        <div className="px-6 py-4 space-y-5 overflow-y-auto">
          {/* Zona + Fecha */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className={lbl}>
                Zona *{zonaFija && <Lock size={10} className="inline ml-1 text-gray-400" />}
              </label>
              {zonaFija ? (
                <div className="w-full border border-gray-200 rounded-lg px-2 py-2 text-sm bg-gray-50 text-gray-600">{zona}</div>
              ) : (
                <select value={zona} onChange={(e) => setZona(e.target.value)} className={inp}>
                  {cfg.zonas.map((z) => <option key={z}>{z}</option>)}
                </select>
              )}
            </div>
            <div>
              <label className={lbl}>Fecha *</label>
              <input type="date" value={fecha} onChange={(e) => setFecha(e.target.value)} className={inp} />
            </div>
          </div>

          {/* Líderes — cada uno crea una fila */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="flex items-center gap-1.5 text-xs font-semibold text-gray-700">
                <Users size={14} className="text-blue-700" />
                Líderes de cuadrilla * <span className="font-normal text-gray-400">(una fila por líder)</span>
              </label>
              <button onClick={addLider} className="flex items-center gap-1 text-xs text-blue-700 hover:text-blue-900 font-medium">
                <Plus size={13} /> Agregar líder
              </button>
            </div>

            <div className="space-y-2">
              {lideres.map((l, i) => (
                <div key={i} className="flex items-center gap-2">
                  <LiderInput
                    value={l.lider}
                    onChange={(v) => setLider(i, 'lider', v)}
                    onPick={(c) => {
                      setLideres((ls) => ls.map((x, idx) => idx === i
                        ? { ...x, lider: c.nombre, tipo_grupo: TIPOS.includes(c.tipo_grupo) ? c.tipo_grupo : x.tipo_grupo }
                        : x))
                    }}
                    catalogo={catalogo}
                    placeholder={`Líder ${i + 1} — escribe el nombre o busca`}
                  />
                  <select
                    value={l.tipo_grupo}
                    onChange={(e) => setLider(i, 'tipo_grupo', e.target.value)}
                    className="border rounded-lg px-2 py-2 text-sm w-28 shrink-0"
                  >
                    {TIPOS.map((t) => <option key={t}>{t}</option>)}
                  </select>
                  {lideres.length > 1 && (
                    <button onClick={() => removeLider(i)} className="text-gray-300 hover:text-red-500 shrink-0" title="Quitar">
                      <Trash2 size={15} />
                    </button>
                  )}
                </div>
              ))}
            </div>
          </div>

          {/* Datos comunes — opcionales, se copian a todas las filas */}
          <div>
            <label className="flex items-center gap-1.5 text-xs font-semibold text-gray-700 mb-2">
              <ClipboardList size={14} className="text-blue-700" />
              Datos comunes <span className="font-normal text-gray-400">(opcionales · se aplican a todas las filas)</span>
            </label>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              <div>
                <label className={lbl}>Proceso</label>
                <select value={comunes.proceso} onChange={(e) => setC('proceso', e.target.value)} className={inp}>
                  {cfg.procesoOptions.map((p) => <option key={p} value={p}>{p || '—'}</option>)}
                </select>
              </div>
              <div>
                <label className={lbl}>Subproceso</label>
                <select value={comunes.subproceso} onChange={(e) => setC('subproceso', e.target.value)} className={inp}>
                  {cfg.subprocesoOptions.map((p) => <option key={p} value={p}>{p || '—'}</option>)}
                </select>
              </div>
              <div>
                <label className={lbl}>Tipo PDT</label>
                <select value={comunes.tipo_pdt} onChange={(e) => setC('tipo_pdt', e.target.value)} className={inp}>
                  {TIPOS_PDT.map((p) => <option key={p} value={p}>{p || '—'}</option>)}
                </select>
              </div>
              <div className="col-span-2 sm:col-span-3">
                <label className={lbl}>Proyecto / Antecedente</label>
                <input value={comunes.proyecto_antecedente} onChange={(e) => setC('proyecto_antecedente', e.target.value)} className={inp} />
              </div>
              <div>
                <label className={lbl}>PMS</label>
                <input value={comunes.pms} onChange={(e) => setC('pms', e.target.value)} className={inp} />
              </div>
              <div>
                <label className={lbl}>Circuito</label>
                <input value={comunes.circuito} onChange={(e) => setC('circuito', e.target.value)} className={inp} />
              </div>
              <div>
                <label className={lbl}>LCL</label>
                <input value={comunes.lcl} onChange={(e) => setC('lcl', e.target.value)} className={inp} />
              </div>
              <div>
                <label className={lbl}>Municipio</label>
                <input value={comunes.municipio} onChange={(e) => setC('municipio', e.target.value)} className={inp} />
              </div>
              <div>
                <label className={lbl}>Vereda</label>
                <input value={comunes.vereda} onChange={(e) => setC('vereda', e.target.value)} className={inp} />
              </div>
              <div>
                <label className={lbl}>Gestor</label>
                <input value={comunes.gestor_cliente} onChange={(e) => setC('gestor_cliente', e.target.value)} className={inp} />
              </div>
              <div className="col-span-2">
                <label className={lbl}>Supervisor</label>
                <input value={comunes.supervisor} onChange={(e) => setC('supervisor', e.target.value)} className={inp} />
              </div>
              <div className="col-span-2 sm:col-span-3">
                <label className={lbl}>Alcance</label>
                <textarea rows={2} value={comunes.alcance} onChange={(e) => setC('alcance', e.target.value)} className={inp + ' resize-none'} />
              </div>
            </div>
          </div>
        </div>

        <div className="flex items-center justify-between gap-3 px-6 py-4 border-t bg-gray-50 rounded-b-xl shrink-0">
          <span className="text-xs text-gray-500">
            {validos > 0 ? <>Se crearán <b className="text-gray-800">{validos}</b> fila{validos > 1 ? 's' : ''}</> : 'Agrega al menos un líder'}
          </span>
          <div className="flex gap-3">
            <button onClick={onClose} className="px-4 py-2 text-sm border rounded-lg hover:bg-gray-100">Cancelar</button>
            <button
              onClick={() => mutation.mutate()}
              disabled={validos === 0 || mutation.isPending}
              className="px-4 py-2 text-sm bg-blue-700 text-white rounded-lg hover:bg-blue-800 disabled:opacity-50"
            >
              {mutation.isPending ? 'Guardando…' : validos > 1 ? `Crear ${validos} registros` : 'Crear registro'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
