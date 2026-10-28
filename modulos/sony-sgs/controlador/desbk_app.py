#!/usr/bin/env python3
"""
Interfaz web para generación de planillas Planillas.
Uso: python app.py
Luego abre: http://localhost:5000
"""

import os
import sys
import re
import subprocess
import webbrowser
import threading
import time
from datetime import datetime
from flask import Flask, request, jsonify, Response, send_file

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
PYTHON       = sys.executable
# DATA_DIR: donde se guardan carpetas/archivos generados.
# En Railway apunta al Volume montado; localmente es la misma carpeta del proyecto.
DATA_DIR     = os.environ.get("DATA_DIR", BASE_DIR)
SGS_TEMP_DIR = os.path.join(DATA_DIR, "_sgs_temp")

app = Flask(__name__)

HTML = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Planillas — Generador de Planillas</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Segoe UI', Arial, sans-serif;
      background: #f0f4f8;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    .card {
      background: #fff;
      border-radius: 12px;
      box-shadow: 0 4px 24px rgba(0,0,0,0.10);
      padding: 40px 48px;
      width: 100%;
      max-width: 520px;
    }

    .logo-bar {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 28px;
    }

    .logo-icon {
      width: 42px; height: 42px;
      background: #1F3864;
      border-radius: 8px;
      display: flex; align-items: center; justify-content: center;
      color: #fff; font-size: 20px; font-weight: bold;
    }

    h1 {
      font-size: 18px;
      color: #1F3864;
      font-weight: 700;
      line-height: 1.2;
    }

    h1 span {
      display: block;
      font-size: 12px;
      font-weight: 400;
      color: #6b7280;
      margin-top: 2px;
    }

    label {
      display: block;
      font-size: 13px;
      font-weight: 600;
      color: #374151;
      margin-bottom: 8px;
    }

    .input-row {
      display: flex;
      gap: 10px;
    }

    input[type="text"] {
      flex: 1;
      padding: 12px 16px;
      border: 2px solid #d1d5db;
      border-radius: 8px;
      font-size: 16px;
      font-family: 'Consolas', monospace;
      color: #111827;
      outline: none;
      transition: border-color .2s;
    }

    input[type="text"]:focus {
      border-color: #2E75B6;
    }

    button {
      padding: 12px 24px;
      background: #1F3864;
      color: #fff;
      border: none;
      border-radius: 8px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      transition: background .2s, transform .1s;
      white-space: nowrap;
    }

    button:hover  { background: #2E75B6; }
    button:active { transform: scale(0.98); }
    button:disabled { background: #9ca3af; cursor: not-allowed; }

    .spinner {
      display: none;
      width: 18px; height: 18px;
      border: 3px solid #fff4;
      border-top-color: #fff;
      border-radius: 50%;
      animation: spin .7s linear infinite;
      margin: 0 auto;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    .result {
      margin-top: 24px;
      border-radius: 8px;
      padding: 16px 20px;
      font-size: 13px;
      display: none;
    }

    .result.ok {
      background: #ecfdf5;
      border: 1px solid #6ee7b7;
      color: #065f46;
    }

    .result.err {
      background: #fef2f2;
      border: 1px solid #fca5a5;
      color: #7f1d1d;
    }

    .result .title {
      font-weight: 700;
      font-size: 14px;
      margin-bottom: 10px;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .result .files {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-top: 8px;
    }

    .file-link {
      display: flex;
      align-items: center;
      gap: 8px;
      text-decoration: none;
      color: #1F3864;
      background: #fff;
      border: 1px solid #d1fae5;
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 12px;
      font-weight: 600;
      transition: background .15s;
    }

    .file-link:hover { background: #f0fdf4; }

    .folder-link {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-top: 12px;
      color: #2E75B6;
      font-weight: 600;
      font-size: 12px;
      text-decoration: none;
      border-bottom: 1px dashed #2E75B6;
      padding-bottom: 1px;
    }

    .folder-link:hover { color: #1F3864; }

    .log {
      margin-top: 12px;
      background: #f9fafb;
      border: 1px solid #e5e7eb;
      border-radius: 6px;
      padding: 10px 12px;
      font-family: 'Consolas', monospace;
      font-size: 11px;
      color: #6b7280;
      max-height: 140px;
      overflow-y: auto;
      white-space: pre-wrap;
    }

    /* ── ERP Section ── */
    .sgs-divider {
      margin-top: 24px;
      border-top: 2px solid #e5e7eb;
      padding-top: 20px;
    }

    .sgs-title {
      font-size: 13px;
      font-weight: 700;
      color: #1F3864;
      margin-bottom: 12px;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .upload-btn {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 18px;
      background: #f3f4f6;
      border: 2px dashed #d1d5db;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      color: #374151;
      cursor: pointer;
      transition: border-color .2s, background .2s;
    }

    .upload-btn:hover { border-color: #2E75B6; background: #eff6ff; }

    .sgs-fname {
      font-size: 12px;
      color: #6b7280;
      margin-left: 8px;
    }

    .match-badge {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 6px;
      font-size: 13px;
      margin-bottom: 10px;
    }

    .match-ok  { background: #ecfdf5; border: 1px solid #6ee7b7; color: #065f46; }
    .match-warn{ background: #fffbeb; border: 1px solid #fcd34d; color: #92400e; }
    .match-err { background: #fef2f2; border: 1px solid #fca5a5; color: #7f1d1d; }

    .sgs-select {
      width: 100%;
      padding: 10px 12px;
      border: 2px solid #d1d5db;
      border-radius: 8px;
      font-size: 13px;
      color: #111827;
      outline: none;
      background: #fff;
      margin-top: 4px;
    }

    .sgs-select:focus { border-color: #2E75B6; }

    .toggle-link {
      font-size: 12px;
      color: #2E75B6;
      text-decoration: none;
      border-bottom: 1px dashed #2E75B6;
    }

    .toggle-link:hover { color: #1F3864; }

    .sgs-file-list {
      display: flex;
      flex-direction: column;
      gap: 5px;
      margin-top: 10px;
    }

    .sgs-file-item {
      display: flex;
      align-items: center;
      gap: 8px;
      background: #f9fafb;
      border: 1px solid #e5e7eb;
      border-radius: 6px;
      padding: 6px 10px;
      font-size: 12px;
      color: #374151;
    }

    .sgs-file-item .sgs-node-tag {
      margin-left: auto;
      background: #dbeafe;
      color: #1e40af;
      border-radius: 4px;
      padding: 2px 7px;
      font-size: 11px;
      font-weight: 600;
      white-space: nowrap;
    }

    .sgs-file-item .sgs-remove {
      cursor: pointer;
      color: #9ca3af;
      font-size: 14px;
      line-height: 1;
      flex-shrink: 0;
    }

    .sgs-file-item .sgs-remove:hover { color: #ef4444; }
  </style>
</head>
<body>
<div class="card">
  <div class="logo-bar">
    <div class="logo-icon">S</div>
    <h1>Generador de Planillas
      <span>Planillas — Plantilla Sergio</span>
    </h1>
  </div>

  <label for="id_input">ID de Planilla</label>
  <div class="input-row">
    <input type="text" id="id_input" placeholder="ej: 17082"
           autofocus autocomplete="off"
           onkeydown="if(event.key==='Enter') procesar()">
    <button id="btn" onclick="procesar()">Generar</button>
  </div>

  <div class="result" id="result"></div>

  <!-- ── Seccion ERP (aparece tras procesar planilla Sony) ── -->
  <div id="sgs-section" style="display:none;">
    <div class="sgs-divider">
      <div class="sgs-title">&#9741; Cruce con ERP</div>

      <!-- Subida de archivos (multiple) -->
      <label class="upload-btn" for="sgs-file">
        &#128193; Agregar archivos ERP (.xlsx)
        <input type="file" id="sgs-file" accept=".xlsx,.xls" multiple style="display:none"
               onchange="cargarSGS(this)">
      </label>
      <div id="sgs-file-list" class="sgs-file-list"></div>

      <!-- Matching DF (se muestra tras subir archivo) -->
      <div id="sgs-match-area" style="display:none; margin-top:14px;">
        <div id="sgs-auto-badge" class="match-badge match-ok" style="display:none;">
          &#10003; Cruce automatico &mdash; <strong id="sgs-df-label" style="margin-left:4px;"></strong>
        </div>
        <div id="sgs-manual-area" style="display:none;">
          <div class="match-badge match-warn">&#9888; Selecciona el DF del archivo ERP</div>
          <label style="font-size:12px; font-weight:600; color:#374151;">DF en ERP:</label>
          <select id="sgs-df-select" class="sgs-select"></select>
        </div>
        <div style="margin-top:8px;">
          <a href="#" id="sgs-toggle-link" class="toggle-link"
             onclick="toggleModoSGS(event)"></a>
        </div>
        <button id="sgs-btn" onclick="generarCruceSGS()"
                style="margin-top:12px; width:100%;">
          Generar Cruce ERP
        </button>
      </div>

      <div class="result" id="sgs-result" style="display:none; margin-top:12px;"></div>
    </div>
  </div>
</div>

<script>
// ── Estado ERP ──────────────────────────────────────────────────────────────
let _currentId    = null;
let _sgsArchivos  = [];   // [{nombre, ruta, nodos}]
let _sgsDfs       = [];   // DFs unicos en todos los archivos subidos
let _sonyDf       = null;
let _modoManual   = false;

// ── Sony ─────────────────────────────────────────────────────────────────────
async function procesar() {
  const id = document.getElementById('id_input').value.trim();
  if (!id || !/^\\d+$/.test(id)) {
    mostrarError('Ingresa un ID numérico válido.');
    return;
  }

  const btn = document.getElementById('btn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner" style="display:block"></div>';
  ocultarResultado();

  try {
    const res  = await fetch('/procesar', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({ id })
    });
    const data = await res.json();

    if (data.ok) {
      mostrarOk(data);
    } else {
      mostrarError(data.error, data.log);
    }
  } catch(e) {
    mostrarError('No se pudo conectar con el servidor.<br><small>Asegúrate de que <b>python app.py</b> esté corriendo.</small>');
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Generar';
  }
}

function mostrarOk(data) {
  const div = document.getElementById('result');
  div.className = 'result ok';
  div.style.display = 'block';
  div.innerHTML = `
    <div class="title">&#10003; Carpeta <strong>${data.id}</strong> creada correctamente</div>
    <div class="files">
      <a class="file-link" href="/abrir_archivo/${data.id}/acta_${data.id}.xls" target="_blank">
        &#128196; acta_${data.id}.xls
      </a>
      <a class="file-link" href="/abrir_archivo/${data.id}/balance_liquidar_${data.id}.xlsx" target="_blank">
        &#128202; balance_liquidar_${data.id}.xlsx
      </a>
    </div>
    ${data.local ? `<a class="folder-link" href="/abrir_carpeta/${data.id}">
      &#128193; Abrir carpeta en el explorador &rarr; ${data.ruta}
    </a>` : ''}
    <div class="log">${data.log}</div>
  `;
  // Mostrar seccion ERP y resetear estado
  _currentId   = data.id;
  _sgsArchivos = [];
  _sgsDfs      = [];
  _sonyDf      = null;
  _modoManual  = false;
  document.getElementById('sgs-file-list').innerHTML = '';
  document.getElementById('sgs-file').value = '';
  document.getElementById('sgs-match-area').style.display = 'none';
  document.getElementById('sgs-result').style.display = 'none';
  document.getElementById('sgs-section').style.display = 'block';
}

// ── ERP ───────────────────────────────────────────────────────────────────────

async function cargarSGS(input) {
  if (!input.files || !input.files.length) return;
  const files = Array.from(input.files);
  input.value = '';  // reset para permitir re-seleccionar mismos archivos
  document.getElementById('sgs-match-area').style.display = 'none';
  document.getElementById('sgs-result').style.display = 'none';

  // Subir cada archivo nuevo (evitar duplicados por nombre)
  const existentes = new Set(_sgsArchivos.map(a => a.nombre));
  const nuevos = files.filter(f => !existentes.has(f.name));

  for (const file of nuevos) {
    const fd = new FormData();
    fd.append('archivo', file);
    let dataFile;
    try {
      const r = await fetch('/subir_sgs', { method: 'POST', body: fd });
      dataFile = await r.json();
    } catch(e) {
      alert('Error al subir ' + file.name + ': ' + e.message);
      continue;
    }
    if (!dataFile.ok) {
      alert('Error en ' + file.name + ': ' + dataFile.error);
      continue;
    }
    _sgsArchivos.push({ nombre: file.name, ruta: dataFile.ruta, nodos: dataFile.nodos });
    // Agregar DFs al pool global
    for (const df of dataFile.dfs) {
      if (!_sgsDfs.includes(df)) _sgsDfs.push(df);
    }
  }

  // Obtener DF Sony (solo la primera vez)
  if (_sonyDf === null) {
    try {
      const r2 = await fetch('/obtener_df_sony/' + _currentId);
      const d2 = await r2.json();
      _sonyDf = (d2.ok && d2.df) ? d2.df : null;
    } catch(e) {
      _sonyDf = null;
    }
  }

  _renderFileList();
  if (_sgsArchivos.length > 0) _renderMatchArea();
}

function _renderFileList() {
  const container = document.getElementById('sgs-file-list');
  container.innerHTML = _sgsArchivos.map((a, i) => {
    const nodesLabel = a.nodos.length ? 'Nodo ' + a.nodos.join(', ') : 'sin nodo';
    return `<div class="sgs-file-item">
      <span>&#10003;</span>
      <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${a.nombre}</span>
      <span class="sgs-node-tag">${nodesLabel}</span>
      <span class="sgs-remove" onclick="removerArchivoSGS(${i})" title="Quitar">&#10005;</span>
    </div>`;
  }).join('');
}

function removerArchivoSGS(idx) {
  _sgsArchivos.splice(idx, 1);
  // Recalcular DFs desde los archivos restantes
  _sgsDfs = [];
  for (const a of _sgsArchivos) {
    // Los DFs del archivo no los tenemos guardados, pero se pueden inferir de los nodos
    // Los obtenemos del match area al reconstruir
  }
  // Reconstruir pool de DFs haciendo un fetch ligero no es necesario:
  // simplemente limpiamos y si quedan archivos mostramos sin auto-match
  _sgsDfs = [];
  _renderFileList();
  if (_sgsArchivos.length === 0) {
    document.getElementById('sgs-match-area').style.display = 'none';
  }
  // Necesitamos los DFs — llamar endpoint ligero de re-deteccion
  _redetectarDFs();
}

async function _redetectarDFs() {
  if (_sgsArchivos.length === 0) return;
  // Re-detectar DFs desde los archivos ya guardados en el servidor
  try {
    const res = await fetch('/dfs_de_rutas', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ rutas: _sgsArchivos.map(a => a.ruta) })
    });
    const data = await res.json();
    if (data.ok) {
      _sgsDfs = data.dfs;
      _renderMatchArea();
    }
  } catch(e) {}
}

function _renderMatchArea() {
  document.getElementById('sgs-match-area').style.display = 'block';

  // Poblar select
  const sel = document.getElementById('sgs-df-select');
  sel.innerHTML = _sgsDfs.map(df => `<option value="${df}">${df}</option>`).join('');

  const autoMatch = _sonyDf && _sgsDfs.includes(_sonyDf);
  const toggleLink = document.getElementById('sgs-toggle-link');

  if (!_modoManual && autoMatch) {
    document.getElementById('sgs-auto-badge').style.display = 'flex';
    document.getElementById('sgs-df-label').textContent = _sonyDf;
    document.getElementById('sgs-manual-area').style.display = 'none';
    toggleLink.textContent = 'Cambiar a modo manual';
    document.getElementById('sgs-toggle-link').parentElement.style.display = 'block';
  } else {
    document.getElementById('sgs-auto-badge').style.display = 'none';
    document.getElementById('sgs-manual-area').style.display = 'block';
    // Pre-seleccionar Sony DF si esta en la lista
    if (_sonyDf) {
      const idx = _sgsDfs.indexOf(_sonyDf);
      if (idx >= 0) sel.selectedIndex = idx;
    }
    if (autoMatch) {
      toggleLink.textContent = 'Volver a modo automatico';
      document.getElementById('sgs-toggle-link').parentElement.style.display = 'block';
    } else {
      document.getElementById('sgs-toggle-link').parentElement.style.display = 'none';
    }
  }
}

function toggleModoSGS(e) {
  e.preventDefault();
  _modoManual = !_modoManual;
  _renderMatchArea();
}

async function generarCruceSGS() {
  if (_sgsArchivos.length === 0) {
    alert('Agrega al menos un archivo ERP.');
    return;
  }
  const autoMatch = _sonyDf && _sgsDfs.includes(_sonyDf);
  const dfSgs = (_modoManual || !autoMatch)
    ? document.getElementById('sgs-df-select').value
    : _sonyDf;

  const btn = document.getElementById('sgs-btn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner" style="display:block"></div>';
  document.getElementById('sgs-result').style.display = 'none';

  try {
    const res = await fetch('/procesar_sgs', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({
        id:        _currentId,
        rutas_sgs: _sgsArchivos.map(a => a.ruta),
        df_sgs:    dfSgs
      })
    });
    const data = await res.json();
    const div = document.getElementById('sgs-result');
    div.style.display = 'block';

    if (data.ok) {
      div.className = 'result ok';
      div.innerHTML = `
        <div class="title">&#10003; Cruce ERP generado (DF: ${dfSgs})</div>
        <div>Se agregaron las hojas <b>balance_SGS</b> y <b>cruce_materiales</b>.</div>
        <div class="files" style="margin-top:8px;">
          <a class="file-link"
             href="/abrir_archivo/${_currentId}/balance_liquidar_${_currentId}.xlsx"
             target="_blank">
            &#128202; balance_liquidar_${_currentId}.xlsx
          </a>
        </div>
        ${data.log ? '<div class="log">' + data.log + '</div>' : ''}
      `;
    } else {
      div.className = 'result err';
      div.innerHTML = `
        <div class="title">&#10007; Error al generar cruce</div>
        <div>${data.error}</div>
        ${data.log ? '<div class="log">' + data.log + '</div>' : ''}
      `;
    }
  } catch(e) {
    const div = document.getElementById('sgs-result');
    div.style.display = 'block';
    div.className = 'result err';
    div.innerHTML = '<div class="title">&#10007; Error de conexion</div>';
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Generar Cruce ERP';
  }
}

function _dfValidationBadge(sonyDf, sgsDf) {
  const sLabel = sonyDf || 'No encontrado';
  const gLabel = sgsDf  || 'No encontrado';
  if (!sonyDf || !sgsDf) {
    return `<div class="match-badge match-warn" style="margin-bottom:10px;">
      &#9888; DF Sony: <strong>${sLabel}</strong> &mdash; DF ERP: <strong>${gLabel}</strong>
    </div>`;
  }
  if (sonyDf === sgsDf) {
    return `<div class="match-badge match-ok" style="margin-bottom:10px;">
      &#10003; DF Sony: <strong>${sLabel}</strong> &mdash; DF ERP: <strong>${gLabel}</strong> &nbsp;&#10003; Coinciden
    </div>`;
  }
  return `<div class="match-badge match-err" style="margin-bottom:10px;">
    &#10007; DF Sony: <strong>${sLabel}</strong> &mdash; DF ERP: <strong>${gLabel}</strong> &nbsp;&#10007; No coinciden
  </div>`;
}

function mostrarError(msg, log) {
  const div = document.getElementById('result');
  div.className = 'result err';
  div.style.display = 'block';
  div.innerHTML = `
    <div class="title">&#10007; Error</div>
    <div>${msg}</div>
    ${log ? `<div class="log">${log}</div>` : ''}
  `;
}

function ocultarResultado() {
  const div = document.getElementById('result');
  div.style.display = 'none';
}
</script>
</body>
</html>
"""


HTML_PRUEBAS = HTML.replace(
    "<title>Planillas — Generador de Planillas</title>",
    "<title>[PRUEBAS] Planillas</title>"
).replace(
    "  </style>",
    """    .test-banner {
      background: #fef3c7;
      border: 2px solid #f59e0b;
      border-radius: 8px;
      padding: 8px 16px;
      margin-bottom: 20px;
      font-size: 13px;
      font-weight: 700;
      color: #92400e;
      text-align: center;
    }
  </style>"""
).replace(
    '<div class="card">',
    '<div class="card">\n  <div class="test-banner">&#9888; ENTORNO DE PRUEBAS &mdash; Solo visible para ti</div>'
# Redirigir todas las llamadas fetch al prefijo /pruebas/
).replace("fetch('/procesar'",       "fetch('/pruebas/procesar'"
).replace("fetch('/subir_sgs'",      "fetch('/pruebas/subir_sgs'"
).replace("fetch('/dfs_de_rutas'",   "fetch('/pruebas/dfs_de_rutas'"
).replace("fetch('/procesar_sgs'",   "fetch('/pruebas/procesar_sgs'"
).replace("fetch('/obtener_df_sony/" ,"fetch('/pruebas/obtener_df_sony/"
).replace("href=\"/abrir_archivo/",  "href=\"/pruebas/abrir_archivo/"
).replace("href=\"/abrir_carpeta/",  "href=\"/pruebas/abrir_carpeta/"
# Inyectar card de categoría antes del título de la sección ERP
).replace(
    '<div class="sgs-title">&#9741; Cruce con ERP</div>',
    '<div id="categoria-card" style="display:none;"></div>\n      <div class="sgs-title">&#9741; Cruce con ERP</div>'
).replace(
    "</script>\n</body>",
    """</script>
<script>
// ── Pruebas: soporte categorización Daniela / Xiomara ────────────────────────
let _planillaInfo = null;

// Patch mostrarOk: después de mostrar resultado, consulta info de categoría
const _origMostrarOk = window.mostrarOk || mostrarOk;
window.mostrarOk = mostrarOk = async function(data) {
  _origMostrarOk(data);
  _planillaInfo = null;
  const cc = document.getElementById('categoria-card');
  if (cc) cc.style.display = 'none';
  try {
    const r = await fetch('/pruebas/obtener_info_planilla/' + data.id);
    const d = await r.json();
    if (d.ok) { _planillaInfo = d; _mostrarCategoriaCard(d); }
  } catch(e) {}
};

function _mostrarCategoriaCard(info) {
  const cc = document.getElementById('categoria-card');
  if (!cc) return;
  const isDaniela = info.categoria === 'daniela';
  const bg     = isDaniela ? '#eff6ff' : '#f0fdf4';
  const border  = isDaniela ? '#3b82f6' : '#16a34a';
  const icon    = isDaniela ? '\\uD83D\\uDCCB' : '\\uD83D\\uDD17';
  const nombre  = isDaniela ? 'DANIELA — Programado' : 'XIOMARA — Asociación';
  cc.style.cssText = `display:block;margin-bottom:16px;padding:12px 16px;border-radius:8px;` +
    `background:${bg};border:1.5px solid ${border};font-size:13px;line-height:1.8;`;
  cc.innerHTML =
    `<div style="font-weight:700;font-size:14px;margin-bottom:4px;">${icon} ${nombre}</div>` +
    (info.lcl    ? `<div><b>LCL:</b> ${info.lcl}</div>` : '') +
    (info.ticket ? `<div><b>PDL:</b> ${info.ticket}</div>` : '') +
    (info.odm    ? `<div><b>OdM:</b> ${info.odm}</div>`  : '') +
    (info.zona   ? `<div><b>Zona:</b> ${info.zona}</div>` : '') +
    `<div style="margin-top:6px;color:#6b7280;font-size:12px;">` +
    `Ahora suba los archivos ERP para generar el cruce.</div>`;
}

// Patch _renderMatchArea: para Daniela oculta el selector de DF y muestra solo el botón
const _origRenderMatchArea = window._renderMatchArea || _renderMatchArea;
window._renderMatchArea = _renderMatchArea = function() {
  if (_planillaInfo && _planillaInfo.categoria === 'daniela') {
    document.getElementById('sgs-match-area').style.display = 'block';
    document.getElementById('sgs-auto-badge').style.display = 'none';
    document.getElementById('sgs-manual-area').style.display = 'none';
    return;
  }
  return _origRenderMatchArea();
};

// Patch generarCruceSGS: para Daniela omite validación de DF
const _origGenerarCruceSGS = window.generarCruceSGS || generarCruceSGS;
window.generarCruceSGS = generarCruceSGS = async function() {
  if (!(_planillaInfo && _planillaInfo.categoria === 'daniela')) {
    return _origGenerarCruceSGS();
  }
  // ── Daniela: sin DF, procesar_sgs.py detecta del RESUMEN ──
  if (_sgsArchivos.length === 0) { alert('Agrega al menos un archivo ERP.'); return; }
  const btn = document.getElementById('sgs-btn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner" style="display:block"></div>';
  document.getElementById('sgs-result').style.display = 'none';
  try {
    const res = await fetch('/pruebas/procesar_sgs', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ id: _currentId, rutas_sgs: _sgsArchivos.map(a => a.ruta), df_sgs: '' })
    });
    const data = await res.json();
    const div = document.getElementById('sgs-result');
    div.style.display = 'block';
    if (data.ok) {
      div.className = 'result ok';
      div.innerHTML =
        `<div class="title">&#10003; Cruce ERP generado — Daniela` +
        (_planillaInfo.lcl ? ` (LCL: ${_planillaInfo.lcl})` : '') + `</div>` +
        `<div>Se agregó la hoja <b>cruce_materiales_daniela</b>.</div>` +
        `<div class="files" style="margin-top:8px;">` +
        `<a class="file-link" href="/pruebas/abrir_archivo/${_currentId}/balance_liquidar_${_currentId}.xlsx" target="_blank">` +
        `&#128202; balance_liquidar_${_currentId}.xlsx</a></div>` +
        (data.log ? `<div class="log">${data.log}</div>` : '');
    } else {
      div.className = 'result err';
      div.innerHTML = `<div class="title">&#10007; Error</div><div>${data.error}</div>` +
        (data.log ? `<div class="log">${data.log}</div>` : '');
    }
  } catch(e) {
    const div = document.getElementById('sgs-result');
    div.style.display = 'block'; div.className = 'result err';
    div.innerHTML = '<div class="title">&#10007; Error de conexion</div>';
  } finally { btn.disabled = false; btn.innerHTML = 'Generar Cruce ERP'; }
};
</script>
</body>"""
).replace(
    "<div class=\"title\">&#10003; Cruce ERP generado (DF: ${dfSgs})</div>\n        <div>Se agregaron las hojas <b>balance_SGS</b> y <b>cruce_materiales</b>.</div>",
    "<div class=\"title\">&#10003; Cruce ERP generado (DF: ${dfSgs})</div>\n        ${_dfValidationBadge(_sonyDf, dfSgs)}\n        <div>Se agregaron las hojas <b>balance_SGS</b>, <b>cruce_materiales_xiomara</b> y <b>cruce_materiales_sergio</b>.</div>"
)


HTML_SGS = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>[PRUEBAS] Balance ERP</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Calibri, sans-serif; background: #f0f4f8; display: flex;
           justify-content: center; align-items: flex-start; min-height: 100vh;
           padding: 40px 16px; }
    .card { background: #fff; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,.1);
            padding: 36px 40px; width: 100%; max-width: 540px; }
    .test-banner { background: #fef3c7; border: 2px solid #f59e0b; border-radius: 8px;
                   padding: 8px 16px; margin-bottom: 20px; font-size: 13px;
                   font-weight: 700; color: #92400e; text-align: center; }
    h1 { font-size: 22px; color: #1F3864; margin-bottom: 6px; }
    .subtitle { color: #6b7280; font-size: 14px; margin-bottom: 28px; }
    label { display: block; font-size: 13px; font-weight: 600; color: #374151;
            margin-bottom: 5px; }
    input[type=text] { width: 100%; padding: 9px 12px; border: 1.5px solid #d1d5db;
                       border-radius: 6px; font-size: 14px; outline: none;
                       transition: border .2s; margin-bottom: 18px; }
    input[type=text]:focus { border-color: #2E75B6; }
    .file-zone { border: 2px dashed #d1d5db; border-radius: 8px; padding: 20px;
                 text-align: center; cursor: pointer; margin-bottom: 24px;
                 transition: border .2s; color: #6b7280; font-size: 14px; }
    .file-zone:hover, .file-zone.has-files { border-color: #2E75B6; color: #1F3864; }
    .file-zone input { display: none; }
    button { width: 100%; padding: 11px; background: #1F3864; color: #fff;
             border: none; border-radius: 8px; font-size: 15px; font-weight: 700;
             cursor: pointer; transition: background .2s; }
    button:hover { background: #2E75B6; }
    button:disabled { background: #9ca3af; cursor: not-allowed; }
    .result { margin-top: 20px; padding: 14px 16px; border-radius: 8px;
              font-size: 14px; display: none; }
    .result.ok  { background: #d1fae5; color: #065f46; border: 1px solid #6ee7b7; }
    .result.err { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
    .result.loading { background: #eff6ff; color: #1e40af; border: 1px solid #93c5fd; }
  </style>
</head>
<body>
<div class="card">
  <div class="test-banner">&#9888; ENTORNO DE PRUEBAS &mdash; /pruebas/sgs</div>
  <h1>Generador de Balance ERP</h1>
  <p class="subtitle">Sube uno o m&aacute;s archivos Excel descargados de ERP y obtiene tu balance agrupado por nodo.</p>

  <label for="df_input">DF (identificador de la orden)</label>
  <input type="text" id="df_input" placeholder="DF202664259239" autocomplete="off">

  <label>Archivos ERP (puedes seleccionar varios nodos)</label>
  <div class="file-zone" id="drop_zone" onclick="document.getElementById('file_input').click()">
    <input type="file" id="file_input" multiple accept=".xlsx,.xls">
    <span id="file_label">&#128194; Haz clic para seleccionar archivos .xlsx</span>
  </div>

  <button id="btn_gen" onclick="generar()">Generar Balance ERP</button>
  <div class="result" id="result_area"></div>
</div>

<script>
  document.getElementById('file_input').addEventListener('change', function() {
    const n = this.files.length;
    const zone = document.getElementById('drop_zone');
    const lbl  = document.getElementById('file_label');
    if (n > 0) {
      zone.classList.add('has-files');
      lbl.textContent = n === 1 ? this.files[0].name : n + ' archivos seleccionados';
    } else {
      zone.classList.remove('has-files');
      lbl.textContent = '\\u{1F4C2} Haz clic para seleccionar archivos .xlsx';
    }
  });

  async function generar() {
    const df    = document.getElementById('df_input').value.trim().toUpperCase();
    const files = document.getElementById('file_input').files;
    const res   = document.getElementById('result_area');
    const btn   = document.getElementById('btn_gen');

    if (!df) { _show(res, 'err', 'Ingresa el DF antes de continuar.'); return; }
    if (!files.length) { _show(res, 'err', 'Selecciona al menos un archivo ERP.'); return; }

    btn.disabled = true;
    _show(res, 'loading', 'Procesando archivos...');

    const fd = new FormData();
    fd.append('df', df);
    for (const f of files) fd.append('archivos', f);

    try {
      const r = await fetch('/pruebas/sgs/generar', { method: 'POST', body: fd });
      if (!r.ok) {
        const j = await r.json().catch(() => ({ error: 'Error desconocido' }));
        _show(res, 'err', '&#10007; ' + j.error);
        return;
      }
      const blob = await r.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement('a');
      a.href = url;
      a.download = 'balance_SGS_' + df + '.xlsx';
      document.body.appendChild(a); a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      _show(res, 'ok', '&#10003; Balance generado correctamente &mdash; descargando...');
    } catch(e) {
      _show(res, 'err', '&#10007; ' + e.message);
    } finally {
      btn.disabled = false;
    }
  }

  function _show(el, cls, msg) {
    el.className = 'result ' + cls;
    el.innerHTML = msg;
    el.style.display = 'block';
  }
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")


@app.route("/pruebas")
def pruebas():
    return Response(HTML_PRUEBAS, mimetype="text/html")


@app.route("/ping")
def ping():
    return jsonify({"ok": True})


@app.route("/procesar", methods=["POST"])
def procesar():
    data = request.get_json()
    id_planilla = (data or {}).get("id", "").strip()

    if not id_planilla.isdigit():
        return jsonify({"ok": False, "error": "ID inválido."})

    log_lines = []

    def run(script):
        env = {**os.environ, "DATA_DIR": DATA_DIR}
        result = subprocess.run(
            [PYTHON, os.path.join(BASE_DIR, script), id_planilla],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=BASE_DIR, env=env
        )
        log_lines.append(f"--- {script} ---")
        if result.stdout:
            log_lines.append(result.stdout.strip())
        if result.stderr:
            log_lines.append(result.stderr.strip())
        return result.returncode

    rc1 = run("descargar_acta.py")
    rc2 = run("balance_liquidar.py")
    log = "\n".join(log_lines)

    if rc1 != 0 or rc2 != 0:
        return jsonify({"ok": False, "error": "Ocurrió un error al procesar.", "log": log})

    carpeta = os.path.join(DATA_DIR, id_planilla)
    return jsonify({
        "ok":    True,
        "id":    id_planilla,
        "ruta":  carpeta,
        "local": os.name == "nt",
        "log":   log,
    })


@app.route("/abrir_carpeta/<id_planilla>")
def abrir_carpeta(id_planilla):
    if not id_planilla.isdigit():
        return "ID inválido", 400
    # Solo funciona en Windows local; en servidor es no-op
    if os.name == "nt":
        carpeta = os.path.join(DATA_DIR, id_planilla)
        if os.path.isdir(carpeta):
            os.startfile(carpeta)
    return Response("", status=204)


@app.route("/abrir_archivo/<id_planilla>/<nombre>")
def abrir_archivo(id_planilla, nombre):
    if not id_planilla.isdigit():
        return "ID inválido", 400
    permitidos = {
        f"acta_{id_planilla}.xls",
        f"balance_liquidar_{id_planilla}.xlsx",
    }
    if nombre not in permitidos:
        return "Archivo no permitido", 403
    ruta = os.path.join(DATA_DIR, id_planilla, nombre)
    if not os.path.isfile(ruta):
        return "Archivo no encontrado", 404
    if os.name == "nt":
        # Local Windows: abrir con la app predeterminada
        os.startfile(ruta)
        return Response("", status=204)
    # Servidor Linux: descargar directamente al navegador
    return send_file(ruta, as_attachment=True, download_name=nombre)


# ── Rutas ERP ──────────────────────────────────────────────────────────────────

@app.route("/obtener_df_sony/<id_planilla>")
def obtener_df_sony(id_planilla):
    """Lee el campo OdM/Grafo (DF) del Excel Sony ya generado."""
    if not id_planilla.isdigit():
        return jsonify({"ok": False, "error": "ID invalido"})
    ruta = os.path.join(DATA_DIR, id_planilla, f"balance_liquidar_{id_planilla}.xlsx")
    if not os.path.isfile(ruta):
        return jsonify({"ok": False, "error": "Excel no encontrado"})
    try:
        import openpyxl
        wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
        ws = wb["RESUMEN"]
        odm = None
        for row in ws.iter_rows(values_only=True):
            label = str(row[0]).strip() if row[0] else ""
            if "OdM" in label or "odm" in label.lower():
                val = str(row[1]).strip() if row[1] is not None else ""
                if val and val != "\u2014":
                    odm = val
                break
        wb.close()
        return jsonify({"ok": True, "df": odm})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/subir_sgs", methods=["POST"])
def subir_sgs():
    """Recibe UN archivo ERP, lo guarda y retorna DFs y nodos encontrados."""
    if "archivo" not in request.files:
        return jsonify({"ok": False, "error": "No se recibio archivo"})
    archivo = request.files["archivo"]
    nombre  = archivo.filename or ""
    if not (nombre.lower().endswith(".xlsx") or nombre.lower().endswith(".xls")):
        return jsonify({"ok": False, "error": "Solo se aceptan archivos .xlsx o .xls"})

    os.makedirs(SGS_TEMP_DIR, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S%f")
    ruta = os.path.join(SGS_TEMP_DIR, f"sgs_{ts}.xlsx")
    archivo.save(ruta)

    try:
        import openpyxl
        wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
        ws = wb.active
        dfs   = set()
        nodos = set()
        last_proyecto = None
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue
            proyecto = row[0] if row[0] is not None else last_proyecto
            if row[0] is not None:
                last_proyecto = row[0]
            if proyecto:
                s = str(proyecto)
                m_df = re.search(r'(DF\d+)', s, re.IGNORECASE)
                if m_df:
                    dfs.add(m_df.group(1).upper())
                else:
                    m_lcl = re.search(r'(6300\d+)', s)
                    if m_lcl:
                        dfs.add(f"LCL{m_lcl.group(1)}")
                m_nd = re.search(r'(?:nodo|node|nd?)\s*0*(\d+)', s, re.IGNORECASE)
                if m_nd:
                    nodos.add(m_nd.group(1))
        wb.close()
        return jsonify({"ok": True, "ruta": ruta,
                        "dfs": sorted(dfs), "nodos": sorted(nodos, key=int)})
    except Exception as e:
        if os.path.isfile(ruta):
            os.remove(ruta)
        return jsonify({"ok": False, "error": f"Error al leer Excel: {e}"})


@app.route("/dfs_de_rutas", methods=["POST"])
def dfs_de_rutas():
    """Re-detecta DFs en una lista de rutas ya guardadas (para refresh tras quitar archivo)."""
    data  = request.get_json() or {}
    rutas = data.get("rutas", [])
    dfs   = set()
    try:
        import openpyxl
        for ruta in rutas:
            if not os.path.isfile(ruta):
                continue
            wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
            ws = wb.active
            last_proyecto = None
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i == 0:
                    continue
                proyecto = row[0] if row[0] is not None else last_proyecto
                if row[0] is not None:
                    last_proyecto = row[0]
                if proyecto:
                    s2 = str(proyecto)
                    m = re.search(r'(DF\d+)', s2, re.IGNORECASE)
                    if m:
                        dfs.add(m.group(1).upper())
                    else:
                        m2 = re.search(r'(6300\d+)', s2)
                        if m2:
                            dfs.add(f"LCL{m2.group(1)}")
            wb.close()
        return jsonify({"ok": True, "dfs": sorted(dfs)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/procesar_sgs", methods=["POST"])
def procesar_sgs():
    """Llama a procesar_sgs.py con una o varias rutas de archivos ERP."""
    data        = request.get_json() or {}
    id_planilla = data.get("id",       "").strip()
    rutas_sgs   = data.get("rutas_sgs", [])
    df_sgs      = data.get("df_sgs",   "").strip()

    if not id_planilla.isdigit():
        return jsonify({"ok": False, "error": "ID invalido"})
    if not rutas_sgs:
        return jsonify({"ok": False, "error": "No se recibieron archivos ERP."})
    if not df_sgs:
        return jsonify({"ok": False, "error": "Selecciona el DF de ERP a usar."})

    faltantes = [r for r in rutas_sgs if not os.path.isfile(r)]
    if faltantes:
        return jsonify({"ok": False,
                        "error": f"{len(faltantes)} archivo(s) no encontrados. Vuelve a subirlos."})

    # Nuevo formato: procesar_sgs.py <id> <df_sgs> <ruta1> [ruta2] ...
    result = subprocess.run(
        [PYTHON, os.path.join(BASE_DIR, "procesar_sgs.py"), id_planilla, df_sgs] + rutas_sgs,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=BASE_DIR, env={**os.environ, "DATA_DIR": DATA_DIR}
    )
    log = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))

    if result.returncode != 0:
        return jsonify({"ok": False, "error": "Error al procesar ERP", "log": log})

    return jsonify({"ok": True, "id": id_planilla, "log": log})


# ── Rutas espejo para /pruebas (aisladas del entorno principal) ────────────────
# Delegan a las mismas funciones — sin duplicar lógica.
# Agregar features nuevas SOLO aquí hasta que estén listas para producción.

@app.route("/pruebas/procesar", methods=["POST"])
def pruebas_procesar():
    """Igual que /procesar pero usa pruebas/balance_liquidar.py (con LCL, CDOPP, etc.)."""
    data = request.get_json()
    id_planilla = (data or {}).get("id", "").strip()
    if not id_planilla.isdigit():
        return jsonify({"ok": False, "error": "ID inválido."})

    log_lines = []
    def run(script):
        env = {**os.environ, "DATA_DIR": DATA_DIR}
        result = subprocess.run(
            [PYTHON, script, id_planilla],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=BASE_DIR, env=env
        )
        log_lines.append(f"--- {os.path.basename(script)} ---")
        if result.stdout: log_lines.append(result.stdout.strip())
        if result.stderr: log_lines.append(result.stderr.strip())
        return result.returncode

    rc1 = run(os.path.join(BASE_DIR, "descargar_acta.py"))
    rc2 = run(os.path.join(BASE_DIR, "pruebas", "balance_liquidar.py"))  # versión con LCL
    log = "\n".join(log_lines)

    if rc1 != 0 or rc2 != 0:
        return jsonify({"ok": False, "error": "Ocurrió un error al procesar.", "log": log})

    carpeta = os.path.join(DATA_DIR, id_planilla)
    return jsonify({"ok": True, "id": id_planilla, "ruta": carpeta,
                    "local": os.name == "nt", "log": log})


@app.route("/pruebas/abrir_carpeta/<id_planilla>")
def pruebas_abrir_carpeta(id_planilla):
    return abrir_carpeta(id_planilla)

@app.route("/pruebas/abrir_archivo/<id_planilla>/<nombre>")
def pruebas_abrir_archivo(id_planilla, nombre):
    return abrir_archivo(id_planilla, nombre)

@app.route("/pruebas/obtener_df_sony/<id_planilla>")
def pruebas_obtener_df_sony(id_planilla):
    return obtener_df_sony(id_planilla)


@app.route("/pruebas/obtener_info_planilla/<id_planilla>")
def pruebas_obtener_info_planilla(id_planilla):
    """Devuelve LCL, OdM, Ticket/PDL, zona y categoría (daniela/xiomara) desde el RESUMEN."""
    if not id_planilla.isdigit():
        return jsonify({"ok": False, "error": "ID invalido"})
    ruta = os.path.join(DATA_DIR, id_planilla, f"balance_liquidar_{id_planilla}.xlsx")
    if not os.path.isfile(ruta):
        return jsonify({"ok": False, "error": "Excel no encontrado"})
    try:
        import openpyxl
        wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
        ws = wb["RESUMEN"]
        info = {"lcl": "", "odm": "", "ticket": "", "zona": ""}
        for row in ws.iter_rows(values_only=True):
            label = str(row[0]).strip() if row[0] else ""
            valor = str(row[1]).strip() if row[1] else ""
            if valor == "—": valor = ""
            if label == "LCL":
                info["lcl"] = valor
            elif "OdM" in label:
                info["odm"] = valor
            elif "Ticket" in label:
                info["ticket"] = valor
            elif label.startswith("Zona"):
                info["zona"] = valor
        wb.close()
        # Categorización
        if info["lcl"].startswith("6300"):
            info["categoria"] = "daniela"
        elif info["odm"].upper().startswith("DF"):
            info["categoria"] = "xiomara"
        else:
            info["categoria"] = "desconocido"
        return jsonify({"ok": True, **info})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/pruebas/subir_sgs", methods=["POST"])
def pruebas_subir_sgs():
    return subir_sgs()

@app.route("/pruebas/dfs_de_rutas", methods=["POST"])
def pruebas_dfs_de_rutas():
    return dfs_de_rutas()


@app.route("/pruebas/procesar_sgs", methods=["POST"])
def pruebas_procesar_sgs():
    """Usa pruebas/procesar_sgs.py — categoría detectada automáticamente por LCL/OdM del RESUMEN."""
    data        = request.get_json() or {}
    id_planilla = data.get("id",        "").strip()
    rutas_sgs   = data.get("rutas_sgs", [])
    df_sgs      = data.get("df_sgs",    "").strip()  # opcional para Daniela

    if not id_planilla.isdigit():
        return jsonify({"ok": False, "error": "ID invalido"})
    if not rutas_sgs:
        return jsonify({"ok": False, "error": "No se recibieron archivos ERP."})

    faltantes = [r for r in rutas_sgs if not os.path.isfile(r)]
    if faltantes:
        return jsonify({"ok": False,
                        "error": f"{len(faltantes)} archivo(s) no encontrados. Vuelve a subirlos."})

    # Construir comando: df_sgs solo se pasa si está presente (Xiomara override)
    cmd = [PYTHON, os.path.join(BASE_DIR, "pruebas", "procesar_sgs.py"), id_planilla]
    if df_sgs:
        cmd.append(df_sgs)
    cmd.extend(rutas_sgs)

    result = subprocess.run(
        cmd,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=BASE_DIR, env={**os.environ, "DATA_DIR": DATA_DIR}
    )
    log = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
    if result.returncode != 0:
        return jsonify({"ok": False, "error": "Error al procesar ERP", "log": log})
    return jsonify({"ok": True, "id": id_planilla, "log": log})

# ── /pruebas/sgs — Balance ERP sin planilla Sony ──────────────────────────────

@app.route("/pruebas/sgs")
def pruebas_sgs():
    return Response(HTML_SGS, mimetype="text/html")


@app.route("/pruebas/sgs/generar", methods=["POST"])
def pruebas_sgs_generar():
    import uuid, shutil
    from procesar_sgs import leer_sgs_multiples, crear_balance_sgs

    df_sgs = request.form.get("df", "").strip().upper()
    if not df_sgs:
        return jsonify({"error": "DF requerido"}), 400

    files = request.files.getlist("archivos")
    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "Selecciona al menos un archivo ERP"}), 400

    # Carpeta temporal única por request
    tmp = os.path.join(DATA_DIR, "_sgs_temp", uuid.uuid4().hex)
    os.makedirs(tmp, exist_ok=True)

    try:
        rutas = []
        for f in files:
            if not f.filename:
                continue
            ruta = os.path.join(tmp, f.filename)
            f.save(ruta)
            rutas.append(ruta)

        balance_sgs, dfs, detalle_sgs = leer_sgs_multiples(rutas)

        if df_sgs not in dfs:
            disponibles = ", ".join(dfs) if dfs else "ninguno detectado"
            return jsonify({
                "error": f"DF '{df_sgs}' no encontrado en los archivos ERP. "
                         f"DFs disponibles: {disponibles}"
            }), 400

        ruta_salida = os.path.join(tmp, f"balance_SGS_{df_sgs}.xlsx")
        crear_balance_sgs(ruta_salida, balance_sgs, df_sgs, detalle_sgs)

        return send_file(
            ruta_salida,
            as_attachment=True,
            download_name=f"balance_SGS_{df_sgs}.xlsx",
            mimetype=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        # Limpiar archivos ERP de entrada (el Excel de salida Flask lo sirve primero)
        for ruta in (rutas if "rutas" in dir() else []):
            try:
                os.remove(ruta)
            except OSError:
                pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT"))

    if on_railway:
        print(f"[*] Iniciando en Railway — puerto {port}")
    else:
        url = f"http://localhost:{port}"
        print(f"[*] Abriendo interfaz en {url}")
        print("    Presiona Ctrl+C para detener.")
        threading.Thread(
            target=lambda: (time.sleep(1.2), webbrowser.open(url)),
            daemon=True
        ).start()

    app.run(host="0.0.0.0", port=port, debug=False)
