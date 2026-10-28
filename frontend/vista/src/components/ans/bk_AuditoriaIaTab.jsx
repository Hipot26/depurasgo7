// Tab «Auditoría IA» (SOLO desarrollador): el banco de pruebas del analista automático de
// WhatsApp. Muestra TODO el proceso de la máquina sobre un caso — línea de tiempo, videos
// reproducibles con su transcripción y lectura de overlay, veredicto y observación
// borrador — para verificar que quedó bien antes de aplicar nada a la celda del 3.2 A.
// La decisión SI/NO de `excluible` nunca la toca: esa es humana (regla 2026-07-21).
import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../utils/api'
import toast from 'react-hot-toast'
import { Bot, Play, Loader2, CheckCircle2, XCircle, Clock, Video, MessageSquare, AlertTriangle, ChevronLeft, ExternalLink, Send, RefreshCw, HelpCircle, Filter } from 'lucide-react'
import CoberturaRapida from './CoberturaRapida'
import LineaTiempoCaso from './LineaTiempoCaso'
import ChatWhatsApp from './ChatWhatsApp'
import { depurarObs } from '../../utils/obs'

const BADGE = {
  SI: 'bg-emerald-100 text-emerald-800', NO: 'bg-red-100 text-red-700',
  DUDA: 'bg-amber-100 text-amber-800',
}
const CONF = { alta: 'text-emerald-700', media: 'text-amber-700', baja: 'text-red-600' }
// El CICLO DE VIDA real del caso (campo `flujo` del backend), no solo el del análisis:
// así se sabe de un vistazo si lo encolado ya salió y si ya respondieron.
const ESTADO = {
  propuesto: 'bg-blue-100 text-blue-800',
  revision: 'bg-orange-100 text-orange-800',
  encolado: 'bg-amber-100 text-amber-800',
  enviado: 'bg-sky-100 text-sky-800',
  respondido: 'bg-emerald-100 text-emerald-800',
  error_envio: 'bg-red-100 text-red-700',
  aplicado: 'bg-emerald-100 text-emerald-800',
  descartado: 'bg-gray-200 text-gray-500',
}
const ESTADO_LABEL = {
  propuesto: 'pendiente de revisar',
  revision: 'Revisión — decide el ING',
  encolado: 'encolado — esperando al bot',
  enviado: 'enviado ✓',
  respondido: 'respondió ✓✓',
  error_envio: 'error de envío',
  aplicado: 'aplicado al ANS',
  descartado: 'descartado',
}

// EL EMBUDO (24-09): «respondió» no dice nada por sí solo — lo que importa es QUÉ
// respondieron. Cada calidad tiene una salida y un color: verde = ya se puede cerrar,
// rojo = la respuesta ES un no, ámbar = la conversación tiene que seguir.
// [etiqueta corta, color, ¿pide una acción de la persona responsable?]
const EMBUDO = {
  decidir: ['listo para decidir', 'bg-emerald-100 text-emerald-800', true],
  cerrado_no: ['la respuesta es un NO', 'bg-red-100 text-red-700', true],
  insistir: ['falta el soporte', 'bg-amber-100 text-amber-800', true],
  responder: ['te preguntan a ti', 'bg-fuchsia-100 text-fuchsia-800', true],
  repreguntar: ['ambigua — repreguntar', 'bg-amber-100 text-amber-800', true],
  decidir_ing: ['se niegan — decides tú', 'bg-orange-100 text-orange-800', true],
  redirigir: ['remiten a otra persona', 'bg-violet-100 text-violet-800', true],
  esperar_remitido: ['esperando a quien remitieron', 'bg-sky-100 text-sky-800', false],   // REMITEN A OTRA PERSONA (#47, 19 oct 2026)
  revisar: ['sin clasificar — léela', 'bg-gray-200 text-gray-700', true],
  esperar: ['prometieron el soporte', 'bg-sky-100 text-sky-800', false],
  sin_respuesta: ['sin respuesta todavía', 'bg-gray-100 text-gray-500', false],
}
const PIDE_ACCION = Object.entries(EMBUDO).filter(([, v]) => v[2]).map(([k]) => k)

