import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * 应用级状态（占位）。
 * 当前只承载应用标题与访问计数，用于验证 Pinia 已正确接入；
 * 后续路由守卫 / 布局可直接消费，避免改动主入口。
 */
export const useAppStore = defineStore('app', () => {
  const appTitle = ref(import.meta.env.VITE_APP_TITLE ?? 'CampusFlow')
  const appSubtitle = ref('智校园 · 高校智能事务 Agent 平台')
  const visitCount = ref(0)

  const displayTitle = computed(() => appTitle.value)

  function increaseVisitCount() {
    visitCount.value += 1
  }

  function reset() {
    appTitle.value = 'CampusFlow'
    appSubtitle.value = '智校园 · 高校智能事务 Agent 平台'
    visitCount.value = 0
  }

  return { appTitle, appSubtitle, displayTitle, visitCount, increaseVisitCount, reset }
})
