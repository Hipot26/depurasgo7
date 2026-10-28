import { useState, useEffect } from 'react'
import { useAuthStore } from '../../store/authStore'
import api from '../../utils/api'
import LoginPage from './LoginPage'
import { Lock, Loader2, ShieldCheck } from 'lucide-react'

// Pantalla neutra para dispositivos no aprobados (sin marca, sin formulario de login a la vista)
function RestrictedScreen() {
  const [nombre, setNombre] = useState('')
  const [sent, setSent] = useState(false)
  const [estado, setEstado] = useState(null)
  const [loading, setLoading] = useState(false)

  const solicitar = async () => {
    setLoading(true)
    try {
      const { data } = await api.post('/acceso/solicitar', { nombre: nombre.trim() || null })
      setEstado(data.estado)
      setSent(true)
    } catch { setSent(true) }
    finally { setLoading(false) }
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-800 to-slate-900 flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md overflow-hidden">
        <div className="bg-slate-900 px-8 py-8 text-center">
          <Lock size={36} className="text-slate-300 mx-auto mb-2" />
          <h1 className="text-white text-xl font-bold">Acceso restringido</h1>
          <p className="text-slate-400 text-sm mt-1">Sistema privado · solo personal autorizado</p>
        </div>

        <div className="px-8 py-7">
          {sent ? (
            <div className="text-center space-y-2">
              <ShieldCheck size={32} className="text-green-600 mx-auto" />
              <p className="text-sm text-gray-700 font-medium">Solicitud enviada</p>
              <p className="text-xs text-gray-500">
                Tu solicitud quedó <b>{estado === 'aprobado' ? 'aprobada' : 'en espera de aprobación'}</b>.
                El administrador la revisará. Vuelve a intentar más tarde.
              </p>
              <button onClick={() => window.location.reload()} className="mt-2 text-xs text-blue-600 hover:underline">
                Reintentar acceso
              </button>
            </div>
          ) : (
            <div className="space-y-4">
              <p className="text-sm text-gray-600">
                Este dispositivo no está autorizado. Solicita acceso al administrador para poder ingresar.
              </p>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Tu nombre (para identificarte)</label>
                <input
                  value={nombre}
                  onChange={(e) => setNombre(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-500"
                  placeholder="ej. Juan Pérez"
                />
              </div>
              <button
                onClick={solicitar}
                disabled={loading}
                className="w-full bg-slate-800 hover:bg-slate-900 text-white font-semibold py-2.5 rounded-lg transition-colors disabled:opacity-60 flex items-center justify-center gap-2"
              >
                {loading ? <Loader2 size={16} className="animate-spin" /> : null}
                Solicitar acceso
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default function AccessGate() {
  const token = useAuthStore((s) => s.token)
  const [estado, setEstado] = useState('cargando')  // cargando | aprobado | restringido

  useEffect(() => {
    if (token) { setEstado('aprobado'); return }
    let alive = true
    api.get('/acceso/estado')
      .then(({ data }) => { if (alive) setEstado(data.estado === 'aprobado' ? 'aprobado' : 'restringido') })
      .catch(() => { if (alive) setEstado('restringido') })
    return () => { alive = false }
  }, [token])

  if (estado === 'cargando') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900 text-slate-300">
        <Loader2 size={22} className="animate-spin" />
      </div>
    )
  }

  // Dispositivo aprobado → login normal del equipo
  if (estado === 'aprobado') return <LoginPage />

  return <RestrictedScreen />
}
