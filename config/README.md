<!-- SPDX-FileCopyrightText: 2026 word3500 contributors -->
<!-- SPDX-License-Identifier: GPL-3.0 -->

# word3500（历史部署说明）

> 本文记录的是早期版本的部署与配置，部分内容（启动路径、线上部署信息）可能已过时。
> 项目总览、快速开始、完整功能列表与最新许可证请以仓库根目录 [README.md](../README.md) 为准。

高考英语词汇查询工具 —— 分页浏览、分册分单元、多收藏夹、搜索、单词 / 自动朗读、英音 / 美音切换、SSO 单点登录。

## 技术栈

- Python 3（仅标准库：`http.server` + `sqlite3`，**零第三方依赖**）
- 原生 HTML / CSS / JavaScript（无 jQuery、无前端框架）
- 数据持久化使用 SQLite，无需安装数据库服务

## 功能

- 分页浏览（每页 50 词）
- **分册分单元**浏览（人教版必修 3 册 + 选择性必修 4 册，共 7 册，按 Unit 筛选）
- **多收藏夹**：可创建 / 重命名 / 删除多个收藏夹，单词可同时归入多个收藏夹
- 搜索（按编号跳转 / 按单词 包含·开头·结尾 模糊匹配）
- 单词朗读（有道 TTS）
- 自动朗读（一遍 / 循环 / 顺序，英文 + 中文释义，百度 TTS）
- 英音 / 美音 切换
- 起始位置选择（"选择"列单选，自动朗读从此词开始）
- **SSO 单点登录**（OAuth 2.0 / OpenID Connect 风格，零第三方依赖）
  - Token 签发（HMAC-SHA256 签名 JWT）
  - 登录 / 登出 / 单点注销（SLO）/ 游客登录
  - 用户注册（服务端 IP 限流防刷，不使用第三方人机验证）
  - 自动 Token 刷新与会话超时管理
  - 基于角色的权限控制（admin / user / guest）
  - 防暴力破解（5 次失败锁定 15 分钟）
  - 安全事件审计日志
  - OIDC Discovery 端点 (`.well-known/openid-configuration`)
- **管理员面板**（`/admin`）：用户 CRUD、搜索筛选、角色 / 密码管理、安全日志

## 用户与权限

首次启动自动创建管理员账户 `admin`：**口令随机生成**，写入 `data/initial_admin_password.txt`（权限 600，已 gitignore）；登录后请立即改密并删除该文件。

| 角色 | 默认权限 |
|------|---------|
| admin | `admin:users`, `admin:config`, `words:read`, `words:star` |
| user | `words:read`, `words:star` |
| guest | `words:read`（游客只读，不能操作收藏夹） |

可通过 `WORD3500_NO_AUTH=1` 环境变量禁用认证（开发调试用）。

### 可选环境变量

| 变量 | 说明 |
|------|------|
| `WORD3500_NO_AUTH` | 设为 `1` 跳过认证（开发调试）|
| `WORD3500_PORT` | 监听端口（默认 8000）|

## 本地运行

需要 Python 3（已在 3.13 验证）。

```shell
# 方式一：直接运行（默认端口 8000）
cd api
python server.py

# 可选：指定端口
python server.py 8080
```

```bat
REM 方式二：Windows 双击
run.bat
```

启动后自动打开浏览器，访问 <http://127.0.0.1:8000/> 。

## 数据库

数据库已拆分为两个独立的 SQLite 文件：

| 文件 | 表 | 说明 |
|------|-----|------|
| `users.db` | `users` / `sessions` / `auth_events` / `_config` | 用户、会话、审计日志、配置 |
| `words.db` | `words` / `collections` / `collection_words` / `user_stars` | 单词、收藏夹、收藏关系 |

- 首次启动会自动把 `3500.txt` 导入到 `words.db`。
- `words` 表含 `book`（册编号 1-7）、`unit`（单元名）字段，用于分册分单元浏览。

### 数据导入 / 迁移脚本

```shell
# 将 data/english_done/ 下的分册分单元教材词库导入 words.db（幂等，可重复运行）
cd api
python import_books.py          # 正式导入
python import_books.py --dry    # 预览，不写入

# 将旧版单库 data/word3500.db 拆分为 users.db + words.db（会先备份）
python migrate_split_db.py          # 拆分
python migrate_split_db.py --dry    # 预览
```

## 发音说明

英文与中文朗读使用有道 / 百度**在线** TTS，需要联网；离线时朗读按钮不会出声，其余功能不受影响。

## 部署建议

对外提供服务时，建议用 systemd（或容器 / 进程管理器）守护进程，并置于反向代理之后启用 HTTPS。下面是一份示例 unit 配置，请按实际路径、用户与端口调整：

```ini
[Unit]
Description=word3500
After=network.target

[Service]
Type=simple
WorkingDirectory=/srv/word3500
ExecStart=/usr/bin/python3 -m api.server --no-browser
Environment=WORD3500_PORT=8000
Restart=always

[Install]
WantedBy=multi-user.target
```

常用管理命令（服务名按实际填写）：

```shell
systemctl status word3500
systemctl restart word3500
journalctl -u word3500 -f
```

