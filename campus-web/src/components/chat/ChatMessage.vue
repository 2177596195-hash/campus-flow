<script setup lang="ts">
/**
 * 单条聊天消息。
 *
 * 只负责「一条消息长什么样」：角色区分 + 长文本换行。
 * 不做任何数据获取，也不读写 store（内容全部来自 props）。
 */

const props = defineProps<{
  /** user = 右侧用户气泡；assistant = 左侧助手消息 */
  role: 'user' | 'assistant'
  content: string
  /** 本地占位回复标记 */
  mock?: boolean
  /** 查询失败提示标记（真实请求未成功，不是业务回答） */
  error?: boolean
  /** 助手正在生成（本阶段用于展示占位状态） */
  pending?: boolean
}>()

const roleLabel = props.role === 'user' ? '我' : 'CampusFlow'
</script>

<template>
  <article class="msg" :class="`msg--${props.role}`">
    <div class="msg__avatar" aria-hidden="true">{{ role === 'user' ? '我' : 'AI' }}</div>

    <div class="msg__body">
      <header class="msg__head">
        <span class="msg__name">{{ roleLabel }}</span>
        <!-- 失败标记优先于占位标记：一条消息不可能同时是「真实请求失败」和「本地占位」 -->
        <span v-if="props.error" class="msg__tag msg__tag--error">失败</span>
        <span v-else-if="props.mock" class="msg__tag">占位</span>
      </header>

      <div v-if="props.pending" class="msg__typing" aria-label="正在生成">
        <span></span><span></span><span></span>
      </div>
      <!-- 用 pre-wrap 保留换行与缩进，长文本自然换行且不产生横向滚动 -->
      <p v-else class="msg__text">{{ content }}</p>
    </div>
  </article>
</template>

<style scoped>
.msg {
  display: flex;
  gap: 0.75rem;
  align-items: flex-start;
  max-width: var(--cf-chat-max-width);
  margin: 0 auto;
  padding: 0.5rem 0;
}

/* 用户消息整体靠右：镜像 flex 方向即可，不用绝对定位 */
.msg--user {
  flex-direction: row-reverse;
}

.msg__avatar {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  width: 1.75rem;
  height: 1.75rem;
  margin-top: 0.125rem;
  border: 1px solid var(--cf-border);
  border-radius: 8px;
  font-size: 0.6875rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--cf-text-muted);
  background: var(--cf-surface);
}

.msg--user .msg__avatar {
  color: var(--cf-on-accent);
  background: var(--cf-accent);
  border-color: var(--cf-accent);
}

.msg__body {
  min-width: 0;
  flex: 1 1 auto;
}

.msg__head {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  margin-bottom: 0.25rem;
}

.msg--user .msg__head {
  justify-content: flex-end;
}

.msg__name {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--cf-text-muted);
}

.msg__tag {
  padding: 0 0.25rem;
  border: 1px solid var(--cf-border);
  border-radius: 4px;
  font-size: 0.625rem;
  line-height: 1.5;
  color: var(--cf-text-subtle);
}

/* 失败标记用红色描边区分（沿用站点调色板，不引入新的设计与组件库） */
.msg__tag--error {
  color: #b3261e;
  border-color: #e8a09a;
}

.msg__text {
  margin: 0;
  /* 长文本换行的关键三件套 */
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  line-height: 1.75;
}

.msg--assistant .msg__text {
  color: var(--cf-text);
}

.msg--user .msg__text {
  padding: 0.5rem 0.75rem;
  border-radius: 10px;
  background: var(--cf-accent-soft);
  text-align: left;
}

/* 占位状态的三个点：静态，不做动画（本阶段明确不做花哨动效） */
.msg__typing {
  display: flex;
  gap: 0.25rem;
  align-items: center;
  height: 1.75rem;
}

.msg__typing span {
  width: 0.3125rem;
  height: 0.3125rem;
  border-radius: 50%;
  background: var(--cf-text-subtle);
  opacity: 0.6;
}
</style>
