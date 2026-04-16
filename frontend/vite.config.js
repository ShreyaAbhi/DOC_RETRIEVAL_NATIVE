import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig(({ mode }) => {
  // Load .env from project root (one level above frontend/)
  const env = loadEnv(mode, path.resolve(process.cwd(), '..'), '')
  const frontendPort = parseInt(env.FRONTEND_PORT) || 5174
  const backendPort = parseInt(env.BACKEND_PORT) || 8002

  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      port: frontendPort,
      proxy: {
        '/api': { target: `http://localhost:${backendPort}`, changeOrigin: true },
        '/ws':  { target: `ws://localhost:${backendPort}`,  ws: true, changeOrigin: true },
      },
    },
  }
})
