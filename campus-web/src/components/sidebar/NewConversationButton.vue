<script setup lang="ts">
/**
 * 「新对话」按钮。
 *
 * 只做一件事：把点击冒泡给父级。文案与图标由本组件负责，行为不在这里。
 */

const emit = defineEmits<{
  (e: 'create'): void
}>()
</script>

<template>
  <button type="button" class="new-conv" @click="emit('create')">
    <span class="new-conv__icon" aria-hidden="true">+</span>
    <span class="new-conv__label">新对话</span>
  </button>
</template>

<style scoped>
.new-conv {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.375rem;
  width: 100%;
  padding: 0.5rem 0.75rem;
  font: inherit;
  font-weight: 500;
  color: var(--cf-on-accent);
  background: var(--cf-accent);
  border: none;
  border-radius: 9px;
  cursor: pointer;
}

.new-conv:hover {
  background: var(--cf-accent-hover);
}

.new-conv:focus-visible {
  outline: 2px solid var(--cf-accent);
  outline-offset: 2px;
}

.new-conv__icon {
  font-size: 0.9375rem;
  line-height: 1;
}

.new-conv__label {
  font-size: 0.8125rem;
}
</style>
