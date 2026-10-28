// «Ver justificación» de una OBSERVACIÓN (requisito 2026-09-03). La celda de
// observación del 3.2 A / 3.2 B / 3.4 muchas veces no la escribe una persona: la redacta
// Auditoría IA (WhatsApp), la lectura del correo en Correos ANS o el chat de investigación.
// Lo primero que enseña este popup es CÓMO SE LLEGÓ A LA CONCLUSIÓN (ajuste 2026-09-03):
// el razonamiento de la IA y el correo con las frases clave en amarillo — adjuntar el
// correo sin mostrar qué lo sustenta no justifica nada. Después, de dónde salió el texto
// (lo dice el historial de escrituras), las demás fuentes, las evidencias y el historial.
import { useEffect, useState } from 'react'
import { X, FileSearch, Mail, MessageCircle, Sparkles, Paperclip, History, ChevronDown, ChevronRight,
  Video, Quote, AlertTriangle, Lightbulb, Play, Loader2 } from 'lucide-react'
import api from '../../utils/api'
import CoberturaRapida from './CoberturaRapida'
import ChatWhatsApp, { chatDeTurnos } from './ChatWhatsApp'
import CorreoCitado, { citasEncontradas, codigosCaso, fragmentosPdl } from './CorreoCitado'

const ORIGEN_CLS = {
  auditoria_ia: 'bg-violet-100 text-violet-800 border-violet-300',
  correo:       'bg-sky-100 text-sky-800 border-sky-300',
  chat:         'bg-indigo-100 text-indigo-800 border-indigo-300',
  bitacora:     'bg-amber-100 text-amber-800 border-amber-300',
  evidencia:    'bg-amber-100 text-amber-800 border-amber-300',
  evidencia_ia: 'bg-violet-100 text-violet-800 border-violet-300',
  manual:       'bg-emerald-100 text-emerald-800 border-emerald-300',
  importe:      'bg-gray-100 text-gray-700 border-gray-300',
  bot_wa:       'bg-green-100 text-green-800 border-green-300',
}
const ORIGEN_DEFAULT = 'bg-gray-100 text-gray-600 border-gray-300'
const fmt = (iso) => (iso ? String(iso).slice(0, 16).replace('T', ' ') : '—')
const ESTADO_CLS = {
  aplicado: 'bg-emerald-100 text-emerald-800', propuesto: 'bg-amber-100 text-amber-800',
  vinculado: 'bg-emerald-100 text-emerald-800', nuevo: 'bg-amber-100 text-amber-800',
  resuelta: 'bg-emerald-100 text-emerald-800', abierta: 'bg-amber-100 text-amber-800',
}
const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toUpperCase()
const contiene = (texto, prop) => !!texto && !!prop && norm(texto).includes(norm(prop))

// Reproduce un video del caso. El <video> no puede mandar el header Authorization → se baja
// el blob con axios y se reproduce desde un object-URL; solo al hacer clic (los videos pesan).
// `ruta` relativa al apiBase: /evidencia/{id}/descargar (ya copiado) o /video-caso/{msg_id}.
function VideoCaso({ ruta, apiBase, etiqueta = 'Ver video' }) {
  const [url, setUrl] = useState(null)
  const [cargando, setCargando] = useState(false)
  const [err, setErr] = useState(null)
  const cargar = async () => {
    setCargando(true); setErr(null)
    try {
      const r = await api.get(ruta, { baseURL: apiBase, responseType: 'blob' })
      setUrl(URL.createObjectURL(r.data))
    } catch { setErr('No se pudo cargar el video') } finally { setCargando(false) }
  }
  if (url) return <video src={url} controls autoPlay className="w-full max-w-md rounded-lg bg-black mt-1" />
  return (
    <span className="inline-flex items-center gap-2">
      <button onClick={cargar} disabled={cargando}
        className="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium bg-gray-900 text-white rounded hover:bg-gray-700 disabled:opacity-50">
        {cargando ? <Loader2 size={11} className="animate-spin" /> : <Play size={11} />} {cargando ? 'Cargando…' : etiqueta}
      </button>
      {err && <span className="text-[11px] text-red-600">{err}</span>}
    </span>
  )
}

