#!/usr/bin/env python3
"""
搜索缓存更新脚本 — 事件驱动版
单浏览器 + 拦截 AJAX 响应即时处理 + DOM 回退
"""
import hashlib
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


def extract_city(text: str) -> str:
    m = CITIES_RE.search(text)
    return m.group(0) if m else ""


def extract_time(text: str) -> str:
    m = TIME_RE.search(text)
    return m.group(1) if m else ""


def search_all_damai(keywords: list[str]) -> list[dict]:
    """单浏览器事件驱动：AJAX 响应一到立即提取，不等多余时间"""
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

            # 事件驱动：AJAX 响应到达时立即触发
            ajax_data = []
            ajax_event = threading.Event()

            def on_response(response):
                if "searchajax" in response.url and response.status == 200:
                    try:
                        data = response.json()
                        ajax_data.append(data)
                        ajax_event.set()
                    except Exception:
                        pass

            page.on("response", on_response)

            # 建立 cookie（只一次）
            try:
                page.goto("https://www.damai.cn/", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1000)
            except Exception:
                pass

            success = 0
            for i, kw in enumerate(keywords):
                try:
                    ajax_data.clear()
                    ajax_event.clear()

                    search_url = f"https://search.damai.cn/search.htm?keyword={kw}"
                    page.goto(search_url, wait_until="domcontentloaded", timeout=15000)

                    # 等待 AJAX 响应，最多等 4 秒
                    got_ajax = ajax_event.wait(timeout=4)

                    if got_ajax and ajax_data:
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
                        if ajax_data:
                            success += 1
                            log.info(f"[{i+1}/{len(keywords)}] {kw}: AJAX {len(ajax_data[0].get('pageData',{}).get('resultData',[]))}个")
                            continue

                    # 回退：DOM 提取
                    page.wait_for_timeout(1000)
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
                                const card = link.closest('li, [class*="item"], [class*="card"], div') || link;
                                results.push({
                                    id: m[1],
                                    name: (link.textContent || '').trim(),
                                    text: (card.textContent || '').substring(0, 300),
                                });
                            });
                            return results;
                        }
                    """)

                    for item in dom_items:
                        if not item.get("id"):
                            continue
                        name = item.get("name", "")
                        text = item.get("text", "")
                        if not name or len(name) < 3:
                            name = f"{kw} 演出(ID {item['id']})"
                        results.append({
                            "name": name,
                            "city": extract_city(text),
                            "time": extract_time(text),
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

                    if dom_items:
                        success += 1
                        log.info(f"[{i+1}/{len(keywords)}] {kw}: DOM {len(dom_items)}个")

                except Exception as e:
                    log.warning(f"[{kw}] 异常: {e}")

            browser.close()
            browser = None
            log.info(f"大麦完成: {success}/{len(keywords)} 个关键词有结果")
    except ImportError:
        log.error("Playwright 未安装")
    except Exception as e:
        log.error(f"大麦搜索崩溃: {e}")
        if browser:
            try:
                browser.close()
            except Exception:
                pass
    return results


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


def main():
    t0 = time.time()
    log.info("搜索缓存更新 — 事件驱动模式")
    existing = load_existing()
    log.info(f"现有缓存: {len(existing)} 条")

    all_new = search_all_damai(DAMAI_KEYWORDS)

    merged = merge(existing, all_new)
    log.info(f"新增: {len(all_new)} 条, 合并后: {len(merged)} 条, 耗时 {time.time()-t0:.0f}s")

    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    count_file = BASE_DIR / "cache_update_count.txt"
    count_file.write_text(str(len(all_new)))
    if all_new:
        log.info(f"::notice::新增 {len(all_new)} 条搜索结果")


if __name__ == "__main__":
    main()
