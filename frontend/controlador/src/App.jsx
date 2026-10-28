import { useEffect } from 'react'
import { esRolGlobal } from './config/unidades'
import { Routes, Route, Navigate } from 'react-router-dom'
import { useAuthStore } from './store/authStore'
import api from './utils/api'
import Layout from './components/layout/Layout'
import LoginPage from './components/auth/LoginPage'
import Dashboard from './components/dashboard/Dashboard'
import ProgramacionPage from './pages/ProgramacionPage'
import BolsasPage from './pages/BolsasPage'
import ExportarPage from './pages/ExportarPage'
import AdminPage from './pages/AdminPage'
import DevDashboardPage from './pages/DevDashboardPage'
import MatrizPage from './pages/MatrizPage'
import AnsPage from './pages/AnsPage'
import BolsasMoesPage from './pages/BolsasMoesPage'
import AuditoriaPage from './pages/AuditoriaPage'
import SgsExportePage from './pages/SgsExportePage'
import ModulosPage from './pages/ModulosPage'
import RegistrosPage from './pages/RegistrosPage'
import LiquidadorPage from './pages/LiquidadorPage'
import FacturacionPage from './pages/FacturacionPage'
import GestionPage from './pages/GestionPage'
import GestionZona from './pages/gestion/ZonaPage'            // (CHANGELOG 2026-10-25) vista para directivos (Zona)
import GestionTrabajo from './pages/gestion/TrabajoPage'      // (CHANGELOG 2026-10-25) un trabajo (cadena, qué falta, plata)
import GestionFlujo from './pages/gestion/FlujoPage'          // (CHANGELOG 2026-10-25) ¿Cómo fluye? (11 pasos, dos carriles, glosario)

// El usuario queda guardado en el navegador desde el login, así que una sesión vieja puede
// traer permisos desactualizados (p.ej. sin el campo `puede_ans`, que se añadió después).
// Al abrir la app lo volvemos a pedir para que los permisos siempre sean los de HOY.
function useRefrescarUsuario() {
  const token = useAuthStore((s) => s.token)
  const setAuth = useAuthStore((s) => s.setAuth)
  useEffect(() => {
    if (!token) return
    api.get('/auth/me')
      .then((r) => setAuth(token, r.data))
      .catch(() => {})     // si falla, se queda con lo cacheado; no rompe la sesión
  }, [token, setAuth])
}

function RequireAuth({ children }) {
  const token = useAuthStore((s) => s.token)
  return token ? children : <Navigate to="/login" replace />
}

function RequireDev({ children }) {
  const usuario = useAuthStore((s) => s.usuario)
  return usuario?.rol === 'desarrollador' ? children : <Navigate to="/" replace />
}

// Módulo ANS: lo ven el desarrollador, la cuenta ans y las cuentas autorizadas por login
// (p. ej. el directivo, con ANS pleno: editar y subir).
function RequireAns({ children }) {
  const usuario = useAuthStore((s) => s.usuario)
  // Lo decide el BACKEND (campo `puede_ans`): dev, cuenta ans, directivo autorizado y quienes diligencian.
  return usuario?.puede_ans ? children : <Navigate to="/" replace />
}

// Auditoría: desarrollador y directivo (jefe superior)
function RequireAuditoria({ children }) {
  const rol = useAuthStore((s) => s.usuario?.rol)
  return (rol === 'desarrollador' || rol === 'directivo') ? children : <Navigate to="/" replace />
}

// SGS Exporte: lo ven el dev y las cuentas autorizadas. Lo decide el BACKEND (campo `puede_sgs_exporte`).
// Liquidador Automático (módulo SGO7): dev + autorizados. Lo decide el BACKEND (`puede_liquidador`).
function RequireLiquidador({ children }) {
  const usuario = useAuthStore((s) => s.usuario)
  return usuario?.puede_liquidador ? children : <Navigate to="/" replace />
}
// Línea de facturación (panel /facturacion + Balance Integrado): rol facturacion + dev. Lo decide el BACKEND (`puede_facturacion`).
function RequireFacturacion({ children }) {
  const usuario = useAuthStore((s) => s.usuario)
  return usuario?.puede_facturacion ? children : <Navigate to="/" replace />
}
// Gestión General (22 oct 2026): un caso por proyecto con el flujo FL-03. Lo decide el BACKEND (`puede_gestion`).
function RequireGestion({ children }) {
  const usuario = useAuthStore((s) => s.usuario)
  return usuario?.puede_gestion ? children : <Navigate to="/" replace />
}
// Módulo embebido: el dev abre cualquiera; quien puede_liquidador solo el Liquidador; quien puede_facturacion el Balance.
function RequireModulo({ children }) {
  const usuario = useAuthStore((s) => s.usuario)
  const id = window.location.pathname.split('/')[2] || ''
  const ok = usuario?.rol === 'desarrollador' || (id === 'liquidador' && usuario?.puede_liquidador) || (id === 'balance' && usuario?.puede_facturacion) || (id === 'control' && usuario?.puede_control)
  return ok ? children : <Navigate to="/" replace />
}
function RequireSgs({ children }) {
  const usuario = useAuthStore((s) => s.usuario)
  return usuario?.puede_sgs_exporte ? children : <Navigate to="/" replace />
}

