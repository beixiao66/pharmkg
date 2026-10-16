# pharmkg · 基于 AI 与知识图谱的医药查询与智能用药系统

用药安全领域的知识图谱问答系统：

- 以**公开中文医学知识图谱**为骨架，融合**药物相互作用数据**；
- 用**大语言模型从药品说明书抽取禁忌与特殊人群知识**，补齐公开数据缺失的部分；
- 在此之上提供**自然语言多轮问答**、**用药安全校验**与**图谱证据可视化**。

## 核心设计原则

| 原则 | 含义 |
|---|---|
| **可追溯** | 每条回答都附上实际执行的 Cypher 与图谱证据，可回溯到具体的边 |
| **不臆造** | 只依据图谱查询结果作答；图谱中无记录时明确拒答，禁止事实外推 |
| **数据合法** | 只用许可清晰的公开数据源，许可证与使用范围逐一声明 |

## 技术栈

| 层 | 选型 |
|---|---|
| 图数据库 | Neo4j 5.26（社区版） |
| 关系数据库 | PostgreSQL 16 |
| 后端 | Python 3.13 + FastAPI |
| 前端 | Vue3 + Vite（待 L5 阶段搭建） |
| 大模型 | 阿里云百炼（OpenAI 兼容接口），默认 `qwen-plus` |
| 容器 | Docker Compose（开发阶段只跑数据库） |

## 快速开始

### 1. 准备环境变量

```powershell
Copy-Item .env.example .env
# 打开 .env，把 changeme 换成真实密码
```

> `.env` 已被 `.gitignore` 排除，**永远不会提交**；`.env.example` 只放占位符。

### 2. 启动数据库

```powershell
docker-compose up -d
docker-compose ps          # 两个服务都应是 healthy
```

| 服务 | 地址 | 说明 |
|---|---|---|
| Neo4j Browser | <http://localhost:7474> | 浏览器打开可直接执行 Cypher |
| Neo4j Bolt | `bolt://localhost:7687` | 后端连接用 |
| PostgreSQL | `localhost:5432` | 库名与用户见 `.env` |

### 3. 启动后端

> ⚠️ **必须用 Python 3.13。** 本机 PATH 里的 `python` 是 3.9，直接用它会建出 3.9 环境——
> 而 3.9.0 自带的 OpenSSL 过旧，`pip` 连国内镜像时会报
> `SSLError(SSLEOFError(8, 'EOF occurred in violation of protocol'))`。
> 用 `py -3.13` 明确指定版本即可绕开。

```powershell
cd backend
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

> 依赖已实测可装：**Python 3.13.15** + 清华镜像（本机 pip 全局配置已指向该镜像）。

验证（两个数据库都连通时返回 `"status": "ok"`）：

```powershell
curl http://localhost:8000/health
```

接口文档：<http://localhost:8000/docs>

## 会话管理（L7 最小版）

按 §13.4，L7 排在 L5 前面的**唯一**理由：L5 要把每一轮的问题、改写、Cypher、回答落库
（这些是 L9 的数据来源），所以**数据表与写入接口必须先存在**。
所以这里只做这三张表 + 认证 + 会话/消息接口，**不做 UI，也不做药箱**（药箱是 L6 的输入）。

```powershell
$PY = ".\backend\.venv\Scripts\python.exe"

