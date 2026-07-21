import os
import asyncio
import base64
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
import httpx
import uvicorn
import yt_dlp

PROXY_URL = os.getenv("PROXY_URL", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
}

client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
    client_kwargs = {
        "headers": HEADERS,
        "follow_redirects": True,
        "limits": limits,
        "timeout": httpx.Timeout(10.0, connect=5.0),
    }
    if PROXY_URL:
        client_kwargs["proxy"] = PROXY_URL

    client = httpx.AsyncClient(**client_kwargs)
    try:
        yield
    finally:
        if client:
            await client.aclose()


app = FastAPI(title="fast-yt-search", version="1.0.0", lifespan=lifespan)


def extract_with_ytdlp(url: str) -> Dict[str, Any]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    if PROXY_URL:
        ydl_opts["proxy"] = PROXY_URL

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


@app.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "Fast-YT-Search API (yt-dlp + Proxy) is running!",
        "endpoints": {
            "search": "/api/search?q=キーワード",
            "stream": "/api/stream/{video_id}",
        },
    }


@app.get("/api/search")
async def search(
    q: str = Query(..., description="検索キーワード"),
    p: int = Query(1, ge=1, description="ページ番号"),
    n: int = Query(10, ge=1, le=50, description="取得件数"),
):
    target_url = f"ytsearch{n * p}:{q}"

    try:
        info = await asyncio.to_thread(extract_with_ytdlp, target_url)
        entries = info.get("entries", [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"検索エラー: {str(e)}")

    start_idx = (p - 1) * n
    end_idx = start_idx + n
    paginated = entries[start_idx:end_idx]

    results = []
    for entry in paginated:
        results.append({
            "type": "video",
            "id": entry.get("id"),
            "url": entry.get("webpage_url") or f"https://www.youtube.com/watch?v={entry.get('id')}",
            "title": entry.get("title"),
            "channel": entry.get("uploader") or entry.get("channel"),
            "views": entry.get("view_count"),
            "thumbnail_url": entry.get("thumbnail"),
        })

    return {
        "query": q,
        "page": p,
        "limit": n,
        "total_returned": len(results),
        "results": results,
    }


@app.get("/api/stream/{video_id}")
async def get_stream(video_id: str):
    target_url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        info = await asyncio.to_thread(extract_with_ytdlp, target_url)

        formats = info.get("formats", [])
        combined_streams = []
        video_only_streams = []
        audio_only_streams = []

        for fmt in formats:
            stream_url = fmt.get("url")
            if not stream_url:
                continue

            vcodec = fmt.get("vcodec", "none")
            acodec = fmt.get("acodec", "none")
            has_video = vcodec != "none"
            has_audio = acodec != "none"

            stream_info = {
                "format_id": fmt.get("format_id"),
                "ext": fmt.get("ext"),
                "quality": fmt.get("format_note") or fmt.get("resolution") or f"{fmt.get('height', 'unknown')}p",
                "resolution": fmt.get("resolution"),
                "fps": fmt.get("fps"),
                "vcodec": vcodec,
                "acodec": acodec,
                "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
                "url": stream_url,
            }

            if has_video and has_audio:
                combined_streams.append(stream_info)
            elif has_video and not has_audio:
                video_only_streams.append(stream_info)
            elif has_audio and not has_video:
                audio_only_streams.append(stream_info)

        return {
            "video_id": video_id,
            "title": info.get("title"),
            "channel": info.get("uploader"),
            "duration": info.get("duration"),
            "total_streams_found": len(combined_streams) + len(video_only_streams) + len(audio_only_streams),
            "streams": {
                "combined": combined_streams,
                "video_only": video_only_streams,
                "audio_only": audio_only_streams,
            },
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp解析エラー: {str(e)}")


def cli():
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    cli()
