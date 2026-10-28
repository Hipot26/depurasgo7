// PM2 — bot de WhatsApp. El BOT_TOKEN real NO va aquí (este archivo viaja con el
// código): vive en /var/www/sabogal-platform/whatsapp-bot/.env.pm2 en el VPS y se
// inyecta al arrancar (el bloque de abajo lo lee sin dependencias externas).
// Arranque: pm2 start ecosystem.config.js
module.exports = {
  apps: [
    {
      // Nombre del proceso en PM2.
      name: 'sabogal-wa-bot',
      script: 'index.js',
      cwd: __dirname,
      autorestart: true,
      max_restarts: 50,
      restart_delay: 10000,
      // API_URL, BOT_TOKEN y NTFY_URL se leen de .env.pm2 (fuera del repositorio).
      env: {
        ...Object.fromEntries(
          (() => {
            try {
              return require('fs')
                .readFileSync(require('path').join(__dirname, '.env.pm2'), 'utf8')
                .split('\n')
                .filter((l) => l.includes('=') && !l.trim().startsWith('#'))
                .map((l) => [l.slice(0, l.indexOf('=')).trim(), l.slice(l.indexOf('=') + 1).trim()]);
            } catch {
              return [];
            }
          })()
        ),
      },
    },
  ],
};
