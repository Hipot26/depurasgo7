// FlujoPage.jsx — /gestion/flujo: «¿Cómo fluye?» (CHANGELOG 2026-10-25). Cómo viaja un trabajo de Enel a Dominion y de vuelta hasta el pago: 11 pasos en
// dos carriles (lo pone Enel / lo pone Dominion), con el documento que nace en cada paso y su número real del trabajo de ejemplo
// (config.GESTION_CASO_EJEMPLO, leído de su cadena), y el glosario de 12 palabras. Un paso muestra su definición y marca su palabra.
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import api from '../../utils/api'
import './gestion-ux.css'
import { BarraVista } from './Estado'
import { arriba, fecha, glosario, mm, plural } from './ux'

function pasosDe(c) {
  const visible = c?.cobro_visible !== false
  const P = c?.cadena?.piezas || {}
  const lcl = (P.origen || []).find((x) => x.tipo === 'LCL') || (P.origen || [])[0]
  const pms = (P.pms || [])[0]
  const sgs = (P.nodos || []).find((x) => x.sgs && !x.gemelo)
  const pl = (P.planillas || [])[0]
  const h = pl?.hitos || {}
  const lib = (P.libretos || []).find((x) => !x.muerto)
  const fa = (P.facturas || [])[0]
  const pg = c?.cadena?.pago || {}
  const y = (...xs) => xs.filter(Boolean).join(' · ')
  return [
    { n: 1, t: 'LCL', quien: 'enel', sis: 'SCM', ej: lcl ? y(lcl.id, lcl.inicio ? `vigente ${fecha(lcl.inicio)} → ${fecha(lcl.fin)}` : '') : '', g: 'LCL' },
    { n: 2, t: 'Solicitud y presupuesto', quien: 'enel', sis: 'PMS', ej: pms ? y(pms.id, `MO ${mm(pms.mo)}`) : '', g: 'Solicitud PMS' },
    { n: 3, t: 'PDL (plan de trabajo)', quien: 'dominion', sis: 'correo y PMS', ej: (P.pdl || []).map((x) => x.id).slice(0, 4).join(', '), g: 'PDL' },
    { n: 4, t: 'Nodos', quien: 'dominion', sis: 'Sonny y SGS', ej: sgs ? y(sgs.n ? plural(sgs.n, 'nodo', 'nodos') : '', sgs.sgs) : '', g: 'Nodo' },
    { n: 5, t: 'Liquidación', quien: 'dominion', sis: 'SGS', ej: sgs ? y(sgs.ultima ? fecha(sgs.ultima) : '', sgs.liquidado ? `MO ${mm(sgs.liquidado)}` : '') : '', g: 'Liquidar' },
    { n: 6, t: 'Planilla', quien: 'dominion', sis: 'Sonny', ej: pl ? y(pl.id, h.firmada ? `firmada ${fecha(h.firmada)}` : h.recibo ? `enviada a Enel ${fecha(h.recibo)}` : '') : '', g: 'Planilla' },
    { n: 7, t: 'Recibo de obra y conciliación', quien: 'enel', sis: 'Sonny', ej: pl ? y(h.conciliada ? `conciliada ${fecha(h.conciliada)}` : '', h.aprobada ? `aprobada ${fecha(h.aprobada)}` : '') : '', g: 'Recibo de obra y conciliación' },
    { n: 8, t: 'Libreto', quien: 'dominion', sis: 'SCM', ej: lib ? y(lib.id, lib.fecha ? fecha(lib.fecha) : '', mm(lib.mo)) : '', g: 'Libreto' },
    { n: 9, t: 'HES', quien: 'enel', sis: 'SAP', ej: fa?.n_em ? `${plural(fa.n_em, 'HES', 'HES')} en la factura` : '', g: 'HES' },
    visible ? { n: 10, t: 'Factura', quien: 'dominion', sis: 'DRAPE', ej: fa ? y(fa.id, fa.registro ? `registrada ${fecha(fa.registro)}` : fa.estado, fa.importe !== null && fa.importe !== undefined ? `${mm(fa.importe)} de esta LCL` : '') : '', g: 'Factura' }
      : { n: 10, t: 'Factura', quien: 'dominion', sis: 'Facturación', ej: 'Facturación emite la factura a Enel', g: 'Factura' },
    visible ? { n: 11, t: 'Pago', quien: 'enel', sis: 'DRAPE', ej: fa ? y(fa.vence ? `vence ${fecha(fa.vence)}` : '', pg.estado === 'pagado' ? (pg.confirmado ? 'pagado' : 'pagado, por confirmar') : 'sin pago aún') : '', g: 'Pago' }
      : { n: 11, t: 'Pago', quien: 'enel', sis: 'Enel', ej: 'Enel la registra y la paga · fuente por definir', g: 'Pago' },
  ]
}

