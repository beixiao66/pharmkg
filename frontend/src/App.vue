<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
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
  <el-container>
    <el-header style="display:flex;align-items:center;justify-content:space-between">
      <h2 style="margin:0">pharmkg · 医药查询与智能用药</h2>
      <div v-if="token">
        <el-radio-group v-model="tab" size="small" data-test="tab-switch">
          <el-radio-button value="ask">智能问答</el-radio-button>
          <el-radio-button value="query">医药查询</el-radio-button>
          <el-radio-button value="box">我的药箱</el-radio-button>
          <el-radio-button value="graph">图谱浏览</el-radio-button>
          <el-radio-button value="board">数据看板</el-radio-button>
        </el-radio-group>
        <el-button size="small" style="margin-left:12px" data-test="logout"
                   @click="logout">退出</el-button>
      </div>
    </el-header>
    <el-main>
      <template v-if="token">
        <AskView v-if="tab === 'ask'" />
        <QueryView v-else-if="tab === 'query'" />
        <GraphView v-else-if="tab === 'graph'" />
        <DataBoardView v-else-if="tab === 'board'" />
        <MedicineBoxView v-else />
      </template>
      <LoginView v-else @logged-in="token = $event" />
    </el-main>
  </el-container>
</template>
