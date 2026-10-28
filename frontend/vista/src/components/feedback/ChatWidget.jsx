import { useState, useRef, useEffect, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { MessageCircle, X, Send, Paperclip, Image as ImageIcon, Loader2, EyeOff, ChevronDown, ChevronRight } from 'lucide-react'

const HIDE_KEY = 'sabogal-chat-hidden'
import api from '../../utils/api'
import { useAuthStore } from '../../store/authStore'
import { buildContexto, fileToCompressedDataURL } from '../../utils/feedbackUtils'
import Hilo from './Hilo'
import toast from 'react-hot-toast'

const TIPOS = [
  { id: 'Mejora', label: 'Mejora' },
  { id: 'Error',  label: 'Error' },
  { id: 'Idea',   label: 'Idea' },
  { id: 'Otro',   label: 'Otro' },
]

const ESTADO_META = {
  nueva:        { label: 'Recibida',      cls: 'bg-blue-100 text-blue-700' },
  en_revision:  { label: 'En revisión',   cls: 'bg-amber-100 text-amber-700' },
  planeada:     { label: 'Planeada',      cls: 'bg-purple-100 text-purple-700' },
  implementada: { label: 'Implementada ✓', cls: 'bg-green-100 text-green-700' },
  rechazada:    { label: 'No se hará',    cls: 'bg-gray-200 text-gray-600' },
}

export default function ChatWidget() {
  const { usuario } = useAuthStore()
  const qc = useQueryClient()
  const [open, setOpen]     = useState(false)
  const [hidden, setHidden] = useState(() => localStorage.getItem(HIDE_KEY) === '1')
  const [tipo, setTipo]     = useState('Mejora')
  const [texto, setTexto]   = useState('')
  const [imgs, setImgs]     = useState([])      // data-URIs comprimidos
  const [busyImg, setBusyImg] = useState(false)
  const [abiertos, setAbiertos] = useState(() => new Set())  // hilos expandidos
  const toggleHilo = (id) => setAbiertos((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n })
  const fileRef = useRef(null)
  const scrollRef = useRef(null)

  // Sincroniza el estado activado/desactivado con el menú de usuario (Layout)
  useEffect(() => {
    const sync = (e) => {
      const h = e?.detail?.hidden ?? (localStorage.getItem(HIDE_KEY) === '1')
      setHidden(h)
      if (h) setOpen(false)
    }
    window.addEventListener('sabogal-chat-set', sync)
    return () => window.removeEventListener('sabogal-chat-set', sync)
  }, [])

  const ocultar = () => {
    localStorage.setItem(HIDE_KEY, '1')
    setHidden(true); setOpen(false)
    window.dispatchEvent(new CustomEvent('sabogal-chat-set', { detail: { hidden: true } }))
    toast('Chat desactivado. Puedes reactivarlo desde el menú de usuario.', { duration: 3000 })
  }

  const { data: mias = [] } = useQuery({
    queryKey: ['feedback-mias'],
    queryFn: () => api.get('/feedback/mias').then((r) => r.data),
    refetchInterval: open ? 15000 : 60000,   // sondea respuestas del dev (punto en el botón)
  })
  const hayNoLeido = mias.some((m) => m.no_leido)

  const enviar = useMutation({
    mutationFn: (payload) => api.post('/feedback', payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['feedback-mias'] })
      setTexto(''); setImgs([])
      toast.success('¡Enviado! Gracias por tu aporte', { duration: 1500 })
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo enviar'),
  })

  // Scroll al fondo cuando cambian los mensajes o se abre
  useEffect(() => {
    if (open && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [open, mias.length, enviar.isPending])

  const addFiles = useCallback(async (fileList) => {
    const files = [...fileList].filter((f) => f.type.startsWith('image/'))
    if (!files.length) return
    setBusyImg(true)
    try {
      const nuevos = []
      for (const f of files.slice(0, 6)) {
        try { nuevos.push(await fileToCompressedDataURL(f)) } catch { /* ignore */ }
      }
      setImgs((prev) => [...prev, ...nuevos].slice(0, 6))
    } finally { setBusyImg(false) }
  }, [])

  const onPaste = useCallback((e) => {
    const items = [...(e.clipboardData?.items || [])]
    const imgItems = items.filter((it) => it.type.startsWith('image/'))
    if (imgItems.length) {
      e.preventDefault()
      addFiles(imgItems.map((it) => it.getAsFile()).filter(Boolean))
    }
  }, [addFiles])

  const onDrop = useCallback((e) => {
    e.preventDefault()
    if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files)
  }, [addFiles])

  const handleSend = () => {
    const desc = texto.trim()
    if (!desc && imgs.length === 0) return
    const titulo = (desc.split('\n')[0] || `${tipo} sin título`).slice(0, 80)
    enviar.mutate({
      tipo,
      titulo,
      descripcion: desc || '(sin texto, ver captura)',
      url_pagina: window.location.pathname,
      adjuntos: imgs,
      contexto: buildContexto({ tipo_reporte: tipo }),
    })
  }

  if (hidden) return null

  return (
    <>
      {/* Botón flotante */}
      {!open && (
        <button
          onClick={() => setOpen(true)}
          className="fixed z-50 bottom-20 right-4 md:bottom-6 md:right-6 w-14 h-14 rounded-full bg-blue-700 hover:bg-blue-800 text-white shadow-xl flex items-center justify-center transition-transform hover:scale-105"
          title="Sugerencias y soporte"
        >
          <MessageCircle size={24} />
          {hayNoLeido && <span className="absolute top-0 right-0 w-4 h-4 bg-red-500 rounded-full border-2 border-white animate-pulse" title="Tienes una respuesta nueva" />}
        </button>
      )}

      {/* Panel del chat */}
      {open && (
        <div
          className="fixed z-50 bottom-20 right-2 left-2 md:left-auto md:bottom-6 md:right-6 md:w-[380px] h-[72vh] md:h-[560px] bg-white rounded-2xl shadow-2xl border flex flex-col overflow-hidden"
          onDragOver={(e) => e.preventDefault()}
          onDrop={onDrop}
        >
          {/* Header */}
          <div className="bg-blue-700 text-white px-4 py-3 flex items-center justify-between shrink-0">
            <div className="flex items-center gap-2">
              <MessageCircle size={18} />
              <div>
                <p className="font-semibold text-sm leading-tight">Sugerencias y soporte</p>
                <p className="text-blue-200 text-[11px]">Cuéntanos cómo mejorar la plataforma</p>
              </div>
            </div>
            <div className="flex items-center gap-1">
              <button onClick={ocultar} className="text-blue-100 hover:text-white p-0.5" title="Desactivar chat"><EyeOff size={16} /></button>
              <button onClick={() => setOpen(false)} className="text-blue-100 hover:text-white p-0.5" title="Cerrar"><X size={18} /></button>
            </div>
          </div>

          {/* Mensajes */}
          <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-3 space-y-3 bg-gray-50">
            {/* Bienvenida */}
            <div className="flex">
              <div className="max-w-[85%] bg-white border rounded-2xl rounded-tl-sm px-3 py-2 text-xs text-gray-700 shadow-sm">
                Bienvenido/a{usuario?.nombre ? <>, <b>{usuario.nombre.split(' ')[0]}</b></> : ''}. Este es el canal oficial de mejoras y soporte de la plataforma.
                <br /><br />Por favor describa su solicitud con el mayor detalle posible: <b>qué esperaba</b>, <b>qué ocurrió</b> y <b>en qué sección</b>. Puede adjuntar capturas de pantalla con <b>Ctrl+V</b>.
              </div>
            </div>

            {mias.map((m) => {
              const abierto = abiertos.has(m.id)
              return (
                <div key={m.id} className={`bg-white border rounded-xl shadow-sm overflow-hidden ${m.no_leido ? 'ring-1 ring-blue-400' : ''}`}>
                  <button onClick={() => toggleHilo(m.id)} className="w-full text-left px-2.5 py-2 flex items-center gap-1.5 hover:bg-gray-50">
                    {abierto ? <ChevronDown size={13} className="text-gray-400 shrink-0" /> : <ChevronRight size={13} className="text-gray-400 shrink-0" />}
                    {m.no_leido && <span className="w-2 h-2 rounded-full bg-blue-500 shrink-0" />}
                    <span className="text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-500 shrink-0">{m.tipo}</span>
                    <span className="flex-1 min-w-0 truncate text-xs font-medium text-gray-700">{m.titulo}</span>
                    <span className={`text-[9px] px-1.5 py-0.5 rounded-full shrink-0 ${ESTADO_META[m.estado]?.cls || 'bg-gray-100 text-gray-600'}`}>
                      {ESTADO_META[m.estado]?.label || m.estado}
                    </span>
                    {m.no_leido && <span className="text-[9px] bg-blue-600 text-white px-1 rounded shrink-0">nuevo</span>}
                  </button>
                  {abierto && (
                    <div className="px-2.5 pb-2.5 pt-1 border-t bg-gray-50">
                      <Hilo feedbackId={m.id} esDev={false} raiz={m} onActivity={() => qc.invalidateQueries({ queryKey: ['feedback-mias'] })} />
                    </div>
                  )}
                </div>
              )
            })}

            {enviar.isPending && (
              <div className="flex justify-end">
                <div className="bg-blue-600/70 text-white rounded-2xl px-3 py-2 text-xs flex items-center gap-2">
                  <Loader2 size={12} className="animate-spin" /> Enviando…
                </div>
              </div>
            )}
          </div>

          {/* Compositor */}
          <div className="border-t bg-white px-3 py-2 shrink-0">
            <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Nueva solicitud</div>
            {/* Tipo */}
            <div className="flex gap-1 mb-2">
              {TIPOS.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setTipo(t.id)}
                  className={`px-2 py-0.5 rounded-full text-[11px] border transition-colors ${
                    tipo === t.id ? 'bg-blue-700 text-white border-blue-700' : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50'
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>

            {/* Thumbnails adjuntos */}
            {(imgs.length > 0 || busyImg) && (
              <div className="flex flex-wrap gap-1.5 mb-2">
                {imgs.map((src, i) => (
                  <div key={i} className="relative">
                    <img src={src} alt="" className="w-12 h-12 object-cover rounded border" />
                    <button
                      onClick={() => setImgs((prev) => prev.filter((_, idx) => idx !== i))}
                      className="absolute -top-1.5 -right-1.5 bg-red-500 text-white rounded-full w-4 h-4 flex items-center justify-center text-[10px]"
                    >×</button>
                  </div>
                ))}
                {busyImg && <div className="w-12 h-12 rounded border flex items-center justify-center"><Loader2 size={14} className="animate-spin text-gray-400" /></div>}
              </div>
            )}

            <div className="flex items-end gap-2">
              <button
                onClick={() => fileRef.current?.click()}
                className="text-gray-400 hover:text-blue-700 p-1.5 shrink-0"
                title="Adjuntar captura"
              >
                <Paperclip size={18} />
              </button>
              <input
                ref={fileRef} type="file" accept="image/*" multiple className="hidden"
                onChange={(e) => { addFiles(e.target.files); e.target.value = '' }}
              />
              <textarea
                value={texto}
                onChange={(e) => setTexto(e.target.value)}
                onPaste={onPaste}
                onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleSend() } }}
                rows={1}
                placeholder="Describe tu sugerencia… (Ctrl+V para pegar captura)"
                className="flex-1 resize-none border rounded-xl px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-blue-400 max-h-24"
              />
              <button
                onClick={handleSend}
                disabled={enviar.isPending || (!texto.trim() && imgs.length === 0)}
                className="bg-blue-700 hover:bg-blue-800 text-white rounded-xl p-2 shrink-0 disabled:opacity-40"
                title="Enviar (Ctrl+Enter)"
              >
                <Send size={16} />
              </button>
            </div>
            <p className="text-[10px] text-gray-400 mt-1 flex items-center gap-1">
              <ImageIcon size={10} /> Pega o adjunta capturas · se registra automáticamente la pantalla donde estás
            </p>
          </div>
        </div>
      )}
    </>
  )
}
