import { useParams, Link, useSearchParams } from 'react-router-dom'
import { useAuthStore } from '../store/authStore'
import { LayoutGrid, ExternalLink, ArrowLeft } from 'lucide-react'

// Herramientas de apoyo a la liquidación, integradas como módulos de SGO7.
// Cada módulo corre como app propia bajo una ruta del mismo dominio (sin subdominios)
// y pide su propio usuario. Solo visible para el rol desarrollador (ver App.jsx / Layout.jsx).
export const MODULOS = [
  { id: 'balance',      nombre: 'Balance Integrado de Proyectos',   area: 'Liquidación',
    desc: 'Compara la planilla de SIPWEBORA con SGS y entrega el cruce en un Excel. Incluye el registro fotográfico RG03 y el cargue de materiales.', path: '/balance/' },
  { id: 'materiales',   nombre: 'Tablero de Materiales Pendientes', area: 'Gerencia y supervisión',
    desc: 'Material que salió de almacén y sigue sin liquidar: cuánto vale, de quién es y desde cuándo.', path: '/materiales/' },
  { id: 'control',      nombre: 'Control Mensual de Liquidación',   area: 'Liquidación',
    desc: 'Todos los proyectos liquidados en el mes y la plantilla de materiales pendientes para el Liquidador.', path: '/verificador/' },
  { id: 'devoluciones', nombre: 'Registro Digital de Devoluciones', area: 'Almacén',
    desc: 'La hoja de bandeja se fotografía con el celular; el sistema la lee, la compara con SGS y una persona la valida.', path: '/devoluciones/' },
  { id: 'liquidador',   nombre: 'Liquidador Automático',            area: 'Liquidación',
    desc: 'Registra en SGS la mano de obra y los materiales que ya fueron verificados y aprobados por una persona.', path: '/liquidador2/' },
  { id: 'centro-mo',    nombre: 'Centro MO',                        area: 'Liquidación',
    desc: 'Los correos marcados «MO» en Outlook se analizan solos (código, PDL, prestaciones, líder, fecha); el sistema arma la planilla de mano de obra y la manda al Liquidador. El agente del PC vigila el buzón.', path: '/centro-mo/' },
]

export default function ModulosPage() {
  const { id } = useParams()
  const mod = MODULOS.find((m) => m.id === id)
  if (mod) return <ModuloEmbebido mod={mod} />
  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center gap-3 mb-1">
        <LayoutGrid className="w-6 h-6 text-blue-700" />
        <h1 className="text-2xl font-bold text-gray-800">Módulos SGO7</h1>
      </div>
      <p className="text-sm text-gray-500 mb-6">Herramientas de apoyo a la liquidación integradas en la plataforma. Cada módulo pide su propio usuario.</p>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {MODULOS.map((m, i) => (
          <Link key={m.id} to={`/modulos/${m.id}`}
            className="block bg-white rounded-xl border border-gray-200 p-5 hover:shadow-md hover:border-blue-300 transition">
            <div className="text-blue-700 font-bold text-lg">{i + 1}</div>
            <h2 className="font-semibold text-gray-800 mt-1">{m.nombre}</h2>
            <p className="text-sm text-gray-600 mt-2 leading-snug">{m.desc}</p>
            <span className="inline-block mt-3 text-[11px] font-bold uppercase tracking-wider text-amber-700 bg-amber-50 px-2 py-0.5 rounded-full">{m.area}</span>
          </Link>
        ))}
      </div>
    </div>
  )
}

function ModuloEmbebido({ mod }) {
  // SGO7 facturación: ?ruta= abre una página concreta del módulo (una orden del Liquidador, o el Balance con
  // la planilla precargada para reconsultar). Solo se acepta una ruta dentro del propio módulo.
  const [params] = useSearchParams()
  const usuario = useAuthStore((s) => s.usuario)
  const ruta = params.get('ruta') || ''
  const src = ruta.startsWith(mod.path) && !ruta.startsWith('//') ? ruta : mod.path
  const volver = mod.id === 'liquidador' ? '/liquidador' : (mod.id === 'balance' && usuario?.puede_facturacion) ? '/facturacion'
    : (mod.id === 'control' && usuario?.rol !== 'desarrollador') ? '/' : '/modulos'   // (CHANGELOG 2026-10-22)
  return (
    <div className="flex flex-col" style={{ height: 'calc(100vh - 0px)' }}>
      <div className="flex items-center gap-3 px-4 py-2 bg-white border-b border-gray-200 shrink-0">
        <Link to={volver} className="text-gray-500 hover:text-blue-700" title="Volver"><ArrowLeft className="w-5 h-5" /></Link>
        <h1 className="font-semibold text-gray-800 flex-1 truncate">{mod.nombre}</h1>
        <a href={mod.path} target="_blank" rel="noopener noreferrer"
          className="text-xs text-blue-700 hover:underline flex items-center gap-1">
          <ExternalLink className="w-3.5 h-3.5" /> Abrir en pestaña nueva
        </a>
      </div>
      <iframe title={mod.nombre} src={src} className="flex-1 w-full border-0 bg-white" />
    </div>
  )
}
