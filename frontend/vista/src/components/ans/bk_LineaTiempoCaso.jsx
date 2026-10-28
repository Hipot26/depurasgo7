import { useState, useEffect, useCallback } from 'react'
import { ListChecks, Video, AlertTriangle, Loader2, X, Play, ShieldAlert, ShieldCheck, Camera } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../../utils/api'

// Lo que JUSTIFICA el desfase y lo que SE ADJUNTA (requisito 2026-10-14): quien expone lo hace
// deduciendo sobre la marcha; este bloque, justo encima de la observación, le deja comparar en
// segundos la descripción con las pruebas: por gap con desfase, el tramo que lo cubre, los hechos
// de ese tramo con su fuente, y los videos grabados dentro de él (los que de verdad se adjuntan).
// Los videos del caso que caen fuera de los tramos no justifican nada: quedan fuera del paquete
// para ENEL y de la frase «SE ANEXAN». Lo calcula el backend (wa_analista.resumen_tiempos) — aquí
// solo se pinta, con la misma lógica que el 00_RESUMEN del paquete.
// Cada evidencia es clickeable y abre el video en un popup (mismo canal con token que el resto del
// tab: /wa/media/{msg_id}) para comprobar lo que dice sin salir del bloque (requisito 2026-10-14).
// CAPTURA AUTOMÁTICA (requisito 2026-10-14: automática e integrada al texto):
// si el tramo solo tiene mensajes, el backend dice qué mensajes irían en la captura (rt.captura); la
// línea del tramo la muestra como evidencia (clic = verla, pintada en memoria) y al aplicar el caso
// se adjunta sola. Nada de botones.
const lapso = (v) => (v.hora ? (v.hora_fin && v.hora_fin !== v.hora ? `${v.hora}–${v.hora_fin}` : v.hora) : 'sin hora')
const corto = (s, n) => ((s || '').length > n ? `${s.slice(0, n)}…` : (s || ''))
const msgDe = (fuente) => { const m = /msg[_ ]?id[= ]?(\d+)/i.exec(fuente || ''); return m ? +m[1] : null }

