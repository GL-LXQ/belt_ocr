import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import RealtimeView from './views/RealtimeView.vue'
import HistoryView from './views/HistoryView.vue'
import AbnormalView from './views/AbnormalView.vue'
import ImagesView from './views/ImagesView.vue'
import MachinesView from './views/MachinesView.vue'
import ConfigurationView from './views/ConfigurationView.vue'
import './style.css'
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', component: RealtimeView, meta: { title: '实时监测', eyebrow: 'LIVE OPERATIONS' } },
    {
      path: '/history',
      component: HistoryView,
      meta: { title: '历史记录', eyebrow: 'MEASUREMENT ARCHIVE' },
    },
    {
      path: '/images',
      component: ImagesView,
      meta: { title: '图片管理', eyebrow: 'VISUAL EVIDENCE' },
    },
    {
      path: '/abnormal',
      component: AbnormalView,
      meta: { title: '异常事件', eyebrow: 'EVENT TRACEABILITY' },
    },
    {
      path: '/machines',
      component: MachinesView,
      meta: { title: '机器管理', eyebrow: 'CONNECTED EQUIPMENT' },
    },
    {
      path: '/configuration',
      component: ConfigurationView,
      meta: { title: '系统配置', eyebrow: 'SYSTEM PREFERENCES' },
    },
  ],
})
createApp(App).use(router).mount('#app')