export default function FlujoPage() {
  const { data: z } = useQuery({ queryKey: ['gestion-zona'], queryFn: () => api.get('/gestion/zona').then((r) => r.data) })
  const ej = z?.ejemplo || {}
  const { data: c } = useQuery({ queryKey: ['gestion-cadena', String(ej.id)], queryFn: () => api.get(`/gestion/proyectos/${ej.id}/cadena`).then((r) => r.data), enabled: !!ej.id })
  const [on, setOn] = useState(null)
  const [ancho, setAncho] = useState(0)
  const svg = useRef(null)
  const PASOS = pasosDe(c)
  const visible = c?.cobro_visible !== false
  const GLOS = glosario(visible)
  const DEF = Object.fromEntries(GLOS)
  useEffect(() => { arriba() }, [])
  useEffect(() => {
    let t = null
    const f = () => { clearTimeout(t); t = setTimeout(() => setAncho(window.innerWidth), 150) }
    window.addEventListener('resize', f)
    return () => { window.removeEventListener('resize', f); clearTimeout(t) }
  }, [])
  // conectores: cada paso sale por el borde que mira al centro y entra al siguiente; cuando cambia de carril, la pelota cruza
  useLayoutEffect(() => {
    const s = svg.current
    if (!s) return
    const wrap = s.parentElement
    if (getComputedStyle(s).display === 'none') { s.innerHTML = ''; return }
    const base = wrap.getBoundingClientRect()
    let d = ''
    for (let i = 1; i < PASOS.length; i++) {
      const A = PASOS[i - 1], B = PASOS[i]
      const ea = wrap.querySelector(`[data-paso="${i}"]`), eb = wrap.querySelector(`[data-paso="${i + 1}"]`)
      if (!ea || !eb) continue
      const a = ea.getBoundingClientRect(), b = eb.getBoundingClientRect()
      const xa = (A.quien === 'enel' ? a.right : a.left) - base.left, ya = a.top + a.height / 2 - base.top
      const xb = (B.quien === 'enel' ? b.right : b.left) - base.left, yb = b.top + b.height / 2 - base.top
      const mx = A.quien === B.quien ? xa + (A.quien === 'enel' ? 26 : -26) : (xa + xb) / 2
      const sg = B.quien === 'enel' ? 1 : -1
      d += `<path d="M${xa} ${ya} C${mx} ${ya}, ${mx} ${yb}, ${xb} ${yb}" fill="none" stroke="var(--muted)" stroke-width="1.4" opacity=".6"/>`
        + `<path d="M${xb + sg * 7} ${yb - 4} L${xb} ${yb} L${xb + sg * 7} ${yb + 4}" fill="none" stroke="var(--muted)" stroke-width="1.4" opacity=".6"/>`
    }
    s.innerHTML = d
  })
  const paso = PASOS.find((p) => p.n === on)
  return (
    <div className="gux" data-ux="flujo" data-ancho={ancho}>
      <BarraVista actual="flujo" />
      <div className="gux-main">
        <div className="vista">
          <section className="intro"><div style={{ flex: '1 1 520px', minWidth: 0 }}>
            <div className="eyebrow">Cómo viaja un trabajo</div>
            <h1>De la LCL al pago: la pelota va y viene</h1>
            <p>Once pasos, {visible ? 'siete' : 'seis'} sistemas. A la izquierda lo que pone Enel; a la derecha, lo que pone Dominion. Cada paso deja un documento con su número;
              el ejemplo es el trabajo {ej.id ? <Link to={`/gestion/${ej.id}`} className="mono">{ej.codigo}</Link> : ej.codigo || '…'}. Toca un paso para ver qué es.</p>
          </div></section>
          <section className="panel">
            <div className="flujo-wrap">
              <svg aria-hidden="true" ref={svg} />
              <div className="flujo">
                <div className="carril enel">Lo pone Enel</div><div className="carril dom">Lo pone Dominion</div>
                {PASOS.map((p) => {
                  const en = p.quien === 'enel'
                  return (
                    <button key={p.n} type="button" className={`paso${on === p.n ? ' on' : ''}`} data-paso={p.n} data-g={p.g} data-carril={en ? 'Lo pone Enel' : 'Lo pone Dominion'} aria-expanded={on === p.n}
                      style={{ '--fila': p.n + 1, '--col': en ? 1 : 2, '--lado': en ? 'end' : 'start', '--lado-m': en ? 'start' : 'end' }} onClick={() => setOn(on === p.n ? null : p.n)}>
                      <span className="n">{p.n}</span><span className="tt"><b>{p.t}</b><span className="etq">{p.sis}</span></span>
                      <span className="doc">{p.ej || (c ? '—' : '…')}</span><span className="def">{DEF[p.g] || ''}</span>
                    </button>)
                })}
              </div>
            </div>
            <p className="nota" style={{ margin: '14px auto 0', maxWidth: 980 }}>El material es de Enel: sale del almacén (SGS), se instala, se reporta en el libreto (SCM) y Enel lo descarga en SAP. No se cobra: se rinde.
              {c && !visible ? ' La facturación se lee del informe de deuda de facturación de Dominion; el pago: fuente por definir.' : c && !c.pago_confirmado ? ' El pago se confirma con facturación: DRAPE no muestra pagos en este contrato.' : ''}</p>
          </section>
          <section className="panel" aria-labelledby="t-glos">
            <header><h2 id="t-glos">Doce palabras</h2>{paso && <span className="muted" style={{ fontSize: '.85rem' }}>paso {paso.n}: {paso.g}</span>}</header>
            <dl className="glosario">{GLOS.map(([a, b]) => <div key={a} data-glos={a} className={paso?.g === a ? 'on' : ''}><dt>{a}</dt><dd>{b}</dd></div>)}</dl>
          </section>
        </div>
      </div>
    </div>
  )
}
