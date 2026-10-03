<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { invoke, isTauri } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import {
  Activity,
  History,
  Images,
  TriangleAlert,
  Cpu,
  SlidersHorizontal,
  ArrowUpRight,
  Radio,
  LoaderCircle,
  Menu,
  X,
  Link2,
  RotateCw,
} from 'lucide-vue-next'
import { api, configureApi, errorText } from './lib/api'
import {
  connectRuntime,
  disconnectRuntime,
  connection,
  runtime,
  applySnapshot,
} from './composables/useRuntime'
import type { BackendStatus, Snapshot } from './lib/types'
const route = useRoute()
const ready = ref(false)
const realtimeLayout = computed(() => ready.value && route.path === '/')
const loading = ref(true)
const startupError = ref('')
const mobileMenu = ref(false)
const browserToken = ref('')
const backendStatus = ref<BackendStatus>({ phase: 'starting', message: '正在连接本地检测服务' })
const native = isTauri()
let unlisten: UnlistenFn | undefined
let disposed = false
const navigation = [
  { path: '/', label: '实时监测', icon: Activity },
  { path: '/history', label: '历史记录', icon: History },
  { path: '/images', label: '图片管理', icon: Images },
  { path: '/abnormal', label: '异常事件', icon: TriangleAlert },
  { path: '/machines', label: '机器管理', icon: Cpu },
  { path: '/configuration', label: '系统配置', icon: SlidersHorizontal },
]
const statusLabel = computed(() =>
  connection.value === 'connected'
    ? '实时连接'
    : connection.value === 'reconnecting'
      ? '正在重连'
      : '正在连接',
)
/** 初始化本地服务连接，令牌仅保存在内存。Args: 无。Returns: 无。 */
async function initializeConnection() {
  loading.value = true
  startupError.value = ''
  try {
    if (native) {
      backendStatus.value = await invoke<BackendStatus>('get_backend_status')
      while (backendStatus.value.phase === 'starting' && !disposed) {
        await new Promise((resolve) => setTimeout(resolve, 500))
        backendStatus.value = await invoke<BackendStatus>('get_backend_status')
      }
      if (disposed) return
      if (backendStatus.value.phase !== 'ready')
        throw new Error(backendStatus.value.message || '本地服务尚未就绪。')
      const settings = await invoke<{ baseUrl: string; token: string }>('get_backend_connection')
      configureApi(settings.baseUrl, settings.token)
    } else configureApi(import.meta.env.VITE_API_BASE_URL || '', browserToken.value)
    const state = await api<Snapshot>('/state')
    applySnapshot(state, true)
    ready.value = true
    browserToken.value = ''
    void connectRuntime()
  } catch (error) {
    startupError.value = errorText(error)
  } finally {
    loading.value = false
  }
}
/**
 * 请求桌面重启已退出的后端并重新连接。
 * Args: 无。
 * Returns: Promise<void>；成功后启动实时状态读取。
 */
