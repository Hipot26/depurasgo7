// Captura un nodo del DOM (una tarjeta de indicador) como imagen PNG, para pegar en un
// correo. La librería se carga solo cuando se usa (import dinámico) para no engordar el
// bundle. Los elementos marcados con data-no-capture (los propios botones) se dejan fuera.

const noCaptura = (n) => !n?.dataset || n.dataset.noCapture === undefined

const opciones = (node) => ({
  backgroundColor: '#ffffff',
  pixelRatio: 2,                       // nítida en pantallas retina / al hacer zoom en el correo
  width: node.scrollWidth,             // toma el ancho real por si la tabla desborda la tarjeta
  height: node.scrollHeight,
  filter: noCaptura,
  style: { overflow: 'visible', margin: '0' },
})

// Copia la imagen al portapapeles (queda lista para Ctrl+V en el correo).
export async function copiarImagen(node) {
  const { toBlob } = await import('html-to-image')
  const blob = await toBlob(node, opciones(node))
  if (!blob) throw new Error('sin imagen')
  await navigator.clipboard.write([new window.ClipboardItem({ 'image/png': blob })])
}

// Descarga la imagen como archivo .png.
export async function descargarImagen(node, nombre) {
  const { toPng } = await import('html-to-image')
  const url = await toPng(node, opciones(node))
  const a = document.createElement('a')
  a.href = url
  a.download = `${(nombre || 'indicador').replace(/[^\w.-]+/g, '_').replace(/^_+|_+$/g, '')}.png`
  a.click()
}
