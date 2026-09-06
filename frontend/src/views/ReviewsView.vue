<template>
  <div>
    <!-- 服务端过滤：按 state 筛选 -->
    <el-card class="filter-card">
      <el-form inline class="filter-form" @submit.prevent>
        <el-form-item label="状态">
          <el-select
            v-model="query.state"
            placeholder="全部状态"
            clearable
            style="width: 160px"
            @change="onFilterChange"
          >
            <el-option label="排队中" value="queued" />
            <el-option label="审查成功" value="completed" />
            <el-option label="已跳过" value="skipped" />
            <el-option label="失败" value="failed" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="onFilterChange">刷新</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card>
      <ReviewsTable :items="items" :loading="loading" @detail="goDetail" />

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
import ReviewsTable from '../components/ReviewsTable.vue'

const router = useRouter()

const items = ref<ReviewItem[]>([])
const total = ref(0)
const loading = ref(false)
const query = reactive({ state: '', limit: 10, offset: 0 })

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
.filter-form {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  row-gap: 4px;
  column-gap: 12px;
}
.filter-form .el-form-item {
  margin: 0;
}
.pager {
  margin-top: 16px;
  justify-content: flex-end;
}
</style>