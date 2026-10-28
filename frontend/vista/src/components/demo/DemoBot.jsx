import { useState, useCallback, useRef, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '../../store/authStore'
import { Play, Pause, RotateCcw, X, Bot, Gauge } from 'lucide-react'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const SPEEDS = {
  lento:  { label: 'Lento',  factor: 1.8 },
  normal: { label: 'Normal', factor: 1.0 },
  rapido: { label: 'Rápido', factor: 0.5 },
}

const ROL_THEME = {
  supervisor:     { label: 'Jefe de Zona',      header: 'bg-blue-800',   badge: 'bg-blue-100 text-blue-800' },
  ingeniero_zona: { label: 'Ingeniero de Zona', header: 'bg-violet-700', badge: 'bg-violet-100 text-violet-800' },
  programador:    { label: 'Programador',       header: 'bg-emerald-700',badge: 'bg-emerald-100 text-emerald-800' },
  facturacion:    { label: 'Facturación / SGS', header: 'bg-amber-600',  badge: 'bg-amber-100 text-amber-800' },
  directivo:      { label: 'Directivo',         header: 'bg-indigo-800', badge: 'bg-indigo-100 text-indigo-800' },
  consulta:       { label: 'Consulta',          header: 'bg-slate-700',  badge: 'bg-slate-100 text-slate-700' },
  desarrollador:  { label: 'Desarrollador',     header: 'bg-gray-800',   badge: 'bg-gray-100 text-gray-800' },
}

// Construye el recorrido según el ROL y la CUENTA del usuario logueado.
// Ningún paso escribe en la base: solo navega, resalta y explica.
function buildTour(usuario) {
  const nombre = (usuario?.nombre || '').split(' ')[0]
  const rol = usuario?.rol
  const zona = usuario?.zona
  const esNoProgramados = usuario?.unidad === 'CHOCONTA'
  const uni = esNoProgramados ? 'No programados' : 'Programados'
  const hola = nombre ? `Hola ${nombre}.` : 'Hola.'
  const chat = { say: 'Un detalle nuevo: el botón de chat flotante (abajo a la derecha) sirve para enviarme sugerencias o reportar fallas, con captura incluida.' }
  const cierre = { say: 'Eso es todo el recorrido. Y lo importante: nada de esto se guardó — fue solo una demostración. Cuando quieras, lo repites.' }
  // Paso de exportar (todos pueden); plantilla = el Excel con el formato estándar
  const exportar = { page: '/exportar', el: 'exp-export', say: 'En "Exportar" bajas el reporte en Excel —por zona o el unificado con todas—. Ese mismo archivo es tu plantilla con el formato estándar.' }

  if (rol === 'ingeniero_zona') return [
    { say: `${hola} Soy tu guía. Te muestro qué puedes hacer como Ingeniero de Zona. Es una demostración: no se guarda ningún dato.` },
    { page: '/programacion', col: 'proceso', rows: 3, say: `Solo ves ${zona || 'tu zona'}. Tus columnas son las azules: proceso, subproceso, PMS, circuito, LCL, estado, municipio y alcance.` },
    { col: 'proceso', rows: 1, say: 'Editas como en Excel: doble clic en la celda, escribes y al salir se guarda solo. Aquí solo te lo muestro, sin guardar.' },
    { col: 'gestor_cliente', say: 'Las columnas verdes (Programador) y las de Facturación están bloqueadas para tu rol — cada quien llena lo suyo.' },
    exportar,
    { page: '/', el: 'dash-filtros', say: 'Y en el Dashboard ves el avance de tu zona, con filtros por período, líder o estado.' },
    cierre,
  ]

  if (rol === 'programador') return [
    { say: `${hola} Te muestro tu trabajo como Programador. Es una demostración: no se guarda nada.` },
    { page: '/programacion', col: 'gestor_cliente', rows: 3, say: 'Tus columnas son las verdes: gestor cliente, supervisor, valor día, MO proyectada, MO real y valor material.' },
    { col: 'gestor_cliente', rows: 1, say: 'Asignas los valores económicos de cada cuadrilla. Doble clic, escribes y se guarda al instante — sin botones de guardar.' },
    exportar,
    { page: '/', el: 'dash-filtros', say: 'En el Dashboard, las gráficas de MO proyectada vs real reflejan justo lo que cargas aquí.' },
    cierre,
  ]

  if (rol === 'facturacion') return [
    { say: `${hola} Te muestro tu trabajo como Facturación / SGS. Es una demostración: no se guarda nada.` },
    { page: '/programacion', col: 'estado_pdt', rows: 3, say: 'Tus columnas están a la derecha: estado PDT, proliquidado, liquidación SGS, estado facturación, OT y cumplimentación.' },
    { col: 'estado_pdt', rows: 1, say: 'Llevas cada proyecto por el ciclo de cobro hasta liquidarlo en SGS. Doble clic en la celda y se guarda solo.' },
    exportar,
    { page: '/', say: 'En el Dashboard, el pipeline de Facturación/SGS muestra en qué etapa va cada proyecto.' },
    cierre,
  ]

  if (rol === 'directivo') return [
    { say: `${hola} Como Directivo ves TODAS las unidades en modo consulta: el panorama completo. Es una demostración, no se guarda nada.` },
    { page: '/', say: 'Este es el Dashboard consolidado: el pulso de toda la operación en tiempo real.' },
    { el: 'dash-unidad', say: 'Aquí cambias de unidad —Programados o No programados— y cada gráfica se recalcula al instante.' },
    { el: 'dash-filtros', say: 'Con estos filtros (período, zona, líder, tipo, estado) recortas todo el tablero a lo que quieras analizar.' },
    { page: '/matriz', say: 'En "Producción por Cuadrilla" tienes la matriz cuadrilla×día: proyección contra ejecución y el % de cumplimiento de cada cuadrilla.' },
    { sel: 'a[href="/matriz"]', say: 'Dentro hay dos pestañas más: Dimensionamiento (requeridas vs activas) y el Roster de integrantes. Esos instrumentos sí los puedes llenar tú.' },
    { ...exportar, say: 'En "Exportar" descargas cualquier recorte en Excel para presentarlo o analizarlo aparte.' },
    chat,
    cierre,
  ]

  if (rol === 'consulta') return [
    { say: `${hola} Tu acceso es de consulta: ves todo el panorama, sin editar. Te muestro dónde está cada cosa.` },
    { page: '/', el: 'dash-filtros', say: 'Este es el Dashboard en tiempo real. Filtras por período, zona, líder o estado para enfocar las gráficas.' },
    { page: '/programacion', rows: 3, say: 'En Programación ves la grilla completa en modo solo lectura — para consultar, no para modificar.' },
    { ...exportar, say: 'Y en "Exportar" puedes descargar el reporte en Excel cuando lo necesites.' },
    cierre,
  ]

  // supervisor / desarrollador / fallback → recorrido completo (incluye plantillas + importación masiva)
  const titulo = rol === 'desarrollador' ? 'Desarrollador' : 'Jefe de Zona'
  const pasos = [
    { say: `${hola} Como ${titulo} tienes acceso completo a ${uni}. Es una demostración guiada: no se guarda nada.` },
    { page: '/', say: 'Empezamos en el Dashboard: tu operación en tiempo real, con tarjetas, pipeline y gráficas.' },
    { el: 'dash-filtros', say: 'Con la barra de filtros recortas todo el tablero por período, zona, líder, tipo o estado.' },
    { page: '/programacion', col: 'zona', say: 'La Programación es tu hoja de cálculo viva: cada fila es una cuadrilla en una fecha.' },
    { el: 'btn-nueva-fila', say: 'Con "Nueva fila" registras cuadrillas una a una — incluso varios líderes de golpe.' },
    { col: 'proceso', rows: 3, say: 'Las azules las llena el Ingeniero; las verdes, el Programador; y las de la derecha, Facturación/SGS. Tú puedes con todas.' },
    { page: '/matriz', say: 'En "Producción por Cuadrilla" tienes la matriz cuadrilla×día, el dimensionamiento y el roster de cuadrillas.' },
  ]
  // Plantillas Excel + importación masiva (tema clave para la unidad de no programados)
  if (esNoProgramados) {
    pasos.push({ page: '/exportar', el: 'exp-export', say: 'Importante: aunque en Chocontá manejas tu propio sistema, aquí también puedes trabajar con plantillas Excel. Descargas el reporte —ese archivo es la plantilla con el formato estándar—.' })
  } else {
    pasos.push({ page: '/exportar', el: 'exp-export', say: 'En "Exportar" bajas el reporte en Excel, por zona o unificado. Y ese mismo archivo te sirve de plantilla con el formato estándar.' })
  }
  pasos.push({ el: 'exp-importar', say: 'Y haces lo contrario: rellenas la plantilla con muchas filas y la subes aquí para cargarlas todas de una vez —importación masiva—, con modos inteligente, reemplazar zona o reemplazar todo.' })
  pasos.push(chat)
  pasos.push(cierre)
  return pasos
}

export default function DemoBot({ onClose }) {
  const navigate = useNavigate()
  const { usuario } = useAuthStore()
  const tour = useMemo(() => buildTour(usuario), [usuario])
  const total = tour.length

  const [status, setStatus] = useState('idle')
  const [speed, setSpeed] = useState('normal')
  const [text, setText] = useState('Pulsa "Iniciar" para un recorrido guiado según tu rol. Es una demostración: no se guarda ningún dato.')
  const [step, setStep] = useState(0)
  const [highlight, setHighlight] = useState(null)
  const abortRef = useRef(false)
  const speedRef = useRef(SPEEDS.normal.factor)

  const theme = ROL_THEME[usuario?.rol] || ROL_THEME.supervisor

  const setVel = (id) => { setSpeed(id); speedRef.current = SPEEDS[id].factor }

  const narrate = useCallback(async (msg) => {
    if (abortRef.current) throw new Error('ABORTED')
    const f = speedRef.current
    setText('')
    for (let i = 0; i <= msg.length; i++) {
      if (abortRef.current) throw new Error('ABORTED')
      setText(msg.slice(0, i))
      await sleep(24 * f)
    }
    await sleep(1600 * f)   // pausa para leer la frase completa
  }, [])

  const scrollToCol = useCallback((col) => {
    setTimeout(() => window._sabogalGridApi?.ensureColumnVisible(col, 'start'), 600)
  }, [])

  const setDemoRows = useCallback((ids) => {
    window._sabogalSetDemoRows?.(ids)
    if (ids.length === 1) {
      setTimeout(() => {
        const api = window._sabogalGridApi
        const node = api?.getRowNode(String(ids[0]))
        if (node) api.ensureNodeVisible(node, 'middle')
      }, 400)
    }
  }, [])

  const firstRowIds = useCallback((n) => {
    const api = window._sabogalGridApi
    if (!api) return []
    const out = []
    const count = api.getDisplayedRowCount()
    for (let i = 0; i < Math.min(n, count); i++) {
      const node = api.getDisplayedRowAtIndex(i)
      if (node?.data?.id != null) out.push(node.data.id)
    }
    return out
  }, [])

  const ringOn = useCallback((el, label) => {
    if (!el) { setHighlight(null); return }
    el.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
    const r = el.getBoundingClientRect()
    setHighlight({ rect: { top: r.top, left: r.left, width: r.width, height: r.height }, label })
  }, [])

  const highlightEl = useCallback((id, label) => ringOn(document.getElementById(id), label), [ringOn])

  const highlightSel = useCallback((selector, label) => {
    let chosen = null
    document.querySelectorAll(selector).forEach((e) => {
      if (chosen) return
      const rr = e.getBoundingClientRect()
      if (rr.width > 0 && rr.height > 0) chosen = e
    })
    ringOn(chosen, label)
  }, [ringOn])

  const clearHighlight = useCallback(() => setHighlight(null), [])

  const go = useCallback(async () => {
    abortRef.current = false
    setStatus('running')
    let i = 0
    try {
      for (const s of tour) {
        if (abortRef.current) throw new Error('ABORTED')
        setStep(++i)
        if (s.page) { navigate(s.page); await sleep(1000 * speedRef.current) }
        if (s.rows) {
          let ids = firstRowIds(s.rows)
          if (!ids.length) { await sleep(900); ids = firstRowIds(s.rows) }
          setDemoRows(ids)
        }
        if (s.col) scrollToCol(s.col)
        if (s.el) highlightEl(s.el, s.label)
        if (s.sel) highlightSel(s.sel, s.label)
        await narrate(s.say)
        clearHighlight()
      }
      setDemoRows([])
      setStatus('done')
    } catch (e) {
      if (e.message !== 'ABORTED') console.error('Demo error:', e)
      setStatus('idle')
    }
  }, [tour, navigate, narrate, scrollToCol, setDemoRows, firstRowIds, highlightEl, highlightSel, clearHighlight])

  const handlePause = () => {
    abortRef.current = true
    setStatus('idle')
    clearHighlight()
    setDemoRows([])
  }

  const handleReset = () => {
    abortRef.current = true
    setStep(0)
    setStatus('idle')
    setHighlight(null)
    setDemoRows([])
    setText('Pulsa "Iniciar" para un recorrido guiado según tu rol. Es una demostración: no se guarda ningún dato.')
  }

  const repetir = () => { handleReset(); setTimeout(go, 120) }

  return (
    <>
      {highlight && createPortal(
        <div className="fixed pointer-events-none z-[9998]"
          style={{ top: highlight.rect.top - 6, left: highlight.rect.left - 6, width: highlight.rect.width + 12, height: highlight.rect.height + 12 }}>
          <div className="w-full h-full rounded-lg ring-4 ring-offset-1 ring-red-500 animate-pulse" />
          {highlight.label && (
            <div className="absolute top-full left-1/2 -translate-x-1/2 mt-2 bg-red-600 text-white text-xs px-2 py-0.5 rounded whitespace-nowrap shadow-lg">{highlight.label}</div>
          )}
        </div>,
        document.body
      )}

      <div className="fixed bottom-16 inset-x-0 sm:bottom-6 sm:left-auto sm:right-6 sm:w-[390px] z-50 rounded-t-2xl sm:rounded-2xl shadow-2xl overflow-hidden border border-gray-200 bg-white">
        <div className={`${theme.header} px-4 py-3 flex items-center justify-between transition-colors duration-700`}>
          <div className="flex items-center gap-2 text-white">
            <Bot size={17} />
            <span className="font-semibold text-sm">Tutorial guiado</span>
          </div>
          <button onClick={onClose} className="text-white/60 hover:text-white transition-colors"><X size={15} /></button>
        </div>

        <div className="px-4 pt-3 pb-1 flex items-center gap-2">
          <span className="text-xs text-gray-400">Tu rol:</span>
          <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${theme.badge}`}>{theme.label}</span>
          <span className="ml-auto text-xs text-gray-400">{step} / {total}</span>
        </div>
        <div className="px-4 pb-2">
          <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
            <div className="h-full bg-blue-500 rounded-full transition-all duration-500" style={{ width: `${(step / total) * 100}%` }} />
          </div>
        </div>

        {/* Velocidad */}
        <div className="px-4 pb-2 flex items-center gap-2">
          <span className="flex items-center gap-1 text-[11px] text-gray-400"><Gauge size={12} /> Velocidad:</span>
          <div className="flex gap-0.5 bg-gray-100 p-0.5 rounded-lg">
            {Object.entries(SPEEDS).map(([id, s]) => (
              <button key={id} onClick={() => setVel(id)}
                className={`px-2 py-0.5 text-[11px] rounded-md transition ${speed === id ? 'bg-white text-blue-700 font-semibold shadow-sm' : 'text-gray-500 hover:text-gray-800'}`}>
                {s.label}
              </button>
            ))}
          </div>
        </div>

        <div className="px-4 py-3 min-h-[92px] border-t border-gray-100 bg-gray-50">
          <p className="text-sm text-gray-700 leading-relaxed">
            {text}
            {status === 'running' && <span className="animate-pulse text-blue-400 ml-0.5">▌</span>}
          </p>
        </div>

        <div className="px-4 py-2 border-t border-gray-100 bg-gray-50/60">
          <p className="text-[11px] text-gray-400">Es solo una demostración — no se crea ni se guarda ningún dato.</p>
        </div>

        <div className="px-4 py-3 bg-white border-t flex gap-2">
          {status !== 'running' && (
            <button onClick={handleReset} className="flex items-center gap-1 px-3 py-2 border rounded-lg text-xs text-gray-500 hover:bg-gray-50"><RotateCcw size={12} /></button>
          )}
          {status === 'idle' && (
            <button onClick={go} className="flex-1 flex items-center justify-center gap-2 bg-blue-700 text-white py-2 rounded-lg text-sm font-medium hover:bg-blue-800">
              <Play size={14} /> Iniciar recorrido
            </button>
          )}
          {status === 'running' && (
            <button onClick={handlePause} className="flex-1 flex items-center justify-center gap-2 bg-gray-600 text-white py-2 rounded-lg text-sm font-medium hover:bg-gray-700">
              <Pause size={14} /> Pausar
            </button>
          )}
          {status === 'done' && (
            <button onClick={repetir} className="flex-1 flex items-center justify-center gap-2 bg-emerald-700 text-white py-2 rounded-lg text-sm font-medium hover:bg-emerald-800">
              <RotateCcw size={14} /> Repetir recorrido
            </button>
          )}
        </div>
      </div>
    </>
  )
}
