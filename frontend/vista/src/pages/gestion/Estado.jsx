// Estado.jsx — piezas pequeñas de las páginas para directivos (CHANGELOG 2026-10-25): punto, píldora de estado, marca ✓ y la barra de la vista.
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import api from '../../utils/api'
import { HAY_FLUJO, descargarCarga, fecha, pillDe, tamano } from './ux'

export function Dot({ color, chico }) {
  return <span className={`dot ${pillDe(color)}`} style={chico ? { width: 7, height: 7 } : undefined} />
}

// ✓ = listo (sin color: el verde es solo para lo cobrado); si no, un punto del color
export function Marca({ tono }) {
  if (tono === 'listo') return <span className="ok-m" aria-hidden="true">✓</span>
  return <span className={`dot ${pillDe(tono)}`} />
}

export function Pill({ estado }) {
  if (!estado) return null
  const c = pillDe(estado.color)
  return <span className={`pill ${c}`} data-estado={estado.k}><span className={`dot ${c}`} />{estado.t}</span>
}

export function LoTiene({ pelota }) {
  return <span className="lotiene">{pelota && pelota !== '—' ? `lo tiene ${pelota}` : ''}</span>
}

// (CHANGELOG 2026-10-25) «Descargar» en la fila de una fuente: con un archivo, lo baja; con varios (SCM: libretos, prestaciones, materiales), abre la lista de los
// archivos de su última carga con su fecha y tamaño. La descarga queda en Registros.
export function Descargar({ cargas = [] }) {
  const [abierto, setAbierto] = useState(false)
  const [msg, setMsg] = useState('')
  const xs = cargas.filter((c) => c.tiene_archivo)
  if (!xs.length) return <span className="muted" style={{ fontSize: '.8rem' }}>{cargas.length ? 'sin archivo en el servidor' : ''}</span>
  const bajar = async (c) => {
    setMsg('descargando…')
    try { await descargarCarga(c.id, c.archivo_nombre); setMsg('') } catch { setMsg('no se pudo descargar') }
  }
  if (xs.length === 1) return <span><button type="button" className="link" data-descargar={xs[0].id} onClick={() => bajar(xs[0])}>Descargar</button>{msg ? <span className="muted"> {msg}</span> : null}</span>
  return (
    <span style={{ display: 'inline-grid', gap: 4 }}>
      <button type="button" className="link" data-descargar-lista aria-expanded={abierto} onClick={() => setAbierto(!abierto)}>Descargar ({xs.length})</button>
      {abierto && <span style={{ display: 'grid', gap: 2 }}>{xs.map((c) => (
        <span key={c.id} style={{ fontSize: '.8rem' }}><button type="button" className="link" data-descargar={c.id} onClick={() => bajar(c)}>{c.archivo_nombre || c.archivo}</button>
          <span className="muted"> · {fecha(c.exporte)} · {tamano(c.archivo_tamano)}</span></span>))}</span>}
      {msg ? <span className="muted">{msg}</span> : null}
    </span>
  )
}

// Barra de la vista: marca, buscar un trabajo y los enlaces entre la vista de directivos y la de operador
export function BarraVista({ actual, flujo = HAY_FLUJO }) {
  const nav = useNavigate()
  const [q, setQ] = useState('')
  const [res, setRes] = useState(null)
  const buscar = async (e) => {
    e.preventDefault()
    const t = q.trim()
    if (!t) return
    try {
      const r = await api.get('/gestion/proyectos', { params: { q: t, limit: 8 } })
      const xs = r.data || []
      if (xs.length === 1) { setRes(null); setQ(''); nav(`/gestion/${xs[0].id}`) } else setRes(xs)
    } catch { setRes([]) }
  }
  return (
    <div className="top">
      <div className="top-in">
        <Link to="/gestion" className="marca"><b>SGO7</b><span><i className="gg" style={{ fontStyle: 'normal' }}>Gestión General · </i>Zona VII</span></Link>
        <form onSubmit={buscar} style={{ position: 'relative' }} data-buscar>
          <input value={q} onChange={(e) => { setQ(e.target.value); setRes(null) }} placeholder="Ir a una LCL o ticket" aria-label="Ir a una LCL o ticket"
            style={{ border: '1px solid var(--line-2)', borderRadius: 999, padding: '4px 12px', fontSize: '.88rem', width: 190, maxWidth: '46vw', background: 'var(--surface)' }} />
          {res && (
            <div style={{ position: 'absolute', zIndex: 30, top: '110%', left: 0, minWidth: 260, background: 'var(--surface)', border: '1px solid var(--line-2)', borderRadius: 8, boxShadow: 'var(--shadow)', padding: 6 }}>
              {res.length === 0 ? <div className="muted" style={{ fontSize: '.85rem', padding: 4 }}>Sin trabajos con ese código.</div> : res.map((x) => (
                <Link key={x.id} to={`/gestion/${x.id}`} onClick={() => { setRes(null); setQ('') }} style={{ display: 'block', padding: '4px 6px', fontSize: '.85rem', textDecoration: 'none' }}>
                  <span className="mono">{x.codigo || x.clave}</span> <span className="muted">· {x.clave}</span>
                </Link>))}
            </div>)}
        </form>
        <nav className="nav" aria-label="Vistas">
          <Link to="/gestion" aria-current={actual === 'zona' ? 'page' : undefined}>Zona</Link>
          {flujo && <Link to="/gestion/flujo" aria-current={actual === 'flujo' ? 'page' : undefined}>¿Cómo fluye?</Link>}
          <Link to="/gestion/lista" title="La lista completa con filtros, la franja y la operación (recalcular, zona, supervisor, confirmar enlaces)">Vista de operador</Link>
        </nav>
      </div>
    </div>
  )
}
