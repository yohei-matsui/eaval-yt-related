"""YouTube 関連動画抽出ツール - FastAPI バックエンド"""
from __future__ import annotations

import csv
import io
from urllib.parse import parse_qs, unquote, urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from pydantic import BaseModel

app = FastAPI(title="YouTube Related Videos API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── モデル ────────────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str
    max_count: int = 10


# ── ユーティリティ ────────────────────────────────────────────────────────────

def is_valid_youtube_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc in ("www.youtube.com", "youtube.com", "youtu.be") and (
        "/watch" in parsed.path or parsed.netloc == "youtu.be"
    )


def clean_video_url(href: str) -> str | None:
    parsed = urlparse(href)
    if "/shorts/" in parsed.path or parsed.path != "/watch":
        return None
    qs = parse_qs(parsed.query)
    if "v" not in qs:
        return None
    return f"https://www.youtube.com/watch?v={qs['v'][0]}"


# ── スクレイピング ────────────────────────────────────────────────────────────

def scrape(url: str, max_count: int = 10) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ja-JP",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("ytd-watch-next-secondary-results-renderer", timeout=30000)
            page.wait_for_timeout(3000)

            # 元動画メタデータ
            source_meta: dict = page.evaluate("""() => {
                const title = document.querySelector('h1.ytd-watch-metadata yt-formatted-string')?.innerText
                    || document.title.replace(' - YouTube', '');
                const channel = document.querySelector('#top-row ytd-channel-name #text a, #owner #text a')?.innerText || '';
                const views = document.querySelector('.view-count, #info .view-count')?.innerText || '';
                const published = document.querySelector('#info-strings yt-formatted-string, #above-the-fold #info-strings yt-formatted-string')?.innerText || '';
                return {title, channel, views, published};
            }""")
            source_meta["url"] = url

            # チャンネルURL マップ
            ch_url_map: dict = page.evaluate("""() => {
                const map = {};
                function walk(obj) {
                    if (!obj || typeof obj !== 'object') return;
                    if (obj.lockupViewModel) {
                        const lv = obj.lockupViewModel;
                        const meta = lv?.metadata?.lockupMetadataViewModel;
                        const channel = (meta?.metadata?.contentMetadataViewModel
                            ?.metadataRows?.[0]?.metadataParts || [])
                            .map(p => p?.text?.content || '').join('');
                        const ep = meta?.image?.decoratedAvatarViewModel?.rendererContext
                            ?.commandContext?.onTap?.innertubeCommand?.browseEndpoint;
                        const url = ep?.canonicalBaseUrl || (ep?.browseId ? '/channel/' + ep.browseId : '');
                        if (channel && url) map[channel] = url;
                    }
                    if (Array.isArray(obj)) obj.forEach(walk);
                    else Object.values(obj).forEach(walk);
                }
                walk(ytInitialData?.contents?.twoColumnWatchNextResults?.secondaryResults);
                return map;
            }""")

            # 関連動画リスト
            raw_items: list = page.eval_on_selector_all(
                "ytd-watch-next-secondary-results-renderer yt-lockup-view-model",
                """els => els.map(el => {
                    const links = [...el.querySelectorAll('a[href*="/watch"]')];
                    const titleLink = links.find(a => { const t = a.innerText.trim(); return t && !/^[\d:]+$/.test(t); });
                    const durLink   = links.find(a => /^[\d:]+$/.test(a.innerText.trim()));
                    const rows = [...el.querySelectorAll('.ytContentMetadataViewModelMetadataRow')];
                    const channel = rows[0] ? [...rows[0].querySelectorAll('span')].map(s=>s.innerText.trim()).filter(t=>t).join('') : '';
                    const row1 = rows[1] ? [...rows[1].querySelectorAll('span')].map(s=>s.innerText.trim()).filter(t=>t&&t!=='•') : [];
                    return {
                        title: titleLink?.innerText?.trim()||'',
                        videoHref: titleLink?.getAttribute('href')||'',
                        channel, views: row1[0]||'', date: row1[1]||'',
                        duration: durLink?.innerText?.trim()||'',
                    };
                })""",
            )

            seen_ids: set = set()
            related: list = []

            for item in raw_items:
                if len(related) >= max_count:
                    break
                href  = item.get("videoHref", "") or ""
                title = (item.get("title", "") or "").strip()
                if href.startswith("/"):
                    href = "https://www.youtube.com" + href
                clean_url = clean_video_url(href)
                if not clean_url or not title:
                    continue
                vid = parse_qs(urlparse(clean_url).query)["v"][0]
                if vid in seen_ids:
                    continue
                ch_name   = item.get("channel", "")
                raw_path  = ch_url_map.get(ch_name, "")
                ch_href   = ("https://www.youtube.com" + unquote(raw_path)) if raw_path else ""
                seen_ids.add(vid)
                related.append({
                    "rank": len(related) + 1,
                    "title": title,
                    "video_url": clean_url,
                    "channel": ch_name,
                    "channel_url": ch_href,
                    "views": item.get("views", ""),
                    "date": item.get("date", ""),
                    "duration": item.get("duration", ""),
                    "tags": [],
                    "publish_date": "",
                })

            # タグ・公開日取得
            tag_page = context.new_page()
            for item in related:
                try:
                    tag_page.goto(item["video_url"], wait_until="domcontentloaded", timeout=20000)
                    result = tag_page.evaluate("""() => {
                        const mf = window.ytInitialPlayerResponse?.microformat?.playerMicroformatRenderer;
                        const rawDate = mf?.publishDate || mf?.uploadDate || '';
                        let publishDate = '';
                        if (rawDate) {
                            try {
                                const d = new Date(rawDate);
                                publishDate = d.toLocaleDateString('ja-JP', {year:'numeric',month:'long',day:'numeric'});
                            } catch(e) { publishDate = rawDate; }
                        }
                        let tags = [];
                        try {
                            const kw = window.ytInitialPlayerResponse?.videoDetails?.keywords;
                            if (Array.isArray(kw) && kw.length) tags = kw;
                        } catch(e) {}
                        return { tags, publishDate };
                    }""")
                    item["tags"]         = result.get("tags", [])
                    item["publish_date"] = result.get("publishDate", "")
                except Exception:
                    pass
            tag_page.close()

            return {"source": source_meta, "related": related}

        except PlaywrightTimeoutError as e:
            raise RuntimeError(f"タイムアウト: {e}") from e
        finally:
            context.close()
            browser.close()


# ── エンドポイント ────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/api/scrape")
def api_scrape(req: ScrapeRequest):
    url = req.url.strip()
    if not is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="無効なYouTube URLです")
    max_count = max(1, min(req.max_count, 50))
    try:
        data = scrape(url, max_count)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"スクレイピングエラー: {e}")
    return data


@app.post("/api/csv")
def api_csv(req: dict):
    from fastapi.responses import Response
    source  = req.get("source", {})
    related = req.get("related", [])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["【元動画情報】"])
    for label, key in [("タイトル","title"),("チャンネル","channel"),
                       ("再生回数","views"),("公開日","published"),("URL","url")]:
        w.writerow([label, source.get(key, "")])
    w.writerow([])
    w.writerow(["【関連動画リスト】"])
    w.writerow(["順位","タイトル","動画URL","チャンネル名","チャンネルURL","投稿（相対）","投稿日","再生回数","動画尺","タグ"])
    for item in related:
        w.writerow([
            item.get("rank",""), item.get("title",""), item.get("video_url",""),
            item.get("channel",""), item.get("channel_url",""),
            item.get("date",""), item.get("publish_date",""),
            item.get("views",""), item.get("duration",""),
            ", ".join(item.get("tags",[])),
        ])
    csv_bytes = ("﻿" + buf.getvalue()).encode("utf-8")
    return Response(content=csv_bytes, media_type="text/csv; charset=utf-8-sig",
                    headers={"Content-Disposition": "attachment; filename=youtube_related_videos.csv"})
