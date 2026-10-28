import { useCallback, useRef, useMemo, useState, useEffect } from 'react'
import { AgGridReact } from 'ag-grid-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../utils/api'
import { useAuthStore } from '../../store/authStore'
import { formatCOP } from '../../utils/format'
import toast from 'react-hot-toast'
import { Plus, RefreshCw, Search, X, Trash2, Copy, Building2, Upload, Info, FileDown, Link2, History } from 'lucide-react'
import ModalNuevaFila from './ModalNuevaFila'
import HistorialProgramacionModal from './HistorialProgramacionModal'
import { confirmarUnidadAjena } from '../../utils/unidadAjena'
import RelacionSgsModal from './RelacionSgsModal'
import ExcelSetFilter from './ExcelSetFilter'
import { MantillaDetalleModal, MantillaResultadoModal, PdtCell } from './MantillaBits'
import { unidadCfg, esRolGlobal, esSoloLectura, ORDEN_UNIDADES, UNIDADES } from '../../config/unidades'
import { esNoLaborable } from '../../utils/festivos'

const CAMPOS_ROL = {
  supervisor:     null,
  // se_gestiona es la columna ROJA de la plantilla: la editan TODOS los roles
  ingeniero_zona: ['fecha','lider','tipo_grupo','proceso','subproceso','proyecto_antecedente','pms','circuito','lcl','fecha_vigencia','tipo_pdt','num_pdt','estado','cd_afectados','saidi','hora','hora_ab','hora_cr','municipio','vereda','alcance','restricciones','se_gestiona'],
  programador:    ['gestor_cliente','supervisor','valor_dia','mo_proyectada','mo_real','mo_facturada','valor_dejado_ejecutar','valor_material','se_gestiona'],
  facturacion:    ['estado_pdt','notas','num_proliquidado','fecha_liquidacion_sgs','estado_facturacion','fecha_envio_facturacion','ot','estado_cumplimentacion','inoperatividad','inoperatividad_tipo','observaciones_inoperatividad','se_gestiona'],
}

const INGE_FIELDS = new Set(['fecha','lider','tipo_grupo','proceso','subproceso','proyecto_antecedente','pms','circuito','lcl','fecha_vigencia','tipo_pdt','num_pdt','estado','cd_afectados','saidi','hora','hora_ab','hora_cr','municipio','vereda','alcance','restricciones','se_gestiona'])
const PROG_FIELDS  = new Set(['gestor_cliente','supervisor','valor_dia','mo_proyectada','mo_real','mo_facturada','valor_dejado_ejecutar','valor_material'])

const ZONAS          = ['ALMEIDAS','GUAVIO','UBATE','LV&TLC']
const TIPOS          = ['CAMP5','CAMP3','ARRIME','GRUA','ESTACA','LVL3','MIE']
const ESTADO_OPTIONS     = ['PROGRAMADO','EJECUTADO','PARCIAL','CANCELADO','REPROGRAMADO']
const ESTADO_PDT_OPTIONS = ['PROGRAMADO','EJECUTADO','PARCIAL','CANCELADO','ANULADO','']

