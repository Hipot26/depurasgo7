import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../utils/api'
import toast from 'react-hot-toast'
import { Lock } from 'lucide-react'

const key = (z, t) => `${z}|${t}`

export default function DimensionTab({ unidad, cfg, opciones, puedeEditar }) {
  const qc = useQueryClient()
  const zonas = opciones?.zonas?.length ? opciones.zonas : cfg.zonas
  const tipos = cfg.tipoOptions.filter(Boolean)

  const { data } = useQuery({
    queryKey: ['dimension', unidad],
    queryFn: () => api.get('/produccion/dimension', { params: { unidad } }).then((r) => r.data),
  })

  const actMap = {}
  ;(data?.activas || []).forEach((a) => { actMap[key(a.zona, a.tipo_grupo)] = a.n })
  const reqServer = {}
  ;(data?.requeridas || []).forEach((r) => { reqServer[key(r.zona, r.tipo_grupo)] = r.requeridas })

  // estado local editable de "requeridas"
  const [req, setReq] = useState({})
  useEffect(() => { setReq(reqServer) }, [data])  // eslint-disable-line

  const save = useMutation({
    mutationFn: ({ zona, tipo_grupo, requeridas }) =>
      api.put('/produccion/dimension', { unidad, zona, tipo_grupo, requeridas }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['dimension', unidad] }); toast.success('Guardado', { duration: 700 }) },
    onError: (e) => toast.error(e.response?.data?.detail || 'Error al guardar'),
  })

  const onBlur = (zona, tipo) => {
    const v = parseInt(req[key(zona, tipo)] || 0, 10) || 0
    if (v !== (reqServer[key(zona, tipo)] || 0)) save.mutate({ zona, tipo_grupo: tipo, requeridas: v })
  }

  const getReq = (z, t) => (req[key(z, t)] ?? '')
  const getAct = (z, t) => actMap[key(z, t)] || 0

  // Totales
  const totZonaReq = (z) => tipos.reduce((s, t) => s + (parseInt(req[key(z, t)] || 0, 10) || 0), 0)
  const totZonaAct = (z) => tipos.reduce((s, t) => s + getAct(z, t), 0)
  const totTipoReq = (t) => zonas.reduce((s, z) => s + (parseInt(req[key(z, t)] || 0, 10) || 0), 0)
  const totTipoAct = (t) => zonas.reduce((s, z) => s + getAct(z, t), 0)
  const totalReq = tipos.reduce((s, t) => s + totTipoReq(t), 0)
  const totalAct = tipos.reduce((s, t) => s + totTipoAct(t), 0)

  const brechaCell = (act, r) => {
    const b = act - r
    if (r === 0 && act === 0) return <span className="text-gray-300">·</span>
    return <span className={`text-[10px] font-semibold ${b >= 0 ? 'text-green-600' : 'text-red-600'}`}>{b > 0 ? `+${b}` : b}</span>
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <p className="text-sm text-gray-600">
          Cuadrillas <b>requeridas</b> (lo defines tú) vs <b>activas</b> (calculadas de la programación). La brecha = activas − requeridas.
        </p>
        {!puedeEditar && <span className="flex items-center gap-1 text-[11px] text-gray-400"><Lock size={11} /> solo lectura</span>}
      </div>

      <div className="bg-white rounded-xl border shadow-sm overflow-auto">
        <table className="w-full text-[11px] border-collapse">
          <thead>
            <tr className="bg-gray-50">
              <th className="text-left px-3 py-2 font-semibold text-gray-600 border-b border-r sticky left-0 bg-gray-50 min-w-[120px]">Tipo de cuadrilla</th>
              {zonas.map((z) => (
                <th key={z} className="px-2 py-2 text-center font-semibold text-gray-700 border-b border-r" colSpan={1}>{z}</th>
              ))}
              <th className="px-2 py-2 text-center font-semibold text-gray-700 border-b bg-blue-50">TOTAL</th>
            </tr>
            <tr className="bg-gray-50 text-[9px] text-gray-400">
              <th className="border-b border-r sticky left-0 bg-gray-50" />
              {zonas.map((z) => <th key={z} className="px-2 py-0.5 border-b border-r font-medium">Req · Act · Δ</th>)}
              <th className="px-2 py-0.5 border-b bg-blue-50 font-medium">Req · Act</th>
            </tr>
          </thead>
          <tbody>
            {tipos.map((t) => (
              <tr key={t} className="border-b hover:bg-blue-50/30">
                <td className="px-3 py-1.5 font-medium text-gray-800 border-r sticky left-0 bg-white">{t}</td>
                {zonas.map((z) => (
                  <td key={z} className="px-2 py-1 text-center border-r">
                    <div className="flex items-center justify-center gap-1.5">
                      <input
                        type="number" min="0"
                        value={getReq(z, t)}
                        disabled={!puedeEditar}
                        onChange={(e) => setReq((p) => ({ ...p, [key(z, t)]: e.target.value }))}
                        onBlur={() => onBlur(z, t)}
                        placeholder="0"
                        className="w-10 text-center border rounded px-1 py-0.5 text-[11px] focus:outline-none focus:ring-1 focus:ring-blue-400 disabled:bg-gray-50 disabled:text-gray-400"
                      />
                      <span className="text-gray-400">{getAct(z, t)}</span>
                      {brechaCell(getAct(z, t), parseInt(req[key(z, t)] || 0, 10) || 0)}
                    </div>
                  </td>
                ))}
                <td className="px-2 py-1 text-center border-l bg-blue-50/40 font-semibold text-gray-700">
                  {totTipoReq(t)} · {totTipoAct(t)}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="bg-gray-100 font-semibold">
              <td className="px-3 py-2 text-gray-700 border-r sticky left-0 bg-gray-100">TOTAL</td>
              {zonas.map((z) => (
                <td key={z} className="px-2 py-2 text-center border-r text-gray-700">{totZonaReq(z)} · {totZonaAct(z)}</td>
              ))}
              <td className="px-2 py-2 text-center bg-blue-100 text-blue-800">{totalReq} · {totalAct}</td>
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="text-[11px] text-gray-400">
        Δ = activas − requeridas. Verde = cubierto o de sobra · Rojo = faltan cuadrillas. Escribe el número en "Req" y se guarda al salir del campo.
      </p>
    </div>
  )
}
