import os
import time
import asyncio
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
import httpx
import yt_dlp
import uvicorn

PROXY_URL = os.getenv("PROXY_URL", "")
COOKIES_FILE = os.getenv("YT_COOKIES_FILE", "")

# --- innertube (YouTube内部API) 用の定数 ---
INNERTUBE_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
INNERTUBE_URL = f"https://www.youtube.com/youtubei/v1/search?key={INNERTUBE_API_KEY}"
INNERTUBE_CONTEXT = {
    "client": {
        "clientName": "WEB",
        "clientVersion": "2.20260715.00.00",
        "hl": "ja",
        "gl": "JP",
    }
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Content-Type": "application/json",
    "X-YouTube-Client-Name": "1",
    "X-YouTube-Client-Version": "2.20260715.00.00",
}

client: Optional[httpx.AsyncClient] = None

# --- 簡易TTLキャッシュ ---
CACHE_TTL = int(os.getenv("SEARCH_CACHE_TTL", "300"))  # 検索結果キャッシュ(秒)
STREAM_CACHE_TTL = int(os.getenv("STREAM_CACHE_TTL", "1800"))  # ストリームURLキャッシュ(秒)

_search_cache: Dict[str, Dict[str, Any]] = {}
_stream_cache: Dict[str, Dict[str, Any]] = {}


def _cache_get(store: Dict[str, Dict[str, Any]], key: str, ttl: int):
    entry = store.get(key)
    if not entry:
        return None
    if time.time() - entry["ts"] > ttl:
        store.pop(key, None)
        return None
    return entry["data"]


def _cache_set(store: Dict[str, Dict[str, Any]], key: str, data: Any):
    store[key] = {"data": data, "ts": time.time()}


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


app = FastAPI(title="fast-yt-search", version="1.1.0", lifespan=lifespan)


# ================= 検索 (innertube API + キャッシュ) =================

async def fetch_search_json(query: str) -> Dict[str, Any]:
    payload = {
        "context": INNERTUBE_CONTEXT,
        "query": query,
    }
    resp = await client.post(INNERTUBE_URL, json=payload)
    resp.raise_for_status()
    return resp.json()


def parse_search_entries(data: Dict[str, Any]) -> list:
    results = []
    try:
        contents = (
            data["contents"]["twoColumnSearchResultsRenderer"]
            ["primaryContents"]["sectionListRenderer"]["contents"]
        )
    except (KeyError, TypeError):
        return results

    for section in contents:
        items = section.get("itemSectionRenderer", {}).get("contents", [])
        for item in items:
            video = item.get("videoRenderer")
            if not video:
                continue

            video_id = video.get("videoId")
            title = "".join(
                run.get("text", "")
                for run in video.get("title", {}).get("runs", [])
            )
            channel = None
            owner_runs = video.get("ownerText", {}).get("runs", [])
            if owner_runs:
                channel = owner_runs[0].get("text")

            view_count_text = video.get("viewCountText", {}).get("simpleText", "")
            thumbnails = video.get("thumbnail", {}).get("thumbnails", [])
            thumbnail_url = thumbnails[-1]["url"] if thumbnails else None

            results.append({
                "type": "video",
                "id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": title,
                "channel": channel,
                "views": view_count_text,
                "thumbnail_url": thumbnail_url,
            })

    return results


@app.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "Fast-YT-Search API (Innertube + po_token + Cache) is running!",
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
    cache_key = f"{q}::all"

    entries = _cache_get(_search_cache, cache_key, CACHE_TTL)
    if entries is None:
        try:
            data = await fetch_search_json(q)
            entries = parse_search_entries(data)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"検索エラー: {str(e)}")
        _cache_set(_search_cache, cache_key, entries)

    start_idx = (p - 1) * n
    end_idx = start_idx + n
    paginated = entries[start_idx:end_idx]

    return {
        "query": q,
        "page": p,
        "limit": n,
        "total_returned": len(paginated),
        "results": paginated,
    }


# ================= ストリーム取得 (yt-dlp + po_token + キャッシュ) =================

def extract_with_ytdlp(url: str) -> Dict[str, Any]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "extractor_args": {
            "youtube": {
                # po_token provider (bgutil) を使う想定のクライアント構成
                "player_client": ["web", "android"],
            }
        },
    }
    if PROXY_URL:
        ydl_opts["proxy"] = PROXY_URL
    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


@app.get("/api/stream/{video_id}")
async def get_stream(video_id: str):
    cached = _cache_get(_stream_cache, video_id, STREAM_CACHE_TTL)
    if cached is not None:
        return cached

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

        result = {
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

        # ストリームURLには有効期限があるため、TTLは短めに設定すること
        _cache_set(_stream_cache, video_id, result)
        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp解析エラー: {str(e)}")


def cli():
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    cli()
