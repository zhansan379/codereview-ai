<template>
  <div class="login-page">
    <!-- 左栏:渐变舞台 + 动画角色(样式参考 guohaolian/animatedlogin) -->
    <aside class="left-panel">
      <div class="logo">
        <span class="logo-badge"><AppLogo :size="20" /></span>
        <span class="logo-text">CodeReview AI</span>
      </div>

      <div class="characters-wrapper">
        <div class="characters-scene">
          <!-- 紫色方块:眨眼;显示密码时会偷看 -->
          <div class="character char-purple" ref="purpleEl" :style="purple.body">
            <div class="eyes purple-eyes" :class="{ 'shake-head': shaking }" :style="purple.eyes">
              <div class="eyeball" ref="purpleEyeLEl" :style="{ height: purpleEyeH }">
                <div class="pupil" :style="purple.pupils" />
              </div>
              <div class="eyeball" :style="{ height: purpleEyeH }">
                <div class="pupil" :style="purple.pupils" />
              </div>
            </div>
          </div>

          <!-- 黑色方块:眨眼 -->
          <div class="character char-black" ref="blackEl" :style="black.body">
            <div class="eyes black-eyes" :class="{ 'shake-head': shaking }" :style="black.eyes">
              <div class="eyeball" ref="blackEyeLEl" :style="{ height: blackEyeH }">
                <div class="pupil" :style="black.pupils" />
              </div>
              <div class="eyeball" :style="{ height: blackEyeH }">
                <div class="pupil" :style="black.pupils" />
              </div>
            </div>
          </div>

          <!-- 橙色半圆:登录失败时露出沮丧嘴 -->
          <div class="character char-orange" ref="orangeEl" :style="orange.body">
            <div class="eyes orange-eyes" :class="{ 'shake-head': shaking }" :style="orange.eyes">
              <div class="bare-pupil" ref="orangePupilLEl" :style="orange.pupils" />
              <div class="bare-pupil" :style="orange.pupils" />
            </div>
            <div
              class="orange-mouth"
              :class="{ visible: orangeSad, 'shake-head': shaking }"
              :style="orange.mouth"
            />
          </div>

          <!-- 黄色圆角:常驻直线嘴 -->
          <div class="character char-yellow" ref="yellowEl" :style="yellow.body">
            <div class="eyes yellow-eyes" :class="{ 'shake-head': shaking }" :style="yellow.eyes">
              <div class="bare-pupil" ref="yellowPupilLEl" :style="yellow.pupils" />
              <div class="bare-pupil" :style="yellow.pupils" />
            </div>
            <div class="yellow-mouth" :class="{ 'shake-head': shaking }" :style="yellow.mouth" />
          </div>
        </div>
      </div>

      <p class="footer-note">{{ $t('login.tagline') }}</p>
    </aside>

    <!-- 右栏:登录表单 -->
    <main class="right-panel">
      <!-- 暗色 / 语言切换:与顶栏共用同一份单例状态,登录页即可直接调 -->
      <div class="panel-toggles">
        <el-switch
          v-model="isDark"
          class="dark-switch"
          :active-action-icon="Moon"
          :inactive-action-icon="Sunny"
          :aria-label="isDark ? $t('menu.toLight') : $t('menu.toDark')"
        />
        <!-- 语言切换:只有两种语言,点击即切,芯片显示目标语言 -->
        <button type="button" class="lang-chip" :title="$t('menu.language')" @click="toggleLocale">
          {{ locale === 'zh-CN' ? 'EN' : '中' }}
        </button>
      </div>

      <div class="form-container">
        <div class="sparkle-icon"><AppLogo :size="32" /></div>
        <div class="form-header">
          <h1>{{ $t('login.welcome') }}</h1>
          <p>{{ $t('login.subtitle') }}</p>
        </div>

        <div class="error-msg" v-show="errorMsg">{{ errorMsg }}</div>

        <form @submit.prevent="onSubmit">
          <div class="form-group">
            <label for="login-username" :class="{ 'error-label': errorField === 'username' }">
              {{ $t('login.username') }}
            </label>
            <div class="input-wrapper">
              <input
                id="login-username"
                v-model="form.username"
                type="text"
                autocomplete="off"
                :placeholder="$t('login.usernamePlaceholder')"
                :class="{ error: errorField === 'username' }"
                @focus="onUsernameFocus"
                @blur="onUsernameBlur"
                @input="updateCharacters"
              />
            </div>
          </div>

          <div class="form-group">
            <label for="login-password" :class="{ 'error-label': errorField === 'password' }">
              {{ $t('login.password') }}
            </label>
            <div class="input-wrapper">
              <input
                id="login-password"
                v-model="form.password"
                :type="showPassword ? 'text' : 'password'"
                autocomplete="off"
                :placeholder="$t('login.passwordPlaceholder')"
                :class="{ error: errorField === 'password' }"
                @focus="onPasswordFocus"
                @blur="onPasswordBlur"
                @input="updateCharacters"
              />
              <button type="button" class="toggle-password" @click="onTogglePassword">
                <svg
                  v-show="!showPassword"
                  width="20"
                  height="20"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                  <circle cx="12" cy="12" r="3" />
                </svg>
                <svg
                  v-show="showPassword"
                  width="20"
                  height="20"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <path
                    d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"
                  />
                  <line x1="1" y1="1" x2="23" y2="23" />
                </svg>
              </button>
            </div>
          </div>

          <button type="submit" class="btn-login" :disabled="loading">
            <span class="btn-text">
              {{ loading ? $t('login.submitting') : $t('login.submit') }}
            </span>
            <span class="btn-hover-content">
              <span>{{ $t('login.submit') }}</span>
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2.5"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <line x1="5" y1="12" x2="19" y2="12" />
                <polyline points="12 5 19 12 12 19" />
              </svg>
            </span>
          </button>
        </form>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