function Popup({ v, apiBase, analisisId, onCerrar }) {
  const [url, setUrl] = useState(null)
  const [error, setError] = useState(null)
  const esCaptura = v.tipo === 'captura'
  useEffect(() => {
    let vivo = true, obj = null
    // la ruta de la captura no termina en .png: la regla de estáticos de nginx se tragaba /api/…/captura-chat.png
    // video del análisis → /wa/media/{msg_id}; video adjunto a la celda (chat directo o subido a mano) → /evidencia/{id}/descargar
    const ruta = esCaptura ? `/wa/analisis/${analisisId}/captura-chat/imagen`
      : v.msg_id ? `/wa/media/${v.msg_id}` : `/evidencia/${v.evidencia_id}/descargar`
    api.get(ruta, { baseURL: apiBase, responseType: 'blob' })
      .then((r) => { if (!vivo) return; obj = URL.createObjectURL(r.data); setUrl(obj) })
      .catch(() => { if (vivo) { setError(esCaptura ? 'No se pudo armar la captura' : 'No se pudo cargar el video'); toast.error('No se pudo cargar') } })
    return () => { vivo = false; if (obj) URL.revokeObjectURL(obj) }
  }, [v.msg_id, v.tipo, apiBase, analisisId, esCaptura])
  return (
    <div className="fixed inset-0 z-[80] bg-black/70 flex items-center justify-center p-4" onClick={onCerrar}>
      <div className="bg-white rounded-xl shadow-2xl max-w-3xl w-full max-h-[92vh] flex flex-col overflow-hidden" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 px-4 py-2.5 border-b">
          {esCaptura ? <Camera size={15} className="text-emerald-700 shrink-0" /> : <Video size={15} className="text-emerald-700 shrink-0" />}
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-gray-800 truncate">
              {esCaptura
                ? `Captura del chat · ${v.mensajes} mensaje${v.mensajes === 1 ? '' : 's'}${v.desde ? ` · ${v.desde}–${v.hasta}` : ''}${v.evidencia_id ? ' · ya adjunta a la celda' : ' · se adjunta sola al aplicar'}`
                : `Video ${lapso(v)}${v.justifica === false ? ' · no justifica el desfase' : v.gap ? ` · justifica el ${v.gap}` : ''}`}
            </p>
            {!esCaptura && v.que_se_ve && <p className="text-xs text-gray-500 truncate" title={v.que_se_ve}>{v.que_se_ve}</p>}
            {esCaptura && <p className="text-xs text-gray-500">Evidencia parcial: no sustituye el correo del Centro de Control.</p>}
          </div>
          {!esCaptura && <span className="text-[10px] font-mono text-gray-400 shrink-0">{v.msg_id ? `msg ${v.msg_id}` : `evidencia ${v.evidencia_id}`}</span>}
          <button onClick={onCerrar} className="text-gray-400 hover:text-gray-700 shrink-0" title="Cerrar (Esc)"><X size={16} /></button>
        </div>
        <div className={`${esCaptura ? 'bg-gray-100 overflow-auto' : 'bg-black'} flex items-start justify-center min-h-[200px] max-h-[78vh]`}>
          {error ? (
            <p className="text-red-500 text-sm p-10">{error}</p>
          ) : !url ? (
            <p className="text-gray-400 text-sm p-10 flex items-center gap-2"><Loader2 size={16} className="animate-spin" /> {esCaptura ? 'Armando la captura…' : 'Cargando…'}</p>
          ) : esCaptura ? (
            <img src={url} alt="Captura del chat" className="w-full h-auto" />
          ) : (
            <video src={url} controls autoPlay className="max-h-[75vh] w-full"
              onError={() => setError('El navegador no puede reproducir este formato')} />
          )}
        </div>
      </div>
    </div>
  )
}

// EVIDENCIA NO SÓLIDA (requisito 2026-10-14): si el tramo se sostiene solo en mensajes de WhatsApp y la
// plantilla del líder, eso nos contextualiza a nosotros pero ENEL exige correos y pruebas puntuales. El
// backend (resumen_tiempos.solidez) lo califica: solida / parcial (pantallazo adjunto) / no_solida (solo
// mensajes — la captura del chat se adjunta sola, pero no cambia el aviso: sigue sin ser correo).
const SOLIDEZ = {
  no_solida: { box: 'border-red-300 bg-red-50 text-red-900', Icono: ShieldAlert, titulo: 'Evidencia NO sólida',
    texto: 'El tramo se sostiene solo en mensajes de WhatsApp y la plantilla del líder. Eso nos sirve para ubicarnos, pero ENEL exige correos y pruebas puntuales (video con letrero, correo del Centro de Control). La captura del chat se adjunta sola al aplicar; aun así, no excluyas sin el correo si existe.' },
  parcial: { box: 'border-amber-300 bg-amber-50 text-amber-900', Icono: ShieldAlert, titulo: 'Evidencia parcial',
    texto: 'Hay pantallazo adjunto a la celda, pero no un video dentro del tramo ni un correo. ENEL puede exigir el correo del Centro de Control: consíguelo si existe.' },
  solida: { box: 'border-emerald-200 bg-emerald-50 text-emerald-900', Icono: ShieldCheck, titulo: 'Evidencia sólida',
    texto: 'El tramo tiene video grabado dentro o un correo/PDF adjunto a la celda.' },
}

