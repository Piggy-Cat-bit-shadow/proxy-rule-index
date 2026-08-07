# Rule Index — 代理规则搜索索引

一个纯静态、可自动更新的代理规则搜索数据库。

用户未来在网页上搜索 `Telegram` / `OpenAI` / `YouTube` / `Netflix` / `GitHub` / `OneDrive` / `WeChat` / `Bilibili` 等 APP 后，立即看到：哪些高质量规则源有针对该 APP 的规则、支持什么客户端、规则数量、域名还是 IP、最后更新时间、下载 URL。

**数据库只保存规则链接索引，不复制整个规则世界。**

> 本项目只收录公开规则仓库和公开规则 URL。禁止导入任何个人敏感配置（节点地址、UUID、密码、订阅 URL、Token 等）。

---

## 架构

```
人工维护 YAML (sources/aliases/overrides)
        │
        ▼
GitHub Actions (每日)
        │
        ├─ 扫描 GitHub 规则仓库
        ├─ 解析规则 / 统计数量 / 类型
        ├─ 真实可用性检测 (HTTP)
        └─ 生成 JSON
        ▼
dist/*.json  →  GitHub Pages 纯静态发布
```

## 目录结构

```
rule-index/
├── data/
│   ├── sources.yaml        # 规则源定义 (人工精选)
│   ├── aliases.yaml        # 服务别名 + canonical service catalog
│   └── overrides.yaml      # 人工覆盖修正
├── scripts/
│   ├── config.py           # 配置加载
│   ├── github_api.py       # GitHub API 客户端
│   ├── discover.py         # 规则文件发现
│   ├── parser.py           # 规则解析 / 统计
│   ├── checker.py          # 可用性检测
│   ├── index.py            # 索引生成主流程
│   └── validate.py         # 构建校验
├── dist/                   # 生成产物 (JSON)
├── .github/workflows/
│   └── update-index.yml    # 每日自动构建 + Pages 部署
└── build.sh                # 本地构建入口
```

## 生成产物

| 文件 | 说明 |
|------|------|
| `dist/catalog.json` | 所有 `Service × Provider × Client` 规则记录 (前端搜索主体) |
| `dist/services.json` | canonical APP 信息 (id/name/aliases/category/search_text) |
| `dist/sources.json` | 规则源信息 (作者/仓库/Tier/客户端/License/状态) |
| `dist/stats.json` | 简单统计 (总服务数/总规则文件/各来源数量/构建时间) |

## 规则源 (Tier)

| Tier | 来源 | 用途 |
|------|------|------|
| A | blackmatrix7 / Repcz / SukkaW / MetaCubeX | APP 级规则核心 |
| B | QuixoticHeart | 多客户端适配补充 |
| C | KeLee / ddgksf2013 / FuGfConfig / Cats-Team / Hackl0us / iab0x00 | 专项 / 特殊规则 |

详细配置见 [data/sources.yaml](data/sources.yaml)。

## 客户端

内部统一 ID：`surge` / `loon` / `shadowrocket` / `egern` / `mihomo` / `sing-box`。Clash / Clash.Meta / Meta 统一归入 `mihomo`。

## 搜索排序 (硬规则)

```
专用规则  >  分类规则  >  综合规则  >  Global 兜底规则
同级内部: 可用性 > Native > 来源等级 A>B>C > 新鲜度 > 规则数量
```

## 本地构建

```bash
python3 -m pip install -r requirements.txt
python3 scripts/index.py      # 生成 dist/*.json
python3 scripts/validate.py   # 校验
```

或：

```bash
./build.sh
```

> 首次全量扫描约 5000 个 URL，需要几分钟。GitHub Actions 每日自动执行。

## 关键设计原则

1. **数据库是索引，不是规则仓库** — 只保存规则名称 / 来源 / 客户端 / URL / 数量 / 类型 / 时间，不保存几十万条规则内容。
2. **静态优先** — 不使用 MySQL / PostgreSQL / MongoDB / Redis / Elasticsearch，也不使用 SQLite。输出纯 JSON，可部署到 GitHub Pages / Cloudflare Pages。
3. **自动扫描为主，人工 override 为辅** — 自动识别服务，遇特殊情况在 `overrides.yaml` 修正。
4. **canonical Service** — APP 名称大小写变化、文件名变化不会生成新的 APP。`ChatGPT` → `OpenAI`，`Twitter` → `X`，`TG` → `Telegram`。
5. **可用性检测** — 每个 URL 真实 GET，验证非空、非 HTML 错误页、可解析、`rule_count > 0`。一次失败不删除，保留旧记录标记 `warning`。
6. **特殊来源 (KeLee)** — `rule.kelee.one` 使用特殊访问模式，不因普通 HTTP 探测异常而判定失效。
7. **不制造无意义 commit** — 内容无变化时不提交。

## 许可证

本项目仅构建公开规则链接索引，不重新声明任何规则仓库的版权。各来源的 License 见 [data/sources.yaml](data/sources.yaml)，未知则标记 `unknown`。
