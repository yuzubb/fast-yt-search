import json
import re
import base64
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
import httpx
import uvicorn

app = FastAPI(
    title="fast-yt-search",
    version="0.4.0"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
}

async def get_base64_image(client: httpx.AsyncClient, url: str) -> str:
    """画像をダウンロードしてBase64文字列に変換するヘルパー関数"""
    try:
        res = await client.get(url, timeout=5.0)
        if res.status_code == 200:
            encoded = base64.b64encode(res.content).decode("utf-8")
            content_type = res.headers.get("content-type", "image/jpeg")
            return f"data:{content_type};base64,{encoded}"
    except Exception:
        pass
    return ""


@app.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "Fast-YT-Search API is running!",
        "usage": {
            "search": "/api/search?q=検索ワード",
            "stream": "/api/stream/{video_id}"
        }
    }


@app.get("/api/search")
async def search(
    q: str = Query(..., description="検索キーワード"),
    p: int = Query(1, ge=1, description="ページ番号"),
    n: int = Query(10, ge=1, le=50, description="取得件数"),
    sort: Optional[str] = Query("relevance", description="並び替え: relevance, date, views, rating"),
):
    """YouTubeの動画検索エンドポイント（サムネイルBase64化）"""
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

        pattern = r"var ytInitialData = ({.*?});</script>"
        match = re.search(pattern, response.text)
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

        # 必要な件数分だけ切り出し
        start_idx = (p - 1) * n
        end_idx = start_idx + n
        paginated_results = raw_results[start_idx:end_idx]

        # 切り出したデータのみサムネイルをBase64化
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
async def get_stream(video_id: str):
    """動画IDから直リンク（ストリームURL）を取得するエンドポイント"""
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
            streaming_data = player_data.get("streamingData", {})
            
            streams = []
            
            # 音声＋映像フォーマット
            for fmt in streaming_data.get("formats", []):
                if "url" in fmt:
                    streams.append({
                        "quality": fmt.get("qualityLabel", "unknown"),
                        "mime_type": fmt.get("mimeType", "").split(";")[0],
                        "has_audio": True,
                        "has_video": True,
                        "url": fmt["url"]
                    })

            # 音声のみ / 映像のみフォーマット
            for fmt in streaming_data.get("adaptiveFormats", []):
                if "url" in fmt:
                    is_audio = "audio" in fmt.get("mimeType", "")
                    streams.append({
                        "quality": fmt.get("audioQuality", "audio") if is_audio else fmt.get("qualityLabel", "unknown"),
                        "mime_type": fmt.get("mimeType", "").split(";")[0],
                        "has_audio": is_audio,
                        "has_video": not is_audio,
                        "url": fmt["url"]
                    })

            if not streams:
                return {
                    "video_id": video_id,
                    "count": 0,
                    "streams": [],
                    "message": "Cipher（暗号化）された動画のため、URLを直接抽出できませんでした"
                }

            return {
                "video_id": video_id,
                "count": len(streams),
                "streams": streams
            }

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"解析エラー: {str(e)}")


def cli():
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    cli()
