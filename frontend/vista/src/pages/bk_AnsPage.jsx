import { useState, useRef, useCallback, Fragment, Component } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../utils/api'
import toast from 'react-hot-toast'
import { Upload, Calculator, FileDown, Table2, BarChart3, Trash2, RefreshCw, FolderOpen, Download, X, Search as SearchIcon, History, Undo2, FlaskConical, AlertTriangle, ExternalLink, Bell, CheckCircle2, Users, FolderArchive, Zap, GitMerge, Copy, ImageDown, Loader2, Mail, UserCheck, FileSpreadsheet, Bot } from 'lucide-react'
import { copiarImagen, descargarImagen } from '../utils/exportImg'
import AnsDataGrid from '../components/grid/AnsDataGrid'
import LideresTab from '../components/ans/LideresTab'
import EvidenciasTab from '../components/ans/EvidenciasTab'
import NodosTab from '../components/ans/NodosTab'
import ProgramadosTab from '../components/ans/ProgramadosTab'
import AuditoriaIaTab from '../components/ans/AuditoriaIaTab'
import CorreosAnsTab from '../components/ans/CorreosAnsTab'
import ZonasFaltantes from '../components/ans/ZonasFaltantes'
import { useAuthStore } from '../store/authStore'

// Paracaídas de los tabs nuevos: un error de render muestra QUÉ falló en vez de dejar
// la página en blanco (pasó con el detalle de un correo — pantalla blanca sin pista).
class TabBoundary extends Component {
  constructor(props) { super(props); this.state = { error: null } }
  static getDerivedStateFromError(error) { return { error } }
  render() {
    if (this.state.error) {
      return (
        <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-800 space-y-2">
          <p className="font-semibold">Este apartado falló al pintarse — repórtale esto al desarrollador:</p>
          <pre className="text-xs whitespace-pre-wrap font-mono">{String(this.state.error?.stack || this.state.error)}</pre>
          <button onClick={() => this.setState({ error: null })}
            className="px-3 py-1.5 bg-white border rounded-lg text-gray-700 hover:bg-gray-50">Reintentar</button>
        </div>
      )
    }
    return this.props.children
  }
}

const hoy = new Date()
const PERIODO_DEFAULT = `${hoy.getFullYear()}-${String(hoy.getMonth() + 1).padStart(2, '0')}`

