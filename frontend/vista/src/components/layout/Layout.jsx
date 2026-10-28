import { useState, useEffect } from 'react'
import { esRolGlobal } from '../../config/unidades'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import api from '../../utils/api'
import { useAuthStore } from '../../store/authStore'
import {
  LayoutDashboard, Table2, BarChart3, FileDown,
  Settings, Package, Boxes, LogOut, Bot, Menu, X, Inbox, MessageCircle, MessageCircleOff, Grid3x3, Calculator, History, FlaskConical, FileSpreadsheet,
  ChevronLeft, ChevronRight
  , LayoutGrid
  , ClipboardList
  , Zap
  , Scale
  , Workflow
  , CalendarCheck
} from 'lucide-react'
import DemoBot from '../demo/DemoBot'
import ChatWidget from '../feedback/ChatWidget'

const ROL_LABELS = {
  supervisor:     'Coordinador',
  ingeniero_zona: 'Ingeniero de Zona',
  programador:    'Programador',
  facturacion:    'Facturación',
  desarrollador:  'Desarrollador',
  consulta:       'Consulta (solo lectura)',
  directivo:      'Directivo',
  cliente:        'Cliente',
  ans:            'ANS',
  moes:           'MOES',
  cumplimentador: 'Cumplimentador',
}

const UNIDAD_LABELS = { SABOGAL: 'Programados', CHOCONTA: 'No programados' }

const NAV = [
  { to: '/',             icon: LayoutDashboard, label: 'Dashboard',    short: 'Inicio',   exact: true },
  { to: '/programacion', icon: Table2,          label: 'Programación', short: 'Grilla'              },
  { to: '/matriz',       icon: Grid3x3,         label: 'Matriz Producción', short: 'Matriz', roles: ['supervisor','directivo','desarrollador','ingeniero_zona'] },
  { to: '/bolsas',       icon: Package,         label: 'Bolsas',  short: 'Bolsas'              },
  { to: '/bolsas-moes',  icon: Boxes,           label: 'Bolsas MOES',  short: 'MOES'                },
  { to: '/reportes',     icon: BarChart3,       label: 'Reportes',     short: 'Reportes'            },
  { to: '/exportar',     icon: FileDown,        label: 'Exportar',     short: 'Exportar'            },
  { to: '/admin',        icon: Settings,        label: 'Admin',        short: 'Admin',    roles: ['supervisor'] },
  { to: '/ans',          icon: Calculator,      label: 'Cálculo ANS',  short: 'ANS',      roles: ['desarrollador','ans'] },
  { to: '/ans-prueba',   icon: FlaskConical,    label: 'ANS de prueba', short: 'Prueba',  roles: ['desarrollador','ans'] },
  { to: '/sgs-exporte',  icon: FileSpreadsheet, label: 'SGS Exporte',  short: 'SGS' }, // gate por cuenta (abajo)
  { to: '/auditoria',    icon: History,         label: 'Auditoría',    short: 'Auditoría', roles: ['desarrollador','directivo'] },
  { to: '/dev',          icon: Inbox,           label: 'Buzón Dev',    short: 'Buzón',    roles: ['desarrollador'] },
  { to: '/modulos',      icon: LayoutGrid,      label: 'Módulos SGO7', short: 'Módulos',  roles: ['desarrollador'] },
  { to: '/modulos/control', icon: CalendarCheck, label: 'Control Mensual', short: 'Control' }, // gate por cuenta (abajo): puede_control, no dev
  { to: '/facturacion',  icon: Scale,           label: 'Balance de Proyectos', short: 'Balance' }, // gate por cuenta (abajo): facturación
  { to: '/gestion',      icon: Workflow,        label: 'Gestión General', short: 'Gestión' }, // gate por cuenta (abajo): puede_gestion
  { to: '/liquidador',   icon: Zap,             label: 'Liquidador Automático', short: 'Liquidador' }, // gate por cuenta (abajo)
  { to: '/registros',    icon: ClipboardList,   label: 'Registros',    short: 'Registros', roles: ['desarrollador'] },
]

