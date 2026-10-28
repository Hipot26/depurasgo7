import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '../utils/api'
import { useAuthStore } from '../store/authStore'
import { esRolGlobal, unidadCfg, ORDEN_UNIDADES, UNIDADES } from '../config/unidades'
import { Building2, CalendarDays, Grid3x3, LayoutGrid, Users } from 'lucide-react'
import { FolderKanban } from 'lucide-react'
import PlannerTab from '../components/produccion/PlannerTab'
import MatrizTab from '../components/produccion/MatrizTab'
import ProyectosTab from '../components/produccion/ProyectosTab'
import DimensionTab from '../components/produccion/DimensionTab'
import RosterTab from '../components/produccion/RosterTab'

const TABS = [
  { id: 'planner', label: 'Planner', icon: CalendarDays },
  { id: 'matriz', label: 'Matriz', icon: Grid3x3 },
  { id: 'proyectos', label: 'Por proyecto', icon: FolderKanban },
  { id: 'dimension', label: 'Dimensionamiento', icon: LayoutGrid },
  { id: 'roster', label: 'Cuadrillas', icon: Users },
]

export default function MatrizPage() {
  const { usuario } = useAuthStore()
  const esGlobal = esRolGlobal(usuario?.rol)
  const puedeEditar = ['supervisor', 'directivo', 'desarrollador'].includes(usuario?.rol)
  const [unidad, setUnidad] = useState(usuario?.unidad || 'SABOGAL')
  const [tab, setTab] = useState('planner')
  const cfg = unidadCfg(unidad)

  // Opciones (zonas/tipos reales) compartidas por las pestañas
  const { data: opciones } = useQuery({
    queryKey: ['kpi-opciones', unidad],
    queryFn: () => api.get('/kpis/opciones', { params: { unidad } }).then((r) => r.data),
  })

  return (
    <div className="p-4 sm:p-6 space-y-4 max-w-full">
      {/* Header */}
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-lg font-bold text-gray-800 flex items-center gap-2">
            <Grid3x3 size={20} className="text-blue-700" /> Producción por Cuadrilla
          </h1>
          <p className="text-sm text-gray-500">Vista de cuadrillas · {UNIDADES[unidad]?.nombre || unidad}</p>
        </div>
        {esGlobal && (
          <div className="relative">
            <Building2 size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-blue-500 pointer-events-none" />
            <select value={unidad} onChange={(e) => setUnidad(e.target.value)} title="Unidad operativa"
              className="pl-7 pr-2 py-1.5 text-xs font-semibold text-blue-800 bg-blue-50 border border-blue-200 rounded-lg">
              {ORDEN_UNIDADES.map((u) => <option key={u} value={u}>{UNIDADES[u].corto}</option>)}
            </select>
          </div>
        )}
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b">
        {TABS.map((t) => {
          const Icon = t.icon
          return (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`flex items-center gap-1.5 px-3 py-2 text-sm border-b-2 -mb-px transition ${tab === t.id ? 'border-blue-600 text-blue-700 font-semibold' : 'border-transparent text-gray-500 hover:text-gray-800'}`}>
              <Icon size={15} /> {t.label}
            </button>
          )
        })}
      </div>

      {tab === 'planner' && <PlannerTab unidad={unidad} cfg={cfg} />}
      {tab === 'matriz' && <MatrizTab unidad={unidad} cfg={cfg} opciones={opciones} />}
      {tab === 'proyectos' && <ProyectosTab unidad={unidad} cfg={cfg} opciones={opciones} />}
      {tab === 'dimension' && <DimensionTab unidad={unidad} cfg={cfg} opciones={opciones} puedeEditar={puedeEditar} />}
      {tab === 'roster' && <RosterTab unidad={unidad} cfg={cfg} puedeEditar={puedeEditar} />}
    </div>
  )
}
