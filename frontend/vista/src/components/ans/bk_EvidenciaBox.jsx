import { useState, useEffect, useCallback, useRef } from 'react'
import { Search, Video, Mail, Upload, Trash2, ArrowRight, Loader2, CheckCircle2, Download, Sparkles, ScanSearch, FileText, Eye, EyeOff } from 'lucide-react'
import api from '../../utils/api'

const EXCL_CHIP = { SI: 'bg-green-100 text-green-800 border-green-300', NO: 'bg-red-100 text-red-800 border-red-300' }
const esCorreoAnalizable = (nombre) => /\.(msg|eml|pdf|png|jpe?g|gif|bmp|webp)$/i.test(nombre || '')
const esArchivoCorreo = (nombre) => /\.(msg|eml|pdf)$/i.test(nombre || '')   // soporte documental (correo o PDF)
const esArchivoPdf = (nombre) => /\.pdf$/i.test(nombre || '')

// Recoge el SOPORTE de una exclusión y lo deja amarrado a la celda. Dos sabores:
//  · tipo="video" (3.2 A): el video del frente de trabajo. Como el sistema sabe la hora planeada y
//    la real, muestra la FRANJA donde estaría en el grupo. Es el paso 1: si aparece, no hay que
//    preguntarle a nadie; si no, el botón «No encontré nada →» abre el paso 2 (preguntar).
//    Rara vez el soporte llega como CORREO (.msg/.eml) — directriz de ENEL, vía cerrada de la
//    alcaldía…: también se acepta aquí, se guarda como evidencia de la MISMA celda y el análisis
//    lo lee JUNTO con el audio de los videos para proponer la observación.
//  · tipo="correo" (3.2 B): el correo del Centro Operativo que consigue el profesional de apoyo.
//    No hay franja (nada se ejecutó); se muestra junto a los contactos, sin ocultar nada.

