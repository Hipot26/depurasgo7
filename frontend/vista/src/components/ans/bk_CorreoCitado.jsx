// El correo con las CITAS de la IA en amarillo — el principio del panel de proyectos
// (la afirmación amarrada al texto original de un vistazo) aplicado a la justificación:
// no basta adjuntar el correo, hay que ver QUÉ frases llevaron a la conclusión.
// Lo comparten el tab Correos ANS y «Ver justificación» de la grilla.
//
// Requisito 2026-09-11: resaltar en amarillo las partes del correo que contienen el PDL.
// Lo primero que se pinta son las líneas del correo que NOMBRAN el PDL
// del caso (con el código en negrilla) y el visor salta a la primera; la aproximación por
// palabras de las lecturas viejas queda solo cuando el correo no nombra el PDL — pintaba la
// frase de OTRO PDL (R07816) porque se parecía al razonamiento.
import { useEffect, useRef } from 'react'
import { Mail, Highlighter } from 'lucide-react'

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

// Los códigos del caso que valen como «su PDL»: el PDL y las piezas del protocolo
// («CDOPP2026R08064/CDOPP2026A07332»), sin repetidos.
export function codigosCaso(...fuentes) {
  const out = new Set()
  for (const f of fuentes) {
    for (const p of String(f || '').split(/[\/,;\s]+/)) {
      if (/^CDOP[A-Z][A-Z0-9]{6,14}$/i.test(p)) out.add(p.toUpperCase())
    }
  }
  return [...out]
}

// Líneas del cuerpo que nombran alguno de los códigos (cada una una vez).
export function fragmentosPdl(cuerpo, codigos) {
  if (!cuerpo || !codigos?.length) return []
  const rex = new RegExp(codigos.map(esc).join('|'), 'i')
  const vistos = new Set()
  return cuerpo.split('\n').map((l) => l.trim())
    .filter((l) => l.length >= 8 && rex.test(l) && !vistos.has(l) && vistos.add(l))
}

// Dentro de un fragmento resaltado, el código del PDL va en negrilla.
function negritaCodigos(texto, codigos) {
  if (!codigos?.length) return texto
  const rex = new RegExp(`(${codigos.map(esc).join('|')})`, 'ig')
  return texto.split(rex).map((t, i) => (i % 2 ? <b key={i} className="font-bold">{t}</b> : t))
}

// Resalta en amarillo los fragmentos citados. Tolerante a diferencias de espacios/saltos de
// línea. `codigos` = PDL del caso (se ponen en negrilla dentro del resaltado).
export function resaltarCitas(texto, citas, codigos = []) {
  if (!texto || !citas?.length) return texto
  let partes = [texto]
  let k = 0
  for (const cita of citas) {
    const limpia = (cita || '').trim()
    if (limpia.length < 8) continue
    const rex = new RegExp(esc(limpia).replace(/\s+/g, '\\s+'), 'i')
    partes = partes.flatMap((p) => {
      if (typeof p !== 'string') return [p]
      const m = p.match(rex)
      if (!m) return [p]
      const i = m.index
      return [p.slice(0, i),
        <mark key={`m${k++}`} data-cita="1" className="bg-yellow-200 rounded px-0.5">{negritaCodigos(m[0], codigos)}</mark>,
        p.slice(i + m[0].length)]
    })
  }
  return partes
}

// ¿Cuántas de las citas aparecen de verdad en este cuerpo? (para saber si el correo mostrado
// es el que la IA leyó, o uno distinto del mismo caso).
export function citasEncontradas(texto, citas) {
  if (!texto || !citas?.length) return 0
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase()
  const t = norm(texto)
  return citas.filter((c) => norm(c).length >= 8 && t.includes(norm(c))).length
}

// Lecturas ANTERIORES a la regla de citas (2026-08-18) no dejaron frases literales. Para no
// dejar el correo «en blanco», se aproximan: frases del cuerpo que comparten palabras
// significativas con el razonamiento/observación. Se avisa que es aproximación.
const STOP = new Set(['para', 'como', 'pero', 'este', 'esta', 'esto', 'estos', 'estas', 'desde', 'hasta', 'sobre',
  'entre', 'porque', 'cuando', 'donde', 'tiene', 'tienen', 'hacia', 'también', 'favor', 'buenos', 'buenas',
  'tardes', 'días', 'dias', 'saludo', 'cordial', 'gracias', 'atentamente', 'correo', 'orden', 'trabajo',
  'solicito', 'solicita', 'señor', 'señores', 'ingeniero', 'quedo', 'atento', 'respuesta', 'mismo', 'misma',
  'puede', 'podría', 'según', 'segun', 'fecha', 'sería', 'siendo', 'hemos', 'habían', 'están', 'estan'])
