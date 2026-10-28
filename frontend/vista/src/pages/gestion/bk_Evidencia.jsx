// Evidencia.jsx — «¿De dónde sale?» en palabras llanas (CHANGELOG 2026-10-25).: arriba la cifra grande y el
// veredicto («las N filas suman exactamente esta cifra ✓»), la fuente en una línea llana y cómo se enlazó, en palabras; las pestañas en el orden del
// flujo (Mano de obra · Material de Enel · Otras pruebas); 5-7 columnas con nombre llano (las que valen igual en todas las filas se dicen una vez
// arriba). Lo técnico (fórmula, archivo, huella, Excel con =SUM) queda en «Para auditoría», plegado. Las filas y las sumas vienen de
// /api/gestion/proyectos/{id}/evidencia: aquí no se suma nada. «Es este» / «No es» siguen aquí, solo para quien puede decidir.
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import toast from 'react-hot-toast'
import api from '../../utils/api'
import { fecha, miles, num, pesos, planillaLlana, plural, sinCodigo } from './ux'
import Motivo from './Motivo'

const EV_FLUJO = [['pms', 'Presupuestado'], ['sgs_mo', 'Liquidado'], ['sonny', 'En planilla'], ['scm_mo', 'En libreto'], ['drape', 'Facturado'], ['deuda', 'En facturación']]
const EV_MAT = [['material', 'Por código'], ['sgs_mat', 'En SGS'], ['scm_mat', 'En SCM'], ['sap_mat', 'En SAP']]
const EV_OTRAS = [['sonny_etapa', 'Planillas por etapa'], ['scm_estado', 'Libretos por estado'], ['scm_lcl', 'La LCL en SCM']]
const EV_TITULO = { pms: ['Presupuestado', 'PMS'], sgs_mo: ['Liquidado', 'SGS'], sonny: ['En planilla', 'Sonny'], scm_mo: ['En libreto', 'SCM'], drape: ['Facturado', 'DRAPE'],
  material: ['Material de Enel, por código', 'SGS · SCM · SAP'], sgs_mat: ['Material en SGS', 'SGS'], scm_mat: ['Material en los libretos', 'SCM'], sap_mat: ['Material descargado por Enel', 'SAP'],
  sonny_etapa: ['Planillas por etapa', 'Sonny'], scm_estado: ['Libretos por estado', 'SCM'], scm_lcl: ['La LCL en SCM', 'SCM'],
  deuda: ['En facturación', 'Informe de deuda'] }      // (CHANGELOG 2026-10-25)
const SISTEMA = { pms: 'PMS', sgs_mo: 'SGS', sgs_mat: 'SGS', sonny: 'Sonny', sonny_etapa: 'Sonny', scm_mo: 'SCM', scm_mat: 'SCM', scm_estado: 'SCM', scm_lcl: 'SCM',
  drape: 'DRAPE', sap_mat: 'SAP', material: 'SAP', deuda: 'facturación de Dominion (informe de deuda)' }
const COL_LLANA = { 'Solicitud (QR)': 'Solicitud', 'MO (services)': 'Mano de obra', 'MAT (materials)': 'Material', 'Cómo se cruzó': 'Cómo se enlazó', 'Precio QP': 'Tarifa',
  Prestación: 'Actividad', 'QR (solicitud PMS)': 'Solicitud', 'QR (PMS)': 'Solicitud', QR: 'Solicitud', 'Planilla Sonny': 'Planilla', 'Consumido (liquidado)': 'Consumido',
  'Código Enel': 'Código', 'Material (SGS)': 'Material', 'Material (SAP)': 'Material', 'MO real': 'Mano de obra', 'MAT real': 'Material', 'MO plan': 'MO planeada',
  'Padre en SGS': 'Proyecto en SGS', 'Trabajo (orden SAP)': 'Orden de SAP', Posado: 'Instalado', 'SCM posado': 'SCM instalado', 'Valor SCM posado': 'Valor SCM instalado',
  'Significado (glosario)': 'Estado', 'La pelota la tiene': 'Lo tiene', 'MO que suma': 'MO que cuenta', 'Días (hábiles en nodos)': 'Días hábiles', MO: 'Mano de obra',
  'Libreto en SCM (confianza)': 'Libreto en SCM', 'Costo SGS': 'Costo en SGS', 'Registro Enel': 'Registrada por Enel', '¿Suma en la caja?': '¿Cuenta en la cifra?',
  'En el estado desde': 'Desde', 'Usuarios': 'Liquidó' }