// 登录页改版:双栏布局 + 左栏四个纯 CSS 动画角色。
// 交互(眼神跟随鼠标、随机眨眼、输用户名时对视、输密码时回避、
// 显示密码时紫色角色偷看、登录失败沮丧摇头)移植自 guohaolian/animatedlogin。
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { Moon, Sunny } from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'
import { useDark } from '../composables/useDark'
import { useLocale } from '../composables/useLocale'
import AppLogo from '../components/AppLogo.vue'

const router = useRouter()
const auth = useAuthStore()
const { t } = useI18n()

// 暗色与语言开关:useDark/useLocale 都是模块级单例,这里切换后
// 登录进后台时顶栏的开关状态自然同步,无需额外通信。
const { isDark } = useDark()
const { locale, setLocale } = useLocale()

function toggleLocale() {
  setLocale(locale.value === 'zh-CN' ? 'en' : 'zh-CN')
}

const form = reactive({ username: 'admin', password: '' })
const loading = ref(false)
const errorMsg = ref('')
const errorField = ref<'' | 'username' | 'password'>('')
const showPassword = ref(false)

/* ---------------- 角色动画 ---------------- */

type StyleMap = Record<string, string>
interface Pose {
  body: StyleMap
  eyes: StyleMap
  pupils: StyleMap
  mouth: StyleMap
  blink: boolean
}

const purple = reactive<Pose>({ body: {}, eyes: {}, pupils: {}, mouth: {}, blink: false })
const black = reactive<Pose>({ body: {}, eyes: {}, pupils: {}, mouth: {}, blink: false })
const orange = reactive<Pose>({ body: {}, eyes: {}, pupils: {}, mouth: {}, blink: false })
const yellow = reactive<Pose>({ body: {}, eyes: {}, pupils: {}, mouth: {}, blink: false })

const purpleEl = ref<HTMLElement | null>(null)
const blackEl = ref<HTMLElement | null>(null)
const orangeEl = ref<HTMLElement | null>(null)
const yellowEl = ref<HTMLElement | null>(null)
const purpleEyeLEl = ref<HTMLElement | null>(null)
const blackEyeLEl = ref<HTMLElement | null>(null)
const orangePupilLEl = ref<HTMLElement | null>(null)
const yellowPupilLEl = ref<HTMLElement | null>(null)

const purpleEyeH = computed(() => (purple.blink ? '2px' : '18px'))
const blackEyeH = computed(() => (black.blink ? '2px' : '16px'))

const isLoginError = ref(false)
const shaking = ref(false)
const orangeSad = ref(false)

