<script setup lang="ts">
/**
 * 应用外壳。
 *
 * 聊天主页自带侧边栏，所以这里只做一件事：占满视口并给 RouterView 一个
 * 高度确定的容器（`height: 100%` 而非 `min-height`，让内部滚动区域能正确收缩）。
 */

import { RouterView } from 'vue-router'
</script>

<template>
  <div class="shell">
    <RouterView />
  </div>
</template>

<style scoped>
.shell {
  height: 100%;
  min-width: 0;
  overflow: hidden;
}
</style>
