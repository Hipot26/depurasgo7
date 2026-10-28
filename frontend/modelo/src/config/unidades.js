// Configuración por unidad operativa — aislamiento Programados / No programados.
// Los ids SABOGAL / CHOCONTA se conservan en la base de datos (nombres históricos).
// Desde 2026-09-07: SABOGAL = coordinación de programados, CHOCONTA = coordinación de no programados.
// Cada unidad ve solo sus datos; el rol `directivo` (jefe superior) ve todas.
// Una sola fuente de verdad para la grilla y el modal de nueva fila.

export const UNIDADES = {
  SABOGAL: {
    id: 'SABOGAL',
    nombre: 'Programados',
    corto: 'Programados',
    tipoLabel: 'TIPO',                       // tipo de grupo / cuadrilla
    // Grafía del cliente (plantilla PROGRAMACIÓN): CMAP5/CMAP3 — antes CAMP5/CAMP3
    tipoOptions: ['CMAP5', 'CMAP3', 'ARRIME', 'GRUA', 'ESTACA', 'LVL3', 'MIE'],
    zonas: ['ALMEIDAS', 'GUAVIO', 'UBATE', 'LV&TLC'],
    procesoOptions: ['OBRAS', 'MANTENIMIENTO', 'DESCANSO', 'PTE MATERIALZIAR', ''],
    subprocesoOptions: ['OBRAS SIMPLES', 'AMP', 'NNDD', 'IPPE', ''],
    estadoPdtOptions: ['PROGRAMADO', 'EJECUTADO', 'PARCIAL', 'CANCELADO', 'ANULADO', ''],
    // Plantilla 2026-08: HORA única → HORA AB/CR; INOPERATIVIDAD $ → texto (inoperatividad_tipo)
    ocultarColumnas: ['hora', 'inoperatividad'],
  },
  CHOCONTA: {
    id: 'CHOCONTA',
    nombre: 'No programados',
    corto: 'No programados',
    tipoLabel: 'TIPO RECURSO',               // MOES: tipo de recurso
    tipoOptions: ['MOE2', 'MOE1', 'TAI', 'SUP.'],
    zonas: ['ALMEIDAS', 'GUAVIO', 'UBATE'],
    procesoOptions: ['ATENCION DE EMERGENCIAS', 'MAYOR IMPACTO', 'BOLSAS', 'DESCANSO', ''],
    subprocesoOptions: ['ATENCION DE EMERGENCIAS', 'MAYOR IMPACTO', 'NNDDD', 'DESPEJES', 'AMP', 'CUN 100', 'IFC', 'MEDICION SPT', 'PLAN BT RURAL', 'PQR', ''],
    estadoPdtOptions: ['PROGRAMADO', 'EJECUTADO', 'PARCIAL', 'CANCELADO', 'ANULADO', ''],
    // Columnas que NO aplican a la unidad CHOCONTA (retiradas del Excel MOES + nuevas de SABOGAL 2026-08)
    ocultarColumnas: ['pms', 'fecha_vigencia', 'cd_afectados', 'ot', 'estado_cumplimentacion',
                      'saidi', 'hora_ab', 'hora_cr', 'restricciones', 'se_gestiona',
                      'inoperatividad_tipo', 'observaciones_inoperatividad'],
  },
}

export const ORDEN_UNIDADES = ['SABOGAL', 'CHOCONTA']

export const unidadCfg = (id) => UNIDADES[id] || UNIDADES.SABOGAL

// Roles que ven TODAS las unidades y pueden cambiar entre ellas: jefe superior, desarrollador
// y, desde 2026-09-07, los coordinadores (supervisor): cada uno ve la unidad del otro.
export const esRolGlobal = (rol) => rol === 'directivo' || rol === 'desarrollador' || rol === 'supervisor'

// Roles de solo lectura (ven dashboard y grilla, no editan)
export const esSoloLectura = (rol) => rol === 'consulta' || rol === 'directivo' || rol === 'cliente'

// Unidad efectiva de un usuario (override para roles globales que cambian de vista)
export const unidadDe = (usuario, override) =>
  override || usuario?.unidad || 'SABOGAL'