let mouseX = 0
let mouseY = 0
let isTyping = false
let isLookingAtEachOther = false
let isPasswordFocused = false
let isPurpleBlinking = false
let isBlackBlinking = false
let isPurplePeeking = false
let disposed = false
let typingTimer: ReturnType<typeof setTimeout> | null = null
let peekTimer: ReturnType<typeof setTimeout> | null = null
let errorRecoverTimer: ReturnType<typeof setTimeout> | null = null
const blinkTimers: Record<'purple' | 'black', ReturnType<typeof setTimeout> | null> = {
  purple: null,
  black: null,
}

const pupilT = (x: number, y: number): StyleMap => ({ transform: `translate(${x}px, ${y}px)` })

// 面部朝向:眼睛位置随鼠标偏移(限幅),身体反向轻微倾斜
function calcPosition(el: HTMLElement | null) {
  if (!el) return { faceX: 0, faceY: 0, bodySkew: 0 }
  const rect = el.getBoundingClientRect()
  const dx = mouseX - (rect.left + rect.width / 2)
  const dy = mouseY - (rect.top + rect.height / 3)
  return {
    faceX: Math.max(-15, Math.min(15, dx / 20)),
    faceY: Math.max(-10, Math.min(10, dy / 30)),
    bodySkew: Math.max(-6, Math.min(6, -dx / 120)),
  }
}

// 瞳孔在眼眶内朝鼠标方向的偏移(限幅)
function calcPupilOffset(el: HTMLElement | null, maxDist: number) {
  if (!el) return { x: 0, y: 0 }
  const rect = el.getBoundingClientRect()
  const dx = mouseX - (rect.left + rect.width / 2)
  const dy = mouseY - (rect.top + rect.height / 2)
  const dist = Math.min(Math.sqrt(dx * dx + dy * dy), maxDist)
  const angle = Math.atan2(dy, dx)
  return { x: Math.cos(angle) * dist, y: Math.sin(angle) * dist }
}

