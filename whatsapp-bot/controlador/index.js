/**
 * Bot de WhatsApp — sabogal-platform (Baileys, dispositivo vinculado).
 *
 * Qué hace:
 *  1. Se vincula al número que YA está en los grupos de ENEL (QR una sola vez;
 *     la sesión queda en ./auth y sobrevive reinicios).
 *  2. Escucha los grupos VIGILADOS (se marcan en la plataforma) y todos los chats
 *     directos: texto y media (video/imagen/documento) van al inbox del backend,
 *     con la media ya descargada.
 *  3. Cada CICLO_HORAS (3 h) pide la cola de salida al backend y manda los mensajes
 *     pendientes con pausas humanas (15–45 s) — nunca en ráfaga: el número es de una
 *     persona real y no se expone.
 *
 * Config por variables de entorno (ecosystem.config.js las lee de .env.pm2):
 *   API_URL   — backend FastAPI (p. ej. http://127.0.0.1:3008/api)
 *   BOT_TOKEN — el mismo WHATSAPP_BOT_TOKEN del .env del backend
 *   NTFY_URL  — tópico ntfy para avisos operativos (vacío = sin avisos)
 */
const {
  default: makeWASocket,
  useMultiFileAuthState,
  downloadMediaMessage,
  DisconnectReason,
} = require('@whiskeysockets/baileys');
const qrcode = require('qrcode-terminal');
const qrimg = require('qrcode');   // además del ASCII, un qr.png (las terminales a veces rompen los bloques)
const pino = require('pino');
const path = require('path');

const API_URL = process.env.API_URL || 'http://127.0.0.1:3008/api';
const BOT_TOKEN = process.env.BOT_TOKEN || '';
const NTFY = process.env.NTFY_URL || '';   // vacío = sin avisos push
const CICLO_HORAS = 3;
const MAX_MEDIA_MB = 200;
// Backfill: hasta qué fecha hacia atrás pedirle historial al teléfono (epoch segundos).
const BACKFILL_DESDE = Math.floor(
  new Date(process.env.BACKFILL_DESDE || '2026-08-01T00:00:00-05:00').getTime() / 1000);

const logger = pino({ level: 'warn' });
let sock = null;
let vigilados = new Set(); // jids de grupos que sí se procesan
let qrAvisado = false;

// ── Anclas del backfill ───────────────────────────────────────────────────────
// El mensaje MÁS VIEJO conocido por chat: es el punto de partida para pedirle al
// teléfono los 50 anteriores (fetchMessageHistory). Persistido: sobrevive reinicios.
const fs = require('fs');
const ANCLAS_PATH = path.join(__dirname, 'anclas.json');
let anclas = {};
try { anclas = JSON.parse(fs.readFileSync(ANCLAS_PATH, 'utf8')); } catch { /* primera vez */ }

// JID anónimo (@lid) → teléfono real. WhatsApp no manda senderPn en TODOS los eventos de un
// mismo chat: un DM (2026-09-03) llegó primero cifrado (con senderPn) y después
// legible (sin él), y el backend no lo pudo cruzar con el contacto. Cada vez que un evento
// trae la pareja se memoriza; cuando falta, se usa lo memorizado. Persistido.
const LIDS_PATH = path.join(__dirname, 'lids.json');
let lids = {};
try { lids = JSON.parse(fs.readFileSync(LIDS_PATH, 'utf8')); } catch { /* primera vez */ }
function aprenderLid(key) {
  if (!key) return;
  const pares = [[key.remoteJid, key.remoteJidAlt || key.senderPn],
                 [key.participant, key.participantAlt || key.participantPn]];
  let cambio = false;
  for (const [lid, pn] of pares) {
    if (lid && pn && lid.endsWith('@lid') && pn.endsWith('@s.whatsapp.net') && lids[lid] !== pn) {
      lids[lid] = pn; cambio = true;
    }
  }
  if (cambio) fs.writeFile(LIDS_PATH, JSON.stringify(lids), () => {});
}
const telefonoDe = (jid, alt) => alt || (jid && lids[jid]) || jid;