> **升级规则**：`users.db` 与 `words.db` 保存全部用户数据，**只替换代码，不要覆盖数据库**。
> 更新前先用 `sqlite3 .backup` 备份两个库，更新完成后再重启服务。
> 具体部署拓扑与访问地址请自行填写，不要写进公开仓库。

## 文件结构

```
word3500/
├── api/                    # 后端 Python 服务与脚本
│   ├── server.py          —— HTTP 服务 + SQLite 数据导入 + REST API + SSO 认证端点
│   ├── auth.py            —— SSO 认证模块（Token / 用户 / 会话 / 权限 / 审计日志）
│   ├── email_sender.py    —— 邮件发送模块（smtplib，可选使用）
│   ├── import_books.py    —— 教材词库导入脚本
│   └── migrate_split_db.py—— 单库拆分迁移脚本
├── web/                   # 前端页面与静态资源
│   ├── index.html         —— 主页面（分页 / 分册 / 收藏 / 朗读，图标为内联 SVG）
│   ├── login.html         —— SSO 登录页面
│   ├── register.html      —— 用户注册页面
│   ├── admin.html         —— 管理员用户管理面板
│   ├── favorites.html     —— 收藏夹页面
│   ├── search.html        —— 搜索页面
│   ├── style.css          —— 样式
│   └── js/                —— JavaScript 脚本
│       ├── app.js         —— 前端主逻辑（分页 / 分册 / 收藏夹 / 搜索 / 朗读）
│       ├── auth.js        —— 前端认证模块（Token 管理 / 自动刷新 / 会话超时）
│       ├── learn.js / quiz.js / search.js / admin.js —— 各页面逻辑
├── data/                  # 数据文件（数据库与词库，本地不入库）
│   ├── 3500.txt           —— 基础单词数据源（每 3 行一组：单词 / 音标 / 释义）
│   ├── english_done/      —— 分册分单元教材词库（b1-b3 必修、xb1-xb4 选择性必修）
│   ├── users.db / words.db —— 首次启动 / 迁移后生成的数据库
│   └── word3500.db(.bak)  —— 旧版单库（迁移用）
├── config/                # 配置与文档
│   ├── requirements.txt
│   ├── run.bat            —— Windows 一键启动
│   └── README.md / WHATS_NEW.md
├── utils/                 # 工具函数占位目录
├── .gitignore             —— 必须位于仓库根目录（忽略数据库/缓存/工具目录）
└── LICENSE                —— GPL-3.0 全文
```

## REST API

### 单词数据
| 接口 | 说明 |
|------|------|
| `GET /api/words?page=N&book=B&unit=U` | 获取第 N 页单词，可按册 / 单元筛选 |
| `GET /api/books` | 获取所有有数据的册与单元列表 |
| `GET /api/search?pattern=contain\|begin\|end&search=KEY` | 搜索单词 |

### 收藏夹
| 方法 | 接口 | 说明 |
|------|------|------|
| `GET` | `/api/collections` | 列出当前用户的收藏夹（含单词数）|
| `POST` | `/api/collections` | 创建 / 重命名 / 删除收藏夹（`action`）|
| `GET` | `/api/collections/words?cid=X` | 获取指定收藏夹内的单词 |
| `GET` | `/api/collections/word-status?wid=X` | 查单词位于哪些收藏夹 |
| `POST` | `/api/collections/toggle` | 设置某单词所属的收藏夹（`{wid, cids}`）|

> 以上接口均需 Bearer Token（经 Cloudflare 时使用 `X-Auth-Token` 头）认证。

### 认证
| 方法 | 接口 | 说明 |
|------|------|------|
| `POST` | `/api/auth/login` | 登录（JSON: username, password） |
| `POST` | `/api/auth/logout` | 登出 |
| `POST` | `/api/auth/refresh` | 刷新 Token（JSON: refresh_token） |
| `POST` | `/api/auth/slo` | 单点注销（撤销所有会话） |
| `POST/GET` | `/api/auth/guest-login` | 游客登录 |
| `POST` | `/api/auth/register` | 公开注册 |
| `POST` | `/api/auth/change-password` | 修改密码 |
| `POST` | `/api/auth/token` | OAuth 2.0 Token 端点 |
| `GET` | `/api/auth/authorize` | OAuth 2.0 授权端点 |
| `GET` | `/api/auth/me` | 当前用户信息 |
| `GET` | `/api/auth/userinfo` | OIDC UserInfo |
| `GET` | `/api/auth/check-username` | 用户名可用性检查 |
| `GET` | `/api/auth/.well-known/openid-configuration` | OIDC Discovery |
| `GET/POST` | `/api/auth/users` | 用户管理（admin）|
| `GET` | `/api/auth/users/<id>` | 用户详情（admin）|
| `GET` | `/api/auth/events` | 安全事件日志（admin）|

## 许可证

本项目以 **GNU General Public License v3.0（GPL-3.0）** 发布（强 Copyleft，衍生作品须同样以 GPLv3 开源）。

- 完整条款见仓库根目录 [`LICENSE`](../LICENSE)；本目录 `config/LICENSE` 是同一份副本，供单独分发 `config/` 时使用。
- 本项目改写自 [pluto0x0/word3500](https://github.com/pluto0x0/word3500)（Copyright (C) 2019 pluto0x0，上游以 GPLv2 声明），上游版权与许可声明予以保留；当前版本的版权声明见根目录 `README.md`。
