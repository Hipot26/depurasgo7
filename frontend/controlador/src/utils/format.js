export const formatCOP = (value) => {
  if (!value && value !== 0) return '—'
  return new Intl.NumberFormat('es-CO', {
    style: 'currency',
    currency: 'COP',
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value)
}

export const formatNum = (value) => {
  if (!value && value !== 0) return '—'
  return new Intl.NumberFormat('es-CO').format(value)
}

export const ESTADO_COLORS = {
  EJECUTADO: 'badge-ejecutado',
  PARCIAL: 'badge-parcial',
  CANCELADO: 'badge-cancelado',
  PROGRAMADO: 'badge-programado',
  REPROGRAMADO: 'badge-reprogramado',
}

export const ZONA_COLORS = {
  ALMEIDAS: 'bg-purple-100 text-purple-800',
  GUAVIO: 'bg-orange-100 text-orange-800',
  UBATE: 'bg-teal-100 text-teal-800',
  'LV&TLC': 'bg-pink-100 text-pink-800',
}