function Chip({ children, cls = 'bg-gray-100 text-gray-700' }) {
  return <span className={`px-2 py-0.5 rounded-full text-[11px] font-medium ${cls}`}>{children}</span>
}

// Tarjeta plegable por fuente. La que ORIGINÓ el texto de la celda va marcada.
function Seccion({ icono, titulo, sub, estado, esOrigen, coincide, abierta, children }) {
  const [open, setOpen] = useState(!!abierta)
  return (
    <div className={`border rounded-xl ${esOrigen ? 'border-violet-400 ring-1 ring-violet-200' : 'border-gray-200'}`}>
      <button onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-50 rounded-xl">
        {open ? <ChevronDown size={14} className="text-gray-400 shrink-0" /> : <ChevronRight size={14} className="text-gray-400 shrink-0" />}
        <span className="text-gray-500 shrink-0">{icono}</span>
        <span className="font-semibold text-gray-800 shrink-0">{titulo}</span>
        {sub && <span className="text-xs text-gray-500 truncate">{sub}</span>}
        <span className="ml-auto flex items-center gap-1 shrink-0">
          {estado && <Chip cls={ESTADO_CLS[estado] || 'bg-gray-100 text-gray-600'}>{estado}</Chip>}
          {esOrigen && <Chip cls="bg-violet-600 text-white">origen del texto</Chip>}
          {!esOrigen && coincide && <Chip cls="bg-violet-100 text-violet-800">mismo texto</Chip>}
        </span>
      </button>
      {open && <div className="px-3 pb-3 pt-1 space-y-2 border-t border-gray-100">{children}</div>}
    </div>
  )
}

function TextoPropuesto({ titulo, texto, coincide }) {
  if (!texto) return null
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wide text-gray-400 font-semibold mb-0.5">
        {titulo}{coincide && <span className="ml-1 normal-case text-violet-700 font-medium">· es el texto de la celda</span>}
      </p>
      <p className={`font-mono text-xs whitespace-pre-wrap rounded px-2 py-1.5 border ${coincide ? 'bg-violet-50 border-violet-200' : 'bg-gray-50 border-gray-200'}`}>{texto}</p>
    </div>
  )
}

function Propuesta({ excluible, revision, categoria, causa, causaTipo, confianza, veredictoIa, relacionado }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {relacionado && <Chip cls={relacionado === 'SI' ? 'bg-emerald-100 text-emerald-800' : 'bg-red-100 text-red-700'}>relacionado con la orden: {relacionado}</Chip>}
      {revision
        ? <Chip cls="bg-orange-100 text-orange-800">Revisión — la decide el ING</Chip>
        : excluible != null && <Chip cls={excluible === 'SI' ? 'bg-emerald-100 text-emerald-800' : 'bg-red-100 text-red-700'}>propone excluible: {excluible}</Chip>}
      {revision && veredictoIa && <Chip>inclinación IA: {veredictoIa}</Chip>}
      {categoria && <Chip>{categoria}</Chip>}
      {causaTipo && <Chip>causa {causaTipo}</Chip>}
      {confianza && <Chip>confianza {confianza}</Chip>}
      {causa && <span className="text-xs text-gray-800 w-full"><b>Causa:</b> {causa}</span>}
    </div>
  )
}

function Revision({ rev }) {
  if (!rev) return null
  return (
    <p className="text-xs text-orange-900 bg-orange-50 border border-orange-200 rounded px-2 py-1.5">
      <AlertTriangle size={12} className="inline mr-1 -mt-0.5" />
      <b>Por qué está en Revisión:</b> {rev.motivo}
    </p>
  )
}

function Citas({ citas }) {
  if (!citas?.length) return null
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wide text-gray-400 font-semibold mb-0.5">
        <Quote size={11} className="inline mr-1 -mt-0.5" />Frases del correo en las que se apoya
      </p>
      <ul className="space-y-1">
        {citas.map((q, i) => <li key={i} className="text-xs bg-yellow-100 border border-yellow-300 rounded px-2 py-1 text-gray-800">«{q}»</li>)}
      </ul>
    </div>
  )
}

