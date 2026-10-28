// Depuración determinista de la observación (regla general 2026-08-25): la celda que
// lee ENEL solo habla de POR QUÉ ESTE caso es excluible según su propia evidencia.
// Se eliminan las frases de proceso interno — videos de otros PDL, ids de mensajes,
// niveles de confianza, «por validar»… — que ya viven en el resumen del análisis.
// La usan el botón «Actualizar observación» de Auditoría IA y el de Correos ANS.
const PROHIBIDAS = [
  /OVERLAY/i,
  // OJO: «causa externa no atribuible (a la empresa)» es lenguaje LEGÍTIMO de la celda —
  // lo prohibido es hablar de VIDEOS no atribuibles al caso (lección del R06277, 25-08).
  /VIDEOS? NO ATRIBUIBLES?/i,
  /NO ATRIBUIBLES? AL (CASO|PDL)/i,
  /SE HALL(Ó|O|ARON)\b/i,
  /BORRADOR SUJETO/i,
  /POR VALIDAR/i,
  /\bMSG[ _]?ID\b/i,
  /\bMSG\s?\d/i,
  /CONFIANZA (ALTA|MEDIA|BAJA)/i,
  /DE OTROS? PDL/i,
  /CORRESPONDEN? A OTROS?\b/i,
  /CROSSWALK/i,
  /REQUIERE VALIDACI/i,
  /SE REVIS(Ó|O|A) (TODO )?EL MATERIAL/i,
  /TRANSCRIPCI(Ó|O)N AUTOM/i,
]

export function depurarObs(texto) {
  const frases = (texto || '').split(/(?<=\.)\s+/)
  return frases
    .filter((f) => !PROHIBIDAS.some((p) => p.test(f)))
    .join(' ')
    .replace(/\s{2,}/g, ' ')
    .trim()
}