const EV_OCULTA = { pms: ['También nombra', 'Confianza'], drape: ['DRAPE ID'], scm_estado: ['Estado', 'Nombre en pantalla'], sap_mat: ['Posición'], sgs_mo: ['Quién liquidó'],
  'alerta:rechazo_abierto': ['Texto de Enel'] }     // (CHANGELOG 2026-10-25) el texto va completo debajo de la tabla
const COMUN_OCULTO = new Set(['Clase', 'Posición', 'Origen', 'Significado', 'De dónde sale la fecha', 'Qué cambió', 'En el estado desde (exporte)', 'Vencimiento de', 'Confianza'])
const CONF_FRASE = { alta: 'con enlace exacto', media: 'enlazados por monto (falta confirmar)', ambigua: 'con varias planillas posibles (falta confirmar)',
  confirmada: 'confirmados por una persona', 'sin planilla': 'sin planilla enlazada' }
const CONF_CORTO = { alta: 'enlace exacto', media: 'por monto, confirmar', ambigua: 'varias planillas posibles, confirmar', confirmada: 'confirmado por una persona', 'sin planilla': 'sin planilla enlazada' }
const LCL_ESTADO = { RIC_IMP: 'en ejecución', POS_ENEL: 'Enel revisa documentos de seguridad', CONCLUSA: 'concluida · no es pago', INV_IMP: 'asignada, faltan documentos de seguridad' }
const pmsEstado = (e) => { const t = String(e || '').replace(/^[\d.]+-/, '').replace(/ejecucion/i, 'ejecución'); return /^RG Gestionado/i.test(t) ? 'levantamiento gestionado' : t }
const sumaLlana = (s) => String(s || '').replace('MO de los vivos', 'MO de los libretos vigentes').replace(/\bMAT\b/g, 'Material').replace(/ real\b/g, '')
  .replace(/ plan \(sin planes atípicos\)/g, ' planeada').replace('Material planeada', 'Material planeado').replace(/\(= caja (\w+)\)/g, '(= la cifra de $1)')
  .replace('(con ANN y RIF)', '(con devueltos y anulados)').replace(/\bvivos\b/g, 'vigentes').replace(/posado/g, 'instalado').replace(/^MO$/, 'Mano de obra')

function celda(col, v) {
  if (v === null || v === undefined || v === '') return ''
  if (col.tipo === 'pesos') return pesos(v)
  if (col.tipo === 'num') return num(v)
  if (col.tipo === 'fecha') return fecha(v)
  if (typeof v === 'boolean') return v ? 'sí' : 'no'
  return String(v)
}
function valorLlano(k, col, v) {
  if (typeof v !== 'string') return v
  if (v.startsWith('"')) v = v.replace(/^"|"$/g, '').replace(/""/g, '"')
  if (col.t === 'Estado' && (k === 'sonny' || k === 'sonny_etapa')) return planillaLlana(v)
  if (col.t === 'Estado' && k === 'pms') return pmsEstado(v)
  if (col.t === 'Estado de la LCL' || (col.t === 'Estado' && k === 'scm_lcl')) return LCL_ESTADO[v] || v
  if (col.t === 'Nodo') return v.split('/').map((s) => s.replace(/^NODO\s*/i, '')).join(' · ')
  if (col.t === 'Cómo se cruzó') return /subprojects/.test(v) ? 'la solicitud nombra el ticket' : /«lcl»/.test(v) ? 'la solicitud nombra la LCL' : v
  if (col.t === 'Libreto en SCM (confianza)') return v.replace(/ \(alta\)/g, '').replace(/ \(media\)/g, ' (por monto, falta confirmar)').replace(/ \(ambigua:[^)]*\)/g, ' (varias planillas posibles)')
  return sinCodigo(v)
}
function fuenteLlana(k, f) {
  if (!f) return '—'
  if (f.vivo) return `Leído en vivo de SGS el ${fecha(f.consultado)} (solo lectura)`
  const c = (f.cargas || [])[0]
  if (!c) return f.sin_registro || 'sin carga'
  const sis = SISTEMA[k] || f.sistema || ''
  return `Archivo exportado de ${sis} el ${fecha(c.exporte)} · cargado el ${fecha(c.cargado)} · ${miles(c.filas || 0)} filas en el archivo`
    + ((f.cargas || []).length > 1 ? ` (y ${plural(f.cargas.length - 1, 'archivo más', 'archivos más')})` : '')
}

