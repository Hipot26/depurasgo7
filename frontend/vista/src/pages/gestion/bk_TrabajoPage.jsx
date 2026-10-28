// TrabajoPage.jsx — /gestion/:id: un trabajo para el directivo (CHANGELOG 2026-10-25).: ¿dónde está este
// trabajo y qué falta para cobrarlo? Estado y quién lo tiene, la cadena de identificadores (calmada; se enciende al tocar una pieza), «Qué falta para
// cobrar», las 6 cifras de mano de obra con «¿De dónde sale?» discreto y el material de Enel aparte. Plegados: línea de tiempo, alertas en texto
// completo y operación. Todo viene armado de /api/gestion/proyectos/{id}/cadena; la línea de tiempo, de /api/gestion/proyectos/{id} al abrirla.
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import api from '../../utils/api'
import { useAuthStore } from '../../store/authStore'
import './gestion-ux.css'
import Cadena from './Cadena'
import QueFalta from './QueFalta'
import Evidencia from './Evidencia'
import { BarraVista, Dot, LoTiene, Pill } from './Estado'
import { HAY_FLUJO, SIS, TIPO, ZONA, arriba, fecha, limpiarEvento, limpiarNombre, mesAnio, miles, mm, plural } from './ux'

// los trámites no traen cifra ni tarjeta de evidencia; las demás alertas sí («¿De dónde sale?» abre su tarjeta en el grupo «Avisos»)
const TRAMITE = new Set(['plan_sin_validar', 'cms_sin_aprobar', 'sin_liquidar', 'sin_actividad', 'sonny_pendiente', 'ejecutado_sin_planilla'])
// (CHANGELOG 2026-10-25) la planilla rechazada sí tiene tarjeta (el motivo, en «Avisos»), salvo el rechazo visto solo por correo
const sinTarjeta = (a) => TRAMITE.has(a.tipo) || (a.tipo === 'rechazo_abierto' && !(a.planillas || []).length)

function Plata({ plata, onEvidencia }) {
  const cifras = plata?.cifras || []
  const m = plata?.material || {}
  return (
    <section className="panel" aria-labelledby="t-plata" data-plata-panel>
      <header><h2 id="t-plata">La mano de obra de este trabajo</h2><span className="ayuda">Toca una cifra para ver de dónde sale.</span></header>
      <div className="plata num" data-plata>
        {cifras.map((x) => {
          const cero = !x.mo
          return (
            <div key={x.k} className="et-p" data-caja-ux={x.k}>
              <div className="h">{x.t} <span className="etq">{x.sis}</span></div>
              {x.mo === null || x.mo === undefined
                ? <span className="v gris">{x.k === 'pago' && x.por_confirmar && x.nota ? (x.nota === 'fuente por definir' ? 'por definir' : 'por confirmar') : '—'}</span>
                : <button type="button" className={`v cifra num${x.gris ? ' gris' : ''}`} data-cifra={x.ev} onClick={() => onEvidencia(x.ev)}>{mm(x.mo)}</button>}
              <div className={`b${x.gris ? ' gris' : ''}${cero ? ' cero' : ''}`} style={{ width: cero ? '100%' : `${Math.max(3, x.barra || 0)}%` }} />
              {x.nota && <span className={`x${x.rojo ? ' rojo' : ''}`}>{x.nota}</span>}
            </div>)
        })}
      </div>
      <div className="mat-linea" data-material>
        <Dot color={m.color} />
        <span><b>Material de Enel</b> (no se cobra, se rinde): {m.n ? `${plural(m.n, 'código', 'códigos')} · ${miles(m.cuadran)} cuadran · ${miles(m.difieren)} con diferencia` : 'sin material leído'}{m.avisos ? ` · ${plural(m.avisos, 'aviso', 'avisos')} por ${mm(m.valor)}` : ''}.</span>
        {m.n > 0 && <button type="button" className="link" data-cifra="material" onClick={() => onEvidencia('material')}>¿De dónde sale?</button>}
      </div>
    </section>
  )
}

