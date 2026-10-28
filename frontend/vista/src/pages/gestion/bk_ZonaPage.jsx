// ZonaPage.jsx — /gestion: la portada de la zona para directivos (CHANGELOG 2026-10-25).: ¿cómo va la
// plata de la zona y qué está trabado? Una figura (MO por etapa), lo que frena el cobro en frases ordenadas por pesos, los trabajos por estado, los
// 10 más grandes en juego con UN estado cada uno y el pie «Garantía de los datos» plegado. Todo viene calculado de /api/gestion/zona.
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import api from '../../utils/api'
import './gestion-ux.css'
import FiguraDinero from './FiguraDinero'
import { BarraVista, Descargar, Dot, LoTiene, Pill } from './Estado'
import { ZONA, arriba, exporteTxt, fecha, limpiarNombre, miles, mm, plural } from './ux'

const ESTADO_CTL = { verde: 'verde', ambar: 'ambar', rojo: 'rojo' }

// (CHANGELOG 2026-10-25) el freno de los rechazos, por categoría del motivo (cuenta y pesos, de más a menos plata); cada categoría abre su lista filtrada
function Desglose({ d }) {
  const filas = d.filas || []
  if (!filas.length && !(d.otros || []).length) return null
  return (
    <div data-desglose>
      <div><b>Por qué los rechazó Enel</b> ({plural(d.n || 0, 'planilla', 'planillas')}, {mm(d.valor)}):</div>
      <table className="desglose num"><tbody>
        {filas.map((x) => (
          <tr key={x.k} data-desglose-cat={x.k}>
            <td><Link className="link" to={`/gestion/lista?alerta=rechazo_abierto&rechazo=${x.k}`}>{x.etiqueta}</Link></td>
            <td className="n">{plural(x.n, x.unidad === 'trabajos' ? 'trabajo' : 'planilla', x.unidad === 'trabajos' ? 'trabajos' : 'planillas')}</td>
            <td className="v">{x.valor !== null && x.valor !== undefined ? mm(x.valor) : '—'}</td>
            <td className="p muted">{x.pct !== null && x.pct !== undefined ? `${String(x.pct).replace('.', ',')} %` : ''}</td>
          </tr>))}
        {(d.otros || []).map((x) => (
          <tr key={x.k} data-desglose-cat={x.k} className="otro">
            <td><Link className="link" to={`/gestion/lista?rechazo=${x.k}`}>{x.etiqueta}</Link><div className="muted" style={{ fontSize: '.78rem' }}>{x.nota}</div></td>
            <td className="n">{plural(x.n, x.unidad === 'facturas' ? 'factura' : 'libreto', x.unidad === 'facturas' ? 'facturas' : 'libretos')}</td>
            <td className="v">{x.valor ? mm(x.valor) : '—'}</td><td />
          </tr>))}
      </tbody></table>
      {d.nota && <div className="muted" style={{ fontSize: '.78rem' }}>{d.nota}</div>}
    </div>
  )
}

function Freno({ g }) {
  const [abierto, setAbierto] = useState(false)
  return (
    <>
      <button type="button" className="freno" data-freno={g.k} aria-expanded={abierto} onClick={() => setAbierto(!abierto)}>
        <Dot color={g.color} />
        <span><span className="fr">{g.frase}</span><br /><span className="qn">{g.quien}{g.k === 'vencidas' ? ' · el valor es el total de las facturas' : ''}</span>
          {g.frase_desglose && <><br /><span className="qn" data-desglose-frase>{g.frase_desglose}</span></>}</span>
        <span className="v num">{g.valor !== null && g.valor !== undefined ? mm(g.valor) : ''}</span>
      </button>
      {abierto && (
        <div className="freno-det" data-freno-det={g.k}>
          {g.nota && <div>{g.nota}</div>}
          {g.desglose && <Desglose d={g.desglose} />}
          {g.ejemplos?.length > 0 && <div>Algunos: <span className="chips">{g.ejemplos.map((x) => <Link key={x.id} className="chip" to={`/gestion/${x.id}`}>{x.codigo}</Link>)}</span></div>}
          <div>{(g.tipos || []).slice(0, 3).map((t, i) => <span key={t}>{i ? ' · ' : ''}<Link className="link" to={`/gestion/lista?alerta=${t}`}>{i ? 'y la otra alerta del grupo' : 'Ver la lista completa de sus trabajos'}</Link></span>)}</div>
        </div>)}
    </>
  )
}

