<!-- SPDX-FileCopyrightText: 2026 word3500 contributors -->
<!-- SPDX-License-Identifier: GPL-3.0 -->

# WHAT'S NEW — word3500 v2.0

## SSO 单点登录系统

- **OAuth 2.0 / OpenID Connect 风格认证**，零第三方依赖（仅 Python 标准库）
- HMAC-SHA256 签名 JWT Token（Access 30min / Refresh 7天 / ID Token）
- PBKDF2-HMAC-SHA256 密码哈希（10 万次迭代）
- 基于角色的权限控制（admin / user），可扩展权限列表
- 防暴力破解：连续 5 次失败锁定 15 分钟
- 安全事件审计日志（登录、登出、刷新、注册、失败等）
- OIDC Discovery 端点（`.well-known/openid-configuration`）
- 会话超时管理：30 分钟空闲 / 8 小时绝对超时
- 支持 `WORD3500_NO_AUTH=1` 环境变量跳过认证（开发调试用）

### 新增文件

| 文件 | 说明 |
|------|------|
| `auth.py` | 认证核心：Token / 密码哈希 / 会话 / 权限 / 审计（~750 行）|
| `auth.js` | 客户端认证模块：Token 管理 / 自动刷新 / 空闲超时 / authFetch |
| `login.html` | SSO 登录页面（支持 redirect 参数） |
| `admin.html` + `admin.js` | 管理员用户管理面板（CRUD + 搜索 + 弹窗）|
| `register.html` | 用户注册页面（含前端实时校验）|
| `email_sender.py` | 邮件发送模块（smtplib，可选使用）|

### 认证 API

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录 |
| POST | `/api/auth/logout` | 登出 |
| POST | `/api/auth/refresh` | 刷新 Token |
| POST | `/api/auth/slo` | 单点注销（撤销所有会话）|
| POST | `/api/auth/register` | 公开注册 |
| POST | `/api/auth/change-password` | 修改密码 |
| POST | `/api/auth/token` | OAuth 2.0 Token 端点 |
| GET | `/api/auth/me` | 当前用户信息 |
| GET | `/api/auth/userinfo` | OIDC UserInfo |
| GET | `/api/auth/users` | 用户列表（admin）|
| GET | `/api/auth/users/<id>` | 用户详情（admin）|
| GET | `/api/auth/events` | 安全事件日志（admin）|

---

## 管理员用户管理面板（/admin）

- 用户列表表格（ID / 用户名 / 邮箱 / 角色 / 状态）+ 搜索筛选
- 新增用户弹窗（可设角色、密码）
- 编辑用户（显示名、邮箱、角色、密码重置）
- 删除确认对话框（不能删除自己）
- 仅 admin 角色可访问，非管理员自动跳回首页

---

## 用户注册

- `/register` — 公开注册页面
- 字段：用户名、密码、确认密码、邮箱
- 前端实时校验（字母开头、密码一致性、邮箱格式）
- 防刷：服务端按客户端 IP 限流（注册 10 次/10 分钟）
- 注册成功后直接登录

> 注：曾接入 Cloudflare Turnstile 人机验证，因其校验域名
> `challenges.cloudflare.com` 在部分网络下不可达，小部件会显示
> 「无法连接到网站」并阻断登录，故已彻底移除，改用服务端限流防刷。

---

## Bug 修复

### `_read_json` 双重读取 Bug（致命）

- **问题**：`_read_body()` 被调用两次，rfile 流被第一次消耗后第二次返回 `b""`，三元表达式永远走 `else → {}`，导致所有 POST API 收到的 body 为空字典
- **影响**：登录、注册、用户管理全部失效
- **修复**：先 `body = self._read_body()` 存为局部变量，再判空 → decode → json.loads

### Cloudflare Rocket Loader 脚本加载顺序

- **问题**：Cloudflare 的 Rocket Loader 延迟了 `auth.js` 执行，导致 `app.js` 引用 `authFetch` 时未定义
- **修复**：所有 `<script>` 标签添加 `data-cfasync="false"`

### Cloudflare 代理剥除 Authorization 头

- **问题**：Cloudflare CDN 自动移除 `Authorization` 请求头，导致 API 调用全部 401
- **修复**：改用自定义头 `X-Auth-Token` 传递 Token

---

## 用户体验改进

- 未登录访问首页 → 立即跳转 `/login`（inline script 在页面渲染前执行，无闪现）
- 登录后顶部工具栏直接显示用户名和登出按钮
- 管理员顶部工具栏显示「管理」入口链接
- 登录页添加「还没有账户？立即注册」按钮
- 单词 API（/api/words、/api/star、/api/search）需要认证
- 分发包不含数据库文件，避免覆盖已有用户数据