function updateCharacters() {
  const showingPwd = form.password.length > 0 && showPassword.value
  const lookingAway = isPasswordFocused && !showPassword.value
  const p = calcPosition(purpleEl.value)
  const b = calcPosition(blackEl.value)
  const o = calcPosition(orangeEl.value)
  const y = calcPosition(yellowEl.value)

  // ---- 紫色 ----
  if (showingPwd) purple.body = { transform: 'skewX(0deg)', height: '370px' }
  else if (lookingAway) purple.body = { transform: 'skewX(-14deg) translateX(-20px)', height: '410px' }
  else if (isTyping) purple.body = { transform: `skewX(${p.bodySkew - 12}deg) translateX(40px)`, height: '410px' }
  else purple.body = { transform: `skewX(${p.bodySkew}deg)`, height: '370px' }

  purple.blink = isPurpleBlinking
  if (isLoginError.value) {
    purple.eyes = { left: '30px', top: '55px' }
    purple.pupils = pupilT(-3, 4)
  } else if (lookingAway) {
    purple.eyes = { left: '20px', top: '25px' }
    purple.pupils = pupilT(-5, -5)
  } else if (showingPwd) {
    purple.eyes = { left: '20px', top: '35px' }
    // 其他角色回避时,紫色角色偶尔偷看
    purple.pupils = isPurplePeeking ? pupilT(4, 5) : pupilT(-4, -4)
  } else if (isLookingAtEachOther) {
    purple.eyes = { left: '55px', top: '65px' }
    purple.pupils = pupilT(3, 4)
  } else {
    purple.eyes = { left: `${45 + p.faceX}px`, top: `${40 + p.faceY}px` }
    const po = calcPupilOffset(purpleEyeLEl.value, 5)
    purple.pupils = { transform: `translate(${po.x}px, ${po.y}px)` }
  }

  // ---- 黑色 ----
  if (showingPwd) black.body = { transform: 'skewX(0deg)' }
  else if (lookingAway) black.body = { transform: 'skewX(12deg) translateX(-10px)' }
  else if (isLookingAtEachOther) black.body = { transform: `skewX(${b.bodySkew * 1.5 + 10}deg) translateX(20px)` }
  else if (isTyping) black.body = { transform: `skewX(${b.bodySkew * 1.5}deg)` }
  else black.body = { transform: `skewX(${b.bodySkew}deg)` }

  black.blink = isBlackBlinking
  if (isLoginError.value) {
    black.eyes = { left: '15px', top: '40px' }
    black.pupils = pupilT(-3, 4)
  } else if (lookingAway) {
    black.eyes = { left: '10px', top: '20px' }
    black.pupils = pupilT(-4, -5)
  } else if (showingPwd) {
    black.eyes = { left: '10px', top: '28px' }
    black.pupils = pupilT(-4, -4)
  } else if (isLookingAtEachOther) {
    black.eyes = { left: '32px', top: '12px' }
    black.pupils = pupilT(0, -4)
  } else {
    black.eyes = { left: `${26 + b.faceX}px`, top: `${32 + b.faceY}px` }
    const bo = calcPupilOffset(blackEyeLEl.value, 4)
    black.pupils = { transform: `translate(${bo.x}px, ${bo.y}px)` }
  }

  // ---- 橙色 ----
  orange.body = showingPwd ? { transform: 'skewX(0deg)' } : { transform: `skewX(${o.bodySkew}deg)` }
  if (isLoginError.value) {
    orange.eyes = { left: '60px', top: '95px' }
    orange.pupils = pupilT(-3, 4)
    orange.mouth = { left: `${80 + o.faceX}px`, top: '130px' }
  } else if (lookingAway) {
    orange.eyes = { left: '50px', top: '75px' }
    orange.pupils = pupilT(-5, -5)
    orange.mouth = {}
  } else if (showingPwd) {
    orange.eyes = { left: '50px', top: '85px' }
    orange.pupils = pupilT(-5, -4)
    orange.mouth = {}
  } else {
    orange.eyes = { left: `${82 + o.faceX}px`, top: `${90 + o.faceY}px` }
    const oo = calcPupilOffset(orangePupilLEl.value, 5)
    orange.pupils = { transform: `translate(${oo.x}px, ${oo.y}px)` }
    orange.mouth = {}
  }

  // ---- 黄色 ----
  yellow.body = showingPwd ? { transform: 'skewX(0deg)' } : { transform: `skewX(${y.bodySkew}deg)` }
  if (isLoginError.value) {
    yellow.eyes = { left: '35px', top: '45px' }
    yellow.pupils = pupilT(-3, 4)
    yellow.mouth = { left: '30px', top: '92px', transform: 'rotate(-8deg)' }
  } else if (lookingAway) {
    yellow.eyes = { left: '20px', top: '30px' }
    yellow.pupils = pupilT(-5, -5)
    yellow.mouth = { left: '15px', top: '78px', transform: 'rotate(0deg)' }
  } else if (showingPwd) {
    yellow.eyes = { left: '20px', top: '35px' }
    yellow.pupils = pupilT(-5, -4)
    yellow.mouth = { left: '10px', top: '88px', transform: 'rotate(0deg)' }
  } else {
    yellow.eyes = { left: `${52 + y.faceX}px`, top: `${40 + y.faceY}px` }
    const yo = calcPupilOffset(yellowPupilLEl.value, 5)
    yellow.pupils = { transform: `translate(${yo.x}px, ${yo.y}px)` }
    yellow.mouth = {
      left: `${40 + y.faceX}px`,
      top: `${88 + y.faceY}px`,
      transform: 'rotate(0deg)',
    }
  }
}

// 紫色/黑色随机眨眼(3~7 秒一次,闭眼 150ms)
function scheduleBlink(which: 'purple' | 'black') {
  blinkTimers[which] = setTimeout(() => {
    if (disposed) return
    if (which === 'purple') isPurpleBlinking = true
    else isBlackBlinking = true
    updateCharacters()
    blinkTimers[which] = setTimeout(() => {
      if (disposed) return
      if (which === 'purple') isPurpleBlinking = false
      else isBlackBlinking = false
      updateCharacters()
      scheduleBlink(which)
    }, 150)
  }, Math.random() * 4000 + 3000)
}

// 显示密码且有内容时,紫色角色定期偷看
function schedulePeek() {
  if (disposed || form.password.length === 0 || !showPassword.value) return
  peekTimer = setTimeout(() => {
    if (disposed || form.password.length === 0 || !showPassword.value) return
    isPurplePeeking = true
    updateCharacters()
    peekTimer = setTimeout(() => {
      if (disposed) return
      isPurplePeeking = false
      updateCharacters()
      schedulePeek()
    }, 800)
  }, Math.random() * 3000 + 2000)
}

