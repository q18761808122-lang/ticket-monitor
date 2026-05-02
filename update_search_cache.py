#!/usr/bin/env python3
"""
搜索缓存更新脚本 — Playwright 版
用真实浏览器渲染大麦搜索页，拦截 AJAX 响应 / 提取 DOM 数据
由 GitHub Actions 定时运行，更新 public/search_cache.json
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

HOT_KEYWORDS = [
    "薛之谦", "周杰伦", "林俊杰", "五月天", "陈奕迅", "邓紫棋",
    "张杰", "华晨宇", "蔡依林", "王菲", "张学友", "刘德华",
    "李宗盛", "张惠妹", "周深", "张信哲", "陶喆", "梁静茹",
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


def search_damai_playwright(keyword: str) -> list[dict]:
    """用 Playwright 渲染大麦搜索页，优先拦截 AJAX JSON，回退到 DOM 提取"""
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

            # 隐藏自动化痕迹
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
            """)

            # 拦截 searchajax 响应
            ajax_data = []

            def on_response(response):
                if "searchajax" in response.url and response.status == 200:
                    try:
                        data = response.json()
                        ajax_data.append(data)
                    except Exception:
                        pass

            page.on("response", on_response)

            # 先访问首页建立 cookie
            try:
                page.goto("https://www.damai.cn/", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(3000)
            except Exception:
                log.debug("访问大麦首页超时，继续搜索")

            # 访问搜索页
            search_url = f"https://search.damai.cn/search.htm?keyword={keyword}"
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(8000)
            except Exception as e:
                log.warning(f"搜索页加载超时 [{keyword}]: {e}")

            # 方式1: 从拦截的 AJAX 响应提取
            if ajax_data:
                for data in ajax_data:
                    items = data.get("pageData", {}).get("resultData", [])
                    for item in items:
                        item_id = str(item.get("id", "") or item.get("itemId", ""))
                        if not item_id:
                            continue
                        results.append(_build_damai_entry(
                            item_id=item_id,
                            name=item.get("name") or item.get("projectName", ""),
                            city=item.get("cityName") or item.get("venueCity", ""),
                            time_str=item.get("showTime") or item.get("performDate", ""),
                            keyword=keyword,
                        ))
                log.info(f"搜索 [{keyword}] AJAX 拦截: {len(results)} 个结果")
                browser.close()
                return results

            # 方式2: 从 DOM 提取链接
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
                            name: (link.textContent || link.getAttribute('title') || '').trim(),
                            text: (card.textContent || '').substring(0, 500),
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
                    name = f"{keyword} 演出(ID {item['id']})"
                results.append(_build_damai_entry(
                    item_id=item["id"],
                    name=name,
                    city=extract_city(text),
                    time_str=extract_time(text),
                    keyword=keyword,
                ))

            browser.close()
            log.info(f"搜索 [{keyword}] DOM 提取: {len(results)} 个结果")
    except ImportError:
        log.error("Playwright 未安装，无法搜索大麦")
    except Exception as e:
        log.error(f"搜索 [{keyword}] 失败: {e}")
        if browser:
            try:
                browser.close()
            except Exception:
                pass
    return results


def _build_damai_entry(item_id: str, name: str, city: str, time_str: str, keyword: str) -> dict:
    """构建统一的大麦演出条目"""
    display_name = name if name and len(name) >= 2 else f"{keyword} 演出(ID {item_id})"
    return {
        "name": display_name,
        "city": city,
        "time": time_str,
        "platform": "damai",
        "item_id": item_id,
        "url": f"https://detail.damai.cn/item.htm?id={item_id}",
        "url_mobile": f"damai://V1/ProjectPage?id={item_id}",
        "buy_url": f"https://detail.damai.cn/item.htm?id={item_id}",
        "buy_keywords": ["立即购买", "立即预订", "选座购买"],
        "sold_keywords": ["缺货登记", "已售罄", "暂无可售"],
        "mode": "page",
        "keywords": keyword,
    }


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
    log.info("搜索缓存更新开始（Playwright 模式）")
    existing = load_existing()
    log.info(f"现有缓存: {len(existing)} 条")

    all_new = []
    success_count = 0
    for kw in HOT_KEYWORDS:
        results = search_damai_playwright(kw)
        if results:
            success_count += 1
            all_new.extend(results)
        time.sleep(2)

    log.info(f"成功搜索: {success_count}/{len(HOT_KEYWORDS)} 个关键词, 新增结果: {len(all_new)} 条")

    merged = merge(existing, all_new)
    log.info(f"合并后总计: {len(merged)} 条")

    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    log.info("缓存写入完成")
    if all_new:
        with open(BASE_DIR / "cache_update_count.txt", "w") as f:
            f.write(str(len(all_new)))
        log.info(f"::notice::新增 {len(all_new)} 条搜索结果")


if __name__ == "__main__":
    main()
