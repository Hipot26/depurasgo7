// Cadena.jsx — la cadena de identificadores de un trabajo (CHANGELOG 2026-10-25): LCL → solicitud PMS → PDL → nodos → planillas → libretos → facturas → pago.
// Calmada por defecto (sin líneas); al tocar una pieza se dibujan sus conectores directos (continua = enlace exacto; punteada = por monto, por
// confirmar o por la LCL), se resalta su hebra a izquierda y derecha y debajo sale su ficha. En celular las columnas se apilan y no hay líneas: la
// ficha nombra los enlaces. Las piezas y los enlaces vienen armados del backend (/api/gestion/proyectos/{id}/cadena).
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Marca, Dot } from './Estado'
import { COLS, HAY_FLUJO, QUE_ES, fecha, mm, pesos, plural, pillDe } from './ux'
import { Link } from 'react-router-dom'
import Motivo from './Motivo'

const MAXV = 5
const HUB = new Set(['origen', 'pms'])        // la LCL y la solicitud tocan todo: desde ellas solo se ven sus enlaces directos
const colIdx = (nodo) => COLS.findIndex((c) => c.k === nodo.split(':')[0])

function useMovil(q = '(max-width: 880px)') {
  const [m, setM] = useState(() => typeof window !== 'undefined' && window.matchMedia(q).matches)
  useEffect(() => {
    const mq = window.matchMedia(q)
    const f = () => setM(mq.matches)
    mq.addEventListener ? mq.addEventListener('change', f) : mq.addListener(f)
    return () => { mq.removeEventListener ? mq.removeEventListener('change', f) : mq.removeListener(f) }
  }, [q])
  return m
}

