import uvicorn
from fastapi import FastAPI, Query
import httpx
import re
import json

app = FastAPI(
    title="fast-yt-search",
    description="Python (FastAPI) で動作する高速・軽量な非公式YouTube検索API",
    version="0.2.0"
)

@app.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "Fast-YT-Search API is running!",
        "usage": "/search?q=検索ワード"
    }

@app.get("/search")
async def search_youtube(q: str = Query(..., description="検索キーワード")):
    url = f"https://www.youtube.com/results?search_query={q}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        
    if response.status_code != 200:
        return {"error": "Failed to fetch YouTube page", "status_code": response.status_code}

    # 初期データのパース処理例
    match = re.search(r"var ytInitialData = ({.*?});</script>", response.text)
    if not match:
        return {"error": "ytInitialData not found"}

    try:
        data = json.loads(match.group(1))
        # 簡易的にレスポンスデータを返す
        return {
            "query": q,
            "raw_data_summary": "Data successfully extracted"
        }
    except Exception as e:
        return {"error": f"Failed to parse JSON: {str(e)}"}


def cli():
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    cli()
