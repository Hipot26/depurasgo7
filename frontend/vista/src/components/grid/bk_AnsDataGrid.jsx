import { useMemo, useCallback, useState, useRef, useEffect, useLayoutEffect, forwardRef, useImperativeHandle } from 'react'
import { AgGridReact } from 'ag-grid-react'
import { Search, X, Maximize2, Minimize2, HelpCircle, MessageCircle, Copy, Check, UserPlus,
  Sparkles, Crosshair, Sheet, Shrink, Filter as FilterIcon, CalendarDays, FileSearch, Mail, Bot } from 'lucide-react'
import api from '../../utils/api'
import { useAuthStore } from '../../store/authStore'
import ChatConsulta from '../ans/ChatConsulta'
import EvidenciaBox from '../ans/EvidenciaBox'
import JustificacionObs from '../ans/JustificacionObs'
import ExcelSetFilter from './ExcelSetFilter'

const CAMPOS_ANULADO = new Set(['excluible_anulado', 'obs_anulado'])   // 3.2 B → soporte = correo

// Celdas que alguien debe diligenciar (el backend marca en fila._pend cuál rol la debe).
// Cada exclusión va con su OBSERVACIÓN: decidir sin justificar no sirve, así que la razón
// también parpadea en cuanto se toma la decisión.
// EN ORDEN DE COLUMNA (izq→der): así el salto «siguiente pendiente» recorre la fila como se lee.
const CAMPOS_PEND_ORDEN = [
  'nombre_responsable', 'sigla_elaborador',  // G · H  — el STM los deja vacíos a veces
  'fecha_debio_validar', 'fecha_envio_validar', 'excluible_tiempos', 'obs_tiempos',  // 3.4 W·Y·AA·AB
  'excluible', 'obs',                        // 3.2 A  AL → AM
  'excluible_anulado', 'obs_anulado',        // 3.2 B  AO → AP
  'nombre_cumplimentador',                   // 3.3    AQ (quién cumplimentó la ejecutada)
  'excluible2', 'obs3',                      // 3.3    AT → AU
  'nodos_planeados', 'nodos_ejecutados',     // 3.2 C  AV · AW
]
const CAMPOS_PEND = new Set(CAMPOS_PEND_ORDEN)

const vacia = (v) => v === null || v === undefined || String(v).trim() === ''

// Envuelve el cellClass de esas columnas para que PARPADEEN mientras estén vacías,
// con el color del rol al que le tocan — y solo si ese rol es visible para quien mira.
// `filaMia(data)`: a una cuenta elaboradora (una de las 4 de programación+cumplimentación)
// solo le parpadean SUS filas — las que la sigla de la col H le atribuye (`_elab`) — más
// las que aún no tienen dueño; para el resto de roles siempre es true.
const conParpadeo = (cols, rolesVisibles, filaMia, miZona) => cols.map((c) => {
  if (c.children) return { ...c, children: conParpadeo(c.children, rolesVisibles, filaMia, miZona) }
  const f = c.colId || c.field
  if (!f || !CAMPOS_PEND.has(f)) return c
  const base = typeof c.cellClass === 'string' ? c.cellClass : null
  return {
    ...c,
    cellClass: (p) => {
      const rol = p.data?._pend?.[f]
      const out = base ? [base] : []
      // Parpadea si el backend dice que la debe alguien Y sigue vacía. Mirar el valor actual
      // (no solo _pend) hace que deje de parpadear EN EL ACTO al escribirla, sin esperar al
      // servidor ni recargar. Mismo criterio que usa el salto «Siguiente pendiente».
      // `miZona`: al ingeniero de zona las celdas 'ans' solo le parpadean en filas de SU zona.
      if (rol && vacia(p.value) && rolesVisibles.has(rol)
          && (rol !== 'elaborador' || filaMia(p.data))
          && (rol !== 'ans' || !miZona || (p.data?.zona || '').toUpperCase() === miZona))
        out.push('pend-blink', `pend-${rol}`)
      return out
    },
  }
})

// Celdas de nodos AJUSTADAS SOLAS por el sistema (reglas 3.2B/3.2C): amarillo fuerte =
// automatización aplicada (anulada excluida → AV=0 / cerrada → AW=AV); rojo = la condición
// que la justificaba ya no se cumple (p. ej. quitaron la exclusión) y hay que revisar.
// Solo colorea en el sistema — el exporte lleva el valor sin color. Clic = ver la regla.
const NODOS_AUTO_CAMPOS = new Set(['nodos_planeados', 'nodos_ejecutados'])
const conAutoNodos = (cols) => cols.map((c) => {
  if (c.children) return { ...c, children: conAutoNodos(c.children) }
  const f = c.colId || c.field
  if (!f || !NODOS_AUTO_CAMPOS.has(f)) return c
  const prev = c.cellClass
  return {
    ...c,
    cellClass: (p) => {
      const base = typeof prev === 'function' ? prev(p) : prev ? [prev] : []
      const out = Array.isArray(base) ? [...base] : [base]
      const m = p.data?.nodos_auto
      if (m?.campo === f) out.push(m.estado === 'invalida' ? 'nodo-auto-rojo' : 'nodo-auto-ok')
      return out
    },
    tooltipValueGetter: (p) => {
      const m = p.data?.nodos_auto
      if (m?.campo !== f) return null
      return m.estado === 'invalida'
        ? 'REVISAR: la condición que justificaba este ajuste automático ya no se cumple (clic para el detalle)'
        : 'Ajustado solo por el sistema (clic para ver la regla)'
    },
  }
})

// Estilo del badge de origen en el popup de justificación
const ORIGEN_META = {
  fuente:   { label: 'Del STM',    cls: 'bg-gray-200 text-gray-700' },
  derivado: { label: 'Derivado',   cls: 'bg-indigo-100 text-indigo-700' },
  auto:     { label: 'Automático', cls: 'bg-blue-100 text-blue-700' },
  presunta: { label: 'Presunta — no viene del STM', cls: 'bg-orange-100 text-orange-700' },
  calculo:  { label: 'Cálculo',    cls: 'bg-green-100 text-green-700' },
  manual:   { label: 'Manual',     cls: 'bg-amber-100 text-amber-700' },
}

// Coloca un flotante junto a la celda SIN que se salga de la pantalla: lo abre debajo si cabe,
// arriba si no cabe debajo (típico de las últimas filas de la grilla) y, si no cabe en ninguno
// de los dos, lo pega al borde visible. Mide el popup ya renderizado, así que funciona aunque
// su alto cambie al llegar los datos.
function useFlotante(ancla, activo = true) {
  const ref = useRef(null)
  const [pos, setPos] = useState(null)

  useLayoutEffect(() => {
    const el = ref.current
    if (!el || !ancla || !activo) { setPos(null); return }
    const colocar = () => {
      const { width: w, height: h } = el.getBoundingClientRect()
      const vw = window.innerWidth
      const vh = window.innerHeight
      const m = 10
      const holguraAbajo = vh - ancla.bottom - m
      const holguraArriba = ancla.top - m
      let top
      if (h <= holguraAbajo) top = ancla.bottom + m           // debajo de la celda
      else if (h <= holguraArriba) top = ancla.top - m - h    // encima (celda cerca del borde inferior)
      else top = Math.max(m, vh - m - h)                      // no cabe: al borde, siempre legible
      setPos({ left: Math.min(Math.max(m, ancla.left), Math.max(m, vw - w - m)), top })
    }
    colocar()
    // El alto cambia cuando llegan los datos ("Pensando…" → contenido): recolocar al vuelo.
    const ro = new ResizeObserver(colocar)
    ro.observe(el)
    window.addEventListener('resize', colocar)
    return () => { ro.disconnect(); window.removeEventListener('resize', colocar) }
  }, [ancla, activo])

  // Hasta que se mide, se pinta fuera de vista (evita el salto visual).
  const style = pos || { left: -9999, top: -9999 }
  return [ref, style]
}

