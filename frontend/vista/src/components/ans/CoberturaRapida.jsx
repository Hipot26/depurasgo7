// «Análisis rápido» de tiempos — REGLA GENERAL del sistema (auditoría 2026-08-24): por
// cada gap (apertura/cierre), la ventana del desfase (plan → real) y encima los tramos
// que la evidencia justifica. Las sumas y la tolerancia (10 min por gap) las calcula el
// backend (wa_analista._calcular_cobertura) — aquí solo se pintan. Lo usan Auditoría IA
// (WhatsApp) y Correos ANS: la regla es del sistema, no de un canal.
import { Clock } from 'lucide-react'

function minutosDe(hhmm) {
  const m = /^\s*(\d{1,2}):(\d{2})/.exec(hhmm || '')
  return m ? +m[1] * 60 + +m[2] : null
}

function BarraGap({ nombre, g, tolerancia, legado }) {
  const vIni = minutosDe(g.ventana?.plan)
  let vFin = minutosDe(g.ventana?.real)
  if (vIni != null && vFin != null && vFin < vIni) vFin += 1440
  const tramos = (g.tramos || []).map((t) => {
    let i = minutosDe(t.desde), f = minutosDe(t.hasta)
    if (i != null && f != null && f < i) f += 1440
    return { ...t, i, f }
  }).filter((t) => t.i != null && t.f != null)
  const pts = [vIni, vFin, ...tramos.flatMap((t) => [t.i, t.f])].filter((x) => x != null)
  const ok = g.cubierto
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <b className="capitalize">{nombre}</b>
        <span className="text-xs text-gray-600">
          desfase <b>{g.requerido} min</b>{!legado && <> · justificado <b>{g.justificado} min</b></>}
        </span>
        {legado
          ? <span className="px-2 py-0.5 rounded-full text-xs font-bold bg-amber-100 text-amber-800">sin cálculo — análisis anterior a la regla de tramos</span>
          : ok
            ? <span className="px-2 py-0.5 rounded-full text-xs font-bold bg-emerald-100 text-emerald-800">cubre ✓ (tolerancia {tolerancia} min)</span>
            : <span className="px-2 py-0.5 rounded-full text-xs font-bold bg-red-100 text-red-700">no cubre — faltan {g.faltan} min</span>}
      </div>
      {pts.length > 0 && (() => {
        const min = Math.min(...pts) - 5, max = Math.max(...pts) + 5
        const pct = (x) => `${(((x - min) / (max - min)) * 100).toFixed(1)}%`
        const ancho = (a, b) => `${(((b - a) / (max - min)) * 100).toFixed(1)}%`
        return (
          <div className="relative h-9 bg-gray-50 border rounded-lg overflow-hidden">
            {vIni != null && vFin != null && (
              <div className="absolute top-1 h-3 bg-gray-300/80 border-x-2 border-gray-500"
                style={{ left: pct(vIni), width: ancho(vIni, vFin) }}
                title={`Ventana del desfase: ${g.ventana.plan} (plan) → ${g.ventana.real} (real)`} />
            )}
            {tramos.map((t, i2) => (
              <div key={i2} className={`absolute top-5 h-3 rounded ${ok ? 'bg-emerald-400' : 'bg-amber-400'}`}
                style={{ left: pct(t.i), width: ancho(t.i, t.f) }}
                title={`${t.desde}–${t.hasta} · ${t.minutos} min · ${t.evidencia}`} />
            ))}
          </div>
        )
      })()}
      <p className="text-[11px] text-gray-500">
        <span className="inline-block w-2.5 h-2.5 bg-gray-300 border border-gray-500 align-middle mr-1" />
        ventana {g.ventana?.plan || '?'} (plan) → {g.ventana?.real || '?'} (real)
        <span className={`inline-block w-2.5 h-2.5 align-middle ml-3 mr-1 rounded-sm ${ok ? 'bg-emerald-400' : 'bg-amber-400'}`} />
        tramos justificados por la evidencia
      </p>
      <ul className="text-xs text-gray-600 space-y-0.5">
        {(g.tramos || []).map((t, i2) => (
          <li key={i2}><span className="font-mono">{t.desde}–{t.hasta}</span> · {t.minutos} min · {t.evidencia}</li>
        ))}
        {!(g.tramos || []).length && (
          <li className="text-gray-400">
            {legado
              ? 'este análisis se hizo antes de la regla de tramos — el re-análisis del caso trae el cálculo completo'
              : 'sin tramos justificados para este gap'}
          </li>
        )}
        {(g.descartados || []).map((t, i2) => (
          <li key={`d${i2}`} className="text-amber-700 line-through decoration-amber-400">
            <span className="font-mono">{t.desde}–{t.hasta}</span> · {t.evidencia}
            <span className="no-underline ml-1 text-amber-800">— descartado: {t.motivo}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export default function CoberturaRapida({ cov }) {
  if (!cov?.aplicable) return null
  const legado = !!cov.legado
  return (
    <div className="bg-white border rounded-xl p-4 space-y-4">
      <h4 className="text-sm font-semibold flex items-center gap-1.5">
        <Clock size={15} /> Análisis rápido de tiempos
        <span className="text-xs font-normal text-gray-500">
          {legado
            ? '(análisis viejo: solo la ventana del desfase — el re-análisis trae los tramos)'
            : `(suma calculada por el sistema · tolerancia ${cov.tolerancia_min} min por gap)`}
        </span>
      </h4>
      {Object.entries(cov.gaps || {}).map(([nombre, g]) => (
        <BarraGap key={nombre} nombre={nombre} g={g} tolerancia={cov.tolerancia_min} legado={legado} />
      ))}
    </div>
  )
}
