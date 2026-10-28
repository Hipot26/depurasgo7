import { useState, useRef, useCallback, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Send, Paperclip, Loader2, Pencil, Check, X } from 'lucide-react'
import api from '../../utils/api'
import { fileToCompressedDataURL } from '../../utils/feedbackUtils'
import { useAuthStore } from '../../store/authStore'
import toast from 'react-hot-toast'

function hora(iso) {
  try { return new Date(iso).toLocaleString('es-CO', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) }
  catch { return '' }
}

/**
 * Hilo de conversación de una solicitud (estilo Slack), reutilizable.
 *  - feedbackId: id de la solicitud
 *  - esDev: true si quien mira es el desarrollador (alinea sus mensajes a la derecha)
 *  - raiz: la solicitud original { autor_nombre, descripcion, adjuntos, creado_en } → primer mensaje
 *  - onActivity: callback al abrir/escribir (para refrescar badges de no-leído del padre)
 */
export default function Hilo({ feedbackId, esDev, raiz, onActivity }) {
  const qc = useQueryClient()
  const yo = useAuthStore((s) => s.usuario)
  const [texto, setTexto] = useState('')
  const [imgs, setImgs] = useState([])
  const [busyImg, setBusyImg] = useState(false)
  const [editId, setEditId] = useState(null)     // mensaje que estoy corrigiendo
  const [editTxt, setEditTxt] = useState('')
  const fileRef = useRef(null)
  const endRef = useRef(null)
  const loaded = useRef(false)

  const { data: msgs = [], isLoading } = useQuery({
    queryKey: ['hilo', feedbackId],
    queryFn: () => api.get(`/feedback/${feedbackId}/mensajes`).then((r) => r.data),
    refetchInterval: 15000,
  })

  // Al abrir el hilo el server lo marca leído → refresca los badges del padre (una vez)
  useEffect(() => {
    if (!isLoading && !loaded.current) { loaded.current = true; onActivity?.() }
  }, [isLoading, onActivity])

  useEffect(() => {
    if (endRef.current) endRef.current.scrollIntoView({ block: 'nearest' })
  }, [msgs.length])

  const enviar = useMutation({
    mutationFn: (payload) => api.post(`/feedback/${feedbackId}/mensajes`, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['hilo', feedbackId] })
      setTexto(''); setImgs([])
      onActivity?.()
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo enviar'),
  })

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
    const items = [...(e.clipboardData?.items || [])].filter((it) => it.type.startsWith('image/'))
    if (items.length) { e.preventDefault(); addFiles(items.map((it) => it.getAsFile()).filter(Boolean)) }
  }, [addFiles])

  const send = () => {
    if (!texto.trim() && imgs.length === 0) return
    enviar.mutate({ cuerpo: texto.trim(), adjuntos: imgs })
  }

  // Corregir un mensaje ya enviado (solo los míos; el backend lo vuelve a validar)
  const editar = useMutation({
    mutationFn: ({ id, cuerpo }) => api.patch(`/feedback/${feedbackId}/mensajes/${id}`, { cuerpo }).then((r) => r.data),
    onSuccess: () => { setEditId(null); qc.invalidateQueries({ queryKey: ['hilo', feedbackId] }) },
    onError: (e) => toast.error(e?.response?.data?.detail || 'No se pudo editar'),
  })

  const abrirEdicion = (m) => { setEditId(m.id); setEditTxt(m.cuerpo || '') }
  const guardarEdicion = () => {
    const t = editTxt.trim()
    if (!t) return toast.error('El mensaje no puede quedar vacío')
    editar.mutate({ id: editId, cuerpo: t })
  }

  // Mensaje raíz (la solicitud) + respuestas, en una sola lista
  const items = [
    { id: 'raiz', es_dev: false, autor_nombre: raiz?.autor_nombre, cuerpo: raiz?.descripcion,
      adjuntos: raiz?.adjuntos || [], creado_en: raiz?.creado_en, _raiz: true },
    ...msgs,
  ]

  return (
    <div className="flex flex-col">
      <div className="space-y-2 max-h-72 overflow-y-auto pr-1">
        {items.map((m) => {
          const mio = esDev === !!m.es_dev          // "mío" → derecha/azul
          return (
            <div key={m.id} className={`flex ${mio ? 'justify-end' : 'justify-start'}`}>
              <div className={`max-w-[85%] rounded-2xl px-3 py-2 text-xs shadow-sm ${
                mio ? 'bg-blue-600 text-white rounded-tr-sm' : 'bg-white border text-gray-700 rounded-tl-sm'
              }`}>
                <div className={`text-[10px] mb-0.5 ${mio ? 'text-blue-100' : 'text-blue-700 font-semibold'}`}>
                  {m._raiz ? `${m.autor_nombre || 'Usuario'} · solicitud` : (m.es_dev ? 'Equipo' : m.autor_nombre)}
                </div>
                {editId === m.id ? (
                  <div className="space-y-1">
                    <textarea value={editTxt} onChange={(e) => setEditTxt(e.target.value)} rows={4} autoFocus
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) guardarEdicion()
                        if (e.key === 'Escape') setEditId(null)
                      }}
                      className="w-full text-xs text-gray-800 border border-blue-300 rounded p-1.5 whitespace-pre-wrap" />
                    <div className="flex items-center gap-1">
                      <button onClick={guardarEdicion} disabled={editar.isPending}
                        className="flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded bg-white text-blue-700 hover:bg-blue-50 disabled:opacity-40">
                        {editar.isPending ? <Loader2 size={10} className="animate-spin" /> : <Check size={10} />} Guardar
                      </button>
                      <button onClick={() => setEditId(null)}
                        className={`flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded ${mio ? 'text-blue-100 hover:bg-blue-500' : 'text-gray-500 hover:bg-gray-100'}`}>
                        <X size={10} /> Cancelar
                      </button>
                      <span className={`text-[9px] ${mio ? 'text-blue-200' : 'text-gray-400'}`}>Ctrl+Enter guarda · Esc cancela</span>
                    </div>
                  </div>
                ) : (
                  m.cuerpo && <div className="whitespace-pre-wrap break-words">{m.cuerpo}</div>
                )}
                {m.adjuntos?.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1.5">
                    {m.adjuntos.map((src, i) => (
                      <a key={i} href={src} target="_blank" rel="noreferrer">
                        <img src={src} alt="" className={`w-14 h-14 object-cover rounded border ${mio ? 'border-white/30' : ''}`} />
                      </a>
                    ))}
                  </div>
                )}
                <div className={`flex items-center gap-1.5 text-[10px] mt-1 ${mio ? 'text-blue-100 justify-end' : 'text-gray-400'}`}>
                  <span>{hora(m.creado_en)}</span>
                  {m.editado_en && <span className="italic opacity-80">(editado)</span>}
                  {/* Solo el autor corrige lo suyo; la raíz (la solicitud) no se edita aquí */}
                  {!m._raiz && editId !== m.id && yo?.id && m.autor_id === yo.id && (
                    <button onClick={() => abrirEdicion(m)} title="Editar mensaje"
                      className={`${mio ? 'text-blue-200 hover:text-white' : 'text-gray-300 hover:text-blue-600'}`}>
                      <Pencil size={10} />
                    </button>
                  )}
                </div>
              </div>
            </div>
          )
        })}
        {enviar.isPending && (
          <div className="flex justify-end"><div className="bg-blue-600/70 text-white rounded-2xl px-3 py-1.5 text-xs flex items-center gap-1"><Loader2 size={11} className="animate-spin" /> Enviando…</div></div>
        )}
        <div ref={endRef} />
      </div>

      {/* Composer */}
      {(imgs.length > 0 || busyImg) && (
        <div className="flex flex-wrap gap-1.5 mt-2">
          {imgs.map((src, i) => (
            <div key={i} className="relative">
              <img src={src} alt="" className="w-11 h-11 object-cover rounded border" />
              <button onClick={() => setImgs((p) => p.filter((_, idx) => idx !== i))}
                className="absolute -top-1.5 -right-1.5 bg-red-500 text-white rounded-full w-4 h-4 flex items-center justify-center text-[10px]">×</button>
            </div>
          ))}
          {busyImg && <div className="w-11 h-11 rounded border flex items-center justify-center"><Loader2 size={13} className="animate-spin text-gray-400" /></div>}
        </div>
      )}
      <div className="flex items-end gap-2 mt-2">
        <button onClick={() => fileRef.current?.click()} className="text-gray-400 hover:text-blue-700 p-1.5 shrink-0" title="Adjuntar captura">
          <Paperclip size={16} />
        </button>
        <input ref={fileRef} type="file" accept="image/*" multiple className="hidden"
          onChange={(e) => { addFiles(e.target.files); e.target.value = '' }} />
        <textarea
          value={texto}
          onChange={(e) => setTexto(e.target.value)}
          onPaste={onPaste}
          onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send() } }}
          rows={1}
          placeholder="Escribe una respuesta… (Ctrl+V pega captura · Ctrl+Enter envía)"
          className="flex-1 resize-none border rounded-xl px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-blue-400 max-h-24"
        />
        <button onClick={send} disabled={enviar.isPending || (!texto.trim() && imgs.length === 0)}
          className="bg-blue-700 hover:bg-blue-800 text-white rounded-xl p-2 shrink-0 disabled:opacity-40" title="Enviar (Ctrl+Enter)">
          <Send size={15} />
        </button>
      </div>
    </div>
  )
}