function EstadoBadge({ value }) {
  const cls = {
    EJECUTADO:   'bg-green-100 text-green-800',
    PARCIAL:     'bg-yellow-100 text-yellow-800',
    CANCELADO:   'bg-red-100 text-red-800',
    PROGRAMADO:  'bg-blue-100 text-blue-800',
    REPROGRAMADO:'bg-purple-100 text-purple-800',
  }
  if (!value) return null
  return <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${cls[value] || 'bg-gray-100 text-gray-700'}`}>{value}</span>
}

// Formatea fecha ISO "YYYY-MM-DD" → "DD/MM/YYYY" sin pasar por Date()
// (evita el corrimiento de un día por zona horaria UTC→Colombia)
const fmtFecha = (v) => {
  if (!v) return ''
  const [y, m, d] = String(v).slice(0, 10).split('-')
  return (y && m && d) ? `${d}/${m}/${y}` : String(v)
}

// Colores de cabecera idénticos al Excel (plantilla 2026-08)
const H_BLUE   = 'hdr-blue text-xs'    // #0070C0 — planificación / ingeniero
const H_GREEN  = 'hdr-green text-xs'   // #92D050 — programación / estado / PDT
const H_LBLUE  = 'hdr-lblue text-xs'   // #00B0F0 — facturación / liquidación
const H_SLATE  = 'hdr-slate text-xs'   // gris azulado — V. dejado, notas, inoperatividad
const H_RED    = 'hdr-red text-xs'     // ROJO — SE GESTIONA SI O NO (todos los roles)

export default function ProgramacionGrid({ zona, mes, anio, onUnidadChange }) {
  const { usuario } = useAuthStore()
  const qc           = useQueryClient()
  const gridRef      = useRef()
  const [showModal, setShowModal]       = useState(false)
  const [showSgs, setShowSgs]           = useState(false)
  const [quickFilter, setQuickFilter]   = useState('')
  const [estadoFilter, setEstadoFilter] = useState('')

  // ── Unidad operativa (SABOGAL / CHOCONTA) ───────────────────────────────────
  const esGlobal  = esRolGlobal(usuario?.rol)       // ve todas las unidades + puede cambiar
  const readOnly  = esSoloLectura(usuario?.rol)     // consulta / directivo: no edita
  const [unidadActiva, setUnidadActiva] = useState(usuario?.unidad || 'SABOGAL')
  const cfg = unidadCfg(unidadActiva)
  useEffect(() => { onUnidadChange?.(unidadActiva) }, [unidadActiva, onUnidadChange])

  // Aviso de unidad AJENA (coordinadores, 2026-09-07): antes de la primera escritura sobre la
  // unidad del otro sale un confirm; aceptado una vez, no se repite en la sesión (sessionStorage).
  // Si lo rechaza, se silencia 2 s para que un pegado de muchas celdas no dispare N popups.
  const guardarUnidad = useCallback(() => confirmarUnidadAjena(usuario, unidadActiva), [usuario, unidadActiva])
  const guardRef = useRef(guardarUnidad)
  useEffect(() => { guardRef.current = guardarUnidad }, [guardarUnidad])
  const rechazoHastaRef = useRef(0)
  const [showHist, setShowHist] = useState(false)

  // Solo-lectura ⇒ ningún campo editable; el resto, según su rol
  const camposEditables = readOnly ? [] : CAMPOS_ROL[usuario?.rol]
  const canDelete = !readOnly && (usuario?.rol === 'supervisor' || usuario?.rol === 'ingeniero_zona')
  const canCrear  = !readOnly && (usuario?.rol === 'supervisor' || usuario?.rol === 'ingeniero_zona')

  // Profesional de apoyo de zona: VE todas las zonas pero solo
  // EDITA la suya — las filas de otras zonas se pintan bloqueadas y no aceptan edición,
  // borrado ni pegado (el backend igual lo rechaza con _check_zona_access).
  const esApoyo = !readOnly && !!usuario?.es_apoyo_ans
  const zonaPropia = usuario?.zona
  const puedeFila = useCallback(
    (data) => !esApoyo || !data || data.zona === zonaPropia,
    [esApoyo, zonaPropia],
  )
  const puedeFilaRef = useRef(puedeFila)
  useEffect(() => { puedeFilaRef.current = puedeFila }, [puedeFila])

  // Plantilla Mantilla (solo unidad CHOCONTA)
  const mantillaRef = useRef()
  const [mantillaDet, setMantillaDet] = useState(null)   // popup desglose # PDT
  const [mantillaRes, setMantillaRes] = useState(null)   // resultado de importación
  const [impMantilla, setImpMantilla] = useState(false)

  const onImportMantilla = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    if (!guardRef.current()) { if (mantillaRef.current) mantillaRef.current.value = ''; return }
    setImpMantilla(true)
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('unidad', unidadActiva)
      const { data } = await api.post('/mantilla/import', form, { headers: { 'Content-Type': 'multipart/form-data' } })
      qc.invalidateQueries({ queryKey: ['programaciones'] })
      setMantillaRes(data)
      toast.success(`${data.actualizadas} días actualizados`)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Error al importar la plantilla')
    } finally {
      setImpMantilla(false)
      if (mantillaRef.current) mantillaRef.current.value = ''
    }
  }

  const [selectedIds,   setSelectedIds]   = useState([])
  const [confirmDelete, setConfirmDelete] = useState(false)

  // ── Filtros de columna (pre-filtrado en React — no depende del API interno de AG Grid) ──
  const [columnFilters, setColumnFilters] = useState({})
  const colFilterKeyOf = (v) => (v == null || v === '') ? '__vacío__' : String(v)

  const onColumnFilterChanged = useCallback((field, selectedKeys, totalValues) => {
    setColumnFilters(prev => {
      if (selectedKeys.size >= totalValues) {
        // Todos seleccionados → quitar filtro para esta columna
        if (!Object.prototype.hasOwnProperty.call(prev, field)) return prev
        const next = { ...prev }
        delete next[field]
        return next
      }
      return { ...prev, [field]: selectedKeys }
    })
  }, [])

  const onSelectionChanged = useCallback(() => {
    const sel = gridRef.current?.api?.getSelectedRows() || []
    setSelectedIds(sel.map((r) => r.id))
    setConfirmDelete(false)
  }, [])

  const deleteMutation = useMutation({
    mutationFn: (ids) => Promise.all(ids.map((id) => api.delete(`/programaciones/${id}`))),
    onSuccess: (_, ids) => {
      qc.invalidateQueries({ queryKey: ['programaciones'] })
      toast.success(`${ids.length} fila${ids.length > 1 ? 's' : ''} eliminada${ids.length > 1 ? 's' : ''}`)
      setSelectedIds([])
      setConfirmDelete(false)
      gridRef.current?.api?.deselectAll()
    },
    onError: (err) => toast.error(err.response?.data?.detail || 'Error al eliminar'),
  })

  const duplicateMutation = useMutation({
    mutationFn: (ids) => Promise.all(ids.map((id) => api.post(`/programaciones/${id}/duplicar`))),
    onSuccess: (_, ids) => {
      qc.invalidateQueries({ queryKey: ['programaciones'] })
      toast.success(`${ids.length} fila${ids.length > 1 ? 's' : ''} duplicada${ids.length > 1 ? 's' : ''}`)
      setSelectedIds([])
      gridRef.current?.api?.deselectAll()
    },
    onError: (err) => toast.error(err.response?.data?.detail || 'Error al duplicar'),
  })

  // Demo bot: highlight specific rows with a red tint + border
  const demoRowsRef = useRef(new Set())
  useEffect(() => {
    window._sabogalSetDemoRows = (ids) => {
      demoRowsRef.current = new Set(ids.map(String))
      window._sabogalGridApi?.redrawRows()
    }
    return () => { delete window._sabogalSetDemoRows }
  }, [])

  const getRowStyle = useCallback((params) => {
    if (params.data && demoRowsRef.current.has(String(params.data.id))) {
      return { backgroundColor: 'rgba(239,68,68,0.08)', boxShadow: 'inset 0 0 0 2px rgba(239,68,68,0.45)' }
    }
  }, [])

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['programaciones', unidadActiva, zona, mes, anio],
    queryFn: async () => {
      const params = { limit: 2000, unidad: unidadActiva }
      if (zona) params.zona = zona
      if (mes)  params.mes  = mes
      if (anio) params.anio = anio
      const { data } = await api.get('/programaciones', { params })
      return data
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, field, value }) =>
      api.patch(`/programaciones/${id}`, { [field]: value ?? null }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['programaciones'] }),
    onError:   (err) => toast.error(err.response?.data?.detail || 'Error al guardar'),
  })

  // TODA escritura de celda (edición, pegado, Delete) pasa por aquí: si el coordinador está en
  // la unidad ajena y no confirma, no se guarda y la grilla se recarga para deshacer lo visual.
  const mutarCelda = useCallback((args) => {
    if (Date.now() < rechazoHastaRef.current) return
    if (!guardRef.current()) {
      rechazoHastaRef.current = Date.now() + 2000
      qc.invalidateQueries({ queryKey: ['programaciones'] })
      return
    }
    updateMutation.mutate(args)
  }, [updateMutation, qc])
  const onCellValueChanged = useCallback((params) => {
    if (!puedeFilaRef.current(params.data)) return   // apoyo: fila de otra zona, no guardar
    const value = params.newValue === '' ? null : (params.newValue ?? null)
    mutarCelda({ id: params.data.id, field: params.colDef.field, value })
  }, [mutarCelda])

  // ── Selección de rango tipo Excel ───────────────────────────────────────────
  const cellSelectionRef   = useRef(new Set())
  const selectionAnchorRef = useRef(null)
  const isDraggingRef      = useRef(false)
  const hasDraggedRef      = useRef(false)
  const dragStartRef       = useRef(null)
  const shiftNavRef        = useRef(false) // true mientras Shift+Flecha mueve el foco

  // Refs estables para el listener de documento (evita closures obsoletas)
  const camEditRef  = useRef(camposEditables)
  useEffect(() => { camEditRef.current = camposEditables }, [camposEditables])
  const mutateRef = useRef(mutarCelda)
  useEffect(() => { mutateRef.current = mutarCelda }, [mutarCelda])

  // Mousedown: inicia drag (solo clic sin modificadores)
  const onCellMouseDown = useCallback((params) => {
    if (!params.colDef?.field) return
    if (params.event?.shiftKey || params.event?.ctrlKey || params.event?.metaKey) return
    const { rowIndex, colDef } = params
    isDraggingRef.current  = true
    hasDraggedRef.current  = false
    dragStartRef.current   = { rowIdx: rowIndex, field: colDef.field }
    selectionAnchorRef.current = { rowIdx: rowIndex, field: colDef.field }
    cellSelectionRef.current   = new Set([`${rowIndex}:${colDef.field}`])
    params.api.refreshCells({ force: true })
  }, [])

  // MouseOver: extiende la selección mientras se arrastra
  const onCellMouseOver = useCallback((params) => {
    if (!isDraggingRef.current || !dragStartRef.current) return
    if (!params.colDef?.field) return
    const { api, rowIndex, colDef } = params
    const field  = colDef.field
    const anchor = dragStartRef.current
    if (rowIndex === anchor.rowIdx && field === anchor.field && !hasDraggedRef.current) return
    hasDraggedRef.current = true
    const allCols = api.getAllDisplayedColumns().filter(c => c.getColDef().field)
    const ai = allCols.findIndex(c => c.getColDef().field === anchor.field)
    const ci = allCols.findIndex(c => c.getColDef().field === field)
    if (ai === -1 || ci === -1) return
    const r1 = Math.min(anchor.rowIdx, rowIndex), r2 = Math.max(anchor.rowIdx, rowIndex)
    const c1 = Math.min(ai, ci),                  c2 = Math.max(ai, ci)
    const sel = new Set()
    for (let r = r1; r <= r2; r++)
      for (let c = c1; c <= c2; c++) { const f = allCols[c]?.getColDef().field; if (f) sel.add(`${r}:${f}`) }
    cellSelectionRef.current = sel
    api.refreshCells({ force: true })
  }, [])

  // Shift+Click / Ctrl+Click para seleccionar rango
  const onCellClicked = useCallback((params) => {
    if (!params.colDef?.field) return
    // Si terminó un drag, no procesar el click
    if (hasDraggedRef.current) { hasDraggedRef.current = false; return }
    const { api, rowIndex, colDef, event } = params
    const field = colDef.field

    if (event?.shiftKey && selectionAnchorRef.current) {
      const anchor  = selectionAnchorRef.current
      const allCols = api.getAllDisplayedColumns().filter(c => c.getColDef().field)
      const ai = allCols.findIndex(c => c.getColDef().field === anchor.field)
      const ci = allCols.findIndex(c => c.getColDef().field === field)
      if (ai === -1 || ci === -1) return
      const r1 = Math.min(anchor.rowIdx, rowIndex), r2 = Math.max(anchor.rowIdx, rowIndex)
      const c1 = Math.min(ai, ci),                  c2 = Math.max(ai, ci)
      const sel = new Set()
      for (let r = r1; r <= r2; r++)
        for (let c = c1; c <= c2; c++) { const f = allCols[c]?.getColDef().field; if (f) sel.add(`${r}:${f}`) }
      cellSelectionRef.current = sel
      api.refreshCells({ force: true })
    } else if (event?.ctrlKey || event?.metaKey) {
      const key = `${rowIndex}:${field}`
      const sel = new Set(cellSelectionRef.current)
      sel.has(key) ? sel.delete(key) : sel.add(key)
      selectionAnchorRef.current = { rowIdx: rowIndex, field }
      cellSelectionRef.current = sel
      api.refreshCells({ force: true })
    }
    // plain click: mousedown ya fijó el anchor y la selección de 1 celda
  }, [])

  // Cuando AG Grid mueve el foco (teclas de flecha sin Shift), actualizar anchor y selección
  const onCellFocused = useCallback((params) => {
    if (!params.column || params.isFullWidthRow) return
    const field = typeof params.column === 'string'
      ? params.column
      : params.column?.getColDef?.()?.field
    if (!field || isDraggingRef.current || shiftNavRef.current) return
    selectionAnchorRef.current = { rowIdx: params.rowIndex, field }
    cellSelectionRef.current = new Set([`${params.rowIndex}:${field}`])
    gridRef.current?.api?.refreshCells({ force: true })
  }, [])

  // Liberar drag al soltar el mouse fuera del grid
  useEffect(() => {
    const onMouseUp = () => { isDraggingRef.current = false }
    document.addEventListener('mouseup', onMouseUp)
    return () => document.removeEventListener('mouseup', onMouseUp)
  }, [])

  // Listener en document: Ctrl+C / Ctrl+V / Delete / Shift+Flechas — funciona sin importar el foco del navegador
  useEffect(() => {
    const handler = async (e) => {
      const tag = document.activeElement?.tagName?.toUpperCase()
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return
      if (document.activeElement?.isContentEditable) return
      const api = gridRef.current?.api
      if (!api) return
      const fc = api.getFocusedCell()
      if (!fc) return
      const colDef = fc.column?.getColDef()
      if (!colDef?.field) return
      const cam     = camEditRef.current
      const canEdit = !cam || cam.includes(colDef.field)

      // ── Shift+Flechas: extender selección ─────────────────────────────────
      if (e.shiftKey && ['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)) {
        e.preventDefault()
        e.stopPropagation()
        const allCols   = api.getAllDisplayedColumns().filter(c => c.getColDef().field)
        const rowCount  = api.getDisplayedRowCount()
        const curColIdx = allCols.findIndex(c => c.getColDef().field === colDef.field)
        let newRow = fc.rowIndex, newColIdx = curColIdx
        if      (e.key === 'ArrowUp')    newRow    = Math.max(0, newRow - 1)
        else if (e.key === 'ArrowDown')  newRow    = Math.min(rowCount - 1, newRow + 1)
        else if (e.key === 'ArrowLeft')  newColIdx = Math.max(0, newColIdx - 1)
        else if (e.key === 'ArrowRight') newColIdx = Math.min(allCols.length - 1, newColIdx + 1)
        const newField = allCols[newColIdx]?.getColDef().field
        if (!newField) return
        if (!selectionAnchorRef.current) selectionAnchorRef.current = { rowIdx: fc.rowIndex, field: colDef.field }
        const anchor       = selectionAnchorRef.current
        const anchorColIdx = allCols.findIndex(c => c.getColDef().field === anchor.field)
        const r1 = Math.min(anchor.rowIdx, newRow), r2 = Math.max(anchor.rowIdx, newRow)
        const c1 = Math.min(anchorColIdx, newColIdx), c2 = Math.max(anchorColIdx, newColIdx)
        const sel = new Set()
        for (let r = r1; r <= r2; r++)
          for (let c = c1; c <= c2; c++) { const f = allCols[c]?.getColDef().field; if (f) sel.add(`${r}:${f}`) }
        cellSelectionRef.current = sel
        shiftNavRef.current = true
        api.setFocusedCell(newRow, newField)
        shiftNavRef.current = false
        api.refreshCells({ force: true })
        return
      }

      // ── Ctrl+C: copiar ──────────────────────────────────────────────────────
      if ((e.ctrlKey || e.metaKey) && e.key === 'c') {
        e.preventDefault()
        const sel = cellSelectionRef.current
        if (sel.size <= 1) {
          const node = api.getDisplayedRowAtIndex(fc.rowIndex)
          await navigator.clipboard.writeText(String(node?.data?.[colDef.field] ?? '')).catch(() => {})
          toast.success('Copiado', { duration: 600 })
        } else {
          const allCols    = api.getAllDisplayedColumns().filter(c => c.getColDef().field)
          const rowSet     = new Set(), fieldSet = new Set()
          sel.forEach(k => { const [r, f] = k.split(':'); rowSet.add(+r); fieldSet.add(f) })
          const sortedRows   = [...rowSet].sort((a, b) => a - b)
          const sortedFields = [...fieldSet].sort((a, b) =>
            allCols.findIndex(c => c.getColDef().field === a) - allCols.findIndex(c => c.getColDef().field === b))
          const tsv = sortedRows.map(r => {
            const node = api.getDisplayedRowAtIndex(r)
            return sortedFields.map(f => String(node?.data?.[f] ?? '')).join('\t')
          }).join('\n')
          await navigator.clipboard.writeText(tsv).catch(() => {})
          toast.success(`${sel.size} celdas copiadas`, { duration: 700 })
        }
        return
      }

      // ── Ctrl+V: pegar ───────────────────────────────────────────────────────
      if ((e.ctrlKey || e.metaKey) && e.key === 'v') {
        if (!canEdit) return
        e.preventDefault()
        try {
          const text = await navigator.clipboard.readText()
          if (!text?.trim()) return
          const lines    = text.trim().split(/\r?\n/)
          const allCols  = api.getAllDisplayedColumns().filter(c => c.getColDef().field)
          const startIdx = allCols.findIndex(c => c.getColDef().field === colDef.field)
          lines.forEach((line, rOff) => {
            const node = api.getDisplayedRowAtIndex(fc.rowIndex + rOff)
            if (!node?.data) return
            if (!puedeFilaRef.current(node.data)) return   // apoyo: no pegar en otra zona
            line.split('\t').forEach((v, cOff) => {
              const col = allCols[startIdx + cOff]
              const f   = col?.getColDef().field
              if (!f || (cam && !cam.includes(f))) return
              mutateRef.current({ id: node.data.id, field: f, value: v.trim() || null })
            })
          })
          toast.success('Pegado', { duration: 600 })
        } catch { toast.error('Habilita el portapapeles en el navegador') }
        return
      }

      // ── Delete / Backspace: limpiar celda(s) ───────────────────────────────
      if (e.key === 'Delete' || e.key === 'Backspace') {
        e.preventDefault()
        const cam = camEditRef.current
        const sel = cellSelectionRef.current
        if (sel.size > 1) {
          // Borrar todo el rango seleccionado
          let count = 0
          sel.forEach(k => {
            const [r, f] = k.split(':')
            if (['zona', 'fecha'].includes(f)) return
            if (cam && !cam.includes(f)) return
            const node = api.getDisplayedRowAtIndex(+r)
            if (!node?.data || !puedeFilaRef.current(node.data)) return   // apoyo: otra zona
            mutateRef.current({ id: node.data.id, field: f, value: null }); count++
          })
          if (count) toast.success(`${count} celda${count > 1 ? 's' : ''} borrada${count > 1 ? 's' : ''}`, { duration: 700 })
        } else {
          if (!canEdit || ['zona', 'fecha'].includes(colDef.field)) return
          const node = api.getDisplayedRowAtIndex(fc.rowIndex)
          if (node?.data && puedeFilaRef.current(node.data)) mutateRef.current({ id: node.data.id, field: colDef.field, value: null })
        }
        return
      }

      // ── Escape: limpiar selección ───────────────────────────────────────────
      if (e.key === 'Escape') {
        cellSelectionRef.current = new Set()
        api.refreshCells({ force: true })
      }
    }
    document.addEventListener('keydown', handler, true)  // capture: dispara ANTES que AG Grid
    return () => document.removeEventListener('keydown', handler, true)
  }, []) // sin deps: lee todo vía refs

  const isEditable = useCallback((params) => {
    if (!puedeFila(params.data)) return false     // apoyo: fila de otra zona
    if (!camposEditables) return true
    return camposEditables.includes(params.colDef.field)
  }, [camposEditables, puedeFila])

  // Colores de columna + resaltado de selección de rango
  const getCellClass = useCallback((params) => {
    const selected = cellSelectionRef.current.has(`${params.rowIndex}:${params.colDef.field}`)
    const f = params.colDef.field
    let base = null
    if (!puedeFila(params.data)) {
      base = 'col-lock'                          // apoyo: fila de otra zona, toda bloqueada
    } else if (camposEditables) {
      if (camposEditables.includes(f)) {
        if (f === 'se_gestiona')      base = 'col-red'   // banda roja: todos los roles
        else if (INGE_FIELDS.has(f))  base = 'col-ing'
        else if (PROG_FIELDS.has(f))  base = 'col-prog'
        else                          base = 'col-fact'
      } else {
        base = 'col-lock'
      }
    }
    return selected ? `${base || ''} cell-selected`.trim() : base
  }, [camposEditables, puedeFila])

  // FECHA en rojo cuando es domingo o festivo — para TODOS los roles (plantilla 2026-08)
  const getFechaCellClass = useCallback((params) => {
    const base = getCellClass(params)
    if (!esNoLaborable(params.value)) return base
    return base ? `${base} cell-no-laborable` : 'cell-no-laborable'
  }, [getCellClass])

  // Solo supervisor puede cambiar zona de una fila existente
  const isZonaEditable = useCallback(() => usuario?.rol === 'supervisor', [usuario])

  const rows = useMemo(() => {
    let items = data?.items || []
    if (estadoFilter) items = items.filter((r) => r.estado === estadoFilter)
    // Aplicar filtros de columna (pre-filtrado — independiente del API de AG Grid)
    for (const [field, selectedKeys] of Object.entries(columnFilters)) {
      items = items.filter(r => selectedKeys.has(colFilterKeyOf(r[field])))
    }
    return items
  }, [data, estadoFilter, columnFilters])

  const colDefs = useMemo(() => [
    // ── Checkbox de selección (solo roles con permiso de borrado) ──────────────
    ...(canDelete ? [{
      colId: 'sel',
      headerCheckboxSelection: true,
      checkboxSelection: true,
      width: 42, minWidth: 42, maxWidth: 42,
      pinned: 'left',
      suppressMovable: true, resizable: false, filter: false, sortable: false,
      headerClass: 'text-xs',
    }] : []),

    // ── Identidad ──────────────────────────────────────────────────────────────
    { field: 'zona',       headerName: 'ZONA',  width: 110, pinned: 'left',
      editable: isZonaEditable,
      cellEditor: 'agSelectCellEditor', cellEditorParams: { values: cfg.zonas },
      cellClass: getCellClass,
      cellRenderer: (p) => {
        const colors = { ALMEIDAS:'bg-purple-100 text-purple-800', GUAVIO:'bg-orange-100 text-orange-800', UBATE:'bg-teal-100 text-teal-800', 'LV&TLC':'bg-pink-100 text-pink-800' }
        return <span className={`px-1 py-0.5 rounded text-xs font-medium ${colors[p.value] || ''}`}>{p.value}</span>
      }
    },
    { field: 'fecha',      headerName: 'FECHA', width: 110, pinned: 'left',
      editable: isEditable, cellClass: getFechaCellClass,
      cellEditor: 'agDateStringCellEditor',
      valueFormatter: (p) => fmtFecha(p.value),
    },
    { field: 'lider',      headerName: 'LÍDER', width: 160, pinned: 'left',
      editable: isEditable, cellClass: getCellClass,
    },
    { field: 'tipo_grupo', headerName: cfg.tipoLabel,  width: 100, pinned: 'left',
      editable: isEditable, cellClass: getCellClass,
      cellEditor: 'agSelectCellEditor', cellEditorParams: { values: cfg.tipoOptions },
    },

    // ── Orden y colores EXACTOS de la plantilla del cliente (2026-08) ──────────
    // Azul: planificación
    { field: 'proceso',             headerName: 'PROCESO',               width: 130, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass, cellEditor: 'agSelectCellEditor', cellEditorParams: { values: cfg.procesoOptions } },
    { field: 'subproceso',          headerName: 'SUBPROCESO',            width: 130, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass, cellEditor: 'agSelectCellEditor', cellEditorParams: { values: cfg.subprocesoOptions } },
    { field: 'proyecto_antecedente',headerName: 'PROYECTO / ANTECEDENTE',width: 250, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass },
    { field: 'pms',                 headerName: 'PMS',                   width: 200, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass },
    { field: 'circuito',            headerName: 'CIRCUITO',              width: 110, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass },
    { field: 'lcl',                 headerName: 'LCL',                   width: 120, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass },
    { field: 'fecha_vigencia',      headerName: 'F. VIGENCIA',           width: 110, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass, cellEditor: 'agDateStringCellEditor', valueFormatter: (p) => fmtFecha(p.value) },
    { field: 'tipo_pdt',            headerName: 'TIPO PDT',              width:  90, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass, cellEditor: 'agSelectCellEditor', cellEditorParams: { values: ['TP','PDL','DF',''] } },

    // Verde: # PDT → HORA CR
    { field: 'num_pdt',             headerName: '# PDT',                 width: 160, headerClass: H_GREEN,  editable: isEditable, cellClass: getCellClass, cellRenderer: PdtCell },
    { field: 'estado',              headerName: 'ESTADO',                width: 130, headerClass: H_GREEN,  editable: isEditable, cellClass: getCellClass, cellRenderer: (p) => <EstadoBadge value={p.value} /> },
    { field: 'cd_afectados',        headerName: 'CD AFECTADOS',          width: 130, headerClass: H_GREEN,  editable: isEditable, cellClass: getCellClass },
    { field: 'saidi',               headerName: 'SAIDI',                 width:  95, headerClass: H_GREEN,  editable: isEditable, cellClass: getCellClass, type: 'numericColumn' },
    { field: 'hora_ab',             headerName: 'HORA AB',               width:  90, headerClass: H_GREEN,  editable: isEditable, cellClass: getCellClass },
    { field: 'hora_cr',             headerName: 'HORA CR',               width:  90, headerClass: H_GREEN,  editable: isEditable, cellClass: getCellClass },
    { field: 'hora',                headerName: 'HORA',                  width:  80, headerClass: H_GREEN,  editable: isEditable, cellClass: getCellClass },

    // Azul: ubicación y programadores
    { field: 'municipio',           headerName: 'MUNICIPIO',             width: 120, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass },
    { field: 'vereda',              headerName: 'VEREDA / VDA',          width: 120, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass },
    { field: 'alcance',             headerName: 'ALCANCE',               width: 250, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass },
    { field: 'gestor_cliente',      headerName: 'GESTOR ENEL',           width: 140, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass },
    { field: 'supervisor',          headerName: 'SUPERVISOR',            width: 140, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass },
    { field: 'valor_dia',           headerName: 'VALOR DÍA',             width: 115, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass, valueFormatter: (p) => formatCOP(p.value), type: 'numericColumn' },
    { field: 'mo_proyectada',       headerName: 'MO PROYECTADA',         width: 135, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass, valueFormatter: (p) => formatCOP(p.value), type: 'numericColumn' },

    // Cián MO REAL · gris V. DEJADO · azul VALOR MATERIAL / ESTADO PDT
    { field: 'mo_real',              headerName: 'MO REAL',              width: 115, headerClass: H_LBLUE,  editable: isEditable, cellClass: getCellClass, valueFormatter: (p) => formatCOP(p.value), type: 'numericColumn' },
    { field: 'mo_facturada',         headerName: 'MO FACTURADA',         width: 135, headerClass: H_LBLUE,  editable: isEditable, cellClass: getCellClass, valueFormatter: (p) => formatCOP(p.value), type: 'numericColumn' },
    { field: 'valor_dejado_ejecutar',headerName: 'V. DEJADO EJECUTAR',   width: 150, headerClass: H_SLATE, editable: isEditable, cellClass: getCellClass, valueFormatter: (p) => formatCOP(p.value), type: 'numericColumn' },
    { field: 'valor_material',      headerName: 'VALOR MATERIAL',        width: 130, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass, valueFormatter: (p) => formatCOP(p.value), type: 'numericColumn' },
    { field: 'estado_pdt',          headerName: 'ESTADO PDT',            width: 160, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass, cellEditor: 'agSelectCellEditor', cellEditorParams: { values: cfg.estadoPdtOptions } },

    // Gris NOTAS · azul RESTRICCIONES · ROJO SE GESTIONA (todos los roles)
    { field: 'notas',                headerName: 'NOTAS',                width: 200, headerClass: H_SLATE,  editable: isEditable, cellClass: getCellClass },
    { field: 'restricciones',        headerName: 'RESTRICCIONES',        width: 180, headerClass: H_BLUE,   editable: isEditable, cellClass: getCellClass },
    { field: 'se_gestiona',          headerName: 'SE GESTIONA SI O NO',  width: 130, headerClass: H_RED,    editable: isEditable, cellClass: getCellClass, cellEditor: 'agSelectCellEditor', cellEditorParams: { values: ['SI', 'NO', ''] } },

    // Cián: liquidación / facturación
    { field: 'num_proliquidado',     headerName: '#PROLIQUIDADO',        width: 130, headerClass: H_LBLUE,  editable: isEditable, cellClass: getCellClass },
    { field: 'fecha_liquidacion_sgs',headerName: 'F. LIQ. SGS',         width: 120, headerClass: H_LBLUE,  editable: isEditable, cellClass: getCellClass, cellEditor: 'agDateStringCellEditor', valueFormatter: (p) => fmtFecha(p.value) },
    { field: 'estado_facturacion',   headerName: 'ESTADO FACTURACIÓN',   width: 165, headerClass: H_LBLUE,  editable: isEditable, cellClass: getCellClass },
    { field: 'fecha_envio_facturacion',headerName: 'F. ENVÍO FACT.',     width: 120, headerClass: H_LBLUE,  editable: isEditable, cellClass: getCellClass, cellEditor: 'agDateStringCellEditor', valueFormatter: (p) => fmtFecha(p.value) },

    // Verde: OT / cumplimentación · gris: inoperatividad
    { field: 'ot',                  headerName: 'OT',                    width: 130, headerClass: H_GREEN,  editable: isEditable, cellClass: getCellClass },
    { field: 'estado_cumplimentacion',headerName: 'ESTADO CUMPLIM.',     width: 150, headerClass: H_GREEN,  editable: isEditable, cellClass: getCellClass },
    { field: 'inoperatividad',       headerName: 'INOPERATIVIDAD',       width: 130, headerClass: H_SLATE,  editable: isEditable, cellClass: getCellClass, valueFormatter: (p) => formatCOP(p.value), type: 'numericColumn' },
    { field: 'inoperatividad_tipo',  headerName: 'INOPERATIVIDAD',       width: 140, headerClass: H_SLATE,  editable: isEditable, cellClass: getCellClass },
    { field: 'observaciones_inoperatividad', headerName: 'OBS. INOPERATIVIDAD', width: 220, headerClass: H_SLATE, editable: isEditable, cellClass: getCellClass },
  ].filter((c) => !c.field || !cfg.ocultarColumnas.includes(c.field)),
  [isEditable, isZonaEditable, getCellClass, getFechaCellClass, canDelete, cfg])

  const defaultColDef = useMemo(() => ({
    sortable: true,
    resizable: true,
    filter: ExcelSetFilter,
    suppressMovable: false,
    headerClass: 'text-xs',
  }), [])

  return (
    <div className="flex flex-col h-full">

      {/* Toolbar — dos filas en móvil, una en escritorio */}
      <div className="bg-white border-b shadow-sm px-3 py-2 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:px-4 sm:py-2.5">

        {/* Fila 1 móvil / izquierda escritorio: búsqueda + estado + contador */}
        <div className="flex items-center gap-2 flex-1 min-w-0">
          {/* Selector de unidad — roles globales (jefe superior / dev) */}
          {esGlobal ? (
            <div className="relative shrink-0">
              <Building2 size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-blue-500 pointer-events-none" />
              <select
                value={unidadActiva}
                onChange={(e) => setUnidadActiva(e.target.value)}
                title="Unidad operativa"
                className="pl-7 pr-2 py-1.5 text-xs font-semibold text-blue-800 bg-blue-50 border border-blue-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-blue-400"
              >
                {ORDEN_UNIDADES.map((u) => <option key={u} value={u}>{UNIDADES[u].corto}</option>)}
              </select>
            </div>
          ) : unidadActiva !== 'SABOGAL' && (
            <span className="shrink-0 inline-flex items-center gap-1 px-2 py-1.5 text-xs font-semibold text-blue-800 bg-blue-50 border border-blue-200 rounded-lg">
              <Building2 size={13} /> {cfg.corto}
            </span>
          )}

          {/* Búsqueda global */}
          <div className="relative flex-1 min-w-0">
            <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              type="text"
              value={quickFilter}
              onChange={(e) => {
                setQuickFilter(e.target.value)
                gridRef.current?.api?.setGridOption('quickFilterText', e.target.value)
              }}
              placeholder="Buscar…"
              className="w-full pl-7 pr-7 py-1.5 text-xs border rounded-lg focus:outline-none focus:ring-1 focus:ring-blue-400"
            />
            {quickFilter && (
              <button
                onClick={() => { setQuickFilter(''); gridRef.current?.api?.setGridOption('quickFilterText', '') }}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-700"
              >
                <X size={12} />
              </button>
            )}
          </div>

          {/* Filtro ESTADO */}
          <select
            value={estadoFilter}
            onChange={(e) => setEstadoFilter(e.target.value)}
            className="border rounded-lg px-1.5 py-1.5 text-xs text-gray-700 min-w-[100px] sm:min-w-[130px]"
          >
            <option value="">Todos</option>
            {ESTADO_OPTIONS.map((e) => <option key={e}>{e}</option>)}
          </select>

          {/* Contador — oculto en móvil muy pequeño */}
          <span className="hidden xs:inline text-xs text-gray-500 whitespace-nowrap">
            <b className="text-gray-800">{rows.length}</b> reg.
          </span>

          {/* Leyenda — solo en pantallas grandes */}
          <div className="hidden lg:flex items-center gap-3 text-xs text-gray-500 border-l pl-3">
            <span className="flex items-center gap-1"><span className="inline-block w-2.5 h-2.5 rounded-sm bg-blue-700"/>Ingeniero</span>
            <span className="flex items-center gap-1"><span className="inline-block w-2.5 h-2.5 rounded-sm bg-green-700"/>Programador</span>
            <span className="flex items-center gap-1"><span className="inline-block w-2.5 h-2.5 rounded-sm bg-teal-700"/>Facturación</span>
            <span className="flex items-center gap-1"><span className="inline-block w-2.5 h-2.5 rounded-sm bg-red-600"/>Todos</span>
            {camposEditables && <span className="text-blue-700 font-medium">· tus columnas resaltadas</span>}
          </div>
        </div>

        {/* Derecha: acciones */}
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => refetch()}
            className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-800 border px-2 py-1.5 rounded"
          >
            <RefreshCw size={13} /> <span className="hidden sm:inline">Actualizar</span>
          </button>

          {/* Historial de cambios (como en ANS): con UNA fila seleccionada abre el de esa fila */}
          <button
            id="btn-historial"
            onClick={() => setShowHist(true)}
            title="Quién cambió qué: cada celda editada, fila creada o borrada (antes → después), con opción de revertir"
            className="flex items-center gap-1 text-xs text-gray-600 hover:text-gray-900 border px-2 py-1.5 rounded"
          >
            <History size={13} /> <span className="hidden sm:inline">Historial{selectedIds.length === 1 ? ' fila' : ''}</span>
          </button>

          {/* Relación con SGS: desarrollador o coordinador de programados, solo unidad SABOGAL */}
          {/* solo el dev o el coordinador de programados (su unidad = SABOGAL), viendo Programados */}
          {(usuario?.rol === 'desarrollador' || (usuario?.rol === 'supervisor' && (usuario?.unidad || 'SABOGAL') === 'SABOGAL')) && unidadActiva === 'SABOGAL' && (
            <button
              onClick={() => setShowSgs(true)}
              title="Autocompletar MO real liquidada cruzando con SGS"
              className="flex items-center gap-1 text-xs border border-emerald-300 text-emerald-700 px-2.5 py-1.5 rounded-lg hover:bg-emerald-50"
            >
              <Link2 size={13} /> <span className="hidden sm:inline">Relación con</span> SGS
            </button>
          )}

          {/* Duplicar — aparece con filas seleccionadas */}
          {canDelete && selectedIds.length > 0 && (
            <button
              onClick={() => guardRef.current() && duplicateMutation.mutate(selectedIds)}
              disabled={duplicateMutation.isPending}
              title="Duplicar filas seleccionadas"
              className="flex items-center gap-1 text-xs text-blue-600 border border-blue-200 px-2 py-1.5 rounded-lg hover:bg-blue-50 disabled:opacity-50"
            >
              <Copy size={13} />
              <span className="hidden sm:inline">{selectedIds.length} dup.</span>
            </button>
          )}

          {/* Borrado — aparece solo cuando hay filas seleccionadas */}
          {canDelete && selectedIds.length > 0 && (
            confirmDelete ? (
              <div className="flex items-center gap-1 bg-red-50 border border-red-200 rounded-lg px-2 py-1">
                <span className="text-xs text-red-700 font-medium whitespace-nowrap">
                  ¿Eliminar {selectedIds.length} fila{selectedIds.length > 1 ? 's' : ''}?
                </span>
                <button
                  onClick={() => deleteMutation.mutate(selectedIds)}
                  disabled={deleteMutation.isPending}
                  className="text-xs bg-red-600 text-white px-2 py-0.5 rounded hover:bg-red-700 disabled:opacity-50"
                >
                  {deleteMutation.isPending ? '…' : 'Sí'}
                </button>
                <button
                  onClick={() => setConfirmDelete(false)}
                  className="text-xs text-red-500 hover:text-red-800 px-1"
                >
                  No
                </button>
              </div>
            ) : (
              <button
                onClick={() => guardRef.current() && setConfirmDelete(true)}
                className="flex items-center gap-1 text-xs text-red-600 border border-red-200 px-2 py-1.5 rounded-lg hover:bg-red-50"
              >
                <Trash2 size={13} />
                <span>{selectedIds.length} sel.</span>
              </button>
            )
          )}

          {canCrear && unidadActiva === 'CHOCONTA' && (
            <>
              <input ref={mantillaRef} type="file" accept=".xlsx" className="hidden" onChange={onImportMantilla} />
              <button
                onClick={() => mantillaRef.current?.click()}
                disabled={impMantilla}
                title="Importar plantilla Mantilla (pisos/MDO por cuadrilla y día)"
                className="flex items-center gap-1 text-xs border border-amber-300 text-amber-700 px-2.5 py-1.5 rounded-lg hover:bg-amber-50 disabled:opacity-60"
              >
                <Upload size={13} /> {impMantilla ? 'Importando…' : 'Plantilla Mantilla'}
              </button>
            </>
          )}

          {canCrear && (
            <button
              id="btn-nueva-fila"
              onClick={() => guardRef.current() && setShowModal(true)}
              className="flex items-center gap-1 text-xs bg-blue-700 text-white px-3 py-1.5 rounded hover:bg-blue-800"
            >
              <Plus size={13} /> <span className="hidden sm:inline">Nueva</span> fila
            </button>
          )}
        </div>
      </div>

      {/* Grid */}
      <div className="flex-1 ag-theme-alpine" style={{ minHeight: 0, height: '100%', width: '100%' }}>
        {isLoading ? (
          <div className="flex items-center justify-center h-full text-gray-500">Cargando...</div>
        ) : (
          <AgGridReact
            ref={gridRef}
            rowData={rows}
            columnDefs={colDefs}
            defaultColDef={defaultColDef}
            onCellValueChanged={onCellValueChanged}
            context={{ onColumnFilterChanged, onVerMantilla: setMantillaDet }}
            onGridReady={(params) => { window._sabogalGridApi = params.api }}
            getRowStyle={getRowStyle}
            onSelectionChanged={onSelectionChanged}
            rowSelection="multiple"
            isRowSelectable={(node) => puedeFila(node.data)}
            singleClickEdit={false}
            stopEditingWhenCellsLoseFocus
            animateRows
            rowHeight={32}
            headerHeight={42}
            suppressRowClickSelection
            getRowId={(p) => String(p.data.id)}
            onCellClicked={onCellClicked}
            onCellMouseDown={onCellMouseDown}
            onCellMouseOver={onCellMouseOver}
            onCellFocused={onCellFocused}
            processCellForClipboard={(params) => String(params.value ?? '')}
          />
        )}
      </div>

      {showModal && (
        <ModalNuevaFila
          zonaDefault={zona}
          unidad={unidadActiva}
          onClose={() => setShowModal(false)}
          onCreated={() => {
            setShowModal(false)
            qc.invalidateQueries({ queryKey: ['programaciones'] })
          }}
        />
      )}

      {showHist && (
        <HistorialProgramacionModal
          unidad={unidadActiva}
          unidadNombre={cfg.corto}
          zona={zona}
          mes={mes}
          anio={anio}
          registroId={selectedIds.length === 1 ? selectedIds[0] : null}
          filaSel={selectedIds.length === 1 ? rows.find((r) => String(r.id) === String(selectedIds[0])) : null}
          readOnly={readOnly}
          onConfirmar={() => guardRef.current()}
          onClose={() => setShowHist(false)}
        />
      )}

      {mantillaDet && <MantillaDetalleModal row={mantillaDet} onClose={() => setMantillaDet(null)} />}
      {mantillaRes && <MantillaResultadoModal res={mantillaRes} onClose={() => setMantillaRes(null)} />}

      {showSgs && (
        <RelacionSgsModal
          mes={mes}
          anio={anio}
          unidad={unidadActiva}
          onClose={() => setShowSgs(false)}
          onAplicado={() => qc.invalidateQueries({ queryKey: ['programaciones'] })}
        />
      )}
    </div>
  )
}
