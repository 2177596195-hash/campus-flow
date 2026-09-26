import { createRouter, createWebHistory } from 'vue-router'

import DefaultLayout from '@/layouts/DefaultLayout.vue'
import ChatView from '@/views/ChatView.vue'

/**
 * 路由表保持与第一阶段相同的结构：单层布局路由 + 子路由 + 兜底重定向。
 *
 * 本阶段只有一个真实页面（聊天主页）。`/chat/:conversationId` 暂时不引入：
 * 会话已经由 Pinia 在内存里承载，加深路径段并不能带来额外能力，
 * 等真正需要「刷新后仍定位到某个会话」时再加，避免过度设计。
 */
const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      component: DefaultLayout,
      children: [
        {
          path: '',
          name: 'chat',
          component: ChatView,
          meta: { title: 'CampusFlow · 校园智能事务助手' },
        },
      ],
    },
    // 兜底路由：占位阶段先回到聊天主页，避免刷新出现空白。
    {
      path: '/:pathMatch(.*)*',
      redirect: { name: 'chat' },
    },
  ],
})

router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  if (title) document.title = title
})

export default router