const palabrasDe = (s) => (s || '').toLowerCase().split(/[^a-záéíóúüñ0-9]+/).filter((w) => w.length >= 5 && !STOP.has(w))
export function citasAproximadas(cuerpo, pistas) {
  if (!cuerpo || !pistas?.length) return []
  const claves = new Set(pistas.flatMap(palabrasDe))
  if (claves.size < 3) return []
  const frases = cuerpo.split(/(?<=[.;!?])\s+|\n+/).map((s) => s.trim())
    .filter((s) => s.length >= 25 && s.length <= 400 && !/^(de|para|cc|enviado|asunto|from|to|sent|subject)\s*:/i.test(s))
  return frases
    .map((f) => ({ f, n: new Set(palabrasDe(f)).size ? [...new Set(palabrasDe(f))].filter((w) => claves.has(w)).length : 0 }))
    .filter((x) => x.n >= 3)
    .sort((a, b) => b.n - a.n).slice(0, 6).map((x) => x.f)
}

// Un correo (asunto/de/fecha + cuerpo) con lo clave pintado. `descarga` = URL del .msg.
// `pdl` = códigos del caso (codigosCaso): sus líneas van primero en amarillo.
// `pistas` (razonamiento/observación) solo se usa si la lectura no dejó citas literales Y el
// correo no nombra el PDL.
export default function CorreoCitado({ correo, citas = [], pistas = [], pdl = [], descarga, titulo = 'El correo' }) {
  const preRef = useRef(null)
  const cuerpo = correo?.cuerpo || ''
  const delPdl = fragmentosPdl(cuerpo, pdl)
  const literales = citas.length > 0
  const aprox = literales || delPdl.length ? [] : citasAproximadas(cuerpo, pistas)
  const pintar = [...delPdl, ...(literales ? citas : aprox)]
  const nLit = citasEncontradas(cuerpo, citas)

  // Salta a la primera línea que nombra el PDL (en los reenvíos suele estar muy abajo).
  useEffect(() => {
    const pre = preRef.current
    const mk = pre?.querySelector('mark[data-cita]')
    if (pre && mk && delPdl.length) pre.scrollTop = Math.max(0, mk.offsetTop - 24)
  }, [cuerpo, delPdl.length])

  if (!correo) return null
  return (
    <div className="border rounded-xl bg-white">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 px-3 py-2 border-b bg-gray-50 rounded-t-xl text-xs">
        <Mail size={13} className="text-gray-500" />
        <b className="text-gray-800">{titulo}</b>
        <span className="text-gray-600 truncate">{correo.asunto || '(sin asunto)'}</span>
        {descarga && <a href={descarga} target="_blank" rel="noreferrer" className="text-sky-700 underline">descargar .msg</a>}
        <span className="w-full text-gray-500">
          <b>De:</b> {correo.de || correo.remitente || '—'} · <b>Fecha:</b> {correo.fecha || correo.fecha_correo || '—'}
          {correo.imagenes ? ` · ${correo.imagenes} imagen(es) adjunta(s), leídas por la IA` : ''}
        </span>
        {delPdl.length > 0 && (
          <span className="w-full flex items-center gap-1 text-amber-800">
            <Highlighter size={12} />
            en amarillo, {delPdl.length} parte(s) del correo que nombran el PDL {pdl.join(' / ')}
            {literales && nLit ? ` + ${nLit} cita(s) de la lectura IA` : ''}
          </span>
        )}
        {!delPdl.length && pintar.length > 0 && (
          <span className={`w-full flex items-center gap-1 ${literales && !nLit ? 'text-gray-400' : 'text-amber-800'}`}>
            <Highlighter size={12} />
            {!literales
              ? `en amarillo, ${citasEncontradas(cuerpo, aprox)} frase(s) del correo que coinciden con el razonamiento — aproximación: el correo no nombra el PDL del caso y esta lectura es anterior a la regla de citas literales`
              : nLit
                ? `en amarillo, las ${nLit} frase(s) del correo que sustentan la conclusión de la máquina`
                : 'las citas de la lectura IA no están en este correo (es otro correo del caso)'}
          </span>
        )}
        {pdl.length > 0 && !delPdl.length && (
          <span className="w-full text-red-700">este correo no nombra el PDL {pdl.join(' / ')}</span>
        )}
      </div>
      <pre ref={preRef} className="relative px-3 py-2 text-xs text-gray-700 whitespace-pre-wrap font-sans max-h-80 overflow-y-auto">
        {resaltarCitas(cuerpo || '(vacío)', pintar, pdl)}
      </pre>
    </div>
  )
}
