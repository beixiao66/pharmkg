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
| 大模型 | DeepSeek / 阿里云百炼（均为 OpenAI 兼容接口） |
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

## 数据准备

原始数据体积约 33.5 MB，**不入库**（体积大 + 部分有再分发限制）。一条命令拉取并校验：

```powershell
python scripts/download_data.py
```

脚本特点：断点友好（已存在的文件自动跳过）、下载后校验 SHA-256 与体积、
结果写入 `data/raw/MANIFEST.json`。

```powershell
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
│   ├── download_data.py  原始数据下载 + 完整性校验
│   └── inspect_raw.py    原始数据体检（格式 / 规模 / 质量）
└── docs/                 需求与技术方案基线、数据源实测报告
```

## 数据来源

原始数据**不入库**，靠下面的地址重新下载（这也是仓库保持轻量的原因）。

| 数据 | 用途 | 地址 | 许可证 |
|---|---|---|---|
| OpenCMKG | 图谱骨架：疾病 / 药物 / 症状 / 食物 / 科室 / 生产商 | <https://github.com/RuiqingDing/OpenCMKG> | 仅学术研究 |
| DDInter | 药物相互作用 + 严重程度（**无机制与处理建议**） | <https://ddinter.scbdd.com/download/> | CC BY-NC-SA 4.0 |
| NMPA 说明书 | 禁忌、特殊人群 | <https://www.nmpa.gov.cn/> | 政府公开信息 |

> ⚠️ **许可证合规**：DDInter 为非商业（NC）+ 相同方式共享（SA）。本项目属学术非商业用途，但若公开分发衍生图谱须采用同协议。**DrugBank 禁止再分发，不入库。**

## 开发约定

分支流程（分支 → 自测 → 手测 → 合并推送）、链路清单与验收标准，见
[`docs/需求与技术方案基线.md`](docs/需求与技术方案基线.md) 第 13、14 节。

## 进度

- [x] 需求与技术方案基线
- [x] 项目骨架（数据库容器化、后端可启动、健康检查通过）
- [x] 原始数据获取与校验（OpenCMKG + DDInter，33.5 MB，SHA-256 校验通过）
- [x] **数据源实测**（格式、规模、质量全部实测；纠正基线中 9 处推测性描述）
- [ ] L1 药品骨架导入
- [ ] L2 药物相互作用导入（含药名映射覆盖率实测）
- [ ] L3 说明书采集
- [ ] L4 说明书知识抽取
- [ ] L5 问答链路
- [ ] L6 用药安全校验
- [ ] L7 会话管理
- [ ] L8 / L9 评测
