// Aviso al coordinador que actúa sobre la unidad del OTRO (requisito 2026-09-07).
// Desde hoy los dos coordinadores (rol supervisor) ven y pueden editar ambas unidades.
// Para evitar cambios por descuido, la PRIMERA vez que uno edita, importa, genera o
// descarga contenido de la unidad ajena sale un popup de confirmación. Una vez aceptado,
// no se vuelve a preguntar por esa unidad en la sesión del navegador (sessionStorage:
// se limpia al cerrar la pestaña o al cerrar sesión).
import { UNIDADES } from '../config/unidades'

export const KEY_UNIDAD_AJENA = 'sabogal-unidad-ajena-ok'

const leer = () => {
  try { return JSON.parse(sessionStorage.getItem(KEY_UNIDAD_AJENA) || '{}') } catch { return {} }
}

export const limpiarConfirmacionesUnidad = () => {
  try { sessionStorage.removeItem(KEY_UNIDAD_AJENA) } catch { /* sin storage */ }
}

// ¿La unidad activa es distinta de la propia del coordinador? (solo aplica al rol supervisor:
// dev/directivo supervisan todo y no necesitan el aviso)
export const esUnidadAjena = (usuario, unidad) =>
  usuario?.rol === 'supervisor' && !!unidad && unidad !== (usuario?.unidad || 'SABOGAL')

// Devuelve true si se puede continuar (no aplica, ya confirmado o el usuario aceptó).
export function confirmarUnidadAjena(usuario, unidad) {
  if (!esUnidadAjena(usuario, unidad)) return true
  const ok = leer()
  if (ok[unidad]) return true
  const ajena  = UNIDADES[unidad]?.corto || unidad
  const propia = UNIDADES[usuario?.unidad || 'SABOGAL']?.corto || usuario?.unidad
  const acepta = window.confirm(
    `Estás haciendo cambios o descargando contenido de ${ajena} y tú perteneces a ${propia}.\n\n` +
    `¿Estás seguro de que quieres continuar?\n\n` +
    `Si aceptas, no se volverá a preguntar por ${ajena} mientras dure esta sesión.`,
  )
  if (acepta) {
    ok[unidad] = true
    try { sessionStorage.setItem(KEY_UNIDAD_AJENA, JSON.stringify(ok)) } catch { /* sin storage */ }
  }
  return acepta
}
