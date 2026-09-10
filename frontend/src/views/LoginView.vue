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
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="请输入用户名" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="请输入登录密码"
          />
        </el-form-item>
        <el-form-item v-if="captcha.show" label="验证码" prop="captcha">
          <div class="captcha-row">
            <el-input
              v-model="form.captcha"
              placeholder="点击右侧题目作答"
              @keyup.enter="onSubmit"
            />
            <span class="captcha-prompt" @click="loadCaptcha">{{ captcha.prompt }}</span>
          </div>
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
import axios from 'axios'
import { ElMessage, type FormInstance } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const form = reactive({ username: 'admin', password: '', captcha: '' })
const loading = ref(false)
const captcha = reactive({ show: false, prompt: '', id: '' })

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

// 后端限速阈值命中后要求验证码：拉一道算术题展示在登录框（点题目可换题）
async function loadCaptcha() {
  try {
    const { data } = await axios.post('/api/auth/captcha')
    captcha.id = data.captcha_id
    captcha.prompt = data.prompt
    captcha.show = true
  } catch {
    captcha.show = false
  }
}

// 登录：带可选验证码（captcha_required 由响应头 X-Captcha-Required 触发取题）
async function onSubmit() {
  await formRef.value?.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      await auth.login(form.username, form.password, captcha.id, form.captcha)
      ElMessage.success('登录成功')
      captcha.show = false
      router.push('/dashboard')
    } catch (e: any) {
      const reqCaptcha = !!e?.response?.headers?.['x-captcha-required']
      if (reqCaptcha || e?.response?.status === 400) {
        await loadCaptcha() // 需验证码/答错 → 弹出题目供重试
      }
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
.captcha-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.captcha-prompt {
  flex-shrink: 0;
  padding: 6px 10px;
  background: #f5f7fa;
  border: 1px dashed #c0c4cc;
  border-radius: 4px;
  color: #606266;
  cursor: pointer;
  font-size: 14px;
  user-select: none;
}
.captcha-prompt:hover {
  border-color: #409eff;
  color: #409eff;
}
</style>