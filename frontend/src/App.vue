<template>
  <!-- el-config-provider 把 Element Plus 自己的内置文案（分页、日期选择器、空态、
       MessageBox 默认按钮）切到当前语言；我们的业务文案走 vue-i18n。 -->
  <el-config-provider :locale="epLocale">
    <router-view />
  </el-config-provider>
</template>

<script setup lang="ts">
// 根组件：注入 Element Plus 语言包 + 渲染路由视图
import { useLocale } from './composables/useLocale'

const { epLocale } = useLocale()
</script>
