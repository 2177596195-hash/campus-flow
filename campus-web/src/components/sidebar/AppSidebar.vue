<script setup lang="ts">
/**
 * 侧边栏：品牌区 + 新对话 + 对话历史 + 底部用户区占位。
 *
 * 本组件是「状态从哪来」的边界：内部直接读写 conversation store，
 * 主区域因此不必把整份会话列表再往下透传。
 *
 * 窄屏行为：`open` 由父级（ChatView）控制，用于把侧边栏收起为抽屉。
 */

import ConversationList from './ConversationList.vue'
import NewConversationButton from './NewConversationButton.vue'
import { useAppStore } from '@/stores/app'
import { useConversationStore } from '@/stores/conversation'

defineProps<{
  /** 窄屏抽屉是否展开（宽屏下恒为可见，不依赖该值） */
  open: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const appStore = useAppStore()
const conversationStore = useConversationStore()

function handleCreate() {
  conversationStore.createConversation()
  emit('close')
}

function handleSelect(id: string) {
  conversationStore.selectConversation(id)
  emit('close')
}
</script>

<template>
  <aside class="sidebar" :class="{ 'sidebar--open': open }">
    <div class="sidebar__brand">
      <span class="sidebar__mark" aria-hidden="true">CF</span>
      <span class="sidebar__brand-text">
        <strong class="sidebar__name">{{ appStore.appTitle }}</strong>
        <span class="sidebar__tagline">智校园 Agent</span>
      </span>
    </div>

    <NewConversationButton @create="handleCreate" />

    <ConversationList
      :conversations="conversationStore.conversations"
      :current-id="conversationStore.currentConversationId"
      @select="handleSelect"
      @remove="conversationStore.removeConversation"
    />

    <!-- 底部用户 / 设置区：本阶段只占位，不做登录 -->
    <footer class="sidebar__foot">
      <span class="sidebar__avatar" aria-hidden="true">游</span>
      <span class="sidebar__user">
        <span class="sidebar__user-name">未登录</span>
        <span class="sidebar__user-hint">学生登录待接入</span>
      </span>
    </footer>
  </aside>
</template>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  gap: 0.875rem;
  width: var(--cf-sidebar-width);
  flex: 0 0 auto;
  height: 100%;
  padding: 1rem 0.75rem 0.75rem;
  background: var(--cf-sidebar-bg);
  border-right: 1px solid var(--cf-border);
}

.sidebar__brand {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0 0.25rem;
}

.sidebar__mark {
  display: grid;
  place-items: center;
  width: 1.75rem;
  height: 1.75rem;
  border-radius: 8px;
  font-size: 0.6875rem;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: var(--cf-on-accent);
  background: var(--cf-accent);
}

.sidebar__brand-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.sidebar__name {
  font-size: 0.9375rem;
  letter-spacing: 0.01em;
}

.sidebar__tagline {
  font-size: 0.6875rem;
  color: var(--cf-text-subtle);
}

.sidebar__foot {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 0.5rem 0.25rem;
  margin-top: auto;
  border-top: 1px solid var(--cf-border);
}

.sidebar__avatar {
  display: grid;
  place-items: center;
  width: 1.5rem;
  height: 1.5rem;
  flex: 0 0 auto;
  border-radius: 50%;
  font-size: 0.6875rem;
  color: var(--cf-text-muted);
  background: var(--cf-border);
}

.sidebar__user {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.sidebar__user-name {
  font-size: 0.75rem;
}

.sidebar__user-hint {
  font-size: 0.6875rem;
  color: var(--cf-text-subtle);
}

/**
 * 窄屏：侧边栏变成覆盖式抽屉。
 * 用 visibility 而不是 display，保证宽度过渡之外的层叠关系稳定；
 * 默认（含宽屏）为可见，故宽屏无需额外媒体查询。
 */
@media (max-width: 860px) {
  .sidebar {
    position: absolute;
    top: 0;
    left: 0;
    z-index: 20;
    transform: translateX(-100%);
    visibility: hidden;
    transition: transform 0.18s ease;
    box-shadow: 0 0 0 100vmax rgba(31, 35, 40, 0.28);
  }

  .sidebar--open {
    transform: translateX(0);
    visibility: visible;
  }
}
</style>
