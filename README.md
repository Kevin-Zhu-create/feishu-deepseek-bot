# 飞书智能体后端

基于 FastAPI + OpenAI，接收飞书消息并自动回复。

## 目录

- `main.py` — FastAPI 主服务
- `requirements.txt` — Python 依赖
- `.env.example` — 环境变量模板
- `Procfile` / `railway.json` — 部署配置

## 部署步骤

### 1. 准备凭据

需要 5 个环境变量，去飞书开放平台获取：

| 变量 | 来源 |
|------|------|
| `FEISHU_APP_ID` | 应用凭证页 → App ID |
| `FEISHU_APP_SECRET` | 应用凭证页 → App Secret |
| `FEISHU_ENCRYPT_KEY` | 事件订阅页 → 加密策略 → Encrypt Key |
| `FEISHU_VERIFICATION_TOKEN` | 事件订阅页 → Verification Token |
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys |

### 2. 部署到 Railway

1. 把代码推送到 GitHub 仓库
2. 在 [railway.app](https://railway.app) 用 GitHub 登录
3. New Project → Deploy from GitHub repo → 选择本项目
4. 在 Variables 中填入上述 5 个环境变量
5. 自动部署，获取公网 URL（如 `https://xxx.up.railway.app`）

### 3. 配置飞书事件订阅

在飞书开放平台 → 应用 → 事件订阅：
- **请求网址 URL**: `https://<你的URL>/webhook`
- 订阅事件：`im.message.receive_v1`（接收消息）
- 保存后飞书会验证 URL，验证通过后即可使用

### 4. 测试

在飞书里 @你的智能体发消息，它应该会通过 OpenAI 回复。

## 本地调试

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入真实凭据
uvicorn main:app --reload --port 8080
```
