import { useState, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../utils/api'
import toast from 'react-hot-toast'
import { useAuthStore } from '../store/authStore'
import { esSoloLectura } from '../config/unidades'
import { Upload, Search, X, Eye, Trash2, Package, Layers, Plus, Pencil } from 'lucide-react'
import BolsasGrid from '../components/grid/BolsasGrid'
import BolsasReport from '../components/bolsas/BolsasReport'

const TIPOS = ['TUTELA', 'PQR', 'BOCATOMA', 'IPPE', 'CUN100', 'CUNBT', 'AMP', 'NNDD', 'TLC', 'DRONE', 'BP']

const fmtFecha = (v) => {
  if (!v) return '—'
  const [y, m, d] = String(v).slice(0, 10).split('-')
  return (y && m && d) ? `${d}/${m}/${y}` : String(v)
}

const TIPO_COLORS = {
  TUTELA: 'bg-red-100 text-red-800', PQR: 'bg-orange-100 text-orange-800',
  BOCATOMA: 'bg-cyan-100 text-cyan-800', IPPE: 'bg-blue-100 text-blue-800',
  CUN100: 'bg-indigo-100 text-indigo-800', CUNBT: 'bg-violet-100 text-violet-800',
  AMP: 'bg-green-100 text-green-800', NNDD: 'bg-teal-100 text-teal-800',
  TLC: 'bg-purple-100 text-purple-800', DRONE: 'bg-sky-100 text-sky-800',
  BP: 'bg-pink-100 text-pink-800',
}
const estadoColor = (e) => {
  const s = (e || '').toUpperCase()
  if (/EJECUT|SOLVED|CERR/.test(s)) return 'bg-green-100 text-green-700'
  if (/ANULAD|RECHAZ|CANCEL/.test(s)) return 'bg-red-100 text-red-700'
  if (/PROGRAM/.test(s)) return 'bg-blue-100 text-blue-700'
  if (/PTE|PENDIENT|OPEN|VISITA/.test(s)) return 'bg-yellow-100 text-yellow-700'
  if (/ENVIAD|COBRAR/.test(s)) return 'bg-orange-100 text-orange-700'
  return 'bg-gray-100 text-gray-600'
}

export default function BolsasPage() {
  const qc = useQueryClient()
  const { usuario } = useAuthStore()
  const fileRef = useRef()
  const [tipo, setTipo] = useState('')
  const [estado, setEstado] = useState('')
  const [q, setQ] = useState('')
  const [detalle, setDetalle] = useState(null)
  const [form, setForm] = useState(null)   // null=cerrado · {}=nueva · {...}=editar
  const [importing, setImporting] = useState(false)
  const [vista, setVista] = useState('listado')   // listado | reporte
  const readOnly = esSoloLectura(usuario?.rol)

  const { data: resumen } = useQuery({
    queryKey: ['bolsas-resumen'],
    queryFn: () => api.get('/bolsas/resumen').then((r) => r.data),
  })

  const { data, isLoading } = useQuery({
    // En vista por tipo el buscador filtra el grid en vivo (no re-consulta el server)
    queryKey: ['bolsas', tipo, estado, tipo ? '' : q],
    queryFn: () => {
      const params = { limit: 3000 }
      if (tipo) params.tipo = tipo
      if (estado) params.estado = estado
      if (q && !tipo) params.q = q
      return api.get('/bolsas', { params }).then((r) => r.data)
    },
  })

  const delMut = useMutation({
    mutationFn: (id) => api.delete(`/bolsas/${id}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['bolsas'] }); qc.invalidateQueries({ queryKey: ['bolsas-resumen'] }); toast.success('Eliminado') },
  })

  const onImport = async (e) => {
    const f = e.target.files?.[0]
    if (!f) return
    if (!confirm('Importar este Excel reemplazará TODAS las bolsas actuales. ¿Continuar?')) { e.target.value = ''; return }
    setImporting(true)
    try {
      const form = new FormData()
      form.append('file', f)
      form.append('reemplazar', 'true')
      const { data } = await api.post('/bolsas/import', form, { headers: { 'Content-Type': 'multipart/form-data' } })
      toast.success(`${data.inserted} bolsas importadas`)
      qc.invalidateQueries({ queryKey: ['bolsas'] })
      qc.invalidateQueries({ queryKey: ['bolsas-resumen'] })
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Error al importar')
    } finally {
      setImporting(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  // Columnas originales del tipo seleccionado (cada hoja conserva su estructura)
  const { data: camposData } = useQuery({
    queryKey: ['bolsa-campos', tipo],
    queryFn: () => api.get('/bolsas/campos', { params: { tipo } }).then((r) => r.data),
    enabled: !!tipo,
  })
  const campos = camposData?.campos || []

  const cellVal = (b, c) => {
    const v = b.datos_extra?.[c]
    if (v == null || v === '') return ''
    return /fecha/i.test(c) ? fmtFecha(v) : String(v)
  }

  const items = data?.items || []
  const tipos = resumen?.por_tipo || []
  const estados = resumen?.por_estado || []

  return (
    <div className="p-4 sm:p-6 space-y-4">
      {/* Header */}
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-lg font-bold text-gray-800 flex items-center gap-2">
            <Package size={20} className="text-blue-700" /> Bolsas
          </h1>
          <p className="text-sm text-gray-500">
            Casos por tipo · {resumen?.total ?? 0} registros
          </p>
        </div>
        {!readOnly && (
          <div className="flex gap-2">
            <input ref={fileRef} type="file" accept=".xlsx,.xls" className="hidden" onChange={onImport} />
            <button onClick={() => fileRef.current?.click()} disabled={importing}
              className="flex items-center gap-1.5 text-xs border border-gray-300 text-gray-600 px-3 py-2 rounded-lg hover:bg-gray-50 disabled:opacity-60">
              <Upload size={14} /> {importing ? 'Importando…' : 'Importar Excel'}
            </button>
            <button onClick={() => setForm({})}
              className="flex items-center gap-1.5 text-xs bg-blue-700 text-white px-3 py-2 rounded-lg hover:bg-blue-800">
              <Plus size={14} /> Nueva bolsa
            </button>
          </div>
        )}
      </div>

      {/* Toggle de vista */}
      <div className="flex gap-1 bg-gray-100 p-0.5 rounded-lg w-fit">
        {[['listado', 'Listado'], ['reporte', 'Reporte gráfico']].map(([v, l]) => (
          <button key={v} onClick={() => setVista(v)}
            className={`px-3 py-1.5 text-xs rounded-md transition ${vista === v ? 'bg-white text-blue-700 font-semibold shadow-sm' : 'text-gray-500 hover:text-gray-800'}`}>
            {l}
          </button>
        ))}
      </div>

      {vista === 'reporte' && <BolsasReport />}

      {vista === 'listado' && (<>
      {/* Resumen por tipo (chips clickeables) */}
      <div className="flex flex-wrap gap-1.5">
        <button onClick={() => setTipo('')}
          className={`px-2.5 py-1 rounded-full text-[11px] border ${!tipo ? 'bg-gray-800 text-white border-gray-800' : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'}`}>
          <Layers size={11} className="inline mr-1" /> Todos {resumen?.total ?? 0}
        </button>
        {tipos.map((t) => (
          <button key={t.tipo} onClick={() => setTipo(t.tipo === tipo ? '' : t.tipo)}
            className={`px-2.5 py-1 rounded-full text-[11px] border transition ${tipo === t.tipo ? 'ring-2 ring-blue-400 ' : ''} ${TIPO_COLORS[t.tipo] || 'bg-gray-100 text-gray-700'} border-transparent`}>
            <b>{t.tipo}</b> {t.total}
          </button>
        ))}
      </div>

      {/* Filtros */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[200px] max-w-sm">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={tipo ? `Buscar en cualquier columna de ${tipo}…` : 'Buscar caso, municipio, PDL, LCL…'}
            className="w-full pl-7 pr-7 py-1.5 text-xs border rounded-lg focus:outline-none focus:ring-1 focus:ring-blue-400" />
          {q && <button onClick={() => setQ('')} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400"><X size={12} /></button>}
        </div>
        <select value={estado} onChange={(e) => setEstado(e.target.value)} className="border rounded-lg px-2 py-1.5 text-xs text-gray-600">
          <option value="">Todos los estados</option>
          {estados.map((e) => <option key={e.estado} value={e.estado}>{e.estado} ({e.total})</option>)}
        </select>
        <span className="text-xs text-gray-500 ml-auto"><b className="text-gray-800">{items.length}</b> de {data?.total ?? 0}</span>
      </div>

      {/* Tabla — por tipo: AG Grid con columnas nativas + filtros estilo Excel; "Todos": resumen */}
      {tipo ? (
        <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
          {isLoading ? (
            <div className="p-10 text-center text-gray-400 text-sm">Cargando…</div>
          ) : (
            <BolsasGrid tipo={tipo} campos={campos} items={items} readOnly={readOnly} quickFilter={q} onVer={(b) => setDetalle(b)} />
          )}
        </div>
      ) : (
        <div className="bg-white rounded-xl border shadow-sm overflow-auto" style={{ maxHeight: '64vh' }}>
          <table className="w-full text-[11px]">
            <thead className="bg-gray-50 sticky top-0 z-10">
              <tr className="text-gray-600">
                {['Tipo', 'Zona', 'Caso', 'Municipio', 'Estado', 'PDL', 'F. Prog', 'Supervisor', ''].map((h, i) => (
                  <th key={i} className="text-left px-3 py-2 font-semibold whitespace-nowrap">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <tr><td colSpan={9} className="px-3 py-10 text-center text-gray-400">Cargando…</td></tr>
              ) : items.length === 0 ? (
                <tr><td colSpan={9} className="px-3 py-10 text-center text-gray-400">Sin registros. Usa "Importar Excel" o "Nueva bolsa".</td></tr>
              ) : items.map((b) => (
                <tr key={b.id} className="border-t hover:bg-blue-50/40">
                  <td className="px-3 py-1.5"><span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${TIPO_COLORS[b.tipo] || 'bg-gray-100 text-gray-700'}`}>{b.tipo}</span></td>
                  <td className="px-3 py-1.5 text-gray-600">{b.zona || '—'}</td>
                  <td className="px-3 py-1.5 font-medium text-gray-800 max-w-[180px] truncate" title={b.caso}>{b.caso || '—'}</td>
                  <td className="px-3 py-1.5 text-gray-600">{b.municipio || '—'}</td>
                  <td className="px-3 py-1.5"><span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${estadoColor(b.estado)}`}>{b.estado || '—'}</span></td>
                  <td className="px-3 py-1.5 font-mono text-[10px] text-gray-500 max-w-[120px] truncate" title={b.pdl}>{b.pdl || '—'}</td>
                  <td className="px-3 py-1.5 text-gray-600 whitespace-nowrap">{fmtFecha(b.fecha_prog)}</td>
                  <td className="px-3 py-1.5 text-gray-600">{b.supervisor || '—'}</td>
                  <td className="px-3 py-1.5 text-right whitespace-nowrap">
                    {!readOnly && <button onClick={() => setForm(b)} className="p-1 text-gray-400 hover:text-blue-600" title="Editar"><Pencil size={13} /></button>}
                    <button onClick={() => setDetalle(b)} className="p-1 text-gray-400 hover:text-blue-600" title="Ver todo"><Eye size={14} /></button>
                    {!readOnly && <button onClick={() => { if (confirm('¿Eliminar esta bolsa?')) delMut.mutate(b.id) }} className="p-1 text-gray-400 hover:text-red-600"><Trash2 size={13} /></button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      </>)}

      {detalle && <DetalleBolsa b={detalle} onClose={() => setDetalle(null)} />}
      {form !== null && <FormBolsa initial={form} tipoActual={tipo} onClose={() => setForm(null)} onSaved={() => setForm(null)} />}
    </div>
  )
}

// Formulario dinámico: los campos dependen del tipo (cada hoja conserva sus columnas)
function FormBolsa({ initial, tipoActual, onClose, onSaved }) {
  const qc = useQueryClient()
  const editing = !!initial?.id
  const [tipo, setTipo] = useState(initial?.tipo || tipoActual || 'PQR')
  const [vals, setVals] = useState(() => ({ ...(initial?.datos_extra || {}) }))

  const { data: camposData } = useQuery({
    queryKey: ['bolsa-campos', tipo],
    queryFn: () => api.get('/bolsas/campos', { params: { tipo } }).then((r) => r.data),
  })
  const campos = camposData?.campos || []
  const set = (c, v) => setVals((s) => ({ ...s, [c]: v }))
  const esFecha = (c) => /fecha/i.test(c)
  const esLargo = (c) => /observ|notas|nota|alcance|direccion|dirección/i.test(c)

  const save = useMutation({
    mutationFn: () => {
      const datos_extra = Object.fromEntries(Object.entries(vals).filter(([, v]) => v !== '' && v != null))
      const payload = { tipo, datos_extra }
      return editing ? api.patch(`/bolsas/${initial.id}`, payload) : api.post('/bolsas', payload)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bolsas'] })
      qc.invalidateQueries({ queryKey: ['bolsas-resumen'] })
      toast.success(editing ? 'Bolsa actualizada' : 'Bolsa creada')
      onSaved()
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'Error al guardar'),
  })

  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-3xl max-h-[92vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b shrink-0">
          <h2 className="font-semibold text-gray-800">{editing ? 'Editar bolsa' : 'Nueva bolsa'}</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
        </div>

        <div className="px-5 py-4 overflow-y-auto space-y-4">
          {/* Tipo (define las columnas) */}
          <div className="flex items-center gap-2">
            <label className="text-xs font-medium text-gray-600">Tipo de bolsa</label>
            <select value={tipo} disabled={editing} onChange={(e) => setTipo(e.target.value)}
              className={`border rounded-lg px-2 py-1.5 text-sm ${editing ? 'bg-gray-100 text-gray-500' : ''}`}>
              {TIPOS.map((t) => <option key={t}>{t}</option>)}
            </select>
            <span className="text-[11px] text-gray-400">{campos.length} campos · conserva el formato original de {tipo}</span>
          </div>

          {/* Campos nativos del tipo */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {campos.map((c) => (
              <div key={c} className={esLargo(c) ? 'sm:col-span-2' : ''}>
                <label className="block text-[11px] font-medium text-gray-500 mb-0.5">{c}</label>
                {esLargo(c) ? (
                  <textarea rows={2} value={vals[c] || ''} onChange={(e) => set(c, e.target.value)}
                    className="w-full border rounded-lg px-2 py-1.5 text-sm resize-none focus:outline-none focus:ring-1 focus:ring-blue-400" />
                ) : (
                  <input type={esFecha(c) ? 'date' : 'text'} value={vals[c] || ''} onChange={(e) => set(c, e.target.value)}
                    className="w-full border rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400" />
                )}
              </div>
            ))}
          </div>
        </div>

        <div className="flex justify-end gap-3 px-5 py-3 border-t bg-gray-50 rounded-b-xl shrink-0">
          <button onClick={onClose} className="px-4 py-2 text-sm border rounded-lg hover:bg-gray-100">Cancelar</button>
          <button onClick={() => save.mutate()} disabled={save.isPending}
            className="px-4 py-2 text-sm bg-blue-700 text-white rounded-lg hover:bg-blue-800 disabled:opacity-50">
            {save.isPending ? 'Guardando…' : 'Guardar'}
          </button>
        </div>
      </div>
    </div>
  )
}

// Popup con TODAS las columnas originales del tipo (datos_extra)
function DetalleBolsa({ b, onClose }) {
  const extra = b.datos_extra || {}
  const keys = Object.keys(extra)
  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-3xl max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b shrink-0">
          <div>
            <h2 className="font-semibold text-gray-800 text-sm flex items-center gap-2">
              <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${TIPO_COLORS[b.tipo] || 'bg-gray-100 text-gray-700'}`}>{b.tipo}</span>
              {b.caso || 'Bolsa'}
            </h2>
            <p className="text-xs text-gray-500">{[b.zona, b.municipio].filter(Boolean).join(' · ')}</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
        </div>
        <div className="px-5 py-4 overflow-y-auto">
          <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-2">Todos los campos del registro</p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1.5">
            {keys.length === 0 ? (
              <p className="text-sm text-gray-400">Sin datos adicionales.</p>
            ) : keys.map((k) => (
              <div key={k} className="flex justify-between gap-3 border-b border-gray-50 py-1">
                <span className="text-[11px] text-gray-500 shrink-0">{k}</span>
                <span className="text-[11px] text-gray-800 text-right break-words">{String(extra[k])}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