function onUsernameFocus() {
  isTyping = true
  isLookingAtEachOther = true
  if (typingTimer) clearTimeout(typingTimer)
  typingTimer = setTimeout(() => {
    isLookingAtEachOther = false
    updateCharacters()
  }, 800)
  updateCharacters()
}

function onUsernameBlur() {
  isTyping = false
  isLookingAtEachOther = false
  if (typingTimer) {
    clearTimeout(typingTimer)
    typingTimer = null
  }
  updateCharacters()
}

function onPasswordFocus() {
  isPasswordFocused = true
  updateCharacters()
}

function onPasswordBlur() {
  isPasswordFocused = false
  updateCharacters()
}

function onTogglePassword() {
  showPassword.value = !showPassword.value
  updateCharacters()
  if (showPassword.value && form.password.length > 0) schedulePeek()
}

// 登录失败:全体角色沮丧 + 摇头,2.5 秒后恢复
async function triggerLoginError() {
  if (errorRecoverTimer) {
    clearTimeout(errorRecoverTimer)
    errorRecoverTimer = null
  }
  // 先摘掉摇头类并强制回流,连续失败也能重播动画
  shaking.value = false
  await nextTick()
  void document.body.offsetHeight
  isLoginError.value = true
  isPasswordFocused = false
  orangeSad.value = true
  updateCharacters()
  setTimeout(() => {
    if (!disposed) shaking.value = true
  }, 350)
  errorRecoverTimer = setTimeout(() => {
    isLoginError.value = false
    orangeSad.value = false
    shaking.value = false
    errorRecoverTimer = null
    updateCharacters()
  }, 2500)
}

/* ---------------- 登录 ---------------- */

async function onSubmit() {
  if (loading.value) return
  errorMsg.value = ''
  errorField.value = ''
  if (!form.username.trim()) {
    errorField.value = 'username'
    errorMsg.value = t('login.usernamePlaceholder')
    triggerLoginError()
    return
  }
  if (!form.password) {
    errorField.value = 'password'
    errorMsg.value = t('login.passwordPlaceholder')
    triggerLoginError()
    return
  }
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success(t('login.success'))
    router.push('/dashboard')
  } catch (e: any) {
    errorField.value = 'password'
    errorMsg.value = e?.response?.data?.detail || t('login.failed')
    triggerLoginError()
  } finally {
    loading.value = false
  }
}

function onMouseMove(e: MouseEvent) {
  mouseX = e.clientX
  mouseY = e.clientY
  if (!isTyping && !isLoginError.value) updateCharacters()
}

onMounted(() => {
  mouseX = window.innerWidth / 2
  mouseY = window.innerHeight / 2
  window.addEventListener('mousemove', onMouseMove)
  updateCharacters()
  scheduleBlink('purple')
  scheduleBlink('black')
})

onBeforeUnmount(() => {
  disposed = true
  window.removeEventListener('mousemove', onMouseMove)
  ;[typingTimer, peekTimer, errorRecoverTimer, blinkTimers.purple, blinkTimers.black].forEach(
    (id) => id && clearTimeout(id),
  )
})
</script>

<style scoped>
.login-page {
  /* fixed 铺满视口:项目未重置 body 的默认 8px margin,常规流式布局会撑出滚动条 */
  position: fixed;
  inset: 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
}

/* ============ 左栏:角色舞台 ============ */
.left-panel {
  position: relative;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background: linear-gradient(135deg, #d4d0dc 0%, #c8c4d0 50%, #bbb7c5 100%);
  padding: 40px 48px;
  overflow: hidden;
}

/* 装饰性光斑 */
.left-panel::after {
  content: '';
  position: absolute;
  top: 20%;
  right: 15%;
  width: 260px;
  height: 260px;
  background: rgba(180, 170, 200, 0.25);
  border-radius: 50%;
  filter: blur(80px);
}

.left-panel::before {
  content: '';
  position: absolute;
  bottom: 15%;
  left: 10%;
  width: 350px;
  height: 350px;
  background: rgba(200, 195, 210, 0.2);
  border-radius: 50%;
  filter: blur(100px);
}

.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 16px;
  font-weight: 600;
  color: #fff;
  position: relative;
  z-index: 10;
}

.logo-badge {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  background: rgba(255, 255, 255, 0.15);
  backdrop-filter: blur(8px);
  border-radius: 6px;
  color: #fff;
}

