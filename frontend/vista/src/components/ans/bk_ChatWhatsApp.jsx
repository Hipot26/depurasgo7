// La conversación tal cual quedó en WhatsApp (requisito 2026-10-01): se muestra como en
// WhatsApp, incluida la función «responder», para que quien expone la lea sin reinterpretarla.
// La arma el backend (`_chat_whatsapp` en ans.py) desde los
// mensajes guardados del chat directo: lo del bot, lo escrito a mano, las respuestas, las citas
// y la media, con la hora de WhatsApp. Aquí solo se pinta: burbujas, separador de día, cita
// dentro de la burbuja y la media en línea. Las lecturas de la IA NO van dentro del chat.
import { useEffect, useState } from 'react'
import { Loader2, Play, FileText } from 'lucide-react'
import api from '../../utils/api'

const DIAS = ['domingo', 'lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado']
const MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre',
  'octubre', 'noviembre', 'diciembre']

// Las horas llegan como ISO naive ya en hora de Bogotá: se leen por texto, sin pasar por la
// zona horaria del navegador.
const dia = (iso) => (iso || '').slice(0, 10)
function etiquetaDia(iso) {
  const [y, m, d] = dia(iso).split('-').map(Number)
  if (!y) return ''
  return `${DIAS[new Date(y, m - 1, d).getDay()]}, ${d} de ${MESES[m - 1]}${y !== new Date().getFullYear() ? ` de ${y}` : ''}`
}
function hora12(iso) {
  const hh = Number((iso || '').slice(11, 13)); const mm = (iso || '').slice(14, 16)
  if (Number.isNaN(hh) || !mm) return ''
  return `${hh % 12 || 12}:${mm} ${hh < 12 ? 'a. m.' : 'p. m.'}`
}
const telefono = (t) => (t && t.length === 12 && t.startsWith('57') ? `+57 ${t.slice(2, 5)} ${t.slice(5)}` : t ? `+${t}` : '')

// *negrita* y _cursiva_ como las pinta WhatsApp
function Formato({ texto }) {
  const partes = (texto || '').replace(/\r/g, '').split(/(\*[^*\n]+\*|_[^_\n]+_)/g)
  return partes.map((p, i) => (
    /^\*[^*\n]+\*$/.test(p) ? <b key={i}>{p.slice(1, -1)}</b>
      : /^_[^_\n]+_$/.test(p) ? <i key={i}>{p.slice(1, -1)}</i>
        : <span key={i}>{p}</span>
  ))
}

// Media: la foto y el audio se cargan solos (pesan poco); el video espera al clic.
function Media({ m, apiBase, rutaMedia }) {
  const [url, setUrl] = useState(null)
  const [estado, setEstado] = useState('nada')
  const cargar = async () => {
    setEstado('cargando')
    try {
      const r = await api.get(rutaMedia(m.id), { baseURL: apiBase, responseType: 'blob' })
      setUrl(URL.createObjectURL(r.data)); setEstado('listo')
    } catch { setEstado('error') }
  }
  useEffect(() => {
    if (m.media && m.id && (m.tipo === 'imagen' || m.tipo === 'audio')) cargar()
  }, [m.id]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!m.media) {
    return m.tipo !== 'texto'
      ? <p className="text-[11px] italic text-gray-500 mb-0.5">({m.tipo}: el archivo ya no está guardado)</p>
      : null
  }
  if (estado === 'error') return <p className="text-[11px] text-red-600 mb-0.5">No se pudo cargar el {m.tipo}</p>
  if (m.tipo === 'imagen') {
    return url
      ? <a href={url} target="_blank" rel="noreferrer"><img src={url} alt="imagen del chat" className="rounded-md max-h-64 mb-0.5" /></a>
      : <div className="w-48 h-32 rounded-md bg-black/5 flex items-center justify-center mb-0.5"><Loader2 size={16} className="animate-spin text-gray-400" /></div>
  }
  if (m.tipo === 'audio') {
    return url
      ? <audio src={url} controls className="h-9 max-w-[16rem] mb-0.5" />
      : <div className="h-9 w-56 rounded bg-black/5 flex items-center px-2 mb-0.5"><Loader2 size={14} className="animate-spin text-gray-400" /></div>
  }
  if (m.tipo === 'video') {
    return url
      ? <video src={url} controls autoPlay className="rounded-md max-h-72 bg-black mb-0.5" />
      : (
        <button onClick={cargar} disabled={estado === 'cargando'}
          className="w-56 h-32 rounded-md bg-black/80 text-white flex items-center justify-center gap-1.5 text-xs mb-0.5 disabled:opacity-60">
          {estado === 'cargando' ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />} Ver video
        </button>
      )
  }
  return (
    <button onClick={cargar} className="flex items-center gap-1.5 text-xs text-[#027eb5] mb-0.5">
      <FileText size={14} /> {url ? <a href={url} target="_blank" rel="noreferrer">Abrir documento</a> : 'Cargar documento'}
    </button>
  )
}