export default function ZonaPage() {
  const nav = useNavigate()
  const { data: z, isLoading, isError } = useQuery({ queryKey: ['gestion-zona'], queryFn: () => api.get('/gestion/zona').then((r) => r.data), refetchInterval: 120000 })
  useEffect(() => { arriba() }, [])
  if (isLoading) return <div className="gux"><BarraVista actual="zona" /><div className="gux-main"><p className="muted">Cargando la zona…</p></div></div>
  if (isError || !z) return <div className="gux"><BarraVista actual="zona" /><div className="gux-main"><p className="aviso">No se pudo leer la zona. La vista de operador sigue en <Link to="/gestion/lista">/gestion/lista</Link>.</p></div></div>
  const E = Object.fromEntries((z.etapas || []).map((e) => [e.k, e]))
  const g = z.garantia || {}
  const c = z.corte || {}
  return (
    <div className="gux" data-ux="zona">
      <BarraVista actual="zona" />
      <div className="gux-main">
        <div className="vista">
          <section className="intro">
            <div style={{ flex: '1 1 520px', minWidth: 0 }}>
              <div className="eyebrow">Zona VII · {miles(z.trabajos)} trabajos · datos al {fecha(z.calculado_en)}</div>
              <h1>¿Dónde está la plata de la zona?</h1>
              {z.cobro_visible === false
                ? <p>De {mm(E.libreto?.valor)} en libretos, {mm(E.facturado?.valor)} está en facturación según el informe de deuda de Dominion ({E.facturado?.sub}); otros {mm(E.facturado?.conciliacion)} siguen en conciliación. El pago: fuente por definir.</p>
                : <p>De {mm(E.libreto?.valor)} en libretos, {mm(E.facturado?.valor)} ya está en una factura registrada por Enel; {mm(E.facturado?.vencido)} de eso está en LCL con facturas vencidas.
                  {E.pagado?.por_confirmar ? ` ${E.pagado?.nota || ''}` : ` Pagado: ${mm(E.pagado?.valor)}.`}</p>}
            </div>
            <div className="corte">{[c.drape !== undefined ? ['Facturas', c.drape] : null, ['Sonny', c.sonny], ['SCM', c.scm], ['PMS', c.pms], ['SAP', c.sap], c.deuda ? ['Informe de deuda', c.deuda] : null]
              .filter(Boolean).map(([a, b]) => `${a} ${fecha(b)}`).join(' · ')}</div>
          </section>
          {z.sin_ux > 0 && <p className="aviso">{plural(z.sin_ux, 'trabajo aún no tiene', 'trabajos aún no tienen')} su estado calculado: salen al terminar el próximo recálculo.</p>}

          <FiguraDinero etapas={z.etapas} comoSeCuenta={z.como_se_cuenta} />

          <div className="dos">
            <section className="panel" aria-labelledby="t-frenos" data-frenos>
              <header><h2 id="t-frenos">Lo que frena el cobro</h2><span className="muted" style={{ fontSize: '.85rem' }}>de más a menos plata</span></header>
              <div className="frenos">{(z.grupos || []).map((gr) => <Freno key={gr.k} g={gr} />)}</div>
              <div className="material" data-material-zona><b>Aparte, el material de Enel</b> (no se cobra, se rinde): {miles(z.material?.casos)} trabajos no cuadran entre almacén, SCM y SAP por {mm(z.material?.valor)} a precio de Enel, y {plural(z.material?.por_liquidar?.n || 0, 'trabajo tiene', 'trabajos tienen')} material despachado hace 15+ días sin liquidar ({mm(z.material?.por_liquidar?.valor)}).</div>
            </section>
            <section className="panel" aria-labelledby="t-estados" data-estados>
              <header><h2 id="t-estados">Los {miles(z.trabajos)} trabajos, por estado</h2><span className="muted" style={{ fontSize: '.85rem' }}>cuántos y su mano de obra</span></header>
              <table className="estados num"><tbody>
                {(z.estados || []).map((e) => (
                  <tr key={e.k}><td><Dot color={e.color} /></td><td>{e.t}<div className="lotiene">{e.pelota !== '—' ? `lo tiene ${e.pelota}` : ''}</div></td><td className="n">{miles(e.n)}</td><td className="v">{mm(e.valor)}</td></tr>))}
              </tbody></table>
              <p className="nota" style={{ margin: '10px 0 0' }}>Cada trabajo con la mano de obra de su etapa más avanzada. Una LCL con dos proyectos en SGS cuenta como dos trabajos.</p>
            </section>
          </div>

          <section className="panel" aria-labelledby="t-top" data-top>
            <header><h2 id="t-top">Lo más grande en juego</h2><span className="muted" style={{ fontSize: '.85rem' }}>10 LCL o tickets con más mano de obra esperando cobro</span></header>
            <div className="scroll-x"><table className="lista num">
              <thead><tr><th>Trabajo</th><th>Zona</th><th>Estado</th><th>Lo tiene</th><th className="r">Mano de obra</th></tr></thead>
              <tbody>
                {(z.top || []).map((f) => (
                  <tr key={f.id} className="abre" tabIndex={0} data-trabajo={f.id} onClick={() => nav(`/gestion/${f.id}`)} onKeyDown={(e) => { if (e.key === 'Enter') nav(`/gestion/${f.id}`) }}>
                    <td className="c-t"><span className="mono">{f.codigo}</span>{f.gemelos > 1 ? <> <span className="etq">{f.gemelos} proyectos en SGS</span></> : null}<div className="nom">{limpiarNombre(f.nombre, f.codigo)}</div></td>
                    <td className="c-z">{ZONA[f.zona] || f.zona || '—'}</td>
                    <td className="c-e"><Pill estado={f.estado} /></td>
                    <td className="lotiene c-q">{f.estado?.pelota}</td>
                    <td className="r c-m"><b>{mm(f.valor)}</b></td>
                  </tr>))}
              </tbody>
            </table></div>
            <p className="nota" style={{ margin: '10px 0 0' }}>Cada fila abre su trabajo. La lista completa, con filtros, está en la <Link to="/gestion/lista">vista de operador</Link>.</p>
          </section>

          <details className="panel garantia" data-garantia>
            <summary><Dot color={g.rojos ? 'rojo' : g.ambar ? 'ambar' : 'verde'} /><b>Garantía de los datos</b><span className="muted" style={{ fontSize: '.88rem' }}>{g.verdes} controles en verde · {g.ambar} esperan a una persona · {g.rojos} en rojo · revisado {fecha(g.calculado)}</span></summary>
            <p className="nota" style={{ marginTop: 12 }}>Antes de mostrar un número, el sistema se revisa: cada cifra de cada trabajo se vuelve a sumar con sus filas y tiene que dar igual al peso.</p>
            <div className="ctls">{(g.controles || []).map((x) => <div key={x.k}><Dot color={ESTADO_CTL[x.estado] || 'gris'} /><span>{x.titulo}</span><span className="num muted">{String(x.valor ?? '').replace('alta', 'exacto').replace('media', 'por monto').replace('ambigua', 'varias planillas')}</span></div>)}</div>
            <div className="scroll-x"><table className="tabla-alt num">
              <thead><tr><th>Fuente</th><th>Exporte</th><th className="r">Filas</th><th>Archivo</th></tr></thead>
              <tbody>{(g.fuentes || []).map((s) => <tr key={s.k} data-fuente={s.k}><td>{s.titulo}</td><td data-exporte>{exporteTxt(s)}</td><td className="r">{s.filas ? miles(s.filas) : '—'}</td>
                <td>{s.tipo === 'exporte' || s.tipo === 'pantalla' ? <Descargar cargas={s.cargas} /> : null}</td></tr>)}</tbody>
            </table></div>
            <p className="nota">El panel técnico completo («Fuentes y controles», con archivos y huellas) sigue en la <Link to="/gestion/lista">vista de operador</Link>.</p>
          </details>
        </div>
      </div>
    </div>
  )
}
