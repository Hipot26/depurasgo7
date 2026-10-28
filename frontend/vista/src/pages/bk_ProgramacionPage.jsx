import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CalendarPlus, Loader2 } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'
import { useAuthStore } from '../store/authStore'
import ProgramacionGrid from '../components/grid/ProgramacionGrid'
import { confirmarUnidadAjena } from '../utils/unidadAjena'

const ZONAS = ['ALMEIDAS','GUAVIO','UBATE','LV&TLC']
const MESES = ['','Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre']

export default function ProgramacionPage() {
  const { usuario } = useAuthStore()
  const qc = useQueryClient()
  const now = new Date()
  const [mes, setMes] = useState(now.getMonth() + 1)
  const [anio, setAnio] = useState(now.getFullYear())
  const [zona, setZona] = useState(usuario?.zona || '')
  // Unidad activa en la grilla (los roles globales la cambian allí): la mensualidad se genera en ESA
  const [unidad, setUnidad] = useState(usuario?.unidad || 'SABOGAL')

  const puedeGenerar = ['supervisor', 'directivo', 'desarrollador'].includes(usuario?.rol)
  const generar = useMutation({
    mutationFn: (forzar) => api.post('/programaciones/generar-mes', null,
      { params: { mes: `${anio}-${String(mes).padStart(2, '0')}`, unidad, ...(forzar ? { forzar: true } : {}) } }).then((r) => r.data),
    onSuccess: (d) => {
      qc.invalidateQueries({ queryKey: ['programaciones'] })
      toast.success(
        d.creados > 0
          ? `Mensualidad generada: ${d.creados} filas nuevas (${d.lideres} líderes × ${d.dias} días).`
          : 'No se creó nada nuevo: el mes ya estaba completo.',
        { duration: 3500 },
      )
    },
    onError: (e, forzar) => {
      // Mes con contenido: ofrecer completar solo lo faltante (p. ej. líderes agregados al catálogo después)
      if (e.response?.status === 409 && !forzar) {
        if (confirm(`${e.response.data?.detail || `El mes ${MESES[mes]} ${anio} ya tiene programaciones.`}\n\n¿Quieres COMPLETAR solo lo que falta?\n\nCrea únicamente las filas de líderes/días que aún no existen (p. ej. líderes nuevos en el catálogo). No toca, no duplica y no borra nada de lo ya diligenciado.`))
          generar.mutate(true)
        return
      }
      toast.error(e.response?.data?.detail || 'No se pudo generar la mensualidad')
    },
  })
  const onGenerar = () => {
    if (!confirmarUnidadAjena(usuario, unidad)) return
    if (confirm(`¿Generar la mensualidad de ${MESES[mes]} ${anio} en vacío?\n\nCrea una fila por líder para cada día del mes (estructura lista, proceso y #PDT en blanco).\n\nSi el mes ya tiene programaciones, te preguntará si quieres completar solo lo que falte (sin duplicar ni afectar lo ya trabajado).`))
      generar.mutate(false)
  }

  return (
    <div className="flex flex-col h-[calc(100vh-3rem)] md:h-screen">
      {/* Header */}
      <div className="bg-white border-b px-4 py-2.5 shadow-sm flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between shrink-0">
        <div>
          <h1 className="text-sm font-bold text-gray-800">Programación de cuadrillas</h1>
          <p className="hidden sm:block text-xs text-gray-400">Edita directamente en la celda — los cambios se guardan automáticamente</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {/* El profesional de apoyo también elige zona: VE todas (solo edita la suya). */}
          {(usuario?.rol === 'supervisor' || usuario?.es_apoyo_ans) && (
            <select value={zona} onChange={(e) => setZona(e.target.value)} className="border rounded-lg px-2 py-1.5 text-xs text-gray-700 flex-1 sm:flex-none">
              <option value="">Todas las zonas</option>
              {ZONAS.map((z) => <option key={z}>{z}</option>)}
            </select>
          )}
          <select value={mes} onChange={(e) => setMes(+e.target.value)} className="border rounded-lg px-2 py-1.5 text-xs text-gray-700 flex-1 sm:flex-none">
            {MESES.slice(1).map((m, i) => <option key={i+1} value={i+1}>{m}</option>)}
          </select>
          <select value={anio} onChange={(e) => setAnio(+e.target.value)} className="border rounded-lg px-2 py-1.5 text-xs text-gray-700 flex-1 sm:flex-none">
            {[2025,2026,2027].map((y) => <option key={y}>{y}</option>)}
          </select>
          {puedeGenerar && (
            <button
              onClick={onGenerar}
              disabled={generar.isPending}
              title="Crea la estructura del mes (1 fila por líder por día) lista para diligenciar"
              className="flex items-center gap-1.5 bg-blue-700 hover:bg-blue-800 disabled:opacity-50 text-white text-xs font-medium px-2.5 py-1.5 rounded-lg shrink-0"
            >
              {generar.isPending ? <Loader2 size={14} className="animate-spin" /> : <CalendarPlus size={14} />}
              Generar mensualidad
            </button>
          )}
        </div>
      </div>

      {/* Grid ocupa todo */}
      <div className="flex-1 overflow-hidden">
        <ProgramacionGrid zona={zona || undefined} mes={mes} anio={anio} onUnidadChange={setUnidad} />
      </div>
    </div>
  )
}