// El <video> no puede mandar el header Authorization → se baja el blob con axios y se
// reproduce desde un object-URL. Solo se carga al hacer clic (los videos pesan).
function VideoWa({ msgId, apiBase }) {
  const [url, setUrl] = useState(null)
  const [cargando, setCargando] = useState(false)
  const cargar = async () => {
    setCargando(true)
    try {
      const r = await api.get(`/wa/media/${msgId}`, { baseURL: apiBase, responseType: 'blob' })
      setUrl(URL.createObjectURL(r.data))
    } catch { toast.error('No se pudo cargar el video') } finally { setCargando(false) }
  }
  if (url) return <video src={url} controls className="w-full max-w-md rounded-lg bg-black" />
  return (
    <button onClick={cargar} disabled={cargando}
      className="flex items-center gap-2 px-3 py-2 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-700 disabled:opacity-50">
      {cargando ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
      Reproducir video (msg {msgId})
    </button>
  )
}

// CUÁNDO SE GRABÓ un video (auditoría 2026-10-01, R09601: el tab decía «reloj 09:12» y el
// letrero del video marcaba 9:44 — la hora la había dicho el líder en el audio). `grabado`
// lo pone el sistema desde el letrero (dos lecturas que cuadran). Los análisis anteriores a
// ese arreglo no traen la llave: si los hizo el analista de texto, su hora no está verificada.
function RelojVideo({ v, analista, meta }) {
  if (v.grabado) {
    return (
      <span className="text-xs">grabado: <b>{v.grabado.inicio.slice(0, 5)}–{v.grabado.fin.slice(0, 5)}</b>
        <span className="text-gray-400"> (reloj del letrero)</span></span>
    )
  }
  if ('grabado' in v || !v.hora_overlay) {
    return v.hora_overlay
      ? <span className="text-xs">reloj: <b>{v.hora_overlay}</b></span>
      : <span className="text-xs text-amber-700">hora de grabación sin leer{meta?.hora_subida ? ` · subido ${meta.hora_subida}` : ''}</span>
  }
  if (analista === 'cloudflare-2etapas') {
    return (
      <span className="text-xs text-amber-700" title="Este análisis es anterior al arreglo del 01-10: la hora pudo salir del audio, no del letrero. Re-analiza el caso o mira el reloj en el video.">
        hora según la IA, sin verificar: <b>{v.hora_overlay}</b>
      </span>
    )
  }
  return <span className="text-xs">reloj: <b>{v.hora_overlay}</b></span>
}

// La media que llega POR EL CHAT (la nota de voz del líder, la foto del correo del Centro
// Operativo, el video del frente): se oye y se ve AQUÍ, y se carga SOLA — la foto y el audio
// pesan poco; el video (pesado) espera al clic. Hasta el 24-09 el turno solo decía «[adjuntó
// imagen]» y había que abrir WhatsApp — con la conversación aquí y la media allá no se
// audita nada, y la máquina concluía sobre algo que nadie había mirado.
function MediaChat({ msgId, tipo, apiBase }) {
  const [url, setUrl] = useState(null)
  const [cargando, setCargando] = useState(false)
  const [error, setError] = useState(false)
  const cargar = async () => {
    setCargando(true)
    try {
      const r = await api.get(`/wa/media/${msgId}`, { baseURL: apiBase, responseType: 'blob' })
      setUrl(URL.createObjectURL(r.data))
    } catch { setError(true) } finally { setCargando(false) }
  }
  const automatico = tipo === 'imagen' || tipo === 'audio'
  useEffect(() => { if (automatico && !url) cargar() }, [msgId])   // eslint-disable-line react-hooks/exhaustive-deps
  if (error) return <p className="mt-1 text-[11px] text-red-600">No se pudo cargar el adjunto (msg {msgId}).</p>
  if (url) {
    if (tipo === 'audio') return <audio src={url} controls preload="metadata" className="mt-1.5 w-64 max-w-full" />
    if (tipo === 'imagen') return (
      <a href={url} target="_blank" rel="noreferrer" title="Abrir la imagen a tamaño completo">
        <img src={url} alt="imagen enviada por el chat" className="mt-1.5 max-h-72 max-w-[300px] rounded-lg border bg-white object-contain" />
      </a>
    )
    if (tipo === 'video') return <video src={url} controls className="mt-1.5 w-full max-w-[300px] rounded-lg bg-black" />
    return <a href={url} download className="mt-1.5 inline-block text-xs underline">Descargar el adjunto</a>
  }
  if (automatico) return <p className="mt-1.5 text-[11px] text-gray-400 flex items-center gap-1"><Loader2 size={11} className="animate-spin" /> cargando {tipo}…</p>
  return (
    <button onClick={cargar} disabled={cargando}
      className="mt-1.5 flex items-center gap-1.5 px-2 py-1 text-[11px] bg-white border rounded-lg hover:bg-gray-50 disabled:opacity-50">
      {cargando ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />} {tipo === 'video' ? 'Reproducir el video' : 'Abrir el adjunto'}
    </button>
  )
}

// 3.2 B sin profesional de apoyo en el directorio: crearlo aquí mismo (nombre + número,
// rol «apoyo» y la zona del caso). Queda para siempre — este caso y todos los que vengan
// de esa zona lo encuentran de una.
function CrearApoyo({ zona, apiBase, onCreado }) {
  const [nombre, setNombre] = useState('')
  const [tel, setTel] = useState('')
  const crear = useMutation({
    mutationFn: async () => (await api.post('/contactos',
      { nombre, telefono: tel, rol: 'apoyo', zona }, { baseURL: apiBase })).data,
    onSuccess: () => {
      toast.success(`${nombre} guardado como profesional de apoyo de ${zona}`)
      onCreado?.()
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo guardar'),
  })
  return (
    <div className="border border-amber-200 bg-amber-50 rounded-lg p-3 space-y-2">
      <p className="text-sm text-amber-900">No hay profesional de apoyo con teléfono para <b>{zona}</b>. Agrégalo y queda para siempre:</p>
      <div className="flex flex-wrap gap-2 items-center">
        <input value={nombre} onChange={(e) => setNombre(e.target.value)} placeholder="Nombre del apoyo"
          className="border rounded-lg px-3 py-1.5 text-sm w-56" />
        <input value={tel} onChange={(e) => setTel(e.target.value)} placeholder="300 123 4567"
          className="border rounded-lg px-3 py-1.5 text-sm w-40 font-mono" />
        <button onClick={() => crear.mutate()} disabled={!nombre || !tel || crear.isPending}
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-700 disabled:opacity-50">
          {crear.isPending ? <Loader2 size={14} className="animate-spin" /> : null}
          Guardar — queda para siempre
        </button>
      </div>
    </div>
  )
}

// ── EL EMBUDO (24-09) ───────────────────────────────────────────────────────────
// Lo que le faltaba al circuito: que respondan no cierra nada. Aquí se ve QUÉ respondieron,
// de qué calidad fue y cuál es la única salida que tiene ese caso:
//   · un NO se cierra en el acto (no se vuelve a preguntar a quien ya dijo que no);
//   · una respuesta concluyente habilita los botones SI/NO sin salir del tab;
//   · una ambigua, una promesa o —sobre todo— una REPREGUNTA no se quedan ahí: la máquina
//     redacta el siguiente mensaje y la persona responsable decide si sale. Ignorar una pregunta válida es
//     perder un caso que podía terminar en excluible.
function EmbudoPanel({ analisisId, det, apiBase, onHecho }) {
  const emb = det.embudo
  const [msg, setMsg] = useState(null)
  const [obs, setObs] = useState(null)
  const [verMsg, setVerMsg] = useState(false)
  const cov = emb?.cobertura

  const seguir = useMutation({
    mutationFn: async (forzar) => (await api.post(`/wa/analisis/${analisisId}/seguimiento`,
      { mensaje: msg ?? emb.mensaje, forzar }, { baseURL: apiBase })).data,
    onSuccess: (d) => { toast.success(`Seguimiento encolado: el bot se lo envía a ${d.persona} en su próximo ciclo`, { duration: 6000 }); onHecho?.() },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo encolar', { duration: 8000 }),
  })
  const decidir = useMutation({
    mutationFn: async (decision) => (await api.post(`/wa/analisis/${analisisId}/decidir`,
      { decision, observacion: obs ?? emb.observacion_sugerida }, { baseURL: apiBase })).data,
    onSuccess: (d) => { toast.success(`Caso cerrado: excluible ${d.decision} + observación en la celda`, { duration: 6000 }); onHecho?.() },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo aplicar', { duration: 8000 }),
  })
  // MENSAJE DE CIERRE (14 oct 2026): con el caso resuelto, decirle a la persona qué sirvió y cómo quedó
  const [msgCierre, setMsgCierre] = useState(null)
  const [verCierre, setVerCierre] = useState(false)
  const cerrarMsg = useMutation({
    mutationFn: async () => (await api.post(`/wa/analisis/${analisisId}/seguimiento`,
      { mensaje: msgCierre ?? emb.mensaje_cierre, forzar: true }, { baseURL: apiBase })).data,
    onSuccess: (d) => { toast.success(`Cierre encolado: el bot se lo envía a ${d.persona} en su próximo ciclo`, { duration: 6000 }); onHecho?.() },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo encolar', { duration: 8000 }),
  })
  const releer = useMutation({
    mutationFn: async () => (await api.post(`/wa/analisis/${analisisId}/releer`, {}, { baseURL: apiBase })).data,
    onSuccess: (d) => {
      toast.success(d.error ? `La IA falló: ${d.error}`
        : `Releído${d.adjuntos_leidos ? ` · ${d.adjuntos_leidos} adjunto(s) transcritos por primera vez` : ''} · ${d.calidad || 'sin clasificar'}`, { duration: 7000 })
      setMsg(null); setObs(null); onHecho?.()
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo releer'),
  })

  if (!emb) return null
  const [etiqueta, color, pide] = EMBUDO[emb.paso] || ['sin clasificar', 'bg-gray-100 text-gray-600', true]
  const cerrado = emb.paso === 'cerrado_no'
  const dec = emb.decision_sugerida || (cerrado ? 'NO' : null)

  // Calmado por defecto: solo se enciende lo que de verdad pide algo. Un caso que solo
  // espera (prometieron el soporte, aún no contestan) va en gris, no en ámbar.
  const marco = !pide ? 'border-gray-200 bg-gray-50'
    : cerrado ? 'border-red-200 bg-red-50'
      : emb.paso === 'decidir' ? 'border-emerald-200 bg-emerald-50' : 'border-amber-200 bg-amber-50'

  return (
    <div className={`border-2 rounded-xl p-4 space-y-3 ${marco}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Filter size={16} className="text-gray-500" />
        <b className="text-sm">Embudo de la respuesta</b>
        <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${color}`}>{etiqueta}</span>
        {emb.persona && <span className="text-xs text-gray-600">· {emb.persona}{emb.cuando ? ` · ${emb.cuando}` : ''}</span>}
        <button onClick={() => releer.mutate()} disabled={releer.isPending}
          title="Vuelve a leer TODA la conversación: transcribe los audios y videos y lee las fotos que aún no se habían mirado"
          className="ml-auto flex items-center gap-1.5 px-2.5 py-1 text-xs bg-white border rounded-lg hover:bg-gray-50 disabled:opacity-50">
          {releer.isPending ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />} Releer la conversación
        </button>
      </div>
      <p className="text-sm text-gray-800">{emb.ayuda}</p>
      {emb.resumen_ia && <p className="text-xs text-gray-600"><b>Lectura de la máquina:</b> {emb.resumen_ia}</p>}

      {/* Su pregunta, en primer plano: sin responderla la conversación muere aquí */}
      {emb.pregunta_pendiente && (
        <div className="rounded-lg bg-white border border-fuchsia-300 p-3 space-y-1">
          <p className="text-sm flex items-start gap-1.5 text-fuchsia-900">
            <HelpCircle size={15} className="shrink-0 mt-0.5" />
            <span><b>Te preguntaron:</b> {emb.pregunta_pendiente}</span></p>
          {emb.respuesta_sugerida && <p className="text-xs text-gray-700"><b>Respuesta que propone la máquina:</b> {emb.respuesta_sugerida}</p>}
        </div>
      )}

      {/* Qué falta, con los minutos del sistema (no los de la IA) */}
      {(emb.falta || emb.linea_faltante) && (
        <div className="text-sm space-y-0.5">
          {emb.falta && <p><b>Falta:</b> {emb.falta}</p>}
          {emb.linea_faltante && <p className="text-xs text-gray-700">{emb.linea_faltante}</p>}
        </div>
      )}
      {cov?.aplicable && (
        <p className="text-[11px] text-gray-500">
          Cobertura sumando los dos canales (evidencia del caso + lo que dijo la conversación):{' '}
          {Object.entries(cov.gaps).map(([g, v]) => `${g} ${v.justificado}/${v.requerido} min`).join(' · ')}
          {cov.cubierto ? ' — el desfase queda cubierto' : ''}
        </p>
      )}

      {/* REMITEN A OTRA PERSONA (#47, 19 oct 2026): a quién escribirle, con su número (o las sugerencias de los
          grupos) y el mensaje ya redactado; nada sale sin el clic */}
      {emb.remitido && (
        <CandidatoPregunta key={emb.remitido.nombre} analisisId={analisisId} candidato={emb.remitido}
          apiBase={apiBase} onHecho={onHecho} />
      )}

      {/* Continuar la conversación: la máquina redacta, la persona responsable decide si sale */}
      {emb.mensaje && (
        <div className="rounded-lg bg-white border p-3 space-y-2">
          <button onClick={() => setVerMsg((v) => !v)} className="flex items-center gap-1.5 text-sm font-semibold text-violet-700">
            <MessageSquare size={14} /> Siguiente mensaje que propone la máquina {verMsg ? '▾' : '▸'}
          </button>
          {verMsg && (<>
            <p className="text-[11px] text-gray-500">
              Para <b>{emb.persona}</b>{emb.telefono ? <> · <span className="font-mono">+{emb.telefono}</span></> : ' · sin número en el directorio'}. Edítalo antes de enviar; nada sale sin tu clic.
            </p>
            <textarea value={msg ?? emb.mensaje} onChange={(e) => setMsg(e.target.value)} rows={8}
              className="w-full border rounded-lg p-2 text-sm" />
            <div className="flex flex-wrap gap-2">
              <button onClick={() => seguir.mutate(false)} disabled={seguir.isPending || !emb.telefono}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-violet-600 text-white rounded-lg hover:bg-violet-700 disabled:opacity-50">
                {seguir.isPending ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />} Enviar seguimiento
              </button>
              <button onClick={() => seguir.mutate(true)} disabled={seguir.isPending || !emb.telefono}
                title="Salta los bloqueadores (correo ya existente, pregunta repetida)"
                className="px-3 py-1.5 text-xs bg-white border text-gray-500 rounded-lg hover:bg-gray-50 disabled:opacity-50">
                Enviar de todas formas
              </button>
            </div>
          </>)}
        </div>
      )}

      {/* El cierre: qué sirvió de lo que mandaron y cómo queda el caso (opcional, nada sale sin clic) */}
      {!emb.mensaje && emb.mensaje_cierre && (
        <div className="rounded-lg bg-white border p-3 space-y-2">
          <button onClick={() => setVerCierre((v) => !v)} className="flex items-center gap-1.5 text-sm font-semibold text-emerald-700">
            <MessageSquare size={14} /> Mensaje de cierre que propone la máquina {verCierre ? '▾' : '▸'}
          </button>
          {verCierre && (<>
            <p className="text-[11px] text-gray-500">
              Para <b>{emb.persona}</b>{emb.telefono ? <> · <span className="font-mono">+{emb.telefono}</span></> : ' · sin número en el directorio'}. Le dice qué sirvió de lo que mandó y cómo queda el caso. Edítalo; nada sale sin tu clic.
            </p>
            <textarea value={msgCierre ?? emb.mensaje_cierre} onChange={(e) => setMsgCierre(e.target.value)} rows={7}
              className="w-full border rounded-lg p-2 text-sm" />
            <button onClick={() => cerrarMsg.mutate()} disabled={cerrarMsg.isPending || !emb.telefono}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">
              {cerrarMsg.isPending ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />} Enviar cierre
            </button>
          </>)}
        </div>
      )}

      {/* La decisión, sin salir del tab ni del caso */}
      {emb.puede_decidir && !emb.hilo_resuelto && (
        <div className="rounded-lg bg-white border p-3 space-y-2">
          <p className="text-sm font-semibold">
            {cerrado ? 'Cerrar el caso con lo que respondieron' : 'Decidir este caso'}
            {dec && <span className="ml-2 text-xs font-normal text-gray-500">la máquina propone {dec}</span>}
          </p>
          <textarea value={obs ?? emb.observacion_sugerida ?? ''} onChange={(e) => setObs(e.target.value)} rows={4}
            placeholder="Observación que va en la celda del ANS"
            className="w-full border rounded-lg p-2 text-sm font-mono" />
          <div className="flex flex-wrap gap-2">
            <button onClick={() => decidir.mutate('SI')} disabled={decidir.isPending}
              className={`flex items-center gap-1.5 px-4 py-2 text-sm rounded-lg disabled:opacity-50 ${dec === 'SI' ? 'bg-emerald-600 text-white hover:bg-emerald-700' : 'bg-white border-2 border-emerald-600 text-emerald-700 hover:bg-emerald-50'}`}>
              {decidir.isPending ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />} Excluible SI
            </button>
            <button onClick={() => decidir.mutate('NO')} disabled={decidir.isPending}
              className={`flex items-center gap-1.5 px-4 py-2 text-sm rounded-lg disabled:opacity-50 ${dec === 'NO' ? 'bg-red-600 text-white hover:bg-red-700' : 'bg-white border-2 border-red-500 text-red-600 hover:bg-red-50'}`}>
              {decidir.isPending ? <Loader2 size={14} className="animate-spin" /> : <XCircle size={14} />} Excluible NO
            </button>
          </div>
          <p className="text-[11px] text-gray-500">Escribe el SI/NO y la observación en la celda, cierra el hilo y deja la media del chat como evidencia del caso.</p>
        </div>
      )}
      {emb.hilo_resuelto && (
        <p className="text-xs text-emerald-800">Este caso ya quedó decidido y el hilo está cerrado.</p>
      )}
    </div>
  )
}

// PASO 2 del 3.2 A: un candidato a preguntarle (líder/supervisor). Si no tiene número,
// aquí mismo se guarda EN EL DIRECTORIO (queda para siempre, no solo para este caso);
// con número, se edita el mensaje y se encola al bot — mismo circuito automático del 3.2 B.
// PASO 1 CON RESPUESTA (#51, 19 oct 2026): con la zona ya sabida, se pone en la orden desde aquí. Es el mismo
// PATCH de la grilla: queda en el historial y el directorio aprende la zona del supervisor.
function PonerZona({ regId, sugerida, apiBase, onHecho }) {
  const [zona, setZona] = useState(sugerida || '')
  const poner = useMutation({
    mutationFn: async () => (await api.patch(`/${regId}`, { zona }, { baseURL: apiBase })).data,
    onSuccess: () => { toast.success(`Zona ${zona} puesta en la orden`); onHecho?.() },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo poner la zona'),
  })
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-gray-600">
      <span>{sugerida ? <>La respuesta dice zona <b>{sugerida}</b>.</> : '¿Ya sabes la zona?'}</span>
      <select value={zona} onChange={(e) => setZona(e.target.value)} className="px-2 py-1 border rounded bg-white">
        <option value="">— zona —</option>
        {['UBATE', 'ALMEIDAS', 'GUAVIO'].map((z) => <option key={z} value={z}>{z}</option>)}
      </select>
      <button onClick={() => poner.mutate()} disabled={!zona || poner.isPending}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50">
        {poner.isPending ? <Loader2 size={13} className="animate-spin" /> : <CheckCircle2 size={13} />}
        Poner zona en la orden
      </button>
      <span className="text-gray-400">El caso sigue solo: se elige el profesional de apoyo de esa zona.</span>
    </div>
  )
}

function CandidatoPregunta({ analisisId, candidato, apiBase, onHecho }) {
  const qc = useQueryClient()
  const [tel, setTel] = useState('')
  const [msg, setMsg] = useState(null)
  const [rechazadas, setRechazadas] = useState([])   // sugerencias rechazadas por la persona responsable

  const guardarTel = useMutation({
    // `vars.telefono` cuando viene de una sugerencia aprobada; sin vars = el input manual
    mutationFn: async (vars) => (await api.post('/contactos',
      { nombre: candidato.nombre, telefono: vars?.telefono || tel, rol: candidato.rol, cedula: candidato.cedula || undefined },
      { baseURL: apiBase })).data,
    onSuccess: () => {
      toast.success(`Número de ${candidato.nombre} guardado en el directorio`)
      qc.invalidateQueries({ queryKey: ['wa-analisis-det'] })
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo guardar'),
  })

  const [msgApoyo, setMsgApoyo] = useState(null)   // mensaje al apoyo pidiendo el número del supervisor

  const encolar = useMutation({
    // Sin vars = la pregunta al candidato; con vars = otra persona/campo (p. ej. pedirle el
    // número del supervisor al profesional de apoyo, 2026-09-23)
    mutationFn: async (vars) => (await api.post(`/wa/analisis/${analisisId}/preguntar`,
      { persona: vars?.persona ?? candidato.nombre, telefono: vars?.telefono ?? candidato.telefono,
        rol: vars?.rol ?? candidato.rol, campo: vars?.campo ?? candidato.campo ?? 'excluible',
        mensaje: vars?.mensaje ?? (msg ?? candidato.mensaje), forzar: vars?.forzar ?? undefined },
      { baseURL: apiBase })).data,
    onSuccess: (data) => {
      toast.success(`Pregunta encolada: el bot se la envía a ${data?.persona || candidato.nombre}`)
      onHecho?.()
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo encolar'),
  })

  return (
    <div className="border rounded-lg p-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <b>{candidato.nombre}</b>
        <span className="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-600">
          {candidato.remitido_por ? `Remitido por ${candidato.remitido_por}`
            : candidato.rol === 'lider' ? 'Líder / RI (col E) — estuvo en campo'
            : candidato.creador ? (candidato.presunta
              ? 'Creó la orden (col H ≈, presunta: confírmalo en el STM)'
              : 'Creó la orden (col H del STM) — sabe la zona y el supervisor')
            : candidato.rol === 'programadora' ? 'Programadora — conoce la zona y el supervisor de cada orden'
              : 'Supervisor (col G) — responde por la cuadrilla'}
        </span>
        {candidato.telefono && <span className="font-mono text-xs text-gray-500">+{candidato.telefono}</span>}
        {candidato.envio && (
          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${ESTADO[candidato.envio] || ''}`}>
            {ESTADO_LABEL[candidato.envio] || candidato.envio}
          </span>
        )}
      </div>
      {candidato.envio ? (
        candidato.respondio
          ? <p className="text-xs text-emerald-700 font-medium">Ya respondió — su respuesta está abajo, en «Respondieron».</p>
          : <p className="text-xs text-gray-500">La pregunta a esta persona ya está en el circuito — cuando responda, la máquina procesa sola.</p>
      ) : candidato.bloqueo ? (
        /* BLOQUEADOR (27-08): correo ya en el sistema o pregunta repetida — no se
           encola sin verlo de frente */
        <div className="bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-xs text-red-800 space-y-2">
          <p className="flex items-start gap-1.5"><AlertTriangle size={13} className="shrink-0 mt-0.5" />
            <span><b>Bloqueado:</b> {candidato.bloqueo}.</span></p>
          <div className="flex flex-wrap gap-2">
            <a href="/ans?tab=correos-ans" className="px-2.5 py-1 rounded bg-white border border-red-300 text-red-700 hover:bg-red-100">Ir a Correos ANS</a>
            {candidato.telefono && (
              <button onClick={() => encolar.mutate({ forzar: true })} disabled={encolar.isPending}
                className="px-2.5 py-1 rounded bg-white border text-gray-500 hover:bg-gray-50 disabled:opacity-50">
                Preguntar de todas formas
              </button>
            )}
          </div>
        </div>
      ) : !candidato.telefono ? (
        <div className="space-y-2">
          {/* Sugerencias minadas de los perfiles de WhatsApp (los grupos están llenos de
              líderes/supervisores): la persona aprueba o rechaza; nunca se rellena solo */}
          {(candidato.sugerencias || []).filter((s) => !rechazadas.includes(s.telefono)).map((s) => (
            <div key={s.telefono} className="bg-sky-50 border border-sky-200 rounded-lg px-3 py-2 text-xs text-sky-900 flex flex-wrap items-center gap-2">
              <span>
                {s.origen === 'directorio'
                  ? <>En el directorio existe <b>«{s.nombre_wa}»</b>{s.rol ? ` (${s.rol})` : ''} · <span className="font-mono">+{s.telefono}</span> — con otra grafía. ¿Es esta persona?</>
                  : s.origen === 'cd10'
                    ? <>El CD10 trae <span className="font-mono">+{s.telefono}</span> como celular del supervisor (columna AX). ¿Guardarlo como número de {candidato.nombre}?</>
                  : s.origen === 'respuesta'
                    ? <><b>{s.nombre_wa}</b> respondió: «{s.texto}» → <span className="font-mono">+{s.telefono}</span>. ¿Guardarlo como número de {candidato.nombre}?</>
                    : <>En WhatsApp hay un perfil <b>«{s.nombre_wa}»</b> · <span className="font-mono">+{s.telefono}</span> · visto en {(s.grupos || []).join(', ')} ({s.mensajes} msjs). ¿Es esta persona?</>}
              </span>
              {/* Blindaje contra homónimos (caso Jaime Peña, 25-08): zona incoherente
                  o apellidos del perfil que no están en el nombre buscado */}
              {s.zona_ok === false && (
                <span className="w-full text-red-700 font-semibold flex items-center gap-1">
                  <AlertTriangle size={12} className="shrink-0" />
                  OJO: {s.origen === 'directorio' ? `su zona (${s.zona || '¿?'})` : 'sus grupos'} no coinciden con la zona del caso — puede ser un homónimo de otra zona.
                </span>
              )}
              {(s.extras || []).length > 0 && (
                <span className="w-full text-amber-700">
                  El {s.origen === 'directorio' ? 'contacto' : 'perfil'} también dice «{s.extras.join(' ')}» — verifica que los apellidos cuadren.
                </span>
              )}
              <button onClick={() => guardarTel.mutate({ telefono: s.telefono })} disabled={guardarTel.isPending}
                className="px-2.5 py-1 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50 font-medium">
                Sí — guardar
              </button>
              <button onClick={() => setRechazadas((prev) => [...prev, s.telefono])}
                className="px-2.5 py-1 rounded bg-white border border-sky-300 hover:bg-sky-100">
                No es
              </button>
            </div>
          ))}
          <div className="flex gap-2 items-center">
            <input value={tel} onChange={(e) => setTel(e.target.value)}
              placeholder={(candidato.sugerencias || []).length ? 'o escríbelo a mano…' : '300 123 4567'}
              className="border rounded-lg px-3 py-1.5 text-sm w-44 font-mono" />
            <button onClick={() => guardarTel.mutate()} disabled={!tel || guardarTel.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-700 disabled:opacity-50">
              {guardarTel.isPending ? <Loader2 size={14} className="animate-spin" /> : null}
              Guardar — queda para siempre
            </button>
          </div>
          {/* Supervisor sin número (2026-09-23): quien lo consigue es el profesional de apoyo de
              la zona. Se le cuenta el caso y se le pide el número; la persona responsable aprueba el envío. */}
          {candidato.pedir_numero && (
            candidato.pedir_numero.posible ? (
              <div className="border border-violet-200 bg-violet-50 rounded-lg p-3 space-y-2">
                <p className="text-xs text-violet-900 flex flex-wrap items-center gap-2">
                  <span>¿No lo tienes? Pídeselo al profesional de apoyo de la zona: <b>{candidato.pedir_numero.nombre}</b>
                    <span className="font-mono text-violet-700"> +{candidato.pedir_numero.telefono}</span></span>
                  {candidato.pedir_numero.envio && (
                    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${ESTADO[candidato.pedir_numero.envio] || ''}`}>
                      {ESTADO_LABEL[candidato.pedir_numero.envio] || candidato.pedir_numero.envio}
                    </span>
                  )}
                </p>
                {candidato.pedir_numero.envio ? (
                  <p className="text-xs text-violet-800">Ya se le pidió. Cuando responda, el número aparece aquí como sugerencia para guardarlo; a las 24 h sin respuesta el sistema le recuerda.</p>
                ) : (
                  <>
                    <textarea value={msgApoyo ?? candidato.pedir_numero.mensaje ?? ''} onChange={(e) => setMsgApoyo(e.target.value)}
                      rows={7} className="w-full border rounded-lg p-2 text-sm bg-white" />
                    <button onClick={() => encolar.mutate({ persona: candidato.pedir_numero.nombre, telefono: candidato.pedir_numero.telefono,
                      rol: 'apoyo', campo: 'numero_supervisor', mensaje: msgApoyo ?? candidato.pedir_numero.mensaje })}
                      disabled={encolar.isPending}
                      className="flex items-center gap-2 px-3 py-1.5 text-sm bg-violet-700 text-white rounded-lg hover:bg-violet-800 disabled:opacity-50">
                      {encolar.isPending ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}
                      Pedirle el número a {candidato.pedir_numero.nombre.split(' ')[0]} — el bot lo envía en su próximo ciclo
                    </button>
                  </>
                )}
              </div>
            ) : (
              <p className="text-xs text-gray-500">No se puede pedir el número al apoyo: {candidato.pedir_numero.motivo}.</p>
            )
          )}
        </div>
      ) : (
        <>
          <textarea value={msg ?? candidato.mensaje ?? ''} onChange={(e) => setMsg(e.target.value)}
            rows={6} className="w-full border rounded-lg p-2 text-sm" />
          <button onClick={() => encolar.mutate()} disabled={encolar.isPending}
            className="flex items-center gap-2 px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">
            {encolar.isPending ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}
            Encolar pregunta — el bot la envía en su próximo ciclo
          </button>
        </>
      )}
    </div>
  )
}