cd backend
& $PY -m alembic upgrade head          # 建表（app_user / conversation / message）
& $PY -m alembic current               # 看当前版本
& $PY -m alembic downgrade base        # 全撤回（实测可逆）
& $PY scripts\l7_selftest.py           # 端到端自测：33 项
```

**三张表**（§6.2）：

| 表 | 作用 | 关键列 |
|---|---|---|
| `app_user` | 用户 | `username UNIQUE`、`password_hash`（bcrypt）、`population_tags TEXT[]` |
| `conversation` | 一次会话 | `user_id`、`title`（首条问题前 20 字自动生成） |
| `message` | 一轮消息 | **`rewritten_query` / `cypher` / `evidence_json`** —— L9 评测的数据来源 |

**接口**：`POST /auth/register`、`POST /auth/login`、`GET /auth/me`；
`POST /conversations`、`GET /conversations`、`GET|POST /conversations/{id}/messages`。

**几个刻意的选择**（都有理由，写进了代码注释）：

1. **不用 `passlib`**（决策 #24）：passlib 1.7.4 读 `bcrypt.__about__.__version__`，
   而 bcrypt 4.x 删了这个私有属性 → 直接抛异常。直接用 `bcrypt` 只有三行。
2. **密码先 `sha256` 预散列再交给 bcrypt**：bcrypt 只取前 **72 字节**，超出部分**静默丢弃**
   ——两个前 72 字节相同的不同密码会互相通过校验，这是真实缺陷不是洁癖。
3. **缺令牌返回 401 而不是 403**：FastAPI 的 `HTTPBearer` 默认抛 403，但"没带令牌"语义上是
   **401（未认证）**；混用会让前端"跳登录页"的逻辑失效。
4. **用户不存在与密码错返回同一句话**：分开提示等于送一个用户名探测接口。
5. **查会话一律 `WHERE id=? AND user_id=?`**，不做"先取再比 owner"；
   查不到返回 **404** 而不是 403——403 等于承认这个 id 存在。
6. **`evidence` 的 ORM 属性叫 `evidence`、列名仍叫 `evidence_json`**：代码里读自然名字，
   表结构与 §6.2 一致。
7. **`alembic.ini` 保持纯 ASCII**：Python 的 configparser 按**本地编码**读它，
   中文 Windows 上是 GBK，任何 UTF-8 中文注释都会在 alembic 启动前就 `UnicodeDecodeError`。
   说明写在 `alembic/env.py` 里（`.py` 一定按 UTF-8 读）。

> ⚠️ `.env` 需要一行 `JWT_SECRET`。留空的话注册/登录会**直接报错**（不静默用默认密钥——
> 那等于任何人都能自己签发令牌）。生成：`python -c "import secrets; print(secrets.token_hex(32))"`

## 数据准备

> ⚠️ **下面所有脚本都用项目虚拟环境的解释器跑**：
> `backend\.venv\Scripts\python.exe`。
> 直接敲 `python` 会用 PATH 里的系统 Python（本机是 3.9），依赖不在那里，
> 报 `No module named 'pydantic_settings'` —— **不是环境坏了，是解释器选错了**。
> 为省事可先设个别名：
>
> ```powershell
> $PY = ".\backend\.venv\Scripts\python.exe"
> & $PY scripts/import_opencmkg.py --stats-only
> ```

原始数据体积约 33.5 MB，**不入库**（体积大 + 部分有再分发限制）。一条命令拉取并校验：

```powershell
& .\backend\.venv\Scripts\python.exe scripts\download_data.py
```

脚本特点：断点友好（已存在的文件自动跳过）、下载后校验 SHA-256 与体积、
结果写入 `data/raw/MANIFEST.json`。

```powershell
# 下面为简洁起见，省略 ".\backend\.venv\Scripts\python.exe" 前缀
python scripts/download_data.py --list             # 只看数据源清单，不下载
python scripts/download_data.py --only ddinter     # 只下 DDInter
python scripts/download_data.py --verify-only      # 离线校验本地文件完整性
python scripts/download_data.py --check-upstream   # HEAD 比对上游，看数据是否更新过
python scripts/inspect_raw.py                      # 体检：格式、规模、数据质量
```

> 📄 **数据实际长什么样，以 [`docs/数据源实测报告.md`](docs/数据源实测报告.md) 为准。**
> 该报告由 `scripts/inspect_raw.py` 对真实文件统计生成，纠正了方案文档中多处推测性描述
> （例如 DDInter 的 CSV 其实没有机制与处理建议；OpenCMKG 的三元组文件是带引号的 CSV，
> 必须用 `csv` 模块解析，否则会静默丢掉 2,171 行）。

## 导入图谱（L1）

原始数据 → Neo4j 图谱。**写入一律用 `MERGE`，重跑不会产生重复**（L1、L2 都已验证）。

```powershell
$PY = ".\backend\.venv\Scripts\python.exe"

