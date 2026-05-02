#!/usr/bin/env python3
"""
搜索缓存更新脚本 — 三阶段自适应
Phase 1: 完整渲染首页+搜索页 → 建立有效 x5sec cookie
Phase 2: 浏览器内 fetch() 12 并发调 AJAX API
Phase 3: fetch 被拦截时自动回退逐页渲染
"""
import json
import logging
import re
import sys
import threading
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
    两阶段策略：
      Phase 1 — 第一个搜索页完整渲染，建立有效 x5sec cookie
      Phase 2 — 利用已建立的 cookie，浏览器内 fetch() 并发调 AJAX
      若 Phase 2 全部被拦，回退到逐页渲染+AJAX拦截模式
    """
    results = []
    browser = None
    try:
        from playwright.sync_api import sync_playwright

        t_start = time.time()
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-setuid-sandbox",
                    "--disable-gpu",
                    "--single-process",
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
            log.info(f"浏览器启动: {time.time()-t_start:.1f}s")

            # ═══ Phase 1: 完整渲染第一个搜索页建立 x5sec ═══
            t_cookie = time.time()
            first_kw = keywords[0]

            # 拦截 AJAX（如果页面触发）
            ajax_data = []
            ajax_got = threading.Event()

            def on_response(response):
                if "searchajax" in response.url and response.status == 200:
                    try:
                        ajax_data.append(response.json())
                        ajax_got.set()
                    except Exception:
                        pass

            page.on("response", on_response)

            # 访问大麦首页
            try:
                page.goto("https://www.damai.cn/", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1500)
            except Exception:
                pass

            # 完整渲染第一个关键词的搜索页
            try:
                page.goto(
                    f"https://search.damai.cn/search.htm?keyword={first_kw}",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                ajax_got.wait(timeout=5)  # 等 AJAX 或超时
                page.wait_for_timeout(500)
            except Exception:
                pass
            log.info(f"x5sec 建立: {time.time()-t_cookie:.1f}s")

            # 提取第一个关键词的结果
            first_results = _extract_from_ajax_or_dom(page, ajax_data, first_kw)
            results.extend(first_results)
            log.info(f"首个 [{first_kw}]: {len(first_results)} 个结果")

            # ═══ Phase 2: 用 fetch() 并发跑剩余关键词 ═══
            remaining = keywords[1:]
            if remaining:
                t_api = time.time()
                raw = page.evaluate(
                    """
                    async (keywords) => {
                        const all = [];
                        const BATCH = 12;
                        for (let i = 0; i < keywords.length; i += BATCH) {
                            const batch = keywords.slice(i, i + BATCH);
                            const tasks = batch.map(kw =>
                                fetch(`https://search.damai.cn/searchajax.html?keyword=${
                                    encodeURIComponent(kw)
                                }&ctl=1&page=1&ts=${Date.now()}`, {
                                    signal: AbortSignal.timeout(6000),
                                    headers: {
                                        'Accept': 'application/json',
                                        'X-Requested-With': 'XMLHttpRequest',
                                    }
                                })
                                .then(r => r.json())
                                .then(data => ({
                                    kw,
                                    items: (data.pageData?.resultData || []).map(it => ({
                                        id: String(it.id || it.itemId || ''),
                                        name: it.name || it.projectName || '',
                                        city: it.cityName || it.venueCity || '',
                                        time: it.showTime || it.performDate || '',
                                    })),
                                }))
                                .catch(() => ({kw, items: []}))
                            );
                            const batchResults = await Promise.all(tasks);
                            all.push(...batchResults);
                        }
                        return all;
                    }
                    """,
                    remaining,
                )

                fetch_count = 0
                for group in raw:
                    kw = group.get("kw", "")
                    for item in group.get("items", []):
                        item_id = item.get("id", "")
                        if not item_id:
                            continue
                        fetch_count += 1
                        results.append({
                            "name": item.get("name", "") or f"{kw} 演出(ID {item_id})",
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
                log.info(f"fetch 并发: {time.time()-t_api:.1f}s, {fetch_count} 个结果")

                # ═══ Phase 3: fetch 全被拦截 → 回退逐页渲染 ═══
                if fetch_count == 0 and first_results:
                    log.warning("fetch 全部被拦截，回退到逐页渲染模式...")
                    for i, kw in enumerate(remaining):
                        try:
                            ajax_data.clear()
                            ajax_got.clear()
                            page.goto(
                                f"https://search.damai.cn/search.htm?keyword={kw}",
                                wait_until="domcontentloaded",
                                timeout=15000,
                            )
                            ajax_got.wait(timeout=3)
                            page.wait_for_timeout(300)
                            kw_results = _extract_from_ajax_or_dom(page, ajax_data, kw)
                            results.extend(kw_results)
                            if kw_results:
                                log.info(f"渲染 [{kw}]({i+2}/{len(keywords)}): {len(kw_results)}个")
                        except Exception as e:
                            log.warning(f"渲染 [{kw}] 失败: {e}")

            browser.close()
            browser = None

        found_kw = len(set(r["keywords"] for r in results))
        log.info(f"完成: {len(results)} 个演出 / {found_kw} 个歌手, 总耗时 {time.time()-t_start:.0f}s")
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


def _extract_from_ajax_or_dom(page, ajax_data: list, kw: str) -> list[dict]:
    """从拦截到的 AJAX 或 DOM 提取当前页的演出数据"""
    results = []

    # AJAX 数据
    for data in ajax_data:
        items = data.get("pageData", {}).get("resultData", [])
        for item in items:
            item_id = str(item.get("id", "") or item.get("itemId", ""))
            if not item_id:
                continue
            results.append({
                "name": item.get("name") or item.get("projectName") or f"{kw} 演出",
                "city": item.get("cityName") or item.get("venueCity", ""),
                "time": item.get("showTime") or item.get("performDate", ""),
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

    if results:
        return results

    # DOM 回退
    try:
        dom_items = page.evaluate("""
            () => {
                const results = [];
                const links = document.querySelectorAll('a[href*="detail.damai.cn/item.htm?id="]');
                const seen = new Set();
                links.forEach(link => {
                    const href = link.href;
                    const m = href.match(/id=(\\\\d+)/);
                    if (!m || seen.has(m[1])) return;
                    seen.add(m[1]);
                    results.push({id: m[1], name: (link.textContent||'').trim()});
                });
                return results;
            }
        """)
        for item in dom_items:
            results.append({
                "name": item.get("name") or f"{kw} 演出(ID {item['id']})",
                "city": "",
                "time": "",
                "platform": "damai",
                "item_id": item["id"],
                "url": f"https://detail.damai.cn/item.htm?id={item['id']}",
                "url_mobile": f"damai://V1/ProjectPage?id={item['id']}",
                "buy_url": f"https://detail.damai.cn/item.htm?id={item['id']}",
                "buy_keywords": ["立即购买", "立即预订", "选座购买"],
                "sold_keywords": ["缺货登记", "已售罄", "暂无可售"],
                "mode": "page",
                "keywords": kw,
            })
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