// Una persona dentro del popup "¿A quién pregunto?": quién es, por qué él, y el botón para
// escribirle con el mensaje ya redactado. Si no tiene número, se agrega AQUÍ mismo y queda
// guardado para siempre — el directorio se construye con el uso, no con una importación masiva.
function ContactoSug({ c, apiBase, onGuardado }) {
  const [tel, setTel] = useState('')
  const [msg, setMsg] = useState(c.mensaje || '')
  const [verMsg, setVerMsg] = useState(false)
  const [guardando, setGuardando] = useState(false)
  const [err, setErr] = useState(null)
  const [copiado, setCopiado] = useState(false)

  useEffect(() => { setMsg(c.mensaje || '') }, [c.mensaje])

  const sinAsignar = (c.quien || '').startsWith('—')

  const escribir = () => {
    window.open(`https://wa.me/${c.telefono}?text=${encodeURIComponent(msg)}`, '_blank', 'noopener')
  }
  const copiar = async () => {
    try { await navigator.clipboard.writeText(msg); setCopiado(true); setTimeout(() => setCopiado(false), 1500) } catch { /* sin portapapeles */ }
  }
  const guardar = async () => {
    setGuardando(true); setErr(null)
    try {
      await api.post('/contactos', { nombre: c.quien, telefono: tel, rol: c.rol_dir, zona: c.zona }, { baseURL: apiBase })
      onGuardado()
    } catch (e) {
      setErr(e?.response?.data?.detail || 'No se pudo guardar')
    } finally { setGuardando(false) }
  }

  return (
    <div className="border border-gray-200 rounded p-2 bg-gray-50">
      <p className="font-semibold text-gray-800">{c.quien}
        <span className="ml-1 font-normal text-[10px] text-teal-700 bg-teal-100 px-1 rounded">{c.rol}</span>
      </p>
      <p className="text-gray-600 mt-0.5 leading-relaxed"><b>Por qué él:</b> {c.porque}</p>

      {sinAsignar ? (
        c.canal && c.canal !== '—' && <p className="text-gray-400 mt-0.5">Canal: {c.canal}</p>
      ) : c.telefono ? (
        <>
          <div className="flex items-center gap-1.5 mt-1.5">
            <button onClick={escribir} disabled={!msg}
              className="flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-40">
              <MessageCircle size={12} /> WhatsApp
            </button>
            <button onClick={copiar} disabled={!msg}
              className="flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-gray-300 bg-white text-gray-600 hover:bg-gray-50 disabled:opacity-40">
              {copiado ? <Check size={12} /> : <Copy size={12} />} {copiado ? 'Copiado' : 'Copiar'}
            </button>
            <button onClick={() => setVerMsg((v) => !v)} className="text-[11px] text-gray-500 underline hover:text-gray-700">
              {verMsg ? 'ocultar' : 'ver / editar'}
            </button>
          </div>
          {verMsg && (
            <textarea value={msg} onChange={(e) => setMsg(e.target.value)} rows={7}
              className="w-full mt-1.5 text-[11px] border border-gray-300 rounded p-1.5 font-mono leading-relaxed" />
          )}
          {!verMsg && <p className="text-[10px] text-gray-400 mt-1">+{c.telefono} · el mensaje va redactado; revísalo antes de enviar</p>}
        </>
      ) : (
        <div className="mt-1.5">
          <p className="text-[10px] text-amber-700 mb-1">No tiene número guardado. Agrégalo y queda para siempre.</p>
          <div className="flex items-center gap-1">
            <input value={tel} onChange={(e) => setTel(e.target.value)} placeholder="300 123 4567"
              onKeyDown={(e) => { if (e.key === 'Enter' && tel) guardar() }}
              className="flex-1 min-w-0 text-[11px] border border-gray-300 rounded px-1.5 py-1" />
            <button onClick={guardar} disabled={!tel || guardando}
              className="flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded bg-teal-700 text-white hover:bg-teal-800 disabled:opacity-40 shrink-0">
              <UserPlus size={12} /> {guardando ? '…' : 'Guardar'}
            </button>
          </div>
          {err && <p className="text-[10px] text-red-600 mt-1">{err}</p>}
        </div>
      )}
    </div>
  )
}

// Campos de exclusión que admiten la consulta guiada con IA (los 3 pares decisión/razón).
const CAMPOS_CONSULTA = new Set([
  'excluible', 'obs', 'excluible_anulado', 'obs_anulado', 'excluible_tiempos', 'obs_tiempos',
])
// Observaciones con «Ver justificación» (requisito 2026-09-03): las escribe Auditoría IA,
// Correos ANS o el chat, así que la celda debe poder mostrar de dónde salió su texto y qué lo sustenta.
const CAMPOS_OBS_JUST = new Set(['obs', 'obs_anulado', 'obs_tiempos'])

// X «Debió enviar» = W − 1 día, a partir de "DD/MM/YYYY". Réplica de la fórmula del Excel oficial del ANS
// =SI.ERROR(W-1;"N/A"): si W es N/A (emergenciales) → N/A, no en blanco.
function menos1(s) {
  const raw = String(s || '').trim()
  if (/^n\.?\/?a$/i.test(raw)) return 'N/A'          // W = N/A → X = N/A
  const m = raw.match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/)
  if (!m) return ''                                   // W vacía → X vacía (aún sin diligenciar)
  const d = new Date(+m[3], +m[2] - 1, +m[1] - 1)
  return `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}/${d.getFullYear()}`
}