function LineaTiempo({ id, abierta }) {
  const { data: det, isLoading } = useQuery({ queryKey: ['gestion-proyecto', String(id)], queryFn: () => api.get(`/gestion/proyectos/${id}`).then((r) => r.data), enabled: abierta })
  const filas = useMemo(() => {
    const vistos = new Map()
    ;(det?.eventos || []).filter((e) => e.paso).forEach((e) => {
      const k = fecha(e.ts) + '|' + e.prueba
      if (!vistos.has(k)) vistos.set(k, e)
    })
    return [...vistos.values()].sort((a, b) => String(a.ts).localeCompare(String(b.ts)))
  }, [det])
  if (!abierta) return null
  if (isLoading) return <p className="muted" style={{ marginTop: 10 }}>Cargando…</p>
  const ult = filas.slice(-150)
  const items = []
  let mes = ''
  if (filas.length > ult.length) items.push(<li key="mas" className="mes">Los {ult.length} más recientes de {filas.length}</li>)
  if (!filas.length) items.push(<li key="nada" className="muted">Sin eventos con paso.</li>)
  ult.forEach((e) => {
    const m = mesAnio(e.ts)
    if (m !== mes) { mes = m; items.push(<li key={'m' + e.id} className="mes">{m}</li>) }
    items.push(<li key={e.id}><span className="num">{fecha(e.ts)}</span><span className="sis-t"><span className="etq">{SIS[e.fuente] || e.fuente}</span></span><span>{limpiarEvento(e.prueba)}</span></li>)
  })
  return <ul className="tl" data-linea-tiempo>{items}</ul>
}

