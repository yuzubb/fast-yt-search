import json
import re
import base64
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query
import httpx
import uvicorn

app = FastAPI(
    title="fast-yt-search",
    description="YouTubeの検索およびストリーム情報を詳細解析する非公式API",
    version="0.5.0"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
}

# --- ヘルパー関数群 ---

async def get_base64_image(client: httpx.AsyncClient, url: str) -> str:
    """画像をダウンロードしてBase64文字列に変換"""
    try:
        res = await client.get(url, timeout=5.0)
        if res.status_code == 200:
            encoded = base64.b64encode(res.content).decode("utf-8")
            content_type = res.headers.get("content-type", "image/jpeg")
            return f"data:{content_type};base64,{encoded}"
    except Exception:
        pass
    return ""


def parse_stream_format(fmt: Dict[str, Any], is_adaptive: bool = False) -> Optional[Dict[str, Any]]:
    """個々のストリームフォーマットを解析して整形する"""
    url = fmt.get("url")
    if not url:
        # 暗号化（cipher / signatureCipher）されている場合はスキップ
        return None

    mime_type_raw = fmt.get("mimeType", "")
    mime_parts = mime_type_raw.split(";")
    container = mime_parts[0] if len(mime_parts) > 0 else ""
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
        "url": url
    }


# --- エンドポイント定義 ---

@app.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "Fast-YT-Search API is running!",
        "endpoints": {
            "search": "/api/search?q=キーワード",
            "stream_analysis": "/api/stream/{video_id}"
        }
    }


@app.get("/api/search")
async def search(
    q: str = Query(..., description="検索キーワード"),
    p: int = Query(1, ge=1, description="ページ番号"),
    n: int = Query(10, ge=1, le=50, description="取得件数"),
    sort: Optional[str] = Query("relevance", description="並び替え: relevance, date, views, rating"),
):
    """YouTube動画の検索結果取得 & サムネイルBase64化"""
    sp_param = ""
    if sort == "date":
        sp_param = "&sp=CAI%253D"
    elif sort == "views":
        sp_param = "&sp=CAMSAhAB"
    elif sort == "rating":
        sp_param = "&sp=CAESAhAB"

    url = f"https://www.youtube.com/results?search_query={q}{sp_param}"

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        response = await client.get(url)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail="YouTubeからの取得に失敗しました")

        match = re.search(r"var ytInitialData = ({.*?});</script>", response.text)
        if not match:
            raise HTTPException(status_code=500, detail="データのパースに失敗しました")

        data = json.loads(match.group(1))
        raw_results = []

        try:
            sections = data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]["sectionListRenderer"]["contents"]
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
                            "thumb_url": thumb_url
                        })
        except KeyError:
            pass

        start_idx = (p - 1) * n
        end_idx = start_idx + n
        paginated_results = raw_results[start_idx:end_idx]

        for item in paginated_results:
            thumb_url = item.pop("thumb_url", "")
            item["thumbnail_base64"] = await get_base64_image(client, thumb_url) if thumb_url else ""

    return {
        "query": q,
        "page": p,
        "limit": n,
        "total_returned": len(paginated_results),
        "results": paginated_results,
    }


@app.get("/api/stream/{video_id}")
async def analyze_stream(video_id: str):
    """動画IDからストリームURLとメタデータを高度に解析して出力"""
    url = f"https://www.youtube.com/watch?v={video_id}"

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        res = await client.get(url)
        if res.status_code != 200:
            raise HTTPException(status_code=500, detail="YouTubeページの取得に失敗しました")

        match = re.search(r"ytInitialPlayerResponse\s*=\s*({.*?});(?:var|script)", res.text)
        if not match:
            raise HTTPException(status_code=404, detail="プレイヤーデータが見つかりませんでした")

        try:
            player_data = json.loads(match.group(1))
            video_details = player_data.get("videoDetails", {})
            streaming_data = player_data.get("streamingData", {})

            # 1. 音声＋映像統合フォーマットの解析
            combined_streams = []
            for fmt in streaming_data.get("formats", []):
                parsed = parse_stream_format(fmt, is_adaptive=False)
                if parsed:
                    combined_streams.append(parsed)

            # 2. 映像のみ / 音声のみセパレートフォーマットの解析
            video_only_streams = []
            audio_only_streams = []
            for fmt in streaming_data.get("adaptiveFormats", []):
                parsed = parse_stream_format(fmt, is_adaptive=True)
                if parsed:
                    if parsed["has_audio"] and not parsed["has_video"]:
                        audio_only_streams.append(parsed)
                    elif parsed["has_video"] and not parsed["has_audio"]:
                        video_only_streams.append(parsed)

            total_count = len(combined_streams) + len(video_only_streams) + len(audio_only_streams)

            return {
                "video_id": video_id,
                "title": video_details.get("title"),
                "channel": video_details.get("author"),
                "length_seconds": video_details.get("lengthSeconds"),
                "is_live": video_details.get("isLiveContent", False),
                "total_streams_found": total_count,
                "streams": {
                    "combined": combined_streams,       # 音声＋映像つき
                    "video_only": video_only_streams,   # 高画質映像のみ
                    "audio_only": audio_only_streams    # 音声のみ
                }
            }

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"解析エラー: {str(e)}")


def cli():
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    cli()
