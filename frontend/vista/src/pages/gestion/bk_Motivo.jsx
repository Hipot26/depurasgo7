// Motivo.jsx — el motivo de un rechazo de Enel como lo escribió en Sonny (CHANGELOG 2026-10-25): estado y fecha, la categoría de cada punto (reglas fijas
// del backend, gestion_flujo.motivo_rechazo) y el texto completo, desescapado, con sus saltos de línea; «ver más» desde 280 caracteres. Si Sonny lo
// exportó cortado (255 caracteres), lo dice. Lo usan la ficha de la planilla en la cadena y la evidencia («Avisos»).
import { useState } from 'react'
import { fecha } from './ux'

const CORTE = 280      // la ficha de la cadena pasa corte={180}: el texto de Sonny llega hasta 255 caracteres

export function textoCorto(t, n = CORTE) {
  const s = String(t || '')
  if (s.length <= n) return s
  const i = s.lastIndexOf(' ', n)
  return s.slice(0, i > n * 0.6 ? i : n).trimEnd() + '…'
}

export default function Motivo({ m, planilla, corte = CORTE }) {
  const [todo, setTodo] = useState(false)
  if (!m) return null
  const texto = String(m.texto || '')
  const largo = texto.length > corte
  const cats = [...new Set((m.puntos || []).filter((p) => !p.cortado).map((p) => p.etiqueta))]
  return (
    <div className="motivo" data-motivo={planilla || ''}>
      <div className="mh">
        <b>Motivo del rechazo</b>
        <span className="muted">{m.estado_llano || 'rechazada por Enel'}{m.fecha ? ` el ${fecha(m.fecha)}` : ''}</span>
      </div>
      {m.frase_corta && <div className="mq">Qué hay que hacer: <b>{m.frase_corta}</b>.</div>}
      {cats.length > 0 && <div className="mc">{cats.map((c) => <span key={c} className="etq">{c}</span>)}{(m.n_puntos || 0) > 1 ? <span className="muted"> · {m.n_puntos} puntos</span> : null}</div>}
      {texto
        ? <div className="mt" data-motivo-texto>{todo || !largo ? texto : textoCorto(texto, corte)}</div>
        : <div className="mt muted">Sonny no trae el texto de este rechazo.</div>}
      {largo && <button type="button" className="link" data-motivo-mas onClick={() => setTodo(!todo)}>{todo ? 'ver menos' : 'ver más'}</button>}
      {m.cortado && <div className="nota" data-motivo-cortado>Sonny exporta este texto cortado a 255 caracteres: el resto está en el botón «i» de la planilla en Sonny.</div>}
    </div>
  )
}