// De las piezas del backend a lo que se pinta: título, estado, marca, una línea de cifra y la ficha
export function piezasDe(cad, codigo) {
  const P = cad?.piezas || {}
  const out = Object.fromEntries(COLS.map((c) => [c.k, []]))
  const esDf = String(codigo || '').startsWith('DF')
  ;(P.origen || []).forEach((x) => {
    const tk = x.tipo === 'Ticket'
    out.origen.push({ id: x.id, tit: x.id, est: x.estado, tono: x.color, m: !tk && x.inicio ? `vigente ${fecha(x.inicio)} → ${fecha(x.fin)}` : '', ev: tk ? null : 'scm_lcl',
      datos: tk ? [['Tipo', x.estado]] : [['Estado en SCM', x.estado], ['Vigencia', x.inicio ? `${fecha(x.inicio)} → ${fecha(x.fin)}` : '—'], ['Importe de la LCL', pesos(x.importe)],
        ['Órdenes de SAP', (x.ordenes || []).join(', ') || '—']] })
  })
  ;(P.pms || []).forEach((x) => out.pms.push({ id: x.id, tit: x.id, est: x.estado, tono: x.color, m: `presupuesto MO ${mm(x.mo)}`, ev: 'pms',
    datos: [['Estado', x.estado], ['Desde', fecha(x.fecha)], ['Presupuesto de mano de obra', pesos(x.mo)], ['Presupuesto de material', pesos(x.mat)]] }))
  ;(P.pdl || []).forEach((x) => out.pdl.push({ id: x.id, tit: x.id, est: '', tono: 'gris', m: [x.en_planillas ? plural(x.en_planillas, 'planilla', 'planillas') : '', x.en_libretos ? plural(x.en_libretos, 'libreto', 'libretos') : ''].filter(Boolean).join(' · '),
    datos: [['Aparece en', [x.en_planillas ? plural(x.en_planillas, 'planilla', 'planillas') : '', x.en_libretos ? plural(x.en_libretos, 'libreto', 'libretos') : ''].filter(Boolean).join(' y ') || '—']] }))
  const nodo = (s) => String(s).replace(/^NODO\s*/i, '')
  ;(P.nodos || []).forEach((x) => {
    if (x.sgs) out.nodos.push({ id: x.id, tit: x.sgs, est: x.gemelo ? 'otro proyecto en SGS de esta LCL' : (x.n ? `proyecto en SGS · ${plural(x.n, 'nodo', 'nodos')}` : 'proyecto en SGS'),
      tono: 'gris', m: x.liquidado ? `liquidado ${mm(x.liquidado)}` : '', ev: x.gemelo ? null : 'sgs_mo', gemelo: !!x.gemelo,
      datos: [['Proyecto en SGS', x.sgs], ['Nodos', x.n ?? '—'], ['Mano de obra liquidada', `${pesos(x.liquidado)} (referencia: se muestra, no se compara)`], ...(x.ultima ? [['Última liquidación', fecha(x.ultima)]] : [])] })
    else if (x.grupo) out.nodos.push({ id: x.id, tit: plural(x.n, 'nodo', 'nodos'), pal: true, est: `de la planilla ${x.grupo}`, tono: 'listo',
      m: (x.muestra || []).slice(0, 4).map(nodo).join(' · ') + ((x.muestra || []).length > 4 ? '…' : ''), datos: [['Nodos', (x.muestra || []).map(nodo).join(' · ')]] })
    else out.nodos.push({ id: x.id, tit: plural(x.n, 'nodo sin planilla', 'nodos sin planilla'), pal: true, est: x.estado, tono: 'ambar', m: x.dias ? `el más viejo: ${x.dias} días hábiles` : '', ev: 'sonny_etapa',
      datos: [['Nodos', (x.muestra || []).map(nodo).join(' · ')], ['Estado', x.estado], ...(x.techo ? [['Presupuestado aún sin planilla', `${pesos(x.techo)} (es un techo, no una deuda)`]] : [])] })
  })
  ;(P.planillas || []).forEach((x) => {
    const h = x.hitos || {}
    const mot = x.motivo          // (CHANGELOG 2026-10-25) la planilla rechazada abierta trae el motivo que escribió Enel
    out.planillas.push({ id: x.id, tit: x.id, est: mot ? 'Rechazada por Enel' : x.estado, tono: x.color, m: `MO ${mm(x.mo)}`, ev: mot ? 'alerta:rechazo_abierto' : 'sonny',
      mot: mot ? `motivo: ${mot.etiqueta || 'sin texto'}` : '', motivo: mot || null,
      datos: [['Estado', x.estado], ['Mano de obra', pesos(x.mo)], ['Nodos', x.nodos || '—'], ['PDL', ((x.pdl || []).join(', ') || '—') + (x.pdl_cortado ? ' (Sonny lo trae cortado)' : '')],
        ['Solicitud', x.qr || '—'], ...(h.firmada ? [['Firmada', fecha(h.firmada)]] : []), ...(h.recibo ? [['Enviada a Enel (recibo de obra)', fecha(h.recibo)]] : []),
        ...(h.conciliada ? [['Conciliada', fecha(h.conciliada)]] : []), ...(h.aprobada ? [['Aprobada por Enel', fecha(h.aprobada)]] : []),
        ...(h.rechazada ? [['Rechazada alguna vez', fecha(h.rechazada)]] : []), ...((x.rechazos_n || 0) > 1 ? [['Rechazos en Sonny', plural(x.rechazos_n, 'vez', 'veces')]] : [])] })
  })
  const libs = P.libretos || []
  libs.filter((x) => !x.muerto).forEach((x) => out.libretos.push({ id: x.id, tit: x.corto, est: x.estado, tono: x.color, ev: 'scm_mo',
    m: `MO ${mm(x.mo)}${x.enlace === 'monto' || x.enlace === 'pendiente' ? ' · falta confirmar' : !x.planilla ? ' · sin planilla' : ''}`,
    datos: [['Libreto', x.id], ['Estado en SCM', x.estado], ['Mano de obra', pesos(x.mo)], ['Autocertificado', fecha(x.fecha)], ['Planilla', x.planilla || 'sin planilla enlazada'],
      ...(x.planilla || x.enlace ? [['Cómo se enlazó', x.enlace_t]] : []), ['PDL', (x.pdl || []).join(', ') || '—']] }))
  libs.filter((x) => x.muerto).forEach((x) => out.libretos.push({ id: x.id, tit: x.corto, est: x.estado, tono: 'gris', m: '', muerto: true,
    datos: [['Libreto', x.id], ['Estado', x.estado], ['Por qué no suma', x.por_que || '—']] }))
  ;(P.facturas || []).forEach((x) => out.facturas.push({ id: x.id, tit: x.id, est: x.estado, tono: x.color, ev: 'drape',
    m: x.importe !== null && x.importe !== undefined ? `${mm(x.importe)} de esta LCL` : `en factura de ${mm(x.total)} (${x.n_lcl} LCL)`,
    datos: [['Estado', x.estado], ['Fecha', fecha(x.fecha)], ['Registrada por Enel', fecha(x.registro)], ['Vence', `${fecha(x.vence)}${x.vence && !x.vence_exacta ? ' (estimado)' : ''}`],
      ['Pago', x.pago ? fecha(x.pago) : 'sin pago'], ['De esta LCL', x.importe !== null && x.importe !== undefined ? pesos(x.importe) : 'DRAPE no la reparte'],
      ['Total de la factura', `${pesos(x.total)} · ${x.n_lcl} LCL · ${x.n_em} HES`]] }))
  const pg = cad?.pago || {}
  if ((P.facturas || []).length) {
    const pagado = pg.estado === 'pagado'
    out.pago.push({ id: 'pago', tit: pagado ? (pg.confirmado ? 'Pagado' : 'Pagado según DRAPE') : 'Sin pago', pal: true,
      est: pg.vencidas ? plural(pg.vencidas, 'factura vencida', 'facturas vencidas') : pg.proximo ? `próximo vence ${fecha(pg.proximo)}` : 'esperando registro de Enel',
      tono: pagado && pg.confirmado ? 'verde' : pg.vencidas ? 'rojo' : pagado ? 'gris' : 'ambar', m: pg.confirmado ? 'según DRAPE' : 'por confirmar con facturación', ev: 'drape',
      datos: [['Pagado', pg.confirmado ? pesos(pg.pagado) : (pg.nota || 'por confirmar')], ['Próximo vencimiento', fecha(pg.proximo)]] })
  } else out.pago.push({ id: 'pago', vacia: true, tit: 'todavía no toca' })
  const vacio = { pms: 'sin solicitud en PMS', pdl: 'sin PDL leído', nodos: 'sin nodos', planillas: 'sin planilla todavía', libretos: 'sin libreto todavía', facturas: 'sin factura todavía' }
  Object.keys(vacio).forEach((k) => { if (!out[k].length) out[k].push({ id: '_' + k, vacia: true, tit: vacio[k] }) })
  if (esDf && (P.origen || []).length === 1) out.origen.push({ id: '_lcl', vacia: true, tit: 'sin LCL todavía' })
  return out
}

