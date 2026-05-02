#!/usr/bin/env python3
"""
搜索缓存更新脚本 — 极致版
浏览器内 fetch() 并发调 AJAX API → cookie 天然有效 + 8 并发秒级完成
"""
import json
import logging
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "public" / "search_cache.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cache_updater")

DAMAI_KEYWORDS = [
    "薛之谦", "周杰伦", "林俊杰", "五月天", "陈奕迅", "邓紫棋",
    "张杰", "华晨宇", "蔡依林", "张学友", "刘德华", "周深",
    "许嵩", "汪苏泷", "徐良", "李荣浩", "毛不易",
    "孙燕姿", "林宥嘉", "赵雷", "凤凰传奇", "刀郎",
    "陶喆", "梁静茹", "张惠妹", "李宗盛", "张信哲",
    "王菲", "刘若英", "李健", "陈粒", "伍佰",
    "任贤齐", "张靓颖", "谭咏麟", "那英",
]

CITIES_RE = re.compile(
    r"北京|上海|广州|深圳|成都|重庆|杭州|武汉|西安|南京|天津|苏州|长沙|郑州|"
    r"沈阳|青岛|大连|东莞|宁波|厦门|福州|合肥|无锡|佛山|昆明|贵阳|南宁|南昌|"
    r"哈尔滨|长春|石家庄|太原|济南|兰州|银川|西宁|拉萨|海口|呼和浩特|乌鲁木齐"
)

TIME_RE = re.compile(r"(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})")


def load_existing() -> list[dict]:
    if CACHE_PATH.exists():
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def merge(existing: list[dict], new: list[dict]) -> list[dict]:
    seen = set()
    merged = []
    for item in existing:
        key = f"{item.get('platform','')}_{item.get('item_id','')}"
        if key not in seen:
            seen.add(key)
            merged.append(item)
    for item in new:
        key = f"{item.get('platform','')}_{item.get('item_id','')}"
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def search_all_fast(keywords: list[str]) -> list[dict]:
    """
    浏览器内 fetch() 并发调用 searchajax API。
    cookie 是浏览器自带的（含 x5sec），不会被拦截。
    8 个并发一组，35 个关键词 ~3 秒完成。
    """
    results = []
    browser = None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-setuid-sandbox",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
            )
            page = context.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            """)

            # Step 1: 访问大麦首页 → 获取基础 cookie
            log.info("获取 Cookie...")
            try:
                page.goto("https://www.damai.cn/", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1500)
            except Exception:
                pass

            # Step 2: 访问搜索页 → 获取 search.damai.cn 的子域 cookie
            try:
                page.goto(
                    "https://search.damai.cn/search.htm?keyword=演唱会",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                page.wait_for_timeout(2000)
            except Exception:
                pass

            # Step 3: 在浏览器内用 fetch() 批量并发调 AJAX API
            #         此时 origin = search.damai.cn，fetch 同域无 CORS 问题
            #         cookie 是浏览器自己的，x5sec 天然有效
            log.info(f"并发搜索 {len(keywords)} 个关键词...")
            t1 = time.time()

            raw = page.evaluate(
                """
                async (keywords) => {
                    const all = [];
                    const CONCURRENCY = 8;

                    for (let i = 0; i < keywords.length; i += CONCURRENCY) {
                        const batch = keywords.slice(i, i + CONCURRENCY);
                        const tasks = batch.map(kw =>
                            fetch(
                                `https://search.damai.cn/searchajax.html?keyword=${
                                    encodeURIComponent(kw)
                                }&ctl=1&page=1&ts=${Date.now()}`
                            )
                            .then(r => r.json())
                            .then(data => ({
                                kw: kw,
                                items: (data.pageData?.resultData || []).map(it => ({
                                    id: String(it.id || it.itemId || ''),
                                    name: it.name || it.projectName || '',
                                    city: it.cityName || it.venueCity || '',
                                    time: it.showTime || it.performDate || '',
                                })),
                            }))
                            .catch(() => ({ kw: kw, items: [] }))
                        );
                        const batchResults = await Promise.all(tasks);
                        all.push(...batchResults);
                    }
                    return all;
                }
                """,
                keywords,
            )

            log.info(f"API 请求完成 ({time.time()-t1:.1f}s)，处理结果...")

            # Step 4: 组装结果
            for group in raw:
                kw = group.get("kw", "")
                for item in group.get("items", []):
                    item_id = item.get("id", "")
                    if not item_id:
                        continue
                    name = item.get("name", "")
                    if not name:
                        name = f"{kw} 演出(ID {item_id})"
                    results.append({
                        "name": name,
                        "city": item.get("city", ""),
                        "time": item.get("time", ""),
                        "platform": "damai",
                        "item_id": item_id,
                        "url": f"https://detail.damai.cn/item.htm?id={item_id}",
                        "url_mobile": f"damai://V1/ProjectPage?id={item_id}",
                        "buy_url": f"https://detail.damai.cn/item.htm?id={item_id}",
                        "buy_keywords": ["立即购买", "立即预订", "选座购买"],
                        "sold_keywords": ["缺货登记", "已售罄", "暂无可售"],
                        "mode": "page",
                        "keywords": kw,
                    })

            browser.close()
            browser = None

            found_kw = len(set(r["keywords"] for r in results))
            log.info(f"大麦完成: {len(results)} 个演出, 覆盖 {found_kw}/{len(keywords)} 个关键词")
    except ImportError:
        log.error("Playwright 未安装")
    except Exception as e:
        log.error(f"搜索失败: {e}")
        if browser:
            try:
                browser.close()
            except Exception:
                pass
    return results


def main():
    t0 = time.time()
    log.info("搜索缓存更新 — 极致模式（浏览器内 fetch 并发）")
    existing = load_existing()
    log.info(f"现有缓存: {len(existing)} 条")

    all_new = search_all_fast(DAMAI_KEYWORDS)

    merged = merge(existing, all_new)
    elapsed = time.time() - t0
    log.info(f"新增: {len(all_new)} 条, 合并后: {len(merged)} 条, 总耗时 {elapsed:.0f}s")

    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    count_file = BASE_DIR / "cache_update_count.txt"
    count_file.write_text(str(len(all_new)))
    if all_new:
        log.info(f"::notice::新增 {len(all_new)} 条搜索结果")


if __name__ == "__main__":
    main()
