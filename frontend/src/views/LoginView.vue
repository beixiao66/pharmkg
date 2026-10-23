<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api, { humanizeError } from '../api'

// ## 设计意图（这一版为什么长这样）
//
// 上一版是一张 420px 的默认样式 Element 卡片，**信息量几乎为零**：
// 用户看不出"这是什么系统、能做什么"，也不知道自己为什么要注册。
//
// 这一版改成**左右分栏**：
//   左 = 品牌与价值主张（标题 + 一句话说明 + 4 条真实能力 + 图谱规模数字）
//   右 = 表单卡片（登录 / 注册 分段切换）
//
// 左栏那些数字是**真的**（63,931 节点 / 505,445 关系 / 150,522 对相互作用），
// 来自 data/quality/graph_quality.json —— 不是装饰性文案。
// 对一个"知识图谱系统"来说，规模本身就是可信度的一部分。
//
// 移动端（< 900px）左栏收成顶部的一段摘要，表单优先。
const emit = defineEmits(['logged-in'])

//: 'login' | 'register' —— 一个卡片两种模式，而不是两个页面
const mode = ref('login')
const username = ref('')
const password = ref('')
const busy = ref(false)
//: 提交过才显示校验错误，避免一进页面就红一片
const touched = ref(false)


const FEATURES = [
  { t: '自然语言提问', d: '问「A 和 B 能一起吃吗」，命中即给分级与出处' },
  { t: '药箱联用风险', d: '把自己在吃的药放进去，逐对检查相互作用' },
  { t: '人群禁忌提醒', d: '孕期、哺乳、儿童等标签与说明书禁忌比对' },
  { t: '证据可回溯', d: '每条结论都能展开看实际执行的 Cypher 与证据子图' },
]

const uErr = computed(() => {
  if (!touched.value) return ''
  if (username.value.trim().length < 3) return '用户名至少 3 个字符'
  return ''
})
const pErr = computed(() => {
  if (!touched.value) return ''
  if (password.value.length < 6) return '密码至少 6 个字符'
  return ''
})
const canSubmit = computed(() =>
  username.value.trim().length >= 3 && password.value.length >= 6)

