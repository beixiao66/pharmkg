import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
// ⚠️ 顺序要紧：设计 token 必须在 Element 的样式**之后**引入，
//    否则 `body`/`#app`/焦点样式会被 Element 的 reset 覆盖回去。
import './styles/theme.css'
import App from './App.vue'

createApp(App).use(ElementPlus).mount('#app')
