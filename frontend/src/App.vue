<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import LoginView from './views/LoginView.vue'
import MedicineBoxView from './views/MedicineBoxView.vue'
import AskView from './views/AskView.vue'
import DataBoardView from './views/DataBoardView.vue'
import GraphView from './views/GraphView.vue'
import QueryView from './views/QueryView.vue'

const token = ref(localStorage.getItem('token') || '')
//: 提案的五模块至此齐了：问答、医药查询、药箱、图谱浏览、数据看板。
const tab = ref('ask')

function onLogout() { token.value = '' }
onMounted(() => window.addEventListener('pharmkg:logout', onLogout))
onUnmounted(() => window.removeEventListener('pharmkg:logout', onLogout))

function logout() {
  localStorage.removeItem('token')
  token.value = ''
}
</script>

<template>
  <!-- 未登录：整屏给登录页（它自己是左右分栏，需要满高） -->
  <LoginView v-if="!token" @logged-in="token = $event" />

  <!-- 已登录：顶栏 + 内容区 -->
  <el-container v-else class="shell">
    <el-header class="shell__bar" height="64px">
      <div class="shell__left">
        <span class="shell__mark" aria-hidden="true">Rx</span>
        <b class="shell__brand">pharmkg</b>
        <span class="shell__sep" aria-hidden="true" />
        <span class="shell__product">医药查询与智能用药</span>
      </div>

      <div class="shell__right">
        <el-radio-group v-model="tab" data-test="tab-switch">
          <el-radio-button value="ask">智能问答</el-radio-button>
          <el-radio-button value="query">医药查询</el-radio-button>
          <el-radio-button value="box">我的药箱</el-radio-button>
          <el-radio-button value="graph">图谱浏览</el-radio-button>
          <el-radio-button value="board">数据看板</el-radio-button>
        </el-radio-group>
        <el-button text class="shell__logout" data-test="logout" @click="logout">
          退出
        </el-button>
      </div>
    </el-header>

    <el-main class="shell__main">
      <div class="shell__inner">
        <AskView v-if="tab === 'ask'" />
        <QueryView v-else-if="tab === 'query'" />
        <GraphView v-else-if="tab === 'graph'" />
        <DataBoardView v-else-if="tab === 'board'" />
        <MedicineBoxView v-else />
      </div>
    </el-main>
  </el-container>
</template>

<style scoped>
.shell { min-height: 100%; background: var(--ph-page); }

/* 顶栏：白底 + 底部细线 + 粘顶。比 Element 默认的 header 更"像一个应用" */
.shell__bar {
  position: sticky; top: 0; z-index: 20;
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--ph-s4);
  padding: 0 var(--ph-s5);
  background: rgba(255, 255, 255, .92);
  backdrop-filter: saturate(180%) blur(8px);
  border-bottom: 1px solid var(--ph-line);
}

.shell__left { display: flex; align-items: center; gap: var(--ph-s2); min-width: 0; }
.shell__mark {
  display: inline-grid; place-items: center;
  width: 26px; height: 26px; border-radius: 7px;
  background: var(--ph-brand); color: #fff;
  font-size: 11px; font-weight: 700;
}
.shell__brand {
  font-size: 15px; letter-spacing: .04em; color: var(--ph-ink);
}
.shell__sep {
  width: 1px; height: 16px; margin: 0 var(--ph-s2);
  background: var(--ph-line);
}
.shell__product {
  font-size: 13px; color: var(--ph-ink-2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

.shell__right { display: flex; align-items: center; gap: var(--ph-s3); }
.shell__logout { color: var(--ph-ink-2); }
.shell__logout:hover { color: var(--ph-brand); }

.shell__main { padding: 0; }
/* 内容区限宽居中：大屏上不要把表格/卡片拉成一整行 */
.shell__inner {
  max-width: 1240px;
  margin: 0 auto;
  padding: var(--ph-s5);
}

@media (max-width: 900px) {
  .shell__bar { flex-wrap: wrap; height: auto !important; padding: var(--ph-s3) var(--ph-s4); }
  .shell__product, .shell__sep { display: none; }
  .shell__inner { padding: var(--ph-s4) var(--ph-s3); }
}
</style>
