<template>
  <div>
    <!-- 服务端过滤：按 state 筛选 -->
    <el-card class="filter-card">
      <el-form inline>
        <el-form-item label="状态">
          <el-select
            v-model="query.state"
            placeholder="全部状态"
            clearable
            style="width: 160px"
            @change="onFilterChange"
          >
            <el-option label="排队中" value="queued" />
            <el-option label="审查成功" value="reviewed" />
            <el-option label="成功" value="success" />
            <el-option label="失败" value="failed" />
          </el-select>
        </el-form-item>
        <el-button type="primary" @click="onFilterChange">刷新</el-button>
      </el-form>
    </el-card>

    <el-card>
      <el-table :data="items" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column prop="provider" label="平台" width="90" />
        <el-table-column prop="repo_id" label="仓库 ID" min-width="120" />
        <el-table-column prop="pr_number" label="PR" width="80" />
        <el-table-column prop="score_total" label="评分" width="90" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="stateTag(row.state)">{{ row.state }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="queued_at" label="排队时间" width="180" />
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="goDetail(row.id)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 服务端分页 -->
      <el-pagination
        class="pager"
        layout="total, prev, pager, next, sizes"
        :total="total"
        :page-size="query.limit"
        :current-page="query.offset / query.limit + 1"
        :page-sizes="[10, 20, 50]"
        @current-change="onPageChange"
        @size-change="onSizeChange"
      />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { listReviews, type ReviewItem } from '../api'

const router = useRouter()

const items = ref<ReviewItem[]>([])
const total = ref(0)
const loading = ref(false)
const query = reactive({ state: '', limit: 10, offset: 0 })

function stateTag(state: string): any {
  if (state === 'reviewed' || state === 'success') return 'success'
  if (state === 'failed') return 'danger'
  return 'warning'
}

// 翻页/过滤均重新请求后端（服务端分页）
async function load() {
  loading.value = true
  try {
    const res = await listReviews({
      state: query.state || undefined,
      limit: query.limit,
      offset: query.offset,
    })
    items.value = res.items || []
    total.value = res.total || 0
  } finally {
    loading.value = false
  }
}

function onFilterChange() {
  query.offset = 0
  load()
}
function onPageChange(p: number) {
  query.offset = (p - 1) * query.limit
  load()
}
function onSizeChange(s: number) {
  query.limit = s
  query.offset = 0
  load()
}
function goDetail(id: number) {
  router.push(`/reviews/${id}`)
}

onMounted(load)
</script>

<style scoped>
.filter-card {
  margin-bottom: 16px;
}
.pager {
  margin-top: 16px;
  justify-content: flex-end;
}
</style>