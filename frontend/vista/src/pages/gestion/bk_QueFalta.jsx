// QueFalta.jsx — «Qué falta para cobrar» (CHANGELOG 2026-10-25): 1-3 frases, la más urgente primero, cada una con su punto de color; las demás, plegadas.
// Las frases vienen armadas del backend (gestion_flujo.que_falta: con pesos y fechas, sin el material de Enel).
import { Dot } from './Estado'
import { plural } from './ux'

export default function QueFalta({ frases = [] }) {
  const vis = frases.slice(0, 3), resto = frases.slice(3)
  return (
    <section className="panel" aria-labelledby="t-falta" data-que-falta>
      <header><h2 id="t-falta">Qué falta para cobrar</h2></header>
      <ul className="falta">{vis.map((x, i) => <li key={i} data-falta={x.tipo}><Dot color={x.color} /><span>{x.t}</span></li>)}</ul>
      {resto.length > 0 && (
        <details className="aud"><summary>{plural(resto.length, 'cosa más', 'cosas más')}</summary>
          <ul className="falta" style={{ marginTop: 10 }}>{resto.map((x, i) => <li key={i} data-falta={x.tipo}><Dot color={x.color} /><span>{x.t}</span></li>)}</ul>
        </details>)}
    </section>
  )
}
