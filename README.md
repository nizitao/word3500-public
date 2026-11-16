<!-- SPDX-FileCopyrightText: 2026 word3500 contributors -->
<!-- SPDX-License-Identifier: GPL-3.0 -->

# word3500

> 零第三方依赖的高考英语词汇学习平台。Python 标准库起服务，SQLite 存数据，原生 JavaScript 渲染页面 —— 克隆下来直接运行，不需要 `pip install`，不需要 Node，不需要构建。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![License](https://img.shields.io/badge/license-GPL--3.0-blue)

## 这是什么

word3500 最初只是一个「高考英语 3500 词」的分页查询页，后来逐步长成了一套完整的背词系统：从词表浏览、搜索、多收藏夹，到答题测验、打卡日历、掌握率趋势、艾宾浩斯复习队列、徽章与排行榜，再到多人房间「一起背」、在线人数和管理员数据报表。

它刻意保持**简单可自持**：没有 Web 框架、没有 ORM、没有前端框架、没有数据库服务。所有数据都落在项目 `data/` 目录下的两个 SQLite 文件里，你可以把它跑在笔记本上，也可以用反向代理挂到公网。

## 功能一览

### 词库与学习

- **分页浏览** 内置基础词条（每页 50 条），支持音标、词性、中文释义
- **分册分单元** 浏览：人教版必修 3 册 + 选择性必修 4 册，共 7 册；未归入分册的词条统一进「未分册」桶，不会被丢弃（教材词表需自行导入，见「数据说明」）
- **多收藏夹**：创建 / 重命名 / 删除，一个单词可同时归入多个收藏夹
- **搜索**：按编号跳转，或按「包含 / 开头 / 结尾」模糊匹配，支持多关键词
- **朗读**：有道 / 百度在线 TTS，支持英音 / 美音切换、自动连读、从指定词条开始
- **词表中心** `/favorites`（全部单词 + 各收藏夹 + 筛选 / 统计 / 连读）与**单词测试** `/quiz`，答题结果自动进入统计链路；旧链接 `/learn` 只是一层跳转壳，会自动落到 `/favorites`

### 学习统计与激励（`/study`）

所有统计都从**追加型事件流 `study_events`** 派生，而非各模块各自记账：

| 模块 | 说明 |
|---|---|
| 打卡日历 | 当日答题 ≥10 题或新学 ≥5 词即打卡；连续天数、最佳连续、每月 2 次补签 |
| 掌握率趋势 | 按日快照绘制折线，缺失日沿用最近一次快照，**绝不把今天的数据倒推成历史** |
| 词类 / 分册分布 | 已学与总词数并排展示，可下钻到具体单词列表 |
| 复习队列 | 简化艾宾浩斯调度（答对 1/2/≥3 次分别 +1/+3/+7 天，答错重置），支持 `?review=1` 专项复习 |
| 徽章 | 连续打卡、掌握词数、累计学时三类共 15 枚 |
| 排行榜 | 全员可见，积分 / 掌握词数等维度，游客自动排除 |
| 积分 | 打卡与连续打卡奖励，可在「我」页面查看 |

> **服务端权威**：「是否掌握」等式由服务端计算（≥3 次答对且正确率 ≥80%），客户端只能上报「哪个词、什么动作、对或错」，无法靠伪造请求虚增进度。事件带 `uid` 唯一索引，断网重试也不会重复计数。

### 一起背（`/together`）

房间制组队背词：创建 / 加入 6 位邀请码房间、选择共享词表范围、查看成员进度、互相点赞。成员进度**只读消费** `daily_stats`，与单人模式口径完全一致。首版不做实时对战。

### 账号与安全

- 零依赖实现的 OAuth 2.0 / OIDC 风格单点登录：Token 签发（HMAC-SHA256）、刷新、单点注销、OIDC Discovery（`/api/auth/.well-known/openid-configuration`）
- PBKDF2-HMAC-SHA256 密码哈希、5 次失败锁定 15 分钟、安全事件审计日志
- 按 IP 分桶限流：登录 / 注册 / 游客 / 改密 / 登出 / 续期 / 收藏夹 / 学习上报 / 补签 / 重建进度 / 一起背写操作，阈值按校园网 NAT 共享出口放宽
- `/api/online/stream` 是公开 SSE 端点，按 IP 4 条 + 全局 200 条双配额，避免脚本批量建连占满线程
- 内部异常只回固定文案，SQLite 报错与表结构只写服务端 stderr，不进 HTTP 响应
- 三级角色：`admin` / `user` / `guest`，权限在服务端逐接口校验
- 公开注册、游客体验（只读、不能写收藏夹）

| 角色 | 默认权限 |
|---|---|
| `admin` | `admin:users`, `admin:config`, `words:read`, `words:star` |
| `user` | `words:read`, `words:star` |
| `guest` | `words:read` |

### 运维可视化

- **在线人数**：基于 SSE 长连接 + 心跳，连接建立 / 断开即时更新，全站角标显示
- **数据报表** `/report`（仅管理员）：来访人数、访问趋势、行为分布，支持日期范围筛选，桌面与移动端自适应
- **用户管理** `/admin`：用户 CRUD、角色与密码管理、安全日志

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.10+ **仅标准库**：`http.server`、`sqlite3`、`hmac`、`hashlib`、`secrets`、`threading` |
| 存储 | SQLite（`data/users.db` + `data/words.db`），WAL 模式，无需数据库服务 |
| 前端 | 原生 HTML / CSS / ES5 风格 JavaScript，内联 SVG 图表，无框架、无构建步骤 |
| 认证 | 自实现 HMAC 签名 Token + 会话表，无第三方 SDK |
| 外部服务 | 仅有道 / 百度在线 TTS（可选，离线时只影响朗读） |

**零依赖是刻意的设计约束**，贡献代码时请一并遵守：不要引入 `pip` 包或 npm 包。

## 快速开始

### 1. 环境

只需 Python 3.10 或更高版本（已在 3.13 验证）。无需安装任何第三方包。

```bash
git clone https://github.com/nizitao/word3500-public.git
cd word3500
python -m api.server
```

Windows 用户也可以直接双击根目录的 [`run.bat`](run.bat)。

启动后服务监听 `0.0.0.0:8000`，默认自动打开浏览器；访问 <http://127.0.0.1:8000/>。

```bash
python -m api.server 8080            # 指定端口
python -m api.server --no-browser    # 不自动打开浏览器（服务器 / CI 用）
python -m api.server --help          # 查看用法
```

### 2. 首次启动会发生什么

- 从 [`data/3500.txt`](data/3500.txt) 导入基础词条到 `data/words.db`（每 3 行一组：单词 / 音标 / 释义）
- 创建 `data/users.db`、初始化表结构与索引、生成随机 HMAC 签名密钥（存于 `users.db` 的 `_config` 表）
- 创建管理员账号 `admin`：**口令随机生成**（`secrets.token_urlsafe(18)`），写入 `data/initial_admin_password.txt`（权限 600、已 gitignore），控制台只打印文件路径，不打印明文
- 该账号带 `must_change_password=1`：**改密之前除改密 / 登出 / 续期 / me 之外的接口一律返回 403 `password_change_required`**，前端据此把用户留在改密页

> ⚠️ 初始口令只出现一次。登录改密后请删除 `data/initial_admin_password.txt`；若文件写入失败，口令会打到 stderr 供运维救急。
> 源码与默认配置里**没有任何硬编码口令**，也不存在可直接登录的固定账户。

### 3. 可选环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WORD3500_PORT` | `8000` | 监听端口；命令行显式传端口时以命令行为准 |
| `WORD3500_NO_AUTH` | 未设置 | 设为 `1` / `true` / `yes` 可跳过认证，**仅限本地开发调试** |
| `WORD3500_SMTP_HOST` / `_PORT` / `_USER` / `_PASS` / `_TLS` / `_FROM` | — | 邮件通知模块 `api/email_sender.py` 的可选配置，主流程当前不调用 |

## 页面一览

| 路径 | 说明 | 登录要求 |
|---|---|---|
| `/`、`/index.html` | 词表主页（分页 / 分册 / 收藏 / 朗读） | 游客可浏览 |
| `/learn` | 旧链接兜底：跳转壳，自动 `location.replace('/favorites')` | — |
| `/quiz` | 单词测试；`?review=1` 进入复习队列 | 登录 |
| `/search` | 搜索 | 登录 |
| `/favorites` | 收藏夹管理 | 登录 |
| `/study` | 学习统计、打卡、复习、徽章、排行榜 | 登录 |
| `/together` | 一起背房间 | 登录 |
| `/me` | 个人中心、积分、徽章 | 登录 |
| `/admin` | 用户管理 | `admin` |
| `/report` | 访问数据报表 | `admin` |
| `/login`、`/register` | 登录与注册 | — |

全站底部为五个槽位：**单词 ｜ 收藏 ｜ 一起背（中央悬浮按钮）｜ 学习 ｜ 我**，由 `web/js/nav.js` 自注入样式，页面本身只需一行 `<script>`；中央按钮点开是「单词训练 / 单词自检」快捷面板。

## 数据说明

| 文件 | 内容 | 是否随仓库分发 |
|---|---|---|
| `data/3500.txt` | 基础词表源文件 | ✅ 是 |
| `data/english_done/` | 分册分单元教材词表 | ❌ 仅保留 `.gitkeep`，需自行准备 |
| `data/users.db` | 用户、会话、审计日志、签名密钥、访问日志 | ❌ 运行时生成，已 gitignore |
| `data/words.db` | 词条、收藏夹、学习事件、进度、每日快照、房间 | ❌ 运行时生成，已 gitignore |
| `data/initial_admin_password.txt` | 首启随机生成的管理员初始口令（**凭据，改密后请删除**） | ❌ 运行时生成，已 gitignore |

**关于分册数据**：仓库自带的 `3500.txt` 不含册 / 单元信息，全部词条会落入「未分册」桶。如果希望使用完整的 7 册分单元浏览，请把教材词表放进 `data/english_done/`，然后运行：

```bash
python -m api.import_books --dry    # 预览，不写入
python -m api.import_books          # 正式导入（幂等，可重复运行）
```

**学习数据采用三层结构**：`study_events`（追加型事件流，唯一事实来源）→ `learn_progress`（当前状态，仅服务端写入）→ `daily_stats`（按日聚合的读优化层）。如需从事件流重建进度，管理员可调用 `POST /api/admin/rebuild-progress`。

## API 概览

所有 `/api/*` 接口统一返回 JSON；未知路径返回 **JSON 404**（`{"error":"not_found"}`），不是 HTML 错误页。需要鉴权的接口接受 `Authorization: Bearer <token>`，或 `X-Auth-Token: <token>`（后者用于反向代理剥除 Authorization 头的场景）。

| 分组 | 代表端点 |
|---|---|
| 单词 | `GET /api/words`、`/api/words/all`、`/api/books`、`/api/search` |
| 收藏夹 | `GET/POST /api/collections`、`/api/collections/words`、`/api/collections/word-status`、`POST /api/collections/toggle` |
| 认证 | `POST /api/auth/login`、`/logout`、`/refresh`、`/slo`、`/register`、`/change-password`、`/token`、`/guest-login`；`GET /api/auth/me`、`/api/auth/userinfo`、`/api/auth/.well-known/openid-configuration`（OIDC Discovery 挂在 `/api/auth` 前缀下） |
| 学习 | `POST /api/study/events`（唯一写入口）、`/api/study/makeup`；`GET /api/study/summary`、`/calendar`、`/day`、`/mastery-trend`、`/pos-stats`、`/book-stats`、`/review`、`/review/queue` |
| 激励 | `GET /api/badges`、`/api/leaderboard` |
| 一起背 | `GET /api/together/rooms`、`/room`、`/scopes`；`POST /api/together/create`、`/join`、`/leave`、`/like`、`/dissolve` |
| 在线与报表 | `GET /api/online/stream`（SSE）、`/api/online`、`/api/admin/report`；`POST /api/online/ping`、`/leave` |
| 管理 | `GET/POST /api/auth/users`、`/api/auth/events`、`POST /api/admin/rebuild-progress` |

更完整的接口与数据模型设计见 [`docs/学习核心功能设计方案.md`](docs/学习核心功能设计方案.md)。

## 目录结构

```
word3500/
├── api/
│   ├── server.py          # 进程入口：组装处理器、声明式路由表、监听端口
│   ├── routes_auth.py     # 认证 / 用户 / 会话 / 权限 / 审计日志接口
│   ├── routes_words.py    # 词表 / 搜索 / 收藏夹 / 学习进度接口
│   ├── routes_study.py    # 学习统计 / 事件上报 / 徽章 / 排行榜接口
│   ├── routes_together.py # 一起背（学习房间）接口
│   ├── routes_online.py   # 在线人数（SSE / 心跳）接口
│   ├── routes_admin.py    # 管理员报表与自愈工具
│   ├── auth.py            # 认证与用户的核心实现（Token / 会话 / 哈希）
│   ├── study.py           # 学习事件流、进度、统计聚合、徽章、排行榜
│   ├── together.py        # 「一起背」房间数据层
│   ├── analytics.py       # 访问日志、在线人数、报表聚合
│   ├── db.py              # words.db 的表 / 列 / 索引唯一定义处（幂等迁移）
│   ├── http_base.py       # 静态文件、JSON 响应、鉴权与限流的公共骨架
│   ├── limits.py          # 进程内按 IP 滑动窗口限流 + SSE 连接配额
│   ├── email_sender.py    # 可选邮件通知模块
│   ├── import_books.py    # 分册教材词表导入（幂等）
│   └── migrate_split_db.py# 旧版单库拆分迁移
├── web/                   # 前端页面与静态资源
│   ├── *.html             # 各功能页面
│   ├── style.css / study.css / report.css / together.css
│   └── js/                # app / auth / nav / quick-search / search / quiz /
│                          # favorites 内联逻辑 / study / together / charts /
│                          # online / admin / report / liquid-glass
├── data/
│   ├── 3500.txt           # 基础词表（已入库）
│   └── english_done/      # 分册词表放置目录
├── docs/                  # 设计文档
├── config/                # 历史部署配置与说明
├── run.bat                # Windows 一键启动
├── LICENSE                # GPL-3.0 全文
└── README.md
```

## 部署与安全建议

- 服务直接监听 `0.0.0.0`，**对外提供服务时请置于反向代理之后并启用 HTTPS**；不要把服务端口直接暴露到公网。
- 首次部署后立即用 `data/initial_admin_password.txt` 里的随机口令登录并改密，改完删除该文件，并按需关闭公开注册。
- `users.db` 内含密码哈希、会话 Token、审计日志与 HMAC 签名密钥；`words.db` 内含用户收藏与学习记录。**两者都不要提交到公开仓库，也不要作为附件分发**。`.gitignore` 只作用于未来的提交 —— 本仓库的历史已经过重写清洗，`git log --all -- data/*.db` 为空，可以放心公开。
- `WORD3500_NO_AUTH=1` 会完全跳过鉴权，仅限本机调试。
- 朗读依赖第三方在线 TTS，离线环境下不会出声，其余功能不受影响。
- 升级或重启前确认端口没有被旧进程占用（见下方 FAQ）。

## 开发

没有构建步骤：修改 `web/` 下的文件后刷新浏览器即可（静态资源使用 `no-cache` + `Last-Modified` 协商缓存）；修改 Python 后重启服务。

后端改动请保持零第三方依赖。前端为兼容老设备，使用 ES5 风格语法，不依赖可选链、箭头函数等新特性。

仓库根目录以下划线开头的 `_*.py` / `_*.mjs` 是开发期的临时验收脚本（接口回归、浏览器自动化等），**不属于运行时依赖**，也不会被服务加载。

### 常见问题

**Q：学习页报 `Unexpected token '<', "<!DOCTYPE "... is not valid JSON`？**

通常是同一个端口上残留了**旧版本的服务进程**，请求被旧进程接管，学习接口落到了 404 HTML 页面。先查并清理重复监听，再重启：

```bash
netstat -ano | findstr :8000     # Windows
# 或 lsof -i :8000              # macOS / Linux
```

**Q：端口被占用？**

```bash
python -m api.server 8080
```

## 贡献

欢迎 Issue 与 Pull Request。提交前请确认：

1. 没有引入任何第三方依赖；
2. 没有把 `data/*.db`、密钥或个人数据加入提交；
3. 涉及统计口径的改动，同步更新 `docs/` 下的设计文档。

## 词表说明

词表用于英语教学与自学，音标与中文释义仅供参考，请以权威词典和教材为准。分册教材词表不包含在本仓库中，请自行准备合法来源的数据。

## 致谢

本项目改写自 [pluto0x0/word3500](https://github.com/pluto0x0/word3500)（2019，PHP + MySQL 实现，GPL）。原项目的功能构想与词表数据是本项目的起点；当前版本为 Python 标准库 + SQLite + 原生前端的重写实现，上游的版权与许可声明一并保留。

## 许可证

本项目以 **GNU General Public License v3.0 (GPL-3.0)** 发布，完整条款见 [`LICENSE`](LICENSE)（[config/LICENSE](config/LICENSE) 为同一份副本，供单独分发 `config/` 目录时使用）。

```
Copyright (C) 2026 word3500 contributors
```

GPL 是**强 Copyleft** 许可证：你可以自由使用、修改和分发本项目，但如果分发修改版或基于本项目的衍生作品，必须以同样的 GPLv3 授权并提供完整源码，且不得附加额外限制。

---

**word3500** · 用最少的依赖，把背单词这件事做完整。
