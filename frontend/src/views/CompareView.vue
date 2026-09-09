<template>
  <div>
    <el-page-header @back="$router.back()" :content="`对比上次审查 #${id}`" />

    <div v-loading="loading">
      <div class="bucket-grid">
        <el-card class="bucket bucket-new" shadow="never">
          <template #header>
            <div class="bucket-head">
              <el-tag type="danger">新增</el-tag>
              <span class="count">{{ result.new.length }}</span>
            </div>
          </template>
          <FindingBucketTable :rows="result.new" empty="本轮无新增问题" severity-tag="danger" />
        </el-card>

        <el-card class="bucket bucket-resolved" shadow="never">
          <template #header>
            <div class="bucket-head">
              <el-tag type="success">已解决</el-tag>
              <span class="count">{{ result.resolved.length }}</span>
            </div>
          </template>
          <FindingBucketTable :rows="result.resolved" empty="本轮已全部修复" severity-tag="success" />
        </el-card>

        <el-card class="bucket bucket-persisting" shadow="never">
          <template #header>
            <div class="bucket-head">
              <el-tag type="warning">持续存在</el-tag>
              <span class="count">{{ result.persisting.length }}</span>
            </div>
          </template>
          <FindingBucketTable :rows="result.persisting" empty="无持续存在的问题" severity-tag="warning" />
        </el-card>

        <el-card class="bucket bucket-not-reviewed" shadow="never">
          <template #header>
            <div class="bucket-head">
              <el-tag type="info">上次未覆盖（不算已修）</el-tag>
              <span class="count">{{ result.not_reviewed.length }}</span>
            </div>
          </template>
          <FindingBucketTable :rows="result.not_reviewed" empty="无" severity-tag="info" />
        </el-card>
      </div>

      <el-alert
        type="info"
        :closable="false"
        title="口径说明"
        description="对比取本任务与上一次 completed MR 任务的 findings 快照做指纹差。『已解决』指上次有、本轮确实审到（在覆盖集内）且不复现；『上次未覆盖』指问题仍在但本轮未审到该文件（未变更复用/缺失覆盖集时保守登记），不代表已修复。"
        style="margin-top: 16px"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { fetchReviewCompare, type CompareResult } from '../api'
import FindingBucketTable from '../components/FindingBucketTable.vue'

const route = useRoute()
const id = Number(route.params.id)
const result = ref<CompareResult>({ new: [], persisting: [], resolved: [], not_reviewed: [] })
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    result.value = await fetchReviewCompare(id)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.bucket-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 14px;
  margin-top: 16px;
  align-items: start;
}
.bucket-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.count {
  font-size: 13px;
  color: #606266;
}
.bucket :deep(.el-card__body) {
  padding: 8px 12px;
}
</style>