// Bolsas MOES: dev/directivo (ven todas las unidades) o usuarios de la unidad MOES (CHOCONTA)
function RequireMoes({ children }) {
  const usuario = useAuthStore((s) => s.usuario)
  const ok = esRolGlobal(usuario?.rol) || usuario?.unidad === 'CHOCONTA'
  return ok ? children : <Navigate to="/" replace />
}

// Los usuarios de la unidad MOES (CHOCONTA) no entran a las bolsas generales: van a su módulo.
function BloquearMoes({ children }) {
  const usuario = useAuthStore((s) => s.usuario)
  const esGlobal = esRolGlobal(usuario?.rol)
  return (!esGlobal && usuario?.unidad === 'CHOCONTA') ? <Navigate to="/bolsas-moes" replace /> : children
}

// El cumplimentador solo ve el ANS real: ni Bolsas ni el sandbox (el backend también lo
// bloquea — esto es defensa en profundidad para la URL directa).
function SoloAnsReal({ children }) {
  const rol = useAuthStore((s) => s.usuario?.rol)
  return rol === 'cumplimentador' ? <Navigate to="/ans" replace /> : children
}

// Cuentas restringidas: el cliente solo ve Bolsas; la cuenta ans solo ANS+Bolsas;
// el cumplimentador solo ANS. Cualquier otra ruta los manda a su módulo por defecto.
function Restringido({ children }) {
  const rol = useAuthStore((s) => s.usuario?.rol)
  if (rol === 'cliente') return <Navigate to="/bolsas" replace />
  if (rol === 'ans' || rol === 'cumplimentador') return <Navigate to="/ans" replace />
  return children
}

export default function App() {
  useRefrescarUsuario()   // permisos siempre frescos, aunque la sesión sea vieja
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={<RequireAuth><Layout /></RequireAuth>}
      >
        <Route index element={<Restringido><Dashboard /></Restringido>} />
        <Route path="programacion" element={<Restringido><ProgramacionPage /></Restringido>} />
        <Route path="matriz" element={<Restringido><MatrizPage /></Restringido>} />
        <Route path="bolsas" element={<SoloAnsReal><BloquearMoes><BolsasPage /></BloquearMoes></SoloAnsReal>} />
        <Route path="bolsas-moes" element={<RequireMoes><BolsasMoesPage /></RequireMoes>} />
        <Route path="exportar" element={<Restringido><ExportarPage /></Restringido>} />
        <Route path="admin" element={<Restringido><AdminPage /></Restringido>} />
        <Route path="dev" element={<RequireDev><DevDashboardPage /></RequireDev>} />
        <Route path="modulos" element={<RequireDev><ModulosPage /></RequireDev>} />
        <Route path="modulos/:id" element={<RequireModulo><ModulosPage /></RequireModulo>} />
        <Route path="registros" element={<RequireDev><RegistrosPage /></RequireDev>} />
        <Route path="liquidador" element={<RequireLiquidador><LiquidadorPage /></RequireLiquidador>} />
        <Route path="facturacion" element={<RequireFacturacion><FacturacionPage /></RequireFacturacion>} />
        {/* (CHANGELOG 2026-10-25) /gestion = Zona (directivos) · /gestion/:id = Trabajo · la vista de operador en /gestion/lista y /gestion/lista/:id */}
        <Route path="gestion" element={<RequireGestion><GestionZona /></RequireGestion>} />
        <Route path="gestion/flujo" element={<RequireGestion><GestionFlujo /></RequireGestion>} />
        <Route path="gestion/lista/:id?" element={<RequireGestion><GestionPage /></RequireGestion>} />
        <Route path="gestion/:id" element={<RequireGestion><GestionTrabajo /></RequireGestion>} />
        <Route path="ans" element={<RequireAns><AnsPage /></RequireAns>} />
        <Route path="ans-prueba" element={<SoloAnsReal><RequireAns><AnsPage apiBase="/api/prueba/ans" modo="prueba" /></RequireAns></SoloAnsReal>} />
        <Route path="auditoria" element={<RequireAuditoria><AuditoriaPage /></RequireAuditoria>} />
        <Route path="sgs-exporte" element={<RequireSgs><SgsExportePage /></RequireSgs>} />
        <Route path="reportes" element={<Restringido><Dashboard /></Restringido>} />
      </Route>
    </Routes>
  )
}
