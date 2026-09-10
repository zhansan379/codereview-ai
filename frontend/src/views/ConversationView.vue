<template>
  <div>
    <el-page-header @back="$router.back()" :content="$t('conversation.pageTitle', { id })" />

    <div v-loading="loading" class="body">
      <el-empty
        v-if="!loading && items.length === 0"
        :description="$t('conversation.emptyDesc')"
      />
      <SwimLaneConversation v-else :items="items" />
    </div>

    <div v-if="hasMore && !loading" class="more">
      <el-button :loading="loadingMore" @click="loadMore">
        {{ $t('conversation.loadMore', { shown: items.length, total }) }}
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 原始对话页：仅负责拉取 + 分页懒加载；展示交给自定义组件 SwimLaneConversation
 * （「任务类型泳道 + 任务卡片」回放，对齐 open-code-review 的会话详情页）。
 */
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { fetchReviewConversation, type ConversationItem, type ConversationPage } from '../api'
import SwimLaneConversation from '../components/SwimLaneConversation.vue'

const route = useRoute()
const id = Number(route.params.id)
const items = ref<ConversationItem[]>([])
const loading = ref(false)
const loadingMore = ref(false)
const total = ref(0)
const hasMore = ref(false)
// 分页懒加载：每页取 PAGE 条，翻页追加到 items；offset 指向下一页起点。
const PAGE = 30

function applyPage(page: ConversationPage) {
  items.value = items.value.concat(page.items)
  total.value = page.total
  hasMore.value = page.has_more
}

async function load() {
  loading.value = true
  try {
    const page = await fetchReviewConversation(id, 0, PAGE)
    applyPage(page)
  } finally {
    loading.value = false
  }
}

async function loadMore() {
  if (loadingMore.value) return
  loadingMore.value = true
  try {
    const page = await fetchReviewConversation(id, items.value.length, PAGE)
    applyPage(page)
  } finally {
    loadingMore.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.body {
  margin-top: 16px;
}
.more {
  display: flex;
  justify-content: center;
  margin-top: 16px;
}
</style>