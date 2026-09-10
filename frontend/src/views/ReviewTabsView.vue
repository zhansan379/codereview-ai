<template>
  <div>
    <!-- MR 汇总 / 明细：一个领域一个入口，用分段切换避免两个平级菜单项打架 -->
    <el-card class="tab-card" shadow="never">
      <el-segmented
        v-model="tab"
        :options="[
          { label: $t('reviewTabs.mrSummary'), value: 'mr' },
          { label: $t('reviewTabs.detail'), value: 'detail' },
        ]"
        @change="onTabChange"
      />
      <span class="tab-hint">
        {{ tab === 'mr' ? $t('reviewTabs.hintMr') : $t('reviewTabs.hintDetail') }}
      </span>
    </el-card>

    <!-- v-show 双挂载：切换即时、各自筛选/分页状态不丢 -->
    <div v-show="tab === 'mr'">
      <ReviewPrsView />
    </div>
    <div v-show="tab === 'detail'">
      <ReviewsView />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ReviewPrsView from './ReviewPrsView.vue'
import ReviewsView from './ReviewsView.vue'

const route = useRoute()
const router = useRouter()

// 选中 tab 与 URL 的 ?tab= 同步：明细为默认，旧 /reviews/prs 链接带 tab=mr 直达 MR 汇总。
const tab = ref(route.query.tab === 'mr' ? 'mr' : 'detail')

function onTabChange(v: string | number) {
  router.replace({
    query: { ...route.query, tab: String(v) },
  })
}
</script>

<style scoped>
.tab-card {
  margin-bottom: 14px;
}
.tab-card :deep(.el-card__body) {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 16px;
}
.tab-hint {
  color: var(--el-text-color-secondary);
  font-size: 12.5px;
}
</style>