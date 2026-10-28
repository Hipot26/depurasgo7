// FiguraDinero.jsx — la figura de la zona (CHANGELOG 2026-10-25): la mano de obra por etapa como UNA figura, seis barras en una escala, en el orden del flujo.
// Los anchos y las cifras vienen del backend (/api/gestion/zona: etapas[].barra, valor, n); aquí solo se dibujan. «Ver como tabla» para quien no
// lee gráficos. «Pagado» es una barra punteada mientras no haya pago confirmado ((CHANGELOG 2026-10-25) «fuente por definir»); sus textos vienen de la API.
import { useState } from 'react'
import { mm, pesos, plural } from './ux'

export function Tip({ tip }) {
  if (!tip) return null
  return <div className="gux-tip" role="tooltip" style={{ left: Math.min(tip.x + 14, window.innerWidth - 320), top: tip.y + 16 }}>{tip.t}</div>
}

function textoTip(e) {
  if (e.k === 'pagado') return e.por_confirmar ? `Pagado: ${e.nota || 'por confirmar'}` : `Pagado: ${pesos(e.valor)} en ${plural(e.n, 'trabajo', 'trabajos')}.`
  let t = `${e.t} (${e.sub}): ${pesos(e.valor)} de mano de obra en ${plural(e.n, 'trabajo', 'trabajos')}. ${e.nota || ''}`
  if (e.k === 'facturado' && e.vencido !== undefined) t += ` De eso, ${pesos(e.vencido)} en ${plural(e.n_vencido, 'LCL', 'LCL')} con alguna factura vencida. Además, enviado a Enel sin registrar: ${pesos(e.enviado)} en ${plural(e.n_enviado, 'trabajo', 'trabajos')}.`
  if (e.k === 'facturado' && e.conciliacion !== undefined) t += ` Además, en conciliación según el mismo informe: ${pesos(e.conciliacion)} en ${plural(e.n_conciliacion, 'trabajo', 'trabajos')}.`
  return t
}

export default function FiguraDinero({ etapas = [], comoSeCuenta }) {
  const [tip, setTip] = useState(null)
  const [tabla, setTabla] = useState(false)
  const F = etapas.find((e) => e.k === 'facturado') || {}
  const mover = (e, t) => setTip({ x: e.clientX, y: e.clientY, t })
  return (
    <section className="panel" aria-labelledby="t-embudo" data-figura>
      <header>
        <h2 id="t-embudo">Mano de obra por etapa</h2><span className="muted" style={{ fontSize: '.85rem' }}>cada barra: lo que ya llegó a esa etapa</span>
        <span className="der"><button className="link" type="button" onClick={() => setTabla(!tabla)} data-ver-tabla>{tabla ? 'Ocultar tabla' : 'Ver como tabla'}</button></span>
      </header>
      <div className="embudo">
        {etapas.map((e) => {
          const vacia = e.k === 'pagado' && (e.por_confirmar || !e.valor)
          const t = textoTip(e)
          return (
            <div key={e.k} className={`fila-e${e.k === 'facturado' && e.enviado ? ' tiene-env' : ''}`} data-etapa={e.k} data-valor={e.valor}>
              <div className="et"><b>{e.t}</b><small>{e.sub}{e.gris ? ' · referencia' : ''}</small></div>
              <div className="pista" tabIndex={0} role="img" aria-label={`${e.t}: ${mm(e.valor)}`}
                onMouseEnter={(ev) => mover(ev, t)} onMouseMove={(ev) => mover(ev, t)} onMouseLeave={() => setTip(null)}
                onFocus={(ev) => { const r = ev.currentTarget.getBoundingClientRect(); setTip({ x: r.left, y: r.bottom - 8, t }) }} onBlur={() => setTip(null)}>
                {vacia ? <div className="seg vacia" /> : (
                  <>
                    <div className={`seg ${e.gris ? 'gris' : 'base'}`} style={{ left: 0, width: `${e.barra || 0}%` }} />
                    {e.k === 'facturado' && e.barra_vencido > 0 && <div className="seg venc" style={{ left: 0, width: `${e.barra_vencido}%` }} />}
                    {e.k === 'facturado' && e.barra_enviado > 0 && <div className="seg env" style={{ left: `${e.barra || 0}%`, width: `${e.barra_enviado}%` }} />}
                  </>)}
              </div>
              <div className="val num">
                <b>{vacia && e.por_confirmar ? (e.texto === 'fuente por definir' ? 'por definir' : 'por confirmar') : mm(e.valor)}</b>
                <small>{vacia && e.por_confirmar ? (e.texto || 'por confirmar') : plural(e.n, 'trabajo', 'trabajos')}{e.k === 'facturado' && e.vencido !== undefined ? ` · ${mm(e.vencido)} vencido` : ''}</small>
              </div>
            </div>)
        })}
      </div>
      <div className="leyenda">
        <span><i className="sw" style={{ background: 'var(--bar)' }} />mano de obra en la etapa</span>
        <span><i className="sw" style={{ background: 'var(--idle)' }} />referencia (SGS)</span>
        {F.vencido !== undefined && <span><i className="sw" style={{ background: 'var(--risk)' }} />en LCL con factura vencida</span>}
        {F.enviado !== undefined && <span><i className="sw" style={{ background: 'var(--wait)', height: 5 }} />enviado a Enel, sin registrar</span>}
        <span><i className="sw" style={{ border: '1.5px dashed var(--line-2)' }} />sin dato todavía</span>
      </div>
      {tabla && (
        <div className="scroll-x" data-tabla-etapas>
          <table className="tabla-alt num">
            <thead><tr><th>Etapa</th><th className="c-sis">Sistema</th><th className="r">Trabajos</th><th className="r">Mano de obra</th></tr></thead>
            <tbody>
              {etapas.map((e) => <tr key={e.k}><td>{e.t}</td><td className="c-sis">{e.sub}</td><td className="r">{plural(e.n, '', '').trim()}</td><td className="r">{e.k === 'pagado' && e.por_confirmar ? (e.texto === 'fuente por definir' ? 'por definir' : 'por confirmar') : pesos(e.valor)}</td></tr>)}
              {F.vencido !== undefined && <tr><td>· en LCL con factura vencida</td><td className="c-sis">{F.sub}</td><td className="r">{plural(F.n_vencido || 0, '', '').trim()}</td><td className="r">{pesos(F.vencido)}</td></tr>}
              {F.enviado !== undefined && <tr><td>· enviado a Enel, sin registrar</td><td className="c-sis">{F.sub}</td><td className="r">{plural(F.n_enviado || 0, '', '').trim()}</td><td className="r">{pesos(F.enviado)}</td></tr>}
              {F.conciliacion !== undefined && <tr><td>· en conciliación según el informe</td><td className="c-sis">{F.sub}</td><td className="r">{plural(F.n_conciliacion || 0, '', '').trim()}</td><td className="r">{pesos(F.conciliacion)}</td></tr>}
            </tbody>
          </table>
        </div>)}
      <details className="aud"><summary>Cómo se cuenta</summary><p className="nota">{comoSeCuenta}</p></details>
      <Tip tip={tip} />
    </section>
  )
}
