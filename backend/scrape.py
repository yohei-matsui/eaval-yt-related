"""YouTube 関連動画抽出 - GitHub Actions 用スタンドアロンスクリプト"""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def clean_video_url(href: str) -> str | None:
    parsed = urlparse(href)
    if "/shorts/" in parsed.path or parsed.path != "/watch":
        return None
    qs = parse_qs(parsed.query)
    if "v" not in qs:
        return None
    return f"https://www.youtube.com/watch?v={qs['v'][0]}"


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

            # 必要件数に達するまでスクロールして追加読み込み（最大5回）
            for _ in range(5):
                current = page.locator(
                    "ytd-watch-next-secondary-results-renderer yt-lockup-view-model"
                ).count()
                if current >= max_count:
                    break
                page.evaluate("window.scrollBy(0, 1200)")
                page.wait_for_timeout(1500)

            source_meta: dict = page.evaluate("""() => {
                const title = document.querySelector('h1.ytd-watch-metadata yt-formatted-string')?.innerText
                    || document.title.replace(' - YouTube', '');
                const channel = document.querySelector('#top-row ytd-channel-name #text a, #owner #text a')?.innerText || '';
                const views = document.querySelector('.view-count, #info .view-count')?.innerText || '';
                const published = document.querySelector('#info-strings yt-formatted-string, #above-the-fold #info-strings yt-formatted-string')?.innerText || '';
                return {title, channel, views, published};
            }""")
            source_meta["url"] = url

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

            raw_items: list = page.eval_on_selector_all(
                "ytd-watch-next-secondary-results-renderer yt-lockup-view-model",
                """els => els.map(el => {
                    const links = [...el.querySelectorAll('a[href*="/watch"]')];
                    const titleLink = links.find(a => { const t = a.innerText.trim(); return t && !/^[\\d:]+$/.test(t); });
                    const durLink   = links.find(a => /^[\\d:]+$/.test(a.innerText.trim()));
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
                ch_name  = item.get("channel", "")
                raw_path = ch_url_map.get(ch_name, "")
                ch_href  = ("https://www.youtube.com" + unquote(raw_path)) if raw_path else ""
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


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: scrape.py <url> <count> <run_id>")
        sys.exit(1)

    target_url = sys.argv[1]
    count      = int(sys.argv[2])
    run_id     = sys.argv[3]

    print(f"Scraping: {target_url} (count={count}, run_id={run_id})")

    try:
        data = scrape(target_url, count)
        data["status"] = "done"
    except Exception as e:
        data = {"status": "error", "error": str(e)}

    os.makedirs("results", exist_ok=True)
    out_path = f"results/{run_id}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Saved to {out_path}")
