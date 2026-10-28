import { useMemo, useCallback, useState, useRef, useEffect, useLayoutEffect, forwardRef, useImperativeHandle } from 'react'
import { AgGridReact } from 'ag-grid-react'
import { Search, X, Sheet, Shrink, Filter as FilterIcon, History } from 'lucide-react'
import ExcelSetFilter from './ExcelSetFilter'

// Popup pegado a una celda que nunca se sale de la pantalla (mismo hook del grid del ANS)
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
      if (h <= holguraAbajo) top = ancla.bottom + m
      else if (h <= holguraArriba) top = ancla.top - m - h
      else top = Math.max(m, vh - m - h)
      setPos({ left: Math.min(Math.max(m, ancla.left), Math.max(m, vw - w - m)), top })
    }
    colocar()
    const ro = new ResizeObserver(colocar)
    ro.observe(el)
    window.addEventListener('resize', colocar)
    return () => { ro.disconnect(); window.removeEventListener('resize', colocar) }
  }, [ancla, activo])
  const style = pos || { left: -9999, top: -9999 }
  return [ref, style]
}

// ── Por qué la celda tiene ese valor: la explicación se arma con lo que ya trae la fila
//    (procedencia, avisos y los cálculos del backend) — sin inventar nada. ──
const NOMBRE_ORIGEN = {
  import: 'Importación directa del Excel de Enel',
  bolsa: 'Lo llenó el sistema desde la bolsa',
  regla_h: 'Regla automática (según el PDL)',
  regla_p: 'Regla automática (según el municipio)',
  manual: 'Corregido a mano',
}

function justificar(fila, campo, label) {
  const val = fila?.[campo]
  const origen = fila?.procedencia?.[campo]
  const lineas = []

  if (origen === 'import') {
    lineas.push('El dato vino así en el Excel de Enel (BASE CONTROL_NNDD) y se copió tal cual.')
  } else if (origen === 'bolsa') {
    lineas.push('La celda venía VACÍA en el archivo de Enel. El sistema buscó el radicado en la bolsa NNDD, lo encontró, y tomó el dato de allá — por eso la celda va en naranja.')
    lineas.push('Si un archivo más nuevo trae el dato real, este valor se reemplaza y el naranja desaparece.')
  } else if (origen === 'regla_h') {
    const pdl = String(fila?.pdl || '')
    const tipo = /^CDOPPI/i.test(pdl) ? 'CDOPPI' : /^CDOPT/i.test(pdl) ? 'CDOPT' : /^CDOPP\d/i.test(pdl) ? 'CDOPP' : 'desconocido'
    lineas.push(`Se calculó a partir del PDL «${pdl}» (columna I): es de tipo ${tipo}.`)
    lineas.push('La regla: CDOPP puro → SI · CDOPT → NO · CDOPPI → NO (pendiente de confirmar con Marl). Sin PDL, la celda queda vacía.')
  } else if (origen === 'regla_p') {
    lineas.push(`El municipio «${fila?.municipio || ''}» (columna G) está relacionado con la zona ${val} en el mapeo editable (botón «Zonas por municipio»).`)
  } else if (origen === 'manual') {
    lineas.push('Lo corrigió una persona a mano — por eso va en amarillo. Las cargas de archivo NUNCA pisan una celda manual.')
  }

  // Columnas de fórmula: la cuenta exacta, número por número, como la hará el Excel
  const m = fila?.dias_ejecucion
  const n = fila?.dias_con_exclusion
  if (campo === 'dias_ejecucion') {
    lineas.push(m == null
      ? 'Sin las dos fechas (entrega y maniobra) la fórmula no puede calcular.'
      : `= NETWORKDAYS(Fecha entrega, Fecha maniobra): días hábiles del ${verFecha(fila.fecha_entrega)} al ${verFecha(fila.fecha_maniobra)}, ambos inclusive, descontando fines de semana y festivos de Colombia = ${m} días.`)
    lineas.push('Es la columna M del Excel: en el archivo exportado va como fórmula viva.')
  }
  if (campo === 'dias_con_exclusion') {
    lineas.push(n == null
      ? 'Depende de «Días de ejecución», que aún no se puede calcular.'
      : `= Días de ejecución (${m}) − Días de exclusión (${fila.dias_exclusion || 0}) = ${n}.`)
    lineas.push('Es la columna N del Excel.')
  }
  if (campo === 'cumple') {
    if (n == null) {
      lineas.push('Sin fechas no hay cálculo de cumplimiento.')
    } else {
      const conAf = String(fila?.afectacion || '').toUpperCase() === 'SI'
      const tope = conAf ? 15 : 10
      lineas.push(`Afectación clientes = ${conAf ? 'SI → el tope es 15 días' : (fila?.afectacion || 'vacía') + ' → el tope es 10 días'}.`)
      lineas.push(`Días con exclusión = ${n} · ${n} ${n <= tope ? '≤' : '>'} ${tope} → ${val}.`)
    }
    lineas.push('Es la columna O del Excel, con la fórmula exacta del formato oficial.')
  }

  if (!origen && !['dias_ejecucion', 'dias_con_exclusion', 'cumple'].includes(campo)) {
    lineas.push(val == null || val === ''
      ? 'La celda está vacía: ni el archivo de Enel ni la bolsa trajeron este dato, y ninguna regla aplica. El sistema no inventa valores.'
      : 'Sin procedencia registrada (dato anterior al registro de cargas).')
  }

  const avisos = (campo === 'radicado' || campo === 'pdl' || campo.startsWith('fecha')) ? (fila?.avisos || []) : []
  return { titulo: label, valor: campo.startsWith('fecha') ? verFecha(val) : (val ?? '—'), origen, lineas, avisos }
}

