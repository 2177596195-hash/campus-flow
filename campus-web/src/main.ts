import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'

import './assets/main.css'

const app = createApp(App)

// Pinia 必须在 Router 之前注册：路由守卫里可能会用到 store。
app.use(createPinia())
app.use(router)

app.mount('#app')
