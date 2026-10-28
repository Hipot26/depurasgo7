import { useState, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../utils/api'
import toast from 'react-hot-toast'
import { Upload, FileDown, MapPin, History, X, AlertTriangle, Trash2, FileSpreadsheet } from 'lucide-react'
import ProgramadosGrid from '../grid/ProgramadosGrid'

const ZONAS = ['UBATE', 'ALMEIDAS', 'GUAVIO', 'LV']

export default function ProgramadosTab({ apiBase, periodo, readOnly }) {
  const base = `${apiBase}`.replace('/ans', '/programados')
  const qc = useQueryClient()
  const fileRef = useRef()
  const [verCargas, setVerCargas] = useState(false)
  const [verZonas, setVerZonas] = useState(false)
  const [hist, setHist] = useState(null)      // registro abierto en el panel de historial
  // El mes se maneja AQUÍ, no con el selector del ANS: el BASE CONTROL trae radicados de
  // muchos meses a la vez y filtrar por el mes del ANS dejaba la tabla "vacía" tras subir.
  // '' = todos los meses.
  const [mes, setMes] = useState('')
  const K = (...p) => ['programados', base, ...p]

  const { data, isLoading } = useQuery({
    queryKey: K('registros', mes),
    queryFn: async () => (await api.get('/registros', { baseURL: base, params: mes ? { periodo: mes } : {} })).data,
  })
  const { data: meses } = useQuery({
    queryKey: K('periodos'),
    queryFn: async () => (await api.get('/periodos', { baseURL: base })).data,
  })

  const subir = useMutation({
    mutationFn: async (file) => {
      const fd = new FormData()
      fd.append('file', file)
      return (await api.post('/cargar', fd, { baseURL: base })).data
    },
    onSuccess: (r) => {
      toast.success(`${r.filas_aplicadas} filas · ${r.celdas_llenas} celdas llenas`)
      if (r.municipios_sin_zona?.length) {
        toast(`${r.municipios_sin_zona.length} municipio(s) sin zona: ${r.municipios_sin_zona.join(', ')}`,
          { icon: '⚠', duration: 8000 })
      }
      setMes('')   // muestra TODO lo recién cargado, venga del mes que venga
      qc.invalidateQueries({ queryKey: K() })
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo leer el archivo', { duration: 10000 }),
  })

  const guardar = useMutation({
    mutationFn: async ({ id, campo, valor }) =>
      (await api.patch(`/registro/${id}`, { [campo]: valor }, { baseURL: base })).data,
    // La respuesta trae la fila ya recalculada (H por regla, M/N/O por fórmula): se inyecta
    // directo en la caché para que el grid refleje el cálculo al instante, sin refetch.
    onSuccess: (fila) => {
      qc.setQueriesData({ queryKey: K('registros') }, (old) =>
        old ? { ...old, items: old.items.map((i) => (i.id === fila.id ? fila : i)) } : old)
    },
    onError: (e) => {
      toast.error(e.response?.data?.detail || 'No se pudo guardar')
      qc.invalidateQueries({ queryKey: K('registros') })   // recupera el valor real tras el fallo
    },
  })

  const setZona = useMutation({
    mutationFn: async ({ municipio, zona }) =>
      (await api.put('/zonas', { municipio, zona }, { baseURL: base })).data,
    onSuccess: (r) => {
      toast.success(`${r.municipio} → ${r.zona || 'sin zona'} · ${r.registros_actualizados} registro(s)`)
      qc.invalidateQueries({ queryKey: K() })
    },
  })

  const borrar = useMutation({
    mutationFn: async () => (await api.delete(`/periodo/${mes}`, { baseURL: base })).data,
    onSuccess: (r) => { toast.success(`${r.borrados} registro(s) borrados`); qc.invalidateQueries({ queryKey: K() }) },
  })

  const exportar = async () => {
    const tid = toast.loading('Generando el Excel…')
    try {
      const res = await api.get('/export', { baseURL: base, params: mes ? { periodo: mes } : {}, responseType: 'blob' })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = `Programados_${mes || 'todos'}.xlsx`
      a.click()
      URL.revokeObjectURL(url)
      toast.success('Descargado', { id: tid })
    } catch {
      toast.error('No se pudo exportar', { id: tid })
    }
  }

  const abrirHistorial = async (r) => {
    const d = (await api.get(`/registro/${r.id}/historial`, { baseURL: base })).data
    setHist({ registro: r, cambios: d })
  }

  const items = data?.items || []
  const sinZona = data?.municipios_sin_zona || []

  return (
    <div className="space-y-3">
      {/* Barra de acciones */}
      <div className="flex flex-wrap gap-2 items-center">
        {!readOnly && (
          <>
            <input ref={fileRef} type="file" accept=".xlsx,.xlsm,.xls" className="hidden"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) subir.mutate(f); e.target.value = '' }} />
            <button onClick={() => fileRef.current?.click()} disabled={subir.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-sky-600 text-white rounded-lg hover:bg-sky-700 disabled:opacity-60">
              <Upload size={14} /> {subir.isPending ? 'Leyendo…' : 'Subir Excel Enel'}
            </button>
          </>
        )}
        <button onClick={exportar}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-white border border-sky-300 text-sky-700 rounded-lg hover:bg-sky-50">
          <FileDown size={14} /> Exportar Excel
        </button>
        <button onClick={() => setVerZonas(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-white border rounded-lg hover:bg-gray-50 text-gray-700">
          <MapPin size={14} /> Zonas por municipio
        </button>
        <button onClick={() => setVerCargas(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-white border rounded-lg hover:bg-gray-50 text-gray-700">
          <History size={14} /> Registro de cargas
        </button>
        {!readOnly && mes && items.length > 0 && (
          <button onClick={() => { if (confirm(`¿Borrar los ${items.length} registros de ${mes}?`)) borrar.mutate() }}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-white border border-red-200 text-red-600 rounded-lg hover:bg-red-50">
            <Trash2 size={14} /> Borrar el mes
          </button>
        )}
      </div>

      {/* Meses con datos: el archivo de Enel trae radicados de varios meses a la vez */}
      {(meses || []).length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
          <span className="text-gray-500 mr-1">Mes:</span>
          <button onClick={() => setMes('')}
            className={`px-2 py-0.5 rounded-full border ${mes === '' ? 'bg-sky-600 text-white border-sky-600' : 'bg-white text-gray-600 hover:bg-sky-50'}`}>
            Todos ({(meses || []).reduce((a, m) => a + m.total, 0)})
          </button>
          {(meses || []).map((m) => (
            <button key={m.periodo} onClick={() => setMes(m.periodo)}
              className={`px-2 py-0.5 rounded-full border ${mes === m.periodo ? 'bg-sky-600 text-white border-sky-600' : 'bg-white text-gray-600 hover:bg-sky-50'}`}>
              {m.periodo} ({m.total})
            </button>
          ))}
        </div>
      )}

      {/* Municipios sin zona: el único aviso que la máquina no puede resolver sola */}
      {sinZona.length > 0 && (
        <div className="text-[11px] text-amber-800 bg-amber-50 border border-amber-200 rounded p-2 max-w-3xl">
          <AlertTriangle size={13} className="inline mr-1 -mt-0.5" />
          <b>{sinZona.length} municipio(s) sin zona</b>: su columna ZONA queda vacía hasta que se relacionen.
          Clic en uno para asignarla:{' '}
          {sinZona.map((m) => (
            <button key={m} onClick={() => setVerZonas(m)}
              className="font-medium underline decoration-dotted underline-offset-2 hover:text-amber-950 mr-1.5">{m}</button>
          ))}
        </div>
      )}

      <p className="text-[11px] text-gray-500 max-w-4xl leading-relaxed">
        Espejo de la hoja <b>5.2</b> del formato de conexión del servicio. Las columnas M, N y O (grises)
        se calculan solas con las mismas fórmulas y festivos del Excel exportado.
        <span className="bg-orange-200 px-1 rounded mx-1">Naranja</span> = lo llenó el sistema desde la bolsa;
        <span className="bg-yellow-100 px-1 rounded mx-1">amarillo</span> = corregido a mano (las cargas no lo pisan).
        F2 o doble-clic edita · Enter baja · Ctrl+Z deshace · el embudo de cada columna filtra por valores.
      </p>

      {/* Grid tipo Excel — misma experiencia que el Registros (data) del ANS */}
      {isLoading ? (
        <p className="p-6 text-gray-400 text-sm">Cargando…</p>
      ) : items.length === 0 ? (
        <p className="p-6 text-gray-400 text-sm border rounded-lg bg-white">
          {(meses || []).length > 0
            ? 'Este mes no tiene registros. Elige otro mes arriba o pulsa «Todos».'
            : 'Sin registros. Sube el Excel de Enel (BASE CONTROL_NNDD) para empezar.'}
        </p>
      ) : (
        <ProgramadosGrid items={items} readOnly={readOnly} titulo={`3.Programados · ${mes || 'todos los meses'}`}
          onSave={(id, campo, valor) => guardar.mutate({ id, campo, valor })}
          onHistorial={abrirHistorial}
          historialDe={async (id) => (await api.get(`/registro/${id}/historial`, { baseURL: base })).data} />
      )}

      {/* ── Historial de un registro ─────────────────────────────────────────── */}
      {hist && (
        <Modal onClose={() => setHist(null)} titulo={`Historial · ${hist.registro.radicado}`}>
          {hist.registro.avisos?.length > 0 && (
            <div className="text-[11px] text-amber-800 bg-amber-50 border border-amber-200 rounded p-2 mb-3">
              {hist.registro.avisos.map((a, i) => <p key={i}>· {a}</p>)}
            </div>
          )}
          {hist.cambios.length === 0 ? (
            <p className="text-sm text-gray-400">Sin cambios registrados.</p>
          ) : (
            <table className="text-[11px] w-full">
              <thead className="text-gray-500 border-b">
                <tr>{['Cuándo', 'Columna', 'Antes', 'Después', 'Origen', 'Motivo'].map((h) => (
                  <th key={h} className="px-2 py-1 text-left">{h}</th>))}</tr>
              </thead>
              <tbody>
                {hist.cambios.map((c) => (
                  <tr key={c.id} className="border-t">
                    <td className="px-2 py-1 text-gray-400">{(c.creado_en || '').replace('T', ' ').slice(0, 16)}</td>
                    <td className="px-2 py-1 font-medium">{c.campo}</td>
                    <td className="px-2 py-1 text-gray-500">{c.valor_anterior || '—'}</td>
                    <td className="px-2 py-1">{c.valor_nuevo || '—'}</td>
                    <td className="px-2 py-1"><Origen v={c.procedencia} /></td>
                    <td className="px-2 py-1 text-gray-500">{c.motivo || ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Modal>
      )}

      {/* ── Registro de cargas ──────────────────────────────────────────────── */}
      {verCargas && <ModalCargas base={base} onClose={() => setVerCargas(false)} K={K} />}

      {/* ── Mapeo municipio → zona ──────────────────────────────────────────── */}
      {verZonas && (
        <ModalZonas base={base} K={K} readOnly={readOnly} resaltado={verZonas}
          onSet={(municipio, zona) => setZona.mutate({ municipio, zona })}
          onClose={() => setVerZonas(false)} />
      )}
    </div>
  )
}

function ModalCargas({ base, K, onClose }) {
  const { data: cargas } = useQuery({
    queryKey: K('cargas'),
    queryFn: async () => (await api.get('/cargas', { baseURL: base })).data,
  })
  return (
    <Modal onClose={onClose} titulo="Excel de Enel subidos" ancho="max-w-4xl">
      {!cargas?.length ? <p className="text-sm text-gray-400">Todavía no se ha subido ningún archivo.</p> : (
        <div className="space-y-2">
          {cargas.map((c) => (
            <div key={c.id} className="border rounded p-2.5 text-[11px]">
              <div className="flex items-center gap-2 flex-wrap">
                <FileSpreadsheet size={13} className="text-sky-600" />
                <b>{c.nombre}</b>
                <span className="text-gray-400">{(c.creado_en || '').replace('T', ' ').slice(0, 16)}</span>
                <span className="ml-auto text-gray-500">
                  hoja «{c.hoja}» · {c.filas_leidas} leídas · {c.filas_aplicadas} aplicadas · {c.celdas_llenas} celdas
                </span>
              </div>
              {c.resumen?.municipios_sin_zona?.length > 0 && (
                <p className="text-amber-700 mt-1">Sin zona: {c.resumen.municipios_sin_zona.join(', ')}</p>
              )}
              {c.resumen?.celdas_desde_bolsa > 0 && (
                <p className="text-orange-700 mt-0.5">{c.resumen.celdas_desde_bolsa} celda(s) resueltas desde la bolsa</p>
              )}
              {c.resumen?.fechas_descartadas?.length > 0 && (
                <details className="mt-1"><summary className="cursor-pointer text-gray-500">
                  {c.resumen.fechas_descartadas.length} fecha(s) descartadas</summary>
                  {c.resumen.fechas_descartadas.map((f, i) => <p key={i} className="text-gray-500 ml-3">· {f}</p>)}
                </details>
              )}
              {c.resumen?.pdl_descartados?.length > 0 && (
                <details className="mt-0.5"><summary className="cursor-pointer text-gray-500">
                  {c.resumen.pdl_descartados.length} PDL descartados por ser relleno</summary>
                  {c.resumen.pdl_descartados.map((f, i) => <p key={i} className="text-gray-500 ml-3">· {f}</p>)}
                </details>
              )}
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}

function ModalZonas({ base, K, readOnly, resaltado, onSet, onClose }) {
  const { data: zonas } = useQuery({
    queryKey: K('zonas'),
    queryFn: async () => (await api.get('/zonas', { baseURL: base })).data,
  })
  return (
    <Modal onClose={onClose} titulo="Zona de cada municipio">
      <p className="text-[11px] text-gray-500 mb-2">
        De aquí sale la columna ZONA. Se sembró con la hoja PROV del propio Excel de Enel y se corrige
        a mano; al cambiar una, los registros de ese municipio se actualizan solos.
      </p>
      <table className="text-[11px] w-full">
        <tbody>
          {(zonas || []).map((z) => (
            <tr key={z.id} className={`border-t ${resaltado === z.municipio ? 'bg-amber-50' : ''}`}>
              <td className="px-2 py-1 font-medium">{z.municipio}</td>
              <td className="px-2 py-1">
                <select value={z.zona || ''} disabled={readOnly}
                  onChange={(e) => onSet(z.municipio, e.target.value || null)}
                  className="px-2 py-0.5 border rounded bg-white">
                  <option value="">(sin zona)</option>
                  {ZONAS.map((zz) => <option key={zz} value={zz}>{zz}</option>)}
                </select>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Modal>
  )
}

function Origen({ v }) {
  const c = { import: 'bg-sky-100 text-sky-800', bolsa: 'bg-orange-200 text-orange-900',
    regla_h: 'bg-violet-100 text-violet-800', regla_p: 'bg-emerald-100 text-emerald-800',
    manual: 'bg-gray-200 text-gray-700' }[v] || 'bg-gray-100 text-gray-600'
  const label = { import: 'del archivo', bolsa: 'de la bolsa', regla_h: 'regla Si/NO',
    regla_p: 'regla de zona', manual: 'a mano' }[v] || v
  return <span className={`px-1.5 py-0.5 rounded ${c}`}>{label}</span>
}

function Modal({ titulo, ancho = 'max-w-3xl', onClose, children }) {
  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-start justify-center p-6 overflow-auto"
      onClick={onClose}>
      <div className={`bg-white rounded-lg shadow-xl w-full ${ancho} p-4`} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-semibold text-gray-800">{titulo}</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
        </div>
        {children}
      </div>
    </div>
  )
}