// Grid del subtab «3.Programados»: la MISMA experiencia del Registros (data) del ANS —
// AG Grid con el autofiltro tipo Excel por columna, navegación con flechas, Enter baja,
// F2 / doble-clic edita, Ctrl+Z deshace, Modo Excel a pantalla completa y búsqueda rápida.
// Columnas = las de la hoja 5.2 del formato oficial, en su orden; M, N y O son la vista previa de
// las fórmulas del export (las calcula el backend con los mismos festivos del Excel).

const ZONAS = ['UBATE', 'ALMEIDAS', 'GUAVIO', 'LV']

// dd/mm/yyyy visible; en datos viaja ISO (YYYY-MM-DD)
const verFecha = (v) => {
  if (!v) return ''
  const [y, m, d] = String(v).slice(0, 10).split('-')
  return (y && m && d) ? `${d}/${m}/${y}` : String(v)
}

// Editor de fecha para AG Grid v32 (reactiveCustomComponents): el grid commitea lo último
// que se avisó por onValueChange — regla de esta casa: cada cambio del valor deseado se
// avisa al proxy (ver el FechaEditor del ANS y su historia de fechas borradas).
const DateEditor = forwardRef((props, ref) => {
  const inicial = useRef(props.value || '').current
  const [v, setV] = useState(inicial ? String(inicial).slice(0, 10) : '')
  const vRef = useRef(v)
  const inputRef = useRef(null)
  const set = (nv) => {
    vRef.current = nv
    setV(nv)
    props.onValueChange?.(nv === '' ? null : nv)
  }
  useEffect(() => { inputRef.current?.focus(); try { inputRef.current?.showPicker() } catch { /* sin picker */ } }, [])
  useImperativeHandle(ref, () => ({ getValue: () => (vRef.current === '' ? null : vRef.current) }))
  return (
    <input ref={inputRef} type="date" value={v}
      onChange={(e) => set(e.target.value)}
      onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); props.stopEditing?.() } }}
      className="w-full h-full px-1 text-xs border-0 outline-none" />
  )
})
DateEditor.displayName = 'DateEditor'