// ── Editor de FECHA con calendario (pedido de las programadoras: nada de digitar fechas
// a mano). Guarda en DD/MM/YYYY, el formato de la casa (fórmula X=W−1, export, backend).
// Botones: Hoy · N/A (emergenciales) · Borrar. Si no se toca nada, conserva el valor tal cual.
const fechaAISO = (s) => {
  const m = String(s || '').match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/)
  if (m) return `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`
  const iso = String(s || '').match(/^(\d{4})-(\d{2})-(\d{2})/)
  return iso ? `${iso[1]}-${iso[2]}-${iso[3]}` : ''
}
const isoAFecha = (iso) => {
  const m = String(iso || '').match(/^(\d{4})-(\d{2})-(\d{2})/)
  return m ? `${m[3]}/${m[2]}/${m[1]}` : ''
}
// Auto-rellena las barras a medida que se teclea: "15072026" → "15/07/2026" (solo dígitos, máx 8).
const mascaraFecha = (raw) => {
  const d = String(raw).replace(/\D/g, '').slice(0, 8)
  if (d.length <= 2) return d
  if (d.length <= 4) return `${d.slice(0, 2)}/${d.slice(2)}`
  return `${d.slice(0, 2)}/${d.slice(2, 4)}/${d.slice(4)}`
}
const FECHA_COMPLETA = /^\d{2}\/\d{2}\/\d{4}$/
// Texto inicial a mostrar en el editor: la fecha tal cual, "N/A", o vacío.
const textoInicialFecha = (v) => {
  const o = String(v ?? '').trim()
  if (/^\d{1,2}\/\d{1,2}\/\d{4}$/.test(o)) return o
  if (o.toUpperCase() === 'N/A') return 'N/A'
  return ''
}
// Editor de fecha con DOS formas de llenar (pedido de las programadoras/digitadora): se puede
// ESCRIBIR a mano —con auto-relleno de las barras: "15072026" → "15/07/2026"— o abrir el
// CALENDARIO con el botón. Guarda en DD/MM/YYYY (formato de la casa). Botones: Hoy · N/A · Borrar.
const FechaEditor = forwardRef((props, ref) => {
  // CONTRATO REAL de ag-grid-react v32 (reactiveCustomComponents, el default): el grid NO
  // lee el getValue de este componente — envuelve el editor en un proxy y al confirmar
  // commitea props.onValueChange(último valor). Si no se llama onValueChange, el proxy
  // conserva el valor VIEJO de la celda: así se borraban fechas solas (el commit de cierre
  // pisaba lo guardado) y luego dejó de guardar del todo. Regla de esta casa: cada vez que
  // cambie "lo que debería quedar en la celda", se avisa por onValueChange.
  const valorInicial = useRef(props.value).current   // congelado: onValueChange re-renderiza con props.value nuevo
  const original = valorInicial ?? ''
  const [texto, setTexto] = useState(() => textoInicialFecha(original))
  const textoRef = useRef(texto)                // espejo síncrono para getValue
  const committedRef = useRef(undefined)        // valor final ya escrito (undefined = aún nada)
  const inputRef = useRef(null)
  const dateRef = useRef(null)                  // input date oculto que abre el calendario nativo
  const colId = props.column?.getColId?.() ?? props.colDef?.field

  // En cada tecleo se ESPEJA al proxy lo que el grid debería commitear si cierra ya mismo
  // (Enter lo procesa AG Grid con un listener nativo ANTES que React — el espejo debe estar
  // siempre al día): fecha completa o N/A → eso; texto a medias o vacío → el valor inicial
  // (nunca un null destructivo; vaciar es solo con «Borrar» o Enter en vacío).
  const setTxt = (t) => {
    textoRef.current = t
    setTexto(t)
    const tt = (t || '').trim().toUpperCase()
    const listo = tt === 'N/A' ? 'N/A' : (FECHA_COMPLETA.test(tt) ? tt : null)
    props.onValueChange?.(listo !== null ? listo : valorInicial)
  }
  // Confirmación explícita (Enter, calendario, Hoy, N/A, Borrar): actualiza el proxy Y
  // escribe imperativo (respaldo si el grid ya cerró la edición); el cierre posterior
  // commitea el mismo valor y no genera segundo evento.
  const guardar = (valorCasa) => {
    const v = valorCasa === '' ? null : valorCasa
    committedRef.current = v
    props.onValueChange?.(v)
    if (colId) props.node.setDataValue(colId, v)
    setTimeout(() => props.stopEditing(), 0)
  }
  // Interpreta lo que hay escrito al confirmar (Enter/foco): fecha completa, N/A, vacío, o —si
  // está a medias— conserva el valor original en vez de guardar basura.
  const valorDeTexto = () => {
    const t = (textoRef.current || '').trim().toUpperCase()
    if (t === 'N/A') return 'N/A'
    if (t === '') return ''
    if (FECHA_COMPLETA.test(t)) return t
    return null                                 // incompleto → no tocar
  }
  const confirmar = () => {
    const v = valorDeTexto()
    if (v === null) {
      committedRef.current = valorInicial
      props.onValueChange?.(valorInicial)
      setTimeout(() => props.stopEditing(), 0)
    } else {
      guardar(v)
    }
  }
  const alTipear = (raw) => {
    if (/[a-zA-Z]/.test(raw)) setTxt(raw.toUpperCase().slice(0, 3))   // deja escribir "N/A"
    else setTxt(mascaraFecha(raw))
  }
  const abrirCalendario = () => {
    const el = dateRef.current
    if (!el) return
    el.value = fechaAISO(textoRef.current) || ''
    try { el.showPicker() } catch { el.focus() }
  }
  useEffect(() => { inputRef.current?.focus(); inputRef.current?.select() }, [])
  useImperativeHandle(ref, () => ({
    // Respaldo si AG Grid cierra el editor él solo (blur, refresco de datos, Esc): fecha
    // completa escrita → se guarda; cualquier otra cosa → devuelve el valor VIVO de la celda
    // (node.data, no el snapshot del montaje) para que el cierre NUNCA genere un cambio que
    // borre lo recién guardado. Vaciar la celda solo pasa por «Borrar» o Enter en vacío.
    getValue: () => {
      if (committedRef.current !== undefined) return committedRef.current
      const v = valorDeTexto()
      if (v !== null && v !== '') return v
      const vivo = colId ? props.node?.data?.[colId] : undefined
      return vivo !== undefined ? vivo : original
    },
  }))
  const boton = (texto, valor, title) => (
    <button type="button" title={title}
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => guardar(valor)}
      className="px-1.5 py-0.5 text-[11px] border rounded bg-white text-gray-600 hover:bg-gray-100">
      {texto}
    </button>
  )
  const hoy = () => { const d = new Date(); return `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}/${d.getFullYear()}` }
  return (
    <div className="bg-white border border-blue-300 rounded shadow-lg p-1.5 flex items-center gap-1.5">
      <input ref={inputRef} type="text" inputMode="numeric" value={texto} placeholder="DD/MM/AAAA"
        onChange={(e) => alTipear(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') confirmar() }}
        className="text-xs border rounded px-1 py-0.5 w-24" />
      <span className="relative inline-flex">
        <button type="button" title="Abrir calendario"
          onMouseDown={(e) => e.preventDefault()} onClick={abrirCalendario}
          className="p-1 border rounded bg-white text-gray-600 hover:bg-gray-100">
          <CalendarDays size={14} />
        </button>
        <input ref={dateRef} type="date" tabIndex={-1} aria-hidden
          onChange={(e) => { if (e.target.value) { const f = isoAFecha(e.target.value); setTxt(f); guardar(f) } }}
          style={{ position: 'absolute', left: 0, bottom: 0, width: 1, height: 1, opacity: 0, pointerEvents: 'none' }} />
      </span>
      {boton('Hoy', hoy(), 'Poner la fecha de hoy')}
      {boton('N/A', 'N/A', 'No aplica (emergenciales / apoyo a PDL)')}
      {boton('Borrar', '', 'Dejar la celda vacía')}
    </div>
  )
})

const cumpleStyle = (p) => {
  if (p.value === 'CUMPLE') return { backgroundColor: '#dcfce7', color: '#15803d', fontWeight: 600 }
  if (p.value === 'NO CUMPLE') return { backgroundColor: '#fee2e2', color: '#b91c1c', fontWeight: 600 }
  return { color: '#9ca3af' }
}

const TipoCell = (p) => (
  <span style={{
    padding: '1px 6px', borderRadius: 4, fontWeight: 600,
    background: p.value === 'ST' ? '#e0e7ff' : '#dbeafe',
    color: p.value === 'ST' ? '#4338ca' : '#1d4ed8',
  }}>{p.value}</span>
)

// Sigla del elaborador. En Línea Viva el STM no la trae: la plataforma la presume por el estilo
// de la descripción (`_sigla_presunta`) y se marca con «≈» para que nadie le atribuya la orden a
// esa persona «porque lo dice el STM» (caso R07351, 02-10-2026). Escrita a mano pierde la marca.
const SiglaCell = (p) => {
  if (!p.data?._sigla_presunta || !p.value) return p.value ?? ''
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}
      title="Sigla PRESUNTA: el STM de Línea Viva no trae quién elaboró la orden. La plataforma la supone por el estilo de la descripción y a veces se equivoca. Confírmala en el STM (columna «Creador») antes de atribuirla; si es de otra persona, corrígela aquí.">
      <span style={{
        flexShrink: 0, padding: '0 4px', borderRadius: 4, fontSize: 10, fontWeight: 700,
        background: '#ffedd5', color: '#c2410c', border: '1px solid #fdba74',
      }}>≈</span>
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{p.value}</span>
    </span>
  )
}

// PDL + «ir al auditor» (requisito 2026-10-01: validar rápido un caso desde la tabla).
// El backend dice a dónde (`_auditor`): Correos ANS si hay un correo del caso por aprobar,
// Auditoría IA si hay análisis, o Auditoría IA para analizarlo. Ámbar si a la celda le falta algo.
const PdlCell = (p) => {
  const dest = p.data?._auditor
  const ir = p.context?.onIrAuditor
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: 4, fontFamily: 'monospace' }}>
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{p.value}</span>
      {dest && ir && (
        <button type="button" title={`Ir a ${dest.tab === 'correos-ans' ? 'Correos ANS' : 'Auditoría IA'}: ${dest.motivo}`}
          onClick={(e) => { e.stopPropagation(); ir(dest, p.data) }}
          onDoubleClick={(e) => e.stopPropagation()}
          style={{
            marginLeft: 'auto', flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 2,
            padding: '0 5px', height: 18, borderRadius: 4, fontSize: 10, fontFamily: 'system-ui, sans-serif', fontWeight: 600,
            border: `1px solid ${dest.pendiente ? '#f59e0b' : '#c4b5fd'}`,
            background: dest.pendiente ? '#fef3c7' : '#f5f3ff', color: dest.pendiente ? '#92400e' : '#6d28d9', cursor: 'pointer',
          }}>
          {dest.tab === 'correos-ans' ? <Mail size={11} /> : <Bot size={11} />}
          {dest.tab === 'correos-ans' ? 'correo' : 'IA'}
        </button>
      )}
    </span>
  )
}

