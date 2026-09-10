<template>
  <!--
    项目图标：`<` ✓ `>` —— 尖括号代表被审查的代码，中间的勾代表审查通过。
    描边全部用 currentColor，所以颜色跟随外层 color（顶栏用主题色，暗色模式自动跟着变）。
  -->
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 32 32"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    role="img"
    aria-label="CodeReview AI"
  >
    <path
      d="M11.5 9.5 L5.5 16 L11.5 22.5"
      stroke="currentColor"
      stroke-width="2.2"
      stroke-linecap="round"
      stroke-linejoin="round"
      opacity="0.5"
    />
    <path
      d="M20.5 9.5 L26.5 16 L20.5 22.5"
      stroke="currentColor"
      stroke-width="2.2"
      stroke-linecap="round"
      stroke-linejoin="round"
      opacity="0.5"
    />
    <path
      d="M12.6 16.4 L15.2 19 L19.4 13.4"
      stroke="currentColor"
      stroke-width="2.8"
      stroke-linecap="round"
      stroke-linejoin="round"
    />
  </svg>
</template>

<script setup lang="ts">
withDefaults(defineProps<{ size?: number | string }>(), { size: 24 })
</script>
