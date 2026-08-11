"""
飞书智能体后端 — 文本对话 + 语音转文字
部署后需在飞书开放平台配置事件订阅 URL: https://<your-domain>/webhook
语音转文字需额外完成 OAuth 授权: https://<your-domain>/auth
"""

import json
import hashlib
import hmac
import time
import os
import asyncio
import logging
import tempfile
from typing import Optional
from urllib.parse import urlencode

import httpx
from openai import OpenAI
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# 环境变量（部署时配置）
# ============================================================
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")
FEISHU_VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
FEISHU_REDIRECT_URI = os.environ.get(
    "FEISHU_REDIRECT_URI", "https://web-production-50302.up.railway.app/callback"
)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# OAuth 授权时申请的 scope（空格分隔）
FEISHU_OAUTH_SCOPES = os.environ.get(
    "FEISHU_OAUTH_SCOPES",
    "minutes:minutes.upload:write minutes:minutes.search:read minutes:minutes.basic:read "
    "minutes:minutes.artifacts:read minutes:minutes.media:export drive:drive offline_access",
)

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
# 用户 access_token 内存缓存
# ============================================================
_user_token_cache = {
    "access_token": None,
    "expires_at": 0,
    "refresh_token": None,
}


def _save_user_token(access_token: str, expires_in: int, refresh_token: str):
    """保存用户 token 到内存（Railway 容器重启后需重新授权）"""
    _user_token_cache["access_token"] = access_token
    _user_token_cache["expires_at"] = time.time() + expires_in - 60
    _user_token_cache["refresh_token"] = refresh_token
    logger.info("user_access_token 已保存")


async def _exchange_token(grant_type: str, code_or_refresh: str) -> dict:
    """用授权码或 refresh_token 换取 user_access_token"""
    payload = {
        "grant_type": grant_type,
        "client_id": FEISHU_APP_ID,
        "client_secret": FEISHU_APP_SECRET,
    }
    if grant_type == "authorization_code":
        payload["code"] = code_or_refresh
        payload["redirect_uri"] = FEISHU_REDIRECT_URI
    else:  # refresh_token
        payload["refresh_token"] = code_or_refresh

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        logger.info(f"token 响应 status={resp.status_code} body={resp.text[:500]}")
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"换取 user_access_token 失败: {data}")
        token_data = data.get("data")
        if not token_data:
            raise Exception(f"token 响应中缺少 data 字段: {data}")
        return token_data


async def get_user_access_token() -> str:
    """获取可用的 user_access_token，自动刷新"""
    if _user_token_cache["access_token"] and time.time() < _user_token_cache["expires_at"]:
        return _user_token_cache["access_token"]

    if _user_token_cache["refresh_token"]:
        logger.info("user_access_token 过期，用 refresh_token 刷新")
        data = await _exchange_token("refresh_token", _user_token_cache["refresh_token"])
        _save_user_token(
            data["access_token"],
            data.get("expires_in", 7200),
            data.get("refresh_token", _user_token_cache["refresh_token"]),
        )
        return _user_token_cache["access_token"]

    raise Exception(
        "尚未完成用户 OAuth 授权，请先访问 /auth 完成授权"
    )


