// Festivos de Colombia (Ley 51 de 1983 — «Ley Emiliani»), calculados.
// Espejo de backend/app/services/festivos.py — mantener iguales.
// Día "no laborable" para la programación = domingo o festivo → se pinta ROJO
// en la columna FECHA para TODOS los roles (pedido del cliente, plantilla 2026-08).

const cache = new Map()

// Domingo de Pascua (algoritmo gregoriano anónimo). Devuelve [mes 1-12, día].
function pascua(anio) {
  const a = anio % 19
  const b = Math.floor(anio / 100), c = anio % 100
  const d = Math.floor(b / 4), e = b % 4
  const f = Math.floor((b + 8) / 25)
  const g = Math.floor((b - f + 1) / 3)
  const h = (19 * a + b - d - g + 15) % 30
  const i = Math.floor(c / 4), k = c % 4
  const l = (32 + 2 * e + 2 * i - h - k) % 7
  const m = Math.floor((a + 11 * h + 22 * l) / 451)
  const mes = Math.floor((h + l - 7 * m + 114) / 31)
  const dia = ((h + l - 7 * m + 114) % 31) + 1
  return Date.UTC(anio, mes - 1, dia)
}

const DIA_MS = 24 * 60 * 60 * 1000

// Traslado Emiliani: si no cae lunes, pasa al lunes siguiente.
function siguienteLunes(utcMs) {
  const dow = new Date(utcMs).getUTCDay()          // 0=domingo … 1=lunes
  return dow === 1 ? utcMs : utcMs + ((8 - dow) % 7 || 7) * DIA_MS
}

function festivosDelAnio(anio) {
  if (cache.has(anio)) return cache.get(anio)
  const p = pascua(anio)
  const fijos = [
    Date.UTC(anio, 0, 1),   // Año Nuevo
    Date.UTC(anio, 4, 1),   // Día del Trabajo
    Date.UTC(anio, 6, 20),  // Independencia
    Date.UTC(anio, 7, 7),   // Batalla de Boyacá
    Date.UTC(anio, 11, 8),  // Inmaculada
    Date.UTC(anio, 11, 25), // Navidad
    p - 3 * DIA_MS,         // Jueves Santo
    p - 2 * DIA_MS,         // Viernes Santo
  ]
  const trasladables = [
    Date.UTC(anio, 0, 6),   // Reyes
    Date.UTC(anio, 2, 19),  // San José
    Date.UTC(anio, 5, 29),  // San Pedro y San Pablo
    Date.UTC(anio, 7, 15),  // Asunción
    Date.UTC(anio, 9, 12),  // Día de la Raza
    Date.UTC(anio, 10, 1),  // Todos los Santos
    Date.UTC(anio, 10, 11), // Independencia de Cartagena
    p + 39 * DIA_MS,        // Ascensión
    p + 60 * DIA_MS,        // Corpus Christi
    p + 68 * DIA_MS,        // Sagrado Corazón
  ].map(siguienteLunes)
  const set = new Set([...fijos, ...trasladables].map((ms) => new Date(ms).toISOString().slice(0, 10)))
  cache.set(anio, set)
  return set
}

// fechaIso: "YYYY-MM-DD" (como viene del API). true → domingo o festivo.
export function esNoLaborable(fechaIso) {
  if (!fechaIso) return false
  const iso = String(fechaIso).slice(0, 10)
  const [y, m, d] = iso.split('-').map(Number)
  if (!y || !m || !d) return false
  if (new Date(Date.UTC(y, m - 1, d)).getUTCDay() === 0) return true   // domingo
  return festivosDelAnio(y).has(iso)
}
