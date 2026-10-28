// ux.js — Gestión General para directivos (CHANGELOG 2026-10-25): formato, vocabulario único y las columnas de la cadena. Un solo diccionario de
// términos. Nada se suma aquí: las cifras vienen armadas del backend (/api/gestion/zona y /cadena).

import api from '../../utils/api'

const MES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
export const miles = (n) => Math.round(Math.abs(Number(n) || 0)).toString().replace(/\B(?=(\d{3})+(?!\d))/g, '.')

// «$3.677 M» desde mil millones, «$71,8 M» desde un millón, al peso por debajo
export function mm(v) {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v), a = Math.abs(n), s = n < 0 ? '−' : ''
  if (a >= 1e9) return `${s}$${miles(a / 1e6)} M`
  if (a >= 1e6) { let t = (a / 1e6).toFixed(1).replace('.', ','); if (t.endsWith(',0')) t = t.slice(0, -2); return `${s}$${t} M` }
  return `${s}$${miles(a)}`
}
export const pesos = (v) => (v === null || v === undefined || v === '' ? '—' : `${Number(v) < 0 ? '−' : ''}$${miles(Number(v))}`)
export const num = (v) => (v === null || v === undefined || v === '' ? '—' : Number(v).toLocaleString('es-CO', { maximumFractionDigits: 3 }))

// fecha en hora Colombia: las que traen zona (UTC) se corren 5 h; las sin zona ya son hora Colombia
function partes(iso) {
  if (!iso) return null
  const s = String(iso)
  if (/T.*(Z|[+-]\d{2}:?\d{2})$/.test(s)) { const d = new Date(new Date(s).getTime() - 5 * 3600e3); return { y: d.getUTCFullYear(), m: d.getUTCMonth() + 1, d: d.getUTCDate() } }
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/)
  return m ? { y: +m[1], m: +m[2], d: +m[3] } : null
}
export function fecha(iso) {
  const p = partes(iso)
  return p ? `${p.d} ${MES[p.m - 1]}${p.y !== new Date().getFullYear() ? ' ' + p.y : ''}` : '—'
}
export function mesAnio(iso) { const p = partes(iso); return p ? `${MES[p.m - 1]} ${p.y}` : '' }
export const plural = (n, a, b) => `${miles(n)} ${Number(n) === 1 ? a : b}`

export const TIPO = { programado: 'Programado', mayor_impacto: 'Mayor impacto', linea_viva: 'Línea viva' }
export const ZONA = { UBATE: 'Ubaté', ALMEIDAS: 'Almeidas', GUAVIO: 'Guavio', 'LV&TLC': 'Línea viva y telecontrol', SABOGAL: 'Sabogal' }
export const SIS = { correo: 'correo', sgs: 'SGS', sonny: 'Sonny', pms: 'PMS', scm: 'SCM', sgo7: 'SGO7', drape: 'DRAPE', sap: 'SAP' }

