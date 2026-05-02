#!/usr/bin/env python3
"""
搜索缓存更新脚本 — 极速版
Playwright 仅用一次获取 cookie → 后续全部 requests 并发调 AJAX API
"""
import hashlib
import json
import logging
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

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


def _get_damai_cookies() -> dict:
    """用 Playwright 获取大麦的有效 cookie（含 x5sec）"""
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
            """)

            # 访问首页
            page.goto("https://www.damai.cn/", wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)

            # 做一次搜索让 x5sec 签发
            page.goto("https://search.damai.cn/search.htm?keyword=演唱会", wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(3000)

            cookies = context.cookies()
            browser.close()

            # 转为 requests 可用的 cookie dict
            return {c["name"]: c["value"] for c in cookies}
    except Exception as e:
        log.warning(f"获取 cookie 失败: {e}")
        return {}


def _search_damai_api(kw: str, session: requests.Session) -> list[dict]:
    """直接调大麦 AJAX API（需要有效 cookie）"""
    results = []
    try:
        params = {
            "keyword": kw,
            "ctl": "1",
            "page": "1",
            "ts": str(int(time.time() * 1000)),
        }
        url = "https://search.damai.cn/searchajax.html?" + urllib.parse.urlencode(params)
        resp = session.get(url, timeout=10)

        if resp.status_code != 200 or len(resp.text) < 50:
            return results
        if "x5sec" in resp.text or "punish" in resp.text.lower():
            log.debug(f"大麦 [{kw}]: 被拦截")
            return results

        data = resp.json()
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
    log.info("搜索缓存更新 — 极速模式")
    existing = load_existing()
    log.info(f"现有缓存: {len(existing)} 条")

    all_new = []

    # ── Step 1: Playwright 获取 cookie（一次性，约 8s） ──
    cookies = _get_damai_cookies()
    if not cookies:
        log.error("无法获取大麦 cookie，退出")
        sys.exit(0)

    log.info(f"获取到 {len(cookies)} 个 cookie，开始并发搜索")

    # ── Step 2: 并发调 AJAX API ──
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://search.damai.cn/",
    })

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_search_damai_api, kw, session): kw for kw in DAMAI_KEYWORDS}
        for f in as_completed(futures):
            kw = futures[f]
            try:
                results = f.result()
                if results:
                    log.info(f"大麦 [{kw}]: {len(results)} 个")
                    all_new.extend(results)
            except Exception as e:
                log.warning(f"大麦 [{kw}] 失败: {e}")

    # ── 合并写入 ──
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
