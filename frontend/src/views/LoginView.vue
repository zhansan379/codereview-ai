<template>
  <div class="login-wrap">
    <el-card class="login-card">
      <h2 class="login-title">CodeReview AI 管理后台</h2>
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @keyup.enter="onSubmit"
      >
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="请输入登录密码"
          />
        </el-form-item>
        <el-button
          type="primary"
          class="login-btn"
          :loading="loading"
          @click="onSubmit"
        >
          登录
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const form = reactive({ password: '' })
const loading = ref(false)

const rules = {
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

// 登录：调用 /auth/login，成功后跳转仪表盘
async function onSubmit() {
  await formRef.value?.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      await auth.login(form.password)
      ElMessage.success('登录成功')
      router.push('/dashboard')
    } catch (e: any) {
      ElMessage.error(e?.response?.data?.detail || '登录失败，请重试')
    } finally {
      loading.value = false
    }
  })
}
</script>

<style scoped>
.login-wrap {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f0f2f5;
}
.login-card {
  width: 360px;
  padding: 12px 8px;
}
.login-title {
  text-align: center;
  margin: 0 0 20px;
}
.login-btn {
  width: 100%;
}
</style>