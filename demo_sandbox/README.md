# 启明商业银行信创数仓治理演示沙箱 (`demo_sandbox/`)

一套专为 **Data.O.G** 设计的银行/金融信创数仓一体化演示沙箱（虚构银行主体：**启明商业银行**），无需甲方生产集群即可在本地单机 15 秒极速复现「**核心 MySQL → 历史星环 Inceptor → 新建华为 GaussDB(DWS)**」全链路真实数仓治理体验。

## 包含资产

| 文件 | 用途 |
| :--- | :--- |
| `docker-compose.yml` | 本地三引擎 Docker 容器编排：PostgreSQL 16 (仿真华为 DWS)、Spark HiveThriftServer2 (仿真星环 Inceptor)、MySQL 8.0 |
| `banking_catalog.py` | 原创 164 条金融数据标准（8 大主题域、10 种物理格式），导出 46 列标准字典 Excel 与批量绑标模板 |
| `live_seeder.py` | 固定随机种子 (`20261119`) 生成 ~30,000+ 行银行客户/账户/借据/流水/押品/授信样本，并精准注入 5 大金融业务暗雷 |
| `workspace_builder.py` | 基于 Layer 3 零密钥导出生成 `qiming_live_sandbox.dataog.db` 与 156 表级压测工作区 `qiming_enterprise_150tables.dataog.db` |
| `sandbox_ctl.py` / `sandbox.sh` | 一键运维控制台（`up` / `reset` / `status` / `generate-assets` / `bootstrap-demo-home` / `down`） |
| `DEMO_PLAYBOOK.md` | **高层 15–20 分钟沉浸式演示剧本**（含主流方案对比、领导追问应答话术、分镜点击路线与 5 个可复制 AI 探查提问） |

## 快速上手

```bash
# 1. 一键启动三引擎容器 + 灌入数据 + 采集生成工作区与标准 Excel
./demo_sandbox/sandbox.sh up

# （可选）若直连 Docker Hub 拉镜像较慢，可加国内镜像加速前缀：
./demo_sandbox/sandbox.sh up --registry-prefix docker.m.daocloud.io/

# 2. 查看三引擎状态与五大业务暗雷精确核验数字
./demo_sandbox/sandbox.sh status

# 3. 演练改表后，5~15 秒不重启容器极速复原现场
./demo_sandbox/sandbox.sh reset

# 4. （可选）免 Docker 仅离线生成 46 列标准字典 Excel 与两份 .dataog.db
./demo_sandbox/sandbox.sh generate-assets

# 5. （可选）向隔离演示目录一键预装双工作区与三引擎本机凭据（不污染个人真实数据目录）
./demo_sandbox/sandbox.sh bootstrap-demo-home
DATAOG_HOME="./demo_sandbox/.demo_home" python3 app.py

# 6. 关闭容器
./demo_sandbox/sandbox.sh down
```

## 三引擎连接速查

| 演练角色 | Data.O.G 连接类型 | 主机:端口 | 库名 / 默认 Schema | 账号 / 密码 | 落标格式档 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **华为 GaussDB(DWS)** | `DWS` (`NATIVE`) | `127.0.0.1:15432` | `qiming_dws` / `dwd_bank` | `dws_admin` / `DemoDws@2026` | `type_dws` |
| **星环 Inceptor** | `INCEPTOR` (`NATIVE`) | `127.0.0.1:11000` | `bank_inceptor` / `bank_inceptor` | `hive` / 无密码 (`NOSASL`) | `type_argodb` |
| **核心交易 MySQL** | `MYSQL` (`NATIVE`) | `127.0.0.1:13306` | `core_bank_ods` | `core_reader` / `DemoMysql@2026` | `type_mysql` |

> 生成的 `.xlsx` 与 `.dataog.db` 均输出至 `demo_sandbox/output/`（已被 `.gitignore` 忽略），且 `build_exe.py` 显式排除了 `demo_sandbox`，不会打入客户版 `DataOG.exe`。