# ============================================================
# 飞书 API 客户端
# ============================================================
class FeishuClient:
    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    SEND_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    DRIVE_UPLOAD_URL = "https://open.feishu.cn/open-apis/drive/v1/files/upload_all"
    MINUTES_UPLOAD_URL = "https://open.feishu.cn/open-apis/minutes/v1/minutes/upload"

    def __init__(self):
        self._token: Optional[str] = None
        self._token_expires: float = 0

    async def get_token(self) -> str:
        """应用身份 tenant_access_token（发消息等）"""
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

    # ---------- 语音转文字（必须用 user_access_token） ----------

    async def download_resource(self, message_id: str, file_key: str) -> bytes:
        """下载消息中的音频/文件资源"""
        token = await self.get_token()
        url = (
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
            f"/resources/{file_key}?type=file"
        )
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            logger.info(f"下载资源成功: {message_id}/{file_key}, size={len(resp.content)}")
            return resp.content

    async def get_root_folder_token(self) -> str:
        """获取「我的空间」根目录 folder token（用户身份）"""
        token = await get_user_access_token()
        url = "https://open.feishu.cn/open-apis/drive/explorer/v2/root_folder/meta"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            data = resp.json()
            if data.get("code") != 0:
                raise Exception(f"获取根目录失败: {data}")
            folder_token = data.get("data", {}).get("token", "")
            if not folder_token:
                raise Exception("根目录 token 为空")
            logger.info(f"根目录 token: {folder_token}")
            return folder_token

    async def upload_to_drive(self, file_name: str, file_content: bytes) -> str:
        """上传文件到飞书 Drive（用户身份），返回 file_token"""
        token = await get_user_access_token()
        folder_token = await self.get_root_folder_token()
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                self.DRIVE_UPLOAD_URL,
                headers={"Authorization": f"Bearer {token}"},
                data={
                    "file_name": file_name,
                    "parent_type": "explorer",
                    "parent_node": folder_token,
                    "size": str(len(file_content)),
                },
                files={
                    "file": (file_name, file_content, "application/octet-stream"),
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                raise Exception(f"上传到 Drive 失败: {data}")
            file_token = data.get("data", {}).get("file_token")
            logger.info(f"上传 Drive 成功: {file_token}")
            return file_token

    async def upload_to_minutes(self, file_token: str) -> str:
        """将 Drive 文件提交到妙记转录（用户身份），返回 minute_token"""
        token = await get_user_access_token()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.MINUTES_UPLOAD_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={"file_token": file_token},
            )
            data = resp.json()
            if data.get("code") != 0:
                raise Exception(f"创建妙记失败: {data}")
            minute_token = data.get("data", {}).get("minute_token")
            logger.info(f"创建妙记成功: {minute_token}")
            return minute_token

    async def get_minutes_subtitle(self, minute_token: str) -> Optional[str]:
        """获取妙记转录文字（用户身份）"""
        token = await get_user_access_token()
        async with httpx.AsyncClient() as client:
            # 方式1: 尝试获取演讲稿/段落
            url = (
                f"https://open.feishu.cn/open-apis/minutes/v1/minutes/{minute_token}"
                f"/transcripts"
            )
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            data = resp.json()
            if data.get("code") == 0:
                transcripts = data.get("data", {}).get("transcripts", [])
                if transcripts:
                    text = "\n".join(
                        t.get("text", "") for t in transcripts if t.get("text")
                    )
                    if text.strip():
                        return text.strip()

            # 方式2: 降级到详细信息的 transcript 字段
            detail_url = (
                f"https://open.feishu.cn/open-apis/minutes/v1/minutes/{minute_token}"
            )
            resp2 = await client.get(
                detail_url, headers={"Authorization": f"Bearer {token}"}
            )
            data2 = resp2.json()
            if data2.get("code") == 0:
                d = data2.get("data", {})
                transcript_text = d.get("transcript", "")
                if transcript_text:
                    return transcript_text
                paragraphs = d.get("paragraphs", [])
                if paragraphs:
                    text = "\n".join(
                        p.get("text", "") for p in paragraphs if p.get("text")
                    )
                    if text.strip():
                        return text.strip()

            return None


feishu = FeishuClient()

# ============================================================
# DeepSeek 客户端
# ============================================================
deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)


async def chat_with_deepseek(user_message: str, user_name: str) -> str:
    """调用 DeepSeek 处理文本消息"""
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
# 语音转文字异步流水线
# ============================================================
async def transcribe_audio(
    message_id: str, file_key: str, file_name: str, open_id: str
):
    """完整的语音转文字流程：下载 → Drive → 妙记 → 转录 → 回复"""
    logger.info(f"开始语音转文字: {file_name}, open_id={open_id}")

    try:
        # Step 1: 下载音频
        logger.info(f"[1/5] 下载音频: {message_id}/{file_key}")
        audio_data = await feishu.download_resource(message_id, file_key)

        # Step 2: 上传到 Drive
        logger.info(f"[2/5] 上传到 Drive")
        drive_token = await feishu.upload_to_drive(file_name, audio_data)

        # Step 3: 创建妙记
        logger.info(f"[3/5] 创建妙记转录")
        minute_token = await feishu.upload_to_minutes(drive_token)

        # Step 4: 轮询等待转录完成
        logger.info(f"[4/5] 等待转录完成...")
        max_wait = 300  # 最多等 5 分钟
        interval = 5
        elapsed = 0
        transcript = None

        while elapsed < max_wait:
            await asyncio.sleep(interval)
            elapsed += interval
            transcript = await feishu.get_minutes_subtitle(minute_token)
            if transcript:
                logger.info(f"转录完成！耗时 {elapsed}s, 文本长度: {len(transcript)}")
                break

        if not transcript:
            # 超时但妙记可能还在处理中，给链接
            link = f"https://meetings.feishu.cn/minutes/{minute_token}"
            await feishu.send_text(
                open_id,
                f"妙记正在转录中，完成后可查看：\n{link}\n\n转录完成后用「帮我转录妙记 {minute_token}」获取文字。",
            )
            return

        # Step 5: 回复转录文字
        logger.info(f"[5/5] 发送转录结果")
        link = f"https://meetings.feishu.cn/minutes/{minute_token}"
        # 如果文本太长，截断
        if len(transcript) > 4000:
            transcript = transcript[:4000] + "\n\n…（文本过长已截断，完整内容见下方链接）"
        await feishu.send_text(
            open_id,
            f"🎙️ 语音转文字结果：\n\n{transcript}\n\n🔗 妙记链接: {link}",
        )

    except Exception as e:
        logger.error(f"语音转文字失败: {e}")
        try:
            await feishu.send_text(
                open_id, f"语音转文字失败: {str(e)}"
            )
        except Exception:
            pass