// Los correos del caso con texto (evidencias .msg/.eml parseadas + bandeja Correos ANS), sin
// duplicar el que está en las dos, y ordenados: primero el que la lectura IA leyó de verdad,
// luego el que nombra el PDL del caso.
function correosDelCaso(data, apiBase, citas, evidenciaId, pdl) {
  const evid = (data.evidencias || []).filter((e) => e.correo?.cuerpo)
  const evidIds = new Set(evid.map((e) => e.id))
  const lista = [
    ...evid.map((e) => ({ key: `ev${e.id}`, evId: e.id, correo: e.correo,
      descarga: `${apiBase}/evidencia/${e.id}/descargar`, titulo: 'Correo adjunto como evidencia' })),
    ...(data.correos || []).filter((c) => c.cuerpo && !(c.evidencia_id && evidIds.has(c.evidencia_id)))
      .map((c) => ({ key: `co${c.id}`, coId: c.id,
        correo: { asunto: c.asunto, de: c.remitente, fecha: c.fecha_correo, cuerpo: c.cuerpo },
        descarga: c.evidencia_id ? `${apiBase}/evidencia/${c.evidencia_id}/descargar` : null,
        titulo: 'Correo de la bandeja Correos ANS' })),
  ]
  const puntaje = (x) => (x.evId && x.evId === evidenciaId ? 1000 : 0)
    + (fragmentosPdl(x.correo.cuerpo, pdl).length ? 100 : 0) + citasEncontradas(x.correo.cuerpo, citas)
  return lista.sort((a, b) => puntaje(b) - puntaje(a))
}

// El bloque principal: CÓMO se llegó a la conclusión, según el tipo de lectura.
function ComoSeLlego({ lectura, data, apiBase }) {
  if (!lectura) return null
  if (lectura.tipo === 'auditoria_ia') {
    const { a } = lectura; const ia = a.ia || {}
    return (
      <div className="space-y-2">
        <Propuesta excluible={ia.excluible_sugerido} revision={ia.revision} categoria={ia.categoria}
          causa={ia.causa} causaTipo={ia.causa_tipo} confianza={ia.confianza} />
        {ia.resumen && <p className="text-xs text-gray-800 leading-relaxed">{ia.resumen}</p>}
        <Revision rev={ia.revision} />
        {ia.cobertura_desfase && <p className="text-xs text-gray-700"><b>Cobertura del desfase:</b> {ia.cobertura_desfase}</p>}
        <CoberturaRapida cov={ia.cobertura_calc} />
        {(ia.linea_tiempo || []).length > 0 && (
          <div>
            <p className="text-[10px] uppercase tracking-wide text-gray-400 font-semibold mb-0.5">Línea de tiempo reconstruida</p>
            <ul className="text-xs text-gray-700 space-y-0.5">
              {ia.linea_tiempo.map((e, i) => <li key={i}><span className="font-mono">{e.hora}</span> · {e.hecho} <span className="text-gray-400">({e.fuente})</span></li>)}
            </ul>
          </div>
        )}
        <div>
          <p className="text-[10px] uppercase tracking-wide text-gray-400 font-semibold mb-0.5">
            <Video size={11} className="inline mr-1 -mt-0.5" />Videos que sustentan ({(a.videos || []).length} de {a.n_videos_revisados} revisados · {a.n_eventos} mensajes del grupo)
          </p>
          {(a.videos || []).length ? (
            <ul className="text-xs text-gray-700 space-y-1">
              {a.videos.map((v, i) => (
                <li key={i} className="border rounded px-2 py-1 bg-yellow-50 border-yellow-200">
                  <span className="font-mono">{v.hora_overlay || '¿?'}</span>{v.pdl_overlay ? ` · PDL ${v.pdl_overlay}` : ''}{v.overlay ? ` · «${v.overlay}»` : ''}
                  <span className="block text-gray-700">{v.que_se_ve}</span>
                  {v.que_se_oye && <span className="block text-gray-500 italic">Audio: {v.que_se_oye}</span>}
                  <div className="mt-1">
                    {v.evidencia_id
                      ? <VideoCaso ruta={`/evidencia/${v.evidencia_id}/descargar`} apiBase={apiBase} />
                      : v.media
                        ? <VideoCaso ruta={`/video-caso/${v.msg_id}`} apiBase={apiBase} />
                        : <span className="text-[11px] text-gray-400">video no disponible en el servidor</span>}
                  </div>
                </li>
              ))}
            </ul>
          ) : <p className="text-xs text-gray-400">ningún video atribuido al caso</p>}
        </div>
      </div>
    )
  }
  // Lectura de un correo (desde Correos ANS o desde el chat/evidencia de la grilla)
  const m = lectura.meta || {}
  const citas = m.citas || []
  const pistas = [m.razonamiento, m.razon, m.causa, data.valor_actual]
  const pdl = codigosCaso(data.pdl, data.protocolo)
  const correos = correosDelCaso(data, apiBase, citas, m.evidencia_id, pdl)
  const algunoNombraPdl = correos.some((c) => fragmentosPdl(c.correo.cuerpo, pdl).length)
  return (
    <div className="space-y-2">
      <Propuesta relacionado={m.relacionado} excluible={m.excluible} revision={m.revision} categoria={m.categoria}
        causa={m.causa} causaTipo={m.causa_tipo} veredictoIa={m.veredicto_ia} />
      {m.razonamiento && (
        <div>
          <p className="text-[10px] uppercase tracking-wide text-gray-400 font-semibold mb-0.5">Razonamiento de la máquina</p>
          <p className="text-xs text-gray-800 leading-relaxed whitespace-pre-wrap bg-white border rounded px-2 py-1.5">{m.razonamiento}</p>
        </div>
      )}
      {m.resumen && m.resumen !== m.razonamiento && <p className="text-xs text-gray-700">{m.resumen}</p>}
      <Revision rev={m.revision} />
      <CoberturaRapida cov={m.cobertura_calc} />
      <Citas citas={citas} />
      {correos.length
        ? correos.map((c) => <CorreoCitado key={c.key} correo={c.correo} citas={citas} pistas={pistas} pdl={pdl} descarga={c.descarga} titulo={c.titulo} />)
        : <p className="text-xs text-gray-400">No hay un correo con texto para mostrar (la lectura pudo ser de un pantallazo o PDF).</p>}
      {!citas.length && m.razonamiento && (
        <p className="text-[11px] text-gray-400">
          Esta lectura no dejó citas literales (análisis anterior a esa regla): el sustento es el razonamiento de arriba;
          {algunoNombraPdl ? ' el amarillo del correo marca dónde se nombra el PDL del caso.' : ' el amarillo del correo es una aproximación por coincidencia de palabras.'}
        </p>
      )}
    </div>
  )
}

