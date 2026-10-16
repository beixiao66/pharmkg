<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import LoginView from './views/LoginView.vue'
import MedicineBoxView from './views/MedicineBoxView.vue'

const token = ref(localStorage.getItem('token') || '')
function onLogout() { token.value = '' }
onMounted(() => window.addEventListener('pharmkg:logout', onLogout))
onUnmounted(() => window.removeEventListener('pharmkg:logout', onLogout))
</script>

<template>
  <el-container>
    <el-header style="display:flex;align-items:center;justify-content:space-between">
      <h2 style="margin:0">pharmkg · 用药安全校验</h2>
      <el-button v-if="token" size="small" @click="localStorage.removeItem('token'); token=''">
        退出
      </el-button>
    </el-header>
    <el-main>
      <MedicineBoxView v-if="token" />
      <LoginView v-else @logged-in="token = $event" />
    </el-main>
  </el-container>
</template>
