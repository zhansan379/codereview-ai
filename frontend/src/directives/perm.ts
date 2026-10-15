import type { Directive } from 'vue'
import { useAuthStore } from '../stores/auth'
import { t } from '../locales'

// v-perm="'code'" 或 v-perm="['code1', 'code2']"（任一命中即视为有权限）。
// 无权限时不隐藏元素，而是置灰禁用：加 is-disabled 类（Element Plus 禁用样式）+
// aria-disabled + 标题提示，并在 document 捕获阶段拦掉 click/keydown。
// 之所以不用原生 disabled 属性：浏览器会吞掉 disabled 控件上的鼠标事件，title 就弹不出来；
// 用 .quiet 修饰符可关掉标题提示（浮层里已另有说明时用）。
export const perm: Directive<HTMLElement, string | string[]> = {
  mounted(el, binding) {
    const auth = useAuthStore()
    const codes = Array.isArray(binding.value) ? binding.value : [binding.value]
    if (codes.some((code) => auth.hasPerm(code))) return

    el.classList.add('is-disabled')
    el.setAttribute('aria-disabled', 'true')
    el.style.cursor = 'not-allowed'
    if (!binding.modifiers.quiet) {
      el.setAttribute('title', t('common.noPermission'))
    }

    // 在 document 捕获阶段拦截：早于元素自身的 @click，且不受监听注册顺序影响。
    // 挂载点含自身及子节点（图标按钮点击目标是内部 svg）。
    const block = (e: Event) => {
      if (!(e.target instanceof Node) || !el.contains(e.target)) return
      e.stopImmediatePropagation()
      e.preventDefault()
    }
    const onKeydown = (e: Event) => {
      const key = (e as KeyboardEvent).key
      if (key === 'Enter' || key === ' ') block(e)
    }
    document.addEventListener('click', block, true)
    document.addEventListener('keydown', onKeydown, true)
    ;(el as any).__permCleanup = () => {
      document.removeEventListener('click', block, true)
      document.removeEventListener('keydown', onKeydown, true)
    }
  },
  unmounted(el) {
    ;(el as any).__permCleanup?.()
  },
}
