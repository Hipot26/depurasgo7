import { useState, useEffect, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import { FolderArchive, Download, FileSpreadsheet, Video, Mail, FileText, Package, Loader2, X, AlertTriangle,
  Eye, EyeOff, Pencil, Check, Tag, Image as ImageIcon, Mic } from 'lucide-react'
import api from '../../utils/api'

// Tab «Evidencias»: replica la estructura de carpetas de SharePoint del proceso manual
// (exclusiones/<indicador>/<N.º de orden>/<archivos>) y exporta el MES completo en un ZIP:
// el Calculo_ANS_<Mes>.xlsx + los videos (3.2 A) + los correos (3.2 B) que se suben para la IA.
//
// PAQUETE PARA ENEL (14 oct 2026, requisito surgido de la conciliación de septiembre): el árbol
// muestra el NOMBRE que tendrá cada archivo en el ZIP — «Evidencia_1_Video_16-50_a_16-53_<qué
// muestra>.mp4», en orden de hora de grabación — y deja decidir qué va y qué no (ojo), corregir la
// hora y la etiqueta (lápiz) y pedirle a la IA que nombre las que faltan. Cada carpeta lleva un
// 00_RESUMEN y la raíz un 00_INDICE: quien expone abre un solo archivo y sabe dónde está parado.
//
// Los archivos se traen por axios (el token va en el header — un <a href> directo daría 401):
// videos e imágenes se VEN en un visor aquí mismo; el resto (.msg) se descarga.

const kb = (n) => (n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.round((n || 0) / 1024)} KB`)
const K = (...p) => ['ans', ...p]

const esVideo  = (n) => /\.(mp4|mov|webm|mkv|avi|m4v|3gp)$/i.test(n || '')
const esImagen = (n) => /\.(png|jpe?g|gif|webp)$/i.test(n || '')

const ICONO_TIPO = { Video, Imagen: ImageIcon, Audio: Mic, Correo: Mail, PDF: FileText, Documento: FileText }
const HHMM = /^([01]?\d|2[0-3]):[0-5]\d$/

export default function EvidenciasTab({ apiBase, apiRoot, periodo }) {
  const qc = useQueryClient()
  const [exportando, setExportando] = useState(null)   // 'si' (solo excluibles) | 'todo' | null
  const [confirmar, setConfirmar] = useState(false)    // modal del paquete completo
  const [textoOk, setTextoOk] = useState('')           // hay que escribir "acepto"
  const [ver, setVer] = useState(null)       // visor: {nombre, cargando} → {nombre, tipo, url, archivo}
  const [bajando, setBajando] = useState(null)  // id del archivo en descarga
  const [editando, setEditando] = useState(null) // {id, hora, hora_fin, fecha, que_muestra}
  const [guardando, setGuardando] = useState(null) // id en PATCH
  const [etiquetando, setEtiquetando] = useState(false)

  const arbol = useQuery({
    queryKey: K('evidencias', periodo),
    queryFn: async () => (await api.get('/evidencias', { baseURL: apiBase, params: { periodo } })).data,
  })
  const refrescar = () => qc.invalidateQueries({ queryKey: K('evidencias', periodo) })

  const traerBlob = async (a) => {
    const res = await api.get(`/evidencia/${a.id}/descargar`, { baseURL: apiRoot, responseType: 'blob' })
    return new Blob([res.data], { type: res.headers['content-type'] || undefined })
  }

  const descargar = async (a) => {
    setBajando(a.id)
    try {
      const url = URL.createObjectURL(await traerBlob(a))
      const el = document.createElement('a')
      el.href = url; el.download = a.nombre_paquete || a.nombre; el.click()
      URL.revokeObjectURL(url)
    } catch {
      toast.error('No se pudo descargar el archivo')
    } finally { setBajando(null) }
  }

  const abrir = async (a) => {
    if (!esVideo(a.nombre) && !esImagen(a.nombre) && a.tipo !== 'Video' && a.tipo !== 'Imagen') { descargar(a); return }
    setVer({ nombre: a.nombre_paquete || a.nombre, cargando: true })
    try {
      const url = URL.createObjectURL(await traerBlob(a))
      setVer({ nombre: a.nombre_paquete || a.nombre, tipo: (a.tipo === 'Video' || esVideo(a.nombre)) ? 'video' : 'imagen', url, archivo: a })
    } catch {
      toast.error('No se pudo cargar el archivo')
      setVer(null)
    }
  }

  const cerrarVer = useCallback(() => {
    setVer((v) => { if (v?.url) URL.revokeObjectURL(v.url); return null })
  }, [])
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') { cerrarVer(); setConfirmar(false); setTextoOk(''); setEditando(null) } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cerrarVer])

  // ── Qué va al paquete y cómo se llama ──
  const patch = async (id, datos, okMsg) => {
    setGuardando(id)
    try {
      await api.patch(`/evidencia/${id}`, datos, { baseURL: apiBase })
      if (okMsg) toast.success(okMsg, { duration: 2500 })
      await refrescar()
      return true
    } catch (e) {
      toast.error(e?.response?.data?.detail || 'No se pudo guardar')
      return false
    } finally { setGuardando(null) }
  }
  const alternar = (a) => patch(a.id, { en_paquete: !a.en_paquete },
    a.en_paquete ? 'Fuera del paquete (no se le enseña a ENEL)' : 'Incluida en el paquete')
  const empezarEdicion = (a) => setEditando({ id: a.id, hora: a.hora || '', hora_fin: a.hora_fin || '', fecha: a.fecha || '', que_muestra: a.que_muestra || '' })
  const guardarEdicion = async () => {
    if (!editando) return
    for (const k of ['hora', 'hora_fin']) {
      if (editando[k] && !HHMM.test(editando[k])) { toast.error(`${k === 'hora' ? 'Hora' : 'Hora fin'}: usa HH:MM`); return }
    }
    if (editando.fecha && !/^\d{4}-\d{2}-\d{2}$/.test(editando.fecha)) { toast.error('Fecha: usa AAAA-MM-DD'); return }
    const ok = await patch(editando.id, { hora: editando.hora, hora_fin: editando.hora_fin, fecha: editando.fecha, que_muestra: editando.que_muestra })
    if (ok) setEditando(null)
  }

  const etiquetar = async () => {
    setEtiquetando(true)
    const tid = toast.loading('La IA está poniendo nombre a las evidencias que faltan…')
    try {
      const r = await api.post('/evidencias/etiquetar', null, { baseURL: apiBase, params: { periodo } })
      toast.success(`${r.data.etiquetadas} etiquetada${r.data.etiquetadas === 1 ? '' : 's'}${r.data.pendientes ? ` · quedan ${r.data.pendientes} (vuelve a pulsar)` : ''}`, { id: tid, duration: 6000 })
      await refrescar()
    } catch (e) {
      toast.error(e?.response?.data?.detail || 'No se pudo etiquetar', { id: tid })
    } finally { setEtiquetando(false) }
  }

  // soloExcluibles=true → el paquete que se envía (solo casos con decisión SI);
  // false → TODO, incluidos soportes de casos NO excluibles (uso interno, pide confirmación antes)
  // `indicador` (2026-10-08): 'tiempos' | 'anulaciones' → solo esa carpeta + el xlsx. Un tramo
  // liviano: las anulaciones son .msg y se reenvían al corregir un correo sin bajar los videos.
  const NOMBRE_IND = { tiempos: '_3.2A_Tiempos', anulaciones: '_3.2B_Anulaciones' }
  const exportar = async (soloExcluibles, indicador = null) => {
    setExportando(indicador || (soloExcluibles ? 'si' : 'todo'))
    const tid = toast.loading(indicador ? `Armando el paquete de ${indicador} (ZIP)…` : 'Armando el paquete del mes (ZIP)…')
    const mb = (n) => `${Math.round((n || 0) / 1048576)} MB`
    try {
      // El paquete pesa cientos de MB: el avance se muestra en el toast (el API manda el
      // tamaño total) — sin esto el botón solo giraba y una descarga larga parecía colgada.
      const res = await api.get('/evidencias/exportar', {
        baseURL: apiBase, params: { periodo, solo_excluibles: soloExcluibles, ...(indicador ? { indicador } : {}) }, responseType: 'blob',
        onDownloadProgress: (p) => {
          const total = p.total || 0
          toast.loading(total
            ? `Descargando el paquete… ${mb(p.loaded)} de ${mb(total)} (${Math.round(100 * p.loaded / total)} %). No cierres la pestaña.`
            : `Descargando el paquete… ${mb(p.loaded)}. No cierres la pestaña.`, { id: tid })
        },
      })
      const mes = arbol.data?.mes || periodo
      const a = document.createElement('a')
      const url = URL.createObjectURL(new Blob([res.data], { type: 'application/zip' }))
      a.href = url
      const suf = NOMBRE_IND[indicador] || ''
      a.download = soloExcluibles ? `ANS_${mes}${suf}.zip` : `ANS_${mes}${suf}_COMPLETO_INTERNO.zip`
      document.body.appendChild(a)
      a.click()
      a.remove()
      // Revocar de inmediato puede cancelar la descarga de un blob grande en Chrome.
      setTimeout(() => URL.revokeObjectURL(url), 120000)
      const que = indicador === 'tiempos' ? 'Paquete de tiempos (3.2 A)' : indicador === 'anulaciones' ? 'Paquete de anulaciones (3.2 B)'
        : soloExcluibles ? 'Paquete de excluibles' : 'Paquete completo (interno)'
      toast.success(`${que} descargado (${mb(res.data.size)}). Revisa la carpeta de descargas.`, { id: tid, duration: 8000 })
    } catch (e) {
      toast.error(e?.response?.status === 404 ? 'No hay datos ni evidencias este mes'
        : `No se pudo exportar (${e?.message || 'error de red'}). Si se cortó a medias, vuelve a intentar.`, { id: tid, duration: 8000 })
    } finally { setExportando(null) }
  }

  const abrirConfirmar = () => { setTextoOk(''); setConfirmar(true) }
  const cerrarConfirmar = () => { setConfirmar(false); setTextoOk('') }
  const confirmarOk = textoOk.trim().toLowerCase() === 'acepto'

  const d = arbol.data
  const total = d?.total_evidencias || 0
  const totalSi = d?.total_si || 0
  const totalPaquete = d?.total_paquete || 0
  const sinEtiqueta = d?.sin_etiqueta || 0

  return (
    <div className="space-y-4">
      {/* Cabecera del paquete + botones de exportar (excluibles = el que se envía; completo = interno) */}
      <div className="flex items-center gap-3 bg-white border rounded-lg p-3 flex-wrap">
        <Package size={26} className="text-blue-600 shrink-0" />
        <div className="flex-1 min-w-[260px]">
          <p className="text-sm font-semibold text-gray-800">Paquete de {d?.mes?.replace('_', ' ') || periodo}</p>
          <p className="text-xs text-gray-500">
            {d?.ans_disponible ? 'Calculo_ANS del mes' : 'sin ANS este mes'} · {totalPaquete} de {totalSi} soporte{totalSi !== 1 ? 's' : ''} de casos excluibles van en el paquete
            {total > totalSi ? ` · ${total - totalSi} de casos no excluibles (solo en el completo)` : ''}
            {sinEtiqueta > 0 && <span className="text-amber-700"> · {sinEtiqueta} sin «qué muestra»</span>}
          </p>
        </div>
        <button onClick={etiquetar} disabled={etiquetando || !sinEtiqueta}
          title="La IA pone una etiqueta corta («Lluvia en el sitio», «Via cerrada…») a las evidencias del paquete que no la tengan. Las escritas a mano no se tocan."
          className="flex items-center gap-1.5 text-sm font-medium px-3 py-2 rounded-lg border border-emerald-300 text-emerald-800 hover:bg-emerald-50 disabled:opacity-40">
          {etiquetando ? <Loader2 size={15} className="animate-spin" /> : <Tag size={15} />}
          Nombrar con IA ({sinEtiqueta})
        </button>
        {/* Por indicador (2026-10-08): tramos livianos — las anulaciones son .msg y se reenvían
            al corregir un correo sin repetir los videos del 3.2 A */}
        <button onClick={() => exportar(true, 'anulaciones')} disabled={exportando || !d?.ans_disponible}
          title="Solo el 3.2 B: correos del Centro Operativo de casos excluibles + el Calculo_ANS. Liviano: para reenviar correcciones de correos."
          className="flex items-center gap-1.5 text-sm font-medium px-3 py-2 rounded-lg border border-violet-300 text-violet-800 hover:bg-violet-50 disabled:opacity-40">
          {exportando === 'anulaciones' ? <Loader2 size={15} className="animate-spin" /> : <Mail size={15} />}
          Solo anulaciones (3.2 B)
        </button>
        <button onClick={() => exportar(true, 'tiempos')} disabled={exportando || !d?.ans_disponible}
          title="Solo el 3.2 A: videos del frente de trabajo de casos excluibles + el Calculo_ANS."
          className="flex items-center gap-1.5 text-sm font-medium px-3 py-2 rounded-lg border border-sky-300 text-sky-800 hover:bg-sky-50 disabled:opacity-40">
          {exportando === 'tiempos' ? <Loader2 size={15} className="animate-spin" /> : <Video size={15} />}
          Solo tiempos (3.2 A)
        </button>
        <button onClick={() => exportar(true)} disabled={exportando || (!d?.ans_disponible && totalSi === 0)}
          title="Solo los soportes de casos con decisión excluible = SI, de los dos indicadores. Este es el que se envía completo."
          className="flex items-center gap-1.5 text-sm font-medium px-3 py-2 rounded-lg bg-blue-700 text-white hover:bg-blue-800 disabled:opacity-40">
          {exportando === 'si' ? <Loader2 size={15} className="animate-spin" /> : <FolderArchive size={15} />}
          Exportar excluibles (ZIP)
        </button>
        <button onClick={abrirConfirmar} disabled={exportando || (!d?.ans_disponible && total === 0)}
          title="TODO el mes, incluidos soportes de casos NO excluibles y los que dejaste fuera. Solo para revisión interna."
          className="flex items-center gap-1.5 text-sm font-medium px-3 py-2 rounded-lg border border-amber-300 text-amber-700 hover:bg-amber-50 disabled:opacity-40">
          {exportando === 'todo' ? <Loader2 size={15} className="animate-spin" /> : <AlertTriangle size={15} />}
          Todo (uso interno)
        </button>
      </div>

      {/* Vista previa del árbol de carpetas que irá en el ZIP, con los nombres finales */}
      <div className="border rounded-lg overflow-hidden">
        <div className="bg-gray-50 px-3 py-2 text-xs font-semibold text-gray-600 flex items-center gap-1.5 flex-wrap">
          <FolderArchive size={14} /> Contenido del paquete — tal como lo verá ENEL
          <span className="font-normal text-gray-400 ml-auto">clic en el nombre = verlo · ojo = va / no va · lápiz = hora y «qué muestra» · flecha = descargar</span>
        </div>
        <div className="p-3 font-mono text-[11px] text-gray-700 space-y-0.5">
          {arbol.isLoading ? (
            <p className="text-gray-400">Cargando…</p>
          ) : (
            <>
              <p className="font-semibold text-gray-800">ANS_{d?.mes || periodo}/</p>
              {d?.ans_disponible && (
                <p className="pl-4 flex items-center gap-1"><FileSpreadsheet size={12} className="text-green-600" /> Calculo_ANS_{periodo}.xlsx</p>
              )}
              <p className="pl-4 flex items-center gap-1 text-gray-500"><FileText size={12} /> 00_INDICE.txt <span className="text-gray-400">· una línea por orden: causa y nº de evidencias</span></p>
              {(d?.grupos || []).length === 0 ? (
                <p className="pl-4 text-gray-400">— aún no hay soportes (videos/correos) este mes —</p>
              ) : (
                <p className="pl-4 text-gray-500">exclusiones/</p>
              )}
              {(d?.grupos || []).map((g) => (
                <div key={g.campo}>
                  <p className="pl-8 text-gray-700">{g.label}/ <span className="text-gray-400">({g.total})</span></p>
                  {g.ordenes.map((o) => (
                    <div key={o.protocolo} className="mb-1">
                      <p className="pl-12 text-gray-600">{o.protocolo}/
                        {o.si === false && (
                          <span className="ml-1.5 text-amber-600" title="La decisión de este caso no quedó en SI">
                            no excluible, solo va en el paquete completo
                          </span>
                        )}
                      </p>
                      <p className="pl-16 flex items-center gap-1.5 text-gray-500">
                        <FileText size={11} className="shrink-0" />
                        <span className="truncate">{o.resumen}</span>
                        {o.causa && <span className="text-gray-400 truncate" title={o.causa}>· {o.causa}</span>}
                      </p>
                      {o.archivos.map((a) => {
                        const Ico = ICONO_TIPO[a.tipo] || FileText
                        const fuera = !a.en_paquete
                        const enEdicion = editando?.id === a.id
                        return (
                          <div key={a.id}>
                            <p className={`pl-16 flex items-center gap-1.5 ${fuera ? 'opacity-60' : ''}`}>
                              <button onClick={() => alternar(a)} disabled={guardando === a.id}
                                title={fuera ? 'NO va en el paquete (clic para incluirla)' : 'Va en el paquete para ENEL (clic para dejarla fuera)'}
                                className={`shrink-0 p-0.5 rounded ${fuera ? 'text-gray-400 hover:text-green-700' : 'text-green-600 hover:text-amber-600'}`}>
                                {guardando === a.id ? <Loader2 size={12} className="animate-spin" /> : fuera ? <EyeOff size={12} /> : <Eye size={12} />}
                              </button>
                              <Ico size={11} className="text-gray-500 shrink-0" />
                              <button onClick={() => abrir(a)}
                                title={`${a.nombre}${a.nota ? ' — ' + a.nota : ''}`}
                                className={`hover:underline truncate text-left ${fuera ? 'text-gray-500 line-through' : 'text-blue-700'}`}>
                                {a.nombre_paquete || a.nombre}
                              </button>
                              {fuera && <span className="text-[10px] text-amber-700 shrink-0">no va</span>}
                              {!fuera && !a.que_muestra && o.si !== false && (
                                <span className="text-[10px] text-amber-600 shrink-0" title="Sin etiqueta «qué muestra»: ponla con el lápiz o con «Nombrar con IA»">sin etiqueta</span>
                              )}
                              {!fuera && a.tipo === 'Video' && !a.hora && (
                                <span className="text-[10px] text-amber-600 shrink-0" title="Sin hora de grabación: irá al final de la carpeta. Ponla con el lápiz (la del letrero del video).">sin hora</span>
                              )}
                              <span className="text-gray-400 shrink-0">· {kb(a.tamano)}</span>
                              <button onClick={() => (enEdicion ? setEditando(null) : empezarEdicion(a))} title="Corregir hora, fecha o «qué muestra»"
                                className={`shrink-0 p-0.5 rounded hover:bg-blue-50 ${enEdicion ? 'text-blue-700' : 'text-gray-400 hover:text-blue-700'}`}>
                                <Pencil size={12} />
                              </button>
                              <button onClick={() => descargar(a)} disabled={bajando === a.id} title="Descargar este archivo"
                                className="shrink-0 p-0.5 rounded text-gray-400 hover:text-blue-700 hover:bg-blue-50 disabled:opacity-50">
                                {bajando === a.id ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
                              </button>
                            </p>
                            {enEdicion && (
                              <div className="pl-24 py-1 flex items-center gap-2 flex-wrap font-sans">
                                <label className="text-[10px] text-gray-500">Hora
                                  <input value={editando.hora} onChange={(e) => setEditando((x) => ({ ...x, hora: e.target.value }))}
                                    placeholder="16:50" className="ml-1 w-14 border rounded px-1 py-0.5 text-[11px] font-mono" />
                                </label>
                                <label className="text-[10px] text-gray-500">Fin
                                  <input value={editando.hora_fin} onChange={(e) => setEditando((x) => ({ ...x, hora_fin: e.target.value }))}
                                    placeholder="16:53" className="ml-1 w-14 border rounded px-1 py-0.5 text-[11px] font-mono" />
                                </label>
                                <label className="text-[10px] text-gray-500">Fecha
                                  <input value={editando.fecha} onChange={(e) => setEditando((x) => ({ ...x, fecha: e.target.value }))}
                                    placeholder="2026-09-10" className="ml-1 w-24 border rounded px-1 py-0.5 text-[11px] font-mono" />
                                </label>
                                <label className="text-[10px] text-gray-500 flex-1 min-w-[200px] flex items-center">Qué muestra
                                  <input value={editando.que_muestra} onChange={(e) => setEditando((x) => ({ ...x, que_muestra: e.target.value }))}
                                    onKeyDown={(e) => { if (e.key === 'Enter') guardarEdicion() }}
                                    placeholder="Lluvia en el sitio" maxLength={100} className="ml-1 flex-1 border rounded px-1 py-0.5 text-[11px]" />
                                </label>
                                <button onClick={guardarEdicion} disabled={guardando === a.id}
                                  className="flex items-center gap-1 text-[11px] font-medium px-2 py-0.5 rounded bg-blue-700 text-white hover:bg-blue-800 disabled:opacity-40">
                                  {guardando === a.id ? <Loader2 size={11} className="animate-spin" /> : <Check size={11} />} Guardar
                                </button>
                                <button onClick={() => setEditando(null)} className="text-[11px] text-gray-500 hover:underline">Cancelar</button>
                                <span className="text-[10px] text-gray-400 w-full">La hora es la del letrero del video (no la de subida). Video e imagen con la misma hora comparten número.</span>
                              </div>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  ))}
                </div>
              ))}
            </>
          )}
        </div>
      </div>

      <p className="text-[11px] text-gray-400">
        Los soportes salen de lo que subes en cada exclusión: los <b>videos</b> del grupo en el 3.2 A y los
        <b> correos (.msg)</b> del Centro Operativo en el 3.2 B. En el ZIP van numerados en orden de hora
        («Evidencia_1_Video_16-50_a_16-53_Lluvia_en_el_sitio.mp4»), cada carpeta con su 00_RESUMEN (observación +
        lista) y la raíz con 00_INDICE. Los audios y los duplicados quedan fuera por defecto; con el ojo decides.
        El botón azul arma el paquete que se envía (solo casos que quedaron en SI); «Solo anulaciones» y
        «Solo tiempos» sacan un indicador a la vez; el paquete completo es solo para revisión interna.
      </p>

      {/* Confirmación del paquete completo: hay que escribir "acepto" para que no pase en piloto automático */}
      {confirmar && (
        <div className="fixed inset-0 z-[70] bg-black/70 flex items-center justify-center p-4" onClick={cerrarConfirmar}>
          <div className="bg-white rounded-xl shadow-2xl max-w-md w-full overflow-hidden" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2 px-4 py-3 border-b bg-amber-50">
              <AlertTriangle size={18} className="text-amber-600 shrink-0" />
              <p className="text-sm font-semibold text-gray-800">Vas a exportar el paquete completo</p>
            </div>
            <div className="p-4 space-y-3">
              <p className="text-sm text-gray-700">
                Este ZIP incluye también los soportes de casos que NO quedaron excluibles y los que dejaste fuera.
                Es solo para revisión interna: si es para enviar, usa «Exportar excluibles».
              </p>
              <p className="text-xs text-gray-500">Escribe <b>acepto</b> para confirmar.</p>
              <input autoFocus value={textoOk} onChange={(e) => setTextoOk(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && confirmarOk) { cerrarConfirmar(); exportar(false) } }}
                placeholder="acepto"
                className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-amber-400" />
            </div>
            <div className="flex justify-end gap-2 px-4 py-3 border-t bg-gray-50">
              <button onClick={cerrarConfirmar} className="text-sm px-3 py-1.5 rounded-lg text-gray-600 hover:bg-gray-100">
                Cancelar
              </button>
              <button onClick={() => { cerrarConfirmar(); exportar(false) }} disabled={!confirmarOk}
                className="text-sm font-medium px-3 py-1.5 rounded-lg bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-40">
                Exportar el completo
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Visor de video / imagen — el archivo se trae con el token y se reproduce aquí */}
      {ver && (
        <div className="fixed inset-0 z-[70] bg-black/70 flex items-center justify-center p-4" onClick={cerrarVer}>
          <div className="bg-white rounded-xl shadow-2xl max-w-4xl w-full max-h-[92vh] flex flex-col overflow-hidden"
            onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2 px-4 py-2.5 border-b">
              <Video size={15} className="text-blue-600 shrink-0" />
              <p className="text-sm font-medium text-gray-800 truncate flex-1">{ver.nombre}</p>
              {ver.archivo && (
                <button onClick={() => descargar(ver.archivo)}
                  className="flex items-center gap-1 text-xs text-blue-700 hover:underline shrink-0">
                  <Download size={13} /> Descargar
                </button>
              )}
              <button onClick={cerrarVer} className="text-gray-400 hover:text-gray-700 shrink-0" title="Cerrar (Esc)">
                <X size={16} />
              </button>
            </div>
            <div className="bg-black flex items-center justify-center min-h-[200px]">
              {ver.cargando ? (
                <p className="text-gray-300 text-sm p-10 flex items-center gap-2">
                  <Loader2 size={16} className="animate-spin" /> Cargando…
                </p>
              ) : ver.tipo === 'video' ? (
                <video src={ver.url} controls autoPlay className="max-h-[78vh] w-full"
                  onError={() => toast.error('El navegador no puede reproducir este formato — descárgalo')} />
              ) : (
                <img src={ver.url} alt={ver.nombre} className="max-h-[78vh] object-contain" />
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