async function submit() {
  touched.value = true
  if (!canSubmit.value || busy.value) return
  busy.value = true
  try {
    const path = mode.value === 'login' ? '/auth/login' : '/auth/register'
    const { data } = await api.post(path, {
      username: username.value.trim(), password: password.value,
    })
    localStorage.setItem('token', data.access_token)
    ElMessage.success(`已登录：${data.username}`)
    emit('logged-in', data.access_token)
  } catch (e) {
    // ⚠️ **不要把原始 detail 直接甩给用户**。第一版写的是
    //    `typeof d === 'string' ? d : JSON.stringify(d)` ——
    //    而 422 的 detail 是**数组**，于是整坨
    //    `[{"type":"string_too_short","loc":["body","password"],...}]` 弹在了页面上。
    //    现在统一走 humanizeError()（见 src/api.js），它会翻成人话。
    ElMessage.error(humanizeError(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="auth">
    <!-- 左：品牌与价值主张 -->
    <section class="auth__brand">
      <div class="auth__brandInner">
        <p class="auth__logo">
          <span class="auth__mark" aria-hidden="true">Rx</span>
          pharmkg
        </p>
        <h1 class="auth__title">医药查询<br />与智能用药</h1>
        <p class="auth__lede">
          把说明书里的禁忌与相互作用，翻译成一句能看懂的话——
          并且每一句都能指出它来自哪条记录。
        </p>

        <ul class="auth__feats">
          <li v-for="f in FEATURES" :key="f.t">
            <span class="auth__tick" aria-hidden="true">✓</span>
            <span>
              <b>{{ f.t }}</b>
              <i>{{ f.d }}</i>
            </span>
          </li>
        </ul>

        <dl class="auth__stats">
          <div><dt>63,931</dt><dd>图谱节点</dd></div>
          <div><dt>505,445</dt><dd>关系</dd></div>
          <div><dt>150,522</dt><dd>相互作用对</dd></div>
        </dl>
      </div>
    </section>

    <!-- 右：表单 -->
    <section class="auth__panel">
      <div class="auth__card">
        <div class="auth__seg" role="tablist" aria-label="登录或注册">
          <button type="button" role="tab"
                  :aria-selected="mode === 'login'"
                  :class="['auth__segBtn', { on: mode === 'login' }]"
                  data-test="tab-login" @click="mode = 'login'">登录</button>
          <button type="button" role="tab"
                  :aria-selected="mode === 'register'"
                  :class="['auth__segBtn', { on: mode === 'register' }]"
                  data-test="tab-register" @click="mode = 'register'">注册</button>
        </div>

        <h2 class="auth__cardTitle">
          {{ mode === 'login' ? '欢迎回来' : '创建账号' }}
        </h2>
        <p class="auth__cardSub">
          {{ mode === 'login'
            ? '登录后可以保存药箱与提问历史。'
            : '注册即创建，不需要邮箱验证。' }}
        </p>

        <form class="auth__form" @submit.prevent="submit">
          <label class="auth__label" for="ph-user">用户名</label>
          <el-input id="ph-user" v-model="username" size="large"
                    placeholder="至少 3 个字符" autocomplete="username"
                    data-test="username" :class="{ 'is-bad': uErr }" />
          <p v-if="uErr" class="auth__err" role="alert">{{ uErr }}</p>

          <label class="auth__label" for="ph-pass">密码</label>
          <el-input id="ph-pass" v-model="password" size="large" type="password"
                    show-password placeholder="至少 6 个字符"
                    autocomplete="current-password"
                    data-test="password" :class="{ 'is-bad': pErr }" />
          <p v-if="pErr" class="auth__err" role="alert">{{ pErr }}</p>

          <el-button type="primary" size="large" native-type="submit"
                     class="auth__submit" :loading="busy"
                     data-test="login">
            {{ busy ? '处理中…' : (mode === 'login' ? '登录' : '注册并登录') }}
          </el-button>

          <p class="auth__switch">
            {{ mode === 'login' ? '还没有账号？' : '已经有账号了？' }}
            <button type="button" class="auth__link" data-test="register"
                    @click="mode = mode === 'login' ? 'register' : 'login'">
              {{ mode === 'login' ? '去注册' : '去登录' }}
            </button>
          </p>
        </form>

        <p class="auth__legal">
          图谱数据仅供学习参考，不能替代医生或药师的判断。
          本系统不提供诊断与处方建议。
        </p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.auth {
  display: grid;
  grid-template-columns: 40fr 60fr;
  min-height: 100%;
  /* 左栏深色、右栏浅底 —— 一眼分出"品牌"与"操作" */
  background: var(--ph-page);
}

/* ── 左栏 ───────────────────────────────────────────────── */
.auth__brand {
  position: relative;
  overflow: hidden;
  display: flex;
  align-items: center;
  padding: 0 clamp(24px, 2.6vw, 44px);
  color: #fff;
  background:
    radial-gradient(120% 120% at 0% 0%, #2563eb 0%, #1d4ed8 42%, #16307f 100%);
}
/* 一层柔光，避免大块纯色显得平 */
.auth__brand::after {
  content: '';
  position: absolute;
  inset: -20% -10% auto auto;
  width: 520px; height: 520px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(255,255,255,.16), transparent 62%);
  pointer-events: none;
}
.auth__brandInner { position: relative; width: 100%; max-width: 330px; margin: 0 auto; }

.auth__logo {
  display: flex; align-items: center; gap: var(--ph-s2);
  margin: 0 0 34px;
  font-size: 15px; font-weight: 650; letter-spacing: .12em;
  text-transform: lowercase; opacity: .95;
}
.auth__mark {
  display: inline-grid; place-items: center;
  width: 30px; height: 30px; border-radius: 8px;
  background: rgba(255,255,255,.16);
  border: 1px solid rgba(255,255,255,.28);
  font-size: 12px; font-weight: 700; letter-spacing: 0;
}

.auth__title {
  margin: 0 0 var(--ph-s4);
  font-size: clamp(24px, 2.2vw, 31px);
  line-height: 1.26;
  letter-spacing: -.01em;
  font-weight: 700;
}
.auth__lede {
  margin: 0 0 24px;
  max-width: 44ch;
  font-size: 13px; line-height: 1.75;
  color: rgba(255,255,255,.82);
}

.auth__feats { list-style: none; margin: 0 0 var(--ph-s6); padding: 0; }
.auth__feats li {
  display: flex; gap: 9px;
  padding: 10px 0;
  border-top: 1px solid rgba(255,255,255,.14);
}
.auth__feats li:last-child { border-bottom: 1px solid rgba(255,255,255,.14); }
.auth__tick {
  flex: none; margin-top: 2px;
  width: 18px; height: 18px; border-radius: 50%;
  display: grid; place-items: center;
  background: rgba(255,255,255,.18);
  font-size: 11px;
}
.auth__feats b { display: block; font-size: 13px; font-weight: 600; }
.auth__feats i {
  display: block; margin-top: 2px;
  font-style: normal; font-size: 12px; line-height: 1.6;
  color: rgba(255,255,255,.72);
}

.auth__stats {
  display: flex; flex-wrap: wrap; gap: 18px;
  margin: 0; padding-top: 14px;
}
.auth__stats div { min-width: 72px; }
.auth__stats dt {
  font-size: 16px; font-weight: 700; letter-spacing: -.02em;
}
.auth__stats dd {
  margin: 2px 0 0; font-size: 10.5px; color: rgba(255,255,255,.64);
}

/* ── 右栏 ───────────────────────────────────────────────── */
.auth__panel {
  display: flex; align-items: center; justify-content: center;
  /* ⚠️ `min-width: 0` 必须写。grid 子项默认 `min-width: auto`，
   *    会按"内容最小宽度"决定轨道，于是卡片（含自定义 minimum 的输入框）
   *    把整条轨道撑得比视口还宽 —— 窄屏上表现为**卡片右侧被裁掉**。
   *    这是 grid 布局最常见的溢出原因，比加 `overflow:hidden` 治标得多。 */
  min-width: 0;
  padding: var(--ph-s6) var(--ph-s5);
}
.auth__card {
  width: 100%; max-width: 392px;
  padding: var(--ph-s6);
  background: var(--ph-surface);
  border: 1px solid var(--ph-line);
  border-radius: var(--ph-radius-lg);
  box-shadow: var(--ph-shadow);
}

/* 分段切换：用两段等高按钮，选中态用实心主色，未选用浅底 */
.auth__seg {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 2px; padding: 3px;
  background: #f1f5f9;
  border-radius: 10px;
}
.auth__segBtn {
  appearance: none; border: 0; cursor: pointer;
  min-height: 36px; border-radius: 8px;
  background: transparent; color: var(--ph-ink-2);
  font-family: inherit; font-size: 14px; font-weight: 600;
  transition: background .18s ease, color .18s ease, box-shadow .18s ease;
}
.auth__segBtn:hover { color: var(--ph-ink); }
.auth__segBtn.on {
  background: var(--ph-surface); color: var(--ph-brand);
  box-shadow: var(--ph-shadow-sm);
}

.auth__cardTitle {
  margin: var(--ph-s5) 0 var(--ph-s1);
  font-size: 22px; font-weight: 700; letter-spacing: -.01em;
}
.auth__cardSub {
  margin: 0 0 var(--ph-s5);
  font-size: 13px; line-height: 1.6; color: var(--ph-ink-2);
}

.auth__form { display: block; }
.auth__label {
  display: block; margin: var(--ph-s4) 0 var(--ph-s2);
  font-size: 13px; font-weight: 600; color: var(--ph-ink);
}
.auth__err {
  margin: var(--ph-s1) 0 0;
  font-size: 12px; color: var(--ph-danger);
}
.auth__submit {
  width: 100%; margin-top: var(--ph-s5);
  height: 44px; /* 触控目标 ≥44px */
  font-weight: 600;
}

.auth__switch {
  margin: var(--ph-s4) 0 0;
  font-size: 13px; color: var(--ph-ink-2); text-align: center;
}
.auth__link {
  appearance: none; border: 0; padding: 0 2px;
  background: none; cursor: pointer;
  color: var(--ph-brand); font: inherit; font-weight: 600;
  text-decoration: underline; text-underline-offset: 2px;
}

.auth__legal {
  margin: var(--ph-s5) 0 0; padding-top: var(--ph-s4);
  border-top: 1px solid var(--ph-line);
  font-size: 12px; line-height: 1.7; color: var(--ph-ink-3);
}

/* 校验失败的输入框描边（Element 自己的红字提示保留，这里只加边） */
.is-bad :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--ph-danger) inset;
}

/* ── 响应式：窄屏把左栏收成顶部摘要，表单优先 ─────────────── */
@media (max-width: 900px) {
  .auth { grid-template-columns: 1fr; }
  .auth__brand { padding: var(--ph-s6) var(--ph-s5); }
  .auth__feats, .auth__stats { display: none; } /* 手机上没有空间讲这些 */
  .auth__title { font-size: 28px; }
  .auth__lede { margin-bottom: var(--ph-s4); font-size: 14px; }
  .auth__panel { padding: var(--ph-s5) var(--ph-s4) var(--ph-s6); }
  .auth__card { box-shadow: var(--ph-shadow-sm); }
}</style>
