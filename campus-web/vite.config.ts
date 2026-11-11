import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 后端服务地址：**仅 Vite 开发服务器使用**（写进浏览器的是相对路径，见下）。
 *
 * - 默认与 `campus-service` 的 `SERVER_PORT` 默认值一致（8080）；
 * - 若后端换端口启动，用 `CAMPUS_API_TARGET` 覆盖，**不要改这个默认值**：
 *      $env:CAMPUS_API_TARGET='http://127.0.0.1:8091'; npm run dev
 * - 刻意**不读 `VITE_API_BASE_URL`**：那个变量是给浏览器里的代码看的
 *   （决定发相对路径还是绝对 URL），把它和代理目标绑在一起会导致
 *   「一改就把两边同时改坏」，两个概念必须分开。
 */
const CAMPUS_API_TARGET = process.env.CAMPUS_API_TARGET ?? 'http://127.0.0.1:8080'

/**
 * Agent 服务地址：**仅 Vite 开发服务器使用**。
 *
 * Python Agent（FastAPI）默认监听 8000 端口（见 `app/api/server.py`）。
 * `/api/agent` 前缀的请求走这条代理到 Agent 服务；其余 `/api` 仍走 Spring Boot。
 * 代理目标可用 `AGENT_API_TARGET` 环境变量覆盖。
 */
const AGENT_API_TARGET = process.env.AGENT_API_TARGET ?? 'http://127.0.0.1:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    /**
     * 反代规则（绕开 CORS 的关键）：
     * - `/api/agent/*` → Python Agent 服务（FastAPI，默认 8000 端口）
     * - `/api/*`（其余）→ Spring Boot 业务服务（默认 8080 端口）
     *
     * 后端都只监听回环地址且没有 CORS 配置，浏览器直接请求会被拦
     * （实测 `net::ERR_FAILED`）。改为同源 `/api/...`，由开发服务器在
     * Node 侧转发，请求就不再有跨源属性。
     *
     * 生产环境应由网关 / 反向代理承担同样的同源暴露职责。
     */
    proxy: {
      // Agent 服务（Python FastAPI）：更具体的路径在前，优先匹配
      '/api/agent': {
        target: AGENT_API_TARGET,
        changeOrigin: true,
      },
      // Spring Boot 业务服务：兜底匹配其余 /api 路径
      '/api': {
        target: CAMPUS_API_TARGET,
        changeOrigin: true,
      },
    },
  },
})
