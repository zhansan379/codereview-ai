<template>
  <div>
    <el-card>
      <template #header>系统设置说明</template>
      <el-descriptions :column="1" border>
        <el-descriptions-item label="Webhook 签名密钥">
          由后端环境变量配置，出于安全考虑不在管理后台展示明文。
        </el-descriptions-item>
        <el-descriptions-item label="JWT 有效期">
          登录返回的 access_token 有有效期（expires_in 字段），过期后自动跳转登录页。
        </el-descriptions-item>
        <el-descriptions-item label="Token 存储">
          登录凭证保存在浏览器 sessionStorage 中，关闭页面即失效。
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card class="note-card">
      <template #header>说明</template>
      <p>本页为只读信息页，暂不提供任何写操作（后端暂无 /api/settings 接口）。</p>
      <p>监控通知的签名校验密钥、反代鉴权等运行时配置均由后端环境变量管理，前端仅提供只读展示。</p>
    </el-card>
  </div>
</template>

<script setup lang="ts">
// 只读设置页：仅文案说明，无写操作
</script>

<style scoped>
.note-card {
  margin-top: 16px;
}
</style>