export default function Layout() {
  const { usuario, logout } = useAuthStore()
  const navigate = useNavigate()
  const [showDemo,       setShowDemo]       = useState(false)
  const [showMobileMenu, setShowMobileMenu] = useState(false)
  const [chatHidden,     setChatHidden]     = useState(() => localStorage.getItem('sabogal-chat-hidden') === '1')

  // Sidebar plegable a riel de iconos: en laptop el ancho vale oro, así que en pantallas
  // angostas arranca plegada. La elección del usuario queda guardada y manda sobre el default.
  const [colapsada, setColapsada] = useState(() => {
    const guardada = localStorage.getItem('sabogal-sidebar-colapsada')
    if (guardada != null) return guardada === '1'
    return window.innerWidth < 1440
  })
  const toggleSidebar = () => setColapsada((v) => {
    localStorage.setItem('sabogal-sidebar-colapsada', v ? '0' : '1')
    return !v
  })

  // Ctrl+B pliega/expande, como en VS Code
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'b') {
        e.preventDefault()
        toggleSidebar()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const handleLogout = () => { api.post('/auth/logout').catch(() => {}); logout(); navigate('/login') }   // SGO7: borra la cookie de los módulos

  const esDev = usuario?.rol === 'desarrollador'

  // Badge de respuestas nuevas sin leer en el Buzón Dev
  const { data: fbStats } = useQuery({
    queryKey: ['feedback-stats'],
    queryFn: () => api.get('/feedback/stats').then((r) => r.data),
    enabled: esDev,
    refetchInterval: 30000,
  })
  const noLeidos = fbStats?.no_leidos || 0

  // Mantiene sincronizado el estado del chat si se desactiva desde el propio widget
  useEffect(() => {
    const sync = (e) => setChatHidden(e?.detail?.hidden ?? (localStorage.getItem('sabogal-chat-hidden') === '1'))
    window.addEventListener('sabogal-chat-set', sync)
    return () => window.removeEventListener('sabogal-chat-set', sync)
  }, [])

  const toggleChat = () => {
    const next = !chatHidden
    localStorage.setItem('sabogal-chat-hidden', next ? '1' : '0')
    setChatHidden(next)
    window.dispatchEvent(new CustomEvent('sabogal-chat-set', { detail: { hidden: next } }))
  }

  // El cliente solo ve Bolsas; la cuenta ans solo ve ANS + Bolsas.
  // Visibilidad por unidad: los usuarios de la unidad MOES (CHOCONTA) ven
  // Bolsas MOES en vez de las bolsas generales; dev/directivo (ven todas las unidades) ven ambas.
  const esGlobal = esRolGlobal(usuario?.rol)
  const esUnidadMoes = usuario?.unidad === 'CHOCONTA'
  const verMoes = esGlobal || esUnidadMoes
  const verBolsasGen = esGlobal || !esUnidadMoes

  const navItems = usuario?.rol === 'cliente'
    ? NAV.filter((n) => n.to === '/bolsas')
    : usuario?.rol === 'ans'
    ? NAV.filter((n) => n.to === '/bolsas' || n.to === '/ans' || n.to === '/ans-prueba')
    : usuario?.rol === 'cumplimentador'
    ? NAV.filter((n) => n.to === '/ans')
    : NAV.filter((n) => {
        if (n.to === '/bolsas-moes') return verMoes
        if (n.to === '/bolsas') return verBolsasGen
        // SGS Exporte: dev + cuentas autorizadas. Lo decide el backend (`puede_sgs_exporte`).
        if (n.to === '/sgs-exporte') return !!usuario?.puede_sgs_exporte
        // Liquidador Automático: dev + autorizados. Lo decide el backend (`puede_liquidador`).
        if (n.to === '/liquidador') return !!usuario?.puede_liquidador
        // Balance de Proyectos (línea de facturación): rol facturacion + dev. Lo decide el backend (`puede_facturacion`).
        if (n.to === '/facturacion') return !!usuario?.puede_facturacion
        // Control Mensual: quien puede_control y no es dev (el dev lo abre desde Módulos SGO7).
        if (n.to === '/modulos/control') return !!usuario?.puede_control && usuario?.rol !== 'desarrollador'
        // Gestión General: lo decide el backend (`puede_gestion`).
        if (n.to === '/gestion') return !!usuario?.puede_gestion
        // Cálculo ANS: lo decide el backend (campo `puede_ans` del usuario) — dev, la cuenta
        // ans, el directivo autorizado y quienes diligencian (programadoras, nodos). Sin listas duplicadas aquí.
        if (n.to === '/ans') return !!usuario?.puede_ans
        // El sandbox NO se le muestra a quien solo diligencia: evita que llenen el de prueba
        // creyendo que es el real.
        if (n.to === '/ans-prueba') return ['desarrollador', 'ans'].includes(usuario?.rol)
        return !n.roles || n.roles.includes(usuario?.rol)
      })

  return (
    <div className="flex h-screen bg-gray-100">

      {/* ── Sidebar — solo escritorio (plegable: en laptop el ancho vale oro) ── */}
      <aside className={`hidden md:flex ${colapsada ? 'w-[68px]' : 'w-56'} bg-blue-900 flex-col shadow-xl shrink-0 relative transition-[width] duration-200`}>
        {/* Pestañita en el borde para plegar/expandir, siempre a la vista */}
        <button
          onClick={toggleSidebar}
          className="absolute -right-3 top-7 z-20 w-6 h-6 rounded-full bg-blue-700 hover:bg-blue-500 text-white flex items-center justify-center shadow-md border border-blue-500"
          title={colapsada ? 'Expandir menú (Ctrl+B)' : 'Plegar menú (Ctrl+B)'}
        >
          {colapsada ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
        </button>

        <div className={`${colapsada ? 'px-2 py-4' : 'px-5 py-5'} border-b border-blue-800 shrink-0`}>
          {colapsada ? (
            <div className="w-9 h-9 mx-auto bg-white rounded-lg flex items-center justify-center" title="SG07 · Sistema de Gestión Operativa Zona VII">
              <img src="/sg07-mark.png" alt="SG07" className="h-7 w-7" />
            </div>
          ) : (
            <>
              <img src="/sg07-logo-blanco.png" alt="SG07" className="h-8 w-auto" />
              <p className="text-blue-300 text-[11px] mt-1.5">Sistema de Gestión Operativa Zona VII</p>
            </>
          )}
        </div>

        <nav className={`flex-1 min-h-0 overflow-y-auto ${colapsada ? 'px-2' : 'px-3'} py-4 space-y-1`}>
          {navItems.map(({ to, icon: Icon, label, exact }) => (
            <NavLink
              key={to} to={to} end={exact}
              title={colapsada ? label : undefined}
              className={({ isActive }) =>
                `relative flex items-center rounded-lg text-sm transition-colors ${
                  colapsada ? 'justify-center px-0 py-2.5' : 'gap-3 px-3 py-2.5'
                } ${isActive ? 'bg-blue-700 text-white font-semibold' : 'text-blue-200 hover:bg-blue-800 hover:text-white'}`
              }
            >
              <Icon size={colapsada ? 18 : 16} />
              {!colapsada && <span className="flex-1">{label}</span>}
              {to === '/dev' && noLeidos > 0 && (
                colapsada ? (
                  <span className="absolute top-0.5 right-0.5 bg-red-500 text-white text-[9px] font-bold rounded-full min-w-[16px] h-4 px-1 flex items-center justify-center">{noLeidos}</span>
                ) : (
                  <span className="bg-red-500 text-white text-[10px] font-bold rounded-full min-w-[18px] h-[18px] px-1 flex items-center justify-center">{noLeidos}</span>
                )
              )}
            </NavLink>
          ))}
        </nav>

        <div className={`${colapsada ? 'px-2' : 'px-3'} py-4 border-t border-blue-800 shrink-0`}>
          {colapsada ? (
            <div className="flex justify-center mb-2"
              title={`${usuario?.nombre || ''} · ${ROL_LABELS[usuario?.rol] || usuario?.rol || ''}${usuario?.zona ? ` · ${usuario.zona}` : ''}`}>
              <div className="w-8 h-8 rounded-full bg-blue-600 flex items-center justify-center text-white text-xs font-bold">
                {usuario?.nombre?.charAt(0).toUpperCase()}
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2 px-3 mb-2">
              <div className="w-8 h-8 rounded-full bg-blue-600 flex items-center justify-center text-white text-xs font-bold">
                {usuario?.nombre?.charAt(0).toUpperCase()}
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-white text-xs font-semibold truncate">{usuario?.nombre}</p>
                <p className="text-blue-300 text-xs truncate">{ROL_LABELS[usuario?.rol] || usuario?.rol}</p>
                {usuario?.zona && <p className="text-yellow-300 text-xs truncate">{usuario.zona}</p>}
                {usuario?.unidad && usuario.unidad !== 'SABOGAL' && (
                  <p className="text-blue-200 text-xs truncate">{UNIDAD_LABELS[usuario.unidad] || usuario.unidad}</p>
                )}
              </div>
            </div>
          )}
          <button onClick={() => setShowDemo(true)} title="Demo interactivo"
            className={`flex items-center ${colapsada ? 'justify-center' : 'gap-2'} w-full px-3 py-2 text-yellow-300 hover:text-white hover:bg-blue-800 rounded-lg text-sm transition-colors mb-1`}>
            <Bot size={colapsada ? 16 : 14} /> {!colapsada && 'Demo interactivo'}
          </button>
          {!esDev && (
            <button onClick={toggleChat} title={chatHidden ? 'Activar chat' : 'Desactivar chat'}
              className={`flex items-center ${colapsada ? 'justify-center' : 'gap-2'} w-full px-3 py-2 text-blue-300 hover:text-white hover:bg-blue-800 rounded-lg text-sm transition-colors mb-1`}>
              {chatHidden ? <MessageCircle size={colapsada ? 16 : 14} /> : <MessageCircleOff size={colapsada ? 16 : 14} />}
              {!colapsada && (chatHidden ? 'Activar chat' : 'Desactivar chat')}
            </button>
          )}
          <button onClick={handleLogout} title="Salir"
            className={`flex items-center ${colapsada ? 'justify-center' : 'gap-2'} w-full px-3 py-2 text-blue-300 hover:text-white hover:bg-blue-800 rounded-lg text-sm transition-colors`}>
            <LogOut size={colapsada ? 16 : 14} /> {!colapsada && 'Salir'}
          </button>
        </div>
      </aside>

      {/* ── Top bar móvil ────────────────────────────────────────────── */}
      <header className="md:hidden fixed top-0 inset-x-0 z-40 h-12 bg-blue-900 flex items-center justify-between px-4 shadow-lg">
        <div className="flex items-center gap-2">
          <img src="/sg07-logo-blanco.png" alt="SG07" className="h-5 w-auto" />
          {usuario?.zona && (
            <span className="text-yellow-300 text-xs font-medium">· {usuario.zona}</span>
          )}
        </div>
        <button onClick={() => setShowMobileMenu((v) => !v)} className="text-white p-1 -mr-1">
          {showMobileMenu ? <X size={20} /> : <Menu size={20} />}
        </button>
      </header>

      {/* ── Menú desplegable móvil (usuario + acciones) ───────────────── */}
      {showMobileMenu && (
        <>
          {/* Overlay */}
          <div className="md:hidden fixed inset-0 z-30 bg-black/40" onClick={() => setShowMobileMenu(false)} />
          <div className="md:hidden fixed top-12 inset-x-0 z-40 bg-blue-900 shadow-2xl border-t border-blue-800 px-3 py-3">
            <div className="flex items-center gap-3 px-3 py-2 mb-2">
              <div className="w-9 h-9 rounded-full bg-blue-600 flex items-center justify-center text-white text-sm font-bold shrink-0">
                {usuario?.nombre?.charAt(0).toUpperCase()}
              </div>
              <div className="min-w-0">
                <p className="text-white text-sm font-semibold truncate">{usuario?.nombre}</p>
                <p className="text-blue-300 text-xs capitalize">{usuario?.rol?.replace('_', ' ')}</p>
              </div>
            </div>
            <div className="border-t border-blue-800 pt-2 space-y-0.5">
              <button
                onClick={() => { setShowDemo(true); setShowMobileMenu(false) }}
                className="flex items-center gap-2 w-full px-3 py-2.5 text-yellow-300 hover:bg-blue-800 rounded-lg text-sm"
              >
                <Bot size={16} /> Demo interactivo
              </button>
              {!esDev && (
                <button
                  onClick={() => { toggleChat(); setShowMobileMenu(false) }}
                  className="flex items-center gap-2 w-full px-3 py-2.5 text-blue-300 hover:bg-blue-800 rounded-lg text-sm"
                >
                  {chatHidden ? <MessageCircle size={16} /> : <MessageCircleOff size={16} />}
                  {chatHidden ? 'Activar chat de sugerencias' : 'Desactivar chat de sugerencias'}
                </button>
              )}
              <button
                onClick={handleLogout}
                className="flex items-center gap-2 w-full px-3 py-2.5 text-blue-300 hover:bg-blue-800 rounded-lg text-sm"
              >
                <LogOut size={16} /> Cerrar sesión
              </button>
            </div>
          </div>
        </>
      )}

      {/* ── Contenido principal ──────────────────────────────────────── */}
      <main className="flex-1 overflow-auto pt-12 md:pt-0 pb-16 md:pb-0 min-w-0">
        <Outlet />
      </main>

      {/* ── Bottom nav móvil ─────────────────────────────────────────── */}
      <nav className="md:hidden fixed bottom-0 inset-x-0 z-40 bg-blue-900 border-t border-blue-800 flex h-16">
        {navItems.map(({ to, icon: Icon, short, exact }) => (
          <NavLink
            key={to} to={to} end={exact}
            onClick={() => setShowMobileMenu(false)}
            className={({ isActive }) =>
              `flex-1 flex flex-col items-center justify-center gap-0.5 text-[10px] font-medium transition-colors ${
                isActive ? 'text-white' : 'text-blue-400'
              }`
            }
          >
            {({ isActive }) => (
              <>
                <div className={`p-1 rounded-lg transition-colors ${isActive ? 'bg-blue-700' : ''}`}>
                  <Icon size={19} />
                </div>
                <span>{short}</span>
              </>
            )}
          </NavLink>
        ))}
      </nav>

      {showDemo && <DemoBot onClose={() => setShowDemo(false)} />}

      {/* Chat de sugerencias — para todos menos el desarrollador */}
      {usuario?.rol !== 'desarrollador' && <ChatWidget />}
    </div>
  )
}