// El mensaje citado con «responder», dentro de la burbuja, como en WhatsApp
function Cita({ cita, persona }) {
  const yo = cita.de_mi === true
  return (
    <div className={`rounded-md mb-1 px-2 py-1 border-l-4 text-[12px] bg-black/5 ${yo ? 'border-[#06cf9c]' : 'border-[#7f66ff]'}`}>
      <p className={`font-semibold text-[11px] ${yo ? 'text-[#06cf9c]' : 'text-[#7f66ff]'}`}>{yo ? 'Tú' : (cita.autor || persona || '')}</p>
      <p className="text-gray-600 line-clamp-3 whitespace-pre-wrap">{(cita.texto || '').replace(/\r/g, '')}</p>
    </div>
  )
}

function Burbuja({ m, cola, persona, apiBase, rutaMedia }) {
  const yo = m.de_mi
  const fondo = yo ? '#d9fdd3' : '#ffffff'
  return (
    <div className={`flex ${yo ? 'justify-end' : 'justify-start'} ${cola ? 'mt-2' : 'mt-0.5'}`}>
      <div className={`relative max-w-[80%] rounded-lg px-2 pt-1 pb-1.5 shadow-sm text-[13px] leading-snug text-[#111b21]
        ${cola ? (yo ? 'rounded-tr-none' : 'rounded-tl-none') : ''} ${m.otro_caso ? 'opacity-60' : ''}`}
        style={{ background: fondo }}>
        {cola && (
          <span aria-hidden className="absolute top-0" style={yo
            ? { right: -7, borderTop: `8px solid ${fondo}`, borderRight: '8px solid transparent' }
            : { left: -7, borderTop: `8px solid ${fondo}`, borderLeft: '8px solid transparent' }} />
        )}
        {m.otro_caso && <p className="text-[10px] italic text-gray-500 mb-0.5">de otro caso · {m.otro_caso}</p>}
        {m.cita && <Cita cita={m.cita} persona={persona} />}
        {m.tipo !== 'texto' && <Media m={m} apiBase={apiBase} rutaMedia={rutaMedia} />}
        {m.texto && <span className="whitespace-pre-wrap break-words"><Formato texto={m.texto} /></span>}
        <span className="float-right ml-3 mt-1.5 text-[10px] leading-none text-gray-500 whitespace-nowrap">
          {yo && m.via === 'bot' && <span title="Enviado por la plataforma (bot de WhatsApp)">plataforma · </span>}
          {hora12(m.hora)}
        </span>
        {m.lectura && (
          <details className="clear-both mt-1">
            <summary className="cursor-pointer text-[10px] text-violet-700 select-none">
              Lo que leyó la máquina de {m.tipo === 'audio' ? 'la nota de voz' : m.tipo === 'video' ? 'el audio del video' : m.tipo === 'imagen' ? 'la imagen' : 'el adjunto'} ▸
            </summary>
            <p className="mt-1 text-[11px] text-gray-600 whitespace-pre-wrap border-l-2 border-violet-200 pl-2">{m.lectura}</p>
          </details>
        )}
      </div>
    </div>
  )
}

export default function ChatWhatsApp({ chats, apiBase, rutaMedia = (id) => `/video-caso/${id}` }) {
  if (!(chats || []).length) return null
  return (
    <div className="space-y-3">
      {chats.map((c, ci) => (
        <div key={ci} className="rounded-lg overflow-hidden border border-gray-200">
          <div className="flex items-center gap-2 px-3 py-2 bg-[#f0f2f5] border-b border-gray-200">
            <span className="w-8 h-8 rounded-full bg-[#dfe5e7] text-[#54656f] flex items-center justify-center text-sm font-semibold">
              {(c.persona || '?').trim().charAt(0).toUpperCase()}
            </span>
            <div className="leading-tight">
              <p className="text-sm text-[#111b21]">{c.persona || telefono(c.telefono)}</p>
              {c.telefono && <p className="text-[11px] text-[#667781]">{telefono(c.telefono)}</p>}
            </div>
          </div>
          <div className="px-4 py-3 max-h-[34rem] overflow-y-auto" style={{ background: '#efeae2' }}>
            {c.mensajes.map((m, i) => {
              const ant = c.mensajes[i - 1]
              const nuevoDia = !ant || dia(ant.hora) !== dia(m.hora)
              const cola = nuevoDia || !ant || ant.de_mi !== m.de_mi
              return (
                <div key={m.id ?? `e${i}`}>
                  {nuevoDia && (
                    <div className="flex justify-center my-2">
                      <span className="px-3 py-1 rounded-lg bg-white text-[11px] text-[#54656f] shadow-sm">{etiquetaDia(m.hora)}</span>
                    </div>
                  )}
                  <Burbuja m={m} cola={cola} persona={c.persona} apiBase={apiBase} rutaMedia={rutaMedia} />
                </div>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}

// Para hilos sin chat guardado (consultas pegadas a mano antes del bot): los turnos del hilo con
// la misma forma, para pintarlos igual.
export function chatDeTurnos(mensajes, persona) {
  const ms = (mensajes || []).filter((m) => m.direccion === 'enviado' || m.direccion === 'recibido')
    .map((m, i) => ({
      id: null, key: i, de_mi: m.direccion === 'enviado', hora: m.cuando_wa || m.cuando, texto: m.texto,
      tipo: 'texto', media: false, via: m.via === 'bot' ? 'bot' : m.via ? 'a mano' : null, cita: null, otro_caso: null,
    }))
  return ms.length ? [{ persona, telefono: null, mensajes: ms }] : []
}