async function retryBackend() {
  loading.value = true
  try {
    await invoke('retry_backend')
    await initializeConnection()
  } catch (error) {
    startupError.value = errorText(error)
    loading.value = false
  }
}
onMounted(async () => {
  if (native)
    unlisten = await listen<BackendStatus>('backend-status', (event) => {
      backendStatus.value = event.payload
      if (event.payload.phase === 'failed' || event.payload.phase === 'stopped') {
        disconnectRuntime()
        ready.value = false
        startupError.value = event.payload.message
      }
    })
  await initializeConnection()
})
onBeforeUnmount(() => {
  disposed = true
  unlisten?.()
  disconnectRuntime()
})
</script>
<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ 'mobile-open': mobileMenu }">
      <a href="#/" class="brand" @click="mobileMenu = false"
        ><span class="brand-mark"><span></span><span></span></span
        ><span>Belt<span class="brand-light">Vision</span><small>皮带视觉检测系统</small></span></a
      >
      <div class="nav-section-label">工作空间</div>
      <nav aria-label="主导航">
        <RouterLink
          v-for="item in navigation"
          :key="item.path"
          :to="item.path"
          class="nav-link"
          :class="{ active: route.path === item.path }"
          @click="mobileMenu = false"
          ><component :is="item.icon" :size="19" /><span>{{ item.label }}</span
          ><span v-if="route.path === item.path" class="nav-active-dot"></span
        ></RouterLink>
      </nav>
      <div class="sidebar-bottom">
        <div class="system-signature">
          <span class="status-dot" :class="{ online: connection === 'connected' }"></span
          ><span
            >本地检测工作站<small>{{ native ? '桌面运行环境' : '浏览器调试环境' }}</small></span
          >
        </div>
        <div class="version-line"><span>BELT VISION</span><span>v0.2</span></div>
      </div>
    </aside>
    <button
      v-if="mobileMenu"
      class="sidebar-scrim"
      aria-label="关闭导航"
      @click="mobileMenu = false"
    ></button>
    <div class="workspace" :class="{ 'realtime-workspace': realtimeLayout }">
      <header class="topbar">
        <div class="breadcrumb">
          <button
            class="icon-button mobile-toggle"
            aria-label="打开导航"
            @click="mobileMenu = !mobileMenu"
          >
            <Menu :size="20" /></button
          ><span>检测工作台</span><span class="breadcrumb-divider">/</span
          ><strong>{{ route.meta.title }}</strong>
        </div>
        <div class="connection-pill" :class="{ disconnected: connection !== 'connected' }">
          <Radio :size="14" /><span>{{ ready ? statusLabel : '等待本地服务' }}</span>
        </div>
      </header>
      <main id="main-content" class="main-content">
        <div class="page-heading">
          <div>
            <p v-if="!realtimeLayout" class="eyebrow">{{ route.meta.eyebrow }}</p>
            <h1>{{ route.meta.title }}</h1>
            <p v-if="realtimeLayout" class="page-description">查看当前机器的检测状态</p>
          </div>
          <span class="workspace-chip"
            ><span class="status-dot" :class="{ online: runtime?.running }"></span
            >{{ runtime?.running ? '监测已启动' : '监测未运行' }}</span
          >
        </div>
        <div v-if="!ready" class="startup-panel panel">
          <span class="startup-icon"
            ><LoaderCircle v-if="loading" class="spin" :size="30" /><Link2 v-else :size="30"
          /></span>
          <h2>{{ loading ? '连接检测工作站' : '本地服务尚未连接' }}</h2>
          <p>{{ startupError || backendStatus.message }}</p>
          <form
            v-if="!native && !loading"
            class="connection-form"
            @submit.prevent="initializeConnection"
          >
            <label
              >本地服务访问令牌<input
                v-model="browserToken"
                type="password"
                autocomplete="off"
                placeholder="由后端启动命令生成" /></label
            ><button class="button primary" type="submit">
              连接本地服务<ArrowUpRight :size="16" /></button
            ><small>令牌仅保存在本次页面内存，不会存储到浏览器。</small>
          </form>
          <button
            v-else-if="!loading"
            class="button primary"
            :disabled="!backendStatus.retryAllowed"
            @click="retryBackend"
          >
            <RotateCw :size="16" />重新启动服务
          </button>
        </div>
        <RouterView v-else v-slot="{ Component }"
          ><KeepAlive><component :is="Component" /></KeepAlive
        ></RouterView>
      </main>
      <footer class="workspace-footer">
        <span>每一轮测量，都有迹可循。</span><span>本地处理 · 实时同步</span>
      </footer>
    </div>
    <div v-if="backendStatus.phase === 'stopping'" class="shutdown-banner" role="status">
      <LoaderCircle class="spin" :size="18" />正在完成测量与设备清理，请保持窗口开启。{{
        backendStatus.message
      }}
    </div>
  </div>
</template>

<style scoped>
/* 仅压缩实时页的外层留白，其余页面继续使用原布局。 */
.realtime-workspace {
  height: 100dvh;
  min-height: 0;
}
.realtime-workspace .topbar {
  height: 48px;
  flex: none;
  padding: 0 24px;
}
.realtime-workspace .main-content {
  display: flex;
  flex-direction: column;
  min-height: 0;
  max-width: none;
  padding: 12px 24px 20px;
}
.realtime-workspace .page-heading {
  flex: none;
  margin-bottom: 12px;
}
.realtime-workspace .page-description {
  margin-top: 4px;
}
.realtime-workspace .workspace-footer {
  display: none;
}
@media (max-width: 1050px) {
  .realtime-workspace {
    height: auto;
    min-height: 100dvh;
  }
}
@media (max-width: 680px) {
  .realtime-workspace .main-content {
    padding: 16px;
  }
  .realtime-workspace .topbar {
    padding: 0 16px;
  }
}
</style>