function grafo(cad) {
  const ady = {}, tipo = {}
  const add = (a, b, t) => { (ady[a] = ady[a] || new Set()).add(b); (ady[b] = ady[b] || new Set()).add(a); tipo[a + '|' + b] = tipo[b + '|' + a] = t }
  ;(cad?.enlaces || []).forEach(([ca, ia, cb, ib, t]) => add(`${ca}:${ia}`, `${cb}:${ib}`, t))
  const P = cad?.piezas || {}
  ;(P.facturas || []).forEach((f) => {
    add(`facturas:${f.id}`, 'pago:pago', 'exacto')
    ;(P.libretos || []).filter((l) => !l.muerto && Number(l.mo || 0) > 0 && (f.lcls || []).includes(l.lcl)).forEach((l) => add(`libretos:${l.id}`, `facturas:${f.id}`, 'lcl'))
  })
  return { ady, tipo }
}

function hebra(G, s) {     // a la izquierda solo por enlaces a la izquierda, y lo mismo a la derecha (sin los «por LCL»)
  const real = (a, b) => G.tipo[a + '|' + b] !== 'lcl'
  const n1 = new Set([...(G.ady[s] || [])].filter((y) => real(s, y)))
  const todo = new Set([s, ...n1])
  if (!HUB.has(s.split(':')[0])) ['izq', 'der'].forEach((dir) => {
    const cola = [s], vis = new Set([s])
    while (cola.length) {
      const x = cola.shift(), ix = colIdx(x)
      if (x !== s && HUB.has(x.split(':')[0])) continue
      ;(G.ady[x] || []).forEach((y) => { const iy = colIdx(y); if (vis.has(y) || !real(x, y)) return; if ((dir === 'izq' && iy < ix) || (dir === 'der' && iy > ix)) { vis.add(y); todo.add(y); cola.push(y) } })
    }
  })
  const suave = new Set()
  todo.forEach((x) => { if (x.startsWith('libretos:')) (G.ady[x] || []).forEach((y) => { if (!real(x, y)) suave.add(y) }) })
  return { n1, todo, suave }
}

