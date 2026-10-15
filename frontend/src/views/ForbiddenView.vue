<template>
  <div class="forbidden">
    <el-result icon="warning" :title="$t('common.forbidden.title')" :sub-title="$t('common.forbidden.desc')">
      <template #icon>
        <div class="code">{{ $t('common.forbidden.code') }}</div>
      </template>
      <template #extra>
        <el-button type="primary" @click="$router.replace('/dashboard')">
          {{ $t('common.forbidden.back') }}
        </el-button>
      </template>
    </el-result>
  </div>
</template>

<script setup lang="ts">
// 无权限 / 未匹配路由的落地页（守卫把未授权路由重定向到这里）
</script>

<style scoped>
.forbidden {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: calc(100vh - 200px);
}
.code {
  font-size: 64px;
  font-weight: 800;
  line-height: 1;
  color: var(--el-color-warning);
}
</style>