// helpers para construir columnas. Fuente STM: editable (correcciones), color gris.
const src = (field, headerName, w = 130) => ({
  field, headerName, width: w, headerClass: 'hdr-gray',
  editable: true, cellClass: 'col-gray',
})
const grp = (tint, hdr) => (field, headerName, w = 120, o = {}) => ({
  field: o.calc ? undefined : field, colId: field, headerName, width: w, headerClass: hdr,
  editable: !!o.edit, cellClass: o.edit ? 'col-ans-edit' : tint,
  ...(o.calc ? { valueGetter: (p) => o.calc(p.data) } : {}),
  ...(o.bool ? { valueFormatter: (p) => (p.value ? 'Sí' : 'No') } : {}),
  ...(o.cumple ? { cellStyle: cumpleStyle } : {}),
  ...(o.num ? { type: 'numericColumn' } : {}),
  // Editores a prueba de digitación (pedido de las programadoras):
  // fecha → calendario · sino → desplegable con los valores permitidos
  ...(o.fecha ? { cellEditor: FechaEditor, cellEditorPopup: true } : {}),
  ...(o.sino ? { cellEditor: 'agSelectCellEditor', cellEditorParams: { values: Array.isArray(o.sino) ? o.sino : ['', 'SI', 'NO'] } } : {}),
  // texto → observaciones con TEXTO COMPLETO (requisito 2026-08-24): la celda
  // envuelve y la fila crece — nada de estirar la columna para leer. Editor en popup
  // multilínea (editar 400 caracteres en un renglón era el otro dolor).
  ...(o.texto ? {
    wrapText: true, autoHeight: true,
    cellClass: [o.edit ? 'col-ans-edit' : tint, 'celda-texto'],
    cellEditor: 'agLargeTextCellEditor', cellEditorPopup: true,
    cellEditorParams: { maxLength: 4000, rows: 10, cols: 60 },
  } : {}),
})
const A = grp('col-amber', 'hdr-amber')   // 3.4 / 3.3 (ámbar)
const V = grp('col-prog', 'hdr-green')     // 3.2 A/B/C (verde)

