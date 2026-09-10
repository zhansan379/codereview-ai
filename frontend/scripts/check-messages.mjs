// 把每条词条喂给 vue-i18n 的消息编译器。
// 起因：裸 `@` 会被当成链接消息语法（@:key），编译期抛 INVALID_LINKED_FORMAT(10），
// 而 `vite build` 不编译消息、静态 key 检查也只看「键在不在」，两道关都拦不住。
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const { baseCompile } = require('@intlify/message-compiler')

const dir = 'src/locales/modules'
let total = 0
const bad = []

function walk(node, path, mod, loc) {
  for (const [k, v] of Object.entries(node ?? {})) {
    const p = path ? `${path}.${k}` : k
    if (v && typeof v === 'object') {
      walk(v, p, mod, loc)
    } else if (typeof v === 'string') {
      total++
      // baseCompile 默认不抛，编译错误只走 onError 回调——不传就静默通过
      try {
        baseCompile(v, {
          onError: (e) => bad.push({ mod, loc, key: p, val: v, msg: `${e.code} ${e.message}` }),
        })
      } catch (e) {
        bad.push({ mod, loc, key: p, val: v, msg: `throw ${e.message}` })
      }
    }
  }
}

for (const f of readdirSync(dir)) {
  const src = readFileSync(join(dir, f), 'utf8')
  const body = src.replace(/^[\s\S]*?export default/, '').replace(/;?\s*$/, '')
  const obj = eval('(' + body + ')')
  const mod = f.replace(/\.ts$/, '')
  for (const loc of Object.keys(obj)) walk(obj[loc], '', mod, loc)
}

console.log(`编译 ${total} 条消息，失败 ${bad.length} 条`)
const byKey = new Map()
for (const b of bad) byKey.set(`${b.mod}.${b.key}`, b)
for (const b of byKey.values()) {
  console.log(`  ${b.mod}.${b.key}  [${b.loc}] ${b.msg}`)
  console.log(`      ${b.val.slice(0, 90)}`)
}
process.exit(bad.length ? 1 : 0)
