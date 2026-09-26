<script setup lang="ts">
/**
 * 欢迎面板：没有任何消息时占据主区域。
 *
 * 只负责「介绍产品 + 提供示例问题入口」。
 * 点击示例只向上抛事件（父级填入输入框），不发请求。
 */

import ExamplePrompt from './ExamplePrompt.vue'
import { CAPABILITIES, EXAMPLE_PROMPTS } from '@/constants/examples'

const emit = defineEmits<{
  (e: 'select-prompt', question: string): void
}>()
</script>

<template>
  <section class="welcome">
    <div class="welcome__inner">
      <p class="welcome__eyebrow">智校园 · 高校智能事务 Agent 平台</p>
      <h1 class="welcome__title">CampusFlow</h1>
      <p class="welcome__subtitle">你的校园智能事务助手</p>

      <p class="welcome__desc">
        把校园里那些「要跑好几个系统才能办完」的事务，交给一次对话。
      </p>

      <ul class="welcome__caps">
        <li v-for="item in CAPABILITIES" :key="item">{{ item }}</li>
      </ul>

      <div class="welcome__examples">
        <p class="welcome__examples-label">试试这样问</p>
        <div class="welcome__grid">
          <ExamplePrompt
            v-for="prompt in EXAMPLE_PROMPTS"
            :key="prompt.id"
            :title="prompt.title"
            :question="prompt.question"
            @select="emit('select-prompt', $event)"
          />
        </div>
      </div>

      <p class="welcome__note">
        当前为界面骨架阶段，尚未接入后端与 Agent，回答为本地占位内容。
      </p>
    </div>
  </section>
</template>

<style scoped>
.welcome {
  display: flex;
  justify-content: center;
  padding: 2.5rem 1.5rem 1.5rem;
}

.welcome__inner {
  width: 100%;
  max-width: var(--cf-chat-max-width);
}

.welcome__eyebrow {
  margin: 0 0 0.5rem;
  font-size: 0.75rem;
  letter-spacing: 0.08em;
  color: var(--cf-text-subtle);
}

.welcome__title {
  margin: 0;
  font-size: 1.875rem;
  font-weight: 650;
  letter-spacing: -0.01em;
}

.welcome__subtitle {
  margin: 0.25rem 0 0;
  font-size: 1rem;
  color: var(--cf-text-muted);
}

.welcome__desc {
  margin: 0.75rem 0 0;
  max-width: 34rem;
  line-height: 1.8;
  color: var(--cf-text-muted);
}

.welcome__caps {
  display: flex;
  flex-wrap: wrap;
  gap: 0.375rem;
  margin: 1rem 0 0;
  padding: 0;
  list-style: none;
}

.welcome__caps li {
  padding: 0.1875rem 0.5rem;
  font-size: 0.75rem;
  color: var(--cf-text-muted);
  background: var(--cf-surface);
  border: 1px solid var(--cf-border);
  border-radius: 999px;
}

.welcome__examples {
  margin-top: 1.75rem;
}

.welcome__examples-label {
  margin: 0 0 0.625rem;
  font-size: 0.75rem;
  color: var(--cf-text-subtle);
}

/* 桌面三列 / 平板两列 / 窄屏一列，窄屏也不会横向溢出 */
.welcome__grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.625rem;
}

.welcome__note {
  margin: 1.5rem 0 0;
  font-size: 0.75rem;
  color: var(--cf-text-subtle);
}

@media (max-width: 900px) {
  .welcome__grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 560px) {
  .welcome {
    padding: 1.75rem 1rem 1rem;
  }

  .welcome__title {
    font-size: 1.5rem;
  }

  .welcome__grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