# L1 骨架：疾病 / 药物 / 症状 / 食物 / 科室 / 生产商 / 检查 + SameAs
& $PY scripts\import_opencmkg.py --stats-only   # 只读：图规模 + 冒烟测试
& $PY scripts\import_opencmkg.py                # 全量（约 17 秒）

# L2 相互作用：160,235 对去重后写成 150,522 条 INTERACTS_WITH 边
& $PY scripts\import_ddi.py --stats-only        # 只读：图规模 + 验收查询
& $PY scripts\import_ddi.py                     # 全量（约 9 秒）
& $PY scripts\import_ddi.py --reset             # 只清 source=ddinter 的数据，不动 L1 骨架
```

导入后的完整规模、两个覆盖口径、验收查询见
[`docs/数据源实测报告.md`](docs/数据源实测报告.md) §7（L1）与 §8（L2）。

> ⚠️ **查图谱时一定要带 Label**：有 1,771 个名字跨多个类型（`木瓜` 既是食物又是药、
> `谷氨酰胺` 既是药又是化验项），不带 Label 的 `MATCH (n {name:...})` 会命中多个节点。

> ⚠️ **不要拿"覆盖率"一个数字说事**。L2 之后图谱里有两类药：
> `source=opencmkg`（4,301 个，有适应症等背景关系）与
> `source=ddinter`（1,387 个，**只有相互作用数据**）。
> **DDI 数据覆盖 100%，药品详情覆盖 21.4%**——两个口径必须分开陈述。

## 说明书采集（L3）

选样 → 采集 → 离线重解析，三段。产物落在 `data/collected/`：

```powershell
$PY = ".\backend\.venv\Scripts\python.exe"

