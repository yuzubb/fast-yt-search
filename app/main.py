import asyncio
import json
import re
import time
import base64
from typing import Optional, Dict, Any, Tuple
from urllib.parse import parse_qs, unquote
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
import httpx
import uvicorn

# ---------------------------------------------------------------------------
# 設定（TVHTML5クライアント用に整合性を統一）
# ---------------------------------------------------------------------------

# TVクローン用のUser-Agent（InnerTubeのTVHTML5と一致させる）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (SmartHUB; SMART-TV; U; Linux/SmartTV) AppleWebKit/537.42 (KHTML, like Gecko) Safari/537.42",
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept-Encoding": "gzip, br, deflate",
}

INNERTUBE_URL = "https://www.youtubei.googleapis.com/youtubei/v1/player"
INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

# TVHTML5 クライアント（制限やPoToken要求を受けにくく直URLが取りやすい）
INNERTUBE_CONTEXT = {
    "client": {
        "clientName": "TVHTML5",
        "clientVersion": "7.20260308.00.00",
        "hl": "ja",
        "gl": "JP",
    }
}

CACHE_TTL_STREAM = 120
_cache: Dict[str, Tuple[float, Any]] = {}


def cache_get(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if time.monotonic() > expires_at:
        _cache.pop(key, None)
        return None
    return value


def cache_set(key: str, value: Any, ttl: float) -> None:
    _cache[key] = (time.monotonic() + ttl, value)


client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    try:
        yield
    finally:
        if client:
            await client.aclose()


app = FastAPI(title="fast-yt-search", version="0.9.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------

async def fetch_player_response(video_id: str) -> Dict[str, Any]:
    """TVHTML5コンテキストでInnerTube APIをコール"""
    payload = {
        "context": INNERTUBE_CONTEXT,
        "videoId": video_id,
    }
    try:
        res = await client.post(
            f"{INNERTUBE_URL}?key={INNERTUBE_KEY}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
        if res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return {}


def parse_stream_item(fmt: Dict[str, Any], is_adaptive: bool = False) -> Optional[Dict[str, Any]]:
    """直URLおよび signatureCipher からのURL抽出処理"""
    url = fmt.get("url")

    # urlがなく signatureCipher / cipher がある場合の緊急抽出
    if not url:
        cipher_str = fmt.get("signatureCipher") or fmt.get("cipher")
        if cipher_str:
            cipher_data = parse_qs(cipher_str)
            raw_url = cipher_data.get("url", [None])[0]
            if raw_url:
                url = unquote(raw_url)

    if not url:
        return None

    mime_raw = fmt.get("mimeType", "")
    mime_parts = mime_raw.split(";")
    container = mime_parts[0].strip() if mime_parts else ""
    codecs = mime_parts[1].replace('codecs="', '').replace('"', '').strip() if len(mime_parts) > 1 else ""

    is_audio = "audio" in container
    is_video = "video" in container or not is_audio

    return {
        "itag": fmt.get("itag"),
        "quality": fmt.get("qualityLabel") or fmt.get("audioQuality") or fmt.get("quality", "unknown"),
        "container": container,
        "codecs": codecs,
        "bitrate": fmt.get("bitrate"),
        "width": fmt.get("width"),
        "height": fmt.get("height"),
        "fps": fmt.get("fps"),
        "has_audio": not is_adaptive or is_audio,
        "has_video": not is_adaptive or is_video,
        "content_length": fmt.get("contentLength"),
        "url": url,
    }


# ---------------------------------------------------------------------------
# エンドポイント
# ---------------------------------------------------------------------------

@app.get("/api/stream/{video_id}")
async def get_stream(video_id: str):
    cache_key = f"stream:{video_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    player_data = await fetch_player_response(video_id)

    if not player_data:
        raise HTTPException(status_code=404, detail="プレイヤーデータが取得できませんでした")

    playability = player_data.get("playabilityStatus", {})
    status = playability.get("status")
    
    # OK以外（LOGIN_REQUIRED, UNPLAYABLE等）の場合はエラー内容を返却
    if status and status != "OK":
        reason = playability.get("reason") or status
        raise HTTPException(status_code=422, detail=f"再生不可 ({status}): {reason}")

    video_details = player_data.get("videoDetails", {})
    streaming_data = player_data.get("streamingData", {})

    combined_streams = []
    video_only_streams = []
    audio_only_streams = []

    for fmt in streaming_data.get("formats", []):
        parsed = parse_stream_item(fmt, is_adaptive=False)
        if parsed:
            combined_streams.append(parsed)

    for fmt in streaming_data.get("adaptiveFormats", []):
        parsed = parse_stream_item(fmt, is_adaptive=True)
        if parsed:
            if parsed["has_audio"] and not parsed["has_video"]:
                audio_only_streams.append(parsed)
            elif parsed["has_video"] and not parsed["has_audio"]:
                video_only_streams.append(parsed)

    total_found = len(combined_streams) + len(video_only_streams) + len(audio_only_streams)

    result = {
        "video_id": video_id,
        "title": video_details.get("title"),
        "channel": video_details.get("author"),
        "length_seconds": video_details.get("lengthSeconds"),
        "is_live": video_details.get("isLiveContent", False),
        "total_streams_found": total_found,
        "streams": {
            "combined": combined_streams,
            "video_only": video_only_streams,
            "audio_only": audio_only_streams,
        },
    }

    cache_set(cache_key, result, CACHE_TTL_STREAM)
    return result


def cli():
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    cli()
