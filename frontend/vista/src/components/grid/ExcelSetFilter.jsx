import { useState, useMemo, useRef, useCallback, useEffect } from 'react'
import { useGridFilter } from 'ag-grid-react'

// Autofiltro tipo Excel (casillas por valor + buscador). Patrón REACTIVO de AG Grid v32
// (useGridFilter + model/onModelChange): el patrón imperativo viejo (useImperativeHandle con
// doesFilterPass) se IGNORA desde v32 y el grid nunca filtraba por sí mismo — por eso
// Programación/Bolsas compensaban con pre-filtrado en React (que se mantiene vía onChanged).
// El modelo vive en el grid: { values: [claves seleccionadas] } o null (= sin filtro).
export default function ExcelSetFilter(props) {
  const { model, onModelChange, api, colDef, context } = props
  // colId cubre las columnas calculadas (valueGetter sin field), como las del ANS
  const colKey    = colDef.colId || colDef.field
  const onChanged = context?.onColumnFilterChanged

  const keyOf = (v) => (v == null || v === '') ? '__vacío__' : String(v)
  // Lee el valor con el mismo criterio que la celda (props.getValue respeta el valueGetter).
  // '' y null se normalizan a null: un solo "(Vacío)" en la lista, no dos.
  const getVal = useCallback((node) => {
    const v = typeof props.getValue === 'function' ? props.getValue(node) : node.data?.[colDef.field]
    return (v == null || v === '') ? null : v
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const cmp = (a, b) => {
    if (a == null) return 1
    if (b == null) return -1
    return String(a).localeCompare(String(b), 'es', { numeric: true })
  }
  const computeValues = useCallback(() => {
    const set = new Set()
    api.forEachNode((node) => { if (node.data) set.add(getVal(node)) })
    return [...set].sort(cmp)
  }, [api, getVal])

  const [allValues, setAllValues] = useState(computeValues)

  const display = (val) => {
    if (val == null || val === '') return '(Vacío)'
    if (colDef.valueFormatter) {
      try {
        const r = colDef.valueFormatter({ value: val, colDef, data: {} })
        if (r != null && r !== '') return String(r)
      } catch { /* fallback */ }
    }
    return String(val)
  }

  // La selección se DERIVA del modelo del grid (única fuente de verdad): así un
  // setFilterModel(null) externo (limpiar todos / cambio de periodo) refresca las casillas.
  const selected = useMemo(
    () => (model?.values ? new Set(model.values) : new Set(allValues.map(keyOf))),
    [model, allValues],
  )
  // doesFilterPass corre fuera del ciclo de render → lee un ref actualizado en sincronía
  const selectedRef = useRef(selected)
  selectedRef.current = selected

  // Notifica al grid padre (pre-filtrado en React de Programación/Bolsas, chips del ANS).
  // El set va PODADO a los valores presentes: claves viejas de datos que ya no existen no
  // deben engañar al conteo ni al check de "todos seleccionados" del padre.
  useEffect(() => {
    const podado = new Set(allValues.filter((v) => selected.has(keyOf(v))).map(keyOf))
    onChanged?.(colKey, podado, allValues.length)
  }, [selected, allValues])  // eslint-disable-line react-hooks/exhaustive-deps

  const [search, setSearch] = useState('')

  const visible = useMemo(
    () => allValues.filter(v => display(v).toLowerCase().includes(search.toLowerCase())),
    [allValues, search],
  )

  const numSel      = useMemo(() => allValues.filter(v => selected.has(keyOf(v))).length, [allValues, selected])
  const allChecked  = visible.length > 0 && visible.every(v => selected.has(keyOf(v)))
  const someChecked = visible.some(v => selected.has(keyOf(v)))

  const aplicar = useCallback((next) => {
    selectedRef.current = next   // antes del refiltrado del grid, sin esperar el re-render
    const todos = allValues.every(v => next.has(keyOf(v)))
    onModelChange(todos ? null : { values: [...next] })
  }, [allValues, onModelChange])

  const toggle = useCallback((val) => {
    const k    = keyOf(val)
    const next = new Set(selectedRef.current)
    next.has(k) ? next.delete(k) : next.add(k)
    aplicar(next)
  }, [aplicar])

  const toggleAll = useCallback(() => {
    const next = new Set(selectedRef.current)
    const shouldDeselect = visible.length > 0 && visible.every(v => selectedRef.current.has(keyOf(v)))
    if (shouldDeselect) {
      visible.forEach(v => next.delete(keyOf(v)))
    } else {
      visible.forEach(v => next.add(keyOf(v)))
    }
    aplicar(next)
  }, [aplicar, visible])

  const clearFilter = useCallback(() => {
    aplicar(new Set(allValues.map(keyOf)))
  }, [aplicar, allValues])

  // Enter = como Excel: si hay texto de búsqueda, la selección pasa a ser LO QUE COINCIDE;
  // luego cierra el popup (sin búsqueda, solo cierra — las casillas ya se aplicaron en vivo).
  const onKeyDown = useCallback((e) => {
    if (e.key !== 'Enter') return
    e.preventDefault()
    e.stopPropagation()
    if (search && visible.length > 0) aplicar(new Set(visible.map(keyOf)))
    api.hidePopupMenu()
  }, [search, visible, aplicar, api])

  useGridFilter({
    doesFilterPass: ({ node }) => !node.data || selectedRef.current.has(keyOf(getVal(node))),
    // Al abrir el popup se refresca la lista (como Excel). UNIÓN con lo ya conocido: en los
    // grids que pre-filtran en React las filas ocultas desaparecen de forEachNode, y sus
    // valores deben seguir listados para poder des-filtrarlos.
    afterGuiAttached: () => {
      setSearch('')
      setAllValues((prev) => {
        const vistos = new Set()
        const union = []
        for (const v of [...computeValues(), ...prev]) {
          const k = keyOf(v)
          if (!vistos.has(k)) { vistos.add(k); union.push(v) }
        }
        return union.sort(cmp)
      })
    },
  })

  return (
    <div style={{ minWidth: 200, maxWidth: 280 }} className="bg-white text-xs select-none" onKeyDown={onKeyDown}>
      <div className="p-2 border-b">
        <input
          type="text"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Buscar… (Enter aplica)"
          autoFocus
          className="w-full border rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-400"
        />
      </div>

      <label className="flex items-center gap-2 px-3 py-1.5 cursor-pointer hover:bg-gray-50 font-semibold border-b">
        <input
          type="checkbox"
          checked={allChecked}
          ref={el => { if (el) el.indeterminate = !allChecked && someChecked }}
          onChange={toggleAll}
        />
        (Seleccionar todo)
      </label>

      <div style={{ maxHeight: 240, overflowY: 'auto' }}>
        {visible.map((val) => (
          <label
            key={keyOf(val)}
            className="flex items-center gap-2 px-3 py-1 cursor-pointer hover:bg-blue-50"
          >
            <input
              type="checkbox"
              checked={selected.has(keyOf(val))}
              onChange={() => toggle(val)}
            />
            <span className={val == null ? 'italic text-gray-400' : ''}>
              {display(val)}
            </span>
          </label>
        ))}
        {visible.length === 0 && (
          <div className="px-3 py-2 text-gray-400 italic">Sin resultados</div>
        )}
      </div>

      {numSel !== allValues.length && (
        <div className="px-3 py-1.5 border-t text-gray-500 text-[11px]">
          {numSel} de {allValues.length} seleccionados
          <button onClick={clearFilter} className="ml-2 text-blue-600 hover:underline">
            Limpiar
          </button>
        </div>
      )}
    </div>
  )
}
