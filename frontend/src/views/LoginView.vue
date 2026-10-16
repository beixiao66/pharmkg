<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import api, { humanizeError } from '../api'

const emit = defineEmits(['logged-in'])
const username = ref('')
const password = ref('')
const busy = ref(false)

async function doAuth(path) {
  busy.value = true
  try {
    const { data } = await api.post(path, {
      username: username.value, password: password.value,
    })
    localStorage.setItem('token', data.access_token)
    ElMessage.success(`已登录：${data.username}`)
    emit('logged-in', data.access_token)
  } catch (e) {
    // ⚠️ **不要把原始 detail 直接甩给用户**。第一版写的是
    //    `typeof d === 'string' ? d : JSON.stringify(d)` ——
    //    而 422 的 detail 是**数组**，于是整坨
    //    `[{"type":"string_too_short","loc":["body","password"],...}]` 弹在了页面上。
    //    现在统一走 humanizeError()（见 src/api.js），它会翻成人话。
    ElMessage.error(humanizeError(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <el-card style="max-width:420px;margin:40px auto">
    <template #header>登录 / 注册</template>
    <el-form label-width="80px" @submit.prevent>
      <el-form-item label="用户名">
        <el-input v-model="username" placeholder="至少 3 个字符" data-test="username" />
      </el-form-item>
      <el-form-item label="密码">
        <el-input v-model="password" type="password" placeholder="至少 6 个字符"
                  data-test="password" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="busy" data-test="login"
                   @click="doAuth('/auth/login')">登录</el-button>
        <el-button :loading="busy" data-test="register"
                   @click="doAuth('/auth/register')">注册</el-button>
      </el-form-item>
    </el-form>
    <el-alert type="info" :closable="false"
      title="图谱数据仅供学习参考，不能替代医生或药师的判断。" />
  </el-card>
</template>