const kb = (n) => (n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`)

const TEMAS = {
  video: {
    Icono: Video, titulo: 'Paso 1 · Busca primero en el grupo', accept: 'video/*,image/*,.msg,.eml,.pdf',
    cta: 'Encontré el video — subirlo', ctaMas: 'Subir otro',
    ok: 'Con este soporte ya puedes justificar la exclusión.',
    pie: 'Normalmente son 2 videos: uno al parar y otro al retomar. Si el soporte llegó por correo o es un PDF (pasa poco), adjúntalo también: arrástralo desde Outlook o súbelo (.msg/.eml/PDF).',
    nota: 'Hallado en el grupo de WhatsApp',
    box: 'border-sky-200 bg-sky-50/60', head: 'text-sky-700', fecha: 'text-sky-800',
    btn: 'bg-sky-600 hover:bg-sky-700', dl: 'hover:text-sky-700',
  },
  correo: {
    Icono: Mail, titulo: 'Soporte · El correo del Centro Operativo', accept: '.msg,.pdf,.eml,image/*',
    cta: 'Adjuntar el correo', ctaMas: 'Adjuntar otro',
    ok: 'Con el correo ya puedes justificar la exclusión.',
    pie: 'Arrastra el correo aquí directo desde Outlook — o súbelo (.msg, .eml, PDF o pantallazo).',
    nota: 'Correo del Centro Operativo',
    box: 'border-violet-200 bg-violet-50/60', head: 'text-violet-700', fecha: 'text-violet-800',
    btn: 'bg-violet-600 hover:bg-violet-700', dl: 'hover:text-violet-700',
  },
}

export default function EvidenciaBox({ regId, campo, apiBase, apiRoot, tipo = 'video', busqueda,
  onNoEncontrado, esDev = false, onAprobado }) {
  const t = TEMAS[tipo] || TEMAS.video
  const [evid, setEvid] = useState([])
  const [subiendo, setSubiendo] = useState(false)
  const [err, setErr] = useState(null)
  const [analisis, setAnalisis] = useState(null)   // {consulta_id, relacionado, excluible, razon, razonamiento, …}
  const [analizando, setAnalizando] = useState(false)
  const [aprobando, setAprobando] = useState(false)
  const inputRef = useRef(null)
  const inputCorreoRef = useRef(null)   // 3.2 A: selector aparte solo-correo (.msg/.eml)

  // ── Arrastrar y soltar: el correo se suelta directo desde Outlook ──
  // El contador evita el parpadeo al pasar sobre los hijos (dragleave dispara por cada uno).
  const [drag, setDrag] = useState(false)
  const dragN = useRef(0)
  const onDragEnter = (e) => { e.preventDefault(); dragN.current += 1; setDrag(true) }
  const onDragOver = (e) => { e.preventDefault() }
  const onDragLeave = (e) => { e.preventDefault(); dragN.current -= 1; if (dragN.current <= 0) { dragN.current = 0; setDrag(false) } }
  const onDrop = (e) => {
    e.preventDefault(); e.stopPropagation()
    dragN.current = 0; setDrag(false)
    const files = Array.from(e.dataTransfer?.files || [])
    if (files.length) { subir(files); return }
    // Outlook clásico no entrega el archivo al navegador (limitación de Windows, no de la app)
    setErr('Outlook no soltó el archivo aquí (le pasa al Outlook clásico con los navegadores). ' +
      'Truco: arrástralo primero al escritorio o a una carpeta, y de ahí aquí.')
  }

  const analizar = async (evidId) => {
    setAnalizando(true); setErr(null); setAnalisis(null)
    try {
      const r = await api.post('/correo/analizar',
        { reg_id: regId, campo, evidencia_id: evidId }, { baseURL: apiBase })
      if (r.data.error) setErr(`IA: ${r.data.error}`)
      else setAnalisis(r.data)
    } catch (e) {
      setErr(e?.response?.data?.detail || 'No se pudo analizar el correo')
    } finally { setAnalizando(false) }
  }

  // Video (3.2 A): analiza TODA la evidencia de la celda junta — el AUDIO de los videos (suelen
  // ser 2: uno al parar, otro al retomar; la explicación suele repartirse entre ambos) y, si hay,
  // el CORREO soporte (.msg/.eml — el backend lo parsea y lo cruza en la misma llamada).
  // `forzar`: "Reinterpretar" — ignora la transcripción cacheada y la vuelve a intentar, por si
  // algún video quedó sin texto por un fallo transitorio (no por falta real de audio).
  const analizarVideo = async (forzar = false) => {
    setAnalizando(true); setErr(null); setAnalisis(null)
    try {
      const r = await api.post('/video/analizar', { reg_id: regId, campo, forzar }, { baseURL: apiBase })
      if (r.data.error) setErr(`IA: ${r.data.error}`)
      else setAnalisis(r.data)
    } catch (e) {
      setErr(e?.response?.data?.detail || 'No se pudo analizar la evidencia (video/correo)')
    } finally { setAnalizando(false) }
  }

  const aprobar = async () => {
    if (!analisis?.consulta_id) return
    setAprobando(true); setErr(null)
    try {
      const r = await api.post(`/consulta/${analisis.consulta_id}/aplicar`,
        { razon: analisis.razon }, { baseURL: apiBase })
      onAprobado?.(r.data)
      setAnalisis((a) => ({ ...a, aplicado: true }))
    } catch (e) {
      setErr(e?.response?.data?.detail || 'No se pudo aplicar')
    } finally { setAprobando(false) }
  }

  const cargar = useCallback(async () => {
    try {
      const r = await api.get('/evidencia', { baseURL: apiBase, params: { reg_id: regId, campo } })
      setEvid(r.data || [])
    } catch { /* sin evidencia aún */ }
  }, [regId, campo, apiBase])

  // Recupera el último análisis (correo o video) para que el razonamiento NO se pierda al cerrar
  // y reabrir el popup.
  const cargarUltimo = useCallback(async () => {
    if (!esDev || (tipo !== 'correo' && tipo !== 'video')) return
    try {
      const ruta = tipo === 'video' ? '/video/ultimo' : '/correo/ultimo'
      const r = await api.get(ruta, { baseURL: apiBase, params: { reg_id: regId, campo } })
      if (r.data?.existe) setAnalisis(r.data)
    } catch { /* sin análisis previo */ }
  }, [tipo, esDev, regId, campo, apiBase])

  useEffect(() => { cargar() }, [cargar])
  useEffect(() => { cargarUltimo() }, [cargarUltimo])

  const subir = async (files) => {
    if (!files?.length) return
    setSubiendo(true); setErr(null)
    try {
      const fd = new FormData()
      fd.append('reg_id', regId)
      fd.append('campo', campo)
      // En el 3.2 A la nota dice «hallado en el grupo», pero un correo/PDF no sale de WhatsApp
      const soloDocs = tipo === 'video' && Array.from(files).every((f) => esArchivoCorreo(f.name))
      const todosPdf = soloDocs && Array.from(files).every((f) => esArchivoPdf(f.name))
      fd.append('nota', soloDocs ? (todosPdf ? 'PDF soporte' : 'Correo soporte') : t.nota)
      Array.from(files).forEach((f) => fd.append('archivos', f))
      await api.post('/evidencia', fd, { baseURL: apiBase })
      await cargar()
    } catch (e) {
      setErr(e?.response?.data?.detail || 'No se pudo subir')
    } finally {
      setSubiendo(false)
      if (inputRef.current) inputRef.current.value = ''
      if (inputCorreoRef.current) inputCorreoRef.current.value = ''
    }
  }

  const borrar = async (id) => {
    try { await api.delete(`/evidencia/${id}`, { baseURL: apiBase }); await cargar() } catch { /* nada */ }
  }

  // PAQUETE PARA ENEL (14 oct 2026): el ojo dice si este soporte va en el ZIP que se le enseña a ENEL.
  // Hora y «qué muestra» se corrigen en la pestaña Evidencias (ahí se ve el nombre final).
  const alternar = async (e) => {
    try { await api.patch(`/evidencia/${e.id}`, { en_paquete: !e.en_paquete }, { baseURL: apiBase }); await cargar() } catch { /* nada */ }
  }

  const { Icono } = t
  return (
    <div className={`border rounded-lg p-2 space-y-2 relative ${t.box} ${drag ? 'ring-2 ring-violet-500 ring-offset-1' : ''}`}
      onDragEnter={onDragEnter} onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop}>
      {drag && (
        <div className="absolute inset-0 z-10 rounded-lg bg-violet-100/90 border-2 border-dashed border-violet-500 flex items-center justify-center pointer-events-none">
          <p className="text-xs font-semibold text-violet-800 flex items-center gap-1.5">
            <Upload size={14} /> Suelta aquí para adjuntar{tipo === 'correo' ? ' el correo' : ' el video o el correo'}
          </p>
        </div>
      )}
      <p className={`text-[10px] uppercase tracking-wide font-semibold flex items-center gap-1 ${t.head}`}>
        <Search size={11} /> {t.titulo}
      </p>

      {/* Dónde buscar: el grupo de la zona y de quién sería el video, a la mano —
          sin tener que ir a mirar las celdas de zona/responsable de la fila */}
      {busqueda?.caso && (
        <div className="bg-sky-50 border border-sky-200 rounded p-2">
          <p className="text-[11px] text-gray-800">
            Busca en el <b>grupo de WhatsApp de {busqueda.caso.zona}</b>
            {busqueda.caso.pdl && <> · orden <b className="font-mono">{busqueda.caso.pdl}</b></>}
          </p>
          {(busqueda.caso.lider || busqueda.caso.supervisor) && (
            <p className="text-[10px] text-gray-600 mt-0.5 leading-relaxed">
              El video lo habría subido la cuadrilla de <b>{busqueda.caso.lider || '—'}</b>
              {busqueda.caso.supervisor && <> · supervisor {busqueda.caso.supervisor}</>}.
            </p>
          )}
        </div>
      )}

      {/* Cuándo mirar (solo video) — calculado de la hora planeada vs la maniobra real */}
      {busqueda?.tramos?.map((tr, i) => (
        <div key={i} className="bg-white border border-sky-200 rounded p-2">
          <p className="text-[11px] text-gray-800">
            {tr.dia_hasta ? (
              <>Del <b>{tr.dia}</b> a las <b className={t.fecha}>{tr.desde}</b>, hasta el{' '}
                <b>{tr.dia_hasta}</b> a las <b className={t.fecha}>{tr.hasta}</b></>
            ) : (
              <><b>{tr.dia}</b>, entre las <b className={t.fecha}>{tr.desde}</b> y las{' '}
                <b className={t.fecha}>{tr.hasta}</b></>
            )}
          </p>
          <p className="text-[10px] text-gray-500 mt-0.5 leading-relaxed">{tr.por_que}</p>
        </div>
      ))}

      {busqueda?.que_buscar && (
        <p className="text-[10px] text-gray-600 leading-relaxed"><b>Qué buscar:</b> {busqueda.que_buscar}</p>
      )}

      {evid.length > 0 && (
        <div className="space-y-1">
          {evid.map((e) => {
            const IconoFila = esArchivoPdf(e.nombre) ? FileText
              : tipo === 'video' && esArchivoCorreo(e.nombre) ? Mail : Icono
            return (
            <div key={e.id} className="flex items-center gap-1.5 bg-white border border-green-200 rounded px-1.5 py-1">
              <IconoFila size={12} className="text-green-600 shrink-0" />
              <span className={`text-[10px] truncate flex-1 ${e.en_paquete === false ? 'text-gray-400 line-through' : 'text-gray-700'}`}
                title={e.que_muestra ? `Qué muestra: ${e.que_muestra}` : undefined}>{e.nombre}</span>
              {e.que_muestra && <span className="text-[10px] text-sky-700 truncate max-w-[110px] shrink-0" title={`Qué muestra: ${e.que_muestra}`}>{e.que_muestra}</span>}
              <span className="text-[10px] text-gray-400 shrink-0">{kb(e.tamano || 0)}</span>
              <button onClick={() => alternar(e)} className={`shrink-0 ${e.en_paquete === false ? 'text-gray-300 hover:text-green-700' : 'text-green-600 hover:text-amber-600'}`}
                title={e.en_paquete === false ? 'NO va en el paquete para ENEL (clic para incluirla)' : 'Va en el paquete para ENEL (clic para dejarla fuera)'}>
                {e.en_paquete === false ? <EyeOff size={12} /> : <Eye size={12} />}
              </button>
              {/* Análisis de correo dedicado: en el 3.2 B para .msg/.eml/pantallazos; en el
                  3.2 A solo para .msg/.eml (una imagen ahí es foto del frente, no un correo) */}
              {esDev && (tipo === 'correo' ? esCorreoAnalizable(e.nombre) : esArchivoCorreo(e.nombre)) && (
                <button onClick={() => analizar(e.id)} disabled={analizando}
                  className="flex items-center gap-0.5 text-[10px] font-medium px-1.5 py-0.5 rounded bg-violet-600 text-white hover:bg-violet-700 disabled:opacity-40 shrink-0"
                  title="Que la IA cruce el correo con esta orden">
                  {analizando ? <Loader2 size={10} className="animate-spin" /> : <ScanSearch size={10} />} Analizar
                </button>
              )}
              <a href={`${apiRoot}/evidencia/${e.id}/descargar`} target="_blank" rel="noreferrer"
                className={`text-gray-400 shrink-0 ${t.dl}`} title="Descargar"><Download size={12} /></a>
              <button onClick={() => borrar(e.id)} className="text-gray-300 hover:text-red-600 shrink-0" title="Quitar">
                <Trash2 size={12} />
              </button>
            </div>
            )
          })}
          <p className="text-[10px] text-green-700 flex items-center gap-1">
            <CheckCircle2 size={11} /> {t.ok}
          </p>

          {/* Video (3.2 A): un solo botón que analiza TODA la evidencia de la celda junta — el
              AUDIO de los videos (la explicación suele repartirse entre el que para y el que
              retoma) y, si hay, el CORREO soporte (texto + pantallazos). */}
          {esDev && tipo === 'video' && (() => {
            const hayCorreo = evid.some((e) => esArchivoCorreo(e.nombre))
            const hayVideo = evid.some((e) => !esArchivoCorreo(e.nombre))
            const soloPdf = !hayVideo && evid.every((e) => esArchivoPdf(e.nombre))
            const cta = !hayVideo ? (soloPdf ? 'Leer el PDF y proponer la observación' : 'Leer el correo y proponer la observación')
              : hayCorreo ? 'Analizar toda la evidencia y proponer la observación'
                : 'Escuchar el audio y proponer la observación'
            return (
              <button onClick={() => analizarVideo(!!analisis)} disabled={analizando}
                className="flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded bg-sky-700 text-white hover:bg-sky-800 disabled:opacity-40"
                title={analisis ? 'Vuelve a pedirle a la IA que decida (no repite la transcripción, ya quedó guardada)' : undefined}>
                {analizando ? <Loader2 size={12} className="animate-spin" /> : <ScanSearch size={12} />}
                {analizando ? 'Analizando la evidencia…' : analisis ? 'Reinterpretar' : cta}
              </button>
            )
          })()}
        </div>
      )}

      {/* Resultado del análisis IA (correo leído o video escuchado). La IA SIEMPRE decide SI/NO;
          tú verificas y autorrellenas. */}
      {analisis && (
        <div className="border border-violet-200 rounded-lg p-2 bg-violet-50/60 space-y-1.5">
          <p className="text-[10px] uppercase tracking-wide text-violet-700 font-semibold flex items-center gap-1">
            <Sparkles size={11} /> Decisión de la IA
            {analisis.correos ? ` · leyó ${analisis.correos} correo${analisis.correos > 1 ? 's' : ''}` : ''}
            {analisis.pdfs ? ` · leyó ${analisis.pdfs} PDF${analisis.pdfs > 1 ? 's' : ''}` : ''}
            {analisis.imagenes ? ` · leyó ${analisis.imagenes} imagen${analisis.imagenes > 1 ? 'es' : ''}` : ''}
            {analisis.videos ? ` · escuchó ${analisis.videos} video${analisis.videos > 1 ? 's' : ''}` : ''}
          </p>

          <div className="flex items-center gap-2 flex-wrap">
            <span className={`text-[11px] font-semibold px-1.5 py-0.5 rounded border ${EXCL_CHIP[analisis.excluible] || 'bg-gray-100'}`}>
              {analisis.excluible === 'SI' ? 'Excluible' : 'No excluible'}{analisis.categoria ? ` · ${analisis.categoria}` : ''}
            </span>
            {analisis.relacionado === 'NO' && (
              <span className="text-[10px] text-amber-700 bg-amber-100 px-1.5 py-0.5 rounded">el correo no corresponde a esta orden</span>
            )}
          </div>

          <p className="text-[11px] text-gray-700 leading-relaxed"><b>Por qué:</b> {analisis.razonamiento}</p>

          {analisis.transcripcion && (
            <details className="text-[10px] text-gray-500">
              <summary className="cursor-pointer hover:text-gray-700">Ver la transcripción del audio</summary>
              <p className="mt-1 p-1.5 bg-white border border-gray-200 rounded italic leading-relaxed whitespace-pre-wrap">
                «{analisis.transcripcion}»
              </p>
            </details>
          )}

          {analisis.razon && (
            <div>
              <p className="text-[10px] text-gray-500">Se escribirá en la observación (edítala si quieres):</p>
              <textarea value={analisis.razon} rows={2}
                onChange={(ev) => setAnalisis((a) => ({ ...a, razon: ev.target.value }))}
                className="w-full text-[11px] border border-gray-300 rounded p-1 leading-relaxed" />
            </div>
          )}

          {analisis.aplicado ? (
            <p className="text-[11px] text-green-700 flex items-center gap-1 font-medium">
              <CheckCircle2 size={12} /> Aplicado al ANS.
            </p>
          ) : analisis.aprobable && (
            <div className="flex items-center gap-2">
              <button onClick={aprobar} disabled={aprobando}
                className="flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded bg-violet-700 text-white hover:bg-violet-800 disabled:opacity-40">
                {aprobando ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
                {tipo === 'video' ? 'Autorrellenar la observación' : 'Autorrellenar el ANS'}
              </button>
              <span className="text-[10px] text-gray-400">
                {tipo === 'video'
                  ? 'Solo copia la observación — el excluible SI/NO lo decides tú en la celda.'
                  : 'Verifica el correo antes de aprobar.'}
              </span>
            </div>
          )}
        </div>
      )}

      {err && <p className="text-[10px] text-red-600">{err}</p>}

      <div className="flex items-center gap-1.5 flex-wrap">
        <input ref={inputRef} type="file" multiple accept={t.accept} className="hidden"
          onChange={(e) => subir(e.target.files)} />
        <button onClick={() => inputRef.current?.click()} disabled={subiendo}
          className={`flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded text-white disabled:opacity-40 ${t.btn}`}>
          {subiendo ? <Loader2 size={12} className="animate-spin" /> : <Upload size={12} />}
          {subiendo ? 'Subiendo…' : evid.length ? t.ctaMas : t.cta}
        </button>
        {/* 3.2 A · caso raro: el soporte llegó por CORREO o es un PDF. Botón visible aparte — el
            selector del video también acepta .msg/.eml/.pdf, pero nadie lo adivina desde ahí. */}
        {tipo === 'video' && (
          <>
            <input ref={inputCorreoRef} type="file" multiple accept=".msg,.eml,.pdf" className="hidden"
              onChange={(e) => subir(e.target.files)} />
            <button onClick={() => inputCorreoRef.current?.click()} disabled={subiendo}
              className="flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded border border-violet-300 bg-white text-violet-700 hover:bg-violet-50 disabled:opacity-40"
              title="Caso raro: el soporte llegó por correo o es un PDF (directriz de ENEL, vía cerrada de la alcaldía…). Adjunta el .msg/.eml/.pdf — o arrástralo aquí directo desde Outlook.">
              <Mail size={12} /> {evid.some((e) => esArchivoCorreo(e.nombre)) ? 'Otro correo/PDF' : 'Adjuntar correo o PDF'}
            </button>
          </>
        )}
        {onNoEncontrado && (
          <button onClick={onNoEncontrado}
            className="flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-gray-300 bg-white text-gray-600 hover:bg-gray-50">
            No encontré nada <ArrowRight size={12} />
          </button>
        )}
      </div>
      <p className="text-[10px] text-gray-400">{t.pie}</p>
    </div>
  )
}
