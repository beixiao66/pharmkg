<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

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
    // ⚠️ 把后端的 detail 原样显示出来。只显示"失败"会让排查变成猜谜——
    //    而 422/409 的 detail 恰好是最有用的那句话。
    const d = e?.response?.data?.detail
    ElMessage.error(typeof d === 'string' ? d : JSON.stringify(d || e.message))
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