export default function AnsDataGrid({ items, onSave, apiBase = '/api/ans', filtroRol = null, titulo = '', periodo = null, ingZona = false, zonaPend = null, onIrAuditor = null }) {
  const rolUsuario = useAuthStore((s) => s.usuario?.rol)
  const veTodo = useAuthStore((s) => s.usuario?.ans_ve_todo)   // dev/ans/directivo + cuentas autorizadas
  const emailUsuario = useAuthStore((s) => s.usuario?.email)
  // «ES EMERGENCIAL» a mano: dev + directivo autorizado (lo decide el backend: puede_editar_emergencial)
  const puedeEmergencial = useAuthStore((s) => !!s.usuario?.puede_editar_emergencial)
  // ¿Es una de las 4 cuentas del proceso programación+cumplimentación? (lo decide el backend)
  const esElab = useAuthStore((s) => !!s.usuario?.es_elaborador)
  // `esDev` queda SOLO para «¿A quién pregunto?» (la ayuda con IA, exclusiva del desarrollador).
  const esDev = rolUsuario === 'desarrollador'
  // Programador y cumplimentador son UN solo rol de diligenciamiento («elaborador», ago-2026):
  // las mismas 4 cuentas llenan programación (3.4 + G·H) y cumplimentación (AQ).
  // Al ingeniero de zona (cuenta de zona o apoyo, sin nodos) le parpadean las celdas del
  // encargado ANS ('ans') pero solo en filas de su zona; hasta resolver la zona, nada.
  const rolPendUsuario = (rolUsuario === 'programador' || rolUsuario === 'cumplimentador')
    ? 'elaborador' : ingZona ? (zonaPend ? 'ans' : null) : rolUsuario
  // Quien solo diligencia ve parpadear SOLO lo suyo. Quien supervisa ve los 3 roles (control
  // total) y puede enfocarse en uno con `filtroRol` para priorizar.
  const rolesVisibles = useMemo(() => (
    veTodo
      ? new Set(filtroRol ? [filtroRol] : ['elaborador', 'ingeniero_zona', 'ans'])
      : new Set(rolPendUsuario ? [rolPendUsuario] : [])
  ), [veTodo, rolPendUsuario, filtroRol])
  // A un elaborador, las celdas «elaborador» solo le parpadean en SUS filas (sigla col H =
  // su cuenta, campo `_elab` del backend) o en las que aún no tienen dueño (sin sigla).
  const filaMia = useCallback((data) => (
    !esElab || !data?._elab || data._elab === emailUsuario
  ), [esElab, emailUsuario])

  // ── Ir a la celda que parpadea ──────────────────────────────────────────────
  // Filtrar no basta: la grilla es anchísima y toca cazar la celda a ojo. Esto salta a ella
  // (scroll vertical + horizontal + foco), y con «Siguiente» se recorren una tras otra.
  const gridApi = useRef(null)
  const [sinPend, setSinPend] = useState(false)   // ya no queda ninguna → se avisa

  // ── No pisar lo VIVO de la grilla ─────────────────────────────────────────────
  // Cada guardado devuelve la fila fresca (y hay refetch cada 60s). Reemplazar filas a ciegas
  // tenía dos males: (1) mataba el editor abierto a mitad de digitación (la respuesta de
  // «Debió validar» cerraba el editor de «Envió validar»), y (2) una foto VIEJA de la fila
  // pisaba visualmente lo recién digitado mientras su PATCH volaba. Regla nueva: una fila
  // conserva su data VIVA (node.data) mientras se esté editando o tenga guardados en vuelo;
  // el resto se actualiza al instante. Nada de retener snapshots para después.
  const guardadosEnVuelo = useRef(new Map())   // id → Map(campo → valor esperado en el cache)
  const refrescarAlParar = useRef(new Set())   // filas con parpadeo por refrescar al cerrar el editor
  const [tick, setTick] = useState(0)          // re-evalúa `rows` al terminar una edición
  const rows = useMemo(() => {
    const api = gridApi.current
    if (!api) return items
    const vivas = new Set()                    // ids cuya data viva se conserva
    ;(api.getEditingCells?.() || []).forEach((c) => {
      const id = api.getDisplayedRowAtIndex?.(c.rowIndex)?.data?.id
      if (id != null) vivas.add(id)
    })
    const eq = (a, b) => String(a ?? '') === String(b ?? '')
    return items.map((r) => {
      const vuelo = guardadosEnVuelo.current.get(r.id)
      if (vuelo) {
        // Se suelta cuando el cache ya trae lo guardado, o a los 20s (un PATCH fallido no
        // puede dejar la fila congelada para siempre — el toast de error ya avisó).
        const caduco = Date.now() - (vuelo.desde || 0) > 20000
        if (caduco || [...vuelo.campos].every(([campo, v]) => eq(r[campo], v))) {
          guardadosEnVuelo.current.delete(r.id)
        } else {
          vivas.add(r.id)                      // la fila fresca aún no trae lo guardado → no pisarla
        }
      }
      if (!vivas.has(r.id)) return r
      const nodo = api.getRowNode?.(String(r.id))
      return nodo?.data ?? r
    })
  }, [items, tick])
  const alTerminarEdicion = useCallback(() => {
    // Refresca el parpadeo de las filas editadas (diferido aquí para no desmontar el editor
    // en plena edición) y re-evalúa `rows` ahora que ya no hay editor abierto.
    const api = gridApi.current
    if (api && refrescarAlParar.current.size) {
      api.refreshCells({ rowNodes: [...refrescarAlParar.current], force: true })
      refrescarAlParar.current.clear()
    }
    setTick((t) => t + 1)
  }, [])

  const saltarPendiente = useCallback((desdeFila = -1) => {
    const api = gridApi.current
    if (!api) return false
    const total = api.getDisplayedRowCount()
    const buscar = (ini, fin) => {
      for (let i = ini; i < fin; i++) {
        const node = api.getDisplayedRowAtIndex(i)
        const pend = node?.data?._pend
        if (!pend) continue
        for (const campo of CAMPOS_PEND_ORDEN) {
          const rol = pend[campo]
          if (!rol || !rolesVisibles.has(rol)) continue
          if (rol === 'elaborador' && !filaMia(node.data)) continue   // fila de otra cuenta
          if (rol === 'ans' && zonaPend
              && (node.data.zona || '').toUpperCase() !== zonaPend) continue   // fila de otra zona
          if (!vacia(node.data[campo])) continue        // ya la llenaron: no es pendiente
          api.ensureIndexVisible(i, 'middle')
          api.ensureColumnVisible(campo)
          api.setFocusedCell(i, campo)
          return true
        }
      }
      return false
    }
    // Desde la fila actual hacia abajo; si no hay más, vuelve a empezar por arriba.
    const ok = buscar(desdeFila + 1, total) || (desdeFila >= 0 && buscar(0, desdeFila + 1))
    setSinPend(!ok)
    if (ok) setTimeout(() => setSinPend(false), 10)
    return ok
  }, [rolesVisibles, filaMia, zonaPend])

  const siguientePendiente = () => {
    const f = gridApi.current?.getFocusedCell()
    saltarPendiente(f ? f.rowIndex : -1)
  }

  // Al activar un filtro de rol (clic en un chip), lleva de una a la primera celda pendiente.
  const filtroPrevio = useRef(null)
  useEffect(() => {
    if (filtroRol && filtroRol !== filtroPrevio.current) {
      const t = setTimeout(() => saltarPendiente(-1), 80)   // deja que la grilla pinte el filtro
      filtroPrevio.current = filtroRol
      return () => clearTimeout(t)
    }
    filtroPrevio.current = filtroRol
  }, [filtroRol, saltarPendiente])

  // ── Modo Excel: la tabla toma TODA la pantalla (adiós sidebar y cabeceras) ─────
  const [excel, setExcel] = useState(false)
  const [quick, setQuick] = useState('')          // búsqueda rápida sobre toda la tabla
  const quickRef = useRef(null)
  const [nFilas, setNFilas] = useState(null)      // filas visibles tras filtros de columna
  const [filtrosCol, setFiltrosCol] = useState({})  // {colKey: {n, total}} → chips

  // Fuera del Modo Excel la grilla igual aprovecha lo que quede de pantalla (antes: 72vh fijos,
  // que en laptop dejaban la tabla enana). Se mide dónde arranca y se estira hasta abajo.
  const wrapRef = useRef(null)
  const [alto, setAlto] = useState(null)
  useLayoutEffect(() => {
    if (excel) return
    const medir = () => {
      const el = wrapRef.current
      if (!el) return
      setAlto(Math.max(480, window.innerHeight - el.getBoundingClientRect().top - 14))
    }
    medir()
    const ro = new ResizeObserver(medir)   // banners que aparecen/desaparecen arriba
    ro.observe(document.body)
    window.addEventListener('resize', medir)
    return () => { ro.disconnect(); window.removeEventListener('resize', medir) }
  }, [excel])

  const onColumnFilterChanged = useCallback((colKey, selectedKeys, totalValues) => {
    setFiltrosCol((prev) => {
      if (selectedKeys.size >= totalValues) {
        if (!(colKey in prev)) return prev
        const next = { ...prev }
        delete next[colKey]
        return next
      }
      return { ...prev, [colKey]: { n: selectedKeys.size, total: totalValues } }
    })
  }, [])
  const gridContext = useMemo(() => ({ onColumnFilterChanged, onIrAuditor }), [onColumnFilterChanged, onIrAuditor])

  const limpiarFiltroCol = useCallback((colKey) => {
    const api = gridApi.current
    if (!api) return
    api.setColumnFilterModel(colKey, null).then(() => api.onFilterChanged())
  }, [])
  const limpiarFiltrosCol = useCallback(() => { gridApi.current?.setFilterModel(null) }, [])

  // Al cambiar de periodo los valores filtrados ya no existen: se resetean los filtros.
  const periodoPrevio = useRef(periodo)
  useEffect(() => {
    if (periodoPrevio.current !== periodo) {
      periodoPrevio.current = periodo
      gridApi.current?.setFilterModel(null)
      setFiltrosCol({})
    }
  }, [periodo])

  const columnDefs = useMemo(() => {
    const defs = [
    { field: 'tipo', headerName: 'Tipo', width: 70, pinned: 'left', cellRenderer: TipoCell },
    { field: 'pdl', headerName: 'PDL', width: 225, pinned: 'left', editable: true, cellClass: 'col-gray', cellStyle: { fontFamily: 'monospace' }, cellRenderer: PdlCell },
    {
      headerName: 'Datos STM (CD10)', headerClass: 'hdr-gray', children: [
        src('protocolo', 'Protocolo', 200),
        src('asociado_pdl', 'Asociado a PdL', 150),
        src('ri_jt', 'RI / JT', 160),
        src('zona', 'Zona', 90),
        src('nombre_responsable', 'Responsable', 150),
        { ...src('sigla_elaborador', 'Sigla elaborador', 170), cellRenderer: SiglaCell },
        src('descripcion', 'Descripción', 240),
        src('fecha_inicio', 'Fecha inicio', 95),
        src('hora_inicio', 'Hora inicio', 80),
        src('fecha_fin', 'Fecha fin', 95),
        src('hora_fin', 'Hora fin', 80),
        src('unidad_resp', 'Unidad Resp.', 130),
        src('elaborado_por', 'Elaborado por', 180),
        src('estado_actual', 'Estado Actual', 120),
        src('modif_consistencia', 'Modif. Consist.', 95),
        src('causal', 'Causal', 130),
        src('maniobra_inicio', 'Maniobra inicio', 130),
        src('maniobra_fin', 'Maniobra fin', 130),
      ],
    },
    {
      headerName: 'Auto', headerClass: 'hdr-gray', children: [
        // ES EMERGENCIAL: la calcula la causal, pero el dev y el directivo autorizado la pueden fijar A MANO
        // (2026-10-01). Se guarda en es_emergencial_manual (SI/NO; «Automático» lo devuelve a la
        // fórmula) y manda en apertura/cierre, el 3.4 y el exporte. Gate real en el PATCH.
        { colId: 'es_emergencial', field: 'es_emergencial_manual', headerName: 'ES EMERGENCIAL', width: 125, headerClass: 'hdr-green',
          valueGetter: (p) => p.data?.es_emergencial_manual || (p.data?.es_emergencial ? 'SI' : 'NO'),
          valueFormatter: (p) => (p.value === 'SI' ? 'Sí' : 'No') + (p.data?.es_emergencial_manual ? ' · a mano' : ''),
          valueSetter: (p) => {
            const v = p.newValue === 'SI' || p.newValue === 'NO' ? p.newValue : null
            if ((p.data.es_emergencial_manual || null) === v) return false
            p.data.es_emergencial_manual = v
            return true
          },
          editable: puedeEmergencial,
          cellEditor: 'agSelectCellEditor',
          cellEditorParams: { values: ['SI', 'NO', 'AUTO'],
            formatValue: (v) => (v === 'SI' ? 'Sí' : v === 'NO' ? 'No' : 'Automático (por la causal)') },
          cellClass: (p) => (p.data?.es_emergencial_manual ? 'col-ans-edit' : 'col-prog') },
        { field: 'apoyo_pdl', headerName: 'APOYO A PDL', width: 110, headerClass: 'hdr-amber', cellClass: 'col-amber', valueFormatter: (p) => (p.value ? 'Sí' : 'No') },
      ],
    },
    {
      headerName: '3.4 · Oportunidad programación', headerClass: 'hdr-amber', children: [
        A('fecha_debio_validar', 'Debió validar', 105, { edit: true, fecha: true }),
        A('_debio_enviar', 'Debió enviar', 105, { calc: (r) => menos1(r.fecha_debio_validar) }),
        A('fecha_envio_validar', 'Envió validar', 105, { edit: true, fecha: true }),
        A('cumple_tprog', 'Cumple t.prog', 110, { cumple: true }),
        A('excluible_tiempos', 'Excluible', 80, { edit: true, sino: true }),
        A('obs_tiempos', 'Observación', 300, { edit: true, texto: true }),
      ],
    },
    {
      headerName: '3.2 A · Tiempos apertura/cierre', headerClass: 'hdr-green', children: [
        V('_ap_plan', 'Apertura planeada', 135, { calc: (r) => `${r.fecha_inicio || ''} ${r.hora_inicio || ''}`.trim() }),
        V('_ap_real', 'Apertura real', 135, { calc: (r) => r.maniobra_inicio || 'N/A' }),
        V('desfase_apertura', 'Desfase ap.', 85),
        V('cumple_apertura', 'Cumple ap.', 110, { cumple: true }),
        V('_ci_plan', 'Cierre planeado', 135, { calc: (r) => `${r.fecha_fin || ''} ${r.hora_fin || ''}`.trim() }),
        V('_ci_real', 'Cierre real', 135, { calc: (r) => r.maniobra_fin || 'N/A' }),
        V('desfase_cierre', 'Desfase ci.', 85),
        V('cumple_cierre', 'Cumple ci.', 110, { cumple: true }),
        V('cumple_tiempos', 'Cumple tiempos', 115, { cumple: true }),
        V('excluible', 'Excluible?', 80, { edit: true, sino: true }),
        V('obs', 'OBS', 300, { edit: true, texto: true }),
      ],
    },
    {
      headerName: '3.2 B · Ejecución', headerClass: 'hdr-green', children: [
        V('se_ejecuto', 'SE EJECUTÓ?', 100, { edit: true, sino: ['', 'SI', 'NO', 'N/A'] }),
        V('excluible_anulado', 'Excluible', 80, { edit: true, sino: true }),
        V('obs_anulado', 'Observación', 300, { edit: true, texto: true }),
      ],
    },
    {
      headerName: '3.3 · Calidad UUCC', headerClass: 'hdr-amber', children: [
        A('nombre_cumplimentador', 'Nombre cumplimentador', 160, { edit: true }),
        A('obs_uucc', 'Obs devolución UUCC', 300, { edit: true, texto: true }),
        A('cumple_uucc', 'Cumple UUCC', 110, { cumple: true }),
        A('excluible2', 'Excluible2', 80, { edit: true, sino: true }),
        A('obs3', 'Observación', 300, { edit: true, texto: true }),
      ],
    },
    {
      headerName: '3.2 C · Nodos', headerClass: 'hdr-green', children: [
        V('nodos_planeados', 'Nodos planeados', 100, { edit: true, num: true }),
        V('nodos_ejecutados', 'Nodos ejecutados', 100, { edit: true, num: true }),
        V('nods_excluible', 'Nods excluible', 85, { edit: true, sino: true }),
        V('obs_nodos', 'Observaciones', 300, { edit: true, texto: true }),
      ],
    },
    ]
    return conAutoNodos(conParpadeo(defs, rolesVisibles, filaMia, zonaPend))
  }, [rolesVisibles, filaMia, zonaPend, puedeEmergencial])

  // Nombre visible de cada columna → etiquetas de los chips de filtro activo
  const nombresCol = useMemo(() => {
    const m = {}
    const walk = (cols) => cols.forEach((c) => c.children ? walk(c.children) : (m[c.colId || c.field] = c.headerName))
    walk(columnDefs)
    return m
  }, [columnDefs])

  const defaultColDef = useMemo(() => ({
    resizable: true, sortable: true,
    filter: ExcelSetFilter,          // autofiltro tipo Excel: casillas por valor + buscador
    suppressHeaderMenuButton: true,  // solo el embudo; el menú ⋮ estorba en 50 columnas
    cellDataType: false,   // evita que columnas vacías no abran el editor (AG Grid v32)
  }), [])

  const onCellValueChanged = useCallback((e) => {
    const field = e.colDef.field
    if (!field) return
    let valor = e.newValue === '' || e.newValue === undefined ? null : e.newValue
    if (field === 'es_emergencial_manual' && valor !== 'SI' && valor !== 'NO') valor = null   // «Automático»
    // Marca el guardado EN VUELO: mientras el cache no traiga este valor, la fila fresca no
    // pisa la data viva de la grilla (evita que una respuesta vieja borre lo recién digitado).
    const vuelo = guardadosEnVuelo.current.get(e.data.id) || { desde: Date.now(), campos: new Map() }
    vuelo.desde = Date.now()
    vuelo.campos.set(field, valor)
    guardadosEnVuelo.current.set(e.data.id, vuelo)
    // Re-evalúa el parpadeo de la fila: la celda llena deja de parpadear y, si al decidir una
    // exclusión ahora toca su razón, esa empieza. PERO nunca con un editor abierto — el force
    // refresh desmonta/remonta el editor y un editor remontado puede confirmar un valor viejo
    // (así se borraban fechas solas el 27-jul). Con editor abierto queda para alTerminarEdicion.
    if ((e.api.getEditingCells?.() || []).length === 0) {
      e.api.refreshCells({ rowNodes: [e.node], force: true })
    } else {
      refrescarAlParar.current.add(e.node)
    }
    onSave(e.data.id, field, valor)
  }, [onSave])

  // ── Modo justificación: clic en celda → popup "por qué este dato" ──────────────
  const [modo, setModo] = useState(false)
  const [popup, setPopup] = useState(null)   // {x, y, label, valor, info}
  const [expandido, setExpandido] = useState(false)
  const cache = useRef({})                    // {regId: campos}

  // Modo "¿A quién pregunto?" — solo dev, por ahora. Sugiere a quién consultar para poder
  // llenar la celda, y POR QUÉ a esa persona (deducido de la fila: estado, zona, líder, supervisor).
  const [modoSug, setModoSug] = useState(false)
  const [sug, setSug] = useState(null)        // {ancla, campo, label, data}
  const [chat, setChat] = useState(null)      // {regId, campo} — chat de investigación abierto
  const [preguntar, setPreguntar] = useState(false)   // pasó del grupo (paso 1) a preguntar (paso 2)

  // «Ver justificación» de una OBSERVACIÓN: al seleccionar (clic o clic derecho) una celda de
  // observación del 3.2 A / 3.2 B / 3.4 aparece la opción junto al cursor; abre un popup con
  // de dónde salió el texto y todo lo que lo sustenta (Auditoría IA, Correos ANS, chat, evidencias).
  const [chip, setChip] = useState(null)     // {ancla, id, campo, label} — la opción junto a la celda
  const [just, setJust] = useState(null)     // {id, campo, label} — popup abierto

  const onCellClicked = useCallback(async (e) => {
    const id = e.data?.id
    const field = e.colDef.field || e.colDef.colId
    if (!id || !field) return
    const ev = e.event || {}
    // Observación: en modo normal ofrece «Ver justificación» junto al cursor; en modo
    // «Justificar datos» abre la justificación de una vez (es lo que se está pidiendo).
    if (CAMPOS_OBS_JUST.has(field) && !modoSug) {
      const label = e.colDef.headerName || field
      if (modo) { setPopup(null); setJust({ id, campo: field, label }); return }
      const px = ev.clientX ?? 200
      const py = ev.clientY ?? 200
      setChip({ ancla: { left: px, top: py, bottom: py }, id, campo: field, label })
      return
    }
    setChip(null)
    // Ancla = la celda en sí (no el punto del clic): el popup se coloca respecto a ella y así
    // puede abrirse ARRIBA cuando la fila está al fondo de la grilla, sin quedar cortado.
    const celda = ev.target?.closest?.('.ag-cell')
    const r = celda?.getBoundingClientRect()
    const cx = ev.clientX ?? 200
    const cy = ev.clientY ?? 200
    const ancla = r
      ? { left: r.left, top: r.top, bottom: r.bottom }
      : { left: cx, top: cy, bottom: cy }
    const label = e.colDef.headerName || field

    if (modoSug) {
      setPreguntar(false)          // cada celda empieza por el paso 1 (buscar en el grupo)
      if (!CAMPOS_PEND.has(field)) {
        setSug({ ancla, label, data: null, vacio: true })   // celda que nadie tiene que investigar
        return
      }
      setSug({ ancla, label, data: null, id, campo: field })
      try {
        const res = await api.get(`/sugerencia/${id}`, { baseURL: apiBase, params: { campo: field } })
        setSug((s) => (s ? { ...s, data: res.data } : null))
      } catch {
        setSug((s) => (s ? { ...s, error: true } : null))
      }
      return
    }

    if (!modo) return
    const valor = e.value
    setPopup({ ancla, label, valor, info: { origen: 'manual', texto: 'Cargando…' } })
    try {
      if (!cache.current[id]) {
        cache.current[id] = (await api.get(`/justificacion/${id}`, { baseURL: apiBase })).data.campos || {}
      }
      const info = cache.current[id][field] || { origen: 'derivado', texto: 'Columna de visualización: se calcula al vuelo a partir de otras celdas.' }
      setPopup((p) => (p ? { ...p, info } : null))
    } catch {
      setPopup((p) => (p ? { ...p, info: { origen: 'manual', texto: 'No se pudo cargar la justificación.' } } : null))
    }
  }, [modo, modoSug, apiBase])

  // Clic derecho sobre una observación: la misma opción, sin el menú del navegador.
  const onCellContextMenu = useCallback((e) => {
    const id = e.data?.id
    const field = e.colDef.field || e.colDef.colId
    if (!id || !CAMPOS_OBS_JUST.has(field)) return
    e.event?.preventDefault?.()
    const px = e.event?.clientX ?? 200
    const py = e.event?.clientY ?? 200
    setChip({ ancla: { left: px, top: py, bottom: py }, id, campo: field, label: e.colDef.headerName || field })
  }, [])

  // La opción se va sola al pulsar fuera de ella o al desplazar la grilla (la celda ya no está ahí).
  const chipRef = useRef(null)
  useEffect(() => {
    if (!chip) return
    const fuera = (ev) => { if (!chipRef.current?.contains(ev.target)) setChip(null) }
    window.addEventListener('mousedown', fuera, true)
    return () => window.removeEventListener('mousedown', fuera, true)
  }, [chip])

  // Esc cierra lo más "cercano" primero: popups propios → búsqueda rápida → edición/popup del
  // grid (los maneja AG Grid) → y solo al final sale del Modo Excel. Fase captura: corre ANTES
  // de que AG Grid cancele la edición, así getEditingCells() aún dice la verdad.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape') return
      if (just) { setJust(null); return }
      if (popup || sug || chip) { setPopup(null); setSug(null); setChip(null); return }
      if (!excel) return
      if (document.activeElement === quickRef.current && quick) { setQuick(''); return }
      const api = gridApi.current
      if (api?.getEditingCells?.().length) return       // Esc = cancelar la edición de la celda
      if (document.querySelector('.ag-popup')) return   // Esc = cerrar el filtro abierto
      setExcel(false)
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [popup, sug, chip, just, excel, quick])

  // Tras guardar un número, se vuelve a pedir la sugerencia: el contacto aparece ya con su
  // botón de WhatsApp, sin cerrar el popup.
  const recargarSug = useCallback(async () => {
    if (!sug?.id) return
    try {
      const res = await api.get(`/sugerencia/${sug.id}`, { baseURL: apiBase, params: { campo: sug.campo } })
      setSug((s) => (s ? { ...s, data: res.data } : null))
    } catch { /* se queda con lo que ya tenía */ }
  }, [sug?.id, sug?.campo, apiBase])

  const [sugRef, sugPos] = useFlotante(sug?.ancla, !!sug)
  const [popRef, popPos] = useFlotante(popup?.ancla, !!popup && !expandido)
  const [chipPosRef, chipPos] = useFlotante(chip?.ancla, !!chip)
  const chipRefs = (el) => { chipRef.current = el; chipPosRef.current = el }

  return (
    <div className={excel ? 'fixed inset-0 z-[60] bg-gray-100 flex flex-col p-2 md:p-3' : 'relative'}>
      <div className="flex flex-wrap items-center gap-2 mb-1.5 shrink-0">
        {excel && titulo && <span className="text-sm font-bold text-gray-800 mr-1">{titulo}</span>}
        <button
          onClick={() => setExcel((v) => !v)}
          className={`flex items-center gap-1.5 text-xs font-semibold px-2.5 py-1 rounded-lg border transition-colors text-white ${
            excel ? 'bg-emerald-800 border-emerald-800 hover:bg-emerald-900' : 'bg-emerald-600 border-emerald-600 hover:bg-emerald-700'
          }`}
          title={excel ? 'Volver a la vista normal (Esc)' : 'La tabla ocupa toda la pantalla, con filtros y navegación como en Excel'}
        >
          {excel ? <Shrink size={13} /> : <Sheet size={13} />}
          {excel ? 'Salir de Modo Excel' : 'Modo Excel'}
          {excel && <span className="text-[9px] uppercase tracking-wide opacity-70">Esc</span>}
        </button>
        {excel && (
          <div className="relative">
            <Search size={13} className="absolute left-2 top-1.5 text-gray-400" />
            <input
              ref={quickRef} value={quick} onChange={(e) => setQuick(e.target.value)}
              placeholder="Buscar en toda la tabla…"
              className="pl-6 pr-6 py-0.5 text-xs border border-gray-300 rounded-lg w-56 focus:outline-none focus:ring-1 focus:ring-emerald-500"
            />
            {quick && (
              <button onClick={() => setQuick('')} className="absolute right-1.5 top-1 text-gray-400 hover:text-gray-700"><X size={13} /></button>
            )}
          </div>
        )}
        <button
          onClick={() => { setModo((v) => !v); setModoSug(false); setPopup(null); setSug(null) }}
          className={`flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-lg border transition-colors ${
            modo ? 'bg-blue-700 text-white border-blue-700' : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50'
          }`}
          title="Muestra de dónde salió el valor de cada celda"
        >
          <Search size={13} /> {modo ? 'Justificación activa — clic en una celda' : 'Justificar datos'}
        </button>
        {esDev && (
          <button
            onClick={() => { setModoSug((v) => !v); setModo(false); setPopup(null); setSug(null) }}
            className={`flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-lg border transition-colors ${
              modoSug ? 'bg-teal-700 text-white border-teal-700' : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50'
            }`}
            title="En evaluación (solo tú): sugiere a quién preguntarle para poder llenar la celda"
          >
            <HelpCircle size={13} /> {modoSug ? 'Clic en una celda pendiente' : '¿A quién pregunto?'}
            <span className="text-[9px] uppercase tracking-wide opacity-70">beta</span>
          </button>
        )}
        {/* Lleva a la celda que parpadea, sin cazarla a ojo en una grilla de 50 columnas */}
        {rolesVisibles.size > 0 && (
          <button onClick={siguientePendiente}
            className="flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-lg border bg-white text-gray-600 border-gray-300 hover:bg-amber-50 hover:border-amber-400 hover:text-amber-800"
            title="Salta a la siguiente celda pendiente (F2 la edita)">
            <Crosshair size={13} /> Siguiente pendiente
          </button>
        )}
        {sinPend && <span className="text-[11px] text-green-700 font-medium">✓ No queda ninguna pendiente</span>}
        {modo && <span className="text-[11px] text-gray-400">Pulsa una celda para ver por qué tiene ese valor · Esc cierra</span>}
        {modoSug && <span className="text-[11px] text-gray-400">Pulsa una celda que parpadea · Esc cierra</span>}

        {/* Chips de filtros de columna activos, como la barra de estado de Excel */}
        {Object.keys(filtrosCol).length > 0 && (
          <>
            {Object.entries(filtrosCol).map(([k, f]) => (
              <span key={k} className="flex items-center gap-1 text-[11px] bg-blue-50 text-blue-800 border border-blue-200 rounded-full px-2 py-0.5">
                <FilterIcon size={10} /> {nombresCol[k] || k}: {f.n}/{f.total}
                <button onClick={() => limpiarFiltroCol(k)} className="text-blue-400 hover:text-red-600" title="Quitar este filtro"><X size={11} /></button>
              </span>
            ))}
            <button onClick={limpiarFiltrosCol} className="text-[11px] text-gray-500 underline hover:text-blue-700">Quitar todos</button>
          </>
        )}

        <span className="ml-auto text-[11px] text-gray-400 whitespace-nowrap">
          {excel && <span className="hidden lg:inline mr-3">Enter baja · F2 o doble-clic edita · Ctrl+Z deshace</span>}
          {nFilas != null && nFilas !== items.length ? `${nFilas} de ${items.length} filas` : `${items.length} filas`}
        </span>
      </div>

      <div ref={wrapRef} className={`ag-theme-alpine ${excel ? 'flex-1 min-h-0' : ''}`}
        style={excel ? { width: '100%' } : { width: '100%', height: alto ? `${alto}px` : '72vh' }}>
        <AgGridReact
          rowData={rows}
          columnDefs={columnDefs}
          defaultColDef={defaultColDef}
          context={gridContext}
          onGridReady={(p) => { gridApi.current = p.api }}
          onCellEditingStopped={alTerminarEdicion}
          getRowId={(p) => String(p.data.id)}
          stopEditingWhenCellsLoseFocus
          singleClickEdit={false}
          enableCellTextSelection
          ensureDomOrder
          undoRedoCellEditing
          undoRedoCellEditingLimit={40}
          suppressMenuHide
          enterNavigatesVertically
          enterNavigatesVerticallyAfterEdit
          quickFilterText={quick}
          onModelUpdated={(e) => setNFilas(e.api.getDisplayedRowCount())}
          onCellValueChanged={onCellValueChanged}
          onCellClicked={onCellClicked}
          onCellContextMenu={onCellContextMenu}
          onBodyScroll={() => setChip(null)}
          headerHeight={30}
          groupHeaderHeight={30}
          rowHeight={30}
        />
      </div>

      {/* «Ver justificación» de la observación seleccionada: la opción junto al cursor y su popup */}
      {chip && (
        <div ref={chipRefs} className="fixed z-50 bg-white border border-violet-300 rounded-lg shadow-xl overflow-hidden" style={chipPos}>
          <button
            onMouseDown={(e) => e.stopPropagation()}
            onClick={() => { setJust({ id: chip.id, campo: chip.campo, label: chip.label }); setChip(null) }}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-violet-800 hover:bg-violet-50"
            title="De dónde salió esta observación y qué la sustenta (Auditoría IA, Correos ANS, chat, evidencias)">
            <FileSearch size={13} /> Ver justificación
          </button>
        </div>
      )}
      {just && (
        <JustificacionObs regId={just.id} campo={just.campo} label={just.label} apiBase={apiBase}
          onCerrar={() => setJust(null)} />
      )}

      {/* Popup de justificación (al clic en otra celda se mueve; Esc o X cierran; ⤢ expande) */}
      {sug && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setSug(null)} />
          <div ref={sugRef}
            className="fixed z-50 w-[24rem] max-h-[80vh] overflow-auto bg-white border border-teal-300 rounded-lg shadow-2xl text-xs"
            style={sugPos} onClick={(e) => e.stopPropagation()}>
            <div className="sticky top-0 flex items-center justify-between px-3 py-2 bg-teal-50 border-b border-teal-200">
              <span className="font-semibold text-teal-900">{sug.data?.titulo || sug.label}</span>
              <button onClick={() => setSug(null)} className="text-gray-400 hover:text-gray-700"><X size={14} /></button>
            </div>
            {sug.vacio ? (
              <p className="p-3 text-gray-500">Esta celda no requiere investigación: nadie tiene que preguntarle a nadie para llenarla.</p>
            ) : sug.error ? (
              <p className="p-3 text-red-600">No se pudo cargar la sugerencia.</p>
            ) : !sug.data ? (
              <p className="p-3 text-gray-400">Pensando a quién preguntarle…</p>
            ) : (
              <div className="p-3 space-y-3">
                <div>
                  <p className="text-[10px] uppercase tracking-wide text-gray-400 font-semibold mb-0.5">Qué tienes que decidir</p>
                  <p className="text-gray-700 leading-relaxed">{sug.data.que_determinar}</p>
                </div>
                {/* 3.2 A · Paso 1: el video del grupo. Solo si no aparece se pasa a preguntar. */}
                {sug.data.busqueda && (
                  <EvidenciaBox
                    tipo="video" regId={sug.id} campo={sug.campo} apiBase={apiBase} apiRoot={apiBase}
                    esDev={esDev}
                    busqueda={sug.data.busqueda}
                    onNoEncontrado={() => setPreguntar(true)}
                    onAprobado={(r) => {
                      // 3.2 A: solo escribe la observación — el excluible SI/NO lo decide la persona
                      if (r.campo_decision) onSave?.(sug.id, r.campo_decision, r.valor_decision)
                      onSave?.(sug.id, r.campo_razon, r.valor_razon)
                    }} />
                )}
                {/* 3.2 B · El correo del Centro Operativo (sin puerta: se pide y se adjunta). La IA
                    lo lee y lo cruza con la orden; tú apruebas. */}
                {CAMPOS_ANULADO.has(sug.campo) && (
                  <EvidenciaBox
                    tipo="correo" regId={sug.id} campo={sug.campo} apiBase={apiBase} apiRoot={apiBase}
                    esDev={esDev}
                    onAprobado={(r) => {
                      if (r.campo_decision) onSave?.(sug.id, r.campo_decision, r.valor_decision)
                      onSave?.(sug.id, r.campo_razon, r.valor_razon)
                      if (r.campo_extra) onSave?.(sug.id, r.campo_extra, r.valor_extra)
                    }} />
                )}

                {(preguntar || !sug.data.busqueda) && (
                  <>
                    {sug.data.contactos?.length > 0 && (
                      <div>
                        <p className="text-[10px] uppercase tracking-wide text-gray-400 font-semibold mb-1">
                          Paso 2 · A quién preguntarle
                        </p>
                        <div className="space-y-1.5">
                          {sug.data.contactos.map((c, i) => (
                            <ContactoSug key={i} c={{ ...c, zona: sug.data.contexto?.zona }}
                              apiBase={apiBase} onGuardado={recargarSug} />
                          ))}
                        </div>
                      </div>
                    )}
                    {CAMPOS_CONSULTA.has(sug.campo) && (
                      <button onClick={() => setChat({ regId: sug.id, campo: sug.campo })}
                        className="w-full flex items-center justify-center gap-1.5 text-[11px] font-medium px-2 py-1.5 rounded bg-indigo-600 text-white hover:bg-indigo-700">
                        <Sparkles size={13} /> Abrir chat de investigación
                      </button>
                    )}
                  </>
                )}

                {sug.data.ruta?.length > 0 && (
                  <div>
                    <p className="text-[10px] uppercase tracking-wide text-gray-400 font-semibold mb-0.5">En qué orden buscarlo</p>
                    <ol className="list-decimal list-inside text-gray-600 space-y-0.5">
                      {sug.data.ruta.map((r, i) => <li key={i}>{r}</li>)}
                    </ol>
                  </div>
                )}
                {sug.data.pista && (
                  <p className="text-amber-800 bg-amber-50 border border-amber-200 rounded px-2 py-1.5 leading-relaxed">
                    <b>Pista del histórico:</b> {sug.data.pista}
                  </p>
                )}
                {sug.data.evidencia && sug.data.evidencia !== '—' && (
                  <p className="text-gray-500"><b>La evidencia va a:</b> {sug.data.evidencia}</p>
                )}
                <p className="text-[10px] text-gray-400 border-t pt-1.5">
                  {sug.data.contexto?.protocolo} · {sug.data.contexto?.zona} · {sug.data.contexto?.estado}
                </p>
              </div>
            )}
          </div>
        </>
      )}

      {popup && (
        <>
          {expandido && <div className="fixed inset-0 z-40 bg-black/30" onClick={() => setPopup(null)} />}
          <div
            ref={popRef}
            className={`fixed z-50 bg-white rounded-xl shadow-2xl border ${
              expandido ? 'w-[460px] max-w-[92vw] p-4 text-sm' : 'w-72 max-h-[80vh] overflow-auto p-3 text-xs'
            }`}
            style={expandido
              ? { left: '50%', top: '18%', transform: 'translateX(-50%)' }
              : popPos}
          >
            <div className="flex items-start justify-between gap-2 mb-2">
              <span className="font-semibold text-gray-800">{popup.label}</span>
              <div className="flex items-center gap-1 shrink-0">
                <button onClick={() => setExpandido((v) => !v)} className="text-gray-300 hover:text-blue-600" title={expandido ? 'Reducir' : 'Expandir'}>
                  {expandido ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
                </button>
                <button onClick={() => setPopup(null)} className="text-gray-300 hover:text-gray-600" title="Cerrar"><X size={15} /></button>
              </div>
            </div>
            <div className={`flex items-center gap-2 mb-2 ${expandido ? 'flex-wrap' : ''}`}>
              <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${ORIGEN_META[popup.info.origen]?.cls || 'bg-gray-100 text-gray-600'}`}>
                {ORIGEN_META[popup.info.origen]?.label || popup.info.origen}
              </span>
              {popup.valor != null && popup.valor !== '' && (
                <span className={`text-gray-500 ${expandido ? 'break-all' : 'truncate'}`}>valor: <b className="text-gray-700">{String(popup.valor)}</b></span>
              )}
            </div>
            <p className="text-gray-600 leading-snug whitespace-pre-wrap break-words">{popup.info.texto}</p>
          </div>
        </>
      )}

      {/* Chat de investigación: todo el ida y vuelta queda registrado; la IA lee y decide */}
      {chat && (
        <ChatConsulta
          regId={chat.regId} campo={chat.campo} apiBase={apiBase}
          onCerrar={() => setChat(null)}
          onAplicado={(r) => {
            // Refleja la escritura en la grilla (la celda deja de parpadear) reusando el guardado normal.
            // 3.2 A (excluible/obs): backend solo devuelve campo_decision si de verdad lo escribió.
            if (r.campo_decision) onSave?.(chat.regId, r.campo_decision, r.valor_decision)
            onSave?.(chat.regId, r.campo_razon, r.valor_razon)
            if (r.campo_extra) onSave?.(chat.regId, r.campo_extra, r.valor_extra)   // 3.2B: «Se ejecutó» = N/A
          }}
        />
      )}
    </div>
  )
}
