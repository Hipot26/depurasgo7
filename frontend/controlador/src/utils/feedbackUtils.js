// Utilidades del chat de sugerencias:
//  · buffer de errores JS recientes (contexto oculto para el desarrollador)
//  · captura automática de contexto técnico
//  · compresión de capturas de pantalla a data-URI

const errorBuffer = []
function pushError(entry) {
  errorBuffer.push({ ...entry, t: new Date().toISOString() })
  while (errorBuffer.length > 12) errorBuffer.shift()
}

if (typeof window !== 'undefined' && !window.__sabogalErrHook) {
  window.__sabogalErrHook = true
  window.addEventListener('error', (e) => {
    pushError({ tipo: 'error', msg: String(e?.message || e?.error || '').slice(0, 300), origen: `${e?.filename || ''}:${e?.lineno || ''}:${e?.colno || ''}` })
  })
  window.addEventListener('unhandledrejection', (e) => {
    pushError({ tipo: 'promise', msg: String(e?.reason?.message || e?.reason || '').slice(0, 300) })
  })
}

// Contexto técnico auto-capturado — NO visible para el usuario, solo para el dev
export function buildContexto(extra = {}) {
  try {
    return {
      url: window.location.href,
      ruta: window.location.pathname,
      titulo_pagina: document.title,
      user_agent: navigator.userAgent,
      plataforma: navigator.platform,
      idioma: navigator.language,
      viewport: `${window.innerWidth}x${window.innerHeight}`,
      pantalla: `${window.screen?.width}x${window.screen?.height}`,
      zona_horaria: Intl.DateTimeFormat().resolvedOptions().timeZone,
      referrer: document.referrer || null,
      online: navigator.onLine,
      errores_recientes: errorBuffer.slice(-8),
      enviado_en: new Date().toISOString(),
      ...extra,
    }
  } catch {
    return { enviado_en: new Date().toISOString(), ...extra }
  }
}

// Comprime una imagen (File/Blob) a un data-URI JPEG manejable
export async function fileToCompressedDataURL(file, maxDim = 1600, quality = 0.75) {
  const dataUrl = await new Promise((res, rej) => {
    const r = new FileReader()
    r.onload = () => res(r.result)
    r.onerror = rej
    r.readAsDataURL(file)
  })
  const img = await new Promise((res, rej) => {
    const i = new Image()
    i.onload = () => res(i)
    i.onerror = rej
    i.src = dataUrl
  })
  let { width, height } = img
  if (width > maxDim || height > maxDim) {
    const s = maxDim / Math.max(width, height)
    width = Math.round(width * s)
    height = Math.round(height * s)
  }
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  canvas.getContext('2d').drawImage(img, 0, 0, width, height)
  return canvas.toDataURL('image/jpeg', quality)
}
