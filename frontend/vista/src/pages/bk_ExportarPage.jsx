import { useState, useRef } from 'react'
import { FileDown, FileSpreadsheet, Globe, Upload, CheckCircle, AlertCircle, FileText, Building2, CalendarDays } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'
import { useAuthStore } from '../store/authStore'
import { esRolGlobal, unidadCfg, ORDEN_UNIDADES, UNIDADES } from '../config/unidades'
import { confirmarUnidadAjena } from '../utils/unidadAjena'

const MESES_LABEL = ['','Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre']

export default function ExportarPage() {
  const { usuario } = useAuthStore()
  const isSupervisor = usuario?.rol === 'supervisor'
  // Importa con los tres modos y a la unidad elegida: coordinador y desarrollador (#57). Los apoyos del #44 solo su zona, en Inteligente.
  const importaTodo = isSupervisor || usuario?.rol === 'desarrollador'
  const esGlobal = esRolGlobal(usuario?.rol)
  const now = new Date()

  // Export state
  const [unidad, setUnidad] = useState(usuario?.unidad || 'SABOGAL')
  const [mes, setMes]     = useState(now.getMonth() + 1)
  const [anio, setAnio]   = useState(now.getFullYear())
  const [zona, setZona]   = useState('')
  const [loading, setLoading] = useState('')
  // Pre-planilla: por defecto el día de mañana (fecha local, no UTC)
  const [diaPre, setDiaPre] = useState(() => {
    const m = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1)
    return `${m.getFullYear()}-${String(m.getMonth() + 1).padStart(2, '0')}-${String(m.getDate()).padStart(2, '0')}`
  })
  // Planilla compacta: por defecto el día de MAÑANA, igual que la pre-planilla
  // (requisito del coordinador, 2026-09-23: la fecha del día siguiente).
  const [diaPlan, setDiaPlan] = useState(() => {
    const m = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1)
    return `${m.getFullYear()}-${String(m.getMonth() + 1).padStart(2, '0')}-${String(m.getDate()).padStart(2, '0')}`
  })
  const cfg = unidadCfg(unidad)
  const ZONAS = ['', ...cfg.zonas]

  // Import state
  const [importFile, setImportFile] = useState(null)
  const [importMode, setImportMode] = useState('smart')
  const [importing, setImporting]   = useState(false)
  const [importResult, setImportResult] = useState(null)
  const fileInputRef = useRef()

  const bajarArchivo = async (url, params, fallback) => {
    const res = await api.get(url, { params, responseType: 'blob' })
    const blob = new Blob([res.data])
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    const disp = res.headers['content-disposition'] || ''
    a.download = disp.match(/filename="(.+)"/)?.[1] || fallback
    a.click()
  }

  const descargar = async (tipo) => {
    if (!confirmarUnidadAjena(usuario, unidad)) return
    setLoading(tipo)
    try {
      const params = { mes, anio, unidad }
      if (zona) params.zona = zona
      const url = tipo === 'zona' ? '/export/excel' : '/export/excel/unificado'
      await bajarArchivo(url, params, `programacion_${mes}_${anio}.xlsx`)
      toast.success('Descarga iniciada')
    } catch {
      toast.error('Error al generar el archivo')
    } finally {
      setLoading('')
    }
  }

  const descargarPreplanilla = async () => {
    if (!diaPre) return
    if (!confirmarUnidadAjena(usuario, unidad)) return
    setLoading('preplanilla')
    try {
      const params = { fecha: diaPre, unidad }
      if (zona) params.zona = zona
      await bajarArchivo('/export/preplanilla', params, `Pre-planilla ${diaPre}.xlsx`)
      toast.success('Pre-planilla descargada')
    } catch {
      toast.error('No se pudo generar la pre-planilla')
    } finally {
      setLoading('')
    }
  }

  const descargarPlanilla = async () => {
    if (!diaPlan) return
    if (!confirmarUnidadAjena(usuario, unidad)) return
    setLoading('planilla')
    try {
      const params = { fecha: diaPlan, unidad }
      if (zona) params.zona = zona
      await bajarArchivo('/export/planilla', params, `Planilla Compacta ${diaPlan}.xlsx`)
      toast.success('Planilla descargada')
    } catch {
      toast.error('No se pudo generar la planilla')
    } finally {
      setLoading('')
    }
  }

  const descargarPlantilla = async () => {
    if (!confirmarUnidadAjena(usuario, unidad)) return
    setLoading('plantilla')
    try {
      await bajarArchivo('/export/plantilla', { unidad }, `plantilla_${unidad}.xlsx`)
      toast.success('Plantilla descargada')
    } catch {
      toast.error('No se pudo generar la plantilla')
    } finally {
      setLoading('')
    }
  }

  const handleImport = async () => {
    if (!importFile) return
    if (!confirmarUnidadAjena(usuario, unidad)) return
    setImporting(true)
    setImportResult(null)
    try {
      const form = new FormData()
      form.append('file', importFile)
      form.append('mode', importMode)
      form.append('unidad', unidad)
      const { data } = await api.post('/import/excel', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      setImportResult(data)
      if (data.inserted > 0) {
        toast.success(`${data.inserted} filas importadas`)
      } else {
        toast('Sin filas nuevas (todas ya existían)', { icon: 'ℹ️' })
      }
      setImportFile(null)
      if (fileInputRef.current) fileInputRef.current.value = ''
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Error al importar',
                  { duration: e.response?.data?.detail ? 15000 : 4000 })   // #54: el detalle dice qué corregir
    } finally {
      setImporting(false)
    }
  }

  return (
    <div className="p-4 sm:p-6 max-w-2xl space-y-6">
      <div>
        <h1 className="text-xl font-bold text-gray-800 mb-0.5">Exportar / Importar</h1>
        <p className="text-sm text-gray-500">Gestión de archivos Excel de programación</p>
      </div>

      {/* ── Filtros de exportación ─────────────────────────────────────────── */}
      <div className="bg-white rounded-xl border p-5 shadow-sm">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold text-gray-700">Parámetros del reporte</h3>
          {esGlobal ? (
            <div className="relative">
              <Building2 size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-blue-500 pointer-events-none" />
              <select value={unidad} onChange={(e) => { setUnidad(e.target.value); setZona('') }}
                className="pl-7 pr-2 py-1.5 text-xs font-semibold text-blue-800 bg-blue-50 border border-blue-200 rounded-lg">
                {ORDEN_UNIDADES.map((u) => <option key={u} value={u}>{UNIDADES[u].corto}</option>)}
              </select>
            </div>
          ) : usuario?.unidad === 'CHOCONTA' && (
            <span className="inline-flex items-center gap-1 px-2 py-1 text-xs font-semibold text-blue-800 bg-blue-50 border border-blue-200 rounded-lg">
              <Building2 size={13} /> {cfg.corto}
            </span>
          )}
        </div>
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Mes</label>
            <select value={mes} onChange={(e) => setMes(+e.target.value)} className="w-full border rounded-lg px-2 py-2 text-sm">
              {MESES_LABEL.slice(1).map((m, i) => <option key={i+1} value={i+1}>{m}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Año</label>
            <select value={anio} onChange={(e) => setAnio(+e.target.value)} className="w-full border rounded-lg px-2 py-2 text-sm">
              {[2025,2026,2027].map((y) => <option key={y}>{y}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Zona</label>
            <select value={zona} onChange={(e) => setZona(e.target.value)} className="w-full border rounded-lg px-2 py-2 text-sm">
              {ZONAS.map((z) => <option key={z} value={z}>{z || 'Todas'}</option>)}
            </select>
          </div>
        </div>
      </div>

      {/* ── Botones de exportación ─────────────────────────────────────────── */}
      <div id="exp-export" className="space-y-3">
        {/* Pre-planilla de un día — la que se envía el día anterior */}
        <div className="bg-white rounded-xl border p-4 shadow-sm flex items-center justify-between gap-3 hover:border-blue-300 transition-colors">
          <div className="flex items-start gap-3">
            <div className="p-2 bg-indigo-50 rounded-lg shrink-0"><CalendarDays size={20} className="text-indigo-600" /></div>
            <div>
              <p className="font-medium text-gray-800 text-sm">Pre-planilla del día</p>
              <p className="text-xs text-gray-500 mt-0.5">
                Lo cargado para ese día, en el formato de envío (una fila por cuadrilla, agrupada por zona y LCL).
                {zona ? ` Solo ${zona}.` : ' Todas las zonas.'}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <input type="date" value={diaPre} onChange={(e) => setDiaPre(e.target.value)}
              className="border rounded-lg px-2 py-1.5 text-sm" />
            <button onClick={descargarPreplanilla} disabled={!!loading || !diaPre} className="flex items-center gap-2 bg-indigo-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-indigo-700 disabled:opacity-60">
              <FileDown size={15} />{loading === 'preplanilla' ? 'Generando...' : 'Descargar'}
            </button>
          </div>
        </div>

        {/* Planilla compacta del día — el formato que Enel devuelve para validar */}
        <div className="bg-white rounded-xl border p-4 shadow-sm flex items-center justify-between gap-3 hover:border-blue-300 transition-colors">
          <div className="flex items-start gap-3">
            <div className="p-2 bg-sky-50 rounded-lg shrink-0"><CalendarDays size={20} className="text-sky-600" /></div>
            <div>
              <p className="font-medium text-gray-800 text-sm">Planilla del día (compacta)</p>
              <p className="text-xs text-gray-500 mt-0.5">
                Una fila por trabajo, con las cuadrillas agrupadas en JEFE CUADRILLA — «NOMBRE (TIPO)/NOMBRE (TIPO)».
                Las columnas de nodos del PMS van vacías: ese dato no existe en la Programación.
                {zona ? ` Solo ${zona}.` : ' Todas las zonas.'}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <input type="date" value={diaPlan} onChange={(e) => setDiaPlan(e.target.value)}
              className="border rounded-lg px-2 py-1.5 text-sm" />
            <button onClick={descargarPlanilla} disabled={!!loading || !diaPlan} className="flex items-center gap-2 bg-sky-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-sky-700 disabled:opacity-60">
              <FileDown size={15} />{loading === 'planilla' ? 'Generando...' : 'Descargar'}
            </button>
          </div>
        </div>

        {/* Plantilla vacía para llenar en Excel e importar */}
        <div className="bg-white rounded-xl border p-4 shadow-sm flex items-center justify-between hover:border-blue-300 transition-colors">
          <div className="flex items-start gap-3">
            <div className="p-2 bg-amber-50 rounded-lg shrink-0"><FileText size={20} className="text-amber-600" /></div>
            <div>
              <p className="font-medium text-gray-800 text-sm">Descargar plantilla vacía</p>
              <p className="text-xs text-gray-500 mt-0.5">Excel con el formato de {cfg.nombre} (solo encabezados) para llenarlo y luego importarlo.</p>
            </div>
          </div>
          <button onClick={descargarPlantilla} disabled={!!loading} className="flex items-center gap-2 bg-amber-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-amber-700 disabled:opacity-60 shrink-0">
            <FileDown size={15} />{loading === 'plantilla' ? 'Generando...' : 'Plantilla'}
          </button>
        </div>

        <div className="bg-white rounded-xl border p-4 shadow-sm flex items-center justify-between hover:border-blue-300 transition-colors">
          <div className="flex items-start gap-3">
            <div className="p-2 bg-green-50 rounded-lg shrink-0"><FileSpreadsheet size={20} className="text-green-600" /></div>
            <div>
              <p className="font-medium text-gray-800 text-sm">Excel por zona</p>
              <p className="text-xs text-gray-500 mt-0.5">Una hoja con todos los datos del mes/zona seleccionado.</p>
            </div>
          </div>
          <button onClick={() => descargar('zona')} disabled={!!loading} className="flex items-center gap-2 bg-green-600 text-white px-4 py-2 rounded-lg text-sm hover:bg-green-700 disabled:opacity-60 shrink-0">
            <FileDown size={15} />{loading === 'zona' ? 'Generando...' : 'Descargar'}
          </button>
        </div>

        <div className="bg-white rounded-xl border p-4 shadow-sm flex items-center justify-between hover:border-blue-300 transition-colors">
          <div className="flex items-start gap-3">
            <div className="p-2 bg-blue-50 rounded-lg shrink-0"><Globe size={20} className="text-blue-600" /></div>
            <div>
              <p className="font-medium text-gray-800 text-sm">Excel unificado (todas las zonas)</p>
              <p className="text-xs text-gray-500 mt-0.5">Un archivo con hoja por zona + hoja consolidada.</p>
            </div>
          </div>
          <button onClick={() => descargar('unificado')} disabled={!!loading} className="flex items-center gap-2 bg-blue-700 text-white px-4 py-2 rounded-lg text-sm hover:bg-blue-800 disabled:opacity-60 shrink-0">
            <FileDown size={15} />{loading === 'unificado' ? 'Generando...' : 'Descargar'}
          </button>
        </div>
      </div>

      {/* ── Importación — Jefe de Zona y desarrollador (#57); y los apoyos de USUARIOS_APOYO_IMPORTA_PROG: su zona, modo Inteligente (#44) */}
      {(importaTodo || usuario?.puede_importar_prog) && (
        <div id="exp-importar" className="bg-white rounded-xl border p-5 shadow-sm space-y-4">
          <div>
            <h3 className="text-sm font-semibold text-gray-700">Importar Excel de programación</h3>
            <p className="text-xs text-gray-500 mt-0.5">
              Sube un .xlsx con el formato de <b>{cfg.nombre}</b>. Lo más fácil: descarga la plantilla de arriba, llénala e impórtala.
              Solo afecta a tu unidad ({cfg.nombre}).
              {!importaTodo && <> Tu cuenta importa solo filas de tu zona (<b>{usuario?.zona}</b>) y en modo Inteligente: agrega lo nuevo y omite duplicados, nunca borra.</>}
            </p>
          </div>

          {/* Modo de importación */}
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-2">Modo de importación</label>
            <div className="space-y-2">
              {[
                { value: 'smart',        label: 'Inteligente — agrega filas nuevas, omite duplicados',           desc: 'Detecta duplicados por zona + fecha + líder' },
                { value: 'replace_zona', label: 'Reemplazar zona — borra y reimporta las zonas del archivo',     desc: 'Solo afecta las zonas presentes en el Excel' },
                { value: 'replace_all',  label: 'Reemplazar todo — limpia toda la BD antes de importar',         desc: 'Cuidado: borra todos los registros existentes' },
              ].filter(({ value }) => importaTodo || value === 'smart').map(({ value, label, desc }) => (
                <label key={value} className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${importMode === value ? 'border-blue-400 bg-blue-50' : 'border-gray-200 hover:border-gray-300'}`}>
                  <input type="radio" name="mode" value={value} checked={importMode === value} onChange={() => setImportMode(value)} className="mt-0.5" />
                  <div>
                    <p className="text-sm font-medium text-gray-700">{label}</p>
                    <p className="text-xs text-gray-500">{desc}</p>
                  </div>
                </label>
              ))}
            </div>
          </div>

          {/* Selector de archivo */}
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Archivo Excel (.xlsx)</label>
            <div
              className="border-2 border-dashed rounded-lg p-4 text-center cursor-pointer hover:border-blue-400 transition-colors"
              onClick={() => fileInputRef.current?.click()}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept=".xlsx,.xls"
                className="hidden"
                onChange={(e) => { setImportFile(e.target.files?.[0] || null); setImportResult(null) }}
              />
              {importFile ? (
                <p className="text-sm text-blue-700 font-medium">{importFile.name}</p>
              ) : (
                <div className="text-gray-400">
                  <Upload size={24} className="mx-auto mb-1" />
                  <p className="text-sm">Haz clic para seleccionar un archivo</p>
                  <p className="text-xs mt-0.5">Plantilla de {cfg.nombre} · zonas: {cfg.zonas.join(', ')}</p>
                </div>
              )}
            </div>
          </div>

          <button
            onClick={handleImport}
            disabled={!importFile || importing}
            className="w-full flex items-center justify-center gap-2 bg-blue-700 text-white py-2.5 rounded-lg text-sm font-medium hover:bg-blue-800 disabled:opacity-50"
          >
            <Upload size={15} />
            {importing ? 'Importando...' : 'Importar'}
          </button>

          {/* Resultado */}
          {importResult && (
            <div className={`rounded-lg p-4 text-sm space-y-1 ${importResult.inserted > 0 ? 'bg-green-50 border border-green-200' : 'bg-gray-50 border'}`}>
              <div className="flex items-center gap-2 font-semibold text-gray-800">
                <CheckCircle size={16} className="text-green-600" />
                Resultado de importación
              </div>
              <p className="text-gray-600">✓ <b>{importResult.inserted}</b> filas insertadas</p>
              {importResult.skipped > 0 && <p className="text-gray-500">⊘ {importResult.skipped} filas omitidas (ya existían)</p>}
              {importResult.errors?.length > 0 && (
                <div className="mt-2">
                  <div className="flex items-center gap-1 text-orange-700 font-medium mb-1"><AlertCircle size={14} /> Advertencias ({importResult.errors.length})</div>
                  <ul className="text-xs text-orange-700 space-y-0.5 max-h-32 overflow-y-auto">
                    {importResult.errors.map((e, i) => <li key={i}>• {e}</li>)}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