function codo(P, r = 6) {   // línea quebrada con esquinas redondeadas
  const Q = P.filter((x, i) => i === 0 || Math.hypot(x[0] - P[i - 1][0], x[1] - P[i - 1][1]) > 0.5)
  let d = `M${Q[0][0]} ${Q[0][1]}`
  for (let i = 1; i < Q.length - 1; i++) {
    const [x0, y0] = Q[i - 1], [x, y] = Q[i], [x1, y1] = Q[i + 1]
    const l1 = Math.hypot(x - x0, y - y0), l2 = Math.hypot(x1 - x, y1 - y), rr = Math.min(r, l1 / 2, l2 / 2)
    d += ` L${x - (x - x0) / l1 * rr} ${y - (y - y0) / l1 * rr} Q${x} ${y} ${x + (x1 - x) / l2 * rr} ${y + (y1 - y) / l2 * rr}`
  }
  const L = Q[Q.length - 1]
  return d + ` L${L[0]} ${L[1]}`
}

function Ficha({ sel, PZ, G, H, onEvidencia }) {
  const ck = sel.split(':')[0], id = sel.slice(sel.indexOf(':') + 1)
  const col = COLS.find((x) => x.k === ck)
  const x = (PZ[ck] || []).find((p) => p.id === id)
  if (!x) return null
  const rel = [...H.n1].map((k) => {
    const kk = k.split(':')[0], ii = k.slice(k.indexOf(':') + 1)
    const p = (PZ[kk] || []).find((y) => y.id === ii)
    const t = G.tipo[sel + '|' + k]
    const u = COLS.find((y) => y.k === kk)?.u
    if (!p) return null
    if (kk === 'pago') return p.tit === 'Pagado' ? 'el pago' : 'el pago (aún no llega)'
    if (kk === 'nodos' && String(p.id).startsWith('sgs-')) return `proyecto en SGS ${p.tit}${p.gemelo ? ' (otro proyecto de esta LCL)' : ''}`
    return `${u ? u + ' ' : ''}${p.tit}${t === 'monto' || t === 'pendiente' ? ' (falta confirmar)' : ''}`
  }).filter(Boolean)
  return (
    <div className="ficha" data-ficha={sel}>
      <div className="t">
        <h3>{col.u ? col.u.charAt(0).toUpperCase() + col.u.slice(1) : ''} <span className="mono">{x.tit}</span></h3>
        <span className="etq">{col.s}</span>
        {x.est && <span className={`pill ${pillDe(x.tono)}`}><Marca tono={x.tono} />{x.est}</span>}
      </div>
      <dl className="num">{(x.datos || []).map(([a, b]) => <Fragment key={a}><dt>{a}</dt><dd>{b}</dd></Fragment>)}</dl>
      {x.motivo && <Motivo m={x.motivo} planilla={x.id} corte={180} />}
      {rel.length > 0 && <div><b>Se enlaza con:</b> {rel.slice(0, 14).join(' · ')}{rel.length > 14 ? ` y ${rel.length - 14} más` : ''}.</div>}
      {ck === 'facturas' && <div className="muted">La factura va por LCL: DRAPE no dice qué libreto cobra. Los libretos de esta LCL están en su columna.</div>}
      {ck === 'libretos' && H.suave.size > 0 && <div className="muted">Se cobra en {plural(H.suave.size, 'factura', 'facturas')} de su LCL (resaltadas): DRAPE nombra la LCL, no el libreto.</div>}
      <div className="que">{QUE_ES[ck]}</div>
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        {x.ev && <button type="button" className="link" data-ficha-ev={x.ev} onClick={() => onEvidencia(x.ev)}>¿De dónde sale?</button>}
        {HAY_FLUJO && <Link className="link" to="/gestion/flujo">¿Qué es esto en el flujo?</Link>}
      </div>
    </div>
  )
}