// HORA ORIGINAL de los mensajes que no se descifran al primer intento (2026-10-01). Baileys
// entrega primero un stub CIPHERTEXT con la hora real (stanza t) y, tras el reintento, el
// mensaje legible con la hora del REENVÍO (una respuesta de las 9:00 quedaba a las 9:06, después
// del mensaje propio de las 9:05). Se guarda la hora del stub y se usa la menor. Persistido
// por si el bot se reinicia entre el stub y el reenvío; se olvida a los 3 días.
const CIPHERTEXT = 2;                          // proto.WebMessageInfo.StubType.CIPHERTEXT
const HORAS_PATH = path.join(__dirname, 'horas_originales.json');
let horasOriginales = {};
try { horasOriginales = JSON.parse(fs.readFileSync(HORAS_PATH, 'utf8')); } catch { /* primera vez */ }
function recordarHoraOriginal(msg) {
  const id = msg.key?.id;
  const ts = Number(msg.messageTimestamp);
  if (!id || !ts || msg.messageStubType !== CIPHERTEXT) return;
  if (horasOriginales[id] && horasOriginales[id] <= ts) return;
  horasOriginales[id] = ts;
  const limite = Date.now() / 1000 - 3 * 86400;
  for (const [k, v] of Object.entries(horasOriginales)) if (v < limite) delete horasOriginales[k];
  fs.writeFile(HORAS_PATH, JSON.stringify(horasOriginales), () => {});
}
const horaReal = (msg) => Math.min(Number(msg.messageTimestamp), horasOriginales[msg.key?.id] || Infinity);

function registrarAncla(msg) {
  const jid = msg.key?.remoteJid;
  const ts = Number(msg.messageTimestamp);
  if (!jid || !ts || !jid.endsWith('@g.us')) return;   // todos los grupos (26-09)
  if (!anclas[jid] || ts < anclas[jid].ts) {
    anclas[jid] = {
      key: { remoteJid: jid, id: msg.key.id, fromMe: !!msg.key.fromMe, participant: msg.key.participant },
      ts,
    };
    fs.writeFile(ANCLAS_PATH, JSON.stringify(anclas), () => {});
  }
}

// ── Backend ───────────────────────────────────────────────────────────────────