& $PY scripts\select_high_risk_drugs.py        # ① 按 Major 级频次选出 40 种高危药
& $PY scripts\collect_inserts.py --login       # ② 先登录（脚本只读 cookie，不接收密码）
& $PY scripts\collect_inserts.py               #    全量采集，**30 秒/请求**
& $PY scripts\collect_inserts.py --candidates  #    补采搜索结果页，落地全部候选
& $PY scripts\reparse_inserts.py --check       # ③ 离线重解析：只报告前后差异
& $PY scripts\reparse_inserts.py               #    重写 text/*.txt 与 index.csv
```

> ⚠️ **请求间隔不要低于 30 秒**——实测 6 秒会在十几个药之后触发站点的人机验证。
>
> ⚠️ **改解析规则不要重新采集**。`raw/*.html` 是采集当时的原始页面，
> 用 `scripts\reparse_inserts.py` 从本地重放即可——重采会改变样本，
> 还可能再次触发人机验证。这条纪律让本次「三处解析缺陷修复」做到了**零次重采**。

采集与重解析共用 `scripts/insert_io.py` 里的**同一个解析器**，两处产物不会漂移。
产物结构、解析器的四处坑（换行 / 丢节 / 清单被覆写 / 表格行列）、【禁忌】信息量差异，
见 [`docs/数据源实测报告.md`](docs/数据源实测报告.md) §9。

## 说明书知识抽取与对齐（L4）

**L4 的交付物不是"一段文字"，是图谱里的边**，所以评分分**两层**
（决策 #33 / #34，见 [`docs/需求与技术方案基线.md`](docs/需求与技术方案基线.md) §2）：

| 方法 | 输入 | 输入 token | 第一层（抽取） | 第二层（边，`+⑤ LLM`） |
|---|---|---|---|---|
| **朴素基线** | 整篇说明书 | 451,484 | F1 **1.000** | F1 **0.624** |
| **消融层级 2** | 【禁忌】一节 + JSON + `generic_only` | **16,385（3.6%）** | F1 **1.000** | F1 **0.729** |
| **CAUTION_FOR 层** | 【注意事项】+【儿童用药】两节 + `level` 两档 | 108,945（24.1%） | — （无金标准） | — （**暂不入图**） |

> **两件事必须一起说。**
> ①**只报第一层会得出"基线已经做完了"的错误结论**——第一层满分、边级只有 0.624，
> 瓶颈完全在"抽出来的东西怎么进图"。
> ②**省的是 token 不是时间**：墙钟 115.7 → 115.0 秒，延迟由调用固有开销与输出长度主导。
> 把"输入降到 3.6%"说成"快 40 倍"就是假话。

`generic_only` 把 37 条假阳性边砍到 19 条，而**未过滤时两个方法第二层逐项同数**
（77 对 / `TP31 FP46 FN18`）——所以 0.624→0.729 **只能归因于这一个变量**。
标记本身**条目级 P 1.000 / R 0.889 / F1 0.941，一条都没误标**。

## 入库（L4 闭环）

`scripts/import_insert_edges.py` 把边写进 Neo4j：**说明书文本 → 禁忌原子 → 图谱节点 → 可查询的边**。

```powershell
$PY = ".\backend\.venv\Scripts\python.exe"

& $PY scripts\extract_baseline.py              # 基线抽取（存档提示词/参数/原始输出，已冻结）
& $PY scripts\score_extraction.py              # 第一层：原子级 P/R/F1
& $PY scripts\align_nodes.py --eval --llm      # 对齐器：规则 vs 规则+⑤ LLM（结果带缓存）
& $PY scripts\score_alignment.py --llm         # 第二层：端到端边级 P/R/F1

& $PY scripts\extract_sections.py --dry-run    # 消融层级 2：先看规模与成本，不调 LLM
& $PY scripts\extract_sections.py              # 按小节送 + JSON 约束 + generic_only
& $PY scripts\score_extraction.py --method section-json --items-dir data\extracted\section-json\items
& $PY scripts\score_alignment.py  --method section-json --items-dir data\extracted\section-json\items --llm

& $PY scripts\import_insert_edges.py --dry-run # 入库：先看要写什么（不写库）
& $PY scripts\import_insert_edges.py           # 写入 171 条 CONTRAINDICATED_FOR（MERGE，可重跑）
& $PY scripts\import_insert_edges.py --reset   # 回滚（只删 source=insert 的东西）

& $PY scripts\insert_io.py --selftest          # 合成用例回归（19 项，不依赖网络与已采数据）

& $PY scripts\verify_l4.py                     # ★ 一键自检：重算 + 断言 19 条数字 + 核对产物零漂移
& $PY scripts\verify_l4.py --with-graph        #   再跑一遍入库与验收查询（需 Neo4j）
```

**⚠️ 图里这批边约 33% 是假阳性**（P 0.672 / R 0.796 / F1 0.729）。它们带着
`align_level` 与 `confidence`（high 58 / medium 113）供下游按可信度过滤——
**不要把它们当 ground truth**。这句话同时写在入库脚本的输出里。

## 验收：§13.5 的两条标准都测完了

| 标准 | 规模 | 结果 |
|---|---|---|
| **完整性**（人工通读比对） | 10–20 篇 | 11 篇分层抽样金标准 → 第一层 **1.000**，边级 **0.729** |
| **忠实度**（回原文查证） | 抽 100 条 | 消融层级 2 **100.0%** ／ 朴素基线 **94.0%**（覆盖 35 个药）；CAUTION_FOR 层【注意事项】**98.9%**、【儿童用药】 **100%** |

§13.5 说忠实度是**查证题**（"原文有没有这么写"），所以它能自动查——把抽出的原子拿回
**模型当时看到的原文**里做子串匹配即可。基线那 6 条不通过的全是**改写/拼接**，最典型的一条是
`Waldenstrtim's`（原文拼错）被模型**改对**成了 `Waldenström's`：**模型比原文更"正确"，但不忠实。**

**一条命令自检**（`scripts/verify_l4.py`）：把每个已发布的数字**重算一遍并断言没动**，
再核对 `data/extracted/` 零漂移。数字改过好几次，而"文档里的数和产物里的数不一致"
是最难发现的一类错——所以把它机械化。**改文档就必须改脚本里的 `EXPECTED`，反之亦然。**

```powershell
& $PY scripts\check_fidelity.py                         # 抽 100 条
& $PY scripts\verify_l4.py                              # 11 步 + 19 条断言 + 零漂移
```

> ⚠️ **`--scope` 必须等于"模型当时看到的输入"**：按小节送的那层只看到【禁忌】一节，
> 基线看到的是整篇。拿整篇查小节产物会放过"从别的节抄来"的条目，反过来会冤枉基线。

**五条纪律**（都是为了不让数字好看而失真）：

1. **疾病侧不写同义词表**。`甲状腺机能亢进` ↔ `甲状腺功能亢进` 这类交给 ⑤ LLM，
   并把"规则能做到多少、剩下多少必须靠语义"**当作结论报出来**——写进规则就等于
   在冻结的评测集上调参。**人群**侧例外：那是我们自己定的**受控词表**，
   `妊娠期妇女` 与 `孕妇` 本就是同一个槽位（见 `align_nodes.POPULATION_ALIASES`），
   而且这个词表是**封闭**的——⑤ 撑不大它（决策 #35）。
2. **两个数字并排报**，不做"什么时候听 ⑤"的裁决规则——那种规则必然是我看着评测集写的。
   ⑤ 会**弄坏**东西（实测净 +3，但把 2 条本来对的改错），这个也要报。
3. **不改冻结的评测口径**。已定位两个需要**决策**的问题：两层切分口径不一致
   （节点金标准对 `或`/`和` 也切，5 条边结构性拿不到）、`用药` 槽位与 §5 定义冲突（2 条边）。
4. **没有金标准的节不抽**。【注意事项】+【儿童用药】→ `CAUTION_FOR` 等标注跟上再做——
   **不可评测的产出做出来只是"看起来做了"**。
5. **对齐目标必须等于图里的节点**。实测图里的病名比 `entities_dict` 多 133 个，
   拿后者当词表会在节点已存在时"新建"重复节点——**入库必然造脏数据**（决策 #35）。

数字、归因与六个结构性缺口的完整证据，见
[`docs/数据源实测报告.md`](docs/数据源实测报告.md) §10–§12。

## 目录结构

```
pharmkg/
├── .env.example          环境变量模板（提交）
├── .env                  真实密码（不提交）
├── .gitattributes        换行符统一为 LF
├── .gitignore
├── docker-compose.yml    Neo4j + PostgreSQL
├── data/
│   ├── raw/              下载的原始数据集（不提交，由脚本重建）
│   ├── collected/        自己采集的说明书文本（提交）
│   └── processed/        药名映射表、抽取结果（提交）
├── backend/              FastAPI 后端
│   ├── requirements.txt
│   └── app/
│       ├── config.py     集中读取环境变量
│       └── main.py       应用入口 + 健康检查
├── scripts/              离线数据管线脚本（L1–L4）
│   ├── download_data.py            原始数据下载 + 完整性校验
│   ├── inspect_raw.py              原始数据体检（格式 / 规模 / 质量）
│   ├── opencmkg_io.py              OpenCMKG 读取层（两个解析坑 + 三处上游 bug 的修复）
│   ├── import_opencmkg.py          L1：骨架导入 Neo4j
│   ├── build_drug_name_mapping.py  第 0 步：中英药名映射（LLM 翻译 + 五级匹配）
│   ├── ddinter_io.py               DDInter 读取层（无序药对去重 + 等级冲突）
│   ├── import_ddi.py               L2：药物相互作用导入 Neo4j
│   ├── select_high_risk_drugs.py   L3①：按 Major 级频次选样
│   ├── insert_io.py                说明书读取层（块级换行 + 文档顺序全节 + 候选解析）
│   ├── collect_inserts.py          L3②：说明书采集（限速 / 登录 / 候选落地）
│   ├── reparse_inserts.py          L3②：从 raw/ 离线重解析（改规则不必重采）
│   ├── build_eval_set.py           L4：分层抽样选评测集（11 药，标好即冻结）
│   ├── extract_baseline.py         L4：朴素基线抽取（整篇输入 + 单次调用）
│   ├── score_extraction.py         L4 第一层：原子级 P/R/F1
│   ├── build_node_gold.py          L4：节点金标准（--lookup 查词表 / --verify 校验）
│   ├── align_nodes.py              L4：原子 → 图谱节点对齐器（规则四级 + ⑤ LLM 兜底）
│   ├── extract_sections.py         L4 消融层级 2：按小节送 + JSON 约束 + generic_only
│   ├── l4_pipeline.py              L4：条目 → 原子 → 节点（**评分与入库共用这一段**）
│   ├── score_alignment.py          L4 第二层：端到端 (药,节点) 边级 P/R/F1
│   ├── check_fidelity.py           L4 忠实度：抽 100 条回原文查证（§13.5）
│   ├── verify_l4.py                ★ L4 一键自检：重算 + 断言已发布数字 + 核对产物零漂移
│   └── import_insert_edges.py      L4 入库：CONTRAINDICATED_FOR / CAUTION_FOR → Neo4j
└── docs/                 需求与技术方案基线、数据源实测报告、药名映射实验报告
```

## 数据来源

原始数据**不入库**，靠下面的地址重新下载（这也是仓库保持轻量的原因）。

| 数据 | 用途 | 地址 | 许可证 |
|---|---|---|---|
| OpenCMKG | 图谱骨架：疾病 / 药物 / 症状 / 食物 / 科室 / 生产商 | <https://github.com/RuiqingDing/OpenCMKG> | 仅学术研究 |
| DDInter | 药物相互作用 + 严重程度（**无机制与处理建议**） | <https://ddinter.scbdd.com/download/> | CC BY-NC-SA 4.0 |
| 丁香园用药助手 | 说明书全文：禁忌、注意事项、儿童用药等 | <https://drugs.dxy.cn/> | 公开可访问的聚合平台；⚠️ **二手转录，非监管原件** |

> ⚠️ **许可证合规**：DDInter 为非商业（NC）+ 相同方式共享（SA）。本项目属学术非商业用途，但若公开分发衍生图谱须采用同协议。**DrugBank 禁止再分发，不入库。**

## 开发约定

分支流程（分支 → 自测 → 手测 → 合并推送）、链路清单与验收标准，见
[`docs/需求与技术方案基线.md`](docs/需求与技术方案基线.md) 第 13、14 节。

## 进度

- [x] 需求与技术方案基线
- [x] 项目骨架（数据库容器化、后端可启动、健康检查通过）
- [x] 原始数据获取与校验（OpenCMKG + DDInter，33.5 MB，SHA-256 校验通过）
- [x] **数据源实测**（格式、规模、质量全部实测；纠正基线中 9 处推测性描述）
- [x] **L1 药品骨架导入**（62,479 节点 / 354,752 边，MERGE 幂等已验证；查出并修复上游嵌套 list bug）
- [x] **第 0 步药名映射实验**（覆盖率 27.3% / 26.7%；查出编辑距离在医药场景不可靠）
- [x] **L2 药物相互作用导入**（150,522 条 `INTERACTS_WITH` 边，MERGE 幂等已验证）
- [x] **L3 说明书采集**（选样 40 种高危药 → 采到 **36 份**可追溯说明书；【禁忌】【注意事项】全完整）
- [x] **L3 产物保真修复**（**四处缺陷**：块级换行丢失 / 白名单丢 56 个小节 / 清单被续采覆写 / 表格被拍成每格一行；正文 **644 → 9,018 行**、小节 **417 → 473**，全程零重采）
- [x] **L4 抽取基线冻结**（36/36，451,484 输入 tokens / 115.7 秒 / 0 失败；**原子级 P = R = F1 = 1.000**——并用**错位对照**证明这个满分不是恒真的）
- [x] **L4 节点词表 + 对齐器 + 端到端两层评分**（决策 #33）。对齐 **规则 78.8% → 规则+⑤ LLM 86.5%**；但真正该看的是**边级**：原子级满分之下，`(药,节点)` **边级 F1 只有 0.492（+LLM 0.624）** —— **瓶颈完全在对齐，不在抽取**
- [x] **L4 消融层级 2：按小节送 + JSON 约束 + `generic_only`**（决策 #34）。第一层仍 **1.000**，**输入 token 451,484 → 16,385（3.6%）**；边级 **F1 0.624 → 0.729**，未过滤时两方法**逐项同数** ⇒ 涨幅只来自这一个变量。标记本身条目级 **P 1.000 / R 0.889**
- [x] **L4 入库闭环**（决策 #35）。171 条 `CONTRAINDICATED_FOR` 入图（+65 节点），**验收查询全过且都是 high 置信**，**幂等与回滚实测过**。过程中修掉三个**正确性**缺陷：对齐词表 ≠ 图节点集（差 133 个，会造重复节点）、⑤ 绕过受控人群词表、9 个药名命中不了（**剂型分裂**）
- [x] **L4 验收两条都测完**（决策 #36）。**完整性**：11 篇人工金标准 → 第一层 1.000 / 边级 0.729；**忠实度**：抽 100 条回原文查证，消融层级 2 **100.0%** vs 基线 **94.0%**。**修正 §13.2 的 L4 跑通标志**——原写的「华法林 → 孕妇禁用」字面无法满足（已核对 `index.csv` 40 行，**华法林不在 L3 选样名单里**），改用样本内等价查询
- [x] **L4 一键自检** `scripts/verify_l4.py`：重算每个已发布数字 + 断言 16 条 + 核对 `data/extracted/` 零漂移。全离线（⑤ 走缓存）
- [ ] L4 收尾 ← **卡在这里**。剩下的**全部**是依赖你的三件，每一件都带实测证据：
  - **① `CAUTION_FOR` 的槽位扩不扩**（→ `Population/Disease`）？【注意事项】只有 **3.4%** 的行涉及人群，**52.9%** 是疾病/状况，现在的槽位**承载不了它的主体**；"没有金标准就不可评测"那个旧理由**已被本项目自己推翻**（忠实度可自动查）。**拍板前不抽**——槽位一变要重做。详见[§14](docs/数据源实测报告.md)
  - **② 原子切分要不要改**？`、，；` 会切开括号内举例、理由从句、后果句，是剩下假阳性边的**大头**（改它＝改冻结口径，要重标金标准并重测第一层）
  - **③ `用药` 槽位 vs §5 定义**（`奥希替尼→圣约翰草` 等 2 条边）：改 §5 还是改金标准
  - 另有 ④ 两层切分口径不一致 → **5 条边结构性不可达**（第二层召回天花板 46/51）
- [ ] L5 问答链路 ← 技术风险最高
- [ ] L6 用药安全校验
- [ ] L7 会话管理
- [ ] L8 / L9 评测
