// Tab «Correos ANS»: los correos etiquetados «ANS» en el buzón vigilado llegan aquí solos
// (agente local correo-bot). Cada uno trae sus PDL cazados y
// cruzados contra el ANS: adjuntar a la celda es un clic (el mismo efecto del flujo
// manual de subir el .msg) y el análisis IA queda a un botón. El SI/NO sigue siendo humano.
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../utils/api'
import toast from 'react-hot-toast'
import { Mail, Loader2, CheckCircle2, XCircle, ChevronLeft, Download, Sparkles, AlertTriangle, RefreshCw, RotateCcw, ShieldAlert } from 'lucide-react'
import CoberturaRapida from './CoberturaRapida'
import { resaltarCitas, codigosCaso, fragmentosPdl } from './CorreoCitado'
import { depurarObs } from '../../utils/obs'

// Valor histórico del campo `analista` guardado en la base para las lecturas registradas desde una sesión asistida.
const ANALISTA_SESION_ASISTIDA = 'fable-chat'

const ESTADO = {
  nuevo: 'bg-blue-100 text-blue-800',
  vinculado: 'bg-emerald-100 text-emerald-800',
  descartado: 'bg-gray-200 text-gray-500',
  revisado: 'bg-slate-200 text-slate-600',
}
const ESTADO_LABEL = { nuevo: 'por revisar', vinculado: 'adjuntado ✓', descartado: 'descartado', revisado: 'revisado · sin cambio' }
// ¿Se trabaja sobre este cruce? Lo decide el backend (flags_vivos); la fórmula vieja queda de respaldo.
// Desde el 2026-10-02 un SI ya no apaga el caso si el correo lo contradice (Enel negó), llegó
// después de la decisión, o el SI no tiene soporte adjunto.
const esAccionable = (m) => (m.accionable ?? (!m.periodo_cerrado && !m.ya_diligenciado))
const fechaCorta = (iso) => (iso ? `${iso.slice(8, 10)}/${iso.slice(5, 7)} ${iso.slice(11, 16)}` : '')
const MESES = ['', 'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
// '2026-09' → 'septiembre 2026'
const mesLargo = (p) => (p && /^\d{4}-\d{2}$/.test(p)) ? `${MESES[Number(p.slice(5, 7))]} ${p.slice(0, 4)}` : (p || '')

// (el resaltado en amarillo de las citas vive en CorreoCitado.jsx: lo comparte «Ver justificación»)

// `periodo`: el del selector de la página — la bandeja solo trae los correos de casos de ese
// mes (o sin caso, por su fecha); coherente con la data y con Auditoría IA (2026-09-07).
export default function CorreosAnsTab({ apiBase, periodo, onVolverTabla = null }) {
  const qc = useQueryClient()
  // El correo puede venir en la URL (?correo=31): «ir al auditor» desde la celda del PDL de la
  // tabla (2026-10-01). `desde=tabla` pinta el botón de vuelta.
  const [sel, setSel] = useState(() => {
    const p = new URLSearchParams(window.location.search).get('correo')
    return p ? Number(p) : null
  })
  const [desdeTabla] = useState(() => new URLSearchParams(window.location.search).get('desde') === 'tabla')
  const [razonEdit, setRazonEdit] = useState({})     // observación editada por caso: {reg_id: texto}

  const { data: lista = [] } = useQuery({
    queryKey: ['correos-ans', apiBase, periodo],
    queryFn: async () => (await api.get('/correos', { baseURL: apiBase, params: periodo ? { periodo } : {} })).data,
    refetchInterval: 60000,     // el agente local sube cada 15 min; esto refresca solo
  })

  const { data: det } = useQuery({
    queryKey: ['correos-ans-det', apiBase, sel],
    queryFn: async () => (await api.get(`/correos/${sel}`, { baseURL: apiBase })).data,
    enabled: !!sel,
    // Mientras una lectura IA esté en curso (slot «pendiente»), se refresca solo cada 8 s
    refetchInterval: (q) => {
      const an = q.state.data?.analisis
      const slots = an ? (an.reg_id ? [an] : Object.values(an)) : []
      return slots.some((a) => a?.pendiente) ? 8000 : false
    },
  })

  const inval = () => {
    qc.invalidateQueries({ queryKey: ['correos-ans'] })
    qc.invalidateQueries({ queryKey: ['correos-ans-det'] })
  }

  const aplicar = useMutation({
    mutationFn: async (m) => (await api.post(`/correos/${sel}/aplicar`,
      { reg_id: m.reg_id, campo: m.campo }, { baseURL: apiBase })).data,
    onSuccess: () => { toast.success('Correo adjuntado a la celda como evidencia'); inval() },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo adjuntar'),
  })

  const analizar = useMutation({
    mutationFn: async (m) => (await api.post(`/correos/${sel}/analizar`,
      { reg_id: m.reg_id, campo: m.campo }, { baseURL: apiBase })).data,
    onSuccess: () => { toast.success('Análisis IA guardado'); inval() },
    onError: (e) => toast.error(e.response?.data?.detail || 'La IA no está disponible'),
  })

  const descartar = useMutation({
    mutationFn: async () => (await api.post(`/correos/${sel}/descartar`, {}, { baseURL: apiBase })).data,
    onSuccess: (d) => {
      toast(d.espera?.length
        ? `Descartado, pero no perdido: nombra ${d.espera.join(', ')}, que aún no está en la tabla. Si esa orden entra en una STM, el correo vuelve solo a la bandeja.`
        : 'Descartado. No es definitivo: se vuelve a cruzar en cada carga de STM.', { duration: 8000 })
      inval()
    },
  })
  // Descartar no es definitivo: se puede devolver a la bandeja a mano
  const restaurar = useMutation({
    mutationFn: async () => (await api.post(`/correos/${sel}/restaurar`, {}, { baseURL: apiBase })).data,
    onSuccess: () => { toast.success('Correo devuelto a la bandeja y re-cruzado'); inval() },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo restaurar'),
  })
  // «Ya lo revisé: mantengo mi decisión» (correo que llegó después de decidir o que la contradice)
  const mantener = useMutation({
    mutationFn: async (m) => (await api.post(`/correos/${sel}/mantener`,
      { reg_id: m.reg_id, campo: m.campo }, { baseURL: apiBase })).data,
    onSuccess: (d) => { toast.success(`Queda constancia: revisaste el correo y se mantiene ${d.decision || 'la celda'}.`, { duration: 6000 }); qc.invalidateQueries(); inval() },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo guardar'),
  })

  // Aprobación final: escribe la observación en la celda y, si viene decision, también el
  // SI/NO de la exclusión (3.2 A: celda AL; 3.2 B: AO + el SE EJECUTÓ, regla del flujo guiado).
  // Si la persona EDITÓ el texto, va tal cual (razon_editada): reemplaza la celda. La fusión
  // «se conserva y se añade» queda solo para la redacción de la IA sin tocar (requisito 2026-09-08).
  const razonEditada = (an) => razonEdit[an.reg_id] !== undefined && razonEdit[an.reg_id].trim() !== (an.razon || '').trim()
  const aprobar = useMutation({
    mutationFn: async ({ an, decision }) => (await api.post(`/correos/${sel}/aprobar`,
      { reg_id: an.reg_id, campo: an.campo, decision,
        razon: razonEdit[an.reg_id] ?? an.razon ?? undefined,
        razon_editada: razonEditada(an) }, { baseURL: apiBase })).data,
    onSuccess: (d) => {
      const obs = d.modo_obs === 'combinada' ? ' Observación combinada: se conservó la redacción existente y se añadió el correo.'
        : d.modo_obs === 'sin_cambio' ? ' La observación ya incluía este correo — sin cambios.'
        : d.modo_obs === 'manual' ? ' Tu texto quedó tal cual en la celda.' : ''
      // La caja muestra lo que quedó escrito, y la data (grid) se refresca sola.
      setRazonEdit((prev) => ({ ...prev, [d.reg_id]: d.razon }))
      qc.invalidateQueries()
      const evid = d.evidencia_nueva ? ' Correo adjuntado en Evidencias.' : (d.evidencia_id ? ' El correo ya estaba en Evidencias.' : '')
      toast.success((d.decision
        ? `Escrito en el ANS: excluible ${d.decision}${d.se_ejecuto ? ` (SE EJECUTÓ → ${d.se_ejecuto})` : ''}.`
        : 'Observación actualizada en la celda.') + evid + obs, { duration: 7000 })
      inval()
    },
    onError: (e) => toast.error(e.response?.data?.detail || 'No se pudo aplicar'),
  })

  const descargarMsg = async () => {
    try {
      const r = await api.get(`/correos/${sel}/descargar`, { baseURL: apiBase, responseType: 'blob' })
      const url = URL.createObjectURL(r.data)
      const a = document.createElement('a')
      a.href = url; a.download = `${(det?.asunto || 'correo').slice(0, 60)}.msg`; a.click()
      URL.revokeObjectURL(url)
    } catch { toast.error('No se pudo descargar el .msg') }
  }

  // ── Detalle ─────────────────────────────────────────────────────────────────
  // Latido real del agente (lo reporta él mismo tras cada pasada, en UTC).
  // OJO: TODOS los hooks van ANTES del return del detalle — ponerlo después causó el
  // React #300 («fewer hooks than expected»: el early return se saltaba este hook).
  const { data: agente } = useQuery({
    queryKey: ['correos-agente', apiBase],
    queryFn: async () => (await api.get('/correos/agente/estado', { baseURL: apiBase })).data,
    refetchInterval: 60000,
  })

  if (sel && det) {
    const matches = det.pdls || []
    const conCaso = matches.filter((m) => m.reg_id)
    const sinCaso = matches.filter((m) => !m.reg_id)
    // AL GRANO: se trabaja sobre los casos que siguen ABIERTOS (periodo competente o el
    // anterior, sin exclusión en SI); periodo cerrado o ya excluido = referencia.
    // Requisito 2026-09-08: la información que pueda salvar un caso se analiza aunque llegue con
    // la STM del periodo siguiente; «otro periodo» no apaga el caso.
    const accionables = conCaso.filter(esAccionable)
    const referencia = conCaso.filter((m) => !esAccionable(m))
    const resp = det.respuesta_enel            // {postura: NIEGA|ACEPTA, frase literal, de} — determinista
    const revivido = (det.meta?.revivido || []).slice(-1)[0]
    const hayContradiccion = accionables.some((m) => m.contradice)
    // analisis: mapa {reg_id: análisis} (un slot POR caso); formato viejo = dict plano.
    // AL GRANO: solo se muestran las lecturas de casos del PERIODO COMPETENTE — la de un
    // caso de otro periodo confunde (aclaración 2026-09-08).
    const anRaw = det.analisis
    const anMap = anRaw ? (anRaw.reg_id ? { [anRaw.reg_id]: anRaw } : anRaw) : {}
    const analisisList = Object.values(anMap).filter((a) => {
      if (!a || !a.reg_id) return false
      return accionables.some((x) => x.reg_id === a.reg_id)
    })
    // Citas de las lecturas visibles → resaltado amarillo sobre el cuerpo, junto con las líneas
    // que nombran el PDL de los casos accionables (requisito 2026-09-11)
    const pdlCasos = codigosCaso(...accionables.map((m) => m.pdl))
    const citas = [...fragmentosPdl(det.cuerpo, pdlCasos), ...analisisList.flatMap((a) => a.citas || [])]
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <button onClick={() => { setSel(null); setRazonEdit({}) }}
            className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-800">
            <ChevronLeft size={15} /> Volver a la bandeja
          </button>
          {desdeTabla && onVolverTabla && (
            <button onClick={onVolverTabla}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-blue-600 text-white rounded-lg hover:bg-blue-700">
              <ChevronLeft size={15} /> Volver a la tabla
            </button>
          )}
        </div>

        <div className="bg-white border rounded-xl p-4 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <Mail size={17} className="text-sky-600" />
            <h3 className="font-semibold">{det.asunto || '(sin asunto)'}</h3>
            <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${ESTADO[det.estado] || ''}`}>{ESTADO_LABEL[det.estado] || det.estado}</span>
          </div>
          <p className="text-xs text-gray-500">De: <b>{det.remitente || 's/d'}</b> · {det.fecha_correo || 's/d'}</p>
          <button onClick={descargarMsg} className="flex items-center gap-1.5 text-xs text-sky-700 hover:underline">
            <Download size={13} /> Descargar el .msg original
          </button>
          {det.estado === 'nuevo' && revivido && revivido.motivo !== 'restaurado a mano' && (
            <p className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-1.5 mt-1">
              <b>Volvió solo a la bandeja</b> el {fechaCorta(revivido.cuando)}: {revivido.motivo}.
              {revivido.antes === 'descartado' && ' Estaba descartado.'}
            </p>
          )}
          {det.estado === 'descartado' && (
            <div className="flex flex-wrap items-center gap-3 text-xs text-gray-600 bg-gray-50 border rounded-lg px-3 py-1.5 mt-1">
              <span><b>Descartado{det.meta?.descarte?.cuando ? ` el ${fechaCorta(det.meta.descarte.cuando)}` : ''}.</b> No es definitivo:
                se vuelve a cruzar en cada carga de STM, y si aparece una orden que entonces no estaba, regresa solo.</span>
              <button onClick={() => restaurar.mutate()} disabled={restaurar.isPending}
                className="flex items-center gap-1 px-2 py-1 bg-white border rounded-lg text-gray-700 hover:bg-gray-100 disabled:opacity-50">
                <RotateCcw size={12} /> Devolver a la bandeja
              </button>
            </div>
          )}
        </div>

        {/* LA RESPUESTA DE ENEL (2026-10-02, R09020): detectada con reglas, con su frase literal.
            Si niega y la celda está en SI, es una contradicción que hay que resolver aquí. */}
        {resp && (
          <div className={`border rounded-xl p-4 space-y-1 ${resp.postura === 'NIEGA' ? 'bg-red-50 border-red-300' : 'bg-emerald-50 border-emerald-300'}`}>
            <p className={`text-sm font-semibold flex items-center gap-1.5 ${resp.postura === 'NIEGA' ? 'text-red-800' : 'text-emerald-800'}`}>
              <ShieldAlert size={16} />
              Enel {resp.postura === 'NIEGA' ? 'NIEGA' : 'ACEPTA'} la exclusión
              {hayContradiccion && <span className="px-2 py-0.5 rounded-full text-[11px] bg-red-600 text-white">la celda está en SI · hay que reconsiderar</span>}
            </p>
            <p className="text-sm text-gray-800">«{resp.frase}»</p>
            <p className="text-[11px] text-gray-500">Frase literal del mensaje más reciente{resp.de ? ` (${resp.de})` : ''}. La detecta el sistema con reglas, no la IA.</p>
          </div>
        )}

        {/* Los casos a los que sirve */}
        <div className="bg-white border rounded-xl p-4 space-y-3">
          {/* UN CORREO, UNA DECISIÓN (aclaración 2026-09-03): aquí solo está el caso
              al que sirve ESTE correo. Nada de saltar a otros correos del mismo caso ni de
              historiales — eso confundía. Lo de otro periodo o ya diligenciado es una línea
              de referencia, sin botones. */}
          <h4 className="text-sm font-semibold">Caso al que sirve este correo</h4>
          {!matches.length && <p className="text-sm text-gray-500">No se detectó ningún código PDL en asunto ni cuerpo.</p>}
          {accionables.map((m) => (
            <div key={m.pdl} className="border rounded-lg p-3 space-y-2">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="font-mono text-xs font-bold">{m.pdl}</span>
                <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${m.tipo === '3.2B' ? 'bg-violet-100 text-violet-700' : 'bg-sky-100 text-sky-700'}`}>{m.tipo}</span>
                <span className="text-xs text-gray-500">{m.zona} · {m.fecha}</span>
                {m.fuente === 'asunto' && <span className="px-1.5 py-0.5 rounded text-[10px] bg-sky-50 text-sky-700">en el asunto</span>}
                {m.decision_previa === 'NO' && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] bg-amber-100 text-amber-800"
                    title="La celda ya se decidió en NO; este correo llegó después y puede cambiarla — por eso no se descarta">
                    hoy en NO · este correo puede cambiarlo
                  </span>
                )}
                {m.favorece && <span className="px-1.5 py-0.5 rounded text-[10px] bg-emerald-600 text-white">Enel aceptó la exclusión</span>}
                {m.contradice && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] bg-red-600 text-white"
                    title="Aprobaste SI y después Enel respondió que la exclusión no aplica">
                    hoy en SI{m.decidido_en ? ` (desde ${fechaCorta(m.decidido_en)})` : ''} · Enel la negó después
                  </span>
                )}
                {m.novedad && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] bg-amber-100 text-amber-800"
                    title="El correo llegó después de que decidiste SI: léelo, puede cambiar la decisión">
                    hoy en SI{m.decidido_en ? ` (desde ${fechaCorta(m.decidido_en)})` : ''} · este correo llegó después
                  </span>
                )}
                {m.si_sin_soporte && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] bg-amber-100 text-amber-800"
                    title="La celda está en SI pero no tiene ningún soporte adjunto: aprobar con este correo lo deja en Evidencias">
                    hoy en SI sin soporte adjunto · este correo puede serlo
                  </span>
                )}
                {m.otro_periodo && !m.periodo_cerrado && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] bg-orange-100 text-orange-800"
                    title="El caso es del periodo anterior y sigue abierto: este correo puede salvarlo, por eso se analiza igual">
                    periodo anterior: {mesLargo(m.periodo)} · sigue abierto
                  </span>
                )}
                {det.estado === 'vinculado' && <span className="px-1.5 py-0.5 rounded text-[10px] bg-emerald-100 text-emerald-800">adjuntado ✓</span>}
                {anMap[m.reg_id]?.pendiente
                  ? <span className="px-1.5 py-0.5 rounded text-[10px] bg-violet-100 text-violet-800 flex items-center gap-1"><Loader2 size={10} className="animate-spin" /> lectura IA en curso…</span>
                  : (anMap[m.reg_id] && !anMap[m.reg_id].error && <span className="px-1.5 py-0.5 rounded text-[10px] bg-violet-100 text-violet-800">lectura IA lista ↓</span>)}
              </div>
              {det.estado !== 'descartado' && (
                <div className="flex flex-wrap items-center gap-3">
                  {/* Camino único: la lectura IA de abajo trae la propuesta y los botones de aprobar;
                      aprobar escribe la decisión + observación y ADJUNTA el correo (sin pasos extra). */}
                  {anMap[m.reg_id] && !anMap[m.reg_id].pendiente && !anMap[m.reg_id].error
                    ? <span className="text-xs text-violet-800">Lee la propuesta de abajo y aprueba: eso escribe la decisión y deja el correo en Evidencias.</span>
                    : (
                      <button onClick={() => analizar.mutate(m)} disabled={analizar.isPending || anMap[m.reg_id]?.pendiente}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-violet-600 text-white rounded-lg hover:bg-violet-700 disabled:opacity-50">
                        {(analizar.isPending || anMap[m.reg_id]?.pendiente) ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
                        {anMap[m.reg_id]?.pendiente ? 'Leyendo el correo…' : (anMap[m.reg_id]?.error ? 'Reintentar la lectura IA' : 'Leer con IA')}
                      </button>
                    )}
                  {det.estado === 'nuevo' && (
                    <button onClick={() => aplicar.mutate(m)} disabled={aplicar.isPending}
                      title="Solo copia el correo a la celda como soporte, sin decidir SI/NO (la decisión queda pendiente)"
                      className="text-xs text-gray-500 hover:text-gray-800 underline decoration-dotted underline-offset-2 disabled:opacity-50">
                      {aplicar.isPending ? 'Adjuntando…' : 'Solo adjuntar sin decidir'}
                    </button>
                  )}
                </div>
              )}
            </div>
          ))}
          {!accionables.length && matches.length > 0 && (
            <p className="text-sm text-gray-500">
              Este correo no tiene ningún caso abierto ({((agente?.periodos_abiertos || [agente?.periodo_competente]).filter(Boolean).map(mesLargo).join(' ni ')) || 'periodo de trabajo'}) — queda como referencia.
              {sinCaso.some((m) => m.espera_stm) && <> Sus PDL aún no están en la tabla (STM de <b>{mesLargo(sinCaso.find((m) => m.espera_stm).espera_stm)}</b>): en cada carga se re-cruzan solos, y si lo descartas vuelve a la bandeja cuando entre la orden.</>}
            </p>
          )}
          {(referencia.length > 0 || sinCaso.length > 0) && (
            <p className="text-xs text-gray-500 flex items-start gap-1.5">
              <AlertTriangle size={13} className="text-amber-500 shrink-0 mt-0.5" />
              <span>
                Otros PDL mencionados en el correo (solo referencia, sin acción):{' '}
                {referencia.map((m) => `${m.pdl} (${m.periodo_cerrado ? `periodo cerrado: ${mesLargo(m.periodo)}` : m.revisado ? 'en SI · ya revisaste este correo' : 'ya excluido en SI'})`)
                  .concat(sinCaso.map((m) => `${m.pdl} (${m.stm_parcial ? 'aún no está en la tabla: se re-cruza en la próxima carga de STM'
                    : m.espera_stm ? `espera la STM de ${mesLargo(m.espera_stm)}` : 'sin caso en la tabla'})`)).join(' · ')}
              </span>
            </p>
          )}
        </div>

        {/* Análisis IA + aprobación final — una caja POR caso analizado */}
        {analisisList.map((an) => an.pendiente ? (
          <div key={an.reg_id} className="bg-violet-50 border border-violet-200 rounded-xl p-4 text-sm flex items-start gap-2">
            <Loader2 size={16} className="animate-spin text-violet-600 shrink-0 mt-0.5" />
            <div>
              <p className="font-semibold">Leyendo el correo contra {an.pdl} ({an.campo === 'excluible' ? '3.2 A' : '3.2 B'})…</p>
              <p className="text-xs text-violet-800">Este caso no tenía lectura (el correo entró cuando el caso no estaba en el periodo de trabajo). La máquina la está haciendo ahora — tarda alrededor de un minuto y esta pantalla se refresca sola. Aquí aparecerá el razonamiento, las citas en amarillo y los botones de aprobar.</p>
            </div>
          </div>
        ) : (
          <div key={an.reg_id} className="bg-violet-50 border border-violet-200 rounded-xl p-4 space-y-2 text-sm">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="font-semibold">Lectura IA vs {an.pdl} ({an.campo === 'excluible' ? '3.2 A' : '3.2 B'}){an.analista === ANALISTA_SESION_ASISTIDA && <span className="ml-2 px-1.5 py-0.5 rounded text-[10px] bg-emerald-100 text-emerald-800 font-normal">lectura hecha en sesión asistida</span>}</p>
              {det.estado !== 'descartado' && (
                <button onClick={() => analizar.mutate({ reg_id: an.reg_id, campo: an.campo })} disabled={analizar.isPending}
                  className="text-[11px] text-violet-700 hover:underline disabled:opacity-50">
                  {analizar.isPending ? 'Leyendo…' : 'Volver a leer con IA'}
                </button>
              )}
            </div>
            {an.error
              ? <p className="text-red-600">{an.error} — usa «Re-analizar con IA» arriba.</p>
              : (<>
                <p>Relacionado con la orden: <b>{an.relacionado}</b>{' · '}
                  {an.revision
                    ? <span className="px-1.5 py-0.5 rounded font-bold bg-orange-200 text-orange-900">REVISIÓN · potencialmente excluible</span>
                    : <>Propone excluible: <b>{an.excluible ?? '—'}</b></>}
                  {an.categoria ? ` · ${an.categoria}` : ''}</p>
                {an.causa && <p className="text-xs text-gray-700"><b>Causa:</b> {an.causa}{an.causa_tipo ? ` (${an.causa_tipo})` : ''}</p>}
                {an.revision && (
                  <p className="text-xs text-orange-800 bg-orange-50 border border-orange-200 rounded-lg px-3 py-2">
                    <b>Por qué está en Revisión:</b> {an.revision.motivo}. Posición provisional: no exclusión
                    hasta completar la evidencia — el SI/NO definitivo lo marca el ING.
                  </p>
                )}
                <CoberturaRapida cov={an.cobertura_calc} />
                {an.hilo_wa_msgs > 0 && (
                  <p className="text-xs text-cyan-800">
                    Lectura con convergencia: incluyó la conversación de WhatsApp del caso
                    ({an.hilo_wa_msgs} mensajes) — la coherencia chat↔correo va en el razonamiento.
                  </p>
                )}
                {an.resumen && <p className="text-xs text-gray-600">{an.resumen}</p>}
                {(an.validacion || []).length > 0 && (
                  <p className="text-xs text-amber-900 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                    <b>El sistema corrigió a la IA:</b> {an.validacion.join(' · ')}
                  </p>
                )}

                {/* Fusión anti-pisadas: si el caso ya tiene observación rica (WhatsApp
                    o manual), se avisa — al aprobar se CONSERVA y el correo se añade */}
                {(() => {
                  const mm = matches.find((x) => x.reg_id === an.reg_id)
                  return mm?.obs_previa ? (
                    <div className="bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2 text-xs text-emerald-900 space-y-1">
                      <p><b>Este caso ya tiene observación {mm.obs_previa.origen === 'whatsapp' ? 'del analista de WhatsApp' : 'en la celda'}</b> — si apruebas la redacción de la IA sin tocarla, se conserva y se le añade lo del correo. Si editas el texto de abajo, lo tuyo reemplaza la celda completa.</p>
                      <p className="text-emerald-800/80 font-mono text-[11px] whitespace-pre-wrap">{mm.obs_previa.texto}</p>
                    </div>
                  ) : null
                })()}
                <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                  <p className="text-xs font-semibold text-gray-600">
                    {razonEditada(an)
                      ? <span className="text-emerald-700">Observación editada por ti — se escribe TAL CUAL en la celda (reemplaza lo que hay):</span>
                      : matches.find((x) => x.reg_id === an.reg_id)?.obs_previa
                        ? 'Aporte del correo (se añadirá a la observación existente; si lo editas, reemplaza):'
                        : 'Observación para la celda (edítala si hace falta):'}
                  </p>
                  <button onClick={() => setRazonEdit((prev) => ({ ...prev, [an.reg_id]: depurarObs(razonEdit[an.reg_id] ?? an.razon ?? '') }))}
                    title="Quita de la observación lo que es proceso interno — la celda solo habla de este caso"
                    className="px-2 py-0.5 text-[11px] font-medium bg-white border text-violet-700 rounded-lg hover:bg-violet-50">
                    Actualizar observación — quitar lo interno
                  </button>
                </div>
                <textarea value={razonEdit[an.reg_id] ?? an.razon ?? ''}
                  onChange={(e) => setRazonEdit((prev) => ({ ...prev, [an.reg_id]: e.target.value }))}
                  rows={3} className="w-full border rounded-lg p-2 text-sm bg-white" />
                {(() => {
                  // RECONSIDERAR (2026-10-02): la celda ya está en SI y este correo llegó después o la
                  // contradice. Los botones dicen lo que pasa: corregir a NO, o dejar constancia de
                  // que se revisó y se mantiene. Nada de un «Aprobar SI» que no cambia nada.
                  const mm = matches.find((x) => x.reg_id === an.reg_id) || {}
                  const enSi = mm.contradice || mm.novedad
                  return (<>
                    {enSi && (
                      <p className={`text-xs rounded-lg px-3 py-2 border ${mm.contradice ? 'bg-red-50 border-red-300 text-red-900' : 'bg-amber-50 border-amber-200 text-amber-900'}`}>
                        <b>{mm.contradice ? 'Reconsiderar la exclusión.' : 'Este correo llegó después de tu decisión.'}</b>{' '}
                        La celda está en <b>SI</b>{mm.decidido_en ? ` desde el ${fechaCorta(mm.decidido_en)}` : ''}.{' '}
                        {mm.contradice
                          ? 'Enel respondió por escrito que la exclusión no aplica: lo coherente es corregir a NO. Al hacerlo, este correo queda en Evidencias como soporte del NO.'
                          : 'Léelo: si cambia el caso, corrige; si no, deja constancia de que lo revisaste.'}
                      </p>
                    )}
                    {mm.si_sin_soporte && !enSi && (
                      <p className="text-xs rounded-lg px-3 py-2 border bg-amber-50 border-amber-200 text-amber-900">
                        <b>La celda ya está en SI, pero sin soporte adjunto.</b> Si este correo es el soporte, «Aprobar: excluible SI» lo deja en Evidencias y escribe la observación.
                      </p>
                    )}
                    <div className="flex flex-wrap gap-2 pt-1">
                      {/* SI/NO en los DOS indicadores (requisito 2026-09-08: los botones de sí o no van aquí
                          mismo). La decisión sigue siendo de la persona; la IA solo
                          propone; el clic escribe la celda, la observación y adjunta el correo. */}
                      {/* Con Enel negando por escrito no se ofrece «SI»: o se corrige, o se mantiene a conciencia */}
                      {!mm.contradice && (
                        <button onClick={() => aprobar.mutate({ an, decision: 'SI' })} disabled={aprobar.isPending}
                          className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50">
                          {aprobar.isPending ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}
                          {mm.novedad ? 'Confirmar el SI y adjuntar este correo'
                            : mm.si_sin_soporte ? 'Aprobar: excluible SI (adjunta este correo)' : 'Aprobar: excluible SI'}
                        </button>
                      )}
                      <button onClick={() => aprobar.mutate({ an, decision: 'NO' })} disabled={aprobar.isPending}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50">
                        <XCircle size={14} /> {enSi ? 'Corregir: NO excluible' : 'Aprobar: NO excluible'}
                      </button>
                      {(enSi || mm.si_sin_soporte) && (
                        <button onClick={() => mantener.mutate(mm)} disabled={mantener.isPending}
                          title="No cambia la celda: deja constancia en el historial de que revisaste este correo y mantienes el SI"
                          className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-white border border-emerald-600 text-emerald-700 rounded-lg hover:bg-emerald-50 disabled:opacity-50">
                          <CheckCircle2 size={14} /> Ya lo revisé: mantener el SI
                        </button>
                      )}
                      <button onClick={() => aprobar.mutate({ an, decision: null })} disabled={aprobar.isPending}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-white border text-gray-700 rounded-lg hover:bg-gray-50 disabled:opacity-50">
                        Solo actualizar la observación
                      </button>
                    </div>
                  </>)
                })()}
                {an.campo === 'excluible' && (
                  <p className="text-[11px] text-violet-700">La decisión es tuya, pero se toma aquí: SI/NO escribe la exclusión de tiempos (celda AL) y la observación, y deja el correo en Evidencias (carpeta 3.2 A). «Solo actualizar la observación» no toca el SI/NO. Si el caso está en Revisión, decide con los tiempos a la vista.</p>
                )}
                {an.campo === 'excluible_anulado' && (
                  <p className="text-[11px] text-violet-700">Un solo clic hace todo: escribe la exclusión, la observación y el SE EJECUTÓ (N/A si se excluye, NO si no) y deja el correo en Evidencias (carpeta 3.2 B). Nada más que hacer después.</p>
                )}
              </>)}
          </div>
        ))}

        {accionables.some((m) => !anMap[m.reg_id]) && det.estado !== 'descartado' && (
          <p className="text-xs text-violet-800 bg-violet-50 border border-violet-200 rounded-xl px-4 py-2">
            Todavía no hay lectura IA para {accionables.filter((m) => !anMap[m.reg_id]).map((m) => m.pdl).join(', ')} — pulsa «Analizar con IA» arriba y en un minuto verás aquí el razonamiento y la propuesta de exclusión.
          </p>
        )}

        {/* Cuerpo. Si el .msg no se pudo leer (2026-09-23), el tab lo dice de frente en vez de
            mostrar el error crudo entre el texto: qué pasó, qué significa y cómo verlo igual. */}
        {(det.cuerpo || '').startsWith('(no se pudo parsear') ? (
          <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 space-y-2">
            <p className="text-sm font-semibold text-amber-900 flex items-center gap-1.5">
              <AlertTriangle size={15} /> El archivo .msg de este correo no se pudo leer
            </p>
            <p className="text-xs text-amber-800">
              El correo sí está guardado, pero su texto no se pudo extraer, así que tampoco se
              buscaron los PDL dentro. Descarga el .msg original arriba para verlo en Outlook.
              Al recargar esta página el sistema vuelve a intentarlo con el lector tolerante.
            </p>
            <p className="text-[11px] font-mono text-amber-700 break-all">{det.cuerpo}</p>
          </div>
        ) : (
          <details open className="bg-white border rounded-xl p-4">
            <summary className="text-sm font-semibold cursor-pointer">
              Cuerpo del correo (texto limpio)
              {citas.length > 0 && <span className="ml-2 text-xs font-normal text-amber-700">— en amarillo, dónde se nombra el PDL y lo que sustenta la decisión de la máquina</span>}
            </summary>
            <pre className="mt-2 text-xs text-gray-700 whitespace-pre-wrap font-sans max-h-96 overflow-y-auto">{resaltarCitas(det.cuerpo || '(vacío)', citas, pdlCasos)}</pre>
          </details>
        )}

        {det.estado === 'nuevo' && (
          <button onClick={() => descartar.mutate()}
            className="flex items-center gap-2 px-4 py-2 text-sm bg-white border text-gray-600 rounded-lg hover:bg-gray-50">
            <XCircle size={15} /> Descartar (no aplica al ANS)
            <span className="text-[11px] text-gray-400">— no es definitivo: vuelve solo si aparece su caso</span>
          </button>
        )}
      </div>
    )
  }

  // ── Bandeja ─────────────────────────────────────────────────────────────────
  const ultima = agente?.ultima_pasada ? new Date(agente.ultima_pasada) : null
  const haceMin = ultima ? Math.max(0, Math.round((Date.now() - ultima.getTime()) / 60000)) : null
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button onClick={async () => {
          try {
            const r = await api.post('/correos/recruzar', {}, { baseURL: apiBase })
            inval()
            const v = r.data.revividos || []
            toast.success(`Bandeja actualizada — ${r.data.recruzados} correo(s) re-cruzados contra el ANS`
              + (v.length ? `. ${v.length} volvieron a la bandeja: ${v.map((x) => x.motivo).join('; ')}` : ''), { duration: v.length ? 10000 : 4000 })
          } catch { toast.error('No se pudo actualizar') }
        }} className="flex items-center gap-2 px-3 py-2 text-sm bg-sky-600 text-white rounded-lg hover:bg-sky-700">
          <RefreshCw size={15} /> Actualizar
        </button>
        <p className="text-xs text-gray-500">
          El agente de tu PC revisa Outlook <b>cada 5 min</b> — un correo recién etiquetado entra en la próxima pasada
          {haceMin !== null && <> · última pasada hace <b>{haceMin < 60 ? `${haceMin} min` : `${Math.round(haceMin / 60)} h`}</b></>}.
          Actualizar re-verifica los cruces PDL→caso de <b>todos</b> los correos (también los descartados) con la tabla de hoy; lo mismo pasa solo al cargar una STM y al decidir una celda.
        </p>
        {agente?.periodo_competente && (
          <p className="w-full text-xs text-gray-600 flex items-start gap-1.5">
            <AlertTriangle size={13} className={`${agente.stm_pendiente ? 'text-amber-500' : 'text-emerald-500'} shrink-0 mt-0.5`} />
            <span>
              Periodo de trabajo: <b>{mesLargo(agente.periodo_competente)}</b> (última STM cargada).
              {agente.stm_pendiente
                ? <> Falta la STM de <b>{mesLargo(agente.stm_pendiente)}</b>: los correos siguen sirviendo a los casos de {mesLargo(agente.periodo_competente)}, y los PDL que aún no estén en la tabla quedan <b>en espera</b> (no se descartan) — al cargar esa STM se re-cruzan solos.</>
                : <> La tabla está al día con el calendario.</>}
            </span>
          </p>
        )}
      </div>
      {(() => {
        // Lo urgente primero (2026-10-02): correos sin revisar que contradicen una decisión en SI
        const urg = lista.filter((c) => c.estado === 'nuevo' && (c.pdls || []).some((m) => m.contradice))
        return urg.length > 0 ? (
          <div className="bg-red-50 border border-red-300 rounded-xl px-4 py-3 space-y-1.5">
            <p className="text-sm font-semibold text-red-800 flex items-center gap-1.5">
              <ShieldAlert size={16} /> {urg.length === 1 ? 'Un correo contradice' : `${urg.length} correos contradicen`} una decisión que ya tomaste
            </p>
            {urg.map((c) => (
              <button key={c.id} onClick={() => { setSel(c.id); setRazonEdit({}) }}
                className="block w-full text-left text-xs text-red-900 hover:underline">
                {(c.pdls || []).filter((m) => m.contradice).map((m) => m.pdl).join(', ')} está en SI, y Enel negó la exclusión
                {c.respuesta_enel?.frase ? `: «${c.respuesta_enel.frase.slice(0, 140)}${c.respuesta_enel.frase.length > 140 ? '…' : ''}»` : ''} — abrir →
              </button>
            ))}
          </div>
        ) : null
      })()}
      <div className="bg-white border rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-xs text-gray-500 uppercase">
            <tr>
              <th className="px-3 py-2 text-left">Fecha</th><th className="px-3 py-2 text-left">De</th>
              <th className="px-3 py-2 text-left">Asunto</th><th className="px-3 py-2 text-left">PDL → caso</th>
              <th className="px-3 py-2 text-left">Estado</th>
            </tr>
          </thead>
          <tbody>
            {lista.map((c) => {
              const conCaso = (c.pdls || []).filter((m) => m.reg_id)
              const sinCaso = (c.pdls || []).filter((m) => !m.reg_id)
              return (
                <tr key={c.id} onClick={() => { setSel(c.id); setRazonEdit({}) }} className="border-t hover:bg-sky-50 cursor-pointer">
                  <td className="px-3 py-2 text-xs whitespace-nowrap">{(c.fecha_correo || '').slice(0, 16)}</td>
                  <td className="px-3 py-2 text-xs">{c.remitente}</td>
                  <td className="px-3 py-2">{(c.asunto || '(sin asunto)').slice(0, 70)}</td>
                  <td className="px-3 py-2">
                    {conCaso.map((m) => {
                      // Chip: gris = sin acción (ya diligenciado / periodo cerrado — solo
                      // referencia); verde = caso abierto al que sirve el correo; ámbar = hoy NO.
                      const apagado = !esAccionable(m)
                      const previaNo = !apagado && m.decision_previa === 'NO'
                      const clase = apagado ? 'bg-gray-100 text-gray-400'
                        : m.contradice ? 'bg-red-600 text-white'
                          : (previaNo || m.novedad || m.si_sin_soporte) ? 'bg-amber-100 text-amber-800' : 'bg-emerald-100 text-emerald-800'
                      return (
                        <span key={m.pdl} className={`inline-block mr-1 mb-0.5 px-1.5 py-0.5 rounded text-[11px] font-mono ${clase}`}
                          title={apagado ? (m.ya_diligenciado ? 'ya excluido en SI — referencia' : `periodo cerrado (${mesLargo(m.periodo)}) — referencia`)
                            : (previaNo ? 'la celda está en NO; este correo llegó después y puede cambiarla' : undefined)}>
                          {m.pdl} <span className="font-sans">({m.tipo})</span>
                          {apagado && <span className="font-sans"> · ref</span>}
                          {previaNo && <span className="font-sans"> · hoy NO{m.favorece ? ' · Enel aceptó' : ''}</span>}
                          {m.contradice && <span className="font-sans"> · en SI · Enel la negó</span>}
                          {m.novedad && <span className="font-sans"> · en SI · llegó después</span>}
                          {m.si_sin_soporte && <span className="font-sans"> · en SI sin soporte</span>}
                          {!apagado && m.otro_periodo && <span className="font-sans"> · {mesLargo(m.periodo).slice(0, 3).toLowerCase()}</span>}
                        </span>
                      )
                    })}
                    {sinCaso.map((m) => (
                      <span key={m.pdl} className={`inline-block mr-1 mb-0.5 px-1.5 py-0.5 rounded text-[11px] font-mono ${m.espera_stm ? 'bg-amber-50 text-amber-700 border border-amber-200' : 'bg-gray-100 text-gray-500'}`}
                        title={m.espera_stm ? `aún no está en la tabla (STM de ${mesLargo(m.espera_stm)}): se re-cruza solo en cada carga` : 'sin caso en la tabla'}>
                        {m.pdl}{m.espera_stm && <span className="font-sans"> · espera STM</span>}
                      </span>
                    ))}
                    {!(c.pdls || []).length && <span className="text-xs text-gray-400">sin PDL detectado</span>}
                  </td>
                  <td className="px-3 py-2">
                    <span className={`px-2 py-0.5 rounded-full text-xs ${ESTADO[c.estado] || ''}`}>{ESTADO_LABEL[c.estado] || c.estado}</span>
                    {/* SIN ETIQUETA ANS (14 oct 2026): entró por la red sin etiqueta del 8 oct 2026; apartado, no vuelve solo */}
                    {c.etiqueta_ans === false && (
                      <span className="block mt-1 text-[10px] text-gray-500" title={`No lleva la categoría ANS de Outlook (categoría: ${c.categorias || 'ninguna'}): entró por la red sin etiqueta del 8 oct 2026. Si lo apartas, no vuelve solo a la bandeja.`}>sin etiqueta ANS</span>
                    )}
                    {c.estado === 'nuevo' && (c.meta?.revivido || []).length > 0 && (
                      <span className="block mt-1 text-[10px] text-amber-700" title={c.meta.revivido.slice(-1)[0].motivo}>volvió a la bandeja</span>
                    )}
                    {c.respuesta_enel && (
                      <span className={`block mt-1 text-[10px] font-medium ${c.respuesta_enel.postura === 'NIEGA' ? 'text-red-700' : 'text-emerald-700'}`}>
                        Enel {c.respuesta_enel.postura === 'NIEGA' ? 'negó' : 'aceptó'}
                      </span>
                    )}
                  </td>
                </tr>
              )
            })}
            {!lista.length && (
              <tr><td colSpan={5} className="px-3 py-8 text-center text-gray-400 text-sm">
                Aún no llegan correos — etiqueta correos con la categoría «ANS» en Outlook y el agente los sube solo (cada 15 min).
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
