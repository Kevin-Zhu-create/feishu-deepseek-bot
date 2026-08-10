"""
飞书智能体后端 — 接收消息 → DeepSeek → 回复
部署后需在飞书开放平台配置事件订阅 URL: https://<your-domain>/webhook
"""

import json
import hashlib
import hmac
import time
import os
import logging
from typing import Optional

import httpx
from openai import OpenAI
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# 环境变量（部署时配置）
# ============================================================
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")  # 事件订阅加密 Key
FEISHU_VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

app = FastAPI(title="Feishu Bot Backend")

# ============================================================
# 飞书事件解密（可选，仅加密模式需要）
# ============================================================
AES_CIPHER = None
if FEISHU_ENCRYPT_KEY:
    from base64 import b64decode
    from Crypto.Cipher import AES as _AES
    from Crypto.Util.Padding import unpad as _unpad

    class AESCipher:
        def __init__(self, key: str):
            self.key = hashlib.sha256(key.encode()).digest()

        def decrypt(self, ciphertext: str) -> str:
            data = b64decode(ciphertext)
            iv = data[:16]
            encrypted = data[16:]
            cipher = _AES.new(self.key, _AES.MODE_CBC, iv=iv)
            plain = _unpad(cipher.decrypt(encrypted), _AES.block_size)
            return plain.decode("utf-8")
    AES_CIPHER = AESCipher(FEISHU_ENCRYPT_KEY)

# ============================================================
# 飞书 API 客户端
# ============================================================
class FeishuClient:
    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    SEND_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"

    def __init__(self):
        self._token: Optional[str] = None
        self._token_expires: float = 0

    async def get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.TOKEN_URL,
                json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            )
            data = resp.json()
            if data.get("code") != 0:
                raise Exception(f"获取 token 失败: {data}")
            self._token = data["tenant_access_token"]
            self._token_expires = time.time() + data.get("expire", 7200)
            return self._token

    async def send_text(self, open_id: str, text: str) -> dict:
        token = await self.get_token()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.SEND_MSG_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": open_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}),
                },
            )
            return resp.json()

    async def get_user_name(self, open_id: str) -> str:
        token = await self.get_token()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/contact/v3/users/{open_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"user_id_type": "open_id"},
            )
            data = resp.json()
            return data.get("data", {}).get("user", {}).get("name", "用户")

feishu = FeishuClient()

# ============================================================
# DeepSeek 客户端（兼容 OpenAI SDK）
# ============================================================
deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)

async def chat_with_deepseek(user_message: str, user_name: str) -> str:
    """调用 DeepSeek 处理用户消息"""
    try:
        resp = deepseek_client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一个飞书智能助手，名字叫「朱可及的飞书 CLI」。"
                        "你直接、简洁地回答问题，不啰嗦、不废话。"
                        "你可以帮助用户处理各种任务，包括但不限于："
                        "日程管理、消息发送、文档操作、语音转文字、信息查询等。"
                        f"当前和你对话的用户叫{user_name}。"
                        "用中文回复。"
                    ),
                },
                {"role": "user", "content": user_message},
            ],
            temperature=0.7,
            max_tokens=2000,
        )
        return resp.choices[0].message.content or "（模型未返回内容）"
    except Exception as e:
        return f"调用 DeepSeek 出错: {str(e)}"

# ============================================================
# Webhook 路由
# ============================================================
@app.api_route("/webhook", methods=["GET", "POST"])
async def webhook(request: Request):
    """
    飞书事件订阅入口。
    - 首次配置时飞书发送 URL 验证请求（POST with type=url_verification）
    - 之后所有事件通过 POST 加密推送
    """
    body = await request.json()

    # --- URL 验证 ---
    if body.get("type") == "url_verification":
        challenge = body.get("challenge", "")
        logger.info(f"URL 验证成功")
        return JSONResponse({"challenge": challenge})

    # --- 消息事件 ---
    encrypt_text = body.get("encrypt")
    if encrypt_text:
        # 加密模式：需要解密
        if not AES_CIPHER:
            raise HTTPException(400, "未配置 Encrypt Key，无法解密")
        try:
            plain_json = AES_CIPHER.decrypt(encrypt_text)
            event_data = json.loads(plain_json)
        except Exception as e:
            logger.error(f"解密失败: {e}")
            raise HTTPException(400, f"解密失败: {e}")
    else:
        # 明文模式：body 本身就是事件数据
        event_data = body

    event_type = event_data.get("type", "")
    event = event_data.get("event", {})

    # 只处理消息事件
    if event_type == "im.message.receive_v1":
        msg_type = event.get("message", {}).get("message_type", "")
        sender_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")

        if not sender_id:
            return JSONResponse({"code": 0})

        # 提取文本
        text = ""
        if msg_type == "text":
            text = json.loads(event["message"]["content"]).get("text", "")
        else:
            # 非文本消息，提示用户
            text = "（非文本消息）"

        if text:
            logger.info(f"收到消息 [{sender_id}]: {text[:200]}")
            user_name = await feishu.get_user_name(sender_id)
            reply = await chat_with_deepseek(text, user_name)
            result = await feishu.send_text(sender_id, reply)
            logger.info(f"回复结果: {result}")

    return JSONResponse({"code": 0})


# ============================================================
# 健康检查
# ============================================================
@app.get("/")
async def root():
    return {"status": "ok"}

@app.get("/health")
async def health():
    return {"status": "ok", "app_id": FEISHU_APP_ID[:8] + "***" if FEISHU_APP_ID else "未配置"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