export function limpiarNombre(n, cod) {
  const c = String(cod || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return String(n || '').replace(new RegExp('LCL\\s*:?\\s*' + c, 'ig'), '').replace(new RegExp('_?' + c + '_?', 'g'), ' ')
    .replace(/\s{2,}/g, ' ').replace(/^[\s_\-·:]+|[\s_\-·:]+$/g, '') || 'sin nombre en SGS'
}

// marca de estado de una pieza: ✓ = listo (sin color: el verde es solo para lo cobrado), si no un punto del color
export const COLORES = new Set(['verde', 'ambar', 'rojo', 'gris'])
export const pillDe = (c) => (COLORES.has(c) ? c : 'gris')

// La cadena: 8 columnas en el orden del flujo
export const COLS = [
  { k: 'origen', t: 'LCL', u: 'LCL', s: 'SCM · Enel', ev: 'scm_lcl' },
  { k: 'pms', t: 'Solicitud', u: 'solicitud', s: 'PMS · Enel', ev: 'pms' },
  { k: 'pdl', t: 'PDL', u: 'PDL', s: 'Programación', ev: 'sonny' },
  { k: 'nodos', t: 'Nodos', u: '', s: 'Sonny y SGS', ev: 'sonny_etapa' },
  { k: 'planillas', t: 'Planillas', u: 'planilla', s: 'Sonny', ev: 'sonny' },
  { k: 'libretos', t: 'Libretos', u: 'libreto', s: 'SCM · Enel', ev: 'scm_mo' },
  { k: 'facturas', t: 'Facturas', u: 'factura', s: 'DRAPE · Enel', ev: 'drape' },
  { k: 'pago', t: 'Pago', u: 'pago', s: 'DRAPE', ev: 'drape' },
]
export const QUE_ES = {
  origen: 'La LCL es el pedido de Enel para este trabajo: fija qué se hace, hasta cuándo y por cuánto. Vive en SCM. La factura se cobra por LCL.',
  pms: 'La solicitud de PMS es el registro del trabajo en la plataforma de Enel, con su presupuesto. Su código viaja en cada planilla de Sonny.',
  pdl: 'El PDL es el plan de trabajo programado. Une las dos puntas: va en la planilla de Sonny y en la descripción del libreto de SCM.',
  nodos: 'Un nodo es un punto o tramo de la red donde se trabaja. Se asigna en Sonny y se liquida en el proyecto de SGS.',
  planillas: 'La planilla es el acta de lo ejecutado en Sonny. Dominion la firma; Enel la recibe, la concilia y la aprueba o la rechaza.',
  libretos: 'El libreto es la autocertificación en SCM de lo ejecutado, por LCL. Es lo que Enel acepta para que se pueda facturar.',
  facturas: 'La factura se emite en DRAPE, el portal de facturación del cliente. Nombra la LCL, no el libreto. Enel la registra y fija el vencimiento.',
  pago: 'El pago lo marca DRAPE («pagada o compensada»). Que la LCL esté concluida en SCM no quiere decir que esté pagada.',
}
// «¿Cómo fluye?» (/gestion/flujo) desde el 2026-10-25
export const HAY_FLUJO = true
export const ENLACE = { exacto: 'enlace exacto', monto: 'enlace por monto, confirmar', pendiente: 'varias planillas posibles, confirmar', confirmado: 'confirmado por una persona', lcl: 'por la LCL' }

// El glosario de «¿Cómo fluye?» (12 palabras)
export const GLOSARIO = [
  ['LCL', 'El pedido de Enel para un trabajo (número 63…): qué se hace, hasta cuándo y por cuánto. Vive en SCM. Sin LCL vigente no se cobra.'],
  ['Solicitud PMS', 'El registro del trabajo en PMS, con su presupuesto de mano de obra y material. Su código viaja en cada planilla de Sonny.'],
  ['PDL', 'El plan de trabajo programado (CDOPP…, CDOPPI…, CDOPT… o el ticket DF). Une la planilla de Sonny con el libreto de SCM.'],
  ['Nodo', 'Un punto o tramo de la red donde se trabaja (poste, vano). Se asigna en Sonny y se liquida en SGS.'],
  ['Proyecto en SGS', 'Donde Dominion lleva los nodos, el material y la liquidación (PRO-…). Una LCL puede tener más de un proyecto.'],
  ['Liquidar', 'Registrar en SGS lo ejecutado: la mano de obra de cada nodo y el material consumido.'],
  ['Planilla', 'El acta de lo ejecutado en Sonny. La firman el supervisor y el profesional; Enel la recibe y la concilia.'],
  ['Recibo de obra y conciliación', 'Enel revisa la planilla en documentos y en terreno. Si cumple, la concilia; si no, la rechaza y vuelve a Dominion.'],
  ['Libreto', 'La autocertificación en SCM de lo ejecutado, por LCL. Puede estar en elaboración, en Enel, devuelto o anulado.'],
  ['HES', 'Hoja de entrada de servicios que Enel abre en SAP al aceptar el libreto. La factura las cita.'],
  ['Factura', 'Dominion la emite en DRAPE, el portal de facturación del cliente, y nombra la LCL. Enel la registra y fija el vencimiento.'],
  ['Pago', 'DRAPE marca la factura pagada o compensada. Una LCL concluida en SCM no está pagada por eso.'],
]
// (CHANGELOG 2026-10-25) el glosario cuando la factura y el pago no se muestran (todo se justifica con exportes)
const SIN_COBRO = { Factura: 'Facturación de Dominion la emite a Enel, por LCL; Enel la registra y la paga. La facturación se lee del informe de deuda de facturación de Dominion.',
  Pago: 'Enel paga la factura. Fuente por definir: ningún exporte cargado trae los pagos.' }
export const glosario = (visible) => (visible ? GLOSARIO : GLOSARIO.map(([a, b]) => [a, SIN_COBRO[a] || b]))

// (CHANGELOG 2026-10-25) «Exporte» de cada fuente: exporte dd mmm · consulta directa dd mmm · en vivo · buzón hasta dd mmm
export function exporteTxt(s) {
  if (!s) return '—'
  if (s.tipo === 'vivo' || s.vivo) return 'en vivo'
  if (s.tipo === 'buzon') return s.leido_hasta ? `buzón hasta ${fecha(s.leido_hasta)}` : 'buzón'
  if (s.tipo === 'pantalla') return s.exporte || s.cargado ? `consulta directa ${fecha(s.exporte || s.cargado)}` : 'consulta directa'
  return s.exporte ? `exporte ${fecha(s.exporte)}` : 'sin exporte cargado'
}
export const tamano = (n) => (!n ? '' : n >= 1048576 ? `${(n / 1048576).toFixed(1).replace('.', ',')} MB` : `${Math.max(1, Math.round(n / 1024))} KB`)

// (CHANGELOG 2026-10-25) «Descargar»: el archivo de una carga tal como se cargó (la descarga queda en Registros)
export async function descargarCarga(id, nombre) {
  const r = await api.get(`/gestion/cargas/${id}/archivo`, { responseType: 'blob' })
  const cd = r.headers?.['content-disposition'] || ''
  const n = (cd.match(/filename\*=UTF-8''([^;]+)/) || [])[1]
  const fin = n ? decodeURIComponent(n) : (cd.match(/filename="([^"]+)"/) || [])[1] || nombre || `fuente_${id}`
  const url = URL.createObjectURL(r.data)
  const a = document.createElement('a'); a.href = url; a.download = fin; document.body.appendChild(a); a.click(); a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
  return fin
}

export const GLOSARIO_COL = { origen: 'LCL', pms: 'Solicitud PMS', pdl: 'PDL', nodos: 'Nodo', planillas: 'Planilla', libretos: 'Libreto', facturas: 'Factura', pago: 'Pago' }

// sube al principio de la página (en SGO7 el que se desplaza es <main>)
export function arriba() {
  try { document.querySelector('main')?.scrollTo({ top: 0 }); window.scrollTo(0, 0) } catch { /* sin scroll */ }
}

// La línea de tiempo en palabras llanas: sin los códigos de SCM entre paréntesis y con el estado de la planilla como lo dice el directivo
const ESTADO_PLANILLA = { 'Libretos Aprobados': 'Aprobada por Enel', 'Libretos Rechazados': 'Rechazada por Enel', 'Libretos SCM': 'Libretos en SCM', Pendiente: 'Pendiente de Dominion',
  'Aprobado Supervisor': 'Aprobada por el supervisor', Firmada: 'Firmada', 'En firmas': 'En firmas', 'Recibo Obra': 'Recibo de obra (Enel)', Conciliacion: 'En conciliación (Enel)',
  Conciliada: 'Conciliada', 'Conc. Pendiente': 'Conciliación pendiente', 'Recibo Rechazado': 'Rechazada por Enel', 'Conc. Rechazada': 'Rechazada por Enel' }
const COD_SCM = 'RIL|ACQ|RIF|ANN|LAV|CHI|INV|SAP|BOZZA|BOZ|BOC'
export const sinCodigo = (s) => String(s ?? '').replace(new RegExp(`\s*\((?:lanzado|${COD_SCM})\)`, 'g'), '').replace(new RegExp(`\b(?:${COD_SCM}) \(([^)]*)\)`, 'g'), '$1')
export const planillaLlana = (e) => ESTADO_PLANILLA[e] || e
export const limpiarEvento = (s) => sinCodigo(s).replace(/«([^»]+)»/g, (m, x) => `«${planillaLlana(x)}»`)
