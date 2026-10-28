// Identificador único y estable por dispositivo/navegador (para el control de acceso)
const KEY = 'sabogal-device-id'

export function getDeviceId() {
  let id = localStorage.getItem(KEY)
  if (!id) {
    id = (crypto?.randomUUID?.() ||
      'd-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12))
    localStorage.setItem(KEY, id)
  }
  return id
}
