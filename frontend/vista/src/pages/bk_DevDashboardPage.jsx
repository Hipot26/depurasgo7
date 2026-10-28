import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../utils/api'
import toast from 'react-hot-toast'
import {
  Inbox, Trash2, ChevronDown, ChevronRight, Monitor, AlertTriangle,
  Clock, MapPin, Globe, X, MessageSquare, CircleDot, CheckCircle2, Archive
} from 'lucide-react'
import Hilo from '../components/feedback/Hilo'

const ESTADOS = [
  { id: 'nueva',        label: 'Recibida',     cls: 'bg-blue-100 text-blue-700' },
  { id: 'en_revision',  label: 'En revisión',  cls: 'bg-amber-100 text-amber-700' },
  { id: 'planeada',     label: 'Planeada',     cls: 'bg-purple-100 text-purple-700' },
  { id: 'implementada', label: 'Implementada', cls: 'bg-green-100 text-green-700' },
  { id: 'rechazada',    label: 'No se hará',   cls: 'bg-gray-200 text-gray-600' },
]
const ESTADO_MAP = Object.fromEntries(ESTADOS.map((e) => [e.id, e]))

// Tabs del buzón: lo pendiente separado de lo ya resuelto/descartado
const TABS = [
  { id: 'pendientes',  label: 'Pendientes',    icon: CircleDot,    estados: ['nueva', 'en_revision', 'planeada'] },
  { id: 'hechas',      label: 'Implementadas', icon: CheckCircle2, estados: ['implementada'] },
  { id: 'descartadas', label: 'No se hará',    icon: Archive,      estados: ['rechazada'] },
]
const PRIORIDADES = ['', 'baja', 'media', 'alta']
const PRIO_CLS = { alta: 'text-red-600', media: 'text-amber-600', baja: 'text-gray-500' }
const ROL_LABELS = { supervisor: 'Jefe de Zona', ingeniero_zona: 'Ingeniero', programador: 'Programador', facturacion: 'Facturación', desarrollador: 'Dev', cumplimentador: 'Cumplimentador' }

function fecha(iso) {
  try { return new Date(iso).toLocaleString('es-CO', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) }
  catch { return '' }
}