const MESES = ['', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']
// "2026-06" -> "Junio 2026"
const fmtPeriodo = (p) => {
  const [y, m] = String(p || '').split('-')
  return (y && m) ? `${MESES[Number(m)] || m} ${y}` : p
}

const pct = (v) => `${(Number(v || 0) * 100).toFixed(1)}%`
const fmtMoney = (v) => '$' + Math.round(Number(v || 0)).toLocaleString('es-CO')
// "2026-07-21T14:30:00" -> "21/07/2026 14:30"
const fmtFechaHora = (iso) => iso
  ? new Date(iso).toLocaleString('es-CO', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
  : '—'

export default function AnsPage({ apiBase = '/api/ans', modo = 'real' }) {
  const prueba = modo === 'prueba'
  // ANS pleno (ver/editar/subir) = dev, rol `ans` y cuentas autorizadas por login
  // (p. ej. el directivo, que en el resto del sistema es solo lectura). Las demás cuentas van en readOnly.
  const usuario = useAuthStore((s) => s.usuario)
  // Quien entra al ANS, edita. Lo decide el backend (`puede_ans`) — incluye a las programadoras
  // y a quien lleva los nodos, que ANTES quedaban en solo-lectura y no podían diligenciar nada.
  const readOnly = !usuario?.puede_ans
  // El cumplimentador solo diligencia celdas (su columna AQ): sin botones de carga/borrado
  // ni edición de líderes. El backend igual se lo bloquea (bloquear_ans_escritura).
  const soloCeldas = usuario?.rol === 'cumplimentador'
  // El profesional de apoyo y los ingenieros de zona ven y diligencian, pero no son dueños del
  // periodo: sin botones de carga ni de borrado (el backend lo bloquea igual, en bloquear_ans_escritura).
  const sinCargas = usuario?.es_apoyo_ans || usuario?.es_ing_zona_ans
  const qc = useQueryClient()
  // Keys de react-query namespaced por modo → la pestaña real y la de prueba no se pisan la caché
  const K = (...p) => [modo, ...p]
  const [periodo, setPeriodo] = useState(PERIODO_DEFAULT)
  // Apartado «Nodos» (quien lleva los nodos, el directivo autorizado y el dev): lo decide el backend (`puede_nodos`).
  // A quien lleva los nodos la recibe directamente su bandeja: es a lo que viene.
  const puedeNodos = !!usuario?.puede_nodos
  // El tab puede venir en la URL (?tab=auditoria-ia) — así un caso de Auditoría IA se
  // abre en una pestaña nueva del navegador (pedido 2026-08-25).
  // Pestaña inicial: la cuenta de ingeniería que lleva los nodos (rol ingeniero_zona con puede_nodos)
  // abre en «Nodos»; el resto, en la tabla.
  const [tab, setTab] = useState(() => new URLSearchParams(window.location.search).get('tab')
    || ((usuario?.puede_nodos && usuario?.rol === 'ingeniero_zona') ? 'nodos' : 'registros'))
  // «Ir al auditor» desde la celda del PDL (2026-10-01): la pestaña destino lee el caso de la URL
  // al montarse (?analisis= / ?correo= / ?pdl=) y `desde=tabla` le pinta «Volver a la tabla».
  const PARAMS_AUDITOR = ['analisis', 'correo', 'pdl', 'desde']
  const irAuditor = useCallback((dest, fila) => {
    const u = new URL(window.location.href)
    PARAMS_AUDITOR.forEach((k) => u.searchParams.delete(k))
    u.searchParams.set('tab', dest.tab)
    if (dest.id) u.searchParams.set(dest.tab === 'correos-ans' ? 'correo' : 'analisis', String(dest.id))
    else if (fila?.pdl) u.searchParams.set('pdl', fila.pdl)
    u.searchParams.set('desde', 'tabla')
    window.history.pushState({}, '', u)
    setTab(dest.tab)
  }, [])
  // Cambiar de pestaña a mano limpia el caso de la URL (si no, volver al tab lo reabriría)
  const cambiarTab = useCallback((id) => {
    const u = new URL(window.location.href)
    PARAMS_AUDITOR.forEach((k) => u.searchParams.delete(k))
    u.searchParams.delete('tab')
    window.history.replaceState({}, '', u)
    setTab(id)
  }, [])
  const [drill, setDrill] = useState(null)   // {grupo, concepto, zona, bucket, supervisor} → popup de justificación
  const [verHistorial, setVerHistorial] = useState(false)
  const [verMapeo, setVerMapeo] = useState(false)   // modal de mapeo subproceso→proceso (20')
  const [subAsig, setSubAsig] = useState(null)      // subproceso huérfano abierto: casos + asignar relación
  const progRef = useRef()
  const prog20Ref = useRef()

  const [busq, setBusq] = useState('')
  const [fTipo, setFTipo] = useState('')
  const [fZona, setFZona] = useState('')
  // Filtros estilo Excel de la pestaña Indicadores (requisito: «más familiar con el Excel»)
  const [fZonaInd, setFZonaInd] = useState('')        // zona de las tablas 3.2–3.4 y las dinámicas
  const [verTareas, setVerTareas] = useState(false)      // campanita abierta
  const [fPendiente, setFPendiente] = useState(null)     // {id, titulo, columnas} → filtra la grilla
  const [filtroRolPend, setFiltroRolPend] = useState(null)  // dev: enfocar el parpadeo de un rol
  // Subfiltro del chip «Elaboradores»: email de una de las 4 cuentas, o 'sin' = filas sin
  // dueño (sin sigla en la H). Cambiar de rol (o quitarlo) siempre lo limpia.
  const [filtroElabPend, setFiltroElabPend] = useState(null)
  const cambiarFiltroRol = (r) => { setFiltroRolPend(r); setFiltroElabPend(null) }
  const [filtroFlujo, setFiltroFlujo] = useState(null)      // filtros de flujo (solo dev): etapa activa
  const esDev = usuario?.rol === 'desarrollador'
  // Tab «Auditoría IA» (analista de WhatsApp): dev + directivo autorizado. Lo decide el
  // backend (config.puede_auditoria_wa → UsuarioOut), como puede_ans/puede_nodos.
  const puedeAuditoria = esDev || !!usuario?.puede_auditoria_wa
  // Tab «Correos ANS»: dev + directivo autorizado (config.puede_correos_ans)
  const puedeCorreos = esDev || !!usuario?.puede_correos_ans

  // Profesional de apoyo de una zona. Quién lo es y de qué zona lo decide
  // el backend leyendo el DIRECTORIO, no el front: así un traslado se arregla en el tab Líderes.
  // Cubre también las cuentas de zona (Ing. Almeidas/Guavio/LV&TLC/Ubate): son la misma persona.
  const { data: miApoyo } = useQuery({
    queryKey: K('ans-mi-apoyo'),
    queryFn: async () => (await api.get('/mi-apoyo', { baseURL: apiBase })).data,
    staleTime: 5 * 60 * 1000,
  })
  const esApoyo = !!miApoyo?.es_apoyo
  // El apoyo entra directo a sus casos: es a lo que viene. La cuenta de zona no: ella viene a
  // ver el cálculo completo (el filtro queda a un clic). Ambos pueden encender/apagar.
  const [soloMisCasos, setSoloMisCasos] = useState(!usuario?.es_ing_zona_ans)
  const misCasos = esApoyo && soloMisCasos
  // Ingeniero de zona (cuenta de zona o profesional de apoyo — misma persona según el
  // directorio): su pendiente es el del encargado ANS pero SOLO los casos de su zona.
  // María Inés (lleva los nodos → puede_nodos) conserva su tarea 3.2 C y no entra aquí.
  const esIngZonaPend = usuario?.rol === 'ingeniero_zona' && !usuario?.puede_nodos
  const zonaPend = esIngZonaPend ? (miApoyo?.zona || null) : null

  // Una sola definición de la clave: si el guardado usa otra, la fila fresca nunca entra en
  // caché y la celda se queda parpadeando hasta recargar (era exactamente el bug).
  const listKey = K('ans', periodo, busq, fTipo, fZona, fPendiente?.id, misCasos)

  const { data, isLoading } = useQuery({
    queryKey: listKey,
    queryFn: async () => (await api.get('', {
      baseURL: apiBase,
      params: { periodo, q: busq || undefined, tipo: fTipo || undefined, zona: fZona || undefined,
                pendiente: fPendiente?.id || undefined, apoyo: misCasos || undefined },
    })).data,
  })
  const items = data?.items || []

  // Filtros de flujo (solo dev): etapas del circuito de trabajo sobre los casos NO-excluibles.
  const { data: flujo } = useQuery({
    queryKey: K('ans-flujo', periodo),
    queryFn: async () => (await api.get('/flujo/no-excluibles', { baseURL: apiBase, params: { periodo } })).data,
    enabled: esDev && tab === 'registros',
    refetchInterval: 60000,
  })

  // Elaborador (una de las 4 cuentas de programación+cumplimentación, ago-2026): sus filas
  // son las que la sigla de la col H le atribuye (`_elab` del backend) + las sin dueño.
  // Rige el parpadeo del grid, el conteo del chip y este filtro — todo con el mismo criterio.
  const esElab = !!usuario?.es_elaborador
  const filaMia = (it) => !esElab || !it._elab || it._elab === usuario?.email
  // Clic en un rol de la leyenda → solo las filas que ESE rol tiene pendientes. Con el
  // subfiltro por persona activo, además solo las filas de ESA cuenta (o las sin dueño).
  const deEsaCuenta = (it) => !filtroElabPend
    || (filtroElabPend === 'sin' ? !it._elab : it._elab === filtroElabPend)
  // SUPERVISOR PRIMERO (requisito 2026-09-29): órdenes del elaborador (o sin dueño) sin
  // responsable en la G. Mientras haya alguna, su filtro de pendientes se queda en ellas: sin
  // supervisor no hay zona, y sin zona el 3.2 B no sabe a qué profesional de apoyo preguntarle.
  // Con la campanita (filtro del servidor) el mismo bloqueo lo aplica el backend.
  const sinSupervisor = esElab ? items.filter((it) => filaMia(it) && it._pend?.nombre_responsable) : []
  const bloqueoChip = esElab && filtroRolPend === 'elaborador' && sinSupervisor.length > 0
  const bloqueoResp = bloqueoChip ? sinSupervisor.length : (fPendiente ? (data?.bloqueo_responsable || 0) : 0)
  const itemsVista0 = bloqueoChip ? sinSupervisor
    : filtroRolPend
    ? items.filter((it) => {
        // Chip del ingeniero de zona: las MISMAS celdas del encargado ANS, solo su zona.
        if (filtroRolPend === 'ing_zona')
          return Object.values(it._pend || {}).includes('ans')
            && !!zonaPend && (it.zona || '').toUpperCase() === zonaPend
        return Object.values(it._pend || {}).includes(filtroRolPend)
          && (filtroRolPend !== 'elaborador' || (filaMia(it) && deEsaCuenta(it)))
      })
    : items
  // Filtro de flujo activo → solo los regs cuyo caso está en esa etapa.
  const idsFlujo = filtroFlujo
    ? new Set((flujo?.casos || []).filter((c) => c.etapas.includes(filtroFlujo)).map((c) => c.reg_id))
    : null
  const itemsVista = idsFlujo ? itemsVista0.filter((it) => idsFlujo.has(it.id)) : itemsVista0

  const { data: tareas } = useQuery({
    queryKey: K('ans-tareas', periodo),
    queryFn: async () => (await api.get('/tareas', { baseURL: apiBase, params: { periodo } })).data,
    refetchInterval: 60000,
  })

  const { data: ind, isFetching: indLoading } = useQuery({
    queryKey: K('ans-ind', periodo),
    queryFn: async () => (await api.get('/indicadores', { baseURL: apiBase, params: { periodo } })).data,
    enabled: tab === 'indicadores',
  })

  const { data: anom } = useQuery({
    queryKey: K('ans-anomalias', periodo),
    queryFn: async () => (await api.get('/anomalias', { baseURL: apiBase, params: { periodo } })).data,
    enabled: tab === 'indicadores',
  })

  const { data: indPlus } = useQuery({
    queryKey: K('ans-indplus', periodo),
    queryFn: async () => (await api.get('/indicadores-plus', { baseURL: apiBase, params: { periodo } })).data,
    enabled: tab === 'indicadores',
  })

  // 5.2 — vive en el módulo 3.Programados (sus propias tablas), no en las del ANS
  const ind52 = useQuery({
    queryKey: K('prog52', periodo),
    queryFn: async () => (await api.get('/indicadores',
      { baseURL: apiBase.replace('/ans', '/programados'), params: { periodo } })).data,
    enabled: esDev && tab === 'indicadores',
  })

  const subirProg20 = async () => {
    const f = prog20Ref.current?.files?.[0]
    if (prog20Ref.current) prog20Ref.current.value = ''
    if (!f) return
    const tid = toast.loading('Cargando programación de los 20’…')
    try {
      const fd = new FormData(); fd.append('file', f)
      const r = (await api.post('/prog20', fd, { baseURL: apiBase })).data
      const meses = Array.isArray(r.meses) ? r.meses.join(', ') : '—'
      toast.success(`Programación 20’ (hoja «${r.hoja || '?'}»): ${r.filas} filas · meses ${meses} · ${r.subprocesos_nuevos} subprocesos nuevos`, { id: tid, duration: 8000 })
      const d = r.descartadas || {}
      const nDesc = (d.zona_desconocida || 0) + (d.sin_fecha || 0)
      if (nDesc > 0) toast(`${nDesc} fila(s) descartadas: ${d.zona_desconocida || 0} con zona irreconocible${Object.keys(r.zonas_raras || {}).length ? ` (${Object.keys(r.zonas_raras).join(', ')})` : ''} y ${d.sin_fecha || 0} sin fecha de ejecución`, { icon: '⚠', duration: 10000 })
      if (r.sin_mapa_35?.length) {
        toast(`${r.sin_mapa_35.length} subproceso(s) sin proceso 3.5 — no cuentan en el PLANEADO: ${r.sin_mapa_35.slice(0, 6).join(', ')}${r.sin_mapa_35.length > 6 ? '…' : ''}. Si alguno debe contar, asígnalo en «Mapeo 20’».`, { duration: 12000 })
      } else if (r.subprocesos_nuevos > 0) {
        toast('Revisa el mapeo de subprocesos → proceso (botón «Mapeo 20’»)', { duration: 9000 })
      }
      qc.invalidateQueries({ queryKey: K('ans-indplus') })
      qc.invalidateQueries({ queryKey: K('ans-prog20-archivos') })
      qc.invalidateQueries({ queryKey: K('ans-nodos') })   // el mismo archivo alimenta la bandeja de nodos
    } catch (e) {
      toast.error(e.response?.data?.detail || 'No se pudo cargar la programación', { id: tid })
    }
  }

  const { data: fuentes } = useQuery({
    queryKey: K('ans-fuentes', periodo),
    queryFn: async () => (await api.get('/fuentes', { baseURL: apiBase, params: { periodo } })).data,
    enabled: tab === 'fuentes',
  })

  const { data: dups } = useQuery({
    queryKey: K('ans-duplicados'),
    queryFn: async () => (await api.get('/duplicados', { baseURL: apiBase })).data,
    enabled: tab === 'fuentes',
  })

  const { data: prog20Files } = useQuery({
    queryKey: K('ans-prog20-archivos'),
    queryFn: async () => (await api.get('/prog20/archivos', { baseURL: apiBase })).data,
    enabled: tab === 'fuentes',
  })

  // Periodos existentes (para el selector). Siempre activo.
  const { data: periodos } = useQuery({
    queryKey: K('ans-periodos'),
    queryFn: async () => (await api.get('/periodos', { baseURL: apiBase })).data,
  })
  // Lista de opciones: rango de meses anclado a un punto FIJO (el mes más reciente entre el
  // actual, los que tienen datos y el seleccionado) → así ningún mes se esconde al navegar.
  const opcionesPeriodo = (() => {
    const counts = new Map((periodos || []).map((p) => [p.periodo, p.registros]))
    const candidatos = [PERIODO_DEFAULT, periodo, ...counts.keys()].sort()
    const ancla = candidatos[candidatos.length - 1]         // YYYY-MM más reciente
    const [ay, am] = ancla.split('-').map(Number)
    const set = new Set([...counts.keys(), periodo])
    for (let i = -2; i < 24; i++) {                          // 2 meses adelante + 24 atrás del ancla
      const d = new Date(ay, am - 1 - i, 1)
      set.add(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`)
    }
    return [...set].sort((a, b) => b.localeCompare(a)).map((p) => [p, counts.get(p) || 0])
  })()

  const descargarFuente = async (f) => {
    const res = await api.get(`/fuentes/${f.id}`, { baseURL: apiBase, responseType: 'blob' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([res.data]))
    a.download = f.nombre
    a.click(); URL.revokeObjectURL(a.href)
  }

  // ZIP de procedencia del mes: todos los archivos fuente, encarpetados por tipo y con
  // la fecha de subida en el nombre (mismo espíritu que el export de Evidencias)
  const [expFuentes, setExpFuentes] = useState(false)
  const exportarFuentes = async () => {
    setExpFuentes(true)
    const tid = toast.loading('Empaquetando la procedencia del mes…')
    try {
      const res = await api.get('/fuentes/exportar', { baseURL: apiBase, params: { periodo }, responseType: 'blob' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([res.data]))
      a.download = `Procedencia_ANS_${periodo}.zip`
      a.click(); URL.revokeObjectURL(a.href)
      toast.success('Procedencia del mes exportada', { id: tid })
    } catch (e) {
      toast.error(e?.response?.status === 404 ? 'Sin archivos fuente este mes' : 'No se pudo exportar', { id: tid })
    } finally { setExpFuentes(false) }
  }

  const importar = useMutation({
    mutationFn: async ({ file, tipo }) => {
      const fd = new FormData()
      fd.append('file', file)
      return (await api.post('/import', fd, { baseURL: apiBase, params: { tipo, periodo } })).data
    },
    onSuccess: (r) => {
      toast.success(`${r.tipo}: +${r.insertados} nuevas, ${r.actualizados} actualizadas`)
      ;(r.avisos || []).forEach((a) => toast(a, { icon: '⚠', duration: 9000, style: { background: '#fffbeb', color: '#92400e', border: '1px solid #fcd34d', maxWidth: 480 } }))
      qc.invalidateQueries({ queryKey: K('ans') })
      qc.invalidateQueries({ queryKey: K('ans-periodos') })
      qc.invalidateQueries({ queryKey: K('ans-duplicados') })
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'Error al importar'),
  })

  const patch = useMutation({
    mutationFn: async ({ id, campo, valor }) =>
      (await api.patch(`/${id}`, { [campo]: valor }, { baseURL: apiBase })).data,
    onSuccess: (row, vars) => {
      // La fila que devuelve el PATCH trae el _pend recalculado (p.ej. al decidir una exclusión,
      // ahora queda pendiente su razón). Se mete en la MISMA clave que usa la lista.
      qc.setQueryData(listKey, (old) =>
        old ? { ...old, items: old.items.map((it) => (it.id === row.id ? row : it)) } : old)
      qc.invalidateQueries({ queryKey: K('ans-tareas') })   // la campanita también baja el conteo
      // Supervisor escrito en la G: la zona se deduce en el acto (2026-09-29), y el filtro del
      // elaborador puede liberarse → la lista del servidor se pide de nuevo.
      if (row._zona_deducida) {
        const { zona, metodo } = row._zona_deducida
        if (zona) toast.success(`Con ese supervisor la orden quedó en ${zona}${metodo === 'consenso'
          ? '. Es deducida: la celda Zona dice por qué' : ''}.`, { duration: 7000 })
        else toast('Ese supervisor no está en el directorio y las señales no alcanzan: la orden sigue sin zona y va al Paso 1 (preguntar a programación).',
          { duration: 9000 })
      }
      if (vars?.campo === 'nombre_responsable') qc.invalidateQueries({ queryKey: K('ans') })
      // AL MARCAR UNA EXCLUSIÓN el servidor re-verifica los correos que nombran la orden
      // (2026-10-02, caso R09687: se marcó NO con un correo del caso descartado cuando la orden
      // aún no estaba en la tabla). Si hay alguno sin adjuntar, se avisa aquí mismo, con salida.
      if (row._correos_caso?.correos?.length) {
        const { decision, correos } = row._correos_caso
        const c0 = correos.find((c) => c.respuesta_enel) || correos[0]
        const vuelto = correos.some((c) => c.revivido)
        toast((t) => (
          <span className="text-sm">
            <b>Ojo antes de dejar ese {decision}:</b> {correos.length === 1 ? 'hay un correo' : `hay ${correos.length} correos`} que
            {correos.length === 1 ? ' nombra' : ' nombran'} esta orden y no {correos.length === 1 ? 'está adjunto' : 'están adjuntos'}
            {c0.respuesta_enel ? ` (Enel ${c0.respuesta_enel === 'NIEGA' ? 'negó' : 'aceptó'} la exclusión)` : ''}.
            {vuelto ? ' Estaba descartado: volvió a la bandeja.' : ''}{' '}
            <button onClick={() => { toast.dismiss(t.id); irAuditor({ tab: 'correos-ans', id: c0.id }, row) }}
              className="underline font-semibold text-sky-700">Abrir el correo</button>
          </span>
        ), { icon: '', duration: 15000 })
        qc.invalidateQueries({ queryKey: ['correos-ans'] })
      }
      // Aprendió la zona de un responsable → lo dejará resuelto en los próximos imports
      if (row._aprendido_zona) {
        const { responsable, zona, tambien, deducidas } = row._aprendido_zona
        const mas = tambien ? ` y ${tambien} fila${tambien > 1 ? 's' : ''} más de esta persona` : ''
        const ded = deducidas ? ` Con eso se resolvieron ${deducidas} orden${deducidas > 1 ? 'es' : ''} más sin zona.` : ''
        toast.success(`Aprendido: ${responsable} → ${zona}${mas}. Los próximos imports lo pondrán solos.${ded}`,
          { duration: 7000 })
        if (tambien || deducidas) qc.invalidateQueries({ queryKey: K('ans') })   // refresca las hermanas
      }
    },
    onError: () => toast.error('No se pudo guardar'),
  })

  const borrarPeriodo = useMutation({
    mutationFn: async () => (await api.delete(`/periodo/${periodo}`, { baseURL: apiBase })).data,
    onSuccess: () => { toast.success('Periodo limpiado'); qc.invalidateQueries({ queryKey: K('ans') }); qc.invalidateQueries({ queryKey: K('ans-periodos') }) },
  })

  const resetTodo = useMutation({
    mutationFn: async () => (await api.delete('/reset', { baseURL: apiBase })).data,
    onSuccess: (r) => {
      toast.success(`Sandbox vaciado · ${r.filas_borradas} filas borradas`)
      qc.invalidateQueries({ queryKey: [modo] })
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo vaciar'),
  })

  const exportar = async () => {
    try {
      const res = await api.get('/export', { baseURL: apiBase, params: { periodo }, responseType: 'blob' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([res.data]))
      a.download = `Calculo_ANS_${periodo}${prueba ? '_PRUEBA' : ''}.xlsx`
      a.click()
      URL.revokeObjectURL(a.href)
    } catch (e) {
      toast.error(e.response?.status === 404 ? 'Sin registros en el periodo' : 'Error al exportar')
    }
  }

  // Correo de estado (solo dev): el server arma asunto + cuerpo HTML con las tablas y aquí
  // solo se DESCARGA como archivo. El Cazador (en el PC) lo detecta en Descargas, arma el
  // borrador en Outlook Classic y el envío es siempre manual — la plataforma no envía nada.
  const descargarCorreo = async () => {
    try {
      const d = (await api.get('/correo-estado', { baseURL: apiBase, params: { periodo } })).data
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], { type: 'application/json' }))
      a.download = `CORREO_ANS_${d.periodo}_corte_${String(d.corte).replaceAll('/', '-')}.json`
      a.click()
      URL.revokeObjectURL(a.href)
      toast.success('Correo generado y descargado. Ábrelo en el Cazador → Correo ANS.', { duration: 7000 })
    } catch (e) {
      toast.error(e.response?.data?.detail || 'No se pudo generar el correo')
    }
  }

  // Sube uno o varios CD10 a la vez; cada archivo se auto-clasifica por mes en el backend.
  const postImport = async (file, tipo) =>
    (await api.post('/import', (() => { const fd = new FormData(); fd.append('file', file); return fd })(),
      { baseURL: apiBase, params: { tipo, periodo } })).data

  const onFiles = async (ref, tipo) => {
    const files = Array.from(ref.current?.files || [])
    if (ref.current) ref.current.value = ''
    if (!files.length) return
    if (files.length === 1) { importar.mutate({ file: files[0], tipo }); return }
    // Varios: procesar en serie y resumir en un solo aviso.
    const tid = toast.loading(`Subiendo 0/${files.length}…`)
    let ins = 0, act = 0, fail = 0
    const meses = {}, tipos = {}, alertas = new Set()
    for (let i = 0; i < files.length; i++) {
      try {
        const r = await postImport(files[i], tipo)
        ins += r.insertados; act += r.actualizados
        tipos[r.tipo] = (tipos[r.tipo] || 0) + 1
        ;(r.avisos || []).filter((a) => a.startsWith('⚠')).forEach((a) => alertas.add(`${files[i].name}: ${a}`))
        Object.entries(r.reparto || {}).forEach(([m, n]) => { meses[m] = (meses[m] || 0) + n })
      } catch { fail++ }
      toast.loading(`Subiendo ${i + 1}/${files.length}…`, { id: tid })
    }
    const resumenTipos = Object.entries(tipos).map(([t, n]) => `${n} ${t}`).join(' + ')
    toast.success(`${files.length} archivos (${resumenTipos || '—'}) · +${ins} nuevas, ${act} actualizadas${fail ? ` · ${fail} con error` : ''}`, { id: tid, duration: 8000 })
    ;[...alertas].slice(0, 4).forEach((a) => toast(a, { icon: '⚠', duration: 10000, style: { background: '#fffbeb', color: '#92400e', border: '1px solid #fcd34d', maxWidth: 520 } }))
    const rep = Object.entries(meses).sort().map(([m, n]) => `${m}: ${n}`).join('  ·  ')
    if (rep) toast(`Clasificado por mes → ${rep}`, { duration: 10000, style: { background: '#eff6ff', color: '#1e40af', border: '1px solid #bfdbfe', maxWidth: 520 } })
    qc.invalidateQueries({ queryKey: K('ans') })
    qc.invalidateQueries({ queryKey: K('ans-periodos') })
    qc.invalidateQueries({ queryKey: K('ans-duplicados') })
  }

  const saveCell = (id, campo, valor) => { if (readOnly) return; patch.mutate({ id, campo, valor }) }

  return (
    <div className="p-4 md:p-6 max-w-[1700px] mx-auto">
      {/* Cabecera */}
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <div className="flex items-center gap-2">
          {prueba ? <FlaskConical className="text-purple-700" size={22} /> : <Calculator className="text-blue-700" size={22} />}
          <h1 className="text-xl font-bold text-gray-800">{prueba ? 'Cálculo ANS de prueba' : 'Cálculo ANS'}</h1>
          {prueba
            ? <span className="text-xs bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full">Beta · sandbox</span>
            : <span className="text-xs bg-amber-100 text-amber-700 px-2 py-0.5 rounded-full">Real</span>}
        </div>
        {/* Campanita: qué le toca llenar a cada rol */}
        <div className="ml-auto relative">
          <button onClick={() => setVerTareas((v) => !v)} title="Tareas ANS que te corresponden"
            className="relative flex items-center gap-1.5 px-2.5 py-1.5 text-sm border rounded-lg bg-white hover:bg-gray-50">
            <Bell size={16} className={tareas?.total_pendientes ? 'text-amber-600' : 'text-gray-400'} />
            {tareas?.total_pendientes > 0 && (
              <span className="absolute -top-1.5 -right-1.5 bg-red-600 text-white text-[10px] rounded-full px-1.5 py-0.5 min-w-[18px] text-center leading-none">{tareas.total_pendientes}</span>
            )}
          </button>
          {verTareas && (
            <PanelTareas tareas={tareas} onCerrar={() => setVerTareas(false)}
              onVerFilas={(t) => { setFPendiente({ id: t.id, titulo: t.titulo, columnas: t.columnas }); setTab('registros'); setVerTareas(false) }} />
          )}
        </div>
        <label className="text-sm text-gray-600">Periodo</label>
        {/* Dropdown de meses que ya existen en el sistema */}
        <select
          value={periodo} onChange={(e) => setPeriodo(e.target.value)}
          className="px-2 py-1.5 text-sm border rounded-lg bg-white min-w-[11rem]"
          title="Meses cargados en el sistema"
        >
          {opcionesPeriodo.map(([p, n]) => (
            <option key={p} value={p}>{fmtPeriodo(p)}{n ? ` · ${n} reg.` : ' · (vacío)'}</option>
          ))}
        </select>
        {/* Selector nativo de mes/año para ir a / crear cualquier periodo */}
        <input
          type="month" value={periodo} onChange={(e) => e.target.value && setPeriodo(e.target.value)}
          className="px-2 py-1.5 text-sm border rounded-lg text-gray-600"
          title="Elegir otro mes/año"
        />
        <button onClick={() => { qc.invalidateQueries({ queryKey: K('ans') }); qc.invalidateQueries({ queryKey: K('ans-periodos') }) }}
          className="p-2 text-gray-500 hover:text-blue-700" title="Refrescar"><RefreshCw size={16} /></button>
      </div>

      {/* Banner de sandbox */}
      {prueba && (
        <div className="mb-4 bg-purple-50 border border-purple-200 rounded-lg px-4 py-2.5 text-sm text-purple-900 flex items-start gap-2">
          <FlaskConical size={16} className="mt-0.5 shrink-0" />
          <span>
            <b>Entorno de pruebas.</b> Sube plantillas/CD10 y experimenta libremente: estos datos están <b>aislados</b> de los reales.
            Úsalo para probar estructuras nuevas y detectar bugs. Cuando quieras empezar de cero, pulsa <b>Borrar todo</b>.
          </span>
        </div>
      )}

      {/* Acciones de carga / export */}
      <div className="flex flex-wrap gap-2 mb-4">
        {/* UN solo botón para los STM (antes eran dos y subir un ST por el botón de CD10
            importaba 0 filas en silencio — bug de jul-2026): el backend detecta FT/ST por
            el protocolo del archivo y clasifica solo. */}
        <input ref={progRef} type="file" accept=".xlsx" hidden multiple onChange={() => onFiles(progRef, 'auto')} />
        <input ref={prog20Ref} type="file" accept=".xlsx" hidden onChange={subirProg20} />
        {!readOnly && !soloCeldas && !sinCargas && (<>
        <button onClick={() => progRef.current?.click()} title="Sube CD10 y Línea Viva juntos o por separado: el sistema detecta el tipo (FT/ST) por el protocolo y clasifica solo por mes"
          className="flex items-center gap-2 px-3 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700">
          <Upload size={15} /> Subir STM (CD10 + Línea Viva)
        </button>
        <button onClick={() => prog20Ref.current?.click()} title="Sube la «Programación Unificada» (hoja P. UNIFICADA, la de los 20') — alimenta el PLANEADO de 3.5"
          className="flex items-center gap-2 px-3 py-2 text-sm bg-rose-600 text-white rounded-lg hover:bg-rose-700">
          <Upload size={15} /> Programación 20’
        </button>
        <button onClick={() => setVerMapeo(true)} title="Editar el mapeo subproceso → proceso 3.5"
          className="flex items-center gap-2 px-3 py-2 text-sm bg-white border border-rose-300 text-rose-700 rounded-lg hover:bg-rose-50">
          <RefreshCw size={15} /> Mapeo 20’
        </button>
        {esDev && <ZonasFaltantes apiBase={apiBase} />}
        </>)}
        <button onClick={exportar}
          className="flex items-center gap-2 px-3 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700">
          <FileDown size={15} /> Exportar Calculo_ANS
        </button>
        {!readOnly && !soloCeldas && !sinCargas && (
        <div className="ml-auto flex gap-2">
          <button onClick={() => { if (confirm(`¿Borrar todos los registros de ${periodo}?`)) borrarPeriodo.mutate() }}
            className="flex items-center gap-2 px-3 py-2 text-sm text-red-600 border border-red-200 rounded-lg hover:bg-red-50">
            <Trash2 size={15} /> Limpiar periodo
          </button>
          {prueba && (
            <button onClick={() => { if (confirm('¿Borrar TODOS los datos del entorno de prueba (todos los periodos, fuentes e historial)? Esto NO afecta los datos reales.')) resetTodo.mutate() }}
              className="flex items-center gap-2 px-3 py-2 text-sm text-white bg-red-600 rounded-lg hover:bg-red-700">
              <Trash2 size={15} /> Borrar todo (sandbox)
            </button>
          )}
        </div>
        )}
      </div>
      {readOnly && (
        <div className="mb-4 -mt-1 bg-amber-50 border border-amber-200 rounded-lg px-4 py-2 text-sm text-amber-900 flex items-center gap-2">
          <AlertTriangle size={15} className="shrink-0" />
          <span>Estás viendo el módulo ANS en <b>solo lectura</b>: puedes consultar y exportar, pero no modificar datos.</span>
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 border-b mb-3">
        {[['registros', 'Registros (data)', Table2], ...(puedeNodos ? [['nodos', 'Nodos (3.2 C)', GitMerge]] : []), ['indicadores', 'Indicadores', BarChart3], ['fuentes', 'Procedencia (STM + 20’)', FolderOpen], ['evidencias', 'Evidencias', FolderArchive], ['lideres', 'Líderes', Users],
          // Apartado nuevo, aún en construcción: solo la cuenta del desarrollador
          ...(esDev ? [['programados', '3.Programados', FileSpreadsheet]] : []),
          // Correos ANS y Auditoría IA: dev + directivo autorizado (banderas del backend)
          ...(puedeCorreos ? [['correos-ans', 'Correos ANS', Mail]] : []),
          ...(puedeAuditoria ? [['auditoria-ia', 'Auditoría IA', Bot]] : [])].map(([id, label, Icon]) => (
          <button key={id} onClick={() => cambiarTab(id)}
            className={`flex items-center gap-1.5 px-4 py-2 text-sm font-medium -mb-px border-b-2 ${
              tab === id ? 'border-blue-600 text-blue-700' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
            <Icon size={15} /> {label}
          </button>
        ))}
        <span className="ml-auto self-center text-xs text-gray-400">{data?.total ?? 0} registros</span>
      </div>

      {tab === 'registros' && (
        <>
          {/* Profesional de apoyo: sus casos son las anuladas de su zona — de esas es de las que
              consigue el correo del Centro Operativo que sustenta la exclusión 3.2 B. */}
          {esApoyo && (
            <div className={`mb-3 rounded-lg p-3 flex flex-wrap items-center gap-3 text-xs border ${
              misCasos ? 'bg-teal-50 border-teal-300' : 'bg-gray-50 border-gray-200'}`}>
              <UserCheck size={16} className={misCasos ? 'text-teal-600 shrink-0' : 'text-gray-400 shrink-0'} />
              <p className={misCasos ? 'text-teal-900' : 'text-gray-600'}>
                {misCasos ? (<>
                  <b>Mis casos</b> · eres el profesional de apoyo de <b>{miApoyo?.zona}</b>. Estás viendo solo las
                  órdenes de tu zona que <b>anuló el Centro Operativo</b>: de esas es de las que se necesita el
                  correo de ENEL con el motivo de la cancelación (indicador 3.2 B).
                  <span className="text-teal-700"> ({data?.total ?? 0} orden{(data?.total ?? 0) !== 1 ? 'es' : ''} en {fmtPeriodo(periodo)})</span>
                </>) : (<>Estás viendo <b>todo el periodo</b>, no solo tus casos de {miApoyo?.zona}.</>)}
              </p>
              <button onClick={() => setSoloMisCasos((v) => !v)}
                className="ml-auto shrink-0 px-2 py-1 bg-white border rounded hover:bg-gray-100">
                {misCasos ? 'Ver todo el periodo' : 'Ver solo mis casos'}
              </button>
            </div>
          )}
          {/* Filtro activo desde la campanita: solo las filas que le faltan a ese rol */}
          {fPendiente && (
            <div className="mb-3 bg-amber-50 border border-amber-300 rounded-lg p-3 flex items-center gap-3 text-xs">
              <Bell size={16} className="text-amber-600 shrink-0" />
              <p className="text-amber-900">
                Mostrando solo las filas <b>pendientes</b> de «{fPendiente.titulo}» — te toca llenar la(s) columna(s) <b>{fPendiente.columnas}</b>.
                <span className="text-amber-700"> ({items.length} fila{items.length !== 1 ? 's' : ''})</span>
              </p>
              <button onClick={() => setFPendiente(null)} className="ml-auto shrink-0 px-2 py-1 bg-white border border-amber-300 rounded hover:bg-amber-100">Quitar filtro</button>
            </div>
          )}
          {bloqueoResp > 0 && (
            <div className="mb-3 bg-red-50 border border-red-300 rounded-lg p-3 flex items-start gap-3 text-xs">
              <AlertTriangle size={16} className="text-red-600 shrink-0 mt-0.5" />
              <p className="text-red-900">
                <b>Primero el supervisor.</b> Tienes {bloqueoResp} orden{bloqueoResp !== 1 ? 'es' : ''} sin
                responsable en la columna <b>G</b>: el CD10 no lo trajo, y sin él no se sabe la zona ni a qué
                profesional de apoyo preguntarle. Tu filtro de pendientes se queda aquí hasta que escribas el
                nombre de quien montó el trabajo; en cuanto lo hagas, sigue con lo demás.
              </p>
            </div>
          )}
          {/* Buscador + filtros (como Programación) */}
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <div className="relative">
              <SearchIcon size={14} className="absolute left-2 top-2.5 text-gray-400" />
              <input value={busq} onChange={(e) => setBusq(e.target.value)} placeholder="Buscar protocolo, PDL, responsable…"
                className="pl-7 pr-2 py-1.5 text-sm border rounded-lg w-72" />
              {busq && <button onClick={() => setBusq('')} className="absolute right-1.5 top-2 text-gray-400 hover:text-gray-700"><X size={14} /></button>}
            </div>
            <select value={fTipo} onChange={(e) => setFTipo(e.target.value)} className="px-2 py-1.5 text-sm border rounded-lg bg-white">
              <option value="">Tipo: todos</option>
              <option value="FT">FT · Programado</option>
              <option value="ST">ST · Línea Viva</option>
            </select>
            <select value={fZona} onChange={(e) => setFZona(e.target.value)} className="px-2 py-1.5 text-sm border rounded-lg bg-white">
              <option value="">Zona: todas</option>
              {['UBATE', 'ALMEIDAS', 'GUAVIO', 'LV'].map((z) => <option key={z} value={z}>{z}</option>)}
            </select>
            {(busq || fTipo || fZona) && (
              <button onClick={() => { setBusq(''); setFTipo(''); setFZona('') }} className="text-xs text-gray-500 hover:text-blue-700">Limpiar filtros</button>
            )}
            <button onClick={() => setVerHistorial(true)}
              className="ml-auto flex items-center gap-1.5 px-3 py-1.5 text-sm border rounded-lg text-gray-600 hover:bg-gray-50">
              <History size={15} /> Historial de cambios
            </button>
            <span className="text-xs text-gray-400">
              {itemsVista.length} de {data?.total ?? 0} reg.
              {filtroRolPend && <span className="text-blue-600"> · filtrado por rol{filtroElabPend
                ? ` · ${filtroElabPend === 'sin' ? 'sin dueño'
                    : (tareas?.elaboradores?.find((e) => e.email === filtroElabPend)?.nombre || filtroElabPend)}`
                : ''}</span>}
            </span>
          </div>
          <p className="text-[11px] text-gray-500 mb-2">
            Como en Excel: muévete con las <b>flechas</b>, <b>Enter</b> baja, <b>doble-clic</b> / <b>F2</b> (o empieza a escribir) edita; el <b>embudo</b> de cada columna filtra por valores con casillas y buscador (grises = datos STM, amarillas = manuales; las <b>calculadas</b> son de solo lectura). En los nodos (3.2 C): <b>amarillo fuerte</b> = la celda la ajustó solo el sistema (anulada con exclusión → planeados 0; cerrada → ejecutados igualados) y <b>rojo</b> = la condición de ese ajuste ya no se cumple, revísala — clic en la celda para ver la regla. Deshacer/rehacer con <b>Ctrl+Z</b> / <b>Ctrl+Y</b>; todo cambio queda en el historial y es reversible. Con <b>Modo Excel</b> la tabla ocupa toda la pantalla.
          </p>
          <LeyendaParpadeo rol={usuario?.rol} veTodo={usuario?.ans_ve_todo} filtroRol={filtroRolPend} setFiltroRol={cambiarFiltroRol} items={items} filaMia={filaMia}
            elaboradores={tareas?.elaboradores} filtroElab={filtroElabPend} setFiltroElab={setFiltroElabPend}
            ingZona={esIngZonaPend} zonaPend={zonaPend} />
          {esDev && <FiltrosFlujo flujo={flujo} activo={filtroFlujo} setActivo={setFiltroFlujo} />}
          {isLoading ? <p className="p-6 text-gray-400 text-sm">Cargando…</p>
          : itemsVista.length === 0 ? (
            <p className="p-6 text-gray-400 text-sm border rounded-lg bg-white">
              {filtroElabPend ? 'Esa cuenta no tiene filas pendientes en este periodo.'
                : filtroRolPend ? 'Ese rol no tiene filas pendientes en este periodo.'
                : 'Sin registros. Sube los CD10 del periodo o ajusta los filtros.'}
            </p>
          ) : <AnsDataGrid items={itemsVista} onSave={saveCell} apiBase={apiBase} filtroRol={filtroRolPend}
                ingZona={esIngZonaPend} zonaPend={zonaPend}
                onIrAuditor={(puedeAuditoria || puedeCorreos) ? irAuditor : null}
                periodo={periodo} titulo={`${prueba ? 'ANS de prueba' : 'Cálculo ANS'} · ${fmtPeriodo(periodo)}`} />}
        </>
      )}

      {tab === 'indicadores' && (
        <div className="space-y-5">
          {indLoading && <p className="text-gray-400 text-sm">Calculando…</p>}
          {ind && (
            <>
              {/* Correo de estado (solo dev): genera y descarga el archivo para el Cazador */}
              {esDev && (
                <div className="flex justify-end gap-2 -mb-3">
                  <a href="https://dominionglobal.sharepoint.com/colaborativos/op_ECU-COL/COL/HSEQ/Forms/AllItems.aspx?id=%2Fcolaborativos%2Fop%5FECU%2DCOL%2FCOL%2FHSEQ%2FSGI%20PROYECTOS%2FSOT%20SOC%20ZVII%20%2D%20JA10185664%2F02%2E%20CALIDAD%2F04%2E%20ANS%2F02%2E%20Programados%2F07%2E%20Septiembre&viewid=a5fd0ec5%2Dc907%2D453d%2D8b92%2Dfc27805312d2&sharingv2=true&fromShare=true&at=9&CID=f9f075d1%2Da8f3%2D4541%2Dbb8b%2D8ac8ab63b6d1&FolderCTID=0x0120007E668B9EDC66C44DA72DB1F0A203EBF9"
                    target="_blank" rel="noopener noreferrer"
                    title="Abre la carpeta '07. Septiembre' en SharePoint"
                    className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-blue-700 text-white hover:bg-blue-800">
                    <FolderOpen size={13} /> Descargar STM
                  </a>
                  <a href="https://dominionglobal-my.sharepoint.com/:x:/r/personal/andres_sabogal_dominion-global_com/_layouts/15/Doc.aspx?sourcedoc=%7BE22FB175-7A79-441C-B171-1423EDFBD854%7D&file=BOLSA.xlsx&fromShare=true&action=default&mobileredirect=true"
                    target="_blank" rel="noopener noreferrer"
                    title="Abre el BOLSA.xlsx compartido en SharePoint para descargar las bolsas del día"
                    className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-emerald-700 text-white hover:bg-emerald-800">
                    <FileSpreadsheet size={13} /> Descargar bolsas
                  </a>
                  {/* Candado: sin bolsas actualizadas HOY el correo no se genera (el backend
                      también lo rechaza) — obliga a refrescar las bolsas antes de enviar */}
                  <button onClick={descargarCorreo}
                    title={ind?.bolsas_carga?.al_dia
                      ? 'Genera el correo de estado (3.1 y 3.2) y lo descarga; el Cazador lo abre como borrador en Outlook'
                      : 'Bloqueado: las bolsas no se han actualizado HOY. Súbelas en el módulo Bolsas y este botón se habilita solo.'}
                    className={`flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg ${ind?.bolsas_carga?.al_dia
                      ? 'bg-slate-700 text-white hover:bg-slate-800'
                      : 'bg-gray-200 text-gray-500 border border-gray-300'}`}>
                    <Mail size={13} /> {ind?.bolsas_carga?.al_dia ? 'Enviar correo' : 'Enviar correo — bolsas sin actualizar hoy'}
                  </button>
                </div>
              )}
              {/* Alimentación de los indicadores, de primeras */}
              <AlimentacionCard alim={ind.alimentacion} />
              {/* Tarjetas ANS */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {[['3.2 Ejec./Efect./Alcance', ind['3.2']?.ANS], ['3.3 Calidad info', ind['3.3']?.ANS],
                  ['3.4 Oport. programación', ind['3.4']?.ANS]].map(([label, v]) => (
                  <div key={label} className="bg-white border rounded-lg p-3">
                    <p className="text-xs text-gray-500">{label}</p>
                    <p className="text-2xl font-bold text-blue-700">{pct(v)}</p>
                  </div>
                ))}
                <div className="bg-blue-700 text-white rounded-lg p-3">
                  <p className="text-xs opacity-80">Registros / No ejecutado</p>
                  <p className="text-2xl font-bold">{ind.total_registros} / {ind.NO_EJECUTADO}</p>
                </div>
              </div>

              {/* Filtro de zona estilo Excel — aplica a las tablas 3.2–3.4 y a la pivote */}
              <div className="flex flex-wrap items-center gap-2 bg-white border rounded-lg px-3 py-2 text-xs">
                <span className="font-semibold text-gray-700">Filtro (como en el Excel):</span>
                <label className="text-gray-500">Zona</label>
                <select value={fZonaInd} onChange={(e) => setFZonaInd(e.target.value)}
                  className="px-2 py-1 border rounded bg-white">
                  <option value="">(Todas)</option>
                  {['UBATE', 'ALMEIDAS', 'GUAVIO', 'LV', ...(ind?.SIN_ZONA ? ['SIN ZONA'] : [])].map((z) => <option key={z} value={z}>{z}</option>)}
                </select>
                <span className="text-gray-400">Filtra las tablas y dinámicas del 3.2, 3.3 y 3.4.</span>
                {fZonaInd && <button onClick={() => setFZonaInd('')} className="ml-auto text-blue-700 hover:underline">Quitar filtro</button>}
              </div>

              {/* ── 3.1: todo en un solo cuadro ── */}
              <Card tono="azul" title="3.1 — Cumplimiento en el envío del presupuesto (bolsas)">
                <UltimaCargaBolsas carga={ind.bolsas_carga} onDescargar={descargarFuente} />
                <p className="text-[11px] text-gray-500 mb-2 max-w-3xl leading-relaxed">
                  Las bolsas como están hoy, según la última carga del módulo Bolsas. PENDIENTE son los casos
                  sin fecha de envío del presupuesto: ese número no cambia al cambiar de mes. ENTREGADO A ENEL
                  (enviado dentro del mes) y PLANEADO (con fecha posterior) sí dependen del mes.
                </p>
                <MatrizBolsas tabla={ind['3.1']} filas={Object.keys(ind['3.1'] || {})}
                  zonas={['UBATE', 'ALMEIDAS', 'GUAVIO']} subcols={['PLANEADO', 'PENDIENTE BOLSA', 'ENTREGADO A ENEL']}
                  grupo="3.1" onCell={setDrill} />
                {/* Resultado del indicador (tabla SENDA 35) + META, dentro del mismo cuadro */}
                <Bloque31Resultado data={indPlus?.['3.1']} onCell={setDrill} />
              </Card>

              {/* ── 3.2 — A·Oportunidad / B·Efectividad / C·Alcance, con sus tablas por zona ── */}
              <Card tono="verde" title="3.2 — A·Oportunidad / B·Efectividad / C·Alcance">
                {/* A, B, C y el ANS como cuadros GRANDES + la tablita META (descuentos)
                    al frente, como el bloque de resultado del 3.1 */}
                <div className="flex flex-wrap gap-8 items-start mb-4">
                  <div className="flex flex-wrap gap-3 items-stretch">
                    {[['A', 'Oportunidad', 'indA', '3.2A'], ['B', 'Efectividad', 'indB', '3.2B'], ['C', 'Alcance', 'indC', '3.2C']].map(([l, nombre, k, grupo]) => (
                      <button key={l} onClick={() => setDrill({ grupo })} title="Ver las órdenes detrás"
                        className="bg-white border rounded-lg px-4 py-3 text-left hover:border-blue-400 hover:shadow-sm">
                        <p className="text-[11px] font-semibold text-gray-500">{l} — {nombre}</p>
                        <p className="text-2xl font-bold text-blue-700">{pct(ind['3.2']?.[k])}</p>
                      </button>
                    ))}
                    <div className="bg-green-50 border border-green-300 rounded-lg px-5 py-3">
                      <p className="text-[11px] font-semibold text-green-800">Cumplimiento ANS 3.2</p>
                      <p className="text-3xl font-bold text-green-700">{pct(ind['3.2']?.ANS)}</p>
                      <p className="text-[11px] text-gray-500">= 0.25·A + 0.25·B + 0.50·C</p>
                    </div>
                  </div>
                  <NaturalezaSLA titulo="META" filas={ind['3.2']?.descuento} valor={ind['3.2']?.ANS} />
                </div>
                {/* Caso por caso: tabla por zona + AL FRENTE su dinámica de equipos que NO
                    cumplen (pantallazo listo para el correo al equipo incumplido) */}
                <div className="space-y-6">
                  <div className="flex flex-wrap gap-8 items-start">
                    <TablaZonas titulo="A — Oportunidad en la ejecución" valor={ind['3.2']?.indA}
                      tabla={ind['3.2']?.A} grupo="3.2A" zonaFiltro={fZonaInd} onCell={setDrill} />
                    <DinamicaNoCumple caso="A" criterio="apertura o cierre fuera del tiempo permitido (más de 10 min de desfase)"
                      items={ind.NO_CUMPLEN?.A} periodo={periodo} zonaFiltro={fZonaInd} onCell={setDrill} />
                  </div>
                  <div className="flex flex-wrap gap-8 items-start pt-4 border-t">
                    <TablaZonas titulo="B — Efectividad en la apertura" valor={ind['3.2']?.indB}
                      tabla={ind['3.2']?.B} grupo="3.2B" zonaFiltro={fZonaInd} onCell={setDrill} />
                    <DinamicaNoCumple caso="B" criterio="no se ejecutó (columna «Se ejecutó» en NO — anuladas por el Centro Operativo)"
                      items={ind.NO_CUMPLEN?.B} periodo={periodo} zonaFiltro={fZonaInd} onCell={setDrill} />
                  </div>
                  <div className="flex flex-wrap gap-8 items-start pt-4 border-t">
                    <TablaZonas titulo="C — Alcance (nodos)" valor={ind['3.2']?.indC} modo="nodos"
                      tabla={ind['3.2']?.C} grupo="3.2C" zonaFiltro={fZonaInd} onCell={setDrill} />
                    <DinamicaNoCumple caso="C" ancha criterio="nodos planeados que quedaron sin ejecutar"
                      items={ind.NO_CUMPLEN?.C} periodo={periodo} zonaFiltro={fZonaInd} onCell={setDrill} />
                  </div>
                </div>
                <p className="text-[11px] text-gray-400 mt-2">Clic en cualquier número para ver las órdenes detrás.</p>
              </Card>

              {/* ── 3.3 — Calidad de la información (UUCC) ── */}
              <Card tono="ambar" title="3.3 — Calidad de la información (UUCC)">
                <div className="flex flex-wrap gap-6 items-start">
                  <TablaZonas titulo="Cumplimiento por zona" valor={ind['3.3']?.ANS} totalSinExclusiones
                    tabla={ind['3.3']?.por_zona} grupo="3.3" zonaFiltro={fZonaInd} onCell={setDrill} />
                  <NoCumplenPorPersona titulo="No cumplen — por supervisor" etiqueta="Supervisor" data={ind['3.3']?.por_programador} />
                </div>
                <p className="text-[11px] text-gray-400 mt-2">Una orden excluida cumple el indicador: ya viene contada en «Cumplen» y en el total. Clic en un número para ver las órdenes.</p>
              </Card>

              {/* ── 3.4 — Oportunidad en la programación ── */}
              <Card tono="violeta" title="3.4 — Oportunidad en la programación">
                <div className="flex flex-wrap gap-6 items-start">
                  <TablaZonas titulo="Cumplimiento por zona" valor={ind['3.4']?.ANS}
                    tabla={ind['3.4']?.por_zona} grupo="3.4" zonaFiltro={fZonaInd} onCell={setDrill} />
                  <NoCumplenPorPersona titulo="No cumplen — por programador(a)" data={ind['3.4']?.por_programador} />
                </div>
                <p className="text-[11px] text-gray-400 mt-2">Las órdenes excluidas quedan en N/A y no descuentan el cumplimiento. Clic en un número para ver las órdenes.</p>
              </Card>

              {/* 3.5 — el único que queda: PLANEADO de la programación de los 20' + INVR (cambio_final) */}
              <Bloque35Prog20 data={indPlus?.['3.5']} meses={indPlus?.prog20_meses} periodo={periodo}
                onSubir={readOnly ? undefined : () => prog20Ref.current?.click()}
                onMapeo={readOnly ? undefined : () => setVerMapeo(true)} onCell={setDrill}
                onSub={setSubAsig} />

              {/* 5.2 — oportunidad de conexión (sale del subtab 3.Programados) */}
              {esDev && <Bloque52 data={ind52.data} cargando={ind52.isLoading} zonaFiltro={fZonaInd} />}

              {/* Datos por revisar: fechas con formato erróneo en las bolsas */}
              <AlertaFechas anom={anom} />
            </>
          )}
        </div>
      )}

      {drill && <DetalleModal apiBase={apiBase} K={K} periodo={periodo} drill={drill} onClose={() => setDrill(null)} />}
      {verHistorial && <HistorialModal apiBase={apiBase} K={K} periodo={periodo} readOnly={readOnly} onClose={() => setVerHistorial(false)} />}
      {verMapeo && <MapeoModal apiBase={apiBase} K={K} onClose={() => setVerMapeo(false)} />}
      {subAsig && <SubprocesoModal apiBase={apiBase} K={K} periodo={periodo} subproceso={subAsig}
        readOnly={readOnly} onClose={() => setSubAsig(null)}
        onMapeo={() => { setSubAsig(null); setVerMapeo(true) }} />}

      {tab === 'fuentes' && (
        <>
        {/* Cronograma 20' subidos (programación de los 20') — para verificar el cruce */}
        <div className="mb-3 bg-white border border-rose-200 rounded-lg overflow-hidden">
          <div className="px-4 py-2 bg-rose-50 border-b border-rose-200 text-xs text-rose-800 flex items-center justify-between">
            <span><b>Cronograma 20’ subidos</b> — la «Programación Unificada» (hoja P. UNIFICADA) que alimenta el PLANEADO de 3.5. Descárgalos para verificar el cruce.</span>
            {!readOnly && <button onClick={() => prog20Ref.current?.click()} className="flex items-center gap-1 px-2 py-0.5 bg-rose-600 text-white rounded text-[11px] hover:bg-rose-700"><Upload size={12} /> Subir</button>}
          </div>
          {(!prog20Files || prog20Files.length === 0) ? (
            <p className="p-4 text-gray-400 text-sm">Aún no se ha subido ninguna programación de los 20’.</p>
          ) : (
            <table className="text-xs w-full">
              <thead className="bg-gray-50 text-left text-gray-600">
                <tr>{['', 'Archivo', 'Subido', 'Filas', 'Tamaño', 'Hash', ''].map((h, i) => <th key={i} className="px-3 py-2 font-semibold">{h}</th>)}</tr>
              </thead>
              <tbody>
                {prog20Files.map((f) => (
                  <tr key={f.id} className={`border-t hover:bg-gray-50 ${f.actual ? 'bg-rose-50/40' : ''}`}>
                    <td className="px-3 py-1.5">{f.actual ? <span className="px-1.5 rounded bg-green-100 text-green-700 text-[10px]">EN USO</span> : <span className="text-gray-300 text-[10px]">histórico</span>}</td>
                    <td className="px-3 py-1.5 font-mono">{f.nombre}</td>
                    <td className="px-3 py-1.5 text-gray-500">{f.subido}</td>
                    <td className="px-3 py-1.5">{f.filas}</td>
                    <td className="px-3 py-1.5 text-gray-500">{(f.tamano / 1024 / 1024).toFixed(1)} MB</td>
                    <td className="px-3 py-1.5 font-mono text-gray-400">{f.hash}…</td>
                    <td className="px-3 py-1.5 text-right">
                      <button onClick={() => descargarFuente(f)} className="inline-flex items-center gap-1 text-rose-600 hover:text-rose-800"><Download size={14} /> Descargar</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        {dups?.total > 0 && (
          <div className="mb-3 bg-amber-50 border border-amber-300 rounded-lg p-3 text-xs text-amber-900">
            <b>⚠ {dups.total} código(s) aparecen en más de un periodo</b> (riesgo de doble conteo). Revisa y elimina del periodo que no corresponda:
            <div className="mt-1 flex flex-wrap gap-2">
              {dups.items.slice(0, 12).map((d) => (
                <span key={d.protocolo} className="bg-white border border-amber-200 rounded px-1.5 py-0.5 font-mono">
                  {d.protocolo.split('/')[0]} <span className="text-amber-600">({d.periodos.map((p) => p.periodo).join(', ')})</span>
                </span>
              ))}
            </div>
          </div>
        )}
        <div className="bg-white border rounded-lg overflow-hidden">
          <div className="px-4 py-2 bg-gray-50 border-b text-xs text-gray-500 flex items-center gap-3">
            <span className="flex-1">
              Archivos STM y reportes que originaron los datos de <b>{periodo}</b> — respaldo de procedencia, descargables, <b>lo último subido primero</b>. Al subir, el sistema avisa de archivos repetidos, tipo/periodo equivocado y códigos duplicados entre meses.
            </span>
            <button onClick={exportarFuentes} disabled={expFuentes || !fuentes?.length}
              title="Descarga TODOS los archivos fuente del mes en un ZIP, encarpetados por tipo y con su fecha de subida"
              className="shrink-0 flex items-center gap-1.5 px-2.5 py-1 bg-green-600 text-white rounded-lg text-[11px] font-medium hover:bg-green-700 disabled:opacity-40">
              <FolderArchive size={13} /> {expFuentes ? 'Empaquetando…' : 'Exportar todo (ZIP)'}
            </button>
          </div>
          {(!fuentes || fuentes.length === 0) ? (
            <p className="p-6 text-gray-400 text-sm">Sin archivos fuente para este periodo.</p>
          ) : (
            <table className="text-xs w-full">
              <thead className="bg-gray-50 text-left text-gray-600">
                <tr>
                  <th className="px-3 py-2 font-semibold">Tipo</th>
                  <th className="px-3 py-2 font-semibold">Archivo</th>
                  <th className="px-3 py-2 font-semibold">Subido</th>
                  <th className="px-3 py-2 font-semibold">Filas STM</th>
                  <th className="px-3 py-2 font-semibold">Tamaño</th>
                  <th className="px-3 py-2 font-semibold"></th>
                </tr>
              </thead>
              <tbody>
                {fuentes.map((f) => (
                  <tr key={f.id} className="border-t hover:bg-gray-50">
                    <td className="px-3 py-1.5">
                      <span className={`px-1.5 rounded ${f.tipo === 'st' ? 'bg-indigo-100 text-indigo-700' : f.tipo === 'programados' ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-600'}`}>{f.tipo}</span>
                    </td>
                    <td className="px-3 py-1.5 font-mono">{f.nombre}</td>
                    <td className="px-3 py-1.5 text-gray-500 whitespace-nowrap">{fmtFechaHora(f.creado_en)}</td>
                    <td className="px-3 py-1.5">{f.registros || '—'}</td>
                    <td className="px-3 py-1.5 text-gray-500">{(f.tamano / 1024).toFixed(0)} KB</td>
                    <td className="px-3 py-1.5 text-right">
                      <button onClick={() => descargarFuente(f)} className="inline-flex items-center gap-1 text-blue-600 hover:text-blue-800">
                        <Download size={14} /> Descargar
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        </>
      )}

      {tab === 'nodos' && puedeNodos && (
        <NodosTab apiBase={apiBase} periodo={periodo} K={K} readOnly={readOnly} />
      )}

      {tab === 'evidencias' && (
        <EvidenciasTab apiBase={apiBase} apiRoot={apiBase} periodo={periodo} />
      )}

      {tab === 'lideres' && (
        <LideresTab apiBase={apiBase} periodo={periodo} readOnly={readOnly || soloCeldas} />
      )}

      {/* Apartado nuevo e independiente: no comparte estado con el cálculo ANS de arriba */}
      {tab === 'programados' && esDev && (
        <ProgramadosTab apiBase={apiBase} periodo={periodo} readOnly={readOnly || soloCeldas} />
      )}

      {/* Auditoría del analista automático de WhatsApp: dev + directivo autorizado */}
      {tab === 'auditoria-ia' && puedeAuditoria && (
        <TabBoundary><AuditoriaIaTab apiBase={apiBase} periodo={periodo} onVolverTabla={() => cambiarTab('registros')} /></TabBoundary>
      )}

      {/* Correos etiquetados ANS en el buzón vigilado: dev + directivo autorizado */}
      {tab === 'correos-ans' && puedeCorreos && (
        <TabBoundary><CorreosAnsTab apiBase={apiBase} periodo={periodo} onVolverTabla={() => cambiarTab('registros')} /></TabBoundary>
      )}
    </div>
  )
}

const ROLES_PEND = [
  // Programación + cumplimentación UNIFICADAS (ago-2026): las 4 cuentas elaboradoras llenan
  // el 3.4 completo (fechas + su exclusión y razón), G·H y la AQ. La sigla de la col H dice
  // de quién es cada fila: a cada cuenta le cuentan/parpadean SUS filas + las sin sigla.
  // El ANS lleva el resto.
  { rol: 'elaborador', label: 'Elaboradores', cols: '3.4 (W·Y·AA·AB) · G · H · AQ', chip: 'bg-amber-200 border-amber-500', on: 'bg-amber-50 border-amber-400' },
  { rol: 'ingeniero_zona', label: 'Nodos / Ingeniería', cols: 'AV · AW', chip: 'bg-violet-200 border-violet-500', on: 'bg-violet-50 border-violet-400' },
  { rol: 'ans', label: 'Encargado ANS', cols: '3.2 A/B y 3.3 + razón', chip: 'bg-rose-200 border-rose-500', on: 'bg-rose-50 border-rose-400' },
]

// Chip del INGENIERO DE ZONA (cuenta de zona o profesional de apoyo — la misma persona según
// el directorio): idéntico al del encargado ANS (mismas celdas, mismo color), pero cuenta y
// filtra SOLO los casos de su zona. Solo lo ve él — a quien supervisa le basta el de arriba.
// La tarea 3.2 C (chip «Nodos / Ingeniería») es exclusiva de quien lleva los nodos (config.rol_pend_ans).
const ING_ZONA_PEND = { rol: 'ing_zona', label: 'Ingeniero de zona', cols: '3.2 A/B y 3.3 + razón · tu zona', chip: 'bg-rose-200 border-rose-500', on: 'bg-rose-50 border-rose-400' }

// ── FILTROS DE FLUJO (solo dev) ─────────────────────────────────────────
// Siguen el ESTADO de un caso dentro del circuito de trabajo, no el valor de una celda.
// V1 «No excluibles»: NO en 3.2 A/B o 3.3 → se consigue el correo → se sube → la IA lo
// analiza → la persona responsable aprueba/niega → si el NO queda definitivo, correo a ing. de zona (próx.).
const ETAPAS_FLUJO = [
  { id: 'sin_soporte', label: 'Sin correo/soporte', hint: 'Pedir el correo de anulación', chip: 'bg-rose-200 border-rose-500', on: 'bg-rose-50 border-rose-400' },
  { id: 'soporte_sin_analizar', label: 'Soporte subido · sin analizar', hint: 'Abre la celda y dale «Analizar»', chip: 'bg-amber-200 border-amber-500', on: 'bg-amber-50 border-amber-400' },
  { id: 'propuesta_si', label: 'IA propone SI', hint: 'La IA sugiere revertir el NO — falta tu aprobación', chip: 'bg-violet-200 border-violet-500', on: 'bg-violet-50 border-violet-400' },
  { id: 'no_definitivo', label: 'NO definitivo', hint: 'La IA confirmó el NO → candidato al correo a ing. de zona (próximamente)', chip: 'bg-gray-300 border-gray-500', on: 'bg-gray-100 border-gray-400' },
]

function FiltrosFlujo({ flujo, activo, setActivo }) {
  const conteos = flujo?.conteos || {}
  const total = flujo?.total ?? 0
  return (
    <div className="flex flex-wrap items-center gap-2 mb-2 text-xs bg-white border rounded-lg px-3 py-2">
      <Zap size={14} className="text-blue-600 shrink-0" />
      <span className="font-semibold text-gray-700">
        Filtros de flujo <span className="text-[10px] bg-blue-100 text-blue-700 px-1 rounded">solo tú · beta</span>
        {' '}· No excluibles ({total}):
      </span>
      {ETAPAS_FLUJO.map((e) => {
        const n = conteos[e.id] || 0
        const on = activo === e.id
        return (
          <button key={e.id} type="button" onClick={() => setActivo(on ? null : e.id)} title={e.hint}
            disabled={!n && !on}
            className={`flex items-center gap-1 px-2 py-1 rounded-full border transition-colors ${
              on ? e.chip : n ? `${e.on} hover:opacity-75` : 'bg-gray-50 border-gray-200 text-gray-400 cursor-default'}`}>
            <b>{n}</b> {e.label}
          </button>
        )
      })}
      {activo
        ? <button onClick={() => setActivo(null)} className="ml-auto shrink-0 text-blue-700 hover:underline">Quitar filtro</button>
        : <span className="ml-auto shrink-0 text-gray-400">Clic en una etapa = solo esos casos</span>}
    </div>
  )
}

// Cuenta las filas que tienen alguna celda pendiente de ese rol. Para «elaborador»,
// `filaMia` acota a las filas de la cuenta que mira (sigla col H) + las sin dueño.
// Para «ing_zona» las celdas son las del rol 'ans' acotadas a la zona de quien mira.
const filasDe = (items, rol, filaMia, zonaPend) => items.filter((it) =>
  rol === 'ing_zona'
    ? Object.values(it._pend || {}).includes('ans') && !!zonaPend && (it.zona || '').toUpperCase() === zonaPend
    : Object.values(it._pend || {}).includes(rol) && (rol !== 'elaborador' || filaMia(it))).length

// Filas pendientes de elaborador atribuidas a UNA cuenta ('sin' = las que no tienen sigla).
const filasDeElab = (items, quien) => items.filter((it) =>
  Object.values(it._pend || {}).includes('elaborador')
  && (quien === 'sin' ? !it._elab : it._elab === quien)).length

function LeyendaParpadeo({ rol, veTodo, filtroRol, setFiltroRol, items, filaMia,
                           elaboradores, filtroElab, setFiltroElab, ingZona, zonaPend }) {
  // Programador y cumplimentador son UN solo rol de diligenciamiento («elaborador», ago-2026).
  // El ingeniero de zona (sin nodos) tiene su chip propio: exclusiones de SU zona.
  const rolPend = (rol === 'programador' || rol === 'cumplimentador') ? 'elaborador'
    : ingZona ? 'ing_zona' : rol
  // Quien supervisa (dev/ans/directivo + cuentas autorizadas) ve los 3 colores; el resto, solo el suyo.
  const visibles = veTodo ? ROLES_PEND
    : ingZona ? [ING_ZONA_PEND] : ROLES_PEND.filter((r) => r.rol === rolPend)
  if (visibles.length === 0) return null
  return (
    <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-gray-600 bg-white border rounded-lg px-3 py-1.5">
      <span className="font-semibold text-gray-700">Celdas que parpadean = pendientes de llenar:</span>
      {visibles.map((r) => {
        const n = filasDe(items, r.rol, filaMia, zonaPend)
        const activo = filtroRol === r.rol
        const apagado = filtroRol && !activo
        return (
          <Fragment key={r.rol}>
            <button type="button"
              onClick={() => setFiltroRol(activo ? null : r.rol)}
              title={n ? `Ver solo las ${n} filas pendientes de ${r.label}` : 'Sin filas pendientes'}
              className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded border transition
                ${activo ? `${r.on} font-semibold` : 'border-transparent hover:bg-gray-50'}
                ${apagado ? 'opacity-40' : ''}`}>
              <span className={`inline-block w-3 h-3 rounded-sm border-2 ${r.chip} ${!filtroRol || activo ? 'animate-pulse' : ''}`} />
              {r.label} <span className="text-gray-400">({r.cols})</span>
              {n > 0 && <span className="font-mono font-semibold text-gray-700">{n}</span>}
              {r.rol === rolPend && <span className="text-[10px] bg-blue-100 text-blue-700 px-1 rounded">te toca</span>}
            </button>
            {/* Subfiltro por persona (solo supervisión): al encender «Elaboradores» se
                despliegan las 4 cuentas + «Sin dueño» (filas sin sigla en la H). */}
            {r.rol === 'elaborador' && activo && veTodo && (
              <span className="inline-flex flex-wrap items-center gap-1 pl-1.5 ml-0.5 border-l-2 border-amber-300">
                {[...(elaboradores || []).map((e) => ({ id: e.email, label: e.nombre })),
                  { id: 'sin', label: 'Sin dueño' }].map((p) => {
                  const np = filasDeElab(items, p.id)
                  const on = filtroElab === p.id
                  return (
                    <button key={p.id} type="button"
                      onClick={() => setFiltroElab(on ? null : p.id)}
                      disabled={!np && !on}
                      title={p.id === 'sin'
                        ? `${np} filas pendientes sin sigla en la H (sin dueño: las ven los cuatro)`
                        : np ? `Ver solo las ${np} filas pendientes de ${p.label} (su sigla en la H)` : 'Sin filas pendientes'}
                      className={`px-1.5 py-0.5 rounded-full border transition ${
                        on ? 'bg-amber-200 border-amber-500 font-semibold'
                        : np ? 'bg-amber-50 border-amber-300 hover:bg-amber-100'
                        : 'bg-gray-50 border-gray-200 text-gray-400 cursor-default'}`}>
                      {p.label}{np > 0 && <b className="ml-1 font-mono">{np}</b>}
                    </button>
                  )
                })}
              </span>
            )}
          </Fragment>
        )
      })}
      {filtroRol
        ? <button type="button" onClick={() => setFiltroRol(null)}
            className="ml-auto px-2 py-0.5 border rounded hover:bg-gray-50">Ver todas</button>
        : <span className="ml-auto text-gray-400">Clic en un rol para ver solo sus filas</span>}
    </div>
  )
}

function PanelTareas({ tareas, onCerrar, onVerFilas }) {
  const lista = tareas?.tareas || []
  const nada = lista.every((t) => t.pendientes === 0)
  return (
    <>
      <div className="fixed inset-0 z-30" onClick={onCerrar} />
      <div className="absolute right-0 top-full mt-1 w-[26rem] bg-white border rounded-lg shadow-xl z-40 overflow-hidden">
        <div className="px-3 py-2 border-b bg-gray-50 flex items-center justify-between">
          <p className="text-xs font-semibold text-gray-700">
            Tareas del ANS · {tareas?.periodo} <span className="text-gray-400 font-normal">({tareas?.registros ?? 0} registros)</span>
          </p>
          <button onClick={onCerrar} className="text-gray-400 hover:text-gray-700"><X size={15} /></button>
        </div>
        {!tareas ? (
          <p className="p-4 text-xs text-gray-400">Cargando…</p>
        ) : lista.length === 0 ? (
          <p className="p-4 text-xs text-gray-400">Tu rol no tiene columnas asignadas en el ANS.</p>
        ) : nada ? (
          <div className="p-4 flex items-center gap-2 text-xs text-green-700">
            <CheckCircle2 size={16} /> Todo al día — no hay filas pendientes.
          </div>
        ) : (
          <div className="max-h-[26rem] overflow-auto divide-y">
            {lista.map((t) => (
              <div key={t.id} className={`p-3 ${t.pendientes ? '' : 'opacity-50'}`}>
                <div className="flex items-start gap-2">
                  <span className={`mt-0.5 shrink-0 text-[10px] px-1.5 py-0.5 rounded ${t.pendientes ? 'bg-red-100 text-red-700' : 'bg-green-100 text-green-700'}`}>
                    {t.pendientes || '✓'}
                  </span>
                  <div className="min-w-0">
                    <p className="text-xs font-semibold text-gray-800">
                      {t.titulo}
                      {t.mio && <span className="ml-1 text-[10px] bg-blue-100 text-blue-700 px-1 rounded">te toca</span>}
                    </p>
                    <p className="text-[11px] text-gray-500">{t.rol_label} · columnas <b>{t.columnas}</b></p>
                    <p className="text-[11px] text-gray-600 mt-1 leading-relaxed">{t.ayuda}</p>
                    {t.pendientes > 0 && (
                      <button onClick={() => onVerFilas(t)}
                        className="mt-1.5 text-[11px] text-blue-600 hover:underline">
                        Ver las {t.pendientes} fila{t.pendientes !== 1 ? 's' : ''} pendientes →
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
        {tareas?.ve_todo && (
          <p className="px-3 py-1.5 border-t bg-gray-50 text-[10px] text-gray-400">
            Ves todas las tareas porque tu rol es «{tareas.rol}». Cada rol solo vería la suya.
          </p>
        )}
      </div>
    </>
  )
}

function NaturalezaSLA({ titulo, filas, valor }) {
  return (
    <div className="text-xs">
      <p className="font-semibold text-gray-600 mb-1">{titulo} — resultado {pct(valor)}</p>
      <table className="border">
        <thead className="bg-slate-800 text-white"><tr><th className="px-3 py-1 text-left">Cumplimiento</th><th className="px-3 py-1 text-left">Descuento</th></tr></thead>
        <tbody>
          {(filas || []).map((r, i) => <tr key={i} className="border-t"><td className="px-3 py-1">{r[0]}</td><td className="px-3 py-1">{r[1]}</td></tr>)}
        </tbody>
      </table>
    </div>
  )
}

// Va DENTRO del cuadro único del 3.1 (todo el 3.1 en una sola unidad)
function Bloque31Resultado({ data, onCell }) {
  if (!data) return null
  const cc = onCell ? 'cursor-pointer hover:underline' : ''
  return (
    <div className="mt-4 pt-3 border-t">
      <p className="font-semibold text-xs text-gray-600 mb-2">Resultado del indicador (visitas entregadas) — medición contra la SENDA (35)</p>
      <div className="flex flex-wrap gap-8 items-start">
        <table className="text-xs">
          <thead className="text-gray-500 border-b">
            <tr>{['Concepto', 'Cantidad total', 'Cumple', 'No cumple', 'Cumplimiento'].map((h) => <th key={h} className="px-3 py-1 text-left whitespace-nowrap">{h}</th>)}</tr>
          </thead>
          <tbody>
            {(data.filas || []).map((f, i) => (
              <tr key={i} className="border-t">
                <td className="px-3 py-1 font-medium">{f.concepto}</td>
                <td className={`px-3 py-1 ${cc}`} onClick={() => onCell?.({ grupo: '3.1', concepto: f.concepto })}>{f.total}</td>
                <td className={`px-3 py-1 text-green-700 ${cc}`} onClick={() => onCell?.({ grupo: '3.1', concepto: f.concepto, bucket: 'ENTREGADO A ENEL' })}>{f.cumple}</td>
                <td className={`px-3 py-1 text-red-600 ${cc}`} onClick={() => onCell?.({ grupo: '3.1', concepto: f.concepto, bucket: 'PENDIENTE BOLSA' })}>{f.no_cumple}</td>
                <td className="px-3 py-1">{pct(f.cumplimiento)}</td>
              </tr>
            ))}
            <tr className="border-t-2 font-semibold bg-gray-50">
              <td className="px-3 py-1">Total (SENDA)</td>
              <td className="px-3 py-1">{data.total}</td>
              <td className="px-3 py-1 text-green-700">{data.cumple}</td>
              <td className="px-3 py-1 text-red-600">{data.no_cumple}</td>
              <td className="px-3 py-1 text-blue-700">{pct(data.cumplimiento)}</td>
            </tr>
          </tbody>
        </table>
        <NaturalezaSLA titulo="META" filas={data.descuento} valor={data.cumplimiento} />
      </div>
      <p className="text-[11px] text-gray-400 mt-2 max-w-3xl">Cumple es lo que ya quedó ENTREGADO A ENEL. El total de cada concepto suma pendiente, entregado y planeado; el de la fila final no es esa suma sino la senda fija de 35 del indicador, confirmada con la dirección. El cumplimiento es Cumple / 35. Clic en un número para ver los casos.</p>
    </div>
  )
}

// Línea visible con la última carga de bolsas + el archivo del cargue masivo
function UltimaCargaBolsas({ carga, onDescargar }) {
  if (!carga) return null
  return (
    <div className="mb-2 flex flex-wrap items-center gap-2 text-xs bg-blue-50 border border-blue-200 rounded px-2.5 py-1.5 text-blue-900">
      <History size={13} className="shrink-0" />
      <span>Última carga de bolsas: <b>{carga.datos ? fmtFechaHora(carga.datos) : '— (sin datos cargados)'}</b></span>
      {carga.archivo ? (
        <button onClick={() => onDescargar?.({ id: carga.archivo.id, nombre: carga.archivo.nombre })}
          title={`Subido el ${fmtFechaHora(carga.archivo.creado_en)}`}
          className="inline-flex items-center gap-1 text-blue-700 hover:underline">
          <Download size={12} /> {carga.archivo.nombre}
        </button>
      ) : (
        <span className="text-blue-700/70">· desde la próxima carga masiva también quedará guardado el archivo Excel</span>
      )}
    </div>
  )
}

const ZONAS_IND = ['UBATE', 'ALMEIDAS', 'GUAVIO', 'LV']

// Tabla por zona (como las hojas 3.2/3.3/3.4 del Excel): Cumplen / No cumplen /
// Exclusiones / Total × zonas + TOTAL. Cada número es clicable (drill-down).
function TablaZonas({ titulo, valor, tabla, grupo, zonaFiltro, onCell, modo = 'ordenes', totalSinExclusiones = false }) {
  // INDICADORES: SIN ZONA (#50, 19 oct 2026): la columna SIN ZONA aparece solo si esta tabla tiene órdenes sin
  // zona; así el TOTAL sigue siendo la suma de lo que se ve y Ubate no carga órdenes ajenas
  const conSinZona = Object.values(tabla?.['SIN ZONA'] || {}).some((v) => v > 0)
  const zonas = zonaFiltro ? [zonaFiltro] : (conSinZona ? [...ZONAS_IND, 'SIN ZONA'] : ZONAS_IND)
  const get = (z, k) => tabla?.[z]?.[k] ?? 0
  const filas = modo === 'nodos'
    ? [['Nodos ejecutados', (z) => get(z, 'cumple'), 'text-green-700', 'CUMPLE'],
       ['Nodos programados', (z) => get(z, 'cumple') + get(z, 'no_cumple'), 'text-gray-700', null],
       ['Exclusiones', (z) => get(z, 'exclusiones'), 'text-amber-700', 'EXCLUSION']]
    : [['Cumplen', (z) => get(z, 'cumple'), 'text-green-700', 'CUMPLE'],
       ['No cumplen', (z) => get(z, 'no_cumple'), 'text-red-600', 'NO CUMPLE'],
       ['Exclusiones', (z) => get(z, 'exclusiones'), 'text-amber-700', 'EXCLUSION'],
       ['Total órdenes', (z) => get(z, 'cumple') + get(z, 'no_cumple') + (totalSinExclusiones ? 0 : get(z, 'exclusiones')), 'font-semibold text-gray-800', null]]
  return (
    <div className="text-xs">
      <p className="font-semibold text-gray-600 mb-1">{titulo}{valor !== undefined && <> — <span className="text-blue-700">{pct(valor)}</span></>}</p>
      <table className="border">
        <thead>
          <tr className="bg-gray-100 text-gray-600">
            <th className="px-2 py-1 text-left border"></th>
            {zonas.map((z) => <th key={z} className="px-2 py-1 border text-center">{z}</th>)}
            {!zonaFiltro && <th className="px-2 py-1 border text-center bg-gray-200">TOTAL</th>}
          </tr>
        </thead>
        <tbody>
          {filas.map(([label, fn, cls, bucket], i) => {
            const esTotal = label.startsWith('Total') || label === 'Nodos programados'
            const celda = (z) => {
              const v = fn(z)
              const clicable = onCell && !esTotal && v > 0
              return (
                <td key={z || 'T'} onClick={() => clicable && onCell({ grupo, zona: z || undefined, bucket: bucket || undefined })}
                  className={`px-2 py-1 border text-center ${cls} ${clicable ? 'cursor-pointer hover:bg-blue-50 hover:underline' : v === 0 ? 'opacity-40' : ''}`}>
                  {v}
                </td>
              )
            }
            const total = zonas.reduce((a, z) => a + fn(z), 0)
            return (
              <tr key={i} className={`border-t ${esTotal ? 'bg-gray-50' : ''}`}>
                <td className={`px-2 py-1 border text-left whitespace-nowrap ${esTotal ? 'font-semibold' : 'font-medium'}`}>{label}</td>
                {zonas.map((z) => celda(z))}
                {!zonaFiltro && (
                  <td onClick={() => onCell && !esTotal && total > 0 && onCell({ grupo, bucket: bucket || undefined })}
                    className={`px-2 py-1 border text-center bg-gray-50 font-semibold ${cls} ${onCell && !esTotal && total > 0 ? 'cursor-pointer hover:bg-blue-50 hover:underline' : ''}`}>
                    {total}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// "2026-06-10" / "10/06/2026" → "10/06" (fecha corta para los chips de las dinámicas)
const fechaCorta = (f) => {
  const s = String(f || '').trim()
  let m = s.match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (m) return `${m[3]}/${m[2]}`
  m = s.match(/^(\d{1,2})\/(\d{1,2})/)
  if (m) return `${m[1].padStart(2, '0')}/${m[2].padStart(2, '0')}`
  return s.slice(0, 10)
}

// Dinámica de INCUMPLIMIENTO de un caso 3.2 (A/B/C), pensada para pantallazo de correo:
// equipos (supervisor + líder RI) agrupados por zona, con cada orden y su motivo a la
// vista. El filtro Excluible arranca en NO (los incumplimientos reales, sin excusa).
function DinamicaNoCumple({ caso, criterio, items, periodo, zonaFiltro, onCell, ancha = false }) {
  const [excl, setExcl] = useState('NO')
  const list = (items || []).filter((it) =>
    (!zonaFiltro || it.zona === zonaFiltro) && (!excl || it.excluible === excl))
  // zona → equipo (supervisor+líder = una unidad, así el total cuadra) → órdenes
  const arbol = {}
  list.forEach((it) => {
    const z = (arbol[it.zona] ||= { n: 0, equipos: {} })
    z.n += 1
    const key = `${it.supervisor}|${it.lider}`
    const e = (z.equipos[key] ||= { supervisor: it.supervisor, lider: it.lider, ordenes: [] })
    e.ordenes.push(it)
  })
  const drill = (extra) => onCell?.({ grupo: 'NCT', concepto: caso, excluible: excl || undefined, ...extra })
  return (
    // max-w-4xl (antes 2xl): la columna Observación necesita su propio aire
    <div className={`text-xs ${ancha ? 'flex-1 min-w-[30rem]' : 'max-w-4xl'}`}>
      <div className="flex flex-wrap items-center gap-2 mb-1">
        <p className="font-semibold text-gray-700">Dinámica {caso} — equipos que NO cumplen · {fmtPeriodo(periodo)}</p>
        <label className="text-gray-500 ml-1">Excluible</label>
        <select value={excl} onChange={(e) => setExcl(e.target.value)} className="px-1.5 py-0.5 border rounded bg-white">
          <option value="NO">NO (incumplimientos reales)</option>
          <option value="SI">SI (con exclusión)</option>
          <option value="">(Todas)</option>
        </select>
      </div>
      <p className="text-[11px] text-gray-500 mb-1.5">Se mide: {criterio}. {excl === 'NO' && 'Con el filtro en NO solo aparecen órdenes sin exclusión aprobada.'}</p>
      {list.length === 0 ? (
        <p className="text-gray-400 border rounded bg-white px-3 py-2">Ningún equipo está incumpliendo con este filtro.</p>
      ) : (
        <table className="border w-full">
          <thead>
            <tr className="bg-slate-700 text-white">
              <th className="px-2 py-1 text-left border border-slate-600">Equipo (supervisor / líder)</th>
              <th className="px-2 py-1 text-center border border-slate-600 whitespace-nowrap">Órdenes</th>
              <th className="px-2 py-1 text-left border border-slate-600">Detalle del incumplimiento</th>
              <th className="px-2 py-1 text-left border border-slate-600">Observación</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(arbol).map(([zona, z]) => (
              <DinamicaZonaRows key={zona} zona={zona} z={z} drill={drill} />
            ))}
            <tr className="border-t-2 bg-gray-100 font-semibold">
              <td className="px-2 py-1 border">Total general</td>
              <td onClick={() => list.length > 0 && drill({})}
                className="px-2 py-1 border text-center text-red-700 cursor-pointer hover:underline">{list.length}</td>
              <td colSpan={2} className="px-2 py-1 border text-gray-400 font-normal">{Object.values(arbol).reduce((a, z) => a + Object.keys(z.equipos).length, 0)} equipo(s) señalado(s)</td>
            </tr>
          </tbody>
        </table>
      )}
      <p className="text-[11px] text-gray-400 mt-1">Cada orden cuenta una sola vez, en su equipo (supervisor + líder). Clic en un conteo abre las órdenes completas.</p>
    </div>
  )
}

function DinamicaZonaRows({ zona, z, drill }) {
  return (
    <>
      {/* La zona es una BANDA de color, no una fila de conteo: así la columna «Órdenes»
          solo trae equipos y el Total general es la suma exacta de lo que se ve */}
      <tr className="border-t">
        {/* INDICADORES: SIN ZONA (#50, 19 oct 2026): las órdenes sin zona van en su propia banda, ámbar */}
        <td colSpan={4} onClick={() => drill({ zona })} title="Ver las órdenes de esta zona"
          className={`px-2 py-1 border font-semibold uppercase tracking-wide cursor-pointer ${zona === 'SIN ZONA'
            ? 'border-amber-200 bg-amber-100 text-amber-900 hover:bg-amber-200'
            : 'border-blue-200 bg-blue-100 text-blue-900 hover:bg-blue-200'}`}>
          {zona === 'SIN ZONA' ? 'Sin zona' : `Zona ${zona}`} <span className={`normal-case font-normal ${zona === 'SIN ZONA' ? 'text-amber-800' : 'text-blue-700'}`}>— {z.n} orden{z.n !== 1 ? 'es' : ''} en {Object.keys(z.equipos).length} equipo{Object.keys(z.equipos).length !== 1 ? 's' : ''}{zona === 'SIN ZONA' && ' · no se le cuenta a ninguna zona hasta asignarla (Paso 1 en Auditoría IA)'}</span>
        </td>
      </tr>
      {/* Una fila por ORDEN (así la observación del encargado de ANS queda alineada con su
          orden). El equipo (supervisor/líder) y el conteo se funden con rowSpan sobre sus órdenes. */}
      {Object.values(z.equipos).map((e, i) => (
        e.ordenes.map((o, j) => (
          <tr key={`${i}-${j}`} className="border-t align-top">
            {j === 0 && (
              <>
                <td rowSpan={e.ordenes.length} className="px-2 py-1 border pl-5">
                  <p className="font-medium text-gray-800">{e.supervisor}</p>
                  <p className="text-[11px] text-gray-500">Líder: {e.lider}</p>
                </td>
                <td rowSpan={e.ordenes.length} onClick={() => drill({ zona, supervisor: e.supervisor, lider: e.lider })}
                  className="px-2 py-1 border text-center text-red-700 font-medium cursor-pointer hover:underline">{e.ordenes.length}</td>
              </>
            )}
            <td className="px-2 py-1 border">
              <span title={`${o.protocolo} · ${o.fecha} · ${o.motivo}`}
                className="inline-block bg-red-50 border border-red-200 text-red-800 rounded px-1.5 py-0.5 whitespace-nowrap">
                <b className="font-mono">{o.pdl}</b>{o.fecha ? ` · ${fechaCorta(o.fecha)}` : ''} · {o.motivo_corto || o.motivo}
              </span>
            </td>
            <td className="px-2 py-1 border text-gray-700 min-w-[10rem]">
              {o.observacion
                ? <span className="whitespace-pre-wrap">{o.observacion}</span>
                : <span className="text-gray-300 italic">— sin observación —</span>}
            </td>
          </tr>
        ))
      ))}
    </>
  )
}

// Mini-tabla «quiénes no cumplen» (como las hojas DINAMICA 3.3 / 3.4 del Excel)
function NoCumplenPorPersona({ titulo, data, etiqueta = 'Programador(a)' }) {
  const rows = Object.entries(data || {}).filter(([, v]) => (v['NO CUMPLE'] || 0) > 0)
  if (rows.length === 0) return null
  return (
    <div className="text-xs">
      <p className="font-semibold text-gray-600 mb-1">{titulo}</p>
      <table className="border">
        <thead><tr className="bg-gray-100 text-gray-600">
          <th className="px-2 py-1 text-left border">{etiqueta}</th>
          <th className="px-2 py-1 text-center border">NO CUMPLE</th>
        </tr></thead>
        <tbody>
          {rows.map(([nombre, v]) => (
            <tr key={nombre} className="border-t">
              <td className="px-2 py-1 border">{nombre}</td>
              <td className="px-2 py-1 border text-center text-red-700 font-medium">{v['NO CUMPLE']}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── Justificación clicable de CADA porcentaje del 3.5 (revisión con la dirección, 31-jul-2026):
//    clic en un % → modal con la fórmula exacta aplicada, número por número. ──
function formulaFila(tipo, nombre, deben, enviaron, ejecutaron) {
  if (!deben) {
    return { titulo: `${tipo === 'crono' ? 'Cronograma' : tipo === 'efec' ? 'Efectividad' : 'Cumplimiento'} · ${nombre}`,
      pasos: ['Deben programar = 0 → por regla del Excel del ANS queda en 100%: no había nada pendiente por programar (así quedó fijado a mano en el Excel oficial, celdas E7=F7=100%).'],
      resultado: '100.0%' }
  }
  if (tipo === 'crono') {
    const raw = enviaron / deben
    return { titulo: `Cronograma · ${nombre}`,
      pasos: ['Cronograma = Enviaron / Deben programar (columna E del Excel del ANS).',
        `= ${enviaron} / ${deben} = ${pct(raw)}${raw > 1 ? ' → se topa al 100%' : ''}`,
        'Nota: hoy «Enviaron» se toma igual a «Deben» (la fuente real de Enviaron está pendiente de definirse).'],
      resultado: pct(Math.min(raw, 1)) }
  }
  if (tipo === 'efec') {
    const raw = ejecutaron / deben
    return { titulo: `Efectividad · ${nombre}`,
      pasos: ['Efectividad = Ejecutaron / Deben programar, con tope del 100% — fórmula del Excel del ANS: SI(Ejecutaron/Deben > 1; 100%; Ejecutaron/Deben). El denominador es DEBEN, no Enviaron.',
        `= min(${ejecutaron} / ${deben}, 100%) = ${pct(Math.min(raw, 1))}`,
        ...(raw > 1 ? [`Se ejecutaron ${ejecutaron} con ${deben} planeadas: el exceso no suma más del 100%.`] : [])],
      resultado: pct(Math.min(raw, 1)) }
  }
  const c = Math.min(enviaron / deben, 1)
  const e = Math.min(ejecutaron / deben, 1)
  return { titulo: `Cumplimiento · ${nombre}`,
    pasos: ['Cumplimiento = 0.4·Cronograma + 0.6·Efectividad (celda B19 del Excel del ANS).',
      `= 0.4·${pct(c)} + 0.6·${pct(e)} = ${pct(0.4 * c + 0.6 * e)}`],
    resultado: pct(0.4 * c + 0.6 * e) }
}

function formulaTotalEfec(nombre, items) {
  const sumD = items.reduce((a, g) => a + (g.deben || 0), 0)
  const sumC = items.reduce((a, g) => a + Math.min(g.ejecutaron || 0, g.deben || 0), 0)
  const v = sumD === 0 ? 1 : Math.min(sumC / sumD, 1)
  return {
    titulo: `Efectividad · ${nombre}`,
    pasos: ['El total se arma CONCEPTO POR CONCEPTO con tope: de cada uno cuenta máximo lo planeado — Cuenta = min(Ejecutaron, Deben). Así el exceso de un concepto NO compensa el faltante de otro (revisión con la dirección: sin este tope, la suma cruda del Excel daría 100% aunque haya conceptos al 33%).'],
    tabla: {
      cols: ['Concepto', 'Deben', 'Ejecutaron', 'Cuenta (con tope)'],
      filas: items.map((g) => ({
        destacar: (g.ejecutaron || 0) > (g.deben || 0),
        celdas: [g.concepto, g.deben || 0, g.ejecutaron || 0, Math.min(g.ejecutaron || 0, g.deben || 0)],
      })),
    },
    post: [`Efectividad = Σ Cuenta / Σ Deben = ${sumC} / ${sumD} = ${sumD ? pct(sumC / sumD) : '100.0% (Deben = 0)'}`],
    nota: 'Las filas resaltadas ejecutaron más de lo planeado (o sin planeación): cuentan solo hasta su tope. Cuando ningún concepto se pasa, este cálculo da idéntico a la fórmula cruda del Excel (F16 = SUMA Ejecutaron / SUMA Deben).',
    resultado: pct(v),
  }
}

const formulaCumplTotal = (nombre, crono, efec) => ({
  titulo: `Cumplimiento 3.5 · ${nombre}`,
  pasos: ['Cumplimiento = 0.4·Cronograma + 0.6·Efectividad (celda B19 del Excel del ANS).',
    `= 0.4·${pct(crono)} + 0.6·${pct(efec)} = ${pct(0.4 * (crono || 0) + 0.6 * (efec || 0))}`],
  resultado: pct(0.4 * (crono || 0) + 0.6 * (efec || 0)),
})

function FormulaModal({ frm, onClose }) {
  if (!frm) return null
  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/20" onClick={onClose} />
      <div className="fixed z-50 top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[32rem] max-w-[95vw] max-h-[80vh] overflow-auto bg-white border border-rose-300 rounded-lg shadow-2xl text-xs">
        <div className="sticky top-0 flex items-center justify-between px-3 py-2 bg-rose-50 border-b border-rose-200">
          <span className="font-semibold text-rose-900">{frm.titulo}</span>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={14} /></button>
        </div>
        <div className="p-3 space-y-2">
          {(frm.pasos || []).map((p, i) => <p key={i} className="text-gray-700 leading-relaxed">{p}</p>)}
          {frm.tabla && (
            <table className="text-[11px] border w-full">
              <thead className="bg-gray-100"><tr>{frm.tabla.cols.map((c) => <th key={c} className="px-2 py-1 border text-left">{c}</th>)}</tr></thead>
              <tbody>
                {frm.tabla.filas.map((r, i) => (
                  <tr key={i} className={r.destacar ? 'bg-amber-50' : ''}>
                    {r.celdas.map((c, j) => <td key={j} className="px-2 py-1 border">{c}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {(frm.post || []).map((p, i) => <p key={i} className="text-gray-700 font-medium">{p}</p>)}
          <p className="text-lg font-bold text-rose-700">= {frm.resultado}</p>
          {frm.nota && <p className="text-[10px] text-gray-400 leading-relaxed">{frm.nota}</p>}
        </div>
      </div>
    </>
  )
}

function Bloque35Prog20({ data, meses, periodo, onSubir, onMapeo, onCell, onSub }) {
  const hayMes = (meses || []).includes(periodo)
  const zonas = data?.zonas || ['UBATE', 'ALMEIDAS', 'GUAVIO', 'LV']
  const procs = data?.procesos || []
  const cc = onCell ? 'cursor-pointer hover:underline' : ''
  const [frm, setFrm] = useState(null)                 // fórmula abierta (justificación del %)
  const cf = 'cursor-pointer hover:underline decoration-dotted underline-offset-2'
  const nombreC = (c) => (c === 'OBRAS' ? 'OBRAS (mill. $)' : c)
  // Desglose para la efectividad de totales (tope por concepto, revisión con la dirección)
  const itemsAns = (data?.general || []).map((g) => ({ concepto: nombreC(g.concepto), deben: g.deben, ejecutaron: g.ejecutaron }))
  const itemsZona = (z) => [
    ...procs.map((p) => ({ concepto: p, deben: data?.planeado?.[p]?.[z] ?? 0, ejecutaron: data?.ejecutado?.[p]?.[z] ?? 0 })),
    { concepto: 'OBRAS (mill. $)', deben: data?.inv_mill?.planeado?.[z] ?? 0, ejecutaron: data?.inv_mill?.ejecutado?.[z] ?? 0 },
  ]
  return (
    <Card tono="rosa" title="3.5 — Envío según programación de los 20’ (NUEVO · en construcción)">
      <div className="flex flex-wrap gap-2 mb-3 items-center">
        {onSubir && <button onClick={onSubir} className="flex items-center gap-1 px-2.5 py-1 text-xs bg-rose-600 text-white rounded hover:bg-rose-700"><Upload size={13} /> Subir programación 20’</button>}
        {onMapeo && <button onClick={onMapeo} className="flex items-center gap-1 px-2.5 py-1 text-xs bg-white border border-rose-300 text-rose-700 rounded hover:bg-rose-50">Mapeo subprocesos</button>}
        <span className="text-[11px] text-gray-400">Meses cargados: {(meses || []).join(', ') || '— (sin datos)'}</span>
      </div>
      {hayMes && data?.sin_mapa?.length > 0 && (
        <div className="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded p-1.5 mb-2 max-w-2xl">
          <b>{data.sin_mapa.length} subproceso(s) huérfanos</b>: sin relación con los procesos 3.5, no cuentan en el PLANEADO ni en la plata de OBRAS. Clic en uno para ver sus casos y asignarle la relación ahí mismo:{' '}
          {data.sin_mapa.map((s) => (
            <button key={s} onClick={() => (onSub ? onSub(s) : onCell?.({ fuente: 'prog20', modo: 'sub', subproceso: s }))}
              className="font-medium underline decoration-dotted underline-offset-2 hover:text-amber-900 mr-1.5">
              {s}
            </button>
          ))}
          <span className="block mt-0.5 text-amber-600">Las relaciones ya hechas se corrigen en «Mapeo subprocesos».</span>
        </div>
      )}
      {!data || !hayMes ? (
        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2 max-w-2xl">
          No hay programación de los 20’ para <b>{fmtPeriodo(periodo)}</b>. Sube la «Programación Unificada» (hoja <b>P. UNIFICADA</b>: zona en la columna A, subproceso en la H) y el PLANEADO se calcula con las filas que tengan fecha de ejecución de ese mes. Un archivo con otra estructura se rechaza sin dañar lo cargado.
          {(meses || []).length > 0 && <> Meses disponibles: {meses.join(', ')}.</>}
        </p>
      ) : (
        <div className="space-y-5">
          {/* Cronograma / Efectividad y el ANS como cuadros GRANDES + la tablita META al
              frente — mismo patrón de la tarjeta 3.2 (requisito 2026-07-30) */}
          <div className="flex flex-wrap gap-8 items-start">
            <div className="flex flex-wrap gap-3 items-stretch">
              {[['Cronograma', 'cronograma', 'Enviaron / Deben programar'],
                ['Efectividad', 'efectividad', 'Ejecutaron / Deben (tope por concepto)']].map(([nombre, k, sub]) => (
                <button key={k} title="Ver el cálculo exacto"
                  onClick={() => setFrm(k === 'cronograma'
                    ? formulaFila('crono', 'ANS (total)', data.ans?.deben ?? 0, data.ans?.enviaron ?? 0, 0)
                    : formulaTotalEfec('ANS (total)', itemsAns))}
                  className="bg-white border rounded-lg px-4 py-3 text-left hover:border-rose-400 hover:shadow-sm">
                  <p className="text-[11px] font-semibold text-gray-500">{nombre}</p>
                  <p className="text-2xl font-bold text-rose-700">{pct(data.ans?.[k])}</p>
                  <p className="text-[10px] text-gray-400">{sub}</p>
                </button>
              ))}
              <button title="Ver el cálculo exacto"
                onClick={() => setFrm(formulaCumplTotal('ANS (total)', data.ans?.cronograma, data.ans?.efectividad))}
                className="bg-rose-50 border border-rose-300 rounded-lg px-5 py-3 text-left hover:shadow-sm">
                <p className="text-[11px] font-semibold text-rose-800">Cumplimiento ANS 3.5</p>
                <p className="text-3xl font-bold text-rose-700">{pct(data.ans?.cumplimiento)}</p>
                <p className="text-[11px] text-gray-500">= 0.4·Cronograma + 0.6·Efectividad</p>
              </button>
            </div>
            <NaturalezaSLA titulo="META" filas={data.descuento} valor={data.ans?.cumplimiento} />
          </div>

          <div className="overflow-auto">
            <p className="font-semibold text-xs text-gray-600 mb-1">Tabla general (indicador final del mes)</p>
            <table className="text-xs">
              <thead className="text-gray-500 border-b">
                <tr>{['Concepto', 'Deben programar', 'Enviaron', 'Ejecutaron', 'Cronograma', 'Efectividad', 'Cumplimiento'].map((h) => <th key={h} className="px-3 py-1 text-left whitespace-nowrap">{h}</th>)}</tr>
              </thead>
              <tbody>
                {(data.general || []).map((g, i) => {
                  const esInv = g.concepto === 'OBRAS'   // la fila de inversión (antes INV)
                  const drillPlan = esInv ? { fuente: 'prog20', modo: 'invr' } : { fuente: 'prog20', proceso: g.concepto, modo: 'lcl' }
                  const drillEjec = esInv ? { fuente: 'prog20', modo: 'invr_ejec' } : { grupo: '3.5', concepto: g.concepto, bucket: 'EJECUTADO' }
                  return (
                  <tr key={i} className="border-t">
                    <td className="px-3 py-1 font-medium" title={esInv ? 'OBRAS = inversión (MO + materiales) en millones de pesos enteros — entra al total ANS como un concepto más' : undefined}>
                      {esInv ? 'OBRAS (mill. $)' : g.concepto}
                    </td>
                    <td className={`px-3 py-1 text-red-700 ${cc}`} onClick={() => onCell?.(drillPlan)}>{g.deben}</td>
                    <td className={`px-3 py-1 text-red-700 ${cc}`} onClick={() => onCell?.(drillPlan)}>{g.enviaron}</td>
                    <td className={`px-3 py-1 ${cc}`} onClick={() => onCell?.(drillEjec)}>{g.ejecutaron}</td>
                    <td className={`px-3 py-1 ${cf}`} title="Ver el cálculo" onClick={() => setFrm(formulaFila('crono', nombreC(g.concepto), g.deben, g.enviaron, g.ejecutaron))}>{pct(g.cronograma)}</td>
                    <td className={`px-3 py-1 ${cf}`} title="Ver el cálculo" onClick={() => setFrm(formulaFila('efec', nombreC(g.concepto), g.deben, g.enviaron, g.ejecutaron))}>{pct(g.efectividad)}</td>
                    <td className={`px-3 py-1 font-medium ${cf}`} title="Ver el cálculo" onClick={() => setFrm(formulaFila('cumpl', nombreC(g.concepto), g.deben, g.enviaron, g.ejecutaron))}>{pct(g.cumplimiento)}</td>
                  </tr>
                  )
                })}
                <tr className="border-t-2 font-semibold bg-gray-50">
                  <td className="px-3 py-1">ANS</td>
                  <td className="px-3 py-1">{data.ans?.deben ?? 0}</td>
                  <td className="px-3 py-1">{data.ans?.enviaron ?? 0}</td>
                  <td className="px-3 py-1" title={`Con tope por concepto cuentan ${data.ans?.ejecutaron_cap ?? '—'} de estos ${data.ans?.ejecutaron ?? 0}`}>{data.ans?.ejecutaron ?? 0}</td>
                  <td className={`px-3 py-1 ${cf}`} title="Ver el cálculo" onClick={() => setFrm(formulaFila('crono', 'ANS (total)', data.ans?.deben ?? 0, data.ans?.enviaron ?? 0, 0))}>{pct(data.ans?.cronograma)}</td>
                  <td className={`px-3 py-1 ${cf}`} title="Ver el cálculo" onClick={() => setFrm(formulaTotalEfec('ANS (total)', itemsAns))}>{pct(data.ans?.efectividad)}</td>
                  <td className={`px-3 py-1 text-blue-700 ${cf}`} title="Ver el cálculo" onClick={() => setFrm(formulaCumplTotal('ANS (total)', data.ans?.cronograma, data.ans?.efectividad))}>{pct(data.ans?.cumplimiento)}</td>
                </tr>
              </tbody>
            </table>
            <p className="text-[11px] text-gray-400 mt-1">Fórmulas del Excel del ANS: Cronograma = Enviaron/Deben · Efectividad = Ejecutaron/Deben con tope 100% · Cumplimiento = 0.4·Cronograma + 0.6·Efectividad · Deben en 0 vale 100%. En los totales la efectividad topa por concepto: el exceso de uno no compensa el faltante de otro. <b>Clic en un porcentaje para ver el cálculo exacto.</b></p>
          </div>

          <div className="overflow-auto">
            <p className="font-semibold text-xs text-gray-600 mb-1">Por zona — <span className="text-red-600">PLANEADO</span> (20’) / <span className="text-gray-500">EJECUTADO</span> (plataforma)</p>
            <table className="text-xs border">
              <thead>
                <tr className="bg-gray-100">
                  <th className="px-2 py-1 text-left border">Proceso</th>
                  {zonas.map((z) => <th key={z} colSpan={2} className="px-2 py-1 text-center border">{z}</th>)}
                </tr>
                <tr className="bg-gray-50 text-[10px]">
                  <th className="border"></th>
                  {zonas.flatMap((z) => [<th key={z + 'p'} className="px-2 py-0.5 border text-red-600">PLAN</th>, <th key={z + 'e'} className="px-2 py-0.5 border text-gray-500">EJEC</th>])}
                </tr>
              </thead>
              <tbody>
                {procs.map((p) => (
                  <tr key={p} className="border-t">
                    <td className="px-2 py-1 font-medium border">{p}</td>
                    {zonas.flatMap((z) => [
                      <td key={z + 'p'} className={`px-2 py-1 text-center border bg-red-50 text-red-700 ${cc}`} onClick={() => onCell?.({ fuente: 'prog20', proceso: p, zona: z, modo: 'lcl' })}>{data.planeado?.[p]?.[z] ?? 0}</td>,
                      <td key={z + 'e'} className={`px-2 py-1 text-center border bg-gray-50 ${cc}`} onClick={() => onCell?.({ grupo: '3.5', concepto: p, zona: z, bucket: 'EJECUTADO' })}>{data.ejecutado?.[p]?.[z] ?? 0}</td>,
                    ])}
                  </tr>
                ))}
                {/* INV en millones enteros + TOTAL + porcentajes — la tablita FORMULADA
                    (resumen por zona del Excel del ANS, mismos 0.4/0.6) */}
                <tr className="border-t">
                  <td className="px-2 py-1 font-medium border" title="OBRAS = inversión (MO + materiales) en millones de pesos enteros">OBRAS (mill. $)</td>
                  {zonas.flatMap((z) => [
                    <td key={z + 'p'} className={`px-2 py-1 text-center border bg-red-50 text-red-700 ${cc}`} onClick={() => onCell?.({ fuente: 'prog20', zona: z, modo: 'invr' })}>{data.inv_mill?.planeado?.[z] ?? 0}</td>,
                    <td key={z + 'e'} className={`px-2 py-1 text-center border bg-gray-50 ${cc}`} onClick={() => onCell?.({ fuente: 'prog20', zona: z, modo: 'invr_ejec' })}>{data.inv_mill?.ejecutado?.[z] ?? 0}</td>,
                  ])}
                </tr>
                <tr className="border-t-2 font-semibold bg-gray-50">
                  <td className="px-2 py-1 border">TOTAL</td>
                  {zonas.flatMap((z) => [
                    <td key={z + 'p'} className="px-2 py-1 text-center border text-red-700">{data.por_zona?.[z]?.deben ?? 0}</td>,
                    <td key={z + 'e'} className="px-2 py-1 text-center border">{data.por_zona?.[z]?.ejecutaron ?? 0}</td>,
                  ])}
                </tr>
                <tr className="border-t">
                  <td className="px-2 py-1 border text-[10px] text-gray-500">Cronograma (0.4)</td>
                  {zonas.map((z) => <td key={z} colSpan={2} className={`px-2 py-1 text-center border ${cf}`} title="Ver el cálculo" onClick={() => setFrm(formulaFila('crono', `zona ${z}`, data.por_zona?.[z]?.deben ?? 0, data.por_zona?.[z]?.deben ?? 0, 0))}>{pct(data.por_zona?.[z]?.cronograma)}</td>)}
                </tr>
                <tr>
                  <td className="px-2 py-1 border text-[10px] text-gray-500">Efectividad (0.6)</td>
                  {zonas.map((z) => <td key={z} colSpan={2} className={`px-2 py-1 text-center border ${cf}`} title="Ver el cálculo" onClick={() => setFrm(formulaTotalEfec(`zona ${z}`, itemsZona(z)))}>{pct(data.por_zona?.[z]?.efectividad)}</td>)}
                </tr>
                <tr className="font-semibold">
                  <td className="px-2 py-1 border text-[10px]">Cumplimiento 3.5</td>
                  {zonas.map((z) => <td key={z} colSpan={2} className={`px-2 py-1 text-center border text-rose-700 ${cf}`} title="Ver el cálculo" onClick={() => setFrm(formulaCumplTotal(`zona ${z}`, data.por_zona?.[z]?.cronograma, data.por_zona?.[z]?.efectividad))}>{pct(data.por_zona?.[z]?.cumplimiento)}</td>)}
                </tr>
              </tbody>
            </table>
            <p className="text-[11px] text-gray-400 mt-1">PLANEADO son los LCL únicos de la 20’ y EJECUTADO viene del cálculo de bolsas; OBRAS va en millones enteros. TOTAL zona = procesos + OBRAS y Cumplimiento zona = 0.4·Cronograma + 0.6·Efectividad, igual que el resumen por zona del Excel. El mapeo de subprocesos se edita con el botón de arriba.</p>
          </div>

          <div className="overflow-auto">
            <p className="font-semibold text-xs text-gray-600 mb-1">OBRAS — inversión (MO + materiales, antes INVR)</p>
            <table className="text-xs border">
              <thead className="bg-gray-100"><tr><th className="px-2 py-1 border text-left"></th>{zonas.map((z) => <th key={z} className="px-2 py-1 border">{z}</th>)}</tr></thead>
              <tbody>
                <tr><td className="px-2 py-1 border font-medium text-red-600">PLANEADO (20’)</td>{zonas.map((z) => <td key={z} className={`px-2 py-1 border text-right ${cc}`} onClick={() => onCell?.({ fuente: 'prog20', zona: z, modo: 'invr' })}>{fmtMoney(data.invr?.planeado?.[z])}</td>)}</tr>
                <tr><td className="px-2 py-1 border font-medium text-gray-500">EJECUTADO (plataforma)</td>{zonas.map((z) => <td key={z} className={`px-2 py-1 border text-right ${cc}`} onClick={() => onCell?.({ fuente: 'prog20', zona: z, modo: 'invr_ejec' })}>{fmtMoney(data.invr?.ejecutado?.[z])}</td>)}</tr>
              </tbody>
            </table>
            {Object.values(data.invr?.planeado || {}).every((v) => !v) ? (
              <p className="text-[11px] text-gray-400 mt-1">Este archivo de la 20’ no trae VALOR MO ni MATERIALES, así que el PLANEADO de OBRAS queda en $0. El EJECUTADO sí sale de la programación interna.</p>
            ) : (
              <p className="text-[11px] text-gray-400 mt-1">El PLANEADO suma MO y materiales de las filas de la 20’ relacionadas con OBRAS <b>en el mapeo</b>; el EJECUTADO, la MO real y el material de la programación interna. Si un subproceso de obra no está relacionado, su plata no entra aquí y el total queda por debajo del de la programación.</p>
            )}
          </div>

          <p className="text-[11px] text-gray-400">Clic en un conteo para ver las filas detrás; clic en un porcentaje para ver la fórmula aplicada.</p>
          <p className="text-[11px] text-amber-600">⚠ En construcción: falta afinar el mapeo de subprocesos y el manejo de la inversión (OBRAS)/LV, así que estos números pueden cambiar al ajustarlo.</p>
        </div>
      )}
      <FormulaModal frm={frm} onClose={() => setFrm(null)} />
    </Card>
  )
}

function MapeoModal({ apiBase, K, onClose }) {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: K('ans-subprocmap'),
    queryFn: async () => (await api.get('/subproc-map', { baseURL: apiBase })).data,
  })
  const [rows, setRows] = useState(null)
  const items = rows ?? data?.items ?? []
  const procesos = data?.procesos || []
  const guardar = useMutation({
    mutationFn: async () => (await api.put('/subproc-map', items, { baseURL: apiBase })).data,
    onSuccess: () => { toast.success('Mapeo guardado'); qc.invalidateQueries({ queryKey: K('ans-indplus') }); onClose() },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo guardar el mapeo'),
  })
  const set = (i, proc) => setRows(items.map((it, j) => (j === i ? { ...it, proceso: proc } : it)))
  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div className="bg-white rounded-lg max-w-2xl w-full max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-4 py-3 border-b">
          <h3 className="font-semibold text-gray-800">Mapeo subproceso → proceso 3.5 (programación 20’)</h3>
          <button onClick={onClose}><X size={18} /></button>
        </div>
        <div className="overflow-auto p-4">
          {isLoading ? <p className="text-sm text-gray-400">Cargando…</p> : items.length === 0 ? (
            <p className="text-sm text-gray-400">Aún no hay subprocesos. Sube primero la programación de los 20’.</p>
          ) : (
            <table className="text-xs w-full">
              <thead className="text-gray-500 border-b"><tr><th className="px-2 py-1 text-left">Subproceso (20’)</th><th className="px-2 py-1 text-left">Proceso 3.5</th></tr></thead>
              <tbody>
                {items.map((it, i) => (
                  <tr key={i} className="border-t">
                    <td className="px-2 py-1">{it.subproceso}</td>
                    <td className="px-2 py-1">
                      <select value={it.proceso || ''} onChange={(e) => set(i, e.target.value || null)} className="border rounded px-1 py-0.5 text-xs">
                        <option value="">— sin decidir (sale en el aviso)</option>
                        <option value="NO_CUENTA">No cuenta en el 3.5 (decidido)</option>
                        {procesos.map((p) => <option key={p} value={p}>{p}</option>)}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="px-4 py-3 border-t flex justify-end gap-2">
          <button onClick={onClose} className="px-3 py-1.5 text-sm border rounded">Cancelar</button>
          <button onClick={() => guardar.mutate()} disabled={guardar.isPending} className="px-3 py-1.5 text-sm bg-rose-600 text-white rounded disabled:opacity-50">Guardar</button>
        </div>
      </div>
    </div>
  )
}

// ── Subproceso huérfano (aviso del 3.5): sus casos + asignar la relación AHÍ MISMO ──
// El aviso ámbar abre esto: arriba el selector de relación (proceso 3.5 / «no cuenta» explícito
// que lo saca del aviso / sin decidir) y abajo las filas del archivo para decidir con contexto.
// Las correcciones futuras se hacen en «Mapeo subprocesos» (link al pie).
function SubprocesoModal({ apiBase, K, periodo, subproceso, readOnly, onClose, onMapeo }) {
  const qc = useQueryClient()
  const [proc, setProc] = useState(undefined)          // undefined = todavía no lo ha tocado
  const { data, isLoading } = useQuery({
    queryKey: K('ans-sub-detalle', periodo, subproceso),
    queryFn: async () => (await api.get('/prog20/detalle',
      { baseURL: apiBase, params: { periodo, modo: 'sub', subproceso } })).data,
  })
  const { data: mapa } = useQuery({
    queryKey: K('ans-subprocmap'),
    queryFn: async () => (await api.get('/subproc-map', { baseURL: apiBase })).data,
  })
  const actual = (mapa?.items || []).find(
    (it) => (it.subproceso || '').trim().toUpperCase() === subproceso.trim().toUpperCase())?.proceso ?? ''
  const valor = proc === undefined ? (actual || '') : proc
  const guardar = useMutation({
    mutationFn: async () => (await api.put('/subproc-map',
      [{ subproceso, proceso: valor || null }], { baseURL: apiBase })).data,
    onSuccess: () => {
      toast.success(valor === 'OBRAS'
        ? `«${subproceso}» quedó relacionado con OBRAS — su plata (MO + materiales) alimenta la fila de inversión`
        : valor && valor !== 'NO_CUENTA'
          ? `«${subproceso}» ahora cuenta como ${valor} en el 3.5`
          : valor === 'NO_CUENTA'
            ? `«${subproceso}» quedó marcado como NO cuenta — deja de aparecer en el aviso`
            : `«${subproceso}» quedó sin decidir — seguirá saliendo en el aviso`)
      qc.invalidateQueries({ queryKey: K('ans-indplus') })
      qc.invalidateQueries({ queryKey: K('ans-subprocmap') })
      onClose()
    },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo guardar la relación'),
  })
  return (
    <div className="fixed inset-0 z-50 bg-black/30 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-2xl w-[44rem] max-w-full max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="px-4 py-2.5 bg-amber-50 border-b border-amber-200 flex items-center justify-between">
          <span className="text-sm font-semibold text-amber-900">Subproceso huérfano · «{subproceso}»</span>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={15} /></button>
        </div>
        <div className="p-4 space-y-3 overflow-auto text-xs">
          <div className="bg-rose-50 border border-rose-200 rounded-lg p-3 flex flex-wrap items-center gap-2">
            <span className="font-semibold text-rose-800">Relación con el 3.5:</span>
            <select value={valor} onChange={(e) => setProc(e.target.value)} disabled={readOnly}
              className="border border-rose-300 rounded px-2 py-1 text-xs bg-white">
              <option value="">— sin decidir (sigue en el aviso)</option>
              <option value="NO_CUENTA">No cuenta en el 3.5 (decidido)</option>
              {(mapa?.procesos || []).map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
            {!readOnly && (
              <button onClick={() => guardar.mutate()} disabled={guardar.isPending || valor === (actual || '')}
                className="px-3 py-1 bg-rose-600 text-white rounded text-xs font-medium hover:bg-rose-700 disabled:opacity-40">
                {guardar.isPending ? 'Guardando…' : 'Guardar relación'}
              </button>
            )}
            <span className="basis-full text-[10px] text-gray-500">
              Si lo relacionas con un proceso, sus LCL únicas empiezan a contar en ese PLANEADO al instante.
              «No cuenta» lo deja fuera del indicador y ya no vuelve a salir en el aviso.
            </span>
          </div>
          {isLoading ? <p className="text-gray-400">Cargando casos…</p> : data && (
            <>
              <p className="text-gray-600 leading-relaxed">{data.explicacion}</p>
              <div className="overflow-auto border rounded">
                <table className="text-[11px] w-full">
                  <thead className="bg-gray-100">
                    <tr>{(data.columnas || []).map((c) => <th key={c} className="px-2 py-1 border text-left whitespace-nowrap">{c}</th>)}</tr>
                  </thead>
                  <tbody>
                    {(data.filas || []).map((f, i) => (
                      <tr key={i} className="border-t">
                        {(data.columnas || []).map((c) => <td key={c} className="px-2 py-1 whitespace-nowrap">{f[c]}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-[10px] text-gray-400">{data.nota}</p>
            </>
          )}
        </div>
        <div className="px-4 py-2.5 border-t flex items-center justify-between">
          <button onClick={onMapeo} className="text-[11px] text-rose-700 underline decoration-dotted underline-offset-2 hover:text-rose-900">
            Ver / corregir todas las relaciones (Mapeo subprocesos) →
          </button>
          <button onClick={onClose} className="px-3 py-1.5 text-xs border rounded">Cerrar</button>
        </div>
      </div>
    </div>
  )
}

// ── «Alimentación de los indicadores» (requisito 2026-07-31): de primeras en la
//    pestaña — última subida STM, última carga de bolsas y el semáforo por rol. Los pendientes
//    son los MISMOS de la campanita; el mismo cuadro viaja en el correo de estado. ──
function AlimentacionCard({ alim }) {
  if (!alim) return null
  const COL = {
    verde: 'bg-green-100 text-green-800 border-green-300',
    amarillo: 'bg-yellow-100 text-yellow-800 border-yellow-300',
    rojo: 'bg-red-100 text-red-700 border-red-300',
  }
  const fmtFH = (iso) => (iso ? `${iso.slice(8, 10)}/${iso.slice(5, 7)}/${iso.slice(0, 4)}, ${iso.slice(11, 16)}` : '—')
  return (
    <div className="bg-white border rounded-lg px-4 py-3">
      <p className="text-sm font-semibold text-gray-800 mb-1.5">Alimentación de los indicadores</p>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-gray-600 mb-2">
        <span>Última actualización de STM:{' '}
          <b className="text-gray-800">{alim.stm?.fecha ? fmtFH(alim.stm.fecha) : '— (sin STM este mes)'}</b>
          {alim.stm?.cortes > 1 && <span className="text-gray-400"> · {alim.stm.cortes} cortes</span>}
        </span>
        <span>Última carga de bolsas:{' '}
          <b className="text-gray-800">{alim.bolsas?.datos ? fmtFH(alim.bolsas.datos) : '—'}</b>
          {alim.bolsas?.datos && (alim.bolsas.al_dia
            ? <span className="ml-1 px-1.5 py-0.5 rounded bg-green-100 text-green-700 text-[10px] font-medium">hoy ✓</span>
            : <span className="ml-1 px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 text-[10px] font-medium">no es de hoy</span>)}
        </span>
      </div>
      <div className="flex flex-wrap gap-2 mb-2">
        {(alim.roles || []).map((r) => (
          <span key={r.rol} title={r.motivo}
            className={`px-3 py-1 rounded-full border text-xs font-semibold ${COL[r.estado] || COL.rojo}`}>
            {r.label} — {r.estado === 'verde' ? 'al día' : `${r.pendientes} pendientes${r.estado === 'amarillo' ? ' (esperando insumo)' : ''}`}
          </span>
        ))}
      </div>
      <p className="text-[11px] text-gray-400 leading-relaxed max-w-3xl">
        Verde: al día. Amarillo: tiene celdas pendientes pero su insumo aún no llega, todavía no depende de esa
        persona. Rojo: pendientes propios que están frenando los indicadores. Son los mismos conteos de la campanita.
      </p>
    </div>
  )
}

// ── 5.2 · Oportunidad de conexión para proyectos de nueva demanda ────────────────
// La hoja «5.2» del formato oficial del ANS, calculada con los radicados del subtab 3.Programados.
// El plazo sale de la columna O de esa hoja: CON afectación de clientes son 15 días hábiles
// y SIN afectación son 10, contados desde la fecha de entrega a EECC (que cuenta como el
// día 1, igual que NETWORKDAYS). Ejecutar el día del límite todavía cumple.
const CLASE_ESTADO = {
  'a tiempo': 'text-green-700',
  'vence hoy': 'text-amber-700 font-semibold',
  'vencido': 'text-red-600 font-semibold',
}

// "2026-07-24" → "24/07/2026"
const fechaLarga = (iso) => {
  const m = String(iso || '').match(/^(\d{4})-(\d{2})-(\d{2})/)
  return m ? `${m[3]}/${m[2]}/${m[1]}` : '—'
}
const habiles = (n) => `${Math.abs(n)} ${Math.abs(n) === 1 ? 'día hábil' : 'días hábiles'}`
// El estado del plazo en palabras, para no tener que sacar la cuenta mirando la fecha
const textoPlazo = (p) => {
  if (!p?.limite) return 'sin fecha de entrega'
  if (p.estado === 'vencido') return `vencido hace ${habiles(p.dias)}`
  if (p.estado === 'vence hoy') return 'vence hoy'
  return `quedan ${habiles(p.dias)}`
}

function Bloque52({ data, cargando, zonaFiltro }) {
  // Arranca mostrando los que NO cumplen, que es lo accionable; la tabla por zona cambia esto
  const [sel, setSel] = useState({ bucket: 'NO CUMPLE', zona: null })
  const TITULO = '5.2 — Oportunidad de conexión para proyectos de nueva demanda (NUEVO · en construcción)'

  if (cargando || !data) {
    return <Card tono="cian" title={TITULO} sinImagen>
      <p className="text-sm text-gray-400">{cargando ? 'Calculando…' : 'Sin datos'}</p>
    </Card>
  }
  if (!data.total) {
    return <Card tono="cian" title={TITULO} sinImagen>
      <p className="text-sm text-gray-500">
        Todavía no hay radicados de este mes. Súbelos con el BASE CONTROL de Enel en el subtab «3.Programados».
      </p>
    </Card>
  }

  const zonas = zonaFiltro ? [zonaFiltro] : ZONAS_IND
  const piv = data.pivote
  const proy = data.proyeccion
  const casos = (data.casos || []).filter((c) =>
    (!sel.bucket || c.cumple === sel.bucket) && (!sel.zona || c.zona === sel.zona) &&
    (!zonaFiltro || c.zona === zonaFiltro))
  const filasProy = (proy.filas || []).filter((f) => !zonaFiltro || f.zona === zonaFiltro)
  const conPdl = (proy.con_pdl || []).filter((f) => !zonaFiltro || f.zona === zonaFiltro)
  const th = 'px-2 py-1 border text-center'

  return (
    <Card tono="cian" title={TITULO}>
      <p className="text-[11px] text-gray-500 mb-3 -mt-1 max-w-4xl leading-relaxed">
        Sale de los radicados del subtab «3.Programados» con la misma cuenta que hace el Excel: días hábiles
        entre la fecha de entrega a EECC y la fecha de maniobra, descontando festivos. El plazo es de{' '}
        <b>{proy.topes.con_afectacion} días hábiles con afectación de clientes</b> y{' '}
        <b>{proy.topes.sin_afectacion} sin afectación</b>. Un radicado sin fecha de maniobra todavía no se
        puede medir: esos van abajo, en la proyección.
      </p>

      <div className="flex flex-wrap gap-8 items-start">
        {/* Cuadro grande con el resultado del indicador */}
        <div className="bg-cyan-50 border border-cyan-300 rounded-lg px-5 py-3">
          <p className="text-[11px] font-semibold text-cyan-800">Cumplimiento ANS 5.2</p>
          <p className="text-3xl font-bold text-cyan-700">{data.cumplimiento === null ? '—' : pct(data.cumplimiento)}</p>
          <p className="text-[11px] text-gray-500">
            {piv.filas.CUMPLE ? Object.values(piv.filas.CUMPLE).reduce((a, b) => a + b, 0) : 0} de {data.medidos} órdenes medidas
          </p>
        </div>

        {/* Por zona — la misma forma del 3.2 A. Clic en un número filtra la lista de abajo */}
        <TablaZonas titulo="Cumplimiento por zona" valor={data.cumplimiento ?? undefined}
          tabla={data.por_zona} grupo="5.2" zonaFiltro={zonaFiltro} totalSinExclusiones
          onCell={({ zona, bucket }) => setSel({ zona: zona || null, bucket: bucket === 'EXCLUSION' ? null : (bucket || null) })} />

        {/* La dinámica de la hoja: Cuenta de Radicado × Afectación de clientes */}
        <div className="text-xs">
          <p className="font-semibold text-gray-600 mb-1">Cuenta de Radicado — por afectación de clientes</p>
          <table className="border">
            <thead>
              <tr className="bg-gray-100 text-gray-600">
                <th className="px-2 py-1 border text-left">Cumple</th>
                {piv.columnas.map((c) => <th key={c} className={th}>{c}</th>)}
                <th className={`${th} bg-gray-200`}>Total general</th>
              </tr>
            </thead>
            <tbody>
              {['CUMPLE', 'NO CUMPLE'].map((f) => {
                const fila = piv.filas[f] || {}
                const total = Object.values(fila).reduce((a, b) => a + b, 0)
                const cls = f === 'CUMPLE' ? 'text-green-700' : 'text-red-600'
                return (
                  <tr key={f} className="border-t">
                    <td className="px-2 py-1 border font-medium whitespace-nowrap">{f}</td>
                    {piv.columnas.map((c) => (
                      <td key={c} onClick={() => fila[c] > 0 && setSel({ bucket: f, zona: null })}
                        className={`${th} ${cls} ${fila[c] > 0 ? 'cursor-pointer hover:bg-blue-50 hover:underline' : 'opacity-40'}`}>
                        {fila[c] || 0}
                      </td>
                    ))}
                    <td className={`${th} bg-gray-50 font-semibold ${cls}`}>{total}</td>
                  </tr>
                )
              })}
              <tr className="border-t bg-gray-50 font-semibold">
                <td className="px-2 py-1 border">Total general</td>
                {piv.columnas.map((c) => <td key={c} className={th}>{piv.total_col[c] || 0}</td>)}
                <td className={`${th} bg-gray-100`}>{piv.total}</td>
              </tr>
            </tbody>
          </table>
          <p className="text-[11px] text-gray-400 mt-1">Cumplimiento = CUMPLE / total general.</p>
        </div>
      </div>

      {/* Caso por caso: quién cumple y quién no, con el dato que lo sustenta */}
      <div className="mt-5 pt-4 border-t">
        <div className="flex flex-wrap items-center gap-2 mb-2">
          <p className="text-xs font-semibold text-gray-600">Caso por caso</p>
          {[['NO CUMPLE', 'No cumplen'], ['CUMPLE', 'Cumplen'], [null, 'Todos']].map(([b, label]) => (
            <button key={label} onClick={() => setSel({ bucket: b, zona: null })}
              className={`text-[11px] px-2 py-0.5 rounded border ${sel.bucket === b
                ? 'bg-cyan-700 text-white border-cyan-700' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
              {label}
            </button>
          ))}
          {sel.zona && (
            <button onClick={() => setSel({ ...sel, zona: null })} className="text-[11px] text-blue-700 hover:underline">
              zona {sel.zona} — quitar
            </button>
          )}
          <span className="text-[11px] text-gray-400 ml-auto">{casos.length} de {data.medidos} órdenes medidas</span>
        </div>
        {casos.length === 0 ? (
          <p className="text-xs text-gray-400">No hay órdenes con ese filtro.</p>
        ) : (
          <div className="overflow-auto max-h-80 border rounded">
            <table className="text-[11px] w-full">
              <thead className="bg-gray-100 text-gray-600 sticky top-0">
                <tr>
                  {['Radicado', 'Proyecto', 'Municipio', 'Zona', 'Afectación', 'Entrega a EECC',
                    'Fecha maniobra', 'Días', 'Plazo', 'Resultado'].map((h) => (
                    <th key={h} className="px-2 py-1 border-b text-left whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {casos.map((c) => (
                  <tr key={c.id} className="border-b hover:bg-gray-50">
                    <td className="px-2 py-1 font-medium whitespace-nowrap">{c.radicado}</td>
                    <td className="px-2 py-1 max-w-[240px] truncate" title={c.proyecto}>{c.proyecto || '—'}</td>
                    <td className="px-2 py-1 whitespace-nowrap">{c.municipio || '—'}</td>
                    <td className="px-2 py-1 whitespace-nowrap">{c.zona || <span className="text-amber-600">sin zona</span>}</td>
                    <td className="px-2 py-1 text-center">{c.afectacion || '—'}</td>
                    <td className="px-2 py-1 whitespace-nowrap">{fechaLarga(c.fecha_entrega)}</td>
                    <td className="px-2 py-1 whitespace-nowrap">{fechaLarga(c.fecha_maniobra)}</td>
                    <td className="px-2 py-1 text-center font-semibold">{c.dias}</td>
                    <td className="px-2 py-1 text-center text-gray-500">{c.tope}</td>
                    <td className={`px-2 py-1 whitespace-nowrap ${c.invertido ? 'text-amber-700 font-semibold'
                      : c.cumple === 'CUMPLE' ? 'text-green-700' : 'text-red-600 font-semibold'}`}>
                      {c.invertido
                        ? 'CUMPLE, pero revisar: la maniobra es anterior a la entrega'
                        : c.cumple === 'CUMPLE'
                          ? `CUMPLE — le sobraron ${habiles(c.tope - c.dias)}`
                          : `NO CUMPLE — se pasó por ${habiles(c.exceso)}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Proyección: lo que aún no tiene fecha de maniobra ni PDL, en los dos escenarios */}
      <div className="mt-5 pt-4 border-t">
        <p className="text-xs font-semibold text-gray-600">Proyección — radicados sin fecha de maniobra ni PDL</p>
        <p className="text-[11px] text-gray-500 mt-1 mb-3 max-w-4xl leading-relaxed">
          Sin PDL no se sabe todavía si el proyecto tendrá afectación de clientes, así que el sistema calcula
          las DOS fechas límite posibles desde la fecha de entrega a EECC: {proy.topes.con_afectacion} días
          hábiles si sale con afectación y {proy.topes.sin_afectacion} si sale sin ella. La cuenta está hecha
          al {fechaLarga(proy.hoy)}.
        </p>

        {filasProy.length === 0 && conPdl.length === 0 ? (
          <p className="text-xs text-gray-400">Todos los radicados del mes ya tienen fecha de maniobra: no hay nada que proyectar.</p>
        ) : (
          <>
            {/* Resumen por zona: cuántos alcanzan y cuántos ya se pasaron en cada escenario */}
            {filasProy.length > 0 && (
              <div className="text-xs mb-4">
                <table className="border">
                  <thead>
                    <tr className="bg-gray-100 text-gray-600">
                      <th className="px-2 py-1 border text-left">Escenario</th>
                      {zonas.map((z) => <th key={z} className={th}>{z}</th>)}
                      {!zonaFiltro && <th className={`${th} bg-gray-200`}>TOTAL</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {[['con_afectacion', `Con afectación (${proy.topes.con_afectacion} hábiles)`],
                      ['sin_afectacion', `Sin afectación (${proy.topes.sin_afectacion} hábiles)`]].map(([k, label]) => {
                      const val = (z) => proy.resumen?.[z]?.[k] || { a_tiempo: 0, vencido: 0 }
                      const tot = zonas.reduce((a, z) => ({ a_tiempo: a.a_tiempo + val(z).a_tiempo, vencido: a.vencido + val(z).vencido }),
                        { a_tiempo: 0, vencido: 0 })
                      const celda = (v, key) => (
                        <td key={key} className={`${th} whitespace-nowrap`}>
                          <span className="text-green-700 font-semibold">{v.a_tiempo}</span>
                          <span className="text-gray-400"> a tiempo · </span>
                          <span className={v.vencido ? 'text-red-600 font-semibold' : 'text-gray-400'}>{v.vencido}</span>
                          <span className="text-gray-400"> vencidos</span>
                        </td>
                      )
                      return (
                        <tr key={k} className="border-t">
                          <td className="px-2 py-1 border font-medium whitespace-nowrap">{label}</td>
                          {zonas.map((z) => celda(val(z), z))}
                          {!zonaFiltro && celda(tot, 'T')}
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}

            {/* Detalle: la fecha límite de cada radicado en los dos escenarios */}
            {filasProy.length > 0 && (
              <div className="overflow-auto max-h-80 border rounded">
                <table className="text-[11px] w-full">
                  <thead className="bg-gray-100 text-gray-600 sticky top-0">
                    <tr>
                      {['Radicado', 'Proyecto', 'Municipio', 'Zona', 'Entrega a EECC'].map((h) => (
                        <th key={h} className="px-2 py-1 border-b text-left whitespace-nowrap">{h}</th>
                      ))}
                      <th className="px-2 py-1 border-b border-l text-left whitespace-nowrap" colSpan={2}>
                        Si sale CON afectación ({proy.topes.con_afectacion} hábiles)
                      </th>
                      <th className="px-2 py-1 border-b border-l text-left whitespace-nowrap" colSpan={2}>
                        Si sale SIN afectación ({proy.topes.sin_afectacion} hábiles)
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {filasProy.map((f) => (
                      <tr key={f.id} className="border-b hover:bg-gray-50">
                        <td className="px-2 py-1 font-medium whitespace-nowrap">{f.radicado}</td>
                        <td className="px-2 py-1 max-w-[220px] truncate" title={f.proyecto}>{f.proyecto || '—'}</td>
                        <td className="px-2 py-1 whitespace-nowrap">{f.municipio || '—'}</td>
                        <td className="px-2 py-1 whitespace-nowrap">{f.zona || <span className="text-amber-600">sin zona</span>}</td>
                        <td className="px-2 py-1 whitespace-nowrap">{fechaLarga(f.fecha_entrega)}</td>
                        {['con_afectacion', 'sin_afectacion'].map((k) => (
                          <Fragment key={k}>
                            <td className="px-2 py-1 border-l whitespace-nowrap font-medium">{fechaLarga(f[k]?.limite)}</td>
                            <td className={`px-2 py-1 whitespace-nowrap ${CLASE_ESTADO[f[k]?.estado] || 'text-gray-400'}`}>
                              {textoPlazo(f[k])}
                            </td>
                          </Fragment>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Los que ya tienen PDL: ahí el escenario ya está decidido, el plazo es uno solo */}
            {conPdl.length > 0 && (
              <div className="mt-4">
                <p className="text-[11px] font-semibold text-gray-600 mb-1">
                  Sin fecha de maniobra pero YA con PDL ({conPdl.length}) — aquí el plazo ya está decidido por la afectación
                </p>
                <div className="overflow-auto max-h-56 border rounded">
                  <table className="text-[11px] w-full">
                    <thead className="bg-gray-100 text-gray-600 sticky top-0">
                      <tr>
                        {['Radicado', 'Municipio', 'Zona', 'PDL', 'Afectación', 'Entrega a EECC', 'Fecha límite', 'Cómo va'].map((h) => (
                          <th key={h} className="px-2 py-1 border-b text-left whitespace-nowrap">{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {conPdl.map((f) => (
                        <tr key={f.id} className="border-b hover:bg-gray-50">
                          <td className="px-2 py-1 font-medium whitespace-nowrap">{f.radicado}</td>
                          <td className="px-2 py-1 whitespace-nowrap">{f.municipio || '—'}</td>
                          <td className="px-2 py-1 whitespace-nowrap">{f.zona || <span className="text-amber-600">sin zona</span>}</td>
                          <td className="px-2 py-1 whitespace-nowrap">{f.pdl}</td>
                          <td className="px-2 py-1 text-center">{f.afectacion || '—'}</td>
                          <td className="px-2 py-1 whitespace-nowrap">{fechaLarga(f.fecha_entrega)}</td>
                          <td className="px-2 py-1 whitespace-nowrap font-medium">{fechaLarga(f.limite)}</td>
                          <td className={`px-2 py-1 whitespace-nowrap ${CLASE_ESTADO[f.estado] || 'text-gray-400'}`}>{textoPlazo(f)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {data.sin_zona > 0 && (
        <p className="text-[11px] text-amber-700 mt-3">
          {data.sin_zona} orden{data.sin_zona === 1 ? '' : 'es'} medida{data.sin_zona === 1 ? '' : 's'} sin zona: no
          entran en la tabla por zona pero sí en el cumplimiento. Asigna el municipio a su zona en «3.Programados».
        </p>
      )}
    </Card>
  )
}

function AlertaFechas({ anom }) {
  const items = anom?.anomalias || []
  if (!items.length) return null
  const rota = (v) => /\d/.test(String(v))   // tiene dígitos → probablemente una fecha mal escrita
  return (
    <div className="bg-amber-50 border border-amber-300 rounded-lg p-4">
      <div className="flex items-center gap-2 mb-1">
        <AlertTriangle size={18} className="text-amber-600" />
        <h3 className="text-sm font-semibold text-amber-800">
          Datos por revisar — {items.length} fecha{items.length !== 1 ? 's' : ''} con formato erróneo en las bolsas
        </h3>
      </div>
      <p className="text-xs text-amber-700 mb-3 max-w-3xl">
        En estas celdas hay algo escrito que no es una fecha válida: rangos como «11/04/2026 - 03/06/2026»,
        años con error de digitación, notas del supervisor, «NA», etc. Como no se pueden ubicar en el
        calendario, esos casos no entran al conteo del 3.1 ni del 3.5. No es un error del cálculo, es un
        dato mal digitado en la bolsa; hay que corregirlo en el origen para que el caso cuente.
      </p>
      <div className="overflow-auto">
        <table className="text-xs w-full">
          <thead className="text-amber-800 border-b border-amber-200">
            <tr>{['Indicador', 'Tipo', 'Zona', 'Caso / PQRS', 'Columna', 'Valor encontrado', 'Supervisor'].map((h) => (
              <th key={h} className="px-2 py-1 text-left font-semibold whitespace-nowrap">{h}</th>
            ))}</tr>
          </thead>
          <tbody>
            {items.map((a, i) => (
              <tr key={i} className="border-b border-amber-100">
                <td className="px-2 py-1 font-medium">{a.grupo}</td>
                <td className="px-2 py-1">{a.tipo}</td>
                <td className="px-2 py-1">{a.zona}</td>
                <td className="px-2 py-1 font-mono whitespace-nowrap">{a.id}</td>
                <td className="px-2 py-1 text-gray-500 whitespace-nowrap">{a.columna}</td>
                <td className="px-2 py-1">
                  <span className={`font-mono px-1.5 py-0.5 rounded ${rota(a.valor) ? 'bg-red-100 text-red-700' : 'bg-gray-100 text-gray-600'}`}>{a.valor}</span>
                </td>
                <td className="px-2 py-1 text-gray-500 whitespace-nowrap">{a.supervisor}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// Distintivo visual por indicador (pedido del equipo): cada apartado con su tono en el
// marco (borde izquierdo grueso + borde + título). Coherente con la grilla: 3.2 verde,
// 3.3 ámbar; 3.1 azul, 3.4 violeta y 3.5 rosa completan la paleta.
const TONOS_CARD = {
  azul:    { card: 'border-blue-300 border-l-4 border-l-blue-600',     titulo: 'text-blue-800' },
  verde:   { card: 'border-green-300 border-l-4 border-l-green-600',   titulo: 'text-green-800' },
  ambar:   { card: 'border-amber-300 border-l-4 border-l-amber-500',   titulo: 'text-amber-800' },
  violeta: { card: 'border-violet-300 border-l-4 border-l-violet-600', titulo: 'text-violet-800' },
  rosa:    { card: 'border-rose-300 border-l-4 border-l-rose-600',     titulo: 'text-rose-800' },
  cian:    { card: 'border-cyan-300 border-l-4 border-l-cyan-600',     titulo: 'text-cyan-800' },
}

// Botones para llevarse la tarjeta como imagen: copiar al portapapeles (Ctrl+V en el correo)
// o descargar el .png. Van marcados data-no-capture para no salir en la propia imagen.
function BotonesImagen({ targetRef, nombre }) {
  const [ocupado, setOcupado] = useState(null)   // 'copy' | 'down'
  const copiar = async () => {
    if (!targetRef.current) return
    setOcupado('copy')
    try {
      await copiarImagen(targetRef.current)
      toast.success('Imagen copiada. Pégala en el correo con Ctrl+V')
    } catch {
      // Si el navegador no deja copiar la imagen al portapapeles, cae a descargarla.
      try { await descargarImagen(targetRef.current, nombre); toast('No se pudo copiar; se descargó la imagen') }
      catch { toast.error('No se pudo generar la imagen') }
    } finally { setOcupado(null) }
  }
  const descargar = async () => {
    if (!targetRef.current) return
    setOcupado('down')
    try { await descargarImagen(targetRef.current, nombre) }
    catch { toast.error('No se pudo generar la imagen') }
    finally { setOcupado(null) }
  }
  const cls = 'flex items-center gap-1 px-2 py-1 rounded text-[11px] border border-gray-200 text-gray-600 hover:bg-gray-100 disabled:opacity-50'
  return (
    <div className="flex items-center gap-1 shrink-0" data-no-capture>
      <button onClick={copiar} disabled={!!ocupado} className={cls} title="Copiar la imagen para pegarla en un correo">
        {ocupado === 'copy' ? <Loader2 size={12} className="animate-spin" /> : <Copy size={12} />} Copiar imagen
      </button>
      <button onClick={descargar} disabled={!!ocupado} className={cls} title="Descargar la imagen (.png)">
        {ocupado === 'down' ? <Loader2 size={12} className="animate-spin" /> : <ImageDown size={12} />} Imagen
      </button>
    </div>
  )
}

function Card({ title, children, tono, sinImagen = false }) {
  const t = TONOS_CARD[tono]
  const ref = useRef(null)
  return (
    <div className={`bg-white border rounded-lg p-4 ${t ? t.card : ''}`} ref={ref}>
      <div className="flex items-start justify-between gap-3 mb-3">
        <h3 className={`text-sm font-semibold ${t ? t.titulo : 'text-gray-700'}`}>{title}</h3>
        {!sinImagen && <BotonesImagen targetRef={ref} nombre={title} />}
      </div>
      <div className="overflow-auto">{children}</div>
    </div>
  )
}

function MatrizBolsas({ tabla, filas, zonas, subcols, grupo, onCell }) {
  if (!tabla) return null
  return (
    <table className="text-xs">
      <thead>
        <tr className="text-gray-500">
          <th className="px-2 py-1 text-left">Concepto</th>
          {zonas.map((z) => <th key={z} colSpan={subcols.length} className="px-2 py-1 border-l text-center">{z}</th>)}
        </tr>
        <tr className="text-gray-400">
          <th></th>
          {zonas.map((z) => subcols.map((s) => <th key={z + s} className="px-2 py-1 font-normal whitespace-nowrap">{s.split(' ')[0]}</th>))}
        </tr>
      </thead>
      <tbody>
        {filas.map((f) => (
          <tr key={f} className="border-t">
            <td className="px-2 py-1 font-medium whitespace-nowrap">{f}</td>
            {zonas.map((z) => subcols.map((s) => {
              const v = tabla[f]?.[z]?.[s] ?? 0
              return (
                <td key={f + z + s}
                  onClick={() => v > 0 && onCell({ grupo, concepto: f, zona: z, bucket: s })}
                  className={`px-2 py-1 text-center ${v > 0 ? 'cursor-pointer text-blue-700 font-medium hover:bg-blue-50 hover:underline' : 'text-gray-400'}`}>
                  {v}
                </td>
              )
            }))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function DetalleModal({ apiBase, K, periodo, drill, onClose }) {
  const [q, setQ] = useState('')
  const { data, isLoading } = useQuery({
    queryKey: K('ans-detalle', periodo, drill),
    queryFn: async () => {
      const { fuente, ...params } = drill                       // fuente='prog20' → tablas nuevas de 3.5
      const path = fuente === 'prog20' ? '/prog20/detalle'
        : fuente === 'cruce35' ? '/cruce35/detalle'             // 3.5 bolsa vs programación
        : '/detalle'
      return (await api.get(path, { baseURL: apiBase, params: { periodo, ...params } })).data
    },
  })
  const descargar = async (id, nombre) => {
    try {
      const res = await api.get(`/fuentes/${id}`, { baseURL: apiBase, responseType: 'blob' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([res.data]))
      a.download = nombre || 'fuente.xlsx'
      a.click(); URL.revokeObjectURL(a.href)
    } catch { toast.error('No se pudo descargar la fuente') }
  }
  const fuente = data?.fuente
  const filas = (data?.filas || []).filter((row) =>
    !q || (data.columnas || []).some((c) => String(row[c] ?? '').toLowerCase().includes(q.toLowerCase())))
  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-2xl max-w-5xl w-full max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-4 py-3 border-b">
          <div className="flex items-center gap-2">
            <SearchIcon size={16} className="text-blue-700" />
            <h3 className="font-semibold text-sm text-gray-800">Justificación · {data?.titulo || '…'}</h3>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
        </div>
        {/* Explicación precisa + fuente (siempre arriba) */}
        {data && (data.explicacion || fuente) && (
          <div className="px-4 py-2.5 border-b bg-slate-50 space-y-1.5">
            {data.explicacion && <p className="text-xs text-gray-700"><b className="text-slate-800">Qué es:</b> {data.explicacion}</p>}
            {fuente && (
              <p className="text-xs text-gray-600 flex items-center gap-2 flex-wrap">
                <b className="text-slate-800">Fuente:</b>
                {fuente.tipo === 'modulo' ? (
                  <a href={fuente.ruta} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-blue-600 hover:underline">
                    {fuente.label} <ExternalLink size={12} />
                  </a>
                ) : fuente.fuente_id ? (
                  <span className="inline-flex items-center gap-1">
                    <button onClick={() => descargar(fuente.fuente_id, fuente.label)} className="inline-flex items-center gap-1 text-rose-600 hover:underline">
                      <Download size={12} /> {fuente.label} <span className="text-gray-400">(descargar Excel)</span>
                    </button>
                    {fuente.nota_fuente && <span className="text-gray-400">{fuente.nota_fuente}</span>}
                  </span>
                ) : (
                  <span className="text-gray-400">{fuente.label}</span>
                )}
              </p>
            )}
          </div>
        )}
        <div className="overflow-auto p-3">
          {isLoading ? <p className="text-gray-400 text-sm p-4">Cargando…</p>
          : !data?.filas?.length ? <p className="text-gray-400 text-sm p-4">Sin filas para este dato.</p>
          : (
            <>
              <div className="flex items-center gap-2 mb-2">
                <p className="text-xs text-gray-500"><b>{filas.length}</b>{q ? ` de ${data.total}` : ''} registro(s) sustentan este valor</p>
                <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filtrar filas…"
                  className="ml-auto px-2 py-1 text-xs border rounded w-52" />
              </div>
              {data.nota && <p className="text-[11px] text-blue-700 bg-blue-50 rounded px-2 py-1 mb-2">{data.nota}</p>}
              <table className="text-xs w-full">
                <thead className="bg-gray-50 sticky top-0">
                  <tr>{data.columnas.map((c) => <th key={c} className="px-2 py-1.5 text-left font-semibold text-gray-600 capitalize">{c}</th>)}</tr>
                </thead>
                <tbody>
                  {filas.map((row, i) => (
                    <tr key={i} className="border-t hover:bg-gray-50">
                      {data.columnas.map((c) => <td key={c} className="px-2 py-1 whitespace-nowrap">{String(row[c] ?? '')}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function HistorialModal({ apiBase, K, periodo, readOnly, onClose }) {
  const qc = useQueryClient()
  const { data: cambios, isLoading } = useQuery({
    queryKey: K('ans-cambios', periodo),
    queryFn: async () => (await api.get('/cambios', { baseURL: apiBase, params: { periodo } })).data,
  })
  const revertir = useMutation({
    mutationFn: async (id) => (await api.post(`/cambios/${id}/revertir`, {}, { baseURL: apiBase })).data,
    onSuccess: () => {
      toast.success('Cambio revertido')
      qc.invalidateQueries({ queryKey: K('ans') })
      qc.invalidateQueries({ queryKey: K('ans-cambios') })
    },
    onError: () => toast.error('No se pudo revertir'),
  })
  const fecha = (iso) => { try { return new Date(iso).toLocaleString('es-CO', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) } catch { return '' } }
  const val = (v) => (v === null || v === '' ? '∅' : v)
  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-2xl max-w-5xl w-full max-h-[82vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-4 py-3 border-b">
          <div className="flex items-center gap-2">
            <History size={16} className="text-blue-700" />
            <h3 className="font-semibold text-sm text-gray-800">Historial de cambios · {periodo}</h3>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={18} /></button>
        </div>
        <div className="overflow-auto p-3">
          {isLoading ? <p className="text-gray-400 text-sm p-4">Cargando…</p>
          : !cambios?.length ? <p className="text-gray-400 text-sm p-4">Sin cambios registrados en este periodo.</p>
          : (
            <table className="text-xs w-full">
              <thead className="bg-gray-50 sticky top-0">
                <tr className="text-left text-gray-600">
                  {['Fecha', 'Usuario', 'Protocolo', 'Campo', 'Antes', '→', 'Después', '', ''].map((h, i) => <th key={i} className="px-2 py-1.5 font-semibold">{h}</th>)}
                </tr>
              </thead>
              <tbody>
                {cambios.map((c) => (
                  <tr key={c.id} className="border-t hover:bg-gray-50">
                    <td className="px-2 py-1 whitespace-nowrap text-gray-500">{fecha(c.creado_en)}</td>
                    <td className="px-2 py-1 whitespace-nowrap">{c.usuario || '—'}</td>
                    <td className="px-2 py-1 font-mono">{(c.protocolo || '').split('/')[0]}</td>
                    <td className="px-2 py-1 whitespace-nowrap text-gray-700">{c.campo}</td>
                    <td className="px-2 py-1 text-red-600 line-through max-w-[140px] truncate">{val(c.valor_anterior)}</td>
                    <td className="px-1 text-gray-400">→</td>
                    <td className="px-2 py-1 text-green-700 max-w-[140px] truncate">{val(c.valor_nuevo)}</td>
                    <td className="px-2 py-1">{c.tipo === 'reversion' && <span className="text-[10px] bg-purple-100 text-purple-700 px-1.5 rounded">revertido</span>}</td>
                    <td className="px-2 py-1">
                      {!readOnly && (
                      <button onClick={() => revertir.mutate(c.id)} disabled={revertir.isLoading}
                        className="inline-flex items-center gap-1 text-blue-600 hover:text-blue-800 disabled:opacity-40">
                        <Undo2 size={13} /> Revertir
                      </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}
