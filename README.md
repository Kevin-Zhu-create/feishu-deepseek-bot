# 飞书智能体后端

基于 FastAPI + DeepSeek，接收飞书消息并自动回复；支持语音/音频文件转文字。

## 目录

- `main.py` — FastAPI 主服务
- `requirements.txt` — Python 依赖
- `.env.example` — 环境变量模板
- `Procfile` / `railway.json` — 部署配置

## 功能

- ✅ 文本消息：调用 DeepSeek 自动回复
- ✅ 语音消息：优先使用飞书自带语音识别，秒出文字
- ✅ 音频/视频文件：上传到用户云盘 → 生成妙记 → 获取转录文字

## 部署步骤

### 1. 准备凭据

需要 6 个环境变量，去飞书开放平台（https://open.feishu.cn/app）获取：

| 变量 | 来源 |
|------|------|
| `FEISHU_APP_ID` | 应用凭证页 → App ID |
| `FEISHU_APP_SECRET` | 应用凭证页 → App Secret |
| `FEISHU_ENCRYPT_KEY` | 事件订阅页 → 加密策略（未开启则留空） |
| `FEISHU_VERIFICATION_TOKEN` | 事件订阅页 → Verification Token |
| `FEISHU_REDIRECT_URI` | OAuth 回调地址：`https://<你的域名>/callback` |
| `DEEPSEEK_API_KEY` | https://platform.deepseek.com/api_keys |

### 2. 开通权限

在飞书开放平台 → 应用 → 权限管理，开通：

**应用身份权限（tenant token）：**
- `im:message:send`
- `im:message:receive`
- `contact:user.base:readonly`

**用户身份权限（user token，语音转文字必须）：**
- `drive:drive:write`
- `minutes:minutes.upload:write`
- `minutes:minutes.search:read`
- `minutes:minutes.basic:read`
- `minutes:minutes.artifacts:read`
- `minutes:minutes.media:export`

### 3. 配置安全设置

在飞书开放平台 → 应用 → 安全设置：
- 添加 **重定向 URL**：`https://<你的域名>/callback`

### 4. 部署到 Railway

1. 把代码推送到 GitHub 仓库
2. 在 [railway.app](https://railway.app) 用 GitHub 登录
3. New Project → Deploy from GitHub repo → 选择本项目
4. 在 Variables 中填入上述环境变量
5. 自动部署，获取公网 URL（如 `https://xxx.up.railway.app`）

### 5. 配置飞书事件订阅

在飞书开放平台 → 应用 → 事件订阅：
- **请求网址 URL**: `https://<你的URL>/webhook`
- 订阅事件：`im.message.receive_v1`（接收消息）
- 保存后飞书会验证 URL，验证通过后即可使用

### 6. 完成用户授权（语音转文字必须）

浏览器打开：

```
https://<你的URL>/auth
```

用飞书账号授权后，后端会拿到 `user_access_token`，语音转文字功能即生效。

> ⚠️ 容器重启后内存中的 refresh_token 会丢失，需要重新访问 `/auth` 授权。

### 7. 测试

- 在飞书里 @你的智能体发文字，它通过 DeepSeek 回复
- 发送语音消息或音频文件，自动转写文字

## 本地调试

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入真实凭据
uvicorn main:app --reload --port 8080
```

## 常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| 文本能回，语音转文字报 99991663 | 妙记 API 需要 user_access_token | 完成 `/auth` 授权 |
| 语音消息不回复 | 没开通 `im:message:receive` / 机器人能力未启用 | 检查权限并重新发布 |
| `/auth` 打开后报错 | 重定向 URL 未配置或 scope 未开通 | 检查安全设置和权限管理 |