async function api(ruta, opciones = {}) {
  const res = await fetch(`${API_URL}/wa-bot${ruta}`, {
    ...opciones,
    headers: { 'X-Bot-Token': BOT_TOKEN, ...(opciones.headers || {}) },
  });
  if (!res.ok) throw new Error(`${ruta} → HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}

async function ntfy(mensaje) {
  if (!NTFY) return;
  try {
    await fetch(NTFY, {
      method: 'POST',
      body: mensaje,
      headers: { Title: 'Bot WhatsApp Sabogal', Priority: 'high' },
    });
  } catch { /* sin red no hay aviso, pero el bot sigue */ }
}

// ── Entrada: mensajes de TODOS los grupos y chats directos ──────────────────

const TIPOS_MEDIA = {
  imageMessage: ['imagen', 'jpg'],
  videoMessage: ['video', 'mp4'],
  documentMessage: ['documento', 'bin'],
  audioMessage: ['audio', 'ogg'],
};

function extraerContenido(msg) {
  const m = msg.message || {};
  const texto =
    m.conversation ||
    m.extendedTextMessage?.text ||
    m.imageMessage?.caption ||
    m.videoMessage?.caption ||
    m.documentMessage?.caption ||
    null;
  for (const [key, [tipo, ext]] of Object.entries(TIPOS_MEDIA)) {
    if (m[key]) {
      const nombre = m.documentMessage?.fileName || `${tipo}.${ext}`;
      const bytes = Number(m[key].fileLength || 0);
      return { tipo, texto, media: { key, nombre, mime: m[key].mimetype, bytes } };
    }
  }
  return { tipo: 'texto', texto, media: null };
}

async function procesarMensaje(msg, historico = false) {
  if (!msg.message) return;
  const chatJid = msg.key.remoteJid || '';
  if (chatJid === 'status@broadcast') return;
  const esGrupo = chatJid.endsWith('@g.us');
  // Se guarda TODO lo disponible (decisión 2026-09-26): todos los grupos, vigilados o
  // no, y también lo propio dentro de los grupos (queda con de_mi=true). «Vigilado» ya no
  // decide qué se guarda: solo en qué grupos busca el analista de IA (wa_analista.py).

  const { tipo, texto, media } = extraerContenido(msg);
  if (!texto && !media) return; // stickers, reacciones, etc.

  // En lotes históricos/backfill, preguntar antes de descargar media: bajar un video
  // para que el dedupe del inbox lo tire es ancho de banda desperdiciado.
  if (historico && media) {
    try {
      const r = await api(`/inbox/existe?wa_id=${encodeURIComponent(msg.key.id)}`);
      if (r.existe) return;
    } catch { /* si el backend no contesta, seguimos por el camino normal */ }
  }

  const form = new FormData();
  form.set('wa_id', msg.key.id);
  form.set('chat_jid', chatJid);
  form.set('es_grupo', String(esGrupo));
  form.set('de_mi', String(Boolean(msg.key.fromMe)));
  // WhatsApp a veces manda chats con JID anónimo (@lid) en vez del teléfono; sin el
  // número real el backend no puede amarrar la respuesta al contacto. En Baileys
  // 7.0.0-rc14 el teléfono viene en key.senderPn / key.participantPn (attrs sender_pn /
  // participant_pn del stanza) — los campos *Alt no existen en esta versión y por eso
  // las respuestas de los ingenieros quedaban huérfanas; se dejan como preferencia por
  // si un upgrade futuro los trae.
  if (msg.key.participant) {
    form.set('remitente_jid', telefonoDe(msg.key.participant, msg.key.participantAlt || msg.key.participantPn));
  } else if (!esGrupo) {
    form.set('remitente_jid', telefonoDe(chatJid, msg.key.remoteJidAlt || msg.key.senderPn));
  }
  // Respuesta CITANDO otro mensaje (deslizar): el id citado permite el match exacto
  // con el caso del envío citado.
  const ctxInfo = Object.values(msg.message || {}).map((v) => v?.contextInfo).find(Boolean);
  if (ctxInfo?.stanzaId) form.set('quote_wa_id', ctxInfo.stanzaId);
  if (msg.pushName) form.set('remitente_nombre', msg.pushName);
  form.set('tipo', tipo);
  if (texto) form.set('texto', texto);
  form.set('enviado_en', new Date(horaReal(msg) * 1000).toISOString());

  if (media) {
    if (media.bytes > MAX_MEDIA_MB * 1024 * 1024) {
      console.log(`[inbox] media de ${media.bytes} bytes supera el techo, se registra sin archivo`);
    } else {
      try {
        // reuploadRequest: si la media ya expiró en los servidores de WhatsApp (pasa con
        // el historial), se le pide al teléfono que la re-suba antes de rendirse.
        const buffer = await downloadMediaMessage(msg, 'buffer', {},
          { logger, reuploadRequest: sock.updateMediaMessage });
        form.set('media', new Blob([buffer], { type: media.mime || 'application/octet-stream' }), media.nombre);
      } catch (e) {
        console.log(`[inbox] no se pudo descargar media de ${msg.key.id}: ${e.message}`);
      }
    }
  }

  await api('/inbox', { method: 'POST', body: form });
  console.log(`[inbox] ${esGrupo ? 'grupo' : 'directo'} ${chatJid} · ${tipo}${texto ? ` · "${texto.slice(0, 60)}"` : ''}`);
  // Un DM entrante puede ser la respuesta de un ingeniero a una consulta: en ~90 s el
  // backend lo amarra a su hilo y la IA actualiza la bitácora — sin manos humanas.
  // Lo PROPIO también (2026-10-01): lo que el operador contesta a mano desde WhatsApp es la otra
  // mitad de la conversación. El backend reconoce y salta los envíos del propio bot.
  if (!esGrupo && !historico) programarRespuestas();
}

// ── Respuestas: pedirle al backend que procese los DM nuevos ─────────────────
// Con debounce de 90 s: si el ingeniero manda varios mensajes seguidos (texto + la foto
// del correo), se procesan juntos como una sola respuesta, no a pedazos.
let respuestasTimer = null;

async function procesarRespuestas() {
  try {
    const r = await api('/procesar-respuestas', { method: 'POST' });
    if (r.procesados) console.log(`[respuestas] ${r.procesados} respuesta(s) procesadas y registradas en su hilo`);
  } catch (e) {
    console.log(`[respuestas] ${e.message}`);
  }
}

function programarRespuestas() {
  clearTimeout(respuestasTimer);
  respuestasTimer = setTimeout(procesarRespuestas, 90 * 1000);
}

// ── Grupos: sincronizar lista y recibir cuáles vigilar ────────────────────────

async function sincronizarGrupos() {
  const todos = await sock.groupFetchAllParticipating();
  const lista = Object.values(todos).map((g) => ({ jid: g.id, nombre: g.subject }));
  const r = await api('/grupos/sync', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(lista),
  });
  vigilados = new Set(r.vigilados);
  console.log(`[grupos] ${lista.length} grupos sincronizados, ${vigilados.size} vigilados`);
}

// ── Salida: la cola de la plataforma, con pausas humanas ─────────────────────

const pausa = (ms) => new Promise((r) => setTimeout(r, ms));

let enviando = false;   // candado: dos ciclos a la vez mandaban cada mensaje dos veces (2026-09-23)

async function cicloEnvios(silencioso = false) {
  if (enviando) { if (!silencioso) console.log('[cola] ciclo anterior aún en curso, se omite'); return; }
  enviando = true;
  try { await _cicloEnvios(silencioso); } finally { enviando = false; }
}

async function _cicloEnvios(silencioso = false) {
  let cola;
  try {
    cola = await api('/cola');
  } catch (e) {
    console.log(`[cola] backend no disponible: ${e.message}`);
    return;
  }
  if (!cola.length) {
    // El chequeo frecuente (cada 5 min) calla cuando no hay nada — sin spam en el log
    if (!silencioso) console.log('[cola] sin pendientes');
    return;
  }
  console.log(`[cola] ${cola.length} mensajes por enviar`);
  for (const envio of cola) {
    try {
      const jid = `${envio.destino.replace(/\D/g, '')}@s.whatsapp.net`;
      let sent;
      if (envio.campo === 'revocar') {
        // «Eliminar para todos» un mensaje nuestro: el texto trae el wa_id a borrar (2026-09-23,
        // duplicados por dos ciclos simultáneos tras la re-vinculación)
        sent = await sock.sendMessage(jid, { delete: { remoteJid: jid, fromMe: true, id: envio.texto } });
      } else {
        sent = await sock.sendMessage(jid, { text: envio.texto });
      }
      await api(`/cola/${envio.id}/resultado`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // wa_id del mensaje enviado: si la persona responde CITÁNDOLO, el backend amarra
        // la respuesta a este caso con certeza.
        body: JSON.stringify({ ok: true, wa_id: sent?.key?.id || null }),
      });
      console.log(`[cola] ${envio.campo === 'revocar' ? 'revocado (eliminar para todos) en' : 'enviado a'} ${envio.persona || envio.destino}`);
    } catch (e) {
      await api(`/cola/${envio.id}/resultado`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ok: false, error: e.message }),
      }).catch(() => {});
      console.log(`[cola] error con ${envio.destino}: ${e.message}`);
    }
    // Pausa humana entre mensajes: el número es de una persona real
    await pausa(15000 + Math.random() * 30000);
  }
}

// ── Backfill bajo demanda ─────────────────────────────────────────────────────
// Si el paquete de historial de la vinculación no alcanzó BACKFILL_DESDE en algún
// grupo vigilado, se le piden al teléfono los 50 mensajes anteriores al ancla, por
// rondas, hasta cubrir la fecha o hasta que el chat no dé más. Los resultados llegan
// por messaging-history.set (el mismo camino idempotente de siempre).
const backfillSinAvance = {};   // jid → rondas seguidas sin traer nada más viejo

let backfillCorriendo = false;
async function pasoBackfill() {
  if (backfillCorriendo) return;
  backfillCorriendo = true;
  try { await rondaBackfill(); } finally { backfillCorriendo = false; }
}

async function rondaBackfill() {
  if (!sock?.user || typeof sock.fetchMessageHistory !== 'function') return;
  let pendientes = 0;
  for (const jid of Object.keys(anclas)) {   // todos los grupos con ancla, no solo vigilados
    const a = anclas[jid];
    if (!a || a.ts <= BACKFILL_DESDE || (backfillSinAvance[jid] || 0) >= 2) continue;
    pendientes++;
    const antes = a.ts;
    try {
      await sock.fetchMessageHistory(50, a.key, a.ts);
    } catch (e) {
      console.log(`[backfill] ${jid}: ${e.message}`);
    }
    await pausa(20000);   // dar tiempo a que el lote llegue y se procese
    if ((anclas[jid]?.ts ?? antes) < antes) {
      backfillSinAvance[jid] = 0;                       // avanzó: hay más historial
    } else {
      backfillSinAvance[jid] = (backfillSinAvance[jid] || 0) + 1;
      if (backfillSinAvance[jid] >= 2) {
        console.log(`[backfill] ${jid} no da más historial (llegó hasta ${new Date(antes * 1000).toISOString().slice(0, 10)})`);
      }
    }
  }
  if (pendientes) {
    console.log(`[backfill] ${pendientes} grupos aún por detrás del objetivo, otra ronda en 2 min`);
    setTimeout(pasoBackfill, 120 * 1000);
  }
}

// ── Conexión ──────────────────────────────────────────────────────────────────

async function conectar() {
  const { state, saveCreds } = await useMultiFileAuthState(path.join(__dirname, 'auth'));
  // syncFullHistory: al VINCULARSE (solo en ese momento) WhatsApp entrega el historial
  // de chats — es la única vía para traer los meses pasados de los grupos.
  sock = makeWASocket({ auth: state, logger, markOnlineOnConnect: false, syncFullHistory: true });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      qrcode.generate(qr, { small: true });
      qrimg.toFile(path.join(__dirname, 'qr.png'), qr, { scale: 8, margin: 2 })
        .catch((e) => console.log(`[qr] no se pudo escribir qr.png: ${e.message}`));
      console.log('[qr] Escanea el QR de arriba (o el qr.png de esta carpeta): WhatsApp → Dispositivos vinculados');
      if (!qrAvisado) {
        qrAvisado = true;
        await ntfy('El bot necesita vincularse: corre "pm2 logs whatsapp-bot" en el VPS y escanea el QR (WhatsApp > Dispositivos vinculados)');
      }
    }
    if (connection === 'open') {
      qrAvisado = false;
      require('fs').unlink(path.join(__dirname, 'qr.png'), () => {}); // que no quede un QR viejo escaneable
      console.log('[conexión] vinculado y en línea');
      await ntfy('Bot de WhatsApp vinculado y escuchando');
      try { await sincronizarGrupos(); } catch (e) { console.log(`[grupos] ${e.message}`); }
      await cicloEnvios();
      await procesarRespuestas();   // por si quedó alguna respuesta pendiente de un reinicio
      // El backfill arranca tarde a propósito: primero deja que el paquete de historial
      // de la vinculación (si lo hay) llegue y siembre las anclas.
      setTimeout(pasoBackfill, 10 * 60 * 1000);
    }
    if (connection === 'close') {
      const codigo = lastDisconnect?.error?.output?.statusCode;
      if (codigo === DisconnectReason.loggedOut) {
        console.log('[conexión] sesión cerrada desde el teléfono — hay que re-escanear');
        // 2026-09-23: con las credenciales viejas en `auth`, el reinicio volvía a caer en
        // loggedOut sin pedir QR (1.565 reinicios en bucle, 15 mensajes aprobados sin salir).
        // Se aparta la sesión vencida para que el siguiente arranque muestre el QR.
        try {
          const fs = require('fs');
          const vencida = path.join(__dirname, `auth.bak-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}`);
          fs.renameSync(path.join(__dirname, 'auth'), vencida);
          console.log(`[conexión] sesión vencida apartada en ${path.basename(vencida)}`);
        } catch (e) { console.log(`[conexión] no se pudo apartar auth: ${e.message}`); }
        await ntfy('La sesión del bot fue cerrada desde el teléfono: hay que re-vincular (pm2 logs sabogal-wa-bot)');
        process.exit(1); // pm2 lo levanta y pedirá QR de nuevo
      }
      console.log(`[conexión] caída (código ${codigo}), reconectando…`);
      setTimeout(conectar, 5000);
    }
  });

  // Historial: llega por lotes tras vincularse. Se procesa con el mismo camino que lo
  // vivo (el inbox es idempotente por wa_id, así que repetir lotes no duplica nada).
  sock.ev.on('messaging-history.set', async ({ messages, progress }) => {
    console.log(`[historia] lote de ${messages.length} mensajes (progreso ${progress ?? '?'}%)`);
    if (!vigilados.size) {
      try { await sincronizarGrupos(); } catch (e) { console.log(`[grupos] ${e.message}`); }
    }
    let subidos = 0;
    for (const msg of messages) {
      registrarAncla(msg);
      try {
        await procesarMensaje(msg, true);
        subidos++;
      } catch (e) {
        console.log(`[historia] error con ${msg.key?.id}: ${e.message}`);
      }
    }
    console.log(`[historia] lote procesado (${subidos}/${messages.length})`);
  });

  sock.ev.on('messages.upsert', async ({ messages }) => {
    for (const msg of messages) {
      aprenderLid(msg.key);
      recordarHoraOriginal(msg);
      registrarAncla(msg);
      try {
        await procesarMensaje(msg);
      } catch (e) {
        console.log(`[inbox] error con ${msg.key?.id}: ${e.message}`);
      }
    }
  });
}

// El ciclo de 3 h: refrescar grupos vigilados + vaciar la cola de salida + procesar
// las respuestas que hayan llegado (barrido de seguridad; lo normal es que el debounce
// de 90 s ya las haya procesado al llegar)
setInterval(async () => {
  if (!sock?.user) return; // sin sesión no hay ciclo
  try { await sincronizarGrupos(); } catch (e) { console.log(`[grupos] ${e.message}`); }
  await cicloEnvios();
  await procesarRespuestas();
  pasoBackfill();   // grupos que entraron después del arranque (p. ej. los no vigilados)
}, CICLO_HORAS * 3600 * 1000);

// La COLA se chequea cada 5 min (requisito 2026-08-24: lo aprobado esperaba hasta
// 3 h sentado). Chequeo silencioso y barato — los envíos conservan sus pausas humanas.
setInterval(() => {
  if (sock?.user) cicloEnvios(true);
}, 5 * 60 * 1000);

if (!BOT_TOKEN) {
  console.error('Falta BOT_TOKEN en el entorno — el bot no puede hablar con el backend');
  process.exit(1);
}
conectar();
