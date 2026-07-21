import json
import re
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
import httpx
import uvicorn

app = FastAPI(
    title="fast-yt-search",
    description="Python (FastAPI) で動作する高速・軽量な非公式YouTube検索API",
    version="0.2.1"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
}


@app.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "Fast-YT-Search API is running!",
        "usage": "/api/search?q=検索ワード"
    }


@app.get("/api/search")
async def search(
    q: str = Query(..., description="検索キーワード"),
    p: int = Query(1, ge=1, description="ページ番号 (1〜)"),
    n: int = Query(10, ge=1, le=50, description="取得件数 (1〜50)"),
    sort: Optional[str] = Query(
        "relevance", description="並び替え: relevance, date, views, rating"
    ),
    type: Optional[str] = Query(
        "all", description="種類: all, video, channel, playlist"
    ),
):
    """YouTube検索非公式API"""

    sp_param = ""
    if sort == "date":
        sp_param = "&sp=CAI%253D"  # アップロード日順
    elif sort == "views":
        sp_param = "&sp=CAMSAhAB"  # 視聴回数順
    elif sort == "rating":
        sp_param = "&sp=CAESAhAB"  # 評価順

    url = f"https://www.youtube.com/results?search_query={q}{sp_param}"

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=HEADERS)

    if response.status_code != 200:
        raise HTTPException(
            status_code=500, detail="YouTubeからの取得に失敗しました"
        )

    pattern = r"var ytInitialData = ({.*?});</script>"
    match = re.search(pattern, response.text)
    if not match:
        raise HTTPException(
            status_code=500, detail="データのパースに失敗しました"
        )

    data = json.loads(match.group(1))

    try:
        contents = data["contents"]["twoColumnSearchResultsRenderer"][
            "primaryContents"
        ]["sectionListRenderer"]["contents"][0]["itemSectionRenderer"][
            "contents"
        ]
    except KeyError:
        return {"query": q, "page": p, "count": 0, "results": []}

    raw_results = []
    for item in contents:
        if "videoRenderer" in item:
            video = item["videoRenderer"]
            raw_results.append(
                {
                    "type": "video",
                    "id": video.get("videoId"),
                    "url": f"https://www.youtube.com/watch?v={video.get('videoId')}",
                    "title": video.get("title", {})
                    .get("runs", [{}])[0]
                    .get("text"),
                    "channel": video.get("ownerText", {})
                    .get("runs", [{}])[0]
                    .get("text"),
                    "views": video.get("viewCountText", {}).get(
                        "simpleText", "非表示"
                    ),
                    "published": video.get("publishedTimeText", {}).get(
                        "simpleText", ""
                    ),
                    "thumbnail": video.get("thumbnail", {})
                    .get("thumbnails", [{}])[-1]
                    .get("url"),
                }
            )

    start_idx = (p - 1) * n
    end_idx = start_idx + n
    paginated_results = raw_results[start_idx:end_idx]

    return {
        "query": q,
        "page": p,
        "limit": n,
        "total_returned": len(paginated_results),
        "results": paginated_results,
    }


def cli():
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    cli()
