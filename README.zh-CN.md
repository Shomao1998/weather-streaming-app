# Weather Streaming Pipeline · 天气流式采集与监控管道

*[English](README.md) · 简体中文*

一条跑在 Azure 上的无服务器采集与监控管道：每 30 秒采集一次遥测数据，经 Event Hubs 流入数据湖，
加工成可查询的表，最后呈现在一个公开看板上——**并且对管道本身和流经它的数据都做了告警**。

管道之上是一个**卡片式建议功能**：由天气条件触发，措辞取自经过审阅的模板，全程没有模型参与。
它能说出的每一句话都是一个人批准过的字符串，它做的每一个决策都能在单测里复现。

> **你正在看的是 v1.1。** 这一版改了什么、为什么这么改，见它的
> [release 说明](https://github.com/Shomao1998/weather-streaming-app/releases/tag/v1.1)；
> 完整的版本历史在
> [releases 页面](https://github.com/Shomao1998/weather-streaming-app/releases)。

| | |
| --- | --- |
| **技术栈** | Azure Functions（Python 3.12，Flex Consumption）· Event Hubs · ADLS Gen2 · Application Insights · Static Web Apps · Bicep |
| **本地运行** | `python scripts/serve_dashboard.py` —— 看板用仓库内的样本数据渲染，不需要 Azure 账号 |
| **线上部署** | 已暂停，见下 |

> **托管的部署目前是关闭的。** 同一订阅下的另一个项目——一个约 \$240/月的
> Azure AI Search Standard 实例——耗尽了额度，订阅转为只读。天气管道本身只占
> 这笔账单的约 2%。计费周期重置后会恢复，处置方案见
> [`docs/deployment.md`](docs/deployment.md)。
>
> 我宁可把这件事写出来，也不愿留一个点开是 404 的链接。真正去测这套东西的成本、
> 并且第一次测错了，反而是这个项目里比较有用的部分之一——最初 README 估的是
> 每月 \$12–14，实测是 \$44。

---

## 这个项目为什么存在

我在咨询行业参与过一家大型金融机构的本地数据上云项目，其中一个需求是上云后如何存储内网
syslog。最初提议以 Azure 存储服务承载日志，并用 Application Insights 监控传输异常，
以满足**日志零丢失这一刚性要求**，以及至少保留两年的合规要求。该方案最终未能推进：
日志体量至少 TB 级，存储费用（**即便全部置于 Archive 层**）叠加 Application Insights
的监控费用，远超项目可负担的预算。

我在该项目中另负责跨团队任务进度追踪及看板呈现。参考其他开源的优秀作品集，
本项目将这两项需求合并为一套天气流式采集与监控看板。

同一个项目上还有第三个需求，我离开时它仍处在初期规划阶段，没有落地。上云之后员工要在一个
不熟悉的系统里工作，规划中的方案是一个**卡片式 chatbot**，用检索回答他们的问题，
知识来源是随系统更新的内部知识库与 FAQ——也就是说，被检索的文档本身每周都在变。
本版本的建议卡片是这个想法确定性的那一半：由条件触发，措辞取自经过审阅的模板，
全程没有模型参与。检索是下一步，而它要接入的那道接缝在这一版里已经留好了。

**构建方式。** 需求、约束与验收标准由我定义，范围与成本决策由我做出；
实现部分通过与 AI 编码代理迭代产出。

**成本控制。** Application Insights 启用采样；默认日志级别设为 `Warning`
（`Information` 下 Azure SDK 会记录每一次 HTTP 请求）；各存储层均设有保留策略；
唯一高成本组件由配置开关控制启停。**技术上成立的设计，仍可能因运行成本而不可行。**

以天气数据替代日志，是因为两者共有三项特征：

- **上报频率高于内容变化频率。** 上游 API 每 10–15 分钟刷新一次，采集器每 30 秒运行一次，
  流中多为重复记录——等同于设备重复输出同一状态日志。
- **记录的重要性不均等。** 超过 38°C 的读数对应一行 `CRITICAL` 日志，需触发响应而非仅落盘。
- **数据缺失才是真正的故障。** 停止摄入的管道与正常管道在外部表现一致，
  除非有机制专门监测"无数据"状态。

## 架构

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/architecture-zh-dark.svg">
    <img src="docs/images/architecture-zh-light.svg" alt="管道架构：weatherapi.com 经两个定时函数写入 Event Hubs；archive_to_bronze 将流排空到 bronze 层；curate 每小时加工出 silver Parquet 与 serving JSON；serving 经 HTTP API 供给 Static Web App 看板，silver 供给 Power BI；阈值突破进入 Application Insights 并触发 Azure Monitor 告警规则。下方 api_advice 读取看板读的同一份 serving 快照，把一张规则驱动的卡片返回看板。" width="560">
  </picture>
</p>

<sub>图源：<a href="scripts/render_architecture.py"><code>scripts/render_architecture.py</code></a>。产品图标为微软官方 Azure 架构图标，按其随附条款原样使用、未作变形；weatherapi.com 是第三方服务，因此用中性图形表示。</sub>

### 五个函数

| 函数 | 触发方式 | 职责 |
| --- | --- | --- |
| `ingest_current` | 定时，30 秒 | 采集当前天气和空气质量；判定阈值 |
| `ingest_forecast` | 定时，30 分钟 | 一次请求同时取回每日预报和活跃告警 |
| `archive_to_bronze` | Event Hub | 把流排空到分区化的原始 JSONL |
| `curate` | 定时，每小时 | bronze → silver Parquet 和服务层文档 |
| `health`、`api_*` | HTTP | 存活探针和看板的只读数据接口 |

### 存储布局

```
bronze/  current/date=2026-08-01/hour=14/20260801T143005-a1b2c3d4.jsonl   仅追加，从不改写
silver/  current/date=2026-08-01/current-20260801T150000.parquet          已去重，扁平列
serving/ latest.json · timeseries_24h.json · breaches_24h.json           小体积，预聚合
```

Hive 风格的分区（`date=`、`hour=`）在启用了分层命名空间的账号上就是真实目录，
所以 Power BI、Spark、Fabric、DuckDB 都能直接做分区裁剪，不需要额外配置。

## 设计决策

**在存储前置缓冲层，原始层只追加。** 采集写入 Event Hubs 而非直接写存储，bronze 层只追加、
从不改写。两者均源自零丢失要求：下游故障时数据先进缓冲区而非丢弃，已落地的记录不被覆写。

**按数据实际变化速度拆分采集。** 原来三个接口都是每 30 秒调一次。预报和天气告警一天才变几次，
用观测频率去轮询它们，等于把大约 90% 的 API 配额花在完全相同的字节上。
当前天气留在 30 秒的定时器上；预报和告警挪到 30 分钟，并且合并成一次请求。

**用确定性记录 ID 替代有状态去重。** 每条记录的 ID 是 `(位置, 上游观测时刻)` 的哈希。
轮询快于源刷新会产生完全相同的 ID，所以加工步骤用一个字典就能折叠重复——
采集器保持无状态，不需要状态存储，不需要水位表，也不要求流做到精确一次投递。
**这是零丢失要求下的取舍：丢失不可接受，重复只需在加工阶段折叠。**

**观测时间和摄入时间是两个独立字段。** 它们会分叉——正常情况下差一个轮询间隔，
故障恢复补数时差得多。把它们合成一列，事后就无法再推理迟到数据——
而在零丢失要求下，故障后重放补数是常态而非例外。

**用消费函数替代 Event Hubs Capture。** Capture 是把流落地到存储的托管方案，
但它按吞吐单元每小时计费，而且写的是 Avro。一个约 40 行的 Event Hub 触发函数，
在这个量级下成本几乎为零，写出的 JSONL 不用任何工具就能读——而且这段代码本身就是作品的一部分。
成本理由与当初否决原提案的一致：按量计费的托管服务在规模上放大得最快。

**数据湖永远不公开可读。** 直接从 Blob 存储给看板供数意味着要在账号级别打开匿名访问，
那样连原始的 bronze 数据也一起暴露了。所以改由 Function App 提供三个匿名只读接口，
带 30 秒的进程内缓存——这样一个开着的浏览器标签页不会变成"每次轮询一次存储事务乘以访客数"。

**用 Flex Consumption，不用经典的 Consumption 计划。** 这不是偏好：在 Visual Studio 订阅上，
`Y1` 和所有 App Service 层级都会在 preflight 阶段以 `SubscriptionIsOverQuotaForSku` 失败——
这些 SKU 消耗的 VM 配额是零。Flex Consumption 走的是另一套配额池。
它同时还有更快的冷启动，以及按实例内存而非 VM 数量的伸缩方式。

**用户分配的托管标识。** Flex Consumption 在首次启动时从 Blob 存储读取部署包，
而系统分配的标识授权不了这一步——因为它要等应用创建出来才存在。
先创建标识、先授予角色，就消除了这个顺序问题；副作用是配置里再也没有任何连接字符串或账号密钥。
Event Hubs、Storage 和 Key Vault 全部通过标识访问。

## 建议卡片

天气值得提醒时，看板显示一条简短、可执行的建议——「带伞」「补水」——并附上它依据的那个数值。

v1.1 是确定性的：规则加模板，**不用模型、不用检索**。系统能说出的每一句话都是固定字符串，
所以同样的天气永远产出同样的措辞，卡片可以在测试里被精确断言。
`providers.AdviceContentProvider` 就是 v1.2 接入检索式生成的接缝——
规则、频控策略和卡片协议都不需要改动。

| 触发条件 | 阈值 | 优先级 |
| --- | --- | --- |
| `EXTREME_HEAT` | `temp_c >= 35` | 1 |
| `HIGH_WIND` | `wind_kph >= 40` | 2 |
| `HIGH_UV` | `uv >= 8` | 3 |
| `RAIN_EXPECTED` | 未来一小时降水概率 `>= 80` | 4 |

**建议严格是次要功能**：它在天气渲染完成之后才发起请求，且任何失败——数据过期、卡片被频控、
内容生成异常、接口不可达——都收敛为「不显示」，而不是让页面变差。

去重是确定性的副产品：推荐 ID 是 `hash(位置 + 触发码 + 天气快照 ID + 规则版本)`，
所以「同一条观测下的同一条建议」本来就是同一张卡片。而风险等级上升时，
频控窗口和静音都会被突破——因为用户关掉了一条温和提醒，不该成为扣下更严重提醒的理由。

完整设计与 API 协议见 **[docs/advice.md](docs/advice.md)**。

### v1.2 —— 建议须有官方出处

v1.2 把模板换成了模型：模型拿到的是当前天气事实，加上从一小份官方安全指引
语料中检索到的段落，并且必须为每条建议标出它来自哪一段。

规则层没有动。是否出卡、属于哪种风险、严重度多少、何时抑制、何时过期，仍然全部
由上面那套确定性引擎决定。检索和生成只负责措辞——一旦它们出任何问题，卡片就由
v1.1 的模板来写。

| | |
| --- | --- |
| 语料 | 6 个登记来源（5 个启用，1 个刻意下架），23 个 chunk，美国联邦公共领域文件 |
| 摄取 | chunk id 由内容寻址——重跑逐字节复现同一份索引 |
| 检索 | BM25 + 向量混合，RRF K=60，危害类型/司法辖区/`enabled` 以结构化过滤器施加 |
| 校验 | 全部由确定性代码完成——citation 必须落在*本次*检索里，动作只能取自 19 个封闭编码，数字必须出现在天气事实或被引段落中 |
| 兜底 | 任何一条失败路径都回落到 v1.1 卡片 |
| 评测 | 53 个用例，随 CI 运行；0 条无法解析的 citation、0 次危害串档、0 次该兜底却没兜底 |

检索层是一个接口配两套实现：生产用 Azure AI Search，另一套本地实现跑同样的策略且
零成本——正是它让检索层能在 CI 里真跑起来，也正是它让这个功能在订阅被停用期间仍
然能继续开发。**Azure 那条路径代码写完了，能离线校验的部分（OData 过滤器、索引
schema、文档字段）都有测试断言，但它从未连过真实服务。**

完整设计、评测结果与局限见 **[docs/rag.md](docs/rag.md)**。

## 监控

三条告警规则，覆盖三种真正不同的失败模式：

| 告警 | 条件 | 为什么需要它 |
| --- | --- | --- |
| 无摄入 | 15 分钟内没有一次成功运行 | 停摆的管道从外部看是健康的 |
| 失败 | 15 分钟内失败调用超过 5 次 | 上游故障、密钥过期、部署损坏 |
| 阈值突破 | 最近 10 分钟出现 `critical` 读数 | 业务级告警——即一行 `CRITICAL` 日志 |

阈值突破以带自定义维度的结构化日志发出，Application Insights 收集它们，告警规则查询它们。
**日志即告警的传输通道，不需要引入额外服务。**

## 仓库结构

```
infra/main.bicep                  全部 Azure 资源，幂等，一条命令
src/functions/                    部署包 —— host.json 在其根目录
  function_app.py                 只做触发器注册
  weather/                        config · api · models · transform · monitoring
                                  clients · sinks · pipeline · serving
dashboard/                        三个文件，无框架，无外部请求
knowledge/                        来源登记表、原始文档、构建好的索引
evals/                            53 个检索与生成用例，作为 CI 门禁运行
scripts/                          知识摄取、索引构建、架构图渲染、样本数据、OIDC 配置
tests/                            326 个测试
docs/architecture.md              更深的权衡、成本、被否掉的备选方案
docs/advice.md                    建议规则、卡片协议（v1.1）
docs/rag.md                       知识库、检索、grounding、评测
docs/deployment.md                部署手册、首次部署清单、排障
powerbi/                          报表模板与连接说明
```

## 本地运行

下面这些都不需要 Azure 订阅。

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

看板可以直接跑在提交进仓库的样本数据上：

```bash
python scripts/serve_dashboard.py   # http://127.0.0.1:4280
```

要让函数跑在真实 API 上，复制配置模板、填入 [weatherapi.com](https://www.weatherapi.com/) 的密钥，
然后启动 host：

```bash
cp src/functions/local.settings.json.example src/functions/local.settings.json
# 填 WEATHER_API_KEY；EVENT_HUB_ENABLED 保持 false，数据会直接写进 Azurite
cd src/functions && func start
```

## 部署

```bash
az deployment group create \
  --resource-group rg-weather-streaming \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json
```

然后推送到 `main`，或手动运行 **Deploy** workflow。首次部署清单、Key Vault 密钥、
以及 CI 鉴权的配置方式见 [docs/deployment.md](docs/deployment.md)。

## 成本

我估的是每月 **12–14 美元**，实测约 **44 美元**——差了三倍。与其悄悄改掉，不如写下来。

| 资源 | 原估算 | 估错在哪 |
| --- | --- | --- |
| Event Hubs Basic | 约 $11 | 估对了。 |
| Function App（Flex Consumption） | 约 9 万次执行下 $1–2 | Flex **没有免费执行额度**。有免费额度的是 Consumption（Y1），我把那个假设直接搬了过来——可这个订阅的 VM 配额为 0，Y1 根本部署不了。 |
| 存储（ADLS Gen2 + 运行时） | < $1 | 大致正确。 |
| Log Analytics / App Insights | $0，在 5GB 免费额度内 | 实际**按摄入 GB 计费**，而一个 30 秒的定时器在 `Information` 级别下写入的量远超预期。把默认日志级别降到 `Warning`，既是降噪也是降本。 |
| Azure Monitor 告警规则 | 没算 | 每条规则每月约 $1，共三条。 |
| Static Web Apps | $0（Free 层） | 估对了。 |

上表是最初的估算；修正版要等订阅恢复、攒够一整周干净的账单数据后再出。这个教训可以外推：
让无服务器估算显得便宜的那些免费额度，是**绑定在特定 SKU 上的**——一旦平台约束逼你换 SKU，
它们会悄无声息地消失。

Event Hubs 是最大的单项开销，也是唯一严格可选的组件——sink 层是一个接口，
把 `EVENT_HUB_ENABLED` 设为 `false` 就让采集直接写入 bronze。

### 数据保留

**只增不减的存储，是日志平台受制于成本而非架构的主要原因。** 每一层的保留策略均写入
`infra/main.bicep`，而非留待运行时决定：

| 层 | 策略 |
| --- | --- |
| Event Hubs | 1 天——它是缓冲区不是存储，归档函数在持续排空它 |
| `bronze` | 30 天转 Cool → 90 天转 Archive → 730 天删除（对应两年的合规保留期） |
| `silver` | 90 天转 Cool；不归档、不删除，而且它随时可以从 bronze 重建 |
| `serving` | 始终 Hot——三个小文件，每小时重写、每次页面加载都会读 |
| Log Analytics | 30 天 |

**把 bronze 归档是安全的，恰恰因为下游不读它**：`curate` 永远只碰最近 24 小时的数据，
所以原始数据落地一天之内就已经是冷数据了。日后取回要等一次解冻——
对于「为了应付审计而不是为了查询」而保存的数据，这是正确的取舍。

## 局限

- Event Hubs Basic 只保留一天、只允许一个消费者组。这里够用，因为归档函数在持续排空它；
  但要再加一个独立消费者就需要 Standard 层。
- `curate` 每次运行都重新处理滚动的 24 小时，而不是记录水位。这个量级下比记账更便宜，
  放大 100 倍就不是了。
- silver 层是整天重写文件，而不是增量压实。
- Power BI 按自己的计划刷新，不是实时的；实时视图由网页看板负责。
  两者为什么分开，见 [powerbi/README.md](powerbi/README.md)。
