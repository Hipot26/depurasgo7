import { create } from 'zustand'
import { limpiarConfirmacionesUnidad } from '../utils/unidadAjena'
import { persist } from 'zustand/middleware'

export const useAuthStore = create(
  persist(
    (set) => ({
      token: null,
      usuario: null,
      setAuth: (token, usuario) => set({ token, usuario }),
      logout: () => { limpiarConfirmacionesUnidad(); set({ token: null, usuario: null }) },
    }),
    { name: 'sabogal-auth' }
  )
)