// Las columnas que se ven: todas las de pesos (hasta 3) y las demás que cambian de fila a fila, hasta 7. Las que valen lo mismo en todas las filas
// se dicen una vez arriba de la tabla. Las de confianza no van como columna: van como «Enlace» en palabras.
function columnasLlanas(k, t) {
  const filas = (t.filas || []).filter((f) => !f._cab)
  const todas = (t.columnas || []).filter((c) => c.tipo !== 'conf' && !(EV_OCULTA[k] || []).includes(c.t))
  const constante = (c) => filas.length > 1 && filas[0][c.k] !== null && filas[0][c.k] !== undefined && filas[0][c.k] !== '' && filas.every((f) => JSON.stringify(f[c.k]) === JSON.stringify(filas[0][c.k]))
  const conDato = (c) => filas.some((f) => f[c.k] !== null && f[c.k] !== undefined && f[c.k] !== '')
  const plata = todas.filter((c) => c.tipo === 'pesos' && conDato(c)).slice(0, 3)
  const otras = todas.filter((c) => c.tipo !== 'pesos' && conDato(c) && !constante(c)).slice(0, Math.max(3, 7 - plata.length))
  const cols = todas.filter((c) => plata.includes(c) || otras.includes(c))
  const comunes = todas.filter((c) => constante(c) && !COMUN_OCULTO.has(c.t)).slice(0, 5).map((c) => ({ ...c, v: filas[0][c.k] }))
  return { filas, cols, comunes, conConf: new Set(filas.map((f) => f.confianza || '')).size > 1 }
}

function Decidir({ acciones, pid, puede, onDecidir }) {
  if (!acciones?.length) return null
  return (
    <div style={{ display: 'grid', gap: 4 }}>
      {acciones.map((a) => (
        <div key={`${a.libreto}-${a.id_planilla}`} className="decidir" style={{ alignItems: 'center' }}>
          <span className={a.del_caso ? '' : 'muted'}>{a.etiqueta}</span>
          {a.decision ? (
            <>
              <span className="muted">{a.decision.decision === 'si' ? 'confirmado' : 'descartado'} por una persona el {fecha(a.decision.fecha)}</span>
              {puede && <button type="button" data-accion="deshacer" onClick={() => onDecidir({ deshacer: a.decision.id })}>Deshacer</button>}
            </>
          ) : puede ? (
            <>
              <button type="button" className="si" data-accion="es-este" onClick={() => onDecidir({ proyecto_id: pid, lcl: a.lcl, libreto: a.libreto, id_planilla: a.id_planilla, decision: 'si' })}>Es este</button>
              <button type="button" className="no" data-accion="no-es" onClick={() => onDecidir({ proyecto_id: pid, lcl: a.lcl, libreto: a.libreto, id_planilla: a.id_planilla, decision: 'no' })}>No es</button>
            </>
          ) : <span className="muted">(falta que una persona lo confirme)</span>}
        </div>))}
    </div>
  )
}