export default function LineaTiempoCaso({ rt, apiBase, analisisId }) {
  const [ver, setVer] = useState(null)   // video: {msg_id, …} · captura: {tipo:'captura', mensajes, desde, hasta, evidencia_id}
  const cerrar = useCallback(() => setVer(null), [])
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') cerrar() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cerrar])
  if (!rt || !(rt.gaps || []).length) return null
  const nSin = (rt.videos_sin_hora || []).length
  const nCtx = (rt.videos_contexto || []).length
  const cap = rt.captura && rt.captura.mensajes > 0 ? { ...rt.captura, tipo: 'captura' } : null
  const porMsg = {}
  // VIDEOS DE LA CELDA (14 oct 2026): los del chat o subidos a mano no tienen msg_id, sí evidencia_id
  const kv = (v) => (v.msg_id != null ? String(v.msg_id) : `e${v.evidencia_id}`)
  for (const v of [...(rt.videos_justifican || []), ...(rt.videos_sin_hora || [])]) porMsg[kv(v)] = v
  for (const v of (rt.videos_contexto || [])) porMsg[kv(v)] = { ...v, justifica: false }
  const abrirMsg = (msgId) => { const v = porMsg[msgId]; if (v) setVer(v) }

  const BotonVideo = ({ v, extra }) => (
    <button onClick={() => setVer(v)} title="Abrir el video en un popup para comprobarlo"
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-left hover:bg-emerald-50 ${extra || 'border-emerald-300 text-emerald-900 bg-white'}`}>
      <Play size={10} className="shrink-0" />
      <span>video {lapso(v)}{v.que_se_ve ? ` · ${corto(v.que_se_ve, 60)}` : ''}</span>
    </button>
  )
  const BotonCaptura = () => cap && (
    <button onClick={() => setVer(cap)} title="Ver la captura del chat que se adjunta (se arma con los mensajes del tramo)"
      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-sky-300 text-sky-900 bg-white text-left hover:bg-sky-50">
      <Camera size={10} className="shrink-0" />
      <span>captura del chat ({cap.mensajes} mensaje{cap.mensajes === 1 ? '' : 's'}{cap.desde ? `, ${cap.desde}–${cap.hasta}` : ''}){cap.evidencia_id ? ' · adjunta' : ' · se adjunta al aplicar'}</span>
    </button>
  )

  return (
    <div className="bg-white border-2 border-emerald-200 rounded-xl p-4 space-y-3">
      <h4 className="text-sm font-semibold flex items-center gap-1.5 flex-wrap">
        <ListChecks size={15} className="text-emerald-700" /> Lo que justifica el desfase — y lo que se adjunta
        <span className="text-xs font-normal text-gray-500">(para comparar la observación con las pruebas de un vistazo · clic en una evidencia = verla)</span>
      </h4>

      {rt.solidez && SOLIDEZ[rt.solidez] && (() => {
        // COBERTURA INCOMPLETA (requisito 2026-10-14): solidez (calidad de la prueba) ≠ cobertura (si tapa el
        // desfase). Con un gap sin cubrir, «sólida» no puede salir en verde: ámbar, «sólida pero incompleta».
        const sinCubrir = rt.gaps_sin_cubrir || []
        const incompleta = sinCubrir.length > 0
        const S = (rt.solidez === 'solida' && incompleta)
          ? { ...SOLIDEZ.solida, box: 'border-amber-300 bg-amber-50 text-amber-900', Icono: ShieldAlert, titulo: 'Evidencia sólida, pero incompleta' }
          : SOLIDEZ[rt.solidez]
        const docs = rt.docs || {}
        const adj = [docs.correo ? `${docs.correo} correo${docs.correo > 1 ? 's' : ''}` : null, docs.pdf ? `${docs.pdf} PDF` : null,
          docs.imagen ? `${docs.imagen} pantallazo${docs.imagen > 1 ? 's' : ''}` : null, docs.captura ? `${docs.captura} captura${docs.captura > 1 ? 's' : ''} del chat` : null].filter(Boolean)
        return (
          <div className={`border rounded-lg p-3 flex items-start gap-2 ${S.box}`}>
            <S.Icono size={18} className="shrink-0 mt-0.5" />
            <div className="flex-1 space-y-1">
              <p className="text-sm font-semibold">{S.titulo}</p>
              <p className="text-xs leading-relaxed">{S.texto}</p>
              {incompleta && (
                <p className="text-xs font-semibold">
                  Sin cubrir: {sinCubrir.map((g) => `${g.gap} (faltan ${g.faltan} min${g.sin_tramo ? ', sin ningún tramo' : ''})`).join(' · ')}. Con eso no se puede excluir todavía.
                </p>
              )}
              {adj.length > 0 && <p className="text-[11px] opacity-80">Adjunto a la celda: {adj.join(' · ')}.</p>}
            </div>
          </div>
        )
      })()}

      {rt.gaps.map((g) => (
        <div key={g.gap} className="space-y-1.5">
          <p className="text-sm">
            <b className="capitalize">{g.gap}</b> · planeado <b>{g.plan || '?'}</b> → real <b>{g.real || '?'}</b> · desfase <b>{g.requerido} min</b>
            {' '}· justificado <b>{g.justificado} min</b>
            {g.cubierto
              ? <span className="ml-2 px-2 py-0.5 rounded-full text-xs font-bold bg-emerald-100 text-emerald-800">cubre ✓</span>
              : <span className="ml-2 px-2 py-0.5 rounded-full text-xs font-bold bg-red-100 text-red-700">no cubre — faltan {g.faltan} min</span>}
          </p>
          {/* LÓGICA Y CUENTA HECHA (requisito 2026-10-14): quien expone bajo presión ve horas, no motivos; la cuenta va servida */}
          {g.cuenta && <p className="text-xs text-gray-700 pl-3"><b>Cuenta:</b> {g.cuenta}.</p>}
          {!(g.tramos || []).length && (
            <p className="text-xs text-gray-400 pl-3">
              {g.cubierto
                ? `Desfase dentro de la tolerancia de ${rt.tolerancia_min || 10} min: no necesita tramo.`
                : 'Sin tramo justificado: nada de lo recolectado explica este desfase.'}
            </p>
          )}
          {(g.tramos || []).map((t, i) => (
            <div key={i} className="ml-3 border-l-4 border-emerald-400 pl-3 py-1 space-y-1">
              <p className="text-sm">
                <span className="font-mono font-semibold">{t.desde} → {t.hasta}</span> · <b>{t.minutos} min</b>
                <span className="text-gray-600"> · {t.evidencia}</span>
              </p>
              {t.logica && <p className="text-xs text-emerald-900 bg-emerald-50 border border-emerald-200 rounded px-2 py-1"><b>Por qué prueba el desfase:</b> {t.logica}</p>}
              <ul className="space-y-0.5">
                {(t.hechos || []).slice(0, 6).map((e, j) => {
                  const mid = msgDe(e.fuente)
                  return (
                    <li key={j} className="text-xs flex gap-2">
                      <span className="font-mono text-gray-500 w-11 shrink-0">{e.hora}</span>
                      <span className="flex-1">
                        {e.hecho}
                        <span className="text-gray-400"> — </span>
                        {mid && porMsg[mid]
                          ? <button onClick={() => abrirMsg(mid)} className="text-emerald-800 underline decoration-dotted hover:text-emerald-600" title="Ver este video">{e.fuente}</button>
                          : <span className="text-gray-400">{e.fuente}</span>}
                        <span className="text-gray-400">{e.grupo ? ` · ${e.grupo}` : ''}{e.dicho ? ' (dicho en el audio)' : ''}</span>
                      </span>
                    </li>
                  )
                })}
                {(t.hechos || []).length > 6 && (
                  <li className="text-xs text-gray-400">
                    <details>
                      <summary className="cursor-pointer">+{t.hechos.length - 6} hechos más dentro del tramo</summary>
                      <ul className="mt-1 space-y-0.5">
                        {t.hechos.slice(6).map((e, j) => (
                          <li key={j} className="flex gap-2"><span className="font-mono w-11 shrink-0">{e.hora}</span><span>{e.hecho}</span></li>
                        ))}
                      </ul>
                    </details>
                  </li>
                )}
                {!(t.hechos || []).length && !cap && (
                  <li className="text-xs text-gray-400">sin hechos de la línea de tiempo dentro de este tramo</li>
                )}
              </ul>
              <div className="text-xs flex items-start gap-1.5 flex-wrap">
                <Video size={12} className="text-emerald-700 shrink-0 mt-1" />
                {/* CAPTURA CUANDO EL TIEMPO SALE DE LA PLANTILLA (14 oct 2026): si el tramo cita plantilla o mensajes, la
                    captura va al paquete aunque haya videos — es lo que sostiene los minutos del tramo ante ENEL */}
                {(t.videos || []).length
                  ? <><b className="mt-0.5">Se adjunta{t.videos.length > 1 || (cap && t.soporte_chat) ? 'n' : ''}:</b>{[...t.videos].sort((x, y) => (x.hora || '').localeCompare(y.hora || '')).map((v) => <BotonVideo key={kv(v)} v={porMsg[kv(v)] || v} />)}
                      {cap && t.soporte_chat && <><BotonCaptura /><span className="text-gray-600 mt-0.5">— los minutos del tramo salen de la plantilla y los mensajes: van en la captura.</span></>}</>
                  : cap
                    ? <><b className="mt-0.5">Se adjunta:</b><BotonCaptura /><span className="text-amber-700 mt-0.5">— sin video dentro del tramo: el soporte son los mensajes y la plantilla del grupo.</span></>
                    : <span className="text-amber-700">Ningún video grabado dentro de este tramo ni mensajes del grupo que capturar: no hay archivo para el paquete.</span>}
              </div>
            </div>
          ))}
        </div>
      ))}

      <div className="text-xs text-gray-700 border-t pt-2 space-y-1">
        <p className="flex items-center gap-1.5 flex-wrap">
          <b>Al paquete para ENEL:</b> {rt.anexar} video{rt.anexar === 1 ? '' : 's'}
          {cap ? ' + la captura del chat' : ''}
          {nSin ? ` (${nSin} sin hora de letrero: se adjunta${nSin > 1 ? 'n' : ''} pero no se pudo ubicar en un tramo)` : ''}.
          {(rt.videos_sin_hora || []).map((v) => <BotonVideo key={kv(v)} v={v} extra="border-gray-300 text-gray-700 bg-white" />)}
        </p>
        {nCtx > 0 && (
          <div className="flex items-start gap-1.5 text-gray-500">
            <AlertTriangle size={12} className="shrink-0 mt-0.5 text-amber-600" />
            <div className="flex items-center gap-1.5 flex-wrap">
              <span><b>{nCtx} video{nCtx > 1 ? 's' : ''} del caso que NO justifica{nCtx > 1 ? 'n' : ''} el desfase</b> (quedan fuera del paquete y de la observación):</span>
              {rt.videos_contexto.map((v) => <BotonVideo key={kv(v)} v={porMsg[kv(v)] || v} extra="border-amber-300 text-amber-900 bg-white hover:bg-amber-50" />)}
            </div>
          </div>
        )}
        {(rt.hechos_contexto || []).length > 0 && (
          <details className="text-gray-400">
            <summary className="cursor-pointer">Otros hechos del día fuera de los tramos ({rt.hechos_contexto.length})</summary>
            <ul className="mt-1 space-y-0.5">
              {rt.hechos_contexto.map((e, j) => <li key={j}><span className="font-mono">{e.hora}</span> {e.hecho}</li>)}
            </ul>
          </details>
        )}
      </div>

      {ver && <Popup v={ver} apiBase={apiBase} analisisId={analisisId} onCerrar={cerrar} />}
    </div>
  )
}