export default function ProgramadosGrid({ items, onSave, onHistorial, historialDe, readOnly, titulo }) {
  const gridApi = useRef(null)

  // ── Modo justificación: clic en una celda → por qué tiene ese valor ────────────
  const [modo, setModo] = useState(false)
  const [popup, setPopup] = useState(null)   // {ancla, info, cambio}

  const onCellClicked = useCallback(async (e) => {
    if (!modo || !e.data) return
    const campo = e.colDef.field || e.colDef.colId
    if (!campo || campo === '_hist') return
    const celda = (e.event?.target)?.closest?.('.ag-cell')
    const r = celda?.getBoundingClientRect()
    const ancla = r ? { left: r.left, top: r.top, bottom: r.bottom }
      : { left: e.event?.clientX ?? 200, top: e.event?.clientY ?? 200, bottom: e.event?.clientY ?? 200 }
    const info = justificar(e.data, campo, e.colDef.headerName || campo)
    setPopup({ ancla, info, cambio: undefined })
    // El último cambio de ESTA columna (qué carga o quién lo puso, y qué había antes)
    if (historialDe) {
      try {
        const cambios = await historialDe(e.data.id)
        const c = (cambios || []).find((x) => x.campo === campo)
        setPopup((p) => (p && p.info === info ? { ...p, cambio: c || null } : p))
      } catch { setPopup((p) => (p && p.info === info ? { ...p, cambio: null } : p)) }
    }
  }, [modo, historialDe])

  const [popRef, popPos] = useFlotante(popup?.ancla, !!popup)

  // ── Columnas de la 5.2, con su letra en el título (así se habla con el Excel) ──
  const columnDefs = useMemo(() => {
    const editable = !readOnly
    const origen = (campo) => (p) => {
      const o = p.data?.procedencia?.[campo]
      if (o === 'bolsa') return { backgroundColor: '#fed7aa' }                       // naranja: lo resolvió la máquina desde la bolsa
      if (o === 'manual') return { backgroundColor: '#fef9c3' }                     // amarillo: corregido a mano (las cargas no lo pisan)
      return null
    }
    const C = (campo, letra, nombre, ancho, extra = {}) => ({
      field: campo, colId: campo, headerName: `${letra} · ${nombre}`, width: ancho,
      editable, cellStyle: origen(campo),
      tooltipValueGetter: (p) => {
        const o = p.data?.procedencia?.[campo]
        const partes = []
        if (o) partes.push({ import: 'Del archivo de Enel', bolsa: 'Lo llenó el sistema desde la bolsa', regla_h: 'Regla automática (según el PDL)', regla_p: 'Regla automática (según el municipio)', mnl: '', manual: 'Corregido a mano — las cargas no lo pisan' }[o] || o)
        if (campo === 'radicado' && p.data?.avisos?.length) partes.push(...p.data.avisos)
        return partes.join('\n') || undefined
      },
      ...extra,
    })
    const F = (campo, letra, nombre, ancho) => C(campo, letra, nombre, ancho, {
      cellEditor: DateEditor, valueFormatter: (p) => verFecha(p.value),
    })
    // Columnas calculadas (fórmulas del export): solo lectura, gris, con su cuenta en el tooltip
    const X = (campo, letra, nombre, ancho, extra = {}) => ({
      field: campo, colId: campo, headerName: `${letra} · ${nombre}`, width: ancho, editable: false,
      headerTooltip: 'Columna de fórmula: se calcula sola, igual que en el Excel exportado',
      cellStyle: (p) => ({ backgroundColor: '#f3f4f6', fontStyle: 'italic', ...(extra.color?.(p) || {}) }),
    })
    return [
      {
        colId: '_hist', headerName: '', width: 36, pinned: 'left', editable: false,
        sortable: false, filter: false, suppressHeaderMenuButton: true,
        cellRenderer: (p) => (
          <button onClick={() => onHistorial?.(p.data)} title="De dónde salió cada dato de esta fila"
            className="text-gray-300 hover:text-sky-600 mt-1.5"><History size={13} /></button>
        ),
      },
      C('radicado', 'A', 'Radicado', 110, {
        pinned: 'left', editable: false,
        cellStyle: (p) => (p.data?.avisos?.length ? { color: '#b45309', fontWeight: 600 } : null),
      }),
      C('lcl', 'B', 'LCL', 110),
      C('eecc', 'C', 'EECC', 95),
      C('proyecto', 'D', 'Proyecto', 210),
      F('fecha_entrega', 'E', 'Fecha entrega a EECC', 125),
      C('direccion', 'F', 'Direccion', 210),
      C('municipio', 'G', 'Municipio', 120),
      C('afectacion', 'H', 'Afectación clientes', 105, {
        cellEditor: 'agSelectCellEditor', cellEditorParams: { values: ['', 'SI', 'NO'] },
        cellStyle: (p) => ({ ...(origen('afectacion')(p) || {}), ...(p.value === 'SI' ? { color: '#15803d', fontWeight: 600 } : {}) }),
      }),
      C('pdl', 'I', 'PDL/ID', 145),
      F('fecha_maniobra', 'J', 'Fecha maniobra', 120),
      C('dias_exclusion', 'K', 'Días de exclusión', 100),
      C('obs_retraso', 'L', 'Observación retraso', 190),
      X('dias_ejecucion', 'M', 'Días de ejecución', 100),
      X('dias_con_exclusion', 'N', 'Días con exclusión', 105),
      X('cumple', 'O', 'Cumple', 105, {
        color: (p) => (p.value === 'CUMPLE' ? { color: '#15803d', fontWeight: 700 }
          : p.value === 'NO CUMPLE' ? { color: '#b91c1c', fontWeight: 700 } : {}),
      }),
      C('zona', 'P', 'ZONA', 105, {
        cellEditor: 'agSelectCellEditor', cellEditorParams: { values: ['', ...ZONAS] },
      }),
      C('excluible2', 'Q', 'Excluible?2', 95),
      C('excluible3', 'R', 'Excluible?3', 210),
    ]
  }, [readOnly, onHistorial])

  const nombresCol = useMemo(() => {
    const m = {}
    columnDefs.forEach((c) => { m[c.colId || c.field] = c.headerName })
    return m
  }, [columnDefs])

  const defaultColDef = useMemo(() => ({
    resizable: true, sortable: true,
    filter: ExcelSetFilter,          // autofiltro tipo Excel: casillas por valor + buscador
    suppressHeaderMenuButton: true,
    cellDataType: false,             // evita que columnas vacías no abran el editor (AG Grid v32)
  }), [])

  const onCellValueChanged = useCallback((e) => {
    const field = e.colDef.field
    if (!field) return
    let valor = e.newValue === '' || e.newValue === undefined ? null : e.newValue
    if (field === 'dias_exclusion' && valor !== null) {
      const n = parseInt(valor, 10)
      valor = Number.isNaN(n) ? null : n
    }
    onSave(e.data.id, field, valor)
  }, [onSave])

  // ── Modo Excel + búsqueda rápida + chips de filtros (mismo patrón del ANS) ─────
  const [excel, setExcel] = useState(false)
  const [quick, setQuick] = useState('')
  const quickRef = useRef(null)
  const [nFilas, setNFilas] = useState(null)
  const [filtrosCol, setFiltrosCol] = useState({})

  const wrapRef = useRef(null)
  const [alto, setAlto] = useState(null)
  useLayoutEffect(() => {
    if (excel) return
    const medir = () => {
      const el = wrapRef.current
      if (!el) return
      setAlto(Math.max(420, window.innerHeight - el.getBoundingClientRect().top - 14))
    }
    medir()
    const ro = new ResizeObserver(medir)
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
  const gridContext = useMemo(() => ({ onColumnFilterChanged }), [onColumnFilterChanged])
  const limpiarFiltroCol = useCallback((colKey) => {
    const api = gridApi.current
    if (!api) return
    api.setColumnFilterModel(colKey, null).then(() => api.onFilterChanged())
  }, [])
  const limpiarFiltrosCol = useCallback(() => { gridApi.current?.setFilterModel(null) }, [])

  // Esc cierra lo más "cercano" primero: popup de justificación → búsqueda → lo que gestione
  // AG Grid (edición/filtro) → salir del Modo Excel
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape') return
      if (popup) { setPopup(null); return }
      if (!excel) return
      if (document.activeElement === quickRef.current && quick) { setQuick(''); return }
      const api = gridApi.current
      if (api?.getEditingCells?.().length) return
      if (document.querySelector('.ag-popup')) return
      setExcel(false)
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [excel, quick, popup])

  return (
    <div className={excel ? 'fixed inset-0 z-[60] bg-gray-100 flex flex-col p-2 md:p-3' : 'relative'}>
      <div className="flex flex-wrap items-center gap-2 mb-1.5 shrink-0">
        {excel && titulo && <span className="text-sm font-bold text-gray-800 mr-1">{titulo}</span>}
        <button
          onClick={() => setExcel((v) => !v)}
          className={`flex items-center gap-1.5 text-xs font-semibold px-2.5 py-1 rounded-lg border transition-colors text-white ${
            excel ? 'bg-emerald-800 border-emerald-800 hover:bg-emerald-900' : 'bg-emerald-600 border-emerald-600 hover:bg-emerald-700'}`}
          title={excel ? 'Volver a la vista normal (Esc)' : 'La tabla ocupa toda la pantalla, con filtros y navegación como en Excel'}>
          {excel ? <Shrink size={13} /> : <Sheet size={13} />}
          {excel ? 'Salir de Modo Excel' : 'Modo Excel'}
          {excel && <span className="text-[9px] uppercase tracking-wide opacity-70">Esc</span>}
        </button>
        <div className="relative">
          <Search size={13} className="absolute left-2 top-1.5 text-gray-400" />
          <input ref={quickRef} value={quick} onChange={(e) => setQuick(e.target.value)}
            placeholder="Buscar en toda la tabla…"
            className="pl-6 pr-6 py-0.5 text-xs border border-gray-300 rounded-lg w-52 focus:outline-none focus:ring-1 focus:ring-emerald-500" />
          {quick && (
            <button onClick={() => setQuick('')} className="absolute right-1.5 top-1 text-gray-400 hover:text-gray-700"><X size={13} /></button>
          )}
        </div>
        <button
          onClick={() => { setModo((v) => !v); setPopup(null) }}
          className={`flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-lg border transition-colors ${
            modo ? 'bg-blue-700 text-white border-blue-700' : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50'}`}
          title="Muestra de dónde salió el valor de cada celda">
          <Search size={13} /> {modo ? 'Justificación activa — clic en una celda' : 'Justificar datos'}
        </button>
        {modo && <span className="text-[11px] text-gray-400">Pulsa una celda para ver por qué tiene ese valor · Esc cierra</span>}

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
          <span className="hidden lg:inline mr-3">Enter baja · F2 o doble-clic edita · Ctrl+Z deshace</span>
          {nFilas != null && nFilas !== items.length ? `${nFilas} de ${items.length} filas` : `${items.length} filas`}
        </span>
      </div>

      <div ref={wrapRef} className={`ag-theme-alpine ${excel ? 'flex-1 min-h-0' : ''}`}
        style={excel ? { width: '100%' } : { width: '100%', height: alto ? `${alto}px` : '68vh' }}>
        <AgGridReact
          rowData={items}
          columnDefs={columnDefs}
          defaultColDef={defaultColDef}
          context={gridContext}
          onGridReady={(p) => { gridApi.current = p.api }}
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
          headerHeight={30}
          rowHeight={30}
          tooltipShowDelay={400}
        />
      </div>

      {/* Popup de justificación: al clic en otra celda se mueve; Esc o X cierran */}
      {popup && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setPopup(null)} />
          <div ref={popRef}
            className="fixed z-50 w-[22rem] max-h-[75vh] overflow-auto bg-white border border-blue-300 rounded-lg shadow-2xl text-xs"
            style={popPos} onClick={(e) => e.stopPropagation()}>
            <div className="sticky top-0 flex items-center justify-between px-3 py-2 bg-blue-50 border-b border-blue-200">
              <span className="font-semibold text-blue-900">{popup.info.titulo}</span>
              <button onClick={() => setPopup(null)} className="text-gray-400 hover:text-gray-700"><X size={14} /></button>
            </div>
            <div className="p-3 space-y-2">
              <p><span className="text-gray-500">Valor:</span> <b>{String(popup.info.valor)}</b>
                {popup.info.origen && (
                  <span className={`ml-2 px-1.5 py-0.5 rounded text-[10px] ${
                    { import: 'bg-sky-100 text-sky-800', bolsa: 'bg-orange-200 text-orange-900',
                      regla_h: 'bg-violet-100 text-violet-800', regla_p: 'bg-emerald-100 text-emerald-800',
                      manual: 'bg-yellow-100 text-yellow-800' }[popup.info.origen] || 'bg-gray-100 text-gray-600'}`}>
                    {NOMBRE_ORIGEN[popup.info.origen] || popup.info.origen}
                  </span>
                )}
              </p>
              {popup.info.lineas.map((l, i) => <p key={i} className="text-gray-700 leading-relaxed">{l}</p>)}
              {popup.info.avisos.length > 0 && (
                <div className="text-[11px] text-amber-800 bg-amber-50 border border-amber-200 rounded p-2">
                  {popup.info.avisos.map((a, i) => <p key={i}>· {a}</p>)}
                </div>
              )}
              {popup.cambio === undefined && historialDe && <p className="text-gray-400">Buscando el último cambio…</p>}
              {popup.cambio && (
                <div className="border-t pt-2 text-gray-600">
                  <p className="font-medium text-gray-700 mb-0.5">Último cambio de esta celda</p>
                  <p>{(popup.cambio.creado_en || '').replace('T', ' ').slice(0, 16)}
                    {popup.cambio.carga_id ? ` · carga #${popup.cambio.carga_id}` : ''} ·{' '}
                    {NOMBRE_ORIGEN[popup.cambio.procedencia] || popup.cambio.procedencia}</p>
                  <p>Antes: <b>{popup.cambio.valor_anterior || '—'}</b> → Después: <b>{popup.cambio.valor_nuevo || '—'}</b></p>
                  {popup.cambio.motivo && <p className="text-gray-500">{popup.cambio.motivo}</p>}
                </div>
              )}
              {popup.cambio === null && (
                <p className="border-t pt-2 text-gray-400">Sin cambios registrados para esta celda.</p>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