.footer-note {
  margin: 0;
  font-size: 13px;
  color: rgba(80, 70, 90, 0.7);
  position: relative;
  z-index: 10;
}

/* ============ 角色 ============ */
.characters-wrapper {
  position: relative;
  z-index: 10;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  height: 420px;
}

.characters-scene {
  position: relative;
  width: 480px;
  height: 360px;
}

.character {
  position: absolute;
  bottom: 0;
  transition: all 0.7s ease-in-out;
  transform-origin: bottom center;
}

.char-purple {
  left: 60px;
  width: 170px;
  height: 370px;
  background: #6c3ff5;
  border-radius: 10px 10px 0 0;
  z-index: 1;
}

.char-black {
  left: 220px;
  width: 115px;
  height: 290px;
  background: #2d2d2d;
  border-radius: 8px 8px 0 0;
  z-index: 2;
}

.char-orange {
  left: 0;
  width: 230px;
  height: 190px;
  background: #ff9b6b;
  border-radius: 115px 115px 0 0;
  z-index: 3;
}

.char-yellow {
  left: 290px;
  width: 135px;
  height: 215px;
  background: #e8d754;
  border-radius: 68px 68px 0 0;
  z-index: 4;
}

.eyes {
  position: absolute;
  display: flex;
  transition: all 0.7s ease-in-out;
}

.purple-eyes { left: 45px; top: 40px; gap: 28px; }
.purple-eyes .eyeball { width: 18px; height: 18px; }
.purple-eyes .pupil { width: 7px; height: 7px; }

.black-eyes { left: 26px; top: 32px; gap: 20px; }
.black-eyes .eyeball { width: 16px; height: 16px; }
.black-eyes .pupil { width: 6px; height: 6px; }

.orange-eyes { left: 82px; top: 90px; gap: 28px; }
.yellow-eyes { left: 52px; top: 40px; gap: 20px; }

.eyeball {
  border-radius: 50%;
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: height 0.15s ease;
  overflow: hidden;
}

.pupil {
  border-radius: 50%;
  background: #2d2d2d;
  transition: transform 0.1s ease-out;
}

.bare-pupil {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #2d2d2d;
  transition: transform 0.7s ease-in-out;
}

.yellow-mouth {
  position: absolute;
  left: 40px;
  top: 88px;
  width: 50px;
  height: 4px;
  background: #2d2d2d;
  border-radius: 2px;
  transition: all 0.7s ease-in-out;
}

.orange-mouth {
  position: absolute;
  left: 90px;
  top: 120px;
  width: 28px;
  height: 14px;
  border: 3px solid #2d2d2d;
  border-top: none;
  border-radius: 0 0 14px 14px;
  opacity: 0;
  transition: all 0.7s ease-in-out;
}

.orange-mouth.visible {
  opacity: 1;
}

/* 摇头:面部部件左右摆动衰减 */
@keyframes shake-head {
  0%, 100% { translate: 0 0; }
  10% { translate: -9px 0; }
  20% { translate: 7px 0; }
  30% { translate: -6px 0; }
  40% { translate: 5px 0; }
  50% { translate: -4px 0; }
  60% { translate: 3px 0; }
  70% { translate: -2px 0; }
  80% { translate: 1px 0; }
  90% { translate: -0.5px 0; }
}

.shake-head {
  animation: shake-head 0.8s cubic-bezier(0.36, 0.07, 0.19, 0.97) both;
}

/* ============ 右栏:表单 ============ */
.right-panel {
  position: relative;
  display: flex;
  background: var(--el-bg-color);
  padding: 40px;
  overflow-y: auto;
}

/* 右上角:暗色/语言开关 */
.panel-toggles {
  position: absolute;
  top: 24px;
  right: 32px;
  display: flex;
  align-items: center;
  gap: 14px;
  z-index: 10;
}

.lang-chip {
  min-width: 36px;
  height: 26px;
  padding: 0 9px;
  border-radius: 13px;
  border: 1px solid var(--el-border-color);
  background: transparent;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  transition:
    color 0.2s,
    border-color 0.2s;
}

.lang-chip:hover {
  color: var(--el-text-color-primary);
  border-color: var(--el-border-color-darker);
}

.form-container {
  /* margin:auto 居中:内容高于视口时也能正常滚动,不会被裁掉顶部 */
  margin: auto;
  width: 100%;
  max-width: 400px;
}

