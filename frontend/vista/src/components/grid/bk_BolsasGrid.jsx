import { useCallback, useRef, useMemo, useState } from 'react'
import { AgGridReact } from 'ag-grid-react'
import { useQueryClient } from '@tanstack/react-query'
import api from '../../utils/api'
import toast from 'react-hot-toast'
import { Eye, Trash2 } from 'lucide-react'
import ExcelSetFilter from './ExcelSetFilter'

const fmtFecha = (v) => {
  if (v == null || v === '') return ''
  const [y, m, d] = String(v).slice(0, 10).split('-')
  return (y && m && d) ? `${d}/${m}/${y}` : String(v)
}

// Celda de acciones (ver / eliminar) — lee callbacks del context
function AccionesCell(p) {
  const { onVer, onDelete, readOnly } = p.context
  if (!p.data?._bolsa) return null
  return (
    <div className="flex items-center gap-1 h-full">
      <button onClick={() => onVer(p.data._bolsa)} className="text-gray-400 hover:text-blue-600" title="Ver todo"><Eye size={14} /></button>
      {!readOnly && <button onClick={() => onDelete(p.data._bolsa)} className="text-gray-400 hover:text-red-600" title="Eliminar"><Trash2 size={13} /></button>}
    </div>
  )
}

export default function BolsasGrid({ tipo, campos, items, onVer, readOnly = false, quickFilter = '' }) {
  const qc = useQueryClient()
  const gridRef = useRef()
  const [columnFilters, setColumnFilters] = useState({})
  const colFilterKeyOf = (v) => (v == null || v === '') ? '__vacío__' : String(v)

  // Pre-filtrado en React (igual que la grilla de Programación)
  const onColumnFilterChanged = useCallback((field, selectedKeys, totalValues) => {
    setColumnFilters((prev) => {
      if (selectedKeys.size >= totalValues) {
        if (!Object.prototype.hasOwnProperty.call(prev, field)) return prev
        const next = { ...prev }; delete next[field]; return next
      }
      return { ...prev, [field]: selectedKeys }
    })
  }, [])

  // Filas: aplanar datos_extra con claves seguras f0,f1… (evita problemas con puntos/espacios)
  const rows = useMemo(() => {
    let rs = items.map((it) => {
      const row = { id: it.id, _bolsa: it, _datos: it.datos_extra || {} }
      campos.forEach((c, i) => { row[`f${i}`] = it.datos_extra?.[c] ?? '' })
      return row
    })
    for (const [field, sel] of Object.entries(columnFilters)) {
      rs = rs.filter((r) => sel.has(colFilterKeyOf(r[field])))
    }
    return rs
  }, [items, campos, columnFilters])

  const onCellValueChanged = useCallback((p) => {
    const realHeader = p.colDef.headerName
    const datos = { ...(p.data._datos || {}) }
    const v = p.newValue
    if (v === '' || v == null) delete datos[realHeader]
    else datos[realHeader] = v
    p.data._datos = datos
    api.patch(`/bolsas/${p.data.id}`, { tipo, datos_extra: datos })
      .then(() => {
        qc.invalidateQueries({ queryKey: ['bolsas-resumen'] })
        qc.invalidateQueries({ queryKey: ['bolsas'] })
      })
      .catch((e) => toast.error(e.response?.data?.detail || 'Error al guardar'))
  }, [tipo, qc])

  const onDelete = useCallback((b) => {
    if (!confirm('¿Eliminar esta bolsa?')) return
    api.delete(`/bolsas/${b.id}`).then(() => {
      qc.invalidateQueries({ queryKey: ['bolsas'] })
      qc.invalidateQueries({ queryKey: ['bolsas-resumen'] })
      toast.success('Eliminado')
    })
  }, [qc])

  const colDefs = useMemo(() => ([
    ...campos.map((c, i) => ({
      field: `f${i}`,
      headerName: c,
      editable: !readOnly,
      minWidth: 110,
      width: /observ|alcance|direcc|notas/i.test(c) ? 280 : 150,
      valueFormatter: /fecha/i.test(c) ? (p) => fmtFecha(p.value) : undefined,
    })),
    {
      headerName: '', colId: '_acc', cellRenderer: AccionesCell,
      pinned: 'right', width: 74, minWidth: 74, maxWidth: 74,
      editable: false, filter: false, sortable: false, resizable: false, suppressMovable: true,
    },
  ]), [campos, readOnly])

  const defaultColDef = useMemo(() => ({
    sortable: true, resizable: true, filter: ExcelSetFilter, headerClass: 'text-xs',
  }), [])

  return (
    <div className="ag-theme-alpine" style={{ width: '100%', height: '64vh' }}>
      <AgGridReact
        ref={gridRef}
        rowData={rows}
        columnDefs={colDefs}
        defaultColDef={defaultColDef}
        context={{ onColumnFilterChanged, onVer, onDelete, readOnly }}
        onCellValueChanged={onCellValueChanged}
        quickFilterText={quickFilter}
        getRowId={(p) => String(p.data.id)}
        stopEditingWhenCellsLoseFocus
        singleClickEdit={false}
        animateRows
        rowHeight={30}
        headerHeight={38}
      />
    </div>
  )
}