# ============================================================
# OAuth 授权路由
# ============================================================
@app.get("/auth")
async def auth():
    """生成飞书 OAuth 授权链接，用户点击后完成授权"""
    params = {
        "client_id": FEISHU_APP_ID,
        "response_type": "code",
        "redirect_uri": FEISHU_REDIRECT_URI,
        "scope": FEISHU_OAUTH_SCOPES,
        "state": hashlib.sha256(os.urandom(32)).hexdigest()[:16],
    }
    url = "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urlencode(params)
    logger.info(f"生成 OAuth 链接: {url[:120]}...")
    return {
        "message": "请在浏览器中打开以下链接完成授权",
        "auth_url": url,
    }


@app.get("/callback")
async def callback(code: str = Query(...), state: Optional[str] = None):
    """飞书 OAuth 回调：用授权码换取 user_access_token 并保存"""
    try:
        data = await _exchange_token("authorization_code", code)
        _save_user_token(
            data["access_token"],
            data.get("expires_in", 7200),
            data["refresh_token"],
        )
        return {
            "message": "授权成功！现在可以发送语音/音频消息进行转文字了。",
            "user_name": data.get("name", ""),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OAuth 回调失败: {e}", exc_info=True)
        raise HTTPException(400, f"授权失败: {e}")


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
        logger.info("URL 验证成功")
        return JSONResponse({"challenge": challenge})

    # --- 消息事件 ---
    encrypt_text = body.get("encrypt")
    if encrypt_text:
        if not AES_CIPHER:
            raise HTTPException(400, "未配置 Encrypt Key，无法解密")
        try:
            plain_json = AES_CIPHER.decrypt(encrypt_text)
            event_data = json.loads(plain_json)
        except Exception as e:
            logger.error(f"解密失败: {e}")
            raise HTTPException(400, f"解密失败: {e}")
    else:
        event_data = body

    event_type = (
        event_data.get("type", "")
        or event_data.get("header", {}).get("event_type", "")
    )
    event = event_data.get("event", {})

    logger.info(f"收到事件: type={event_type}")

    if event_type == "im.message.receive_v1":
        msg = event.get("message", {})
        msg_type = msg.get("message_type", "")
        message_id = msg.get("message_id", "")
        sender_info = event.get("sender", {}).get("sender_id", {})
        sender_id = (
            sender_info.get("open_id", "")
            or sender_info.get("user_id", "")
            or sender_info.get("union_id", "")
        )

        if not sender_id:
            return JSONResponse({"code": 0})

        # ========== 语音 / 音频文件消息 → 语音转文字 ==========
        if msg_type in ("audio", "file"):
            try:
                content = json.loads(msg.get("content", "{}"))
                file_key = content.get("file_key", "")
            except json.JSONDecodeError:
                content = {}
                file_key = ""

            if msg_type == "audio" and file_key:
                # 飞书语音消息：飞书事件本身已附带 speech_to_text，优先直接返回
                speech_text = content.get("speech_to_text", "")
                if speech_text:
                    logger.info(f"直接使用飞书语音识别结果: {speech_text[:100]}")
                    await feishu.send_text(
                        sender_id, f"🎙️ 语音转文字结果：\n\n{speech_text}"
                    )
                    return JSONResponse({"code": 0})
                # 如果没有 speech_to_text，降级到 Drive + 妙记
                file_name = f"voice_{message_id}.amr"
                await feishu.send_text(sender_id, "收到语音消息，正在转写文字…")
                asyncio.create_task(
                    transcribe_audio(message_id, file_key, file_name, sender_id)
                )
                return JSONResponse({"code": 0})

            elif msg_type == "file" and file_key:
                # 音频/视频文件
                file_name = content.get("file_name", f"audio_{message_id}")
                ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
                audio_exts = {
                    "wav", "mp3", "m4a", "aac", "ogg", "wma", "amr",
                    "avi", "mp4", "mov", "flv", "m4v", "wmv", "mpeg",
                }
                if ext in audio_exts:
                    await feishu.send_text(sender_id, f"收到音频文件 {file_name}，正在转写文字…")
                    asyncio.create_task(
                        transcribe_audio(message_id, file_key, file_name, sender_id)
                    )
                    return JSONResponse({"code": 0})

        # ========== 文本消息 → DeepSeek 对话 ==========
        text = ""
        if msg_type == "text":
            text = json.loads(msg.get("content", "{}")).get("text", "")

        if text:
            logger.info(f"收到文本 [{sender_id}]: {text[:200]}")
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
    return {
        "status": "ok",
        "app_id": FEISHU_APP_ID[:8] + "***" if FEISHU_APP_ID else "未配置",
        "user_auth": bool(_user_token_cache.get("refresh_token")),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
