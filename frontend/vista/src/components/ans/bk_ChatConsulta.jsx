import { useState, useEffect, useCallback, useRef } from 'react'
import { X, Send, Sparkles, UserPlus, MessageCircle, CheckCircle2, Loader2, CornerDownRight, FileText } from 'lucide-react'
import api from '../../utils/api'

// Chat de investigación de UNA celda de exclusión. Todo queda registrado: el mensaje que
// REALMENTE enviaste (editable antes de mandarlo), lo que te respondieron, y la lectura de la IA.
// Cuando la IA detecta que hay que preguntarle a otra persona, aparece aquí mismo el aviso, y si
// esa persona no tiene número, se agrega sin salir del chat (queda guardada para siempre).
//
// El backend decide el paso: enviar → esperar → decidir → resuelta. El frontend solo lo pinta.

const EXCL_CHIP = {
  SI: 'bg-green-100 text-green-800 border-green-300',
  NO: 'bg-red-100 text-red-800 border-red-300',
}

export default function ChatConsulta({ regId, campo, apiBase, onAplicado, onCerrar }) {
  const [hilo, setHilo] = useState(null)
  const [borrador, setBorrador] = useState('')
  const [respuesta, setRespuesta] = useState('')
  const [tel, setTel] = useState('')
  const [sel, setSel] = useState(null)           // a quién elegiste escribirle (de las opciones)
  const [cargando, setCargando] = useState(true)
  const [ocupado, setOcupado] = useState(null)   // 'enviar' | 'analizar' | 'aplicar' | 'contacto'
  const [err, setErr] = useState(null)
  const finRef = useRef(null)

  const aplica = useCallback((d) => {
    setHilo(d)
    setSel(null)                                 // el backend manda a quién toca ahora
    setBorrador(d?.destinatario?.mensaje || '')
    setErr(null)
  }, [])

  // Elegir a otra de las personas sugeridas: cada una trae SU propio borrador (bien saludada)
  const elegir = (o) => { setSel(o.quien); setBorrador(o.mensaje || ''); setTel('') }

  const cargar = useCallback(async () => {
    setCargando(true)
    try {
      const r = await api.get('/consulta', { baseURL: apiBase, params: { reg_id: regId, campo } })
      aplica(r.data)
    } catch (e) {
      setErr(e?.response?.data?.detail || 'No se pudo abrir el chat')
    } finally { setCargando(false) }
  }, [regId, campo, apiBase, aplica])

  useEffect(() => { cargar() }, [cargar])
  useEffect(() => { finRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [hilo?.mensajes?.length])

  const opciones = hilo?.opciones || []
  const dest = (sel && opciones.find((o) => o.quien === sel)) || hilo?.destinatario
  const paso = hilo?.paso
  // Solo hay que elegir cuando el sistema sugiere varios (líder / supervisor) y aún no has escrito.
  const elegible = paso === 'enviar' && !dest?.remitido_por && opciones.length > 1
  // 3.2 A (excluible/obs): el SI/NO de la exclusión de tiempos lo decide la persona a mano en el
  // grid; "Aprobar" aquí solo copia la observación redactada, nunca el excluible.
  const soloObs = campo === 'excluible' || campo === 'obs'

  // Envía por WhatsApp Y deja constancia del texto exacto que se mandó.
  const enviar = async (abrirWa) => {
    if (!borrador.trim() || !dest) return
    setOcupado('enviar'); setErr(null)
    try {
      if (abrirWa && dest.telefono) {
        window.open(`https://wa.me/${dest.telefono}?text=${encodeURIComponent(borrador)}`, '_blank', 'noopener')
      }
      const r = await api.post('/consulta/enviado',
        { reg_id: regId, campo, persona: dest.quien, rol_dir: dest.rol_dir, texto: borrador },
        { baseURL: apiBase })
      aplica(r.data)
    } catch (e) {
      setErr(e?.response?.data?.detail || 'No se pudo registrar el envío')
    } finally { setOcupado(null) }
  }

  const analizar = async () => {
    if (!respuesta.trim()) return
    setOcupado('analizar'); setErr(null)
    try {
      const r = await api.post('/consulta/analizar',
        { reg_id: regId, campo, persona: dest?.quien, rol_dir: dest?.rol_dir, texto: respuesta },
        { baseURL: apiBase })
      aplica(r.data); setRespuesta('')
    } catch (e) {
      setErr(e?.response?.data?.detail || 'No se pudo analizar')
    } finally { setOcupado(null) }
  }

  const guardarContacto = async () => {
    if (!tel.trim() || !dest) return
    setOcupado('contacto'); setErr(null)
    try {
      await api.post('/contactos',
        { nombre: dest.quien, telefono: tel, rol: dest.rol_dir, zona: hilo?.zona }, { baseURL: apiBase })
      setTel('')
      await cargar()                     // vuelve con su teléfono ya cargado
    } catch (e) {
      setErr(e?.response?.data?.detail || 'No se pudo guardar el contacto')
    } finally { setOcupado(null) }
  }

  const aplicar = async () => {
    if (!hilo?.id) return
    setOcupado('aplicar'); setErr(null)
    try {
      const r = await api.post(`/consulta/${hilo.id}/aplicar`,
        { razon: hilo.propuesta?.razon }, { baseURL: apiBase })   // por si la corregiste aquí
      onAplicado?.(r.data)
      setHilo((h) => ({ ...h, paso: 'resuelta', estado: 'resuelta' }))
    } catch (e) {
      setErr(e?.response?.data?.detail || 'No se pudo aplicar')
    } finally { setOcupado(null) }
  }

  return (
    <>
      <div className="fixed inset-0 z-[60] bg-black/40" onClick={onCerrar} />
      <div className="fixed z-[61] inset-0 flex items-center justify-center p-4 pointer-events-none">
        <div className="pointer-events-auto w-full max-w-xl max-h-[88vh] bg-white rounded-xl shadow-2xl flex flex-col overflow-hidden">

          {/* Cabecera */}
          <div className="flex items-start justify-between px-4 py-2.5 bg-indigo-700 text-white">
            <div>
              <p className="text-sm font-semibold flex items-center gap-1.5">
                <Sparkles size={14} /> Investigación · indicador {hilo?.indicador || '—'}
              </p>
              <p className="text-[11px] text-indigo-200">
                Orden {hilo?.orden || '—'}{hilo?.zona ? ` · ${hilo.zona}` : ''}{hilo?.fecha ? ` · ${hilo.fecha}` : ''}
              </p>
            </div>
            <button onClick={onCerrar} className="text-indigo-200 hover:text-white"><X size={18} /></button>
          </div>

          {/* Conversación */}
          <div className="flex-1 overflow-auto px-4 py-3 bg-gray-50 space-y-2">
            {cargando ? (
              <p className="text-xs text-gray-400 text-center py-6">Abriendo el chat…</p>
            ) : hilo?.mensajes?.length ? (
              hilo.mensajes.map((m, i) => (
                m.direccion === 'ia' ? (
                  <div key={i} className="flex justify-center">
                    <p className="text-[11px] text-indigo-800 bg-indigo-50 border border-indigo-200 rounded-full px-3 py-1 flex items-center gap-1 max-w-[90%]">
                      <Sparkles size={11} className="shrink-0" /> {m.texto}
                    </p>
                  </div>
                ) : (
                  <div key={i} className={`flex ${m.direccion === 'enviado' ? 'justify-end' : 'justify-start'}`}>
                    <div className={`max-w-[80%] rounded-lg px-3 py-1.5 text-xs whitespace-pre-wrap leading-relaxed ${
                      m.direccion === 'enviado'
                        ? 'bg-green-600 text-white rounded-br-none'
                        : 'bg-white border border-gray-200 text-gray-800 rounded-bl-none'}`}>
                      <p className={`text-[10px] font-semibold mb-0.5 ${m.direccion === 'enviado' ? 'text-green-100' : 'text-gray-400'}`}>
                        {m.direccion === 'enviado' ? `Tú → ${m.persona || ''}` : m.persona || 'Respuesta'}
                      </p>
                      {m.texto}
                    </div>
                  </div>
                )
              ))
            ) : (
              <p className="text-xs text-gray-400 text-center py-6">
                Aún no hay conversación. Empieza enviando el mensaje de abajo.
              </p>
            )}
            <div ref={finRef} />
          </div>

          {/* Zona de acción — el backend dice en qué paso vamos */}
          <div className="border-t bg-white px-4 py-3 space-y-2">
            {err && <p className="text-[11px] text-red-600">{err}</p>}

            {/* La bitácora que el hilo ya dejó escrita en la casilla de observación del ANS */}
            {hilo?.nota && paso !== 'resuelta' && (
              <div className="text-[11px] bg-amber-50 border border-amber-200 rounded px-2 py-1.5">
                <p className="text-[10px] uppercase tracking-wide text-amber-700 font-semibold flex items-center gap-1 mb-0.5">
                  <FileText size={11} /> Escrito en la observación del ANS
                </p>
                <p className="text-amber-900 leading-relaxed">{hilo.nota}</p>
              </div>
            )}

            {hilo?.falta && paso !== 'resuelta' && (
              <p className="text-[11px] text-amber-800 bg-amber-50 border border-amber-200 rounded px-2 py-1">
                La IA dice que aún falta: {hilo.falta}
              </p>
            )}
            {hilo?.ia_error && (
              <p className="text-[11px] text-gray-500 bg-gray-50 border rounded px-2 py-1">{hilo.ia_error}</p>
            )}

            {paso === 'resuelta' && (
              <p className="text-xs text-green-700 flex items-center gap-1.5 font-medium py-1">
                <CheckCircle2 size={15} /> Determinación aplicada al ANS. La celda ya quedó diligenciada.
              </p>
            )}

            {/* La IA propuso una determinación → aprobar */}
            {paso === 'decidir' && hilo.propuesta && (
              <div className="border border-indigo-200 rounded-lg p-2.5 bg-indigo-50/60 space-y-1.5">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] uppercase tracking-wide text-gray-500 font-semibold">
                    {soloObs ? 'Sugerencia de la IA' : 'Determinación'}
                  </span>
                  <span className={`text-[11px] font-semibold px-1.5 py-0.5 rounded border ${EXCL_CHIP[hilo.propuesta.excluible] || 'bg-gray-100'}`}>
                    {hilo.propuesta.excluible === 'SI' ? 'Excluible' : 'No excluible'} · {hilo.propuesta.categoria || '—'}
                  </span>
                </div>
                <textarea value={hilo.propuesta.razon || ''} rows={2}
                  onChange={(e) => setHilo((h) => ({ ...h, propuesta: { ...h.propuesta, razon: e.target.value } }))}
                  className="w-full text-xs border border-gray-300 rounded p-1.5 leading-relaxed" />
                <div className="flex items-center gap-2">
                  <button onClick={aplicar} disabled={ocupado === 'aplicar'}
                    className="flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded bg-indigo-700 text-white hover:bg-indigo-800 disabled:opacity-40">
                    {ocupado === 'aplicar' ? <Loader2 size={13} className="animate-spin" /> : <CheckCircle2 size={13} />}
                    {soloObs ? 'Aprobar y escribir la observación' : 'Aprobar y escribir en el ANS'}
                  </button>
                  <span className="text-[10px] text-gray-400">
                    {soloObs ? 'Solo copia la observación — el excluible SI/NO lo decides tú en la celda.'
                              : 'Se copia tal cual a la celda. Puedes corregirla antes.'}
                  </span>
                </div>
              </div>
            )}

            {/* Hay que escribirle a alguien */}
            {paso === 'enviar' && dest && (
              <div className="space-y-1.5">
                {dest.remitido_por && (
                  <p className="text-[11px] text-indigo-700 flex items-center gap-1">
                    <CornerDownRight size={12} /> Hay que escribirle a <b>{dest.quien}</b> — {dest.porque}
                  </p>
                )}

                {/* El sistema sugiere varios (líder / supervisor): ELIGE tú a cuál le escribes */}
                {elegible && (
                  <div>
                    <p className="text-[10px] uppercase tracking-wide text-gray-400 font-semibold mb-1">
                      ¿A quién le escribes?
                    </p>
                    <div className="grid grid-cols-2 gap-1.5">
                      {opciones.map((o) => {
                        const activo = o.quien === dest.quien
                        return (
                          <button key={o.quien} onClick={() => elegir(o)}
                            className={`text-left rounded border p-1.5 transition-colors ${
                              activo ? 'border-indigo-500 bg-indigo-50 ring-1 ring-indigo-300'
                                     : 'border-gray-200 bg-white hover:bg-gray-50'}`}>
                            <p className={`text-[11px] font-semibold leading-tight ${activo ? 'text-indigo-900' : 'text-gray-700'}`}>
                              {o.quien}
                            </p>
                            <p className="text-[10px] text-gray-500 mt-0.5">{o.rol}</p>
                            <p className={`text-[10px] mt-0.5 ${o.telefono ? 'text-green-700' : 'text-amber-700'}`}>
                              {o.telefono ? `+${o.telefono}` : 'sin número'}
                            </p>
                          </button>
                        )
                      })}
                    </div>
                    <p className="text-[10px] text-gray-400 mt-1">{dest.porque}</p>
                  </div>
                )}
                {!dest.telefono ? (
                  <div className="border border-amber-200 bg-amber-50 rounded p-2">
                    <p className="text-[11px] text-amber-900 mb-1.5">
                      <b>{dest.quien}</b> no tiene número guardado. Agrégalo y queda para siempre.
                    </p>
                    <div className="flex gap-1">
                      <input value={tel} onChange={(e) => setTel(e.target.value)} placeholder="300 123 4567"
                        onKeyDown={(e) => { if (e.key === 'Enter') guardarContacto() }}
                        className="flex-1 min-w-0 text-xs border border-gray-300 rounded px-2 py-1" />
                      <button onClick={guardarContacto} disabled={!tel.trim() || ocupado === 'contacto'}
                        className="flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-40 shrink-0">
                        <UserPlus size={13} /> Guardar contacto
                      </button>
                    </div>
                  </div>
                ) : (
                  <p className="text-[11px] text-gray-500">
                    Mensaje para <b className="text-gray-700">{dest.quien}</b> · +{dest.telefono} — edítalo si quieres; se guarda tal como lo envíes.
                  </p>
                )}
                <textarea value={borrador} onChange={(e) => setBorrador(e.target.value)} rows={6}
                  className="w-full text-xs border border-gray-300 rounded p-2 leading-relaxed" />
                <div className="flex items-center gap-2">
                  <button onClick={() => enviar(true)} disabled={!dest.telefono || !borrador.trim() || ocupado === 'enviar'}
                    className="flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-40">
                    {ocupado === 'enviar' ? <Loader2 size={13} className="animate-spin" /> : <MessageCircle size={13} />}
                    Enviar por WhatsApp
                  </button>
                  <button onClick={() => enviar(false)} disabled={!borrador.trim() || ocupado === 'enviar'}
                    className="text-xs px-2.5 py-1 rounded border border-gray-300 text-gray-600 hover:bg-gray-50 disabled:opacity-40">
                    Solo registrar
                  </button>
                </div>
              </div>
            )}

            {/* Ya le escribimos → pegar lo que respondió */}
            {paso === 'esperar' && (
              <div className="space-y-1.5">
                <p className="text-[11px] text-gray-500">
                  Esperando a <b className="text-gray-700">{dest?.quien}</b>. Pega aquí su respuesta y la IA decide.
                </p>
                <textarea value={respuesta} onChange={(e) => setRespuesta(e.target.value)} rows={3}
                  placeholder="Pega lo que te respondió por WhatsApp…"
                  className="w-full text-xs border border-gray-300 rounded p-2" />
                <button onClick={analizar} disabled={!respuesta.trim() || ocupado === 'analizar'}
                  className="flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-40">
                  {ocupado === 'analizar' ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
                  {ocupado === 'analizar' ? 'Analizando…' : 'Analizar con IA'}
                </button>
              </div>
            )}

            {!cargando && !dest && paso !== 'resuelta' && paso !== 'decidir' && (
              <p className="text-[11px] text-gray-500">
                No hay a quién preguntarle automáticamente para esta celda. Decídela a mano.
              </p>
            )}
          </div>
        </div>
      </div>
    </>
  )
}
