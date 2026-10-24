<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import LoginView from './views/LoginView.vue'
import MedicineBoxView from './views/MedicineBoxView.vue'
import AskView from './views/AskView.vue'
import DataBoardView from './views/DataBoardView.vue'
import GraphView from './views/GraphView.vue'
import QueryView from './views/QueryView.vue'

//: 侧栏导航。**不放图标**：试过手写 SVG path（分别画气泡/放大镜/盒子/网络/柱状），
//: 在 17px 下根本认不出来，反而像一堆括号和逗号。与其放认不出的符号，
//: 不如把文字排版做干净——很多成熟产品的侧栏就是纯文字的。
const NAV = [
  { k: 'ask', t: '智能问答' },
  { k: 'query', t: '医药查询' },
  { k: 'box', t: '我的药箱' },
  { k: 'graph', t: '图谱浏览' },
  { k: 'board', t: '数据看板' },
]
const TABS = NAV.map((n) => n.k)
const savedTab = localStorage.getItem('pharmkg:tab') || ''

const token = ref(localStorage.getItem('token') || '')
//: **记住上次所在的页签**：刷新后回到同一页，而不是被弹回"智能问答"。
const tab = ref(TABS.includes(savedTab) ? savedTab : 'ask')
watch(tab, (v) => {
  try { localStorage.setItem('pharmkg:tab', v) } catch { /* 隐私模式下可能抛 */ }
})

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

  <!-- 已登录：左侧导航 + 内容区 -->
  <div v-else class="app">
    <aside class="side">
      <div class="side__brand">
        <span class="side__mark" aria-hidden="true">Rx</span>
        <span class="side__name">pharmkg</span>
      </div>

      <nav class="side__nav" aria-label="主导航">
        <p class="side__label">工作区</p>
        <button v-for="n in NAV" :key="n.k" type="button"
                :class="['side__item', { on: tab === n.k }]"
                :aria-current="tab === n.k ? 'page' : undefined"
                :data-test="`nav-${n.k}`" @click="tab = n.k">
          <span>{{ n.t }}</span>
        </button>
      </nav>

      <div class="side__foot">
        <p class="side__note">图谱数据仅供学习参考<br />不能替代医生判断</p>
        <button type="button" class="side__item side__item--quiet"
                data-test="logout" @click="logout">
          <span>退出登录</span>
        </button>
      </div>
    </aside>

    <main class="main">
      <div class="main__inner">
        <AskView v-if="tab === 'ask'" />
        <QueryView v-else-if="tab === 'query'" />
        <GraphView v-else-if="tab === 'graph'" />
        <DataBoardView v-else-if="tab === 'board'" />
        <MedicineBoxView v-else />
      </div>
    </main>
  </div>
</template>

<style scoped>
.app {
  display: grid;
  grid-template-columns: 232px minmax(0, 1fr);
  min-height: 100%;
  background: var(--ph-page);
}

/* ── 侧栏 ───────────────────────────────────────────────────
   为什么不用顶栏那排单选按钮：五个功能平铺在顶栏像"表单选项"，
   而它们其实是五个**并列的工作区**。左侧导航是这类应用的常规形态，
   也能容下更多项（以后加分册/设置都不用改布局）。 */
.side {
  position: sticky; top: 0;
  align-self: start;
  height: 100vh;
  display: flex;
  flex-direction: column;
  padding: var(--ph-s4) var(--ph-s3);
  background: var(--ph-surface);
  border-right: 1px solid var(--ph-line);
}

.side__brand {
  display: flex; align-items: center; gap: var(--ph-s2);
  padding: var(--ph-s2) var(--ph-s2) var(--ph-s5);
}
.side__mark {
  display: inline-grid; place-items: center;
  width: 28px; height: 28px; border-radius: 8px;
  background: var(--ph-brand); color: #fff;
  font-size: 11px; font-weight: 700;
}
.side__name {
  font-size: 15px; font-weight: 700; letter-spacing: .02em;
  color: var(--ph-ink);
}

.side__nav { display: flex; flex-direction: column; gap: 2px; }
.side__label {
  margin: 0 0 var(--ph-s2);
  padding: 0 var(--ph-s3);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .14em;
  color: var(--ph-ink-3);
}

.side__item {
  appearance: none;
  display: flex; align-items: center; gap: var(--ph-s3);
  width: 100%;
  min-height: 40px;
  padding: 0 var(--ph-s3);
  border: 0; border-radius: 9px;
  background: transparent;
  color: var(--ph-ink-2);
  font-family: inherit; font-size: 14px; font-weight: 500;
  text-align: left; cursor: pointer;
  transition: background .16s ease, color .16s ease;
}
.side__item:hover { background: #f4f6fb; color: var(--ph-ink); }
/* 选中态：淡品牌底 + 左侧一条实色指示条（不只靠颜色，形状也变） */
.side__item.on {
  background: var(--ph-brand-soft);
  color: var(--ph-brand);
  font-weight: 600;
  box-shadow: inset 3px 0 0 var(--ph-brand);
}
.side__item--quiet { color: var(--ph-ink-3); }

.side__foot { margin-top: auto; padding-top: var(--ph-s4); }
.side__note {
  margin: 0 0 var(--ph-s3);
  padding: 0 var(--ph-s3);
  font-size: 11px; line-height: 1.7; color: var(--ph-ink-3);
}

/* ── 内容区 ───────────────────────────────────────────────── */
.main { min-width: 0; }
.main__inner {
  max-width: 1180px;
  margin: 0 auto;
  padding: var(--ph-s5) var(--ph-s5) var(--ph-s7);
}

/* ── 窄屏：侧栏变成顶部横向条 ─────────────────────────────── */
@media (max-width: 900px) {
  .app { grid-template-columns: 1fr; }
  .side {
    position: static;
    height: auto;
    flex-direction: row;
    align-items: center;
    gap: var(--ph-s2);
    padding: var(--ph-s2) var(--ph-s3);
    border-right: 0;
    border-bottom: 1px solid var(--ph-line);
    overflow-x: auto;
  }
  .side__brand { padding: 0 var(--ph-s2) 0 0; }
  .side__name { display: none; }
  .side__nav { flex-direction: row; gap: 4px; }
  .side__item { min-height: 36px; padding: 0 var(--ph-s3); white-space: nowrap; }
  .side__item.on { box-shadow: inset 0 -3px 0 var(--ph-brand); }
  .side__foot { margin-top: 0; padding-top: 0; margin-left: auto; }
  .side__note { display: none; }
  .main__inner { padding: var(--ph-s4) var(--ph-s4) var(--ph-s6); }
}
</style>
