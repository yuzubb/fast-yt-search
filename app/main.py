import os
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

PROXY_URL = os.getenv("PROXY_URL", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (SmartHUB; SMART-TV; U; Linux/SmartTV) AppleWebKit/537.42 (KHTML, like Gecko) Safari/537.42",
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept-Encoding": "gzip, br, deflate",
}

INNERTUBE_URL = "https://www.youtubei.googleapis.com/youtubei/v1/player"
INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

INNERTUBE_CONTEXT = {
    "client": {
        "clientName": "TVHTML5",
        "clientVersion": "7.20260308.00.00",
        "hl": "ja",
        "gl": "JP",
    }
}

THUMB_CONCURRENCY = 8
CACHE_TTL_SEARCH = 60
CACHE_TTL_STREAM = 120

RE_YT_INITIAL_DATA = re.compile(r"var ytInitialData = ({.*?});</script>")
RE_PLAYER_PATTERNS = [
    re.compile(r"ytInitialPlayerResponse\s*=\s*({.*?});(?:var|script)"),
    re.compile(r"var\s+ytInitialPlayerResponse\s*=\s*({.*?});"),
    re.compile(r'"playerResponse":\s*({.*?})\s*,\s*"responseContext"'),
]

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


app = FastAPI(title="fast-yt-search", version="0.9.0", lifespan=lifespan)


async def get_base64_image(url: str, sem: asyncio.Semaphore) -> str:
    if not url:
        return ""
    async with sem:
        try:
            res = await client.get(url, timeout=5.0)
            if res.status_code == 200:
                encoded = base64.b64encode(res.content).decode("utf-8")
                content_type = res.headers.get("content-type", "image/jpeg")
                return f"data:{content_type};base64,{encoded}"
        except Exception:
            pass
    return ""


async def fetch_player_response(video_id: str) -> Dict[str, Any]:
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


def extract_player_response(html: str) -> Dict[str, Any]:
    for pattern in RE_PLAYER_PATTERNS:
        match = pattern.search(html)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                continue
    return {}


def parse_stream_item(fmt: Dict[str, Any], is_adaptive: bool = False) -> Optional[Dict[str, Any]]:
    url = fmt.get("url")

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


@app.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "Fast-YT-Search API is running!",
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
    sort: Optional[str] = Query("relevance", description="並び替え"),
    thumbnails: bool = Query(False, description="サムネイルBase64化"),
):
    cache_key = f"search:{q}:{p}:{n}:{sort}:{thumbnails}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    sp_param = ""
    if sort == "date":
        sp_param = "&sp=CAI%253D"
    elif sort == "views":
        sp_param = "&sp=CAMSAhAB"
    elif sort == "rating":
        sp_param = "&sp=CAESAhAB"

    url = f"https://www.youtube.com/results?search_query={q}{sp_param}"

    response = await client.get(url)
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="YouTubeからの取得に失敗しました")

    match = RE_YT_INITIAL_DATA.search(response.text)
    if not match:
        raise HTTPException(status_code=500, detail="データのパースに失敗しました")

    data = json.loads(match.group(1))
    raw_results = []

    try:
        sections = data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"][
            "sectionListRenderer"
        ]["contents"]
        for section in sections:
            contents = section.get("itemSectionRenderer", {}).get("contents", [])
            for item in contents:
                if "videoRenderer" in item:
                    video = item["videoRenderer"]
                    title_runs = video.get("title", {}).get("runs", [])
                    title = title_runs[0].get("text") if title_runs else "タイトルなし"

                    owner_runs = video.get("ownerText", {}).get("runs", [])
                    channel = owner_runs[0].get("text") if owner_runs else "不明"

                    thumb_url = video.get("thumbnail", {}).get("thumbnails", [{}])[-1].get("url", "")

                    raw_results.append({
                        "type": "video",
                        "id": video.get("videoId"),
                        "url": f"https://www.youtube.com/watch?v={video.get('videoId')}",
                        "title": title,
                        "channel": channel,
                        "views": video.get("viewCountText", {}).get("simpleText", "非表示"),
                        "published": video.get("publishedTimeText", {}).get("simpleText", ""),
                        "thumb_url": thumb_url,
                    })
    except KeyError:
        pass

    start_idx = (p - 1) * n
    end_idx = start_idx + n
    paginated_results = raw_results[start_idx:end_idx]

    if thumbnails:
        sem = asyncio.Semaphore(THUMB_CONCURRENCY)
        thumb_urls = [item.get("thumb_url", "") for item in paginated_results]
        encoded_list = await asyncio.gather(*(get_base64_image(u, sem) for u in thumb_urls))
        for item, encoded in zip(paginated_results, encoded_list):
            item.pop("thumb_url", None)
            item["thumbnail_base64"] = encoded
    else:
        for item in paginated_results:
            item["thumbnail_url"] = item.pop("thumb_url", "")

    result = {
        "query": q,
        "page": p,
        "limit": n,
        "total_returned": len(paginated_results),
        "results": paginated_results,
    }
    cache_set(cache_key, result, CACHE_TTL_SEARCH)
    return result


@app.get("/api/stream/{video_id}")
async def get_stream(video_id: str):
    cache_key = f"stream:{video_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    player_data = await fetch_player_response(video_id)

    if not player_data or player_data.get("playabilityStatus", {}).get("status") != "OK":
        html_res = await client.get(f"https://www.youtube.com/watch?v={video_id}")
        if html_res.status_code == 200:
            player_data = extract_player_response(html_res.text)

    if not player_data:
        raise HTTPException(status_code=404, detail="プレイヤーデータが見つかりませんでした")

    playability = player_data.get("playabilityStatus", {})
    status = playability.get("status")
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