export default function TrabajoPage() {
  const { id } = useParams()
  const usuario = useAuthStore((s) => s.usuario)
  const esDev = usuario?.rol === 'desarrollador'
  const { data: c, isLoading, isError } = useQuery({ queryKey: ['gestion-cadena', String(id)], queryFn: () => api.get(`/gestion/proyectos/${id}/cadena`).then((r) => r.data) })
  const [ev, setEv] = useState(null)
  const [verTl, setVerTl] = useState(false)
  useEffect(() => { arriba(); setEv(null); setVerTl(false) }, [id])
  if (isLoading) return <div className="gux"><BarraVista actual="trabajo" /><div className="gux-main"><p className="muted">Cargando el trabajo…</p></div></div>
  if (isError || !c) return <div className="gux"><BarraVista actual="trabajo" /><div className="gux-main"><p className="aviso">No se encontró este trabajo. <Link to="/gestion">Volver a la zona</Link>.</p></div></div>
  const esDf = String(c.codigo || '').startsWith('DF')
  const herm = c.hermanos || []
  const alertas = c.alertas || []
  return (
    <div className="gux" data-ux="trabajo" data-trabajo-id={c.id}>
      <BarraVista actual="trabajo" />
      <div className="gux-main">
        <div className="vista">
          <div><Link to="/gestion" className="volver">← Zona VII</Link></div>
          <section className="cab">
            <div className="id">
              <h1 className="cod"><small>{esDf ? 'Ticket' : 'LCL'}</small>{c.codigo || c.clave}</h1>
              <div className="nm">{limpiarNombre(c.nombre, c.codigo)}</div>
              <div className="meta">{TIPO[c.tipo] || c.tipo} · {ZONA[c.zona] || c.zona || 'sin zona'} · {c.supervisor || 'sin supervisor'} · proyecto en SGS <span className="mono">{(c.codes || [c.clave]).join(' + ')}</span></div>
            </div>
            <div className="est"><Pill estado={c.estado} /><LoTiene pelota={c.estado?.pelota} /></div>
          </section>
          {herm.length > 0 && (
            <div className="gemelo" data-gemelo>
              <span>Esta LCL tiene otro proyecto en SGS: <span className="mono">{herm[0].clave}</span> ({TIPO[herm[0].tipo] || herm[0].tipo}{herm[0].supervisor ? `, ${herm[0].supervisor}` : ''}). La plata de la LCL es la misma; cada proyecto se ve por separado.</span>
              <Link className="link" to={`/gestion/${herm[0].id}`}>Ver {herm[0].clave}</Link>
            </div>)}
          {(c.sin_calcular || c.error) && <p className="aviso">Este trabajo aún no tiene su estado calculado{c.error ? ' (el último cálculo falló)' : ''}: sale con el próximo recálculo. Mientras tanto, la <Link to={`/gestion/lista/${c.id}`}>vista de operador</Link> lo muestra completo.</p>}

          <QueFalta frases={c.que_falta} />

          <section className="panel" aria-labelledby="t-cadena" data-cadena-panel>
            <header><h2 id="t-cadena">La cadena de este trabajo</h2><span className="ayuda">Toca una pieza para ver con qué se enlaza.</span>
              {HAY_FLUJO && <span className="der"><Link className="link" to="/gestion/flujo">¿Cómo fluye?</Link></span>}</header>
            <Cadena cadena={c.cadena} codigo={c.codigo} onEvidencia={setEv} />
          </section>

          <Plata plata={c.plata} onEvidencia={setEv} />

          <section className="panel">
            <details className="plegado" onToggle={(e) => { if (e.currentTarget.open) setVerTl(true) }} data-plegado="linea">
              <summary>Línea de tiempo · {plural(c.n_eventos || 0, 'evento', 'eventos')}</summary>
              <LineaTiempo id={c.id} abierta={verTl} />
            </details>
            <details className="plegado" style={{ marginTop: 10 }} data-plegado="alertas">
              <summary>Alertas en texto completo · {alertas.length || 'ninguna'}</summary>
              <ul className="falta" style={{ marginTop: 10 }}>
                {alertas.length ? alertas.map((a, i) => (
                  <li key={i}><Dot color={a.color} /><span style={{ fontSize: '.9rem' }}>{a.frase || a.texto}{a.texto && a.frase ? <><br /><span className="muted" style={{ fontSize: '.8rem' }}>{a.texto}</span></> : null}
                    {!sinTarjeta(a) && <> <button type="button" className="link" style={{ fontSize: '.8rem' }} data-alerta-ev={a.tipo} onClick={() => setEv(`alerta:${a.tipo}`)}>¿De dónde sale?</button></>}</span></li>))
                  : <li className="muted">Sin alertas.</li>}
              </ul>
            </details>
            {esDev && (
              <details className="plegado" style={{ marginTop: 10 }} data-plegado="operacion">
                <summary>Operación (solo desarrollador)</summary>
                <p className="nota" style={{ marginTop: 8 }}>Recalcular el trabajo, corregir zona o supervisor, confirmar enlaces («Es este» / «No es») y el Excel de evidencia están en la vista de operador.</p>
                <div className="operar">
                  <Link className="link" to={`/gestion/lista/${c.id}`}>Abrir en la vista de operador</Link>
                  <Link className="link" to="/modulos/centro-mo">Centro MO</Link>
                  <Link className="link" to="/modulos/control">Control Mensual</Link>
                  <Link className="link" to="/liquidador">Liquidador</Link>
                  <Link className="link" to={`/registros?q=${encodeURIComponent(c.codigo || c.clave)}`}>Registros</Link>
                </div>
              </details>)}
            <p className="nota" style={{ marginTop: 12 }}>Datos calculados el {fecha(c.calculado_en)}.{c.cobro_visible === false ? ' La facturación se lee del informe de deuda de facturación de Dominion; el pago: fuente por definir.' : c.pago_confirmado ? '' : ' El pago se confirma con facturación: DRAPE no muestra pagos en este contrato.'}</p>
          </section>
        </div>
      </div>
      {ev && <Evidencia pid={c.id} inicial={ev} onClose={() => setEv(null)} />}
    </div>
  )
}