export default function JustificacionObs({ regId, campo, label, apiBase, onCerrar }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let vivo = true
    setData(null); setError(null)
    api.get(`/justificacion-obs/${regId}`, { baseURL: apiBase, params: { campo } })
      .then((r) => { if (vivo) setData(r.data) })
      .catch((e) => { if (vivo) setError(e?.response?.data?.detail || 'No se pudo cargar la justificación.') })
    return () => { vivo = false }
  }, [regId, campo, apiBase])

  const o = data?.origen
  const fuentes = data ? [
    ...(data.analisis_wa || []).map((a) => ({ k: `wa${a.id}`, tipo: 'wa', es_origen: a.es_origen, coincide: a.coincide, a })),
    ...(data.correos || []).map((c) => ({ k: `co${c.id}`, tipo: 'correo', es_origen: c.es_origen, coincide: c.coincide, c })),
    ...(data.consultas || []).map((q) => ({ k: `ch${q.id}`, tipo: 'chat', es_origen: q.es_origen, coincide: q.coincide, q })),
  ].sort((x, y) => (y.es_origen ? 1 : 0) - (x.es_origen ? 1 : 0) || (y.coincide ? 1 : 0) - (x.coincide ? 1 : 0)) : []

  // La LECTURA que explica la conclusión: la de la fuente que originó el texto; si no hay,
  // la de la fuente cuyo texto coincide; si no, la última lectura IA que exista del caso.
  const lecturas = []
  for (const f of fuentes) {
    if (f.tipo === 'wa' && f.a.tipo_caso !== '3.2B' && !f.a.ia?.error) {
      lecturas.push({ tipo: 'auditoria_ia', a: f.a, es_origen: f.es_origen, coincide: f.coincide, cuando: f.a.cuando, titulo: `Auditoría IA · análisis #${f.a.id}` })
    } else if (f.tipo === 'correo' && f.c.analisis && !f.c.analisis.error) {
      lecturas.push({ tipo: 'correo', meta: f.c.analisis, es_origen: f.es_origen, coincide: f.coincide, cuando: f.c.analizado_en, titulo: `Correos ANS · lectura del correo #${f.c.id}` })
    } else if (f.tipo === 'chat') {
      const ia = [...(f.q.mensajes || [])].reverse().find((mm) => mm.meta && (mm.meta.razonamiento || mm.meta.citas || mm.meta.razon))
      if (ia) lecturas.push({ tipo: 'chat', meta: { ...ia.meta, razonamiento: ia.meta.razonamiento || ia.texto }, es_origen: f.es_origen, coincide: f.coincide, cuando: ia.cuando, titulo: `${ia.persona || 'Lectura IA'} · hilo #${f.q.id}` })
    }
  }
  const lectura = lecturas.find((l) => l.es_origen) || lecturas.find((l) => l.coincide) || lecturas[0] || null
  const sinNada = data && !fuentes.length && !(data.evidencias || []).length && !(data.historial || []).length

  return (
    <div className="fixed inset-0 z-[70] bg-black/40 flex items-center justify-center p-3" onClick={onCerrar}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-3xl max-h-[90vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 px-4 py-2.5 border-b bg-violet-50 rounded-t-xl">
          <FileSearch size={16} className="text-violet-700" />
          <h3 className="font-semibold text-sm text-gray-800">
            Justificación de la observación
            {data && <span className="font-normal text-gray-500"> · {data.indicador} · PDL {data.pdl || '—'}{data.zona ? ` · ${data.zona}` : ''}</span>}
          </h3>
          <span className="text-[10px] text-gray-400 ml-1">Esc cierra</span>
          <button onClick={onCerrar} className="ml-auto text-gray-400 hover:text-gray-700" title="Cerrar"><X size={16} /></button>
        </div>

        <div className="overflow-auto p-4 space-y-4 text-sm">
          {error && <p className="text-red-600">{error}</p>}
          {!data && !error && <p className="text-gray-400">Reuniendo lo que sustenta esta observación…</p>}

          {data && (
            <>
              {/* ── La conclusión: el texto de la celda y quién lo escribió ── */}
              <div className="border rounded-xl p-3 space-y-2 bg-gray-50">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`px-2 py-0.5 rounded-full text-xs font-semibold border ${ORIGEN_CLS[o.tipo] || ORIGEN_DEFAULT}`}>{o.label}</span>
                  {o.usuario && <span className="text-xs text-gray-600">{o.usuario}{o.rol ? ` (${o.rol})` : ''}</span>}
                  {o.cuando && <span className="text-xs text-gray-500">{fmt(o.cuando)}</span>}
                  {data.decision_actual && (
                    <Chip cls={data.decision_actual === 'SI' ? 'bg-emerald-100 text-emerald-800' : 'bg-red-100 text-red-700'}>
                      decisión en la celda: {data.decision_actual}
                    </Chip>
                  )}
                </div>
                {o.detalle && <p className="text-xs text-gray-700">{o.detalle}</p>}
                {o.base && (
                  <p className="text-xs text-gray-500">Base: {o.base.label} · {fmt(o.base.cuando)}{o.base.usuario ? ` · ${o.base.usuario}` : ''}</p>
                )}
                <div>
                  <p className="text-[10px] uppercase tracking-wide text-gray-400 font-semibold mb-0.5">{label || 'Observación'} · texto actual</p>
                  {data.valor_actual
                    ? <p className="font-mono text-xs whitespace-pre-wrap bg-white border rounded px-2 py-1.5">{data.valor_actual}</p>
                    : <p className="text-xs text-gray-400 italic">(vacía)</p>}
                </div>
                <p className="text-[11px] text-gray-500">{data.protocolo}{data.estado_actual ? ` · ${data.estado_actual}` : ''} · {data.objetivo}</p>
              </div>

              {/* ── CÓMO SE LLEGÓ A ESA CONCLUSIÓN: razonamiento + el correo con lo clave en amarillo ── */}
              <div className="border-2 border-yellow-300 rounded-xl p-3 space-y-2 bg-yellow-50/40">
                <div className="flex flex-wrap items-center gap-2">
                  <Lightbulb size={15} className="text-yellow-600" />
                  <span className="font-semibold text-gray-800">Cómo se llegó a esta conclusión</span>
                  {lectura && <span className="text-xs text-gray-500">{lectura.titulo} · {fmt(lectura.cuando)}</span>}
                  {lectura && !lectura.es_origen && (
                    <Chip cls="bg-amber-100 text-amber-800">
                      {lectura.coincide ? 'lectura con el mismo texto' : 'la lectura IA más reciente del caso — no escribió este texto'}
                    </Chip>
                  )}
                </div>
                {lectura
                  ? <ComoSeLlego lectura={lectura} data={data} apiBase={apiBase} />
                  : (
                    <p className="text-xs text-gray-600">
                      No hay una lectura de la máquina detrás de este texto: {o.tipo === 'vacia' ? 'la celda está vacía.'
                        : o.tipo === 'manual' ? 'se escribió a mano en la grilla.'
                        : o.tipo === 'bot_wa' ? 'es la respuesta que llegó por WhatsApp, anotada por el bot tal cual.'
                        : o.tipo === 'bitacora' || o.tipo === 'evidencia' ? 'es una nota automática de seguimiento (la decisión aún no se ha tomado).'
                        : 'no quedó registrada ninguna lectura IA para esta celda.'}
                    </p>
                  )}
              </div>

              {sinNada && (
                <p className="text-gray-500 text-xs border rounded-xl p-3">
                  El sistema no tiene análisis, evidencias ni escrituras registradas para esta celda: el texto se
                  digitó a mano antes del historial o vino en el Excel importado.
                </p>
              )}

              {/* ── Las fuentes, una por una (la del origen marcada) ── */}
              {fuentes.map((f) => {
                if (f.tipo === 'wa') {
                  const a = f.a; const ia = a.ia || {}
                  return (
                    <Seccion key={f.k} icono={<Sparkles size={14} />} titulo="Auditoría IA"
                      sub={`análisis #${a.id} · ${fmt(a.cuando)} · ${a.analista}`}
                      estado={a.estado} esOrigen={a.es_origen} coincide={a.coincide} abierta={false}>
                      {ia.error && <p className="text-xs text-red-600">{ia.error}</p>}
                      {a.tipo_caso === '3.2B' ? (
                        <>
                          {ia.resumen && <p className="text-xs text-gray-700">{ia.resumen}</p>}
                          {a.consulta && (
                            <p className="text-xs text-gray-700 bg-gray-50 border rounded px-2 py-1.5">
                              <b>Se propuso preguntar a {a.consulta.persona || '—'}</b>{a.consulta.telefono ? ` (+${a.consulta.telefono})` : ''}:
                              <span className="block font-mono whitespace-pre-wrap mt-1">{a.consulta.mensaje}</span>
                            </p>
                          )}
                        </>
                      ) : (
                        <>
                          <ComoSeLlego lectura={{ tipo: 'auditoria_ia', a }} data={data} apiBase={apiBase} />
                          <TextoPropuesto titulo="Observación propuesta" texto={ia.observacion} coincide={contiene(data.valor_actual, ia.observacion)} />
                          <TextoPropuesto titulo="Redacción favorable (alternativa)" texto={ia.observacion_favorable} coincide={contiene(data.valor_actual, ia.observacion_favorable)} />
                        </>
                      )}
                    </Seccion>
                  )
                }
                if (f.tipo === 'correo') {
                  const c = f.c; const an = c.analisis
                  return (
                    <Seccion key={f.k} icono={<Mail size={14} />} titulo="Correos ANS"
                      sub={`correo #${c.id} · ${c.asunto || '(sin asunto)'}`}
                      estado={c.estado} esOrigen={c.es_origen} coincide={c.coincide} abierta={false}>
                      {an ? (
                        <>
                          {an.error && <p className="text-xs text-red-600">{an.error}</p>}
                          {an.hilo_wa_msgs ? <Chip>lectura cruzada con {an.hilo_wa_msgs} mensajes de WhatsApp</Chip> : null}
                          <ComoSeLlego lectura={{ tipo: 'correo', meta: an }} data={{ ...data, evidencias: [], correos: [c] }} apiBase={apiBase} />
                          <TextoPropuesto titulo="Observación propuesta por la lectura del correo" texto={an.razon} coincide={c.coincide} />
                        </>
                      ) : (
                        <>
                          <p className="text-xs text-gray-500">Este correo se adjuntó como evidencia de la celda; no tiene lectura IA propia para ella.</p>
                          {c.cuerpo && <CorreoCitado correo={{ asunto: c.asunto, de: c.remitente, fecha: c.fecha_correo, cuerpo: c.cuerpo }} citas={lectura?.meta?.citas || []} pistas={[lectura?.meta?.razonamiento, data.valor_actual]} pdl={codigosCaso(data.pdl, data.protocolo)}
                            descarga={c.evidencia_id ? `${apiBase}/evidencia/${c.evidencia_id}/descargar` : null} />}
                        </>
                      )}
                    </Seccion>
                  )
                }
                const q = f.q
                return (
                  <Seccion key={f.k} icono={<MessageCircle size={14} />} titulo="Chat de investigación"
                    sub={`hilo #${q.id}${q.persona_actual ? ` · ${q.persona_actual}` : ''} · ${fmt(q.cuando)}`}
                    estado={q.estado} esOrigen={q.es_origen} coincide={q.coincide} abierta={false}>
                    <Propuesta excluible={q.excluible_sug} categoria={q.categoria} />
                    {q.resumen && <p className="text-xs text-gray-700">{q.resumen}</p>}
                    {/* La conversación TAL CUAL quedó en WhatsApp (2026-10-01, R09583): burbujas, citas
                        («responder»), media y horas de WhatsApp. Las lecturas de la IA van aparte,
                        plegadas: dentro del chat lo cortaban y no se leía como la conversación real. */}
                    {(() => {
                      const chats = (q.chat || []).length ? q.chat : chatDeTurnos(q.mensajes, q.persona_actual)
                      const lecturas = (q.mensajes || []).filter((mm) => mm.direccion === 'ia')
                      return (
                        <>
                          {chats.length > 0 && (
                            <>
                              <p className="text-[11px] text-gray-500">
                                {(q.chat || []).length
                                  ? 'Tal cual quedó en WhatsApp, con su hora. Lo que se habló de otro caso con la misma persona sale atenuado.'
                                  : 'Este hilo no tiene el chat guardado (consulta anterior al bot): se muestran los turnos registrados.'}
                              </p>
                              <ChatWhatsApp chats={chats} apiBase={apiBase} />
                            </>
                          )}
                          {lecturas.length > 0 && (
                            <details className="rounded-lg border border-violet-200 bg-violet-50/50 px-3 py-2">
                              <summary className="cursor-pointer text-xs font-medium text-violet-800 select-none">
                                Lo que leyó la máquina en esta conversación ({lecturas.length} {lecturas.length === 1 ? 'lectura' : 'lecturas'})
                              </summary>
                              <div className="mt-2 space-y-1.5">
                                {lecturas.map((mm, i) => (
                                  <div key={i} className="text-xs rounded px-2 py-1 border bg-white border-violet-200">
                                    <span className="text-[10px] text-gray-500">{mm.persona || 'LECTURA IA'} · {fmt(mm.cuando)}</span>
                                    {mm.meta && (
                                      <div className="my-1"><Propuesta relacionado={mm.meta.relacionado} excluible={mm.meta.excluible} categoria={mm.meta.categoria} causa={mm.meta.causa} causaTipo={mm.meta.causa_tipo} /></div>
                                    )}
                                    <span className="block whitespace-pre-wrap text-gray-800 italic">{mm.texto}</span>
                                    {mm.meta?.citas?.length > 0 && <div className="mt-1"><Citas citas={mm.meta.citas} /></div>}
                                  </div>
                                ))}
                              </div>
                            </details>
                          )}
                        </>
                      )
                    })()}
                    <TextoPropuesto titulo="Observación propuesta por el hilo" texto={q.razon_sug} coincide={q.coincide} />
                  </Seccion>
                )
              })}

              {/* ── Material que el matcher amarró al caso (código / caption / líder / ventana) ── */}
              {(data.media_amarrada || []).length > 0 && (
                <Seccion icono={<Video size={14} />} titulo="Material amarrado por el sistema"
                  sub={`${data.media_amarrada.length} archivo(s) de WhatsApp atribuidos a este caso sin IA`} abierta={false}>
                  <ul className="text-xs space-y-1">
                    {data.media_amarrada.map((v) => (
                      <li key={v.msg_id} className={`border rounded px-2 py-1 ${v.puntaje >= 75 ? 'bg-yellow-50 border-yellow-200' : 'bg-gray-50'}`}>
                        <span className="font-mono">{fmt(v.cuando)}</span> · {v.tipo} · {v.grupo}
                        <Chip cls={v.puntaje >= 75 ? 'bg-emerald-100 text-emerald-800 ml-1' : 'bg-gray-200 text-gray-600 ml-1'}>{v.criterio} · {v.puntaje}</Chip>
                        <span className="block text-gray-600">{v.detalle}</span>
                        {v.letrero && <span className="block text-gray-500">letrero: {v.letrero}</span>}
                        {v.caption && <span className="block text-gray-500 italic">«{v.caption}»</span>}
                        {v.transcripcion && <span className="block text-gray-500 italic">Audio: {v.transcripcion}</span>}
                        <div className="mt-1">
                          {v.evidencia_id
                            ? <VideoCaso ruta={`/evidencia/${v.evidencia_id}/descargar`} apiBase={apiBase} etiqueta={v.tipo === 'video' ? 'Ver video' : 'Ver archivo'} />
                            : v.media
                              ? <VideoCaso ruta={`/video-caso/${v.msg_id}`} apiBase={apiBase} etiqueta={v.tipo === 'video' ? 'Ver video' : 'Ver archivo'} />
                              : <span className="text-[11px] text-gray-400">archivo no disponible</span>}
                        </div>
                      </li>
                    ))}
                  </ul>
                </Seccion>
              )}

              {/* ── Evidencias amarradas a la celda ── */}
              {(data.evidencias || []).length > 0 && (
                <Seccion icono={<Paperclip size={14} />} titulo="Evidencias adjuntas" sub={`${data.evidencias.length} archivo(s)`} abierta={false}>
                  <ul className="text-xs space-y-1">
                    {data.evidencias.map((e) => (
                      <li key={e.id} className="border rounded px-2 py-1 bg-gray-50">
                        <a href={`${apiBase}/evidencia/${e.id}/descargar`} target="_blank" rel="noreferrer" className="text-sky-700 underline font-medium">{e.nombre}</a>
                        <span className="text-gray-400"> · {fmt(e.cuando)}</span>
                        {e.nota && <span className="block text-gray-600">{e.nota}</span>}
                        {e.transcripcion && <span className="block text-gray-500 italic mt-0.5">Audio: {e.transcripcion}</span>}
                        {(e.mime || '').startsWith('video/') && <div className="mt-1"><VideoCaso ruta={`/evidencia/${e.id}/descargar`} apiBase={apiBase} /></div>}
                        {e.correo?.cuerpo && <div className="mt-1"><CorreoCitado correo={e.correo} citas={lectura?.meta?.citas || []} pistas={[lectura?.meta?.razonamiento, data.valor_actual]} pdl={codigosCaso(data.pdl, data.protocolo)} titulo="Contenido" /></div>}
                      </li>
                    ))}
                  </ul>
                </Seccion>
              )}

              {/* ── Historial de escrituras de la celda ── */}
              {(data.historial || []).length > 0 && (
                <Seccion icono={<History size={14} />} titulo="Historial de la celda" sub={`${data.historial.length} escritura(s) registradas`} abierta={false}>
                  <ul className="text-xs space-y-1">
                    {data.historial.map((h) => (
                      <li key={h.id} className="border-b border-gray-100 pb-1">
                        <span className="text-gray-500">{fmt(h.cuando)}</span> · <b>{h.usuario || 'sistema'}</b> ·{' '}
                        <span className={`px-1.5 rounded border text-[10px] ${ORIGEN_CLS[h.tipo] || ORIGEN_DEFAULT}`}>{h.label}</span>
                        <span className="block font-mono text-gray-700 whitespace-pre-wrap" title={h.valor_nuevo || ''}>
                          {h.valor_nuevo ? (h.valor_nuevo.length > 220 ? h.valor_nuevo.slice(0, 220) + '…' : h.valor_nuevo) : <i className="text-gray-400">(se vació)</i>}
                        </span>
                      </li>
                    ))}
                  </ul>
                </Seccion>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
