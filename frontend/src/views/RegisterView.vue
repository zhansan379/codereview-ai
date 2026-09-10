<template>
  <div class="register-wrap">
    <el-card class="register-card">
      <h2 class="register-title">创建账号</h2>
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
            placeholder="至少 8 位"
          />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirm">
          <el-input
            v-model="form.confirm"
            type="password"
            show-password
            placeholder="再次输入密码"
          />
        </el-form-item>
        <el-form-item label="邮箱（可选）" prop="email">
          <el-input v-model="form.email" placeholder="用于接收审查通知与验证邮箱" />
        </el-form-item>
        <el-button type="primary" class="register-btn" :loading="loading" @click="onSubmit">
          注册
        </el-button>
      </el-form>
      <div class="register-footer">
        已有账号？
        <router-link to="/login">前往登录</router-link>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance } from 'element-plus'
import { register as apiRegister } from '../api'

const router = useRouter()
const formRef = ref<FormInstance>()
const form = reactive({ username: '', password: '', confirm: '', email: '' })
const loading = ref(false)

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, min: 8, message: '密码至少 8 位', trigger: 'blur' }],
  confirm: [
    {
      validator: (_r: unknown, value: string, cb: (e?: Error) => void) => {
        if (value !== form.password) cb(new Error('两次输入的密码不一致'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
  email: [{ type: 'email', message: '邮箱格式不正确', trigger: 'blur' }],
}

async function onSubmit() {
  await formRef.value?.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      await apiRegister(form.username, form.password, form.email)
      ElMessage.success('注册成功，请使用新账号登录')
      router.push('/login')
    } catch (e: any) {
      ElMessage.error(e?.response?.data?.detail || '注册失败，请重试')
    } finally {
      loading.value = false
    }
  })
}
</script>

<style scoped>
.register-wrap {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f0f2f5;
}
.register-card {
  width: 380px;
  padding: 12px 8px;
}
.register-title {
  text-align: center;
  margin: 0 0 20px;
}
.register-btn {
  width: 100%;
}
.register-footer {
  margin-top: 14px;
  text-align: center;
  color: #606266;
  font-size: 14px;
}
.register-footer a {
  color: #409eff;
  text-decoration: none;
}
</style>