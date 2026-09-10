<template>
  <div class="verify-wrap">
    <el-card class="verify-card">
      <template v-if="state === 'pending'">
        <el-result icon="info" title="正在验证邮箱…" />
      </template>
      <template v-else-if="state === 'success'">
        <el-result icon="success" title="邮箱已验证">
          <template #sub-title>
            <p>验证成功，现在可以正常登录并接收审查相关通知。</p>
          </template>
          <template #extra>
            <el-button type="primary" @click="$router.push('/login')">前往登录</el-button>
          </template>
        </el-result>
      </template>
      <template v-else>
        <el-result icon="error" title="验证失败">
          <template #sub-title>
            <p>{{ errorMsg }}</p>
            <p>链接可能已过期或无效，请重新注册或登录后联系管理员。</p>
          </template>
          <template #extra>
            <el-button type="primary" @click="$router.push('/login')">返回登录</el-button>
          </template>
        </el-result>
      </template>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { verifyEmail } from '../api'

const route = useRoute()
const state = ref<'pending' | 'success' | 'error'>('pending')
const errorMsg = ref('')

onMounted(async () => {
  const token = (route.query.token as string) || ''
  if (!token) {
    state.value = 'error'
    errorMsg.value = '缺少验证令牌'
    return
  }
  try {
    await verifyEmail(token)
    state.value = 'success'
  } catch (e: any) {
    state.value = 'error'
    errorMsg.value = e?.response?.data?.detail || '验证链接无效或已过期'
  }
})
</script>

<style scoped>
.verify-wrap {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f0f2f5;
}
.verify-card {
  width: 420px;
  padding: 12px;
}
</style>