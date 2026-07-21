import asyncio
import json
import re
import time
import base64
from typing import Optional, Dict, Any, Tuple
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
import httpx
import uvicorn

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    # 圧縮を受け入れて転送量を減らす（httpxは自動でデコードしてくれる）
    "Accept-Encoding": "gzip, br, deflate",
}

THUMB_CONCURRENCY = 8          # サムネイル同時ダウンロード数の上限
CACHE_TTL_SEARCH = 60          # 検索結果キャッシュの有効秒数
CACHE_TTL_STREAM = 120         # ストリーム情報キャッシュの有効秒数

# 正規表現は起動時に1回だけコンパイル
RE_YT_INITIAL_DATA = re.compile(r"var ytInitialData = ({.*?});</script>")
RE_PLAYER_PATTERNS = [
    re.compile(r"ytInitialPlayerResponse\s*=\s*({.*?});(?:var|script)"),
    re.compile(r"var\s+ytInitialPlayerResponse\s*=\s*({.*?});"),
    re.compile(r'"playerResponse":\s*({.*?})\s*,\s*"responseContext"'),
]

# ---------------------------------------------------------------------------
# 共有HTTPクライアント（コネクションプールを使い回す）
# ---------------------------------------------------------------------------

client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
    client = httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        http2=True,
        limits=limits,
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    yield
    await client.aclose()


app = FastAPI(title="fast-yt-search", version="0.7.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# 超簡易TTLキャッシュ（プロセス内メモリ、依存ライブラリ不要）
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------

async def get_base64_image(url: str, sem: asyncio.Semaphore) -> str:
    """画像をダウンロードしてBase64形式に変換（同時実行数を制限）"""
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


def extract_player_response(html: str) -> Dict[str, Any]:
    """HTMLから複数のパターンを走査して playerResponse の JSON を抽出（事前コンパイル済み正規表現を使用）"""
    for pattern in RE_PLAYER_PATTERNS:
        match = pattern.search(html)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                continue
    return {}


def parse_stream_item(fmt: Dict[str, Any], is_adaptive: bool = False) -> Optional[Dict[str, Any]]:
    """個々のストリームフォーマットを解析（暗号化されていない直URLのみ取得）"""
    url = fmt.get("url")
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

@app.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "Fast-YT-Search API (Pure Python) is running!",
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
    thumbnails: bool = Query(
        False, description="サムネイルをBase64で埋め込むか（重いのでデフォルトOFF）"
    ),
):
    """YouTube動画の検索結果取得 & （任意で）サムネイルBase64化"""
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

                    raw_results.append(
                        {
                            "type": "video",
                            "id": video.get("videoId"),
                            "url": f"https://www.youtube.com/watch?v={video.get('videoId')}",
                            "title": title,
                            "channel": channel,
                            "views": video.get("viewCountText", {}).get("simpleText", "非表示"),
                            "published": video.get("publishedTimeText", {}).get("simpleText", ""),
                            "thumb_url": thumb_url,
                        }
                    )
    except KeyError:
        pass

    start_idx = (p - 1) * n
    end_idx = start_idx + n
    paginated_results = raw_results[start_idx:end_idx]

    if thumbnails:
        # 逐次awaitではなく、Semaphoreで制限しつつ並列ダウンロード
        sem = asyncio.Semaphore(THUMB_CONCURRENCY)
        thumb_urls = [item.get("thumb_url", "") for item in paginated_results]
        encoded_list = await asyncio.gather(
            *(get_base64_image(u, sem) for u in thumb_urls)
        )
        for item, encoded in zip(paginated_results, encoded_list):
            item.pop("thumb_url", None)
            item["thumbnail_base64"] = encoded
    else:
        # デフォルトは元のサムネイルURLをそのまま返す（変換コストゼロ）
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
    """自律解析でストリームURL（直リンク）を取得するエンドポイント"""
    cache_key = f"stream:{video_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"https://www.youtube.com/watch?v={video_id}"

    res = await client.get(url)
    if res.status_code != 200:
        raise HTTPException(status_code=500, detail="YouTubeページの取得に失敗しました")

    player_data = extract_player_response(res.text)
    if not player_data:
        raise HTTPException(status_code=404, detail="プレイヤーデータが見つかりませんでした")

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

    # ストリームURLには有効期限があるため、キャッシュTTLは短めに
    cache_set(cache_key, result, CACHE_TTL_STREAM)
    return result


def cli():
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, workers=4)


if __name__ == "__main__":
    cli()
