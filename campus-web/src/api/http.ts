import axios, { type AxiosInstance, type InternalAxiosRequestConfig, type AxiosResponse } from 'axios'

import { API_DEFAULT_HEADERS, API_TIMEOUT_MS } from './constants'

/**
 * CampusFlow 唯一的 Axios 实例。
 *
 * 本阶段只做“基础实例封装”：
 * - 不接真实后端（baseURL 默认留空，由 VITE_API_BASE_URL 提供）
 * - 不写 Agent API / 登录 API / 业务接口
 * - 拦截器只做最基础的透传与错误归一化
 */
export function createHttpClient(): AxiosInstance {
  const instance = axios.create({
    baseURL: import.meta.env.VITE_API_BASE_URL ?? '',
    timeout: API_TIMEOUT_MS,
    headers: { ...API_DEFAULT_HEADERS },
  })

  instance.interceptors.request.use((config: InternalAxiosRequestConfig) => config)

  instance.interceptors.response.use(
    (response: AxiosResponse) => response,
    (error: unknown) => Promise.reject(error),
  )

  return instance
}

/** 全局共享实例 */
export const http: AxiosInstance = createHttpClient()