// `periodo`: el del selector de la página — la lista solo trae los casos de ese mes del ANS
// (coherente con la data y con Correos ANS; requisito 2026-09-07).
export default function AuditoriaIaTab({ apiBase, periodo, onVolverTabla = null }) {
  const qc = useQueryClient()
  // El análisis puede venir en la URL (?analisis=41): permite abrir cada caso en una
  // pestaña nueva del navegador (pedido 2026-08-25).
  const [sel, setSel] = useState(() => {
    const p = new URLSearchParams(window.location.search).get('analisis')
    return p ? Number(p) : null
  })
  // Desde la celda del PDL de la tabla (2026-10-01): un caso sin análisis llega con ?pdl= ya
  // escrito en «Analizar caso», y `desde=tabla` pinta «Volver a la tabla».
  const [pdl, setPdl] = useState(() => new URLSearchParams(window.location.search).get('pdl') || '')
  const [desdeTabla] = useState(() => new URLSearchParams(window.location.search).get('desde') === 'tabla')
  const [obsEdit, setObsEdit] = useState(null)  // observación editada antes de aplicar (3.2 A)
  const [msgEdit, setMsgEdit] = useState(null)  // mensaje editado antes de encolar (3.2 B)

  const { data: lista = [] } = useQuery({
    queryKey: ['wa-analisis', apiBase, periodo],
    queryFn: async () => (await api.get('/wa/analisis', { baseURL: apiBase, params: periodo ? { periodo } : {} })).data,
    // El bot envía y la gente responde sin que nadie toque el tab: los estados se
    // refrescan solos para que «encolado» pase a «enviado ✓» ante tus ojos.
    refetchInterval: 30000,
  })

  // Casos del periodo que DEBERÍAN estar aquí y no están (3.2 A NO CUMPLE / 3.2 B anulados
  // sin decisión): el botón los manda a analizar todos de una (pedido 2026-09-07).
  const { data: sinAnalizar = [] } = useQuery({
    queryKey: ['wa-pendientes', apiBase, periodo],
    queryFn: async () => (await api.get('/wa/pendientes', { baseURL: apiBase, params: { periodo } })).data,
    enabled: !!periodo,
    refetchInterval: 30000,
  })
  const analizarPendientes = useMutation({
    mutationFn: async (reintentar) => (await api.post('/wa/analizar-pendientes', { periodo, reintentar }, { baseURL: apiBase })).data,
    onSuccess: (d) => {
      toast.success(d.en_cola
        ? `${d.en_cola} caso(s) en cola — 1 a 3 min cada uno; la lista se va llenando sola`
        : 'No hay casos pendientes de análisis en este periodo', { duration: 6000 })
      setTimeout(() => { qc.invalidateQueries({ queryKey: ['wa-analisis'] }); qc.invalidateQueries({ queryKey: ['wa-pendientes'] }) }, 4000)
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo encolar'),
  })
  // Las respuestas que la máquina nunca clasificó (hilos anteriores al embudo del 24-09 o
  // lecturas que fallaron): se releen en bloque y entran al embudo como las nuevas.
  const sinClasificar = lista.filter((x) => x.flujo === 'respondido' && !x.calidad)
  const releerPendientes = useMutation({
    mutationFn: async () => (await api.post('/wa/releer-pendientes', { periodo }, { baseURL: apiBase })).data,
    onSuccess: (d) => {
      toast.success(d.en_cola
        ? `${d.en_cola} conversación(es) en cola — la máquina transcribe los adjuntos y las clasifica; la lista se refresca sola`
        : 'No hay respuestas sin clasificar en este periodo', { duration: 7000 })
      setTimeout(() => qc.invalidateQueries({ queryKey: ['wa-analisis'] }), 8000)
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo encolar la relectura'),
  })
  const nFallidos = lista.filter((x) => x.error && x.estado !== 'descartado').length
  // Tablero del ciclo de vida (orquestación 2026-09-07): estado de cada caso por auditar y qué falta
  const { data: tablero = [] } = useQuery({
    queryKey: ['wa-tablero', apiBase, periodo],
    queryFn: async () => (await api.get('/wa/tablero', { baseURL: apiBase, params: { periodo } })).data,
    enabled: !!periodo,
    refetchInterval: 30000,
  })
  const [verTablero, setVerTablero] = useState(true)
  const [aprobados, setAprobados] = useState([])       // reg_ids marcados por la persona para pedir
  const pedirFaltantes = useMutation({
    mutationFn: async () => (await api.post('/wa/pedir-faltantes', { periodo, reg_ids: aprobados }, { baseURL: apiBase })).data,
    onSuccess: (d) => {
      const enc = (d.acciones || []).filter((x) => x.accion === 'encolado')
      const blo = (d.acciones || []).filter((x) => x.accion === 'bloqueado')
      toast.success(`${enc.length} pedido(s) encolados al bot${blo.length ? ` · ${blo.length} bloqueado(s) (ya hay correo o pregunta)` : ''}`, { duration: 7000 })
      setAprobados([])
      qc.invalidateQueries({ queryKey: ['wa-tablero'] }); qc.invalidateQueries({ queryKey: ['wa-analisis'] })
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo pedir'),
  })
  const enviarAprobados = () => {
    const filas = tablero.filter((c) => aprobados.includes(c.reg_id))
    if (!filas.length) return
    const lista = filas.map((c) => `• ${c.pdl} → ${c.pedido_propuesto?.persona} (${c.pedido_propuesto?.rol})`).join('\n')
    if (window.confirm(`Se enviará por WhatsApp, con la plantilla oficial, a:\n\n${lista}\n\n¿Confirmas el envío?`)) pedirFaltantes.mutate()
  }
  const ESTADO_CICLO = {
    nuevo: ['Nuevo', 'bg-gray-100 text-gray-600'], recolectando: ['Recolectando', 'bg-amber-100 text-amber-800'],
    analizado: ['Analizado', 'bg-violet-100 text-violet-800'], decidido_sin_soporte: ['Decidido sin soporte', 'bg-red-100 text-red-700'],
    verificado: ['Verificado', 'bg-emerald-100 text-emerald-800'],
  }

  // Cola de revisión: lo pendiente, de más viejo a más nuevo (para despachar en orden)
  // «Pendiente» = por el flujo real: un caso con pregunta ya encolada/enviada deja de
  // estar en la cola de revisión aunque su análisis siga técnicamente «propuesto».
  // …y desde el 24-09 también lo RESPONDIDO que pide una acción (decidir, contestar una
  // repregunta, insistir por el soporte): antes «respondió ✓✓» salía de la cola y el caso se
  // quedaba quieto para siempre aunque la respuesta abriera la puerta a una exclusión.
  // PENDIENTE = la celda todavía falta en la TABLA (2026-10-01: el botón decía 16 y 15 ya estaban
  // decididos en la grilla o en Correos ANS; la tabla pedía 3). Misma regla del parpadeo, que
  // calcula el backend (`celda_pendiente`). Primero los que piden una acción tuya; después los
  // que esperan respuesta — también faltan en la tabla, y así el conteo cuadra con ella.
  const accionable = (x) => ['propuesto', 'revision'].includes(x.flujo || x.estado)
    || (x.flujo === 'respondido' && PIDE_ACCION.includes(x.embudo_paso || 'revisar'))
  const pendientes = lista.filter((x) => x.estado !== 'descartado' && (x.celda_pendiente ?? accionable(x)))
    .sort((a, b) => (Number(accionable(b)) - Number(accionable(a))) || a.id - b.id)
  // Los descartados (análisis viejos re-corridos, métodos superados) van OCULTOS por
  // defecto: estorban y confunden — abrir uno muestra su resultado congelado (p. ej. un
  // error de API viejo) y parece bug. Un interruptor los revela si hace falta auditar.
  const [verDescartados, setVerDescartados] = useState(false)
  // Filtros de la lista (pedido 2026-08-25): estado, zona, propuesta y búsqueda por PDL
  const [fEstado, setFEstado] = useState('')
  const [fZona, setFZona] = useState('')
  const [fProp, setFProp] = useState('')
  const [fEmbudo, setFEmbudo] = useState('')
  const [fBusq, setFBusq] = useState('')
  const zonas = [...new Set(lista.map((x) => x.zona).filter(Boolean))].sort()
  const listaVisible = (verDescartados ? lista : lista.filter((x) => x.estado !== 'descartado'))
    .filter((x) => !fEstado || (x.flujo || x.estado) === fEstado)
    .filter((x) => !fZona || x.zona === fZona)
    .filter((x) => !fProp || (fProp === 'revision' ? (x.flujo || '') === 'revision'
      : fProp === 'sin' ? (!x.excluible_sugerido && (x.flujo || '') !== 'revision')
        : x.excluible_sugerido === fProp))
    .filter((x) => !fEmbudo || (x.flujo === 'respondido' && (x.embudo_paso || 'revisar') === fEmbudo))
    .filter((x) => !fBusq || (x.pdl || '').toUpperCase().includes(fBusq.trim().toUpperCase()))
  const nDescartados = lista.length - lista.filter((x) => x.estado !== 'descartado').length
  const irSiguiente = () => {
    const sig = pendientes.find((x) => x.id > (sel || 0)) || pendientes.find((x) => x.id !== sel)
    if (!sig) { toast('No quedan casos pendientes'); setSel(null); setObsEdit(null); setMsgEdit(null); return }
    setSel(sig.id); setObsEdit(null); setMsgEdit(null)
  }

  const { data: det } = useQuery({
    queryKey: ['wa-analisis-det', apiBase, sel],
    queryFn: async () => (await api.get(`/wa/analisis/${sel}`, { baseURL: apiBase })).data,
    enabled: !!sel,
  })

  const analizar = useMutation({
    mutationFn: async () => (await api.post('/wa/analizar', { pdl }, { baseURL: apiBase })).data,
    onSuccess: (d) => {
      toast.success(`Análisis #${d.id} listo`)
      qc.invalidateQueries({ queryKey: ['wa-analisis'] }); setSel(d.id); setPdl('')
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'Falló el análisis'),
  })

  const aplicar = useMutation({
    // persona/teléfono van desde el detalle EN VIVO (el análisis congelado puede ser
    // anterior a que se guardara el contacto en el directorio)
    mutationFn: async (vars) => (await api.post(`/wa/analisis/${sel}/aplicar`,
      // MENSAJE 3.2 B EN VIVO (#52, 20 oct 2026): se manda el texto del recuadro, editado o no
      { observacion: obsEdit ?? undefined, mensaje: msgEdit ?? det?.resultado?.consulta?.mensaje ?? undefined,
        decision: vars?.decision ?? undefined, forzar: vars?.forzar ?? undefined,
        persona: det?.resultado?.consulta?.persona ?? undefined,
        telefono: det?.resultado?.consulta?.telefono ?? undefined }, { baseURL: apiBase })).data,
    onSuccess: (d) => {
      toast.success(d.encolado
        ? `Mensaje encolado: el bot se lo envía a ${d.persona} en su próximo ciclo`
        : d.decision
          ? `Aplicado al ANS: excluible ${d.decision} + observación + videos como evidencia`
          : 'Aplicado: observación escrita y videos adjuntados como evidencia', { duration: 6000 })
      qc.invalidateQueries({ queryKey: ['wa-analisis'] })
      qc.invalidateQueries({ queryKey: ['wa-analisis-det'] })
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo aplicar'),
  })

  const descartar = useMutation({
    mutationFn: async () => (await api.post(`/wa/analisis/${sel}/descartar`, {}, { baseURL: apiBase })).data,
    onSuccess: () => {
      toast('Análisis descartado')
      qc.invalidateQueries({ queryKey: ['wa-analisis'] })
      qc.invalidateQueries({ queryKey: ['wa-analisis-det'] })
    },
  })

  // ── Detalle de un análisis ──────────────────────────────────────────────────
  if (sel && det) {
    const res = det.resultado || {}
    const ia = res.ia || {}
    const conError = !!ia.error
    const es32B = res.tipo === '3.2B'
    const consulta = res.consulta || {}
    const quedanPendientes = pendientes.filter((x) => x.id !== sel).length
    // PASO 1 A QUIEN CREÓ LA ORDEN (#49, 19 oct 2026): orden sin zona (o con un celular en la col de supervisor) → antes
    // de todo, preguntarle la zona y el supervisor a quien la creó (col H). El mismo bloque en el 3.2 A y en el
    // 3.2 B (antes solo existía en el 3.2 A y un anulado sin zona se quedaba sin salida, R10478).
    const alHechoPaso1 = () => { qc.invalidateQueries({ queryKey: ['wa-analisis'] }); qc.invalidateQueries({ queryKey: ['wa-analisis-det'] }) }
    const p1 = det.programadora
    const paso1 = det.estado === 'propuesto' && p1 ? (
      <div className="bg-white border border-amber-200 rounded-xl p-4 space-y-3">
        <h4 className="text-sm font-semibold flex flex-wrap items-center gap-1.5">
          <MessageSquare size={15} /> Paso 1 · Zona y supervisor — {p1.creador ? 'preguntar a quien creó la orden' : 'preguntar a programación'}
          <span className="text-xs font-normal text-gray-500">({p1.hallazgo})</span>
        </h4>
        {p1.sin_directorio ? (
          <p className="text-sm text-amber-800">No hay contactos con rol «programadora» en el directorio. Créalos en Contactos (en el orden en que deben consultarse) y vuelve aquí.</p>
        ) : (
          <>
            <CandidatoPregunta key={p1.nombre} analisisId={sel} candidato={p1} apiBase={apiBase} onHecho={alHechoPaso1} />
            {(p1.alternas || []).length > 0 && (
              <div className="space-y-2">
                <p className="text-xs text-gray-600">
                  {p1.telefono ? 'Programación (ya se le preguntó):'
                    : <>¿No tienes el número de <b>{p1.nombre}</b>? Pregúntale a programación:</>}
                </p>
                {p1.alternas.map((c) => (
                  <CandidatoPregunta key={c.nombre} analisisId={sel} candidato={c} apiBase={apiBase} onHecho={alHechoPaso1} />
                ))}
              </div>
            )}
            {p1.escalamiento && (
              <p className="text-xs text-gray-500">
                Si <b>{p1.nombre}</b> no responde en {p1.escalamiento.horas} h, el sistema le pregunta solo a <b>{p1.escalamiento.nombre}</b>.
                Cuando respondan, corriges zona y supervisor en la data y el análisis sigue su camino normal.
              </p>
            )}
            {/* PASO 1 CON RESPUESTA (#51, 19 oct 2026): el embudo del Paso 1 */}
            {(p1.respuestas || []).length > 0 && (
              <div className="border border-emerald-200 bg-emerald-50 rounded-lg p-3 space-y-2">
                <p className="text-xs font-semibold text-emerald-900">Respondieron</p>
                {p1.respuestas.map((r, i) => (
                  <p key={i} className="text-sm text-gray-800">
                    <b>{r.persona}</b>{r.hora && <span className="text-xs text-gray-500"> · {r.hora}</span>}: «{r.texto}»
                  </p>
                ))}
                {p1.siguiente ? (
                  <div className="space-y-1.5 pt-1">
                    <p className="text-xs text-emerald-900"><b>{p1.siguiente.remitido_por}</b> indica que el dato lo tiene <b>{p1.siguiente.nombre}</b>. Pregúntale:</p>
                    <CandidatoPregunta key={'sig-' + p1.siguiente.nombre} analisisId={sel} candidato={p1.siguiente} apiBase={apiBase} onHecho={alHechoPaso1} />
                  </div>
                ) : !p1.lectura?.zona && (
                  <p className="text-xs text-gray-600">La máquina no encontró en la respuesta ni la zona ni a quién más preguntar: léela y decide.</p>
                )}
              </div>
            )}
            <PonerZona key={p1.lectura?.zona || 'z'} regId={det.reg_id} sugerida={p1.lectura?.zona} apiBase={apiBase} onHecho={alHechoPaso1} />
          </>
        )}
      </div>
    ) : null
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button onClick={() => { setSel(null); setObsEdit(null); setMsgEdit(null) }}
              className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-800">
              <ChevronLeft size={15} /> Volver a la lista
            </button>
            {desdeTabla && onVolverTabla && (
              <button onClick={onVolverTabla}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-blue-600 text-white rounded-lg hover:bg-blue-700">
                <ChevronLeft size={15} /> Volver a la tabla
              </button>
            )}
          </div>
          <div className="flex items-center gap-2">
            {sinAnalizar.length > 0 && (
              <button onClick={() => analizarPendientes.mutate(false)} disabled={analizarPendientes.isPending}
                title={sinAnalizar.map((c) => `${c.pdl} — ${c.motivo}`).join('\n')}
                className="px-3 py-1.5 text-xs rounded-lg bg-amber-100 text-amber-900 border border-amber-300 hover:bg-amber-200 disabled:opacity-50">
                {analizarPendientes.isPending ? 'Analizando…' : `+${sinAnalizar.length} sin análisis: analizar`}
              </button>
            )}
            {quedanPendientes > 0 && (
              <button onClick={irSiguiente} title="Casos cuya celda todavía falta en la tabla (decidir SI/NO o escribir la razón)"
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-violet-600 text-white rounded-lg hover:bg-violet-700">
                Ver siguiente pendiente ({quedanPendientes}) →
              </button>
            )}
          </div>
        </div>

        {/* Si este caso tiene un re-análisis posterior (p. ej. con el motor de
            tiempos nuevo), avisarlo: mirar el viejo hace creer que faltan cosas */}
        {(() => {
          const regIdSel = det.reg_id ?? lista.find((x) => x.id === sel)?.reg_id
          const masReciente = regIdSel != null
            ? lista.filter((x) => x.reg_id === regIdSel && x.id > sel && x.estado !== 'descartado')
                   .sort((a, b) => b.id - a.id)[0]
            : null
          if (!masReciente) return null
          return (
            <div className="bg-amber-50 border border-amber-300 rounded-xl px-4 py-3 text-sm text-amber-900 flex flex-wrap items-center gap-2">
              <AlertTriangle size={15} className="shrink-0" />
              <span>Estás viendo un análisis <b>antiguo</b> de este caso. Hay un re-análisis más
                reciente (#{masReciente.id}) con el análisis rápido de tiempos y los detalles nuevos.</span>
              <button onClick={() => { setSel(masReciente.id); setObsEdit(null); setMsgEdit(null) }}
                className="px-3 py-1 text-xs font-semibold bg-amber-600 text-white rounded-lg hover:bg-amber-700">
                Ver el análisis más reciente →
              </button>
            </div>
          )
        })()}

        {/* El caso */}
        <div className="bg-white border rounded-xl p-4">
          <div className="flex flex-wrap items-center gap-2 mb-2">
            <Bot size={18} className="text-violet-600" />
            <h3 className="font-semibold">{det.pdl} · {det.zona}</h3>
            <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${ESTADO[det.flujo || det.estado] || ''}`}>
              {ESTADO_LABEL[det.flujo || det.estado] || det.estado}{det.flujo === 'respondido' && det.flujo_detalle === 'por correo' ? ' — por correo' : ''}
            </span>
            {det.embudo && det.embudo.paso !== 'sin_respuesta' && (
              <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${(EMBUDO[det.embudo.paso] || [])[1] || 'bg-gray-100 text-gray-600'}`}>
                {(EMBUDO[det.embudo.paso] || ['sin clasificar'])[0]}
              </span>
            )}
            {ia.confianza && <span className={`text-xs font-medium ${CONF[ia.confianza]}`}>confianza {ia.confianza}</span>}
            {(det.correos_caso || []).length > 0 && (
              <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-cyan-100 text-cyan-800"
                title={det.correos_caso.map((c) => `#${c.id} · ${c.asunto} (${c.estado})`).join('\n')}>
                 {det.correos_caso.length} correo{det.correos_caso.length !== 1 ? 's' : ''} del caso en Correos ANS
              </span>
            )}
          </div>
          <pre className="text-xs text-gray-600 whitespace-pre-wrap font-sans">{res.caso?.contexto}</pre>
          {det.flujo === 'error_envio' && (
            <p className="mt-2 text-xs text-red-600 flex items-center gap-1.5">
              <AlertTriangle size={13} /> El bot no pudo enviar el mensaje: {det.flujo_detalle || 'error desconocido'}
            </p>
          )}
        </div>

        {conError && (
          <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-800 flex gap-2">
            <AlertTriangle size={16} className="shrink-0" /> La IA no pudo analizar: {ia.error}
          </div>
        )}

        {/* Conversación de WhatsApp del caso (25-08): la trazabilidad completa aquí
            mismo — lo que el bot envió y lo que respondieron, sin abrir WhatsApp */}
        {/* El chat TAL CUAL WhatsApp (2026-10-01, R09583): el mismo de «Ver justificación». Si el
            hilo no tiene chat guardado, abajo queda la conversación por turnos de siempre. */}
        {(det.chat || []).length > 0 && (
          <div className="bg-white border rounded-xl p-4 space-y-2">
            <h4 className="text-sm font-semibold flex items-center gap-1.5">
              <MessageSquare size={15} /> Conversación de WhatsApp del caso
              <span className="text-xs font-normal text-gray-500">(tal cual quedó en WhatsApp, con su hora — lo de otros casos sale atenuado)</span>
            </h4>
            <ChatWhatsApp chats={det.chat} apiBase={apiBase} rutaMedia={(id) => `/wa/media/${id}`} />
            {(det.conversacion || []).some((m) => m.direccion === 'ia') && (
              <details className="rounded-lg border border-amber-200 bg-amber-50/60 px-3 py-2">
                <summary className="cursor-pointer text-xs font-medium text-amber-900 select-none">
                  Lo que leyó la máquina en la conversación ({det.conversacion.filter((m) => m.direccion === 'ia').length})
                </summary>
                <ul className="mt-2 space-y-1">
                  {det.conversacion.filter((m) => m.direccion === 'ia').map((m, i) => (
                    <li key={i} className="text-xs text-amber-900 whitespace-pre-wrap"><b>{m.hora || ''}:</b> {m.texto}</li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
        {!(det.chat || []).length && (det.conversacion || []).length > 0 && (
          <div className="bg-white border rounded-xl p-4">
            <h4 className="text-sm font-semibold mb-2 flex items-center gap-1.5">
              <MessageSquare size={15} /> Conversación de WhatsApp del caso
              <span className="text-xs font-normal text-gray-500">({det.conversacion.length} mensajes — las respuestas ya las procesó la máquina)</span>
            </h4>
            <div className="space-y-2 max-h-96 overflow-y-auto pr-1">
              {det.conversacion.map((m, i) => (
                m.direccion === 'ia' ? (
                  <div key={i} className="flex justify-center">
                    <div className="max-w-[85%] rounded-lg px-3 py-1.5 text-xs bg-amber-50 border border-amber-200 text-amber-900 whitespace-pre-wrap">
                      <b>lectura de la máquina · {m.hora || ''}:</b> {m.texto}
                    </div>
                  </div>
                ) : (
                  <div key={i} className={`flex ${m.direccion === 'enviado' ? 'justify-end' : 'justify-start'}`}>
                    <div className={`max-w-[80%] rounded-xl px-3 py-2 text-sm whitespace-pre-wrap ${m.direccion === 'enviado' ? 'bg-violet-100 text-violet-900' : 'bg-gray-100 text-gray-800'}`}>
                      <p className="text-[10px] text-gray-500 mb-0.5">
                        {m.direccion === 'enviado' ? `${m.via === 'bot' || !m.via ? 'bot' : `nosotros (${m.via})`} → ${m.persona || ''}` : (m.persona || 'respuesta')} · {m.hora || ''}
                        {m.msg_id && <span className="ml-1 text-gray-400">· {m.tipo}</span>}
                      </p>
                      {/* Gráfico antes que párrafo: la foto / el audio primero, las palabras
                          de la persona después y lo que leyó la máquina plegado. */}
                      {m.msg_id && <MediaChat msgId={m.msg_id} tipo={m.tipo} apiBase={apiBase} />}
                      {m.texto && <div className={m.msg_id ? 'mt-1.5' : ''}>{m.texto}</div>}
                      {m.adjunto && !m.msg_id && <p className="mt-1 text-[10px] text-gray-400">(adjunto no disponible: el archivo ya no está en el buzón del bot)</p>}
                      {m.lectura && (
                        <details className="mt-1.5">
                          <summary className="cursor-pointer text-[11px] text-violet-700 select-none">Lo que leyó la máquina de {m.tipo === 'audio' ? 'la nota de voz' : m.tipo === 'video' ? 'el audio del video' : m.tipo === 'imagen' ? 'la imagen' : 'el adjunto'} ▸</summary>
                          <p className="mt-1 text-xs text-gray-600 whitespace-pre-wrap border-l-2 border-violet-200 pl-2">{m.lectura}</p>
                        </details>
                      )}
                      {m.msg_id && !m.lectura && ['audio', 'imagen', 'video'].includes(m.tipo) && (
                        <p className="mt-1 text-[10px] text-amber-700">La máquina aún no ha leído este adjunto — pulsa «Releer la conversación».</p>
                      )}
                    </div>
                  </div>
                )
              ))}
            </div>
          </div>
        )}

        {/* EL EMBUDO: qué respondieron, de qué calidad y cuál es la salida del caso (24-09).
            Va por encima del resto porque, cuando hay conversación, ES el camino. */}
        <EmbudoPanel analisisId={sel} det={det} apiBase={apiBase}
          onHecho={() => { qc.invalidateQueries({ queryKey: ['wa-analisis'] }); qc.invalidateQueries({ queryKey: ['wa-analisis-det'] }); qc.invalidateQueries({ queryKey: ['wa-tablero'] }) }} />

        {/* ── Caso 3.2 B (anulada): la propuesta es el MENSAJE al profesional de apoyo ── */}
        {!conError && es32B && (
          <div className="space-y-4">
            <div className="bg-violet-50 border border-violet-200 rounded-xl p-4 space-y-2">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <b>Ruta de la máquina:</b>
                <span className="px-2 py-0.5 rounded-full text-xs font-bold bg-violet-200 text-violet-900">CASO 3.2 B · ANULACIÓN</span>
              </div>
              <p className="text-sm text-gray-700">{ia.resumen}</p>
            </div>
            {paso1}
            <div className="bg-white border rounded-xl p-4 space-y-3">
              <h4 className="text-sm font-semibold">Mensaje al profesional de apoyo (edítalo si hace falta)</h4>
              {consulta.sin_zona
                ? <p className="text-sm text-amber-900 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">{consulta.sin_contacto}</p>
                : consulta.sin_contacto
                ? <CrearApoyo zona={det.zona} apiBase={apiBase}
                    onCreado={() => qc.invalidateQueries({ queryKey: ['wa-analisis-det'] })} />
                : <p className="text-sm text-gray-600">Para: <b>{consulta.persona}</b> · <span className="font-mono text-xs">+{consulta.telefono}</span></p>}
              <textarea value={msgEdit ?? consulta.mensaje ?? ''} onChange={(e) => setMsgEdit(e.target.value)}
                rows={9} className="w-full border rounded-lg p-2 text-sm" disabled={det.estado !== 'propuesto'} />
              {/* BLOQUEADOR (incidente del 27-08): si el correo del caso ya está
                  en Correos ANS, pedirlo por WhatsApp es ilógico — el botón desaparece */}
              {det.estado === 'propuesto' && !consulta.sin_contacto && det.bloqueo_envio && (
                <div className="bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-sm text-red-800 space-y-2">
                  <p className="flex items-start gap-1.5"><AlertTriangle size={15} className="shrink-0 mt-0.5" />
                    <span><b>Bloqueado:</b> {det.bloqueo_envio}.</span></p>
                  <div className="flex flex-wrap gap-2">
                    <a href="/ans?tab=correos-ans" className="px-3 py-1.5 text-xs rounded-lg bg-white border border-red-300 text-red-700 hover:bg-red-100">
                      Ir a Correos ANS
                    </a>
                    <button onClick={() => aplicar.mutate({ forzar: true })} disabled={aplicar.isPending}
                      className="px-3 py-1.5 text-xs rounded-lg bg-white border text-gray-500 hover:bg-gray-50 disabled:opacity-50">
                      Enviar de todas formas
                    </button>
                    <button onClick={() => descartar.mutate()}
                      className="px-3 py-1.5 text-xs rounded-lg bg-white border text-gray-600 hover:bg-gray-50">
                      Descartar propuesta
                    </button>
                  </div>
                </div>
              )}
              {det.estado === 'propuesto' && !consulta.sin_contacto && !det.bloqueo_envio && (
                <div className="flex gap-2">
                  <button onClick={() => aplicar.mutate()} disabled={aplicar.isPending}
                    className="flex items-center gap-2 px-4 py-2 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">
                    {aplicar.isPending ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />}
                    Encolar: el bot lo envía en su próximo ciclo
                  </button>
                  <button onClick={() => descartar.mutate()}
                    className="flex items-center gap-2 px-4 py-2 text-sm bg-white border text-gray-600 rounded-lg hover:bg-gray-50">
                    <XCircle size={15} /> Descartar
                  </button>
                </div>
              )}
              {det.estado === 'aplicado' && (
                <p className="text-xs text-emerald-700">Encolado. Cuando el apoyo responda, su mensaje cae por el bot y sigue el flujo del chat guiado; la bitácora «a la espera» ya quedó en la celda del 3.2 B.</p>
              )}
            </div>
          </div>
        )}

        {!conError && !es32B && (<>
          {/* Veredicto — o REVISIÓN (auditoría 24-08): la máquina no se compromete */}
          <div className={`border rounded-xl p-4 space-y-2 ${ia.revision ? 'bg-orange-50 border-orange-200' : 'bg-violet-50 border-violet-200'}`}>
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <b>Propuesta de la máquina:</b>
              {ia.revision ? (
                <span className="px-2 py-0.5 rounded-full text-xs font-bold bg-orange-200 text-orange-900">
                  REVISIÓN · potencialmente excluible
                </span>
              ) : (
                <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${BADGE[ia.excluible_sugerido] || 'bg-gray-200'}`}>
                  excluible {ia.excluible_sugerido || '—'}
                </span>
              )}
              {ia.categoria && <span className="px-2 py-0.5 rounded-full text-xs bg-white border">{ia.categoria}</span>}
            </div>
            {ia.causa && <p className="text-sm text-gray-800"><b>Causa:</b> {ia.causa}{ia.causa_tipo ? ` (${ia.causa_tipo})` : ''}</p>}
            <p className="text-sm text-gray-700">{ia.resumen}</p>
            <p className="text-xs text-gray-600"><b>Cobertura del desfase:</b> {ia.cobertura_desfase}</p>
            {ia.revision ? (
              <p className="text-xs text-orange-800">
                <b>Por qué está en Revisión:</b> {ia.revision.motivo}. Posición provisional: no exclusión
                hasta completar la evidencia. La decisión final (SI/NO) es del ING — abajo los dos botones
                tienen el mismo peso.
              </p>
            ) : (
              <p className="text-[11px] text-violet-700">Tu aprobación decide: al aplicar se escribe el SI/NO que elijas abajo, la observación y los videos como evidencia.</p>
            )}
          </div>

          {/* Gráfica del análisis rápido de tiempos */}
          <CoberturaRapida cov={ia.cobertura_calc} />

          {/* Línea de tiempo */}
          <div className="bg-white border rounded-xl p-4">
            <h4 className="text-sm font-semibold mb-2 flex items-center gap-1.5"><Clock size={15} /> Línea de tiempo que armó la máquina</h4>
            {(det.grupos_nombres || []).length > 0 && (
              <p className="text-xs text-gray-500 mb-2">Material consultado de: <b>{det.grupos_nombres.join(' · ')}</b></p>
            )}
            <ul className="space-y-1">
              {(ia.linea_tiempo || []).map((e, i) => (
                <li key={i} className="text-sm flex gap-2">
                  <span className="font-mono text-xs text-gray-500 shrink-0 w-12">{e.hora}</span>
                  <span className="text-[10px] uppercase text-gray-400 shrink-0 w-24">
                    {e.fuente}
                    {e.grupo && <span className="block normal-case text-violet-500">{e.grupo}</span>}
                  </span>
                  <span>
                    {e.hecho}
                    {/* Auditoría 2026-10-01 (R09601): la hora la DICE el audio; el video se grabó a otra hora */}
                    {e.dicho && (
                      <span className="ml-2 px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 text-[11px] whitespace-nowrap">
                        dicho en el audio · video grabado {e.grabado}
                      </span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          </div>

          {/* Videos analizados: de frente solo los DEL CASO; los rechazados van plegados
              (mostrarlos como si contaran fue uno de los bugs del piloto) */}
          <div className="bg-white border rounded-xl p-4 space-y-4">
            <h4 className="text-sm font-semibold flex items-center gap-1.5"><Video size={15} /> Videos del caso (atribución por overlay)</h4>
            {!(ia.videos || []).some((v) => v.del_caso === 'SI') && (
              <p className="text-sm text-gray-500">Ningún video del día pertenece a este PDL.</p>
            )}
            {(ia.videos || []).filter((v) => v.del_caso === 'SI').map((v) => (
              <div key={v.msg_id} className="border rounded-lg p-3 space-y-2">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${BADGE[v.del_caso]}`}>
                    {v.del_caso === 'SI' ? 'DEL CASO' : v.del_caso === 'NO' ? 'DE OTRO PDL' : 'DUDA'}
                  </span>
                  {/* el mismo número que citan la línea de tiempo y los tramos (msg_id=…) */}
                  <span className="px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 text-[11px] font-mono">MSG {v.msg_id}</span>
                  {v.pdl_overlay && <span className="text-xs font-mono">{v.pdl_overlay}</span>}
                  <RelojVideo v={v} analista={ia.analista} meta={(res.videos || []).find((x) => x.msg_id === v.msg_id)} />
                  {(() => {
                    const meta = (res.videos || []).find((x) => x.msg_id === v.msg_id)
                    return meta?.grupo ? <span className="text-xs text-gray-400">· {meta.grupo}</span> : null
                  })()}
                </div>
                {v.overlay && <p className="text-xs text-gray-500">Overlay leído: {v.overlay}</p>}
                <p className="text-sm">{v.que_se_ve}</p>
                {v.que_se_oye && <p className="text-xs text-gray-600 italic">Audio: {v.que_se_oye}</p>}
                {(() => {
                  const meta = (res.videos || []).find((x) => x.msg_id === v.msg_id)
                  return meta?.transcripcion
                    ? <details className="text-xs text-gray-500"><summary className="cursor-pointer">Transcripción completa</summary><p className="mt-1">{meta.transcripcion}</p></details>
                    : null
                })()}
                <VideoWa msgId={v.msg_id} apiBase={apiBase} />
              </div>
            ))}
            {(ia.videos || []).some((v) => v.del_caso !== 'SI') && (
              <details>
                <summary className="text-xs text-gray-500 cursor-pointer">
                  Videos revisados y descartados ({(ia.videos || []).filter((v) => v.del_caso !== 'SI').length}) — de otros PDL o sin atribución
                </summary>
                <div className="mt-2 space-y-3">
                  {(ia.videos || []).filter((v) => v.del_caso !== 'SI').map((v) => (
                    <div key={v.msg_id} className="border rounded-lg p-3 space-y-1 opacity-70">
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${BADGE[v.del_caso]}`}>
                          {v.del_caso === 'NO' ? 'DE OTRO PDL' : 'DUDA'}
                        </span>
                        <span className="px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 text-[11px] font-mono">MSG {v.msg_id}</span>
                        {v.pdl_overlay && <span className="text-xs font-mono">{v.pdl_overlay}</span>}
                        {(v.grabado || v.hora_overlay) && <RelojVideo v={v} analista={ia.analista} />}
                      </div>
                      <p className="text-xs text-gray-600">{v.que_se_ve}</p>
                      <VideoWa msgId={v.msg_id} apiBase={apiBase} />
                    </div>
                  ))}
                </div>
              </details>
            )}
            {/* Validación determinista (auditoría 2026-09-22): lo que el sistema corrigió de la IA
                por letrero/audio, y los videos que ni se le mostraron por ser de otra orden */}
            {(ia.validacion || []).length > 0 && (
              <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 space-y-1">
                <p className="text-xs font-semibold text-amber-800">Validación del sistema (letrero, audio y tiempos) — lo que corrigió o advierte sobre la IA:</p>
                <ul className="list-disc pl-4 space-y-0.5">
                  {ia.validacion.map((t, i) => <li key={i} className="text-xs text-amber-900">{t}</li>)}
                </ul>
              </div>
            )}
            {(res.videos_excluidos || []).length > 0 && (
              <details>
                <summary className="text-xs text-gray-500 cursor-pointer">
                  Videos del día con letrero de OTRA orden ({res.videos_excluidos.length}) — excluidos antes del análisis
                </summary>
                <ul className="mt-2 space-y-1">
                  {res.videos_excluidos.map((v) => (
                    <li key={v.msg_id} className="text-xs text-gray-600 flex flex-wrap gap-2 items-center">
                      <span className="font-mono">{v.codigo}</span>
                      <span>subido {v.hora_subida}</span>
                      {v.grupo && <span className="text-gray-400">· {v.grupo}</span>}
                      <span className="text-gray-400">· leído por {v.fuente === 'vision' ? 'visión IA' : 'OCR'}</span>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>

          {/* Mensajes del día */}
          <details className="bg-white border rounded-xl p-4">
            <summary className="text-sm font-semibold cursor-pointer flex items-center gap-1.5">
              <MessageSquare size={15} /> Mensajes del grupo ese día ({(res.eventos || []).length})
            </summary>
            <ul className="mt-2 space-y-1">
              {(res.eventos || []).map((e, i) => <li key={i} className="text-xs text-gray-600 font-mono whitespace-pre-wrap">{e}</li>)}
            </ul>
          </details>

          {paso1}

          {/* Paso 2: preguntar al líder/supervisor — imprescindible cuando no hubo
              evidencia; opcional cuando sí la hay pero quieres confirmar */}
          {det.estado === 'propuesto' && (det.preguntar || []).length > 0 && (
            <div className="bg-white border rounded-xl p-4 space-y-3">
              <h4 className="text-sm font-semibold flex items-center gap-1.5">
                <MessageSquare size={15} /> Paso 2 · Preguntar por WhatsApp
                <span className="text-xs font-normal text-gray-500">
                  {ia.revision ? '(los tiempos no quedan cubiertos — pide la evidencia que falta)'
                    : ia.excluible_sugerido ? '(opcional: ya hay evidencia en video)' : '(la máquina no encontró evidencia — este es el camino)'}
                </span>
              </h4>
              {det.preguntar.map((c) => (
                <CandidatoPregunta key={c.nombre} analisisId={sel} candidato={c} apiBase={apiBase}
                  onHecho={() => { qc.invalidateQueries({ queryKey: ['wa-analisis'] }); qc.invalidateQueries({ queryKey: ['wa-analisis-det'] }) }} />
              ))}
            </div>
          )}

          {/* LO QUE JUSTIFICA Y LO QUE SE ADJUNTA (14 oct 2026): resumen para quien expone, justo encima de la observación */}
          <LineaTiempoCaso rt={ia.resumen_tiempos} apiBase={apiBase} analisisId={sel} />

          {/* Observación propuesta + acciones */}
          <div className="bg-white border rounded-xl p-4 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h4 className="text-sm font-semibold">Observación que escribiría en la celda (edítala si hace falta)</h4>
              {det.estado === 'propuesto' && (
                <button onClick={() => setObsEdit(depurarObs(obsEdit ?? ia.observacion ?? ''))}
                  title="Quita de la observación lo que es proceso interno (videos de otros PDL, por validar, confianza…) — la celda solo habla de este caso"
                  className="px-3 py-1 text-xs font-medium bg-white border text-violet-700 rounded-lg hover:bg-violet-50">
                  Actualizar observación — quitar lo interno
                </button>
              )}
            </div>
            {/* Doble redacción (25-08): en Revisión la máquina trae DOS versiones — la
                precisa (la verdad completa) y la argumentativa favorable (verdadera
                frase a frase, centrada en lo externo). El auditor elige cuál usar. */}
            {ia.observacion_favorable && det.estado === 'propuesto' && (
              <div className="grid sm:grid-cols-2 gap-2">
                <div className="border rounded-lg p-2 text-xs space-y-1 bg-gray-50">
                  <div className="flex items-center justify-between gap-2">
                    <b>Versión precisa (la verdad completa)</b>
                    <button onClick={() => setObsEdit(ia.observacion)}
                      className="px-2 py-0.5 text-[11px] bg-white border rounded-lg hover:bg-gray-100 shrink-0">Usar esta</button>
                  </div>
                  <p className="font-mono whitespace-pre-wrap text-gray-600">{ia.observacion}</p>
                </div>
                <div className="border border-emerald-300 rounded-lg p-2 text-xs space-y-1 bg-emerald-50">
                  <div className="flex items-center justify-between gap-2">
                    <b>Versión argumentativa (favorable, sin faltar a la verdad)</b>
                    <button onClick={() => setObsEdit(ia.observacion_favorable)}
                      className="px-2 py-0.5 text-[11px] bg-white border border-emerald-400 text-emerald-700 rounded-lg hover:bg-emerald-100 shrink-0">Usar esta</button>
                  </div>
                  <p className="font-mono whitespace-pre-wrap text-gray-700">{ia.observacion_favorable}</p>
                </div>
              </div>
            )}
            <textarea value={obsEdit ?? ia.observacion ?? ''} onChange={(e) => setObsEdit(e.target.value)}
              rows={5} className="w-full border rounded-lg p-2 text-sm font-mono" disabled={det.estado !== 'propuesto'} />
            {/* Con conversación respondida, la decisión se toma ARRIBA, en el embudo (con la
                observación que salió del chat): dos sitios para lo mismo confunden. */}
            {det.embudo?.puede_decidir && !det.embudo?.hilo_resuelto && (
              <p className="text-xs text-gray-500">Este caso ya tiene respuesta por WhatsApp: decídelo arriba, en <b>Embudo de la respuesta</b> — allí va la observación que salió de la conversación.</p>
            )}
            {det.estado === 'propuesto' && !det.embudo?.puede_decidir && (
              <div className="flex flex-wrap gap-2">
                {/* Tu clic ES la decisión humana (evolución del 19-08): el botón principal
                    aplica el SI/NO que propuso la máquina; el secundario, lo contrario. */}
                {ia.excluible_sugerido ? (<>
                  <button onClick={() => aplicar.mutate({ decision: ia.excluible_sugerido })} disabled={aplicar.isPending}
                    className={`flex items-center gap-2 px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50 ${ia.excluible_sugerido === 'SI' ? 'bg-emerald-600 hover:bg-emerald-700' : 'bg-red-600 hover:bg-red-700'}`}>
                    {aplicar.isPending ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />}
                    Quedó bien — aplicar (excluible {ia.excluible_sugerido})
                  </button>
                  <button onClick={() => aplicar.mutate({ decision: ia.excluible_sugerido === 'SI' ? 'NO' : 'SI' })} disabled={aplicar.isPending}
                    className="flex items-center gap-2 px-3 py-2 text-sm bg-white border text-gray-700 rounded-lg hover:bg-gray-50 disabled:opacity-50">
                    Corregir: aplicar excluible {ia.excluible_sugerido === 'SI' ? 'NO' : 'SI'}
                  </button>
                  <button onClick={() => aplicar.mutate({})} disabled={aplicar.isPending}
                    className="flex items-center gap-2 px-3 py-2 text-sm bg-white border text-gray-500 rounded-lg hover:bg-gray-50 disabled:opacity-50">
                    Solo observación y videos
                  </button>
                </>) : ia.revision ? (<>
                  {/* REVISIÓN: la máquina no propone — el ING decide, ambos botones pesan igual */}
                  <button onClick={() => aplicar.mutate({ decision: 'SI' })} disabled={aplicar.isPending}
                    className="flex items-center gap-2 px-4 py-2 text-sm bg-white border-2 border-emerald-600 text-emerald-700 rounded-lg hover:bg-emerald-50 disabled:opacity-50">
                    {aplicar.isPending ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />}
                    Decisión del ING: excluible SI
                  </button>
                  <button onClick={() => aplicar.mutate({ decision: 'NO' })} disabled={aplicar.isPending}
                    className="flex items-center gap-2 px-4 py-2 text-sm bg-white border-2 border-red-500 text-red-600 rounded-lg hover:bg-red-50 disabled:opacity-50">
                    {aplicar.isPending ? <Loader2 size={15} className="animate-spin" /> : <XCircle size={15} />}
                    Decisión del ING: excluible NO
                  </button>
                  <button onClick={() => aplicar.mutate({})} disabled={aplicar.isPending}
                    className="flex items-center gap-2 px-3 py-2 text-sm bg-white border text-gray-500 rounded-lg hover:bg-gray-50 disabled:opacity-50">
                    Solo observación y videos
                  </button>
                </>) : (
                  <button onClick={() => aplicar.mutate({})} disabled={aplicar.isPending}
                    className="flex items-center gap-2 px-4 py-2 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">
                    {aplicar.isPending ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />}
                    Aplicar observación y videos (sin decisión: no hay veredicto)
                  </button>
                )}
                <button onClick={() => descartar.mutate()}
                  className="flex items-center gap-2 px-4 py-2 text-sm bg-white border text-gray-600 rounded-lg hover:bg-gray-50">
                  <XCircle size={15} /> Descartar
                </button>
              </div>
            )}
          </div>
        </>)}
      </div>
    )
  }

  // ── Lista + lanzar análisis ─────────────────────────────────────────────────
  return (
    <div className="space-y-4">
      {desdeTabla && onVolverTabla && (
        <div className="flex items-center gap-3 bg-blue-50 border border-blue-200 rounded-xl px-4 py-2 text-sm text-blue-900">
          <span>Llegaste desde la tabla{pdl ? <> por <b className="font-mono">{pdl}</b>: aún no tiene análisis — pulsa «Analizar» abajo.</> : '.'}</span>
          <button onClick={onVolverTabla}
            className="ml-auto flex items-center gap-1.5 px-3 py-1 text-xs font-medium bg-blue-600 text-white rounded-lg hover:bg-blue-700">
            <ChevronLeft size={13} /> Volver a la tabla
          </button>
        </div>
      )}
      {/* Lo que la TABLA pide y el tab aún no tiene (2026-10-01): sin esto, un caso cuyo análisis
          se cayó no aparecía en ninguna parte y el conteo de pendientes no cuadraba con la tabla. */}
      {sinAnalizar.length > 0 && (
        <div className="flex flex-wrap items-center gap-3 bg-amber-50 border border-amber-300 rounded-xl px-4 py-2.5 text-sm text-amber-900">
          <AlertTriangle size={16} className="shrink-0" />
          <span>La tabla pide <b>{sinAnalizar.length}</b> caso(s) que todavía no tienen análisis:{' '}
            <span className="font-mono">{sinAnalizar.map((c) => c.pdl).join(', ')}</span>.</span>
          <button onClick={() => analizarPendientes.mutate(false)} disabled={analizarPendientes.isPending}
            className="ml-auto px-3 py-1 text-xs rounded-lg bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-50">
            {analizarPendientes.isPending ? 'Analizando…' : 'Analizarlos ahora'}
          </button>
        </div>
      )}
      {pendientes.length > 0 && (
        <div className="flex items-center gap-3 text-sm">
          <span className="text-gray-600">Faltan en la tabla <b>{pendientes.length + sinAnalizar.length}</b> caso(s)
            {sinAnalizar.length ? ` (${pendientes.length} con análisis · ${sinAnalizar.length} sin análisis)` : ''}.</span>
          <button onClick={irSiguiente}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-violet-600 text-white rounded-lg hover:bg-violet-700">
            Ir al primero →
          </button>
        </div>
      )}
      {periodo && tablero.length > 0 && (
        <div className="bg-white border rounded-xl">
          <button onClick={() => setVerTablero((v) => !v)} className="w-full flex items-center gap-2 px-4 py-2 text-left">
            <span className="font-semibold text-sm">Tablero del periodo {periodo}</span>
            <span className="text-xs text-gray-500">{tablero.length} caso(s) por auditar ·{' '}
              {Object.entries(ESTADO_CICLO).map(([k, [l]]) => `${tablero.filter((x) => x.estado === k).length} ${l.toLowerCase()}`).join(' · ')}</span>
            <span className="ml-auto text-xs text-gray-400">{verTablero ? 'ocultar' : 'ver'}</span>
          </button>
          {verTablero && (
            <div className="px-3 pb-3">
              <div className="flex items-center gap-2 mb-2">
                <button onClick={enviarAprobados} disabled={!aprobados.length || pedirFaltantes.isPending}
                  title="Envía por WhatsApp SOLO los pedidos que marcaste (te pide confirmación con la lista)"
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">
                  {pedirFaltantes.isPending ? <Loader2 size={13} className="animate-spin" /> : <MessageSquare size={13} />} Enviar pedidos marcados ({aprobados.length})
                </button>
                <span className="text-[11px] text-gray-500">La máquina PROPONE a quién pedir la evidencia; nada sale sin que lo marques y confirmes.</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="text-[10px] uppercase text-gray-500">
                    <tr><th className="text-left px-2 py-1">PDL</th><th className="text-left px-2 py-1">Tipo</th><th className="text-left px-2 py-1">Estado</th>
                      <th className="text-left px-2 py-1">Evidencia</th><th className="text-left px-2 py-1">Pedido</th><th className="text-left px-2 py-1">Qué falta</th></tr>
                  </thead>
                  <tbody>
                    {tablero.map((c) => (
                      <tr key={c.reg_id} className="border-t hover:bg-gray-50 cursor-pointer" onClick={() => { if (c.analisis_id) { setSel(c.analisis_id); setObsEdit(null); setMsgEdit(null) } }}>
                        <td className="px-2 py-1 font-mono">{c.pdl}<span className="block text-[10px] text-gray-400 font-sans">{c.zona || '—'} · {c.fecha}</span></td>
                        <td className="px-2 py-1">{c.tipo}</td>
                        <td className="px-2 py-1"><span className={`px-2 py-0.5 rounded-full ${(ESTADO_CICLO[c.estado] || [])[1] || ''}`}>{(ESTADO_CICLO[c.estado] || [c.estado])[0]}</span>
                          {c.propuesta && <span className="block text-[10px] text-gray-500">propone {c.propuesta}</span>}
                          {c.decision && <span className="block text-[10px] text-gray-700">decidido: {c.decision}</span>}</td>
                        <td className="px-2 py-1 text-gray-600">{c.amarres_fuertes} video(s) amarrados · {c.candidatos} candidatos · {c.evidencias} evid. · {c.correos} correo(s)</td>
                        <td className="px-2 py-1 text-gray-600" onClick={(e) => e.stopPropagation()}>
                          {c.pedido ? `${c.pedido.persona} · ${c.pedido.estado}${c.pedido.respondido ? ' · respondió' : ''}`
                            : c.pedido_propuesto ? (
                              c.pedido_propuesto.posible
                                ? <label className="flex items-center gap-1.5 cursor-pointer">
                                    <input type="checkbox" checked={aprobados.includes(c.reg_id)}
                                      onChange={(e) => setAprobados((p) => e.target.checked ? [...p, c.reg_id] : p.filter((x) => x !== c.reg_id))} />
                                    <span>propuesto: <b>{c.pedido_propuesto.persona}</b> ({c.pedido_propuesto.rol})</span>
                                  </label>
                                : <span className="text-amber-700">no se pide: {c.pedido_propuesto.bloqueo}</span>)
                            : '—'}
                        </td>
                        <td className="px-2 py-1 text-gray-700">{c.falta}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
      <div className="flex gap-2">
        <input value={pdl} onChange={(e) => setPdl(e.target.value)} placeholder="PDL del caso (ej. CDOPP2026R07474)"
          className="border rounded-lg px-3 py-2 text-sm w-72 font-mono"
          onKeyDown={(e) => e.key === 'Enter' && pdl && analizar.mutate()} />
        <button onClick={() => analizar.mutate()} disabled={!pdl || analizar.isPending}
          className="flex items-center gap-2 px-4 py-2 text-sm bg-violet-600 text-white rounded-lg hover:bg-violet-700 disabled:opacity-50">
          {analizar.isPending ? <Loader2 size={15} className="animate-spin" /> : <Bot size={15} />}
          {analizar.isPending ? 'Analizando (1–3 min: frames + audio + IA)…' : 'Analizar caso'}
        </button>
        {periodo && (
          <button onClick={() => analizarPendientes.mutate(false)} disabled={!sinAnalizar.length || analizarPendientes.isPending}
            title={sinAnalizar.length ? sinAnalizar.map((c) => `${c.pdl} — ${c.motivo}`).join('\n') : 'Todos los casos del periodo que hay que auditar ya tienen análisis'}
            className="flex items-center gap-2 px-4 py-2 text-sm bg-white border border-violet-300 text-violet-700 rounded-lg hover:bg-violet-50 disabled:opacity-50">
            {analizarPendientes.isPending ? <Loader2 size={15} className="animate-spin" /> : <Bot size={15} />}
            Analizar pendientes ({sinAnalizar.length})
          </button>
        )}
        {periodo && sinClasificar.length > 0 && (
          <button onClick={() => releerPendientes.mutate()} disabled={releerPendientes.isPending}
            title={'Respuestas que llegaron pero que la máquina nunca clasificó: ' + sinClasificar.map((c) => c.pdl).join(', ')}
            className="flex items-center gap-2 px-4 py-2 text-sm bg-white border border-amber-300 text-amber-800 rounded-lg hover:bg-amber-50 disabled:opacity-50">
            {releerPendientes.isPending ? <Loader2 size={15} className="animate-spin" /> : <RefreshCw size={15} />}
            Releer {sinClasificar.length} respuesta(s) sin clasificar
          </button>
        )}
        {periodo && nFallidos > 0 && (
          <button onClick={() => analizarPendientes.mutate(true)} disabled={analizarPendientes.isPending}
            title="Vuelve a correr los análisis que terminaron en error (p. ej. la API de IA sin créditos)"
            className="self-center text-xs text-red-600 underline hover:text-red-800">
            Reintentar {nFallidos} fallido(s)
          </button>
        )}
        {nDescartados > 0 && (
          <button onClick={() => setVerDescartados((v) => !v)}
            className="ml-auto self-center text-xs text-gray-400 hover:text-gray-600 underline">
            {verDescartados ? 'ocultar descartados' : `mostrar descartados (${nDescartados})`}
          </button>
        )}
      </div>

      {/* Filtros para despachar rápido (25-08) */}
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <input value={fBusq} onChange={(e) => setFBusq(e.target.value)} placeholder="Buscar PDL…"
          className="border rounded-lg px-2.5 py-1.5 text-xs font-mono w-44" />
        <select value={fEstado} onChange={(e) => setFEstado(e.target.value)}
          className="border rounded-lg px-2 py-1.5 text-xs bg-white">
          <option value="">Estado: todos</option>
          {Object.entries(ESTADO_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select value={fZona} onChange={(e) => setFZona(e.target.value)}
          className="border rounded-lg px-2 py-1.5 text-xs bg-white">
          <option value="">Zona: todas</option>
          {zonas.map((z) => <option key={z} value={z}>{z}</option>)}
        </select>
        <select value={fProp} onChange={(e) => setFProp(e.target.value)}
          className="border rounded-lg px-2 py-1.5 text-xs bg-white">
          <option value="">Propuesta: todas</option>
          <option value="SI">excluible SI</option>
          <option value="NO">excluible NO</option>
          <option value="revision">Revisión (potencial)</option>
          <option value="sin">sin veredicto</option>
        </select>
        <select value={fEmbudo} onChange={(e) => setFEmbudo(e.target.value)}
          title="Solo entre los que ya respondieron: clasificados por la CALIDAD de su respuesta"
          className="border rounded-lg px-2 py-1.5 text-xs bg-white">
          <option value="">Respuesta: todas</option>
          {Object.entries(EMBUDO).filter(([k]) => k !== 'sin_respuesta').map(([k, v]) => (
            <option key={k} value={k}>{v[0]}</option>
          ))}
        </select>
        {(fBusq || fEstado || fZona || fProp || fEmbudo) && (
          <button onClick={() => { setFBusq(''); setFEstado(''); setFZona(''); setFProp(''); setFEmbudo('') }}
            className="text-xs text-violet-600 underline">quitar filtros</button>
        )}
        <span className="text-xs text-gray-400 ml-auto">{listaVisible.length} de {lista.length}{periodo ? ` · periodo ${periodo}` : ''}</span>
      </div>

      <div className="bg-white border rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-xs text-gray-500 uppercase">
            <tr>
              <th className="px-3 py-2 text-left">#</th><th className="px-3 py-2 text-left">PDL</th>
              <th className="px-3 py-2 text-left">Zona</th><th className="px-3 py-2 text-left">Fecha</th>
              <th className="px-3 py-2 text-left">Propuesta</th><th className="px-3 py-2 text-left">Confianza</th>
              <th className="px-3 py-2 text-left">Estado</th>
            </tr>
          </thead>
          <tbody>
            {listaVisible.map((a) => (
              <tr key={a.id} onClick={() => setSel(a.id)} className="border-t hover:bg-violet-50 cursor-pointer">
                <td className="px-3 py-2 text-gray-400">{a.id}</td>
                <td className="px-3 py-2 font-mono text-xs">{a.pdl}
                  <span className={`ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-sans font-medium ${a.tipo === '3.2B' ? 'bg-violet-100 text-violet-700' : 'bg-sky-100 text-sky-700'}`}>{a.tipo}</span>
                  <a href={`/ans?tab=auditoria-ia&analisis=${a.id}`} target="_blank" rel="noreferrer"
                    onClick={(e) => e.stopPropagation()} title="Abrir este caso en una pestaña nueva"
                    className="ml-1.5 inline-block align-middle text-gray-300 hover:text-violet-600">
                    <ExternalLink size={12} />
                  </a>
                </td>
                <td className="px-3 py-2">{a.zona}</td>
                <td className="px-3 py-2 text-xs">{a.fecha}</td>
                <td className="px-3 py-2">
                  {a.error ? <span className="text-xs text-red-600">error IA</span>
                    : (a.flujo === 'revision')
                      ? <span className="px-2 py-0.5 rounded-full text-xs font-bold bg-orange-100 text-orange-800">
                          potencialmente excluible{a.categoria ? ` · ${a.categoria}` : ''}
                        </span>
                      : <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${BADGE[a.excluible_sugerido] || 'bg-gray-100 text-gray-500'}`}>
                        {a.excluible_sugerido || 'sin veredicto'}{a.categoria ? ` · ${a.categoria}` : ''}
                      </span>}
                </td>
                <td className={`px-3 py-2 text-xs ${CONF[a.confianza] || ''}`}>{a.confianza || '—'}</td>
                <td className="px-3 py-2">
                  <span className={`px-2 py-0.5 rounded-full text-xs ${ESTADO[a.flujo || a.estado] || ''}`}>{ESTADO_LABEL[a.flujo || a.estado] || a.estado}</span>
                  {/* lo que dice la TABLA, que es lo que manda */}
                  {a.celda && !a.celda_pendiente && (
                    <span className="block mt-1 text-[10px] font-medium text-emerald-700">ya decidido en la tabla: {a.celda}</span>
                  )}
                  {a.falta_celda === 'razón' && (
                    <span className="block mt-1 text-[10px] font-medium text-rose-700">decidido {a.celda} · falta la razón en la tabla</span>
                  )}
                  {a.flujo === 'respondido' && (
                    <span className={`block mt-1 px-2 py-0.5 rounded-full text-[10px] font-medium w-fit ${(EMBUDO[a.embudo_paso] || [])[1] || 'bg-gray-100 text-gray-600'}`}>
                      {(EMBUDO[a.embudo_paso] || ['sin clasificar'])[0]}
                    </span>
                  )}
                </td>
              </tr>
            ))}
            {!listaVisible.length && (
              <tr><td colSpan={7} className="px-3 py-8 text-center text-gray-400 text-sm">
                {sinAnalizar.length
                  ? <>Hay <b className="text-gray-700">{sinAnalizar.length}</b> caso(s) del periodo por auditar sin análisis todavía
                      ({sinAnalizar.map((c) => c.pdl).join(', ')}) — pulsa <b>Analizar pendientes</b>.</>
                  : periodo
                    ? 'Este periodo no tiene casos por auditar (ningún NO CUMPLE de tiempos ni anulada sin decisión) o ya están todos analizados.'
                    : 'Aún no hay análisis — corre el primero con el PDL de un caso.'}
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
