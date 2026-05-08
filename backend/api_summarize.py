"""AI 视频总结相关 API 路由（独立模块，通过 include_router 挂载）"""

import asyncio
import json
from collections.abc import AsyncIterable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from auth import get_optional_user
from database import check_and_increment_summary, FREE_DAILY_SUMMARY_LIMIT

router = APIRouter(prefix="/api", tags=["AI 总结"])


class SummarizeRequest(BaseModel):
    url: str
    language: str = "zh"
    model: str = ""  # 不传则自动选默认模型


class ChatRequest(BaseModel):
    url: str
    question: str
    subtitle_text: str = ""
    model: str = ""


def _check_summary_permission(user: dict | None):
    """
    检查 AI 总结权限。
    未登录用户：不允许使用。
    免费用户：每日限制次数。
    VIP 用户：无限制。
    返回 (allowed, remaining, message)
    """
    if not user:
        return False, 0, "请先登录后使用 AI 总结功能"

    allowed, remaining = check_and_increment_summary(user["id"])
    if not allowed:
        return False, 0, f"今日免费 AI 总结次数已用完（每日 {FREE_DAILY_SUMMARY_LIMIT} 次），开通 VIP 可无限使用"

    return True, remaining, None


def _create_summarizer(provider: str = ""):
    """根据 provider 创建 VideoSummarizer 实例"""
    from summarizer import VideoSummarizer
    try:
        return VideoSummarizer(provider=provider or None)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))


def _get_extractor():
    """延迟初始化 SubtitleExtractor"""
    from summarizer import SubtitleExtractor
    if not hasattr(_get_extractor, "_instance"):
        _get_extractor._instance = SubtitleExtractor()
    return _get_extractor._instance


@router.get("/models")
async def list_models():
    """返回所有可用的 AI 模型列表"""
    from summarizer import VideoSummarizer
    try:
        available = VideoSummarizer.get_available_providers()
        default = VideoSummarizer.get_default_provider()
        return {"success": True, "data": {"models": available, "default": default}}
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/summarize", response_class=EventSourceResponse)
async def summarize_video(req: SummarizeRequest, user: dict | None = Depends(get_optional_user)) -> AsyncIterable[ServerSentEvent]:
    """
    AI 视频总结（SSE 流式）
    事件类型: subtitle / summary / mindmap / done / error / quota
    """
    allowed, remaining, message = _check_summary_permission(user)
    if not allowed:
        yield ServerSentEvent(
            raw_data=json.dumps({"message": message, "need_login": user is None, "need_vip": user is not None}, ensure_ascii=False),
            event="error",
        )
        return

    try:
        loop = asyncio.get_event_loop()
        extractor = _get_extractor()
        subtitle_data = await loop.run_in_executor(
            None, extractor.extract, req.url
        )

        yield ServerSentEvent(
            raw_data=json.dumps(subtitle_data, ensure_ascii=False),
            event="subtitle",
        )

        if not subtitle_data["has_subtitle"]:
            yield ServerSentEvent(
                raw_data=json.dumps({"message": "该视频没有可用的字幕，无法生成总结"}, ensure_ascii=False),
                event="error",
            )
            return

        full_text = subtitle_data["full_text"]
        summarizer = _create_summarizer(req.model)

        for token in summarizer.summarize_stream(full_text, req.language):
            yield ServerSentEvent(raw_data=json.dumps(token, ensure_ascii=False), event="summary")

        mindmap_md = await loop.run_in_executor(
            None, summarizer.generate_mindmap, full_text, req.language
        )
        yield ServerSentEvent(
            raw_data=json.dumps({"markdown": mindmap_md}, ensure_ascii=False),
            event="mindmap",
        )

        quota_info = {"remaining": remaining, "limit": FREE_DAILY_SUMMARY_LIMIT}
        yield ServerSentEvent(
            raw_data=json.dumps(quota_info, ensure_ascii=False),
            event="quota",
        )

        yield ServerSentEvent(raw_data="[DONE]", event="done")

    except Exception as e:
        err_msg = str(e)
        if "Connection" in err_msg or "Connect" in err_msg or "Timeout" in err_msg:
            err_msg = "大模型 API 网络调用不稳定，请稍后再试"
        yield ServerSentEvent(
            raw_data=json.dumps({"message": f"总结失败: {err_msg}"}, ensure_ascii=False),
            event="error",
        )


@router.post("/chat", response_class=EventSourceResponse)
async def chat_with_video(req: ChatRequest, user: dict | None = Depends(get_optional_user)) -> AsyncIterable[ServerSentEvent]:
    """AI 视频问答（SSE 流式）"""
    try:
        if not req.subtitle_text.strip():
            loop = asyncio.get_event_loop()
            extractor = _get_extractor()
            subtitle_data = await loop.run_in_executor(
                None, extractor.extract, req.url
            )
            if not subtitle_data["has_subtitle"]:
                yield ServerSentEvent(
                    raw_data=json.dumps({"message": "该视频没有可用的字幕，无法回答问题"}, ensure_ascii=False),
                    event="error",
                )
                return
            subtitle_text = subtitle_data["full_text"]
        else:
            subtitle_text = req.subtitle_text

        summarizer = _create_summarizer(req.model)
        for token in summarizer.chat_stream(subtitle_text, req.question):
            yield ServerSentEvent(raw_data=json.dumps(token, ensure_ascii=False), event="answer")

        yield ServerSentEvent(raw_data="[DONE]", event="done")

    except Exception as e:
        err_msg = str(e)
        if "Connection" in err_msg or "Connect" in err_msg or "Timeout" in err_msg:
            err_msg = "大模型 API 网络调用不稳定，请稍后再试"
        yield ServerSentEvent(
            raw_data=json.dumps({"message": f"回答失败: {err_msg}"}, ensure_ascii=False),
            event="error",
        )