.sparkle-icon {
  display: flex;
  justify-content: center;
  margin-bottom: 24px;
  color: var(--el-color-primary);
}

.form-header {
  text-align: center;
  margin-bottom: 32px;
}

.form-header h1 {
  font-size: 28px;
  font-weight: 700;
  color: var(--el-text-color-primary);
  letter-spacing: -0.5px;
  margin: 0 0 6px;
}

.form-header p {
  font-size: 14px;
  color: var(--el-text-color-secondary);
  margin: 0;
}

.form-group {
  margin-bottom: 20px;
}

.form-group label {
  display: block;
  font-size: 13px;
  font-weight: 500;
  color: var(--el-text-color-regular);
  margin-bottom: 8px;
}

.form-group label.error-label {
  color: #dc2626;
}

.form-group .input-wrapper {
  position: relative;
}

.form-group input {
  width: 100%;
  height: 48px;
  border: none;
  border-bottom: 1.5px solid var(--el-border-color);
  padding: 0 40px 0 0;
  font-size: 15px;
  font-family: inherit;
  color: var(--el-text-color-primary);
  background: transparent;
  outline: none;
  transition: border-color 0.3s;
}

.form-group input:focus {
  border-bottom-color: #5b21b6;
}

.form-group input::placeholder {
  color: var(--el-text-color-placeholder);
}

.form-group input[type='password']:not(:placeholder-shown) {
  letter-spacing: 2px;
}

.form-group input.error {
  border-bottom-color: #dc2626;
}

/* 浏览器自动填充时压掉默认的淡蓝底 */
.form-group input:-webkit-autofill {
  -webkit-text-fill-color: var(--el-text-color-primary);
  transition: background-color 9999s ease-out;
}

.form-group input[type='password']::-ms-reveal,
.form-group input[type='password']::-ms-clear {
  display: none;
}

.toggle-password {
  position: absolute;
  right: 0;
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  background: none;
  border: none;
  cursor: pointer;
  color: var(--el-text-color-secondary);
  padding: 6px;
  transition: color 0.2s;
}

.toggle-password:hover {
  color: var(--el-text-color-primary);
}

.error-msg {
  padding: 10px 14px;
  font-size: 13px;
  color: #dc2626;
  background: rgba(220, 38, 38, 0.08);
  border: 1px solid rgba(220, 38, 38, 0.2);
  border-radius: 10px;
  margin-bottom: 16px;
}

/* 登录按钮:悬停时文字滑出、紫色底 + 箭头滑入 */
.btn-login {
  position: relative;
  width: 100%;
  height: 50px;
  border-radius: 25px;
  border: 1.5px solid #1a1a2e;
  background: #1a1a2e;
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  overflow: hidden;
  margin-top: 4px;
  transition: all 0.3s;
}

.btn-login .btn-text {
  display: inline-block;
  transition: all 0.3s;
}

.btn-login .btn-hover-content {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: #5b21b6;
  color: #fff;
  opacity: 0;
  transition: all 0.3s;
  border-radius: 25px;
}

.btn-login:hover .btn-text {
  transform: translateX(40px);
  opacity: 0;
}

.btn-login:hover .btn-hover-content {
  opacity: 1;
}

.btn-login:disabled {
  opacity: 0.65;
  cursor: default;
}

.btn-login:disabled .btn-text {
  transform: none;
  opacity: 1;
}

.btn-login:disabled .btn-hover-content {
  opacity: 0;
}

/* ============ 暗色适配 ============ */
html.dark .left-panel {
  background: linear-gradient(135deg, #2b2740 0%, #262239 50%, #211d30 100%);
}

html.dark .left-panel::after {
  background: rgba(130, 120, 170, 0.15);
}

html.dark .left-panel::before {
  background: rgba(120, 110, 160, 0.12);
}

html.dark .logo-badge {
  background: rgba(255, 255, 255, 0.08);
}

html.dark .footer-note {
  color: rgba(210, 205, 230, 0.5);
}

html.dark .btn-login {
  background: #eceaf4;
  border-color: #eceaf4;
  color: #1a1a2e;
}

html.dark .error-msg {
  background: rgba(220, 38, 38, 0.14);
}

/* ============ 响应式 ============ */
@media (max-width: 900px) {
  .login-page {
    grid-template-columns: 1fr;
  }

  .left-panel {
    display: none;
  }
}
</style>
