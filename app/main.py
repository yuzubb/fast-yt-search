import json
import re
from typing import Literal, Optional
from dict2xml import dict2xml
from fastapi import FastAPI, HTTPException, Query, Response
import httpx

app = FastAPI(
    title="YouTube Search Unofficial API",
    description="Python (FastAPI) で動作する高速・軽量な非公式YouTube検索API",
    version="1.0.0",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
}


def build_sp_parameter(sort: str, type_filter: str, duration: str) -> str:
    """YouTube内部のフィルター用 `sp` パラメーターを組み立てる関数"""
    sp_map = {
        ("date", "all", "all"): "CAI%253D",  # アップロード日順
        ("views", "all", "all"): "CAMSAhAB",  # 視聴回数順
        ("rating", "all", "all"): "CAESAhAB",  # 評価順
    }
    key = (sort, type_filter, duration)
    return f"&sp={sp_map[key]}" if key in sp_map else ""


@app.get("/api/search")
async def search(
    q: str = Query(..., description="検索キーワード"),
    p: int = Query(1, ge=1, description="ページ番号 (1〜)"),
    n: int = Query(
        10, ge=1, le=50, description="1ページあたりの取得件数 (1〜50)"
    ),
    format: Literal["json", "xml"] = Query(
        "json", description="レスポンス形式 (json または xml)"
    ),
    sort: Optional[Literal["relevance", "date", "views", "rating"]] = Query(
        "relevance",
        description="並び替え: 関連度, 最新順, 再生数順, 高評価順",
    ),
    type: Optional[Literal["all", "video", "channel", "playlist"]] = Query(
        "all", description="種類: すべて, 動画, チャンネル, 再生リスト"
    ),
    duration: Optional[Literal["all", "short", "medium", "long"]] = Query(
        "all",
        description="動画の長さ: すべて, ショート(<4分), 中(<20分), 長(>20分)",
    ),
):
    """YouTube検索 情報フル取得APIエンドポイント"""

    sp_param = build_sp_parameter(sort, type, duration)
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
            status_code=500, detail="HTMLデータの解析に失敗しました"
        )

    data = json.loads(match.group(1))

    try:
        contents = data["contents"]["twoColumnSearchResultsRenderer"][
            "primaryContents"
        ]["sectionListRenderer"]["contents"][0]["itemSectionRenderer"][
            "contents"
        ]
    except KeyError:
        contents = []

    raw_results = []
    for item in contents:
        # 1. 通常動画 (videoRenderer)
        if "videoRenderer" in item:
            v = item["videoRenderer"]

            badges = [
                b.get("metadataBadgeRenderer", {}).get("label")
                for b in v.get("badges", [])
                if "metadataBadgeRenderer" in b
            ]

            raw_results.append(
                {
                    "type": "video",
                    "id": v.get("videoId"),
                    "url": f"https://www.youtube.com/watch?v={v.get('videoId')}",
                    "title": v.get("title", {})
                    .get("runs", [{}])[0]
                    .get("text"),
                    "description_snippet": "".join(
                        [
                            r.get("text", "")
                            for r in v.get("detailedMetadataSnippets", [{}])[0]
                            .get("snippetText", {})
                            .get("runs", [])
                        ]
                    ),
                    "duration": v.get("lengthText", {}).get(
                        "simpleText", "LIVE/Unknown"
                    ),
                    "published_time": v.get("publishedTimeText", {}).get(
                        "simpleText", ""
                    ),
                    "view_count": v.get("viewCountText", {}).get(
                        "simpleText", "非表示"
                    ),
                    "channel": {
                        "name": v.get("ownerText", {})
                        .get("runs", [{}])[0]
                        .get("text"),
                        "id": v.get("ownerText", {})
                        .get("runs", [{}])[0]
                        .get("navigationEndpoint", {})
                        .get("browseEndpoint", {})
                        .get("browseId"),
                        "url": "https://www.youtube.com"
                        + v.get("ownerText", {})
                        .get("runs", [{}])[0]
                        .get("navigationEndpoint", {})
                        .get("commandMetadata", {})
                        .get("webCommandMetadata", {})
                        .get("url", ""),
                        "icon": v.get("channelThumbnailSupportedRenderers", {})
                        .get("channelThumbnailWithBadgeRenderer", {})
                        .get("thumbnail", {})
                        .get("thumbnails", [{}])[-1]
                        .get("url"),
                        "is_verified": bool(v.get("ownerBadges")),
                    },
                    "thumbnails": v.get("thumbnail", {}).get("thumbnails", []),
                    "badges": badges,
                }
            )

        # 2. チャンネル (channelRenderer)
        elif "channelRenderer" in item:
            c = item["channelRenderer"]
            raw_results.append(
                {
                    "type": "channel",
                    "id": c.get("channelId"),
                    "url": f"https://www.youtube.com/channel/{c.get('channelId')}",
                    "title": c.get("title", {}).get("simpleText"),
                    "subscriber_count": c.get("subscriberCountText", {}).get(
                        "simpleText", ""
                    ),
                    "video_count": c.get("videoCountText", {})
                    .get("runs", [{}])[0]
                    .get("text", ""),
                    "description_snippet": c.get("descriptionSnippet", {})
                    .get("runs", [{}])[0]
                    .get("text", ""),
                    "thumbnails": c.get("thumbnail", {}).get("thumbnails", []),
                }
            )

        # 3. 再生リスト (playlistRenderer)
        elif "playlistRenderer" in item:
            p_item = item["playlistRenderer"]
            raw_results.append(
                {
                    "type": "playlist",
                    "id": p_item.get("playlistId"),
                    "url": f"https://www.youtube.com/playlist?list={p_item.get('playlistId')}",
                    "title": p_item.get("title", {}).get("simpleText"),
                    "video_count": p_item.get("videoCount"),
                    "channel_name": p_item.get("shortBylineText", {})
                    .get("runs", [{}])[0]
                    .get("text"),
                    "thumbnails": p_item.get("thumbnails", [{}])[0].get(
                        "thumbnails", []
                    ),
                }
            )

    # タイプフィルター
    if type != "all":
        raw_results = [r for r in raw_results if r["type"] == type]

    # ページネーション処理
    start_idx = (p - 1) * n
    end_idx = start_idx + n
    paginated_results = raw_results[start_idx:end_idx]

    response_payload = {
        "query": q,
        "page": p,
        "limit": n,
        "total_results_on_page": len(paginated_results),
        "results": paginated_results,
    }

    if format == "xml":
        xml_data = dict2xml(response_payload, wrap="response", indent="  ")
        return Response(content=xml_data, media_type="application/xml")

    return response_payload
