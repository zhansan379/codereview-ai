<template>
  <div class="login-wrap">
    <el-card class="login-card">
      <div class="login-brand">
        <AppLogo :size="40" />
      </div>
      <h2 class="login-title">{{ $t('menu.appTitle') }}</h2>
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @keyup.enter="onSubmit"
      >
        <el-form-item :label="$t('login.username')" prop="username">
          <el-input v-model="form.username" :placeholder="$t('login.usernamePlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('login.password')" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            :placeholder="$t('login.passwordPlaceholder')"
          />
        </el-form-item>
        <el-button
          type="primary"
          class="login-btn"
          :loading="loading"
          @click="onSubmit"
        >
          {{ $t('login.submit') }}
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, type FormInstance } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import AppLogo from '../components/AppLogo.vue'

const router = useRouter()
const auth = useAuthStore()
const { t } = useI18n()

const formRef = ref<FormInstance>()
const form = reactive({ username: 'admin', password: '' })
const loading = ref(false)

// computed 而非常量：切语言后校验提示也得跟着变
const rules = computed(() => ({
  username: [{ required: true, message: t('login.usernamePlaceholder'), trigger: 'blur' }],
  password: [{ required: true, message: t('login.passwordPlaceholder'), trigger: 'blur' }],
}))

// 登录：调用 /auth/login，成功后跳转仪表盘
async function onSubmit() {
  await formRef.value?.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      await auth.login(form.username, form.password)
      ElMessage.success(t('login.success'))
      router.push('/dashboard')
    } catch (e: any) {
      ElMessage.error(e?.response?.data?.detail || t('login.failed'))
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
  background: var(--el-bg-color-page);
}
.login-card {
  width: 360px;
  padding: 12px 8px;
}
.login-brand {
  display: flex;
  justify-content: center;
  color: var(--el-color-primary);
  margin-bottom: 8px;
}
.login-title {
  text-align: center;
  margin: 0 0 20px;
}
.login-btn {
  width: 100%;
}
</style>