function ContextoTecnico({ ctx }) {
  const [open, setOpen] = useState(false)
  if (!ctx) return null
  const errores = ctx.errores_recientes || []
  return (
    <div className="mt-2 border-t pt-2">
      <button onClick={() => setOpen((v) => !v)} className="flex items-center gap-1 text-[11px] text-gray-500 hover:text-gray-700">
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        Contexto técnico {errores.length > 0 && <span className="text-red-500 font-medium">· {errores.length} error(es) JS</span>}
      </button>
      {open && (
        <div className="mt-1.5 bg-gray-50 rounded-lg p-2 text-[11px] text-gray-600 space-y-1 font-mono">
          <div className="flex items-start gap-1"><MapPin size={11} className="mt-0.5 shrink-0" /> <span className="break-all">{ctx.ruta} · {ctx.titulo_pagina}</span></div>
          <div className="flex items-start gap-1"><Globe size={11} className="mt-0.5 shrink-0" /> <span className="break-all">{ctx.url}</span></div>
          <div className="flex items-start gap-1"><Monitor size={11} className="mt-0.5 shrink-0" /> {ctx.viewport} (pantalla {ctx.pantalla}) · {ctx.plataforma} · {ctx.idioma}</div>
          <div className="flex items-start gap-1"><Clock size={11} className="mt-0.5 shrink-0" /> {ctx.zona_horaria} · enviado {fecha(ctx.enviado_en)}</div>
          <div className="break-all text-gray-400">{ctx.user_agent}</div>
          {errores.length > 0 && (
            <div className="mt-1 border-t border-gray-200 pt-1">
              <div className="flex items-center gap-1 text-red-600 font-semibold"><AlertTriangle size={11} /> Errores JS recientes</div>
              {errores.map((er, i) => (
                <div key={i} className="text-red-500 break-all">• [{er.tipo}] {er.msg} <span className="text-gray-400">{er.origen}</span></div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function DevDashboardPage() {
  const qc = useQueryClient()
  const [tab, setTab] = useState('pendientes')
  const [filtro, setFiltro] = useState('')          // sub-filtro por estado dentro de Pendientes
  const [lightbox, setLightbox] = useState(null)
  const [abiertos, setAbiertos] = useState(() => new Set())  // hilos expandidos
  const toggleHilo = (id) => setAbiertos((prev) => {
    const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n
  })
  const refrescar = () => {
    qc.invalidateQueries({ queryKey: ['feedback'] })
    qc.invalidateQueries({ queryKey: ['feedback-stats'] })
  }

  const { data: items = [], isLoading } = useQuery({
    queryKey: ['feedback'],
    queryFn: () => api.get('/feedback').then((r) => r.data),
    refetchInterval: 30000,
  })

  const tabActivo = TABS.find((t) => t.id === tab) || TABS[0]
  const porTab = useMemo(() => {
    const g = Object.fromEntries(TABS.map((t) => [t.id, []]))
    for (const it of items) {
      const t = TABS.find((t) => t.estados.includes(it.estado)) || TABS[0]
      g[t.id].push(it)
    }
    return g
  }, [items])
  const visibles = porTab[tab].filter((it) => tab !== 'pendientes' || !filtro || it.estado === filtro)

  const patch = useMutation({
    mutationFn: ({ id, ...data }) => api.patch(`/feedback/${id}`, data),
    onSuccess: (_r, vars) => {
      qc.invalidateQueries({ queryKey: ['feedback'] })
      qc.invalidateQueries({ queryKey: ['feedback-stats'] })
      const destino = vars.estado && TABS.find((t) => t.estados.includes(vars.estado))
      toast.success(destino && !tabActivo.estados.includes(vars.estado) ? `Movida a ${destino.label}` : 'Actualizado', { duration: 1400 })
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'Error'),
  })

  const del = useMutation({
    mutationFn: (id) => api.delete(`/feedback/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feedback'] })
      qc.invalidateQueries({ queryKey: ['feedback-stats'] })
      toast.success('Eliminado')
    },
  })

  return (
    <div className="p-4 md:p-6 max-w-4xl mx-auto">
      <div className="flex items-center gap-2 mb-4">
        <Inbox size={22} className="text-blue-700" />
        <div>
          <h1 className="text-xl font-bold text-gray-800">Buzón de sugerencias</h1>
          <p className="text-sm text-gray-500">Peticiones de mejora de todos los usuarios</p>
        </div>
      </div>

      {/* Tabs por estado: pendiente / implementada / descartada */}
      <div className="flex border-b mb-3">
        {TABS.map((t) => {
          const Icono = t.icon
          const nuevos = porTab[t.id].some((it) => it.no_leido)
          return (
            <button
              key={t.id}
              onClick={() => { setTab(t.id); setFiltro('') }}
              className={`relative flex items-center gap-1.5 px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${
                tab === t.id ? 'border-blue-700 text-blue-700 font-semibold' : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              <Icono size={15} />
              {t.label}
              <span className={`text-[11px] px-1.5 py-0.5 rounded-full ${tab === t.id ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-500'}`}>
                {porTab[t.id].length}
              </span>
              {nuevos && <span className="w-2 h-2 rounded-full bg-blue-500 absolute top-1.5 right-1" title="Hay mensajes sin leer" />}
            </button>
          )
        })}
      </div>

      {/* Sub-filtro por estado, solo dentro de Pendientes */}
      {tab === 'pendientes' && (
        <div className="flex flex-wrap gap-2 mb-4">
          <button
            onClick={() => setFiltro('')}
            className={`px-3 py-1.5 rounded-lg text-xs border ${!filtro ? 'bg-blue-700 text-white border-blue-700' : 'bg-white text-gray-600'}`}
          >
            Todas <b className="ml-1">{porTab.pendientes.length}</b>
          </button>
          {ESTADOS.filter((e) => tabActivo.estados.includes(e.id)).map((e) => (
            <button
              key={e.id}
              onClick={() => setFiltro(e.id)}
              className={`px-3 py-1.5 rounded-lg text-xs border ${filtro === e.id ? 'bg-blue-700 text-white border-blue-700' : 'bg-white text-gray-600'}`}
            >
              {e.label} <b className="ml-1">{porTab.pendientes.filter((it) => it.estado === e.id).length}</b>
            </button>
          ))}
        </div>
      )}

      {isLoading ? (
        <div className="text-gray-400 text-sm py-10 text-center">Cargando…</div>
      ) : visibles.length === 0 ? (
        <div className="text-gray-400 text-sm py-10 text-center">
          {tab === 'pendientes' ? (filtro ? 'Sin solicitudes en este estado.' : 'Nada pendiente.') : `Sin solicitudes en ${tabActivo.label.toLowerCase()}.`}
        </div>
      ) : (
        <div className="space-y-3">
          {visibles.map((m) => (
            <div key={m.id} className={`bg-white border rounded-xl shadow-sm p-4 ${m.no_leido ? 'ring-2 ring-blue-400 border-blue-300' : ''}`}>
              {/* Cabecera */}
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    {m.no_leido && <span className="w-2 h-2 rounded-full bg-blue-500 shrink-0" title="Mensaje nuevo sin leer" />}
                    <span className="text-[11px] px-1.5 py-0.5 rounded bg-blue-50 text-blue-700 font-bold font-mono" title="ID de la solicitud">#{m.id}</span>
                    <span className="text-[11px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 font-medium">{m.tipo}</span>
                    <span className="font-semibold text-gray-800 text-sm truncate">{m.titulo}</span>
                  </div>
                  <div className="text-[11px] text-gray-400 mt-0.5">
                    {m.autor_nombre} · {ROL_LABELS[m.autor_rol] || m.autor_rol}{m.autor_zona ? ` · ${m.autor_zona}` : ''} · {fecha(m.creado_en)}
                  </div>
                </div>
                <button onClick={() => del.mutate(m.id)} className="text-gray-300 hover:text-red-500 shrink-0" title="Eliminar"><Trash2 size={15} /></button>
              </div>

              {/* Descripción */}
              <p className="text-sm text-gray-700 mt-2 whitespace-pre-wrap break-words">{m.descripcion}</p>

              {/* Capturas */}
              {m.adjuntos?.length > 0 && (
                <div className="flex flex-wrap gap-2 mt-2">
                  {m.adjuntos.map((src, i) => (
                    <img
                      key={i} src={src} alt="" onClick={() => setLightbox(src)}
                      className="w-20 h-20 object-cover rounded-lg border cursor-pointer hover:opacity-80"
                    />
                  ))}
                </div>
              )}

              {/* Contexto técnico oculto */}
              <ContextoTecnico ctx={m.contexto} />

              {/* Controles del dev */}
              <div className="mt-3 border-t pt-3 flex flex-wrap items-center gap-2">
                <select
                  value={m.estado}
                  onChange={(e) => patch.mutate({ id: m.id, estado: e.target.value })}
                  className="border rounded-lg px-2 py-1 text-xs"
                >
                  {ESTADOS.map((e) => <option key={e.id} value={e.id}>{e.label}</option>)}
                </select>
                <select
                  value={m.prioridad || ''}
                  onChange={(e) => patch.mutate({ id: m.id, prioridad: e.target.value || null })}
                  className={`border rounded-lg px-2 py-1 text-xs ${PRIO_CLS[m.prioridad] || 'text-gray-500'}`}
                >
                  {PRIORIDADES.map((p) => <option key={p} value={p}>{p ? `Prioridad ${p}` : 'Sin prioridad'}</option>)}
                </select>
                <span className={`text-[11px] px-2 py-0.5 rounded-full ${ESTADO_MAP[m.estado]?.cls}`}>{ESTADO_MAP[m.estado]?.label}</span>
              </div>

              {/* Conversación (hilo) */}
              <div className="mt-3 border-t pt-3">
                <button onClick={() => toggleHilo(m.id)} className="flex items-center gap-1.5 text-xs font-medium text-blue-700 hover:text-blue-900">
                  <MessageSquare size={14} />
                  {abiertos.has(m.id) ? 'Ocultar conversación' : 'Conversación'}
                  {m.n_mensajes > 0 && <span className="text-gray-400">({m.n_mensajes})</span>}
                  {m.no_leido && <span className="ml-0.5 text-[10px] bg-blue-600 text-white px-1.5 py-0.5 rounded-full">nuevo</span>}
                </button>
                {abiertos.has(m.id) && (
                  <div className="mt-2">
                    <Hilo feedbackId={m.id} esDev raiz={m} onActivity={refrescar} />
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Lightbox */}
      {lightbox && (
        <div className="fixed inset-0 z-[60] bg-black/80 flex items-center justify-center p-4" onClick={() => setLightbox(null)}>
          <button className="absolute top-4 right-4 text-white"><X size={26} /></button>
          <img src={lightbox} alt="" className="max-w-full max-h-full rounded-lg" />
        </div>
      )}
    </div>
  )
}
