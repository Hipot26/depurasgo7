import { useState, useRef, useEffect, useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import { Upload, CheckCircle2, SkipForward, RotateCcw, AlertTriangle, Loader2, FileSpreadsheet } from 'lucide-react'
import api from '../../utils/api'

// Pestaña «Nodos» (María Inés · 3.2 C): sube la «Programación Unificada» (el MISMO
// archivo de los 20', una sola subida alimenta 3.5 y esta bandeja), el sistema cruza cada
// PDL del mes contra su archivo y ella aprueba CASO A CASO — la máquina propone con
// evidencia (sus propias filas) pero nunca escribe sola. Enter = aprobar y pasar al
// siguiente, como una cola de revisión. Visible solo con usuario.puede_nodos.
// OJO (jul-2026): la P. UNIFICADA no trae columnas de NODOS PMS/CANT — el cruce de nodos
// queda sin sugerencias de cantidades hasta que el archivo las incluya (el parser ya las
// recoge solas si aparecen). El formato viejo «Programación diaria ENEL_V1» ya no se acepta.

const fmtF = (iso) => (iso ? String(iso).slice(0, 10).split('-').reverse().join('/') : '—')

const BADGE = {
  alta:      ['CRUCE CLARO', 'bg-green-100 text-green-700'],
  revisar:   ['REVISA EL CRUCE', 'bg-amber-100 text-amber-700'],
  sin_cruce: ['NO ESTÁ EN TU ARCHIVO', 'bg-gray-200 text-gray-600'],
}
const ESTADO_TXT = { pendiente: 'Pendiente', aprobado: 'Aprobado', omitido: 'Omitido', revisar: 'Cambió' }

// Orden de la cola: primero lo que cambió tras re-subir el archivo, luego lo pendiente con
// cruce claro (se despacha rápido), luego lo dudoso y de último lo que no está en su Excel.
const PESO = { revisar: 0, alta: 1, sin_cruce: 3 }
const peso = (c) => (c.estado === 'revisar' ? 0 : (PESO[c.confianza] ?? 2))

export default function NodosTab({ apiBase, periodo, K, readOnly }) {
  const qc = useQueryClient()
  const fileRef = useRef()
  const [sel, setSel] = useState(null)          // id del caso abierto
  const [vista, setVista] = useState('cola')    // cola | aprobados | omitidos
  const [plan, setPlan] = useState('')
  const [ejec, setEjec] = useState('')
  const [obs, setObs] = useState('')
  const [guardando, setGuardando] = useState(false)

  const { data, isLoading, refetch } = useQuery({
    queryKey: K('ans-nodos', periodo),
    queryFn: async () => (await api.get('/nodos/casos', { baseURL: apiBase, params: { periodo } })).data,
  })
  const casos = data?.casos || []

  const cola = useMemo(() =>
    casos.filter((c) => c.estado === 'pendiente' || c.estado === 'revisar')
      .sort((a, b) => peso(a) - peso(b) || String(a.reg.pdl || '').localeCompare(String(b.reg.pdl || ''))),
    [casos])
  const aprobados = casos.filter((c) => c.estado === 'aprobado')
  const omitidos = casos.filter((c) => c.estado === 'omitido')
  const lista = vista === 'cola' ? cola : vista === 'aprobados' ? aprobados : omitidos
  const caso = casos.find((c) => c.id === sel) || null

  // Al cambiar de caso, precarga: lo ya guardado en la grilla manda; si no, la propuesta.
  useEffect(() => {
    if (!caso) return
    setPlan(caso.reg.nodos_planeados ?? caso.sugerido_planeados ?? '')
    setEjec(caso.reg.nodos_ejecutados ?? caso.sugerido_ejecutados ?? '')
    setObs(caso.reg.obs_nodos ?? '')
  }, [sel])  // eslint-disable-line react-hooks/exhaustive-deps

  // Sin selección (o el caso salió de la lista): abre el primero de la vista actual.
  useEffect(() => {
    if (!lista.length) { setSel(null); return }
    if (!lista.some((c) => c.id === sel)) setSel(lista[0].id)
  }, [lista, sel])

  const invalidar = () => {
    qc.invalidateQueries({ queryKey: K('ans-nodos') })
    qc.invalidateQueries({ queryKey: K('ans') })          // la grilla: AV/AW recién escritos
    qc.invalidateQueries({ queryKey: K('ans-tareas') })   // campanita 3.2C
  }

  const siguiente = (idActual) => {
    const resto = cola.filter((c) => c.id !== idActual)
    setSel(resto.length ? resto[0].id : null)
  }

  const aprobar = async () => {
    if (!caso || guardando) return
    if (plan === '' && ejec === '') { toast.error('Escribe al menos los nodos planeados o ejecutados'); return }
    setGuardando(true)
    try {
      const r = (await api.post(`/nodos/casos/${caso.id}/aprobar`, {
        nodos_planeados: plan === '' ? null : Number(plan),
        nodos_ejecutados: ejec === '' ? null : Number(ejec),
        obs_nodos: obs || null,
      }, { baseURL: apiBase })).data
      toast.success(`PDL ${caso.reg.pdl || caso.reg.protocolo}: nodos guardados`, { duration: 2500 })
      // Si encima de lo aprobado se disparó una regla automática (anulada excluida → planeados 0,
      // o cerrada → ejecutados = planeados), se avisa: en la grilla la celda queda amarilla.
      const m = r?.reg?.nodos_auto
      if (m?.campo && m.estado !== 'invalida') {
        toast(`El sistema ajustó ${m.campo === 'nodos_planeados' ? 'los nodos planeados' : 'los nodos ejecutados'} a ${m.valor} (${m.regla === 'anulado_excluido' ? 'orden anulada con exclusión aprobada' : 'orden cerrada: se iguala a lo planeado'}). La celda queda amarilla en la grilla.`,
          { duration: 7000 })
      }
      siguiente(caso.id)
      invalidar()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'No se pudo aprobar el caso')
    } finally { setGuardando(false) }
  }

  const cambiarEstado = async (c, estado) => {
    try {
      await api.post(`/nodos/casos/${c.id}/estado`, { estado }, { baseURL: apiBase })
      if (estado === 'omitido') siguiente(c.id)
      invalidar()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'No se pudo cambiar el estado')
    }
  }

  const subir = async () => {
    const f = fileRef.current?.files?.[0]
    if (fileRef.current) fileRef.current.value = ''
    if (!f) return
    const tid = toast.loading('Subiendo tu Programación Unificada…')
    try {
      const fd = new FormData(); fd.append('file', f)
      const r = (await api.post('/prog20', fd, { baseURL: apiBase })).data
      toast.success(`Archivo cargado: ${r.filas} filas. Recalculando el cruce…`, { id: tid, duration: 5000 })
      invalidar()
      qc.invalidateQueries({ queryKey: K('ans-indplus') })          // también alimenta el 3.5
      qc.invalidateQueries({ queryKey: K('ans-prog20-archivos') })
      refetch()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'No se pudo subir el archivo', { id: tid })
    }
  }

  // Enter aprueba (fuera del textarea); en el textarea, Ctrl+Enter.
  const onKey = (e) => {
    if (e.key !== 'Enter' || readOnly || vista !== 'cola') return
    if (e.target.tagName === 'TEXTAREA' && !e.ctrlKey) return
    e.preventDefault()
    aprobar()
  }

  const total = casos.length
  const hechos = aprobados.length
  const [badgeTxt, badgeCls] = caso ? (caso.estado === 'revisar'
    ? ['CAMBIÓ TU ARCHIVO', 'bg-rose-100 text-rose-700']
    : BADGE[caso.confianza] || ['', 'bg-gray-100 text-gray-500']) : ['', '']

  return (
    <div className="space-y-3">
      {/* Cabecera: archivo + subir + progreso */}
      <div className="bg-white rounded-lg shadow p-3 flex flex-wrap items-center gap-3">
        <input ref={fileRef} type="file" accept=".xlsx" hidden onChange={subir} />
        {!readOnly && (
          <button onClick={() => fileRef.current?.click()}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 text-white rounded text-xs font-medium hover:bg-blue-700">
            <Upload size={14} /> Subir mi Programación Unificada
          </button>
        )}
        <div className="flex items-center gap-1.5 text-xs text-gray-600">
          <FileSpreadsheet size={14} className="text-emerald-600" />
          {data?.fuente
            ? <span><b>{data.fuente.nombre}</b> · {data.fuente.filas} filas · subido {fmtF(data.fuente.subido)}</span>
            : <span className="text-amber-600">Aún no has subido tu archivo: sube la «Programación Unificada» (hoja P. UNIFICADA) para que el sistema haga el cruce.</span>}
        </div>
        <div className="ml-auto flex items-center gap-2 text-xs">
          <span className="text-gray-500">Progreso del mes:</span>
          <span className="font-semibold text-green-700">{hechos} de {total} aprobados</span>
          <div className="w-28 h-2 bg-gray-200 rounded-full overflow-hidden">
            <div className="h-full bg-green-500" style={{ width: total ? `${(hechos / total) * 100}%` : 0 }} />
          </div>
        </div>
      </div>

      <p className="text-[11px] text-gray-400 -mt-1">
        El sistema cruza cada PDL del mes con la columna PDT de tu archivo y te <b>propone</b> los nodos —
        nada se guarda hasta que tú apruebes cada caso. Corrige los números a la realidad, deja tu
        observación si aplica y dale <b>Aprobar y siguiente</b> (o la tecla <b>Enter</b>).
      </p>

      {isLoading ? (
        <div className="flex items-center gap-2 text-sm text-gray-500 p-8 justify-center"><Loader2 className="animate-spin" size={18} /> Cruzando con tu archivo…</div>
      ) : (
        <div className="flex gap-3 items-start">
          {/* Lista izquierda */}
          <div className="w-72 shrink-0 bg-white rounded-lg shadow overflow-hidden">
            <div className="flex border-b text-[11px]">
              {[['cola', `Por revisar (${cola.length})`], ['aprobados', `Aprobados (${aprobados.length})`], ['omitidos', `Omitidos (${omitidos.length})`]].map(([id, label]) => (
                <button key={id} onClick={() => { setVista(id); setSel(null) }}
                  className={`flex-1 px-1 py-1.5 font-medium ${vista === id ? 'bg-blue-50 text-blue-700' : 'text-gray-500 hover:bg-gray-50'}`}>
                  {label}
                </button>
              ))}
            </div>
            <div className="max-h-[62vh] overflow-y-auto divide-y">
              {lista.length === 0 && (
                <p className="text-xs text-gray-400 p-4 text-center">
                  {vista === 'cola' ? 'Nada por revisar en este mes' : 'Vacío'}
                </p>
              )}
              {lista.map((c) => (
                <button key={c.id} onClick={() => setSel(c.id)}
                  className={`w-full text-left px-2.5 py-2 text-xs hover:bg-blue-50 ${sel === c.id ? 'bg-blue-50 border-l-2 border-blue-600' : ''}`}>
                  <div className="flex items-center justify-between gap-1">
                    <span className="font-mono font-semibold truncate">{c.reg.pdl || c.reg.protocolo}</span>
                    {c.estado === 'revisar'
                      ? <span className="text-[9px] px-1 rounded bg-rose-100 text-rose-700 shrink-0">CAMBIÓ</span>
                      : c.confianza === 'alta' && vista === 'cola'
                        ? <span className="text-[9px] px-1 rounded bg-green-100 text-green-700 shrink-0">CLARO</span>
                        : c.confianza === 'sin_cruce' && vista === 'cola'
                          ? <span className="text-[9px] px-1 rounded bg-gray-200 text-gray-500 shrink-0">SIN CRUCE</span>
                          : null}
                  </div>
                  <div className="text-[10px] text-gray-500 flex justify-between">
                    <span>{c.reg.zona || '—'} · {c.reg.fecha_inicio || ''}</span>
                    {c.sugerido_planeados != null && vista === 'cola' && <span className="text-gray-400">propone {c.sugerido_planeados}</span>}
                    {vista === 'aprobados' && <span>{c.reg.nodos_planeados ?? '—'} / {c.reg.nodos_ejecutados ?? '—'}</span>}
                  </div>
                </button>
              ))}
            </div>
          </div>

          {/* Tarjeta del caso */}
          <div className="flex-1 min-w-0" onKeyDown={onKey}>
            {!caso ? (
              <div className="bg-white rounded-lg shadow p-10 text-center text-sm text-gray-400">
                {vista === 'cola' && cola.length === 0
                  ? <span>No queda nada por revisar en {periodo}. Si subes un archivo más nuevo, el cruce se recalcula solo.</span>
                  : <span>Elige un caso de la lista</span>}
              </div>
            ) : (
              <div className="bg-white rounded-lg shadow p-4 space-y-3">
                {/* Encabezado del registro ANS */}
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono font-bold text-base">{caso.reg.pdl || caso.reg.protocolo}</span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-100 text-blue-700">{caso.reg.tipo}</span>
                  {caso.reg.zona && <span className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700">{caso.reg.zona}</span>}
                  {badgeTxt && <span className={`text-[10px] px-1.5 py-0.5 rounded font-semibold ${badgeCls}`}>{badgeTxt}</span>}
                  <span className="ml-auto text-[11px] text-gray-400">{caso.reg.fecha_inicio || ''} · {caso.reg.estado_actual || ''}</span>
                </div>
                {caso.reg.descripcion && <p className="text-xs text-gray-600 line-clamp-2">{caso.reg.descripcion}</p>}

                {/* Qué encontró la máquina y por qué */}
                <div className={`text-xs rounded p-2 flex gap-2 ${caso.confianza === 'alta' && caso.estado !== 'revisar' ? 'bg-green-50 text-green-800' : caso.confianza === 'sin_cruce' ? 'bg-gray-50 text-gray-600' : 'bg-amber-50 text-amber-800'}`}>
                  {caso.confianza !== 'alta' && <AlertTriangle size={14} className="shrink-0 mt-0.5" />}
                  <span>{caso.motivo}</span>
                </div>

                {/* Evidencia: SUS filas, tal cual vienen en su Excel */}
                {caso.evidencia?.length > 0 && (
                  <div className="overflow-x-auto border rounded">
                    <table className="text-[11px] w-full">
                      <thead className="bg-gray-50 text-gray-500">
                        <tr>
                          <th className="px-2 py-1 text-left">Fecha</th>
                          <th className="px-2 py-1 text-left">Nodos (col F)</th>
                          <th className="px-2 py-1 text-left">Cant (col G)</th>
                          <th className="px-2 py-1 text-left">Avance 20:30</th>
                          <th className="px-2 py-1 text-left">Municipio</th>
                          <th className="px-2 py-1 text-left">Jefe cuadrilla</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y">
                        {caso.evidencia.map((e, i) => (
                          <tr key={i} className={e.cant_num != null && e.cant_num === caso.sugerido_planeados ? 'bg-green-50/60' : ''}>
                            <td className="px-2 py-1 whitespace-nowrap">{fmtF(e.fecha)}</td>
                            <td className="px-2 py-1 max-w-[220px] truncate" title={e.nodos || ''}>{e.nodos || '—'}</td>
                            <td className="px-2 py-1">{e.cant ?? '—'}{e.cant_num != null && String(e.cant_num) !== String(e.cant || '').trim() ? <span className="text-gray-400"> (→{e.cant_num})</span> : null}</td>
                            <td className="px-2 py-1 max-w-[140px] truncate" title={e.avance || ''}>{e.avance ?? '—'}</td>
                            <td className="px-2 py-1">{e.municipio || '—'}</td>
                            <td className="px-2 py-1 max-w-[160px] truncate" title={e.jefe || ''}>{e.jefe || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {/* Los DOS números de ella + su observación */}
                <div className="flex flex-wrap items-end gap-3">
                  <label className="text-xs text-gray-600">
                    <span className="block mb-0.5 font-medium">Nodos planeados <span className="text-gray-400">(AV)</span></span>
                    <input type="number" min="0" value={plan} onChange={(e) => setPlan(e.target.value)} disabled={readOnly}
                      className="w-28 border rounded px-2 py-1.5 text-sm font-semibold focus:ring-2 focus:ring-blue-400 outline-none" />
                  </label>
                  <label className="text-xs text-gray-600">
                    <span className="block mb-0.5 font-medium">Nodos ejecutados <span className="text-gray-400">(AW)</span></span>
                    <input type="number" min="0" value={ejec} onChange={(e) => setEjec(e.target.value)} disabled={readOnly}
                      className="w-28 border rounded px-2 py-1.5 text-sm font-semibold focus:ring-2 focus:ring-blue-400 outline-none" />
                  </label>
                  <label className="text-xs text-gray-600 flex-1 min-w-[220px]">
                    <span className="block mb-0.5 font-medium">Tu observación <span className="text-gray-400">(AY, opcional)</span></span>
                    <textarea rows={1} value={obs} onChange={(e) => setObs(e.target.value)} disabled={readOnly}
                      placeholder="P. ej.: se ejecutaron 7 de 9 por lluvia en la zona"
                      className="w-full border rounded px-2 py-1.5 text-xs focus:ring-2 focus:ring-blue-400 outline-none resize-y" />
                  </label>
                </div>

                {/* Acciones */}
                {vista !== 'aprobados' && !readOnly && (
                  <div className="flex items-center gap-2 pt-1">
                    <button onClick={aprobar} disabled={guardando}
                      className="flex items-center gap-1.5 px-4 py-2 bg-green-600 text-white rounded text-sm font-medium hover:bg-green-700 disabled:opacity-50">
                      {guardando ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />} Aprobar y siguiente <kbd className="text-[9px] bg-green-700/60 px-1 rounded">Enter</kbd>
                    </button>
                    <button onClick={() => cambiarEstado(caso, 'omitido')}
                      className="flex items-center gap-1 px-3 py-2 bg-gray-100 text-gray-600 rounded text-xs hover:bg-gray-200"
                      title="Sácalo de la cola sin escribir nada (lo puedes reabrir luego)">
                      <SkipForward size={13} /> Omitir
                    </button>
                  </div>
                )}
                {(vista === 'aprobados' || vista === 'omitidos') && !readOnly && (
                  <div className="flex items-center gap-2 pt-1">
                    {vista === 'aprobados' && (
                      <p className="text-[11px] text-gray-400">
                        Aprobado por <b>{caso.aprobado_por}</b>{caso.aprobado_en ? ` · ${fmtF(caso.aprobado_en)}` : ''}. Puedes corregir los números y volver a aprobar.
                      </p>
                    )}
                    {vista === 'aprobados' && (
                      <button onClick={aprobar} disabled={guardando}
                        className="flex items-center gap-1 px-3 py-1.5 bg-green-600 text-white rounded text-xs hover:bg-green-700 disabled:opacity-50">
                        <CheckCircle2 size={13} /> Guardar corrección
                      </button>
                    )}
                    <button onClick={() => cambiarEstado(caso, 'pendiente')}
                      className="flex items-center gap-1 px-3 py-1.5 bg-gray-100 text-gray-600 rounded text-xs hover:bg-gray-200">
                      <RotateCcw size={13} /> Devolver a la cola
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
