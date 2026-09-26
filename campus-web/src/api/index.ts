/**
 * Axios 层统一出口。
 *
 * 只聚合「基于 Axios 的基础设施」（实例与常量）。
 * 业务接口**不挂在这里**：`./exams` 是本阶段唯一一个真实业务调用，
 * 它不使用 Axios（原因见 `./exams` 顶部注释），因此保持顶层模块、
 * 由调用方从 `@/api/exams` 直接引入，避免「拿基础设施却顺带拉起业务请求」的隐式耦合。
 */
export { http, createHttpClient } from './http'
export { API_DEFAULT_HEADERS, API_TIMEOUT_MS } from './constants'
