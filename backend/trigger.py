"""GitHub Actions トリガー用 軽量API"""
import os
import random
import string
import time

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "yohei-matsui")
GITHUB_REPO  = os.environ.get("GITHUB_REPO",  "eaval-yt-related")
WORKFLOW_FILE = "scrape.yml"


class TriggerRequest(BaseModel):
    url: str
    count: int = 10


@app.get("/")
@app.head("/")
def health():
    return {"status": "ok"}


@app.post("/trigger")
def trigger(req: TriggerRequest):
    if not GITHUB_TOKEN:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN が設定されていません")

    # ユニークなrun_idを生成
    run_id = str(int(time.time())) + ''.join(random.choices(string.ascii_lowercase, k=4))

    res = httpx.post(
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        },
        json={
            "ref": "main",
            "inputs": {
                "url": req.url,
                "count": str(req.count),
                "run_id": run_id,
            },
        },
        timeout=15,
    )

    if res.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail=f"ワークフロー起動失敗: {res.text}")

    return {"run_id": run_id}