export default function Evidencia({ pid, inicial, onClose }) {
  const qc = useQueryClient()
  const { data: ev, isLoading, isError } = useQuery({ queryKey: ['gestion-evidencia', pid], queryFn: () => api.get(`/gestion/proyectos/${pid}/evidencia`).then((r) => r.data), enabled: !!pid })
  const [k, setK] = useState(inicial)
  const [todas, setTodas] = useState(false)
  useEffect(() => { setK(inicial) }, [inicial])
  useEffect(() => { setTodas(false) }, [k])
  useEffect(() => {
    const esc = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', esc)
    return () => document.removeEventListener('keydown', esc)
  }, [onClose])
  const decidir = useMutation({
    mutationFn: (b) => (b.deshacer ? api.delete(`/gestion/decisiones/${b.deshacer}`) : api.post('/gestion/decisiones', b)),
    onSuccess: (r, b) => {
      ['gestion-evidencia', 'gestion-proyecto', 'gestion-proyectos', 'gestion-fuentes', 'gestion-cadena', 'gestion-zona'].forEach((q) => qc.invalidateQueries({ queryKey: [q] }))
      toast.success(b.deshacer ? 'Decisión deshecha · recalculado' : `${b.decision === 'si' ? 'Confirmado' : 'Descartado'} · ${r.data.casos_recalculados} trabajo(s) recalculado(s)`)
    },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo guardar la decisión'),
  })
  const [bajando, setBajando] = useState(false)
  const excel = async () => {
    setBajando(true)
    try {
      const r = await api.get(`/gestion/proyectos/${pid}/evidencia/excel`, { responseType: 'blob' })
      const nombre = (r.headers?.['content-disposition'] || '').match(/filename="([^"]+)"/)?.[1] || `evidencia_${pid}.xlsx`
      const url = URL.createObjectURL(r.data)
      const a = document.createElement('a'); a.href = url; a.download = nombre; document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 2000)
      toast.success('Excel de evidencia descargado (queda en Registros)')
    } catch { toast.error('No se pudo generar el Excel') } finally { setBajando(false) }
  }
  const T = ev?.tarjetas || {}
  const alertas = (ev?.orden || []).filter((x) => x.startsWith('alerta:')).map((x) => [x, (T[x]?.titulo || x).replace(/^Alerta · /, '')])
  const kk = T[k] ? k : (ev?.orden || [])[0]
  const t = T[kk]
  const vista = useMemo(() => (t ? columnasLlanas(kk, t) : null), [t, kk])
  const puede = !!ev?.puede_decidir
  const boton = ([x, nom]) => (T[x] ? (
    <button key={x} type="button" data-ev-tab={x} aria-pressed={x === kk} onClick={() => setK(x)}>
      <span className={`dot ${T[x].ok === false ? 'rojo' : T[x].ok ? 'verde' : 'gris'}`} style={{ width: 7, height: 7 }} />{nom}
    </button>) : null)
  const [titulo, sis] = EV_TITULO[kk] || [(t?.titulo || '').replace(/^Alerta · /, 'Aviso: '), t?.sistema || '']
  const s0 = (t?.sumas || [])[0]
  const n = t?.n_filas || 0
  const veredicto = t?.ok === true ? (n === 1 ? '✓ La única fila da exactamente esta cifra.' : `✓ Las ${miles(n)} filas suman exactamente esta cifra.`)
    : t?.ok === false ? '✗ Las filas no dan esta cifra: hay que revisarla.' : 'Sin cifra que comparar en este trabajo.'
  const conf = Object.entries((t?.filas || []).reduce((a, f) => { if (f._cab || (kk !== 'scm_mo' && f.confianza)) { const c = f.confianza || 'sin planilla'; a[c] = (a[c] || 0) + 1 } return a }, {}))
  const porConfirmar = (t?.filas || []).filter((f) => f.acciones?.length)
  const filasVer = vista ? (todas ? vista.filas : vista.filas.slice(0, 25)) : []
  const fuera = [...(t?.pendientes || []).map((x) => ({ ...x, por: 'pendiente de confirmar: no suma' })), ...(t?.descartadas || []).map((x) => ({ ...x, por: `no es la planilla ${x.planilla}` })),
    ...(t?.no_suman || []).map((x) => ({ ...x, por: sinCodigo(x.motivo || 'no suma') }))]
  return (
    <>
      <div className="gux-velo" onClick={onClose} />
      <aside className="gux hoja" role="dialog" aria-modal="true" aria-label="¿De dónde sale?" data-evidencia-llana>
        <button type="button" className="cerrar" aria-label="Cerrar" onClick={onClose}>×</button>
        <div className="eyebrow">¿De dónde sale? · {ev?.caso?.codigo || ''}</div>
        {isLoading ? <p className="muted">Cargando…</p> : isError ? <p className="aviso">No se pudo leer la evidencia de este trabajo.</p> : !t ? (
          <p className="muted">Este trabajo aún no tiene sus cifras calculadas.</p>
        ) : (
          <>
            <h2 style={{ marginTop: 4, fontSize: '1.3rem' }}>{titulo} {sis && <span className="etq" style={{ verticalAlign: 'middle' }}>{sis}</span>}</h2>
            <div className="pasos-ev" data-pestanas>
              <div className="grupo"><span>Mano de obra</span>{EV_FLUJO.map(boton)}</div>
              <div className="grupo"><span>Material de Enel</span>{EV_MAT.map(boton)}</div>
              <div className="grupo"><span>Otras pruebas</span>{EV_OTRAS.map(boton)}</div>
              {alertas.length > 0 && <div className="grupo"><span>Avisos</span>{alertas.map(boton)}</div>}
            </div>
            <div className={`veredicto ${t.ok === false ? 'no' : t.ok ? '' : 'nada'}`} data-veredicto>
              {s0 && <div className="eyebrow">{sumaLlana(s0.etiqueta)}</div>}
              {s0 && <div className="grande num">{s0.unidad === 'n' ? num(s0.cifra ?? s0.suma) : pesos(s0.cifra ?? s0.suma)}</div>}
              <div>{veredicto}</div>
              {(t.sumas || []).slice(1, 4).filter((s) => s.cifra !== null && s.cifra !== undefined).map((s) => (
                <div key={s.k} className="muted num" style={{ fontSize: '.85rem' }}>{sumaLlana(s.etiqueta)}: {s.unidad === 'n' ? num(s.suma) : pesos(s.suma)} {s.ok ? '= cifra ✓' : s.ok === false ? '≠ cifra' : ''}</div>))}
              {t.gris && <div className="muted" style={{ fontSize: '.85rem' }}>Liquidado en SGS es referencia: se muestra, no se compara con las demás cifras.</div>}
            </div>
            <div className="fuente-ll" data-fuente-llana>
              <div><b>Fuente:</b> {fuenteLlana(kk, t.fuente)}.</div>
              {conf.length > 0 && <div><b>Cómo se enlazó:</b> {conf.map(([a, c]) => `${miles(c)} ${CONF_FRASE[a] || a}`).join(' · ')}.</div>}
              {t.provisional && <div className="muted">Material a precio de Enel donde SAP lo trae; el resto, a costo de SGS (provisional).</div>}
            </div>
            <h3 style={{ margin: '6px 0 8px' }}>{n === 1 ? 'La fila' : `Las filas (${miles(n)})`}</h3>
            {vista.comunes.length > 0 && <p className="nota" style={{ margin: '0 0 8px' }}>{n === 1 ? 'Además' : 'En todas'}: {vista.comunes.map((x) => `${COL_LLANA[x.t] || x.t} ${valorLlano(kk, x, celda(x, x.v))}`).join(' · ')}.</p>}
            <div className="scroll-x"><table className="filas num" data-filas>
              <thead><tr>{vista.cols.map((c) => <th key={c.k} className={['pesos', 'num'].includes(c.tipo) ? 'r' : ''}>{COL_LLANA[c.t] || c.t}</th>)}{vista.conConf && <th>Enlace</th>}</tr></thead>
              <tbody>
                {filasVer.map((f, i) => (
                  <tr key={i}>{vista.cols.map((c) => <td key={c.k} className={['pesos', 'num'].includes(c.tipo) ? 'r' : ''} data-l={COL_LLANA[c.t] || c.t}>{valorLlano(kk, c, celda(c, f[c.k]))}</td>)}
                    {vista.conConf && <td data-l="Enlace">{CONF_CORTO[f.confianza] || f.confianza || ''}</td>}</tr>))}
                {filasVer.length === 0 && <tr><td className="muted">Sin filas: el sistema no trae nada para este trabajo.</td></tr>}
              </tbody>
            </table></div>
            {vista.filas.length > 25 && <p className="nota"><button type="button" className="link" onClick={() => setTodas(!todas)}>{todas ? 'Ver solo las primeras 25' : `Ver todas (${miles(vista.filas.length)})`}</button></p>}
            {(t.motivos || []).length > 0 && (
              <div className="motivos" data-motivos>
                <h3 style={{ margin: '10px 0 6px' }}>El motivo, como lo escribió Enel en Sonny</h3>
                {t.motivos.map((m) => <div key={m.planilla}><div className="eyebrow">Planilla <span className="mono">{m.planilla}</span> · {pesos(m.mo)} de mano de obra</div><Motivo m={m} planilla={m.planilla} corte={1000} /></div>)}
              </div>)}
            {(t.previos || []).length > 0 && (
              <details className="aud" data-rechazos-previos>
                <summary>Rechazos anteriores de estas planillas ({t.n_previos || t.previos.length}): ya respondidos</summary>
                <ul style={{ margin: '8px 0 0', paddingLeft: 18, display: 'grid', gap: 6 }}>{t.previos.map((x, i) => (
                  <li key={i}><span className="mono">{x.planilla}</span> · {x.estado} el {fecha(x.fecha)} · <b>{x.motivo}</b>{x.que_hacer ? ` — ${x.que_hacer}` : ''}<div className="mt">{x.texto}</div></li>))}
                </ul>
              </details>)}
            {porConfirmar.length > 0 && (
              <details className="aud" open={puede} data-por-confirmar>
                <summary>{plural(porConfirmar.length, 'libreto espera', 'libretos esperan')} que una persona confirme su planilla</summary>
                <div style={{ display: 'grid', gap: 8, marginTop: 8 }}>{porConfirmar.map((f) => (
                  <div key={f.libreto}><span className="mono">{f.libreto}</span> · {CONF_CORTO[f.confianza] || f.confianza} · <Decidir acciones={f.acciones} pid={pid} puede={puede} onDecidir={(b) => decidir.mutate(b)} /></div>))}
                </div>
              </details>)}
            {fuera.length > 0 && (
              <details className="aud" data-fuera>
                <summary>Lo que no entra en la cifra ({fuera.length})</summary>
                <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>{fuera.map((x, i) => (
                  <li key={i}><span className="mono">{x.libreto || x.codigo || ''}</span> · {x.estado ? `${sinCodigo(x.estado)} · ` : ''}{x.mo !== undefined ? `${pesos(x.mo)} · ` : ''}{x.por}
                    {x.acciones?.length ? <Decidir acciones={x.acciones} pid={pid} puede={puede} onDecidir={(b) => decidir.mutate(b)} /> : null}</li>))}
                </ul>
              </details>)}
            <details className="aud" data-auditoria>
              <summary>Para auditoría</summary>
              <p>{t.formula}</p>
              <ul>{(t.fuente?.cargas || []).map((c) => <li key={c.id}><span className="mono">{c.archivo}</span> · exporte {fecha(c.exporte)} · cargado {fecha(c.cargado)} · {miles(c.filas || 0)} filas · huella <span className="mono">{String(c.sha256 || '').slice(0, 12)}…</span></li>)}
                {t.fuente?.vivo && <li>Lectura en vivo de SGS (solo lectura) por el Centro MO.</li>}
                {t.fuente?.sin_registro && <li>{t.fuente.sin_registro}</li>}</ul>
              {(t.sumas || []).length > 0 && <ul>{t.sumas.map((s) => <li key={s.k}>{sumaLlana(s.etiqueta)}: suma de las filas {s.unidad === 'n' ? num(s.suma) : pesos(s.suma)} · cifra {s.cifra === null || s.cifra === undefined ? '—' : s.unidad === 'n' ? num(s.cifra) : pesos(s.cifra)} · {s.ok === true ? 'cuadra al peso' : s.ok === false ? `no cuadra (diferencia ${pesos(s.diferencia)})` : 'sin cifra'}</li>)}</ul>}
              {(t.notas || []).length > 0 && <ul>{t.notas.map((x, i) => <li key={i}>{sinCodigo(x)}</li>)}</ul>}
              {(t.revisar || []).length > 0 && <p className="nota">{plural(t.revisar.length, 'código de SCM que SGS no reconoce en esta LCL', 'códigos de SCM que SGS no reconoce en esta LCL')} (no suman ni crean aviso).</p>}
              {(t.menores || []).length > 0 && <p className="nota">Diferencias menores de material: {pesos(t.menores_suma)} en todo el trabajo (menos de {pesos(t.menores_minimo)}: no son aviso).</p>}
              <p><button type="button" className="link" disabled={bajando} onClick={excel} data-accion="excel">{bajando ? 'Generando…' : 'Excel de evidencia (una hoja por cifra, con =SUM)'}</button></p>
            </details>
          </>)}
      </aside>
    </>
  )
}