export default function Cadena({ cadena, codigo, onEvidencia }) {
  const PZ = useMemo(() => piezasDe(cadena, codigo), [cadena, codigo])
  // (CHANGELOG 2026-10-25) sin la factura ni el pago cuando la API dice que no se muestran (todo se justifica con exportes)
  const cols = cadena?.cobro_visible === false ? COLS.filter((c) => c.k !== 'facturas' && c.k !== 'pago') : COLS
  const G = useMemo(() => grafo(cadena), [cadena])
  const [sel, setSel] = useState(null)
  const [abiertas, setAbiertas] = useState({})
  const movil = useMovil()
  const cont = useRef(null)
  const svg = useRef(null)
  const [ancho, setAncho] = useState(0)
  const H = sel ? hebra(G, sel) : null
  useEffect(() => { setSel(null); setAbiertas({}) }, [cadena])
  useEffect(() => {
    let t = null
    const f = () => { clearTimeout(t); t = setTimeout(() => setAncho(window.innerWidth), 150) }
    window.addEventListener('resize', f)
    return () => { window.removeEventListener('resize', f); clearTimeout(t) }
  }, [])
  // las líneas: nunca cruzan una pieza; van por el canal entre columnas y, si saltan columnas, por debajo de las piezas
  useLayoutEffect(() => {
    const s = svg.current, c = cont.current
    if (!s || !c) return
    if (!H || movil) { s.innerHTML = ''; return }
    const base = c.getBoundingClientRect()
    const el = (k) => c.querySelector(`.pz[data-k="${CSS.escape(k)}"]`)
    const a = el(sel)
    if (!a) { s.innerHTML = ''; return }
    const R = (e) => { const r = e.getBoundingClientRect(); return { l: r.left - base.left, r: r.right - base.left, b: r.bottom - base.top, y: r.top + r.height / 2 - base.top } }
    const cols = [...c.querySelectorAll('.col')]
    const fondo = cols.map((x) => (x.lastElementChild ? R(x.lastElementChild).b : 0))
    const canal = (i) => (R(cols[i]).r + R(cols[i + 1]).l) / 2
    const ia = colIdx(sel)
    const muchas = H.n1.size > 4
    let out = ''
    H.n1.forEach((k) => {
      const b = el(k)
      if (!b) return
      const ib = colIdx(k)
      if (ib === ia) return
      const [p, q, ip, iq] = ib > ia ? [R(a), R(b), ia, ib] : [R(b), R(a), ib, ia]
      let puntos
      if (iq === ip + 1) { const xm = canal(ip); puntos = [[p.r, p.y], [xm, p.y], [xm, q.y], [q.l, q.y]] } else {
        const bajo = Math.max(...fondo.slice(ip + 1, iq)) + 12, x1 = canal(ip), x2 = canal(iq - 1)
        puntos = [[p.r, p.y], [x1, p.y], [x1, bajo], [x2, bajo], [x2, q.y], [q.l, q.y]]
      }
      const t = G.tipo[sel + '|' + k]
      out += `<path d="${codo(puntos)}" fill="none" stroke="var(--accent)" stroke-width="${muchas ? 1.2 : 1.6}" ${t === 'exacto' || t === 'confirmado' ? '' : 'stroke-dasharray="4 4"'} opacity="${muchas ? '.6' : '.85'}" data-linea="${t}"/>`
    })
    s.innerHTML = out
  })
  const clic = (k) => setSel(sel === k ? null : k)
  const ficha = sel && H ? <Ficha sel={sel} PZ={PZ} G={G} H={H} onEvidencia={onEvidencia} /> : null
  const colSel = sel ? sel.split(':')[0] : null
  return (
    <>
      <div className="scroll-x">
        <div className={`cadena-wrap${cols.length < COLS.length ? ' cols-6' : ''}`}>
          <div className={`cadena${sel ? ' sel' : ''}${cols.length < COLS.length ? ' cols-6' : ''}`} ref={cont} data-cadena data-ancho={ancho} data-columnas={cols.length}>
            <svg className="lineas" aria-hidden="true" ref={svg} />
            {cols.map((col) => {
              const xs = PZ[col.k] || []
              const vivos = xs.filter((x) => !x.muerto), muertos = xs.filter((x) => x.muerto)
              let extra = 0
              const vis = abiertas[col.k] ? vivos : vivos.filter((x, i) => i < MAXV || (H && H.n1.has(`${col.k}:${x.id}`) && extra++ < 8))
              const ocultos = vivos.length - vis.length
              const ocultosRel = H ? vivos.filter((x) => !vis.includes(x) && (H.todo.has(`${col.k}:${x.id}`) || H.suave.has(`${col.k}:${x.id}`))).length : 0
              const pieza = (x) => {
                if (x.vacia) return <div key={x.id} className="pz vacia" data-vacia={col.k}><span className="i">{x.tit}</span></div>
                const key = `${col.k}:${x.id}`
                const cls = !H ? '' : key === sel ? 'on' : H.n1.has(key) ? 'n1' : H.todo.has(key) || H.suave.has(key) ? 'n2' : ''
                return (
                  <button key={key} type="button" className={`pz ${cls}`} data-k={key} data-pieza={key} aria-pressed={key === sel} onClick={() => clic(key)}>
                    <span className={`i${x.pal ? ' pal' : ''}`}>{x.tit}</span>
                    {x.est && <span className="s"><Marca tono={x.tono} />{x.est}</span>}
                    {x.m && <span className="m num">{x.m}</span>}
                    {x.mot && <span className="mot" data-pieza-motivo>{x.mot}</span>}
                  </button>)
              }
              const n = vivos.filter((x) => !x.vacia).length
              return (
                <div key={col.k} className="col" data-col={col.k}>
                  <div className="col-h"><b>{col.t}{n > 1 ? ` · ${n}` : ''}</b><small>{col.s}</small></div>
                  {vis.map(pieza)}
                  {ocultos > 0 && <button type="button" className="mas" onClick={() => setAbiertas({ ...abiertas, [col.k]: true })}>+ {ocultos} más{ocultosRel ? ` (${ocultosRel} ${ocultosRel === 1 ? 'enlazada' : 'enlazadas'})` : ''}</button>}
                  {abiertas[col.k] && vivos.length > MAXV && <button type="button" className="mas" onClick={() => setAbiertas({ ...abiertas, [col.k]: false })}>ver menos</button>}
                  {muertos.length > 0 && (abiertas[col.k + '-m']
                    ? <>{muertos.map(pieza)}<button type="button" className="mas" onClick={() => setAbiertas({ ...abiertas, [col.k + '-m']: false })}>ocultar devueltos y anulados</button></>
                    : <button type="button" className="mas" onClick={() => setAbiertas({ ...abiertas, [col.k + '-m']: true })}>{plural(muertos.length, 'devuelto o anulado', 'devueltos o anulados')}</button>)}
                  {movil && colSel === col.k && ficha}
                </div>)
            })}
          </div>
        </div>
      </div>
      {!movil && ficha}
      {!sel && <p className="ayuda" style={{ margin: '10px 0 0' }}><Dot color="gris" chico /> Línea continua = enlace exacto · punteada = por monto, por confirmar o por la LCL. Lo que falta se dice en su columna.</p>}
    </>
  )
}
