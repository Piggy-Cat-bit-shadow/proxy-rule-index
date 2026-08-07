# Rule Index — 代理规则搜索索引

一个纯静态、可自动更新、**增量构建**的代理规则搜索数据库。

在网页上搜索 `Telegram` / `OpenAI` / `YouTube` / `Netflix` / `GitHub` / `OneDrive` / `WeChat` / `Bilibili` 等 APP 后，立即看到：哪些高质量规则源有针对该 APP 的规则、支持什么客户端、规则数量、域名还是 IP、可用状态、下载 URL。

**数据库只保存规则链接索引（元数据），不复制整个规则世界。**

> 本项目只收录公开规则仓库和公开规则 URL。禁止导入任何个人敏感配置（节点地址、UUID、密码、订阅 URL、Token 等）。

---

## 架构与增量构建

```
人工维护 YAML (sources / aliases / overrides)
        │
        ▼
GitHub Actions (每日, 增量)
        │
        ├─ Resolve: 读取上一版 Snapshot (generated/), 比较 Source SHA + 配置指纹
        ├─ Source Scan: 只扫描发生变化的 Source (Matrix 横向)
        │    └─ Blob SHA 相同 → REUSE 元数据; 变化 → GET + Parse + 可用性检测
        ├─ Aggregate: 生成按客户端拆分的静态 JSON
        ├─ Data Compare: data_hash 无变化 → 结束; 有变化 → Site Build
        └─ Deploy: GitHub Pages (未来可加 VPS, 同一 Artifact)
```

### 增量三级

1. **Source 级** — 比较目标分支 commit SHA 与上一版。相同 → REUSE 整个 Source Snapshot。
2. **文件级 (Blob SHA)** — Source 更新时，比较每个文件的 git blob SHA。相同 → REUSE 元数据；变化才下载+解析。
3. **配置指纹** — source 配置 (`repo`/`branch`/`templates`/`validation`) 变化才触发该 Source 重扫。`aliases.yaml` 分类变化只重新 Aggregate，不重新下载规则。

**目标：数据没有变化时几乎什么都不做；只有哪里变化，就只处理哪里。**

## 目录结构

```
rule-index/
├── data/
│   ├── sources.yaml        # 规则源定义 (人工精选, 含 url 模板/预算/验证模式)
│   ├── aliases.yaml        # canonical 服务目录 + 别名 + provider 文件映射
│   └── overrides.yaml      # 人工覆盖修正 (排除/粒度/客户端/格式)
├── scripts/
│   ├── config.py           # 配置加载
│   ├── github_api.py       # GitHub API 客户端 (token 优先, 最小请求)
│   ├── discover.py         # 规则文件发现 (含 KeLee catalog 模式)
│   ├── urlresolver.py      # URL 模板 / path_override / url_override
│   ├── snapshot.py         # Metadata Snapshot (可从 generated/ 恢复)
│   ├── incremental.py      # 每 Source 增量扫描 (reuse/refresh/removed/failed)
│   ├── parser.py           # 规则解析 / 统计 (安全, 文本处理)
│   ├── checker.py          # 可用性检测 (standard / special)
│   ├── aggregate.py        # 确定性聚合 → 按客户端 shard + manifest + health
│   ├── build.py            # 主编排 (auto / full-refresh / source / site-only)
│   └── validate.py         # 构建校验 (发布前强制)
├── tests/                  # 本地单元测试 (无网络)
├── generated/              # 每 Source 独立 shard (增量复用的事实来源, 入库)
├── dist/                   # 生产产物 (入库, 用于 Pages 部署)
└── .github/workflows/
    └── update-index.yml    # 每日增量扫描 + 无变化跳过 + Pages 部署
```

## 生产产物 (dist/)

```
dist/
├── index.html              # 前端 (客户端优先, 懒加载)
└── data/
    ├── manifest.json       # schemaVersion + 客户端 → catalog shard + source URL 解析器
    ├── build-info.json     # dataVersion (data_hash) + builtAt
    ├── services.json       # canonical 服务 + search_text + aliases
    ├── sources.json        # 来源元数据 + URL resolver 信息 (不含每记录 URL)
    ├── stats.json          # 廉价统计
    ├── health.json         # record → status/timestamps (与稳定数据分离)
    └── catalog/            # 按客户端拆分, 前端懒加载
        ├── surge.json
        ├── loon.json
        └── ...
```

**前端首屏只加载 `manifest + services + stats + health` + 当前客户端 shard。** 用户切换客户端才懒加载对应 shard。不一次下载全部客户端数据。

**URL 不逐条保存** — 记录只存 `slug` + `path`，前端通过 `manifest.source_urls[source]` 的模板重建完整 URL。上游域名/分支变化只改一处。

## 客户端

内部统一 ID：`surge` / `loon` / `shadowrocket` / `egern` / `mihomo` / `sing-box`。Clash / Clash.Meta / Meta 统一归入 `mihomo`。

## 搜索排序 (硬规则)

```
专用 APP 规则  >  分类规则  >  综合规则  >  Global 兜底规则
同级内部: 可用性 > Native > 来源等级 A>B>C > 新鲜度 > 规则数量
```

排序键在构建时预计算成稳定 tuple，前端只按 tuple 排序，不做复杂质量评分。

## KeLee / rule.kelee.one (特殊验证)

- `validation_mode: special` — 不做普通 HTTP GET + Parse
- `count_available: false` — 不显示 `0 条`，前端隐藏规则数量
- 不因无法拉取正文标记 unavailable，不增加 failure_count
- 不参与规则数量排行，`rule_count = null` 不降低排序
- 正常参与 Loon 等客户端搜索，显示 🟣 特殊访问

## 本地构建

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest tests/          # 单元测试 (无网络)
RI_MODE=auto python3 scripts/build.py   # 增量构建 (首次全量, 之后增量)
python3 scripts/validate.py       # 发布前校验
```

### RI_MODE 选择

| 模式 | 用途 |
|------|------|
| `auto` | 默认增量 (推荐) |
| `full-refresh` | 强制全量重扫 |
| `source=<id>` | 只刷新单个 Source |
| `site-only` | 复用 generated/ 只重建站点 |
| `audit-only` | 扫描+比较, 不 commit 不 deploy (workflow_dispatch) |

## 关键设计原则

1. **增量优先** — Source SHA / Blob SHA / 配置指纹三级复用，不重复下载未变化规则。
2. **静态优先** — 无数据库 / 后端 API / 常驻服务。GitHub Pages 与未来 VPS 共用同一 Artifact。
3. **确定性输出** — 稳定排序 + sort_keys，相同输入 → 相同 data_hash → 无假变化。
4. **稳定数据与健康状态分离** — catalog shard 无时间戳；`health.json` 单独存 status/timestamps。
5. **安全** — YAML safe parser、无 eval、不可信文本只做文本处理、下载限流/限大小。
6. **失败隔离** — 某 Source 网络失败保留上一版 Snapshot + 标记 warning，不污染其他 Source。
7. **权限最小化** — Scan job 只 `contents: read`；commit 与 deploy 权限分离。

## 许可证

本项目仅构建公开规则链接索引，不重新声明任何规则仓库的版权。各来源的 License 见 [data/sources.yaml](data/sources.yaml)，未知则标记 `unknown`。
