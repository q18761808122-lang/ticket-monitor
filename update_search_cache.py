#!/usr/bin/env python3
"""
搜索缓存更新脚本 — 单浏览器实例版
一个浏览器跑完所有搜索，避免反复启动/关闭，大幅提速
"""
import hashlib
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

# 大麦关键词（精简到核心高频）
DAMAI_KEYWORDS = [
    "薛之谦", "周杰伦", "林俊杰", "五月天", "陈奕迅", "邓紫棋",
    "张杰", "华晨宇", "蔡依林", "张学友", "刘德华", "周深",
    "许嵩", "汪苏泷", "徐良", "李荣浩", "毛不易",
    "孙燕姿", "林宥嘉", "赵雷", "凤凰传奇", "刀郎",
    "陶喆", "梁静茹", "张惠妹", "李宗盛", "张信哲",
    "王菲", "刘若英", "李健", "陈粒", "伍佰",
    "任贤齐", "张靓颖", "谭咏麟", "那英",
]

# 抖音关键词（高频子集）
DOUYIN_KEYWORDS = [
    "薛之谦", "周杰伦", "林俊杰", "五月天", "陈奕迅", "邓紫棋",
    "张杰", "华晨宇", "许嵩", "汪苏泷", "徐良", "李荣浩",
    "周深", "赵雷", "凤凰传奇", "刀郎",
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


# ── 大麦：单浏览器跑全部关键词 ──

def search_all_damai(keywords: list[str]) -> list[dict]:
    """一个浏览器实例跑完所有大麦搜索"""
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

            # 先访问首页建立 cookie（只一次）
            try:
                page.goto("https://www.damai.cn/", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1500)
            except Exception:
                pass

            for i, kw in enumerate(keywords):
                try:
                    ajax_data.clear()
                    search_url = f"https://search.damai.cn/search.htm?keyword={kw}"
                    page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(3000)  # Vue 渲染通常 2-3 秒

                    # 方式1: AJAX 拦截
                    if ajax_data:
                        for data in ajax_data:
                            for item in data.get("pageData", {}).get("resultData", []):
                                item_id = str(item.get("id", "") or item.get("itemId", ""))
                                if not item_id:
                                    continue
                                results.append(_build_damai_entry(
                                    item_id=item_id,
                                    name=item.get("name") or item.get("projectName", ""),
                                    city=item.get("cityName") or item.get("venueCity", ""),
                                    time_str=item.get("showTime") or item.get("performDate", ""),
                                    keyword=kw,
                                ))
                        if ajax_data:
                            log.info(f"大麦 [{kw}]({i+1}/{len(keywords)}): AJAX {len(ajax_data)}个响应")
                            time.sleep(0.5)
                            continue

                    # 方式2: DOM 提取
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
                            name = f"{kw} 演出(ID {item['id']})"
                        results.append(_build_damai_entry(
                            item_id=item["id"],
                            name=name,
                            city=extract_city(text),
                            time_str=extract_time(text),
                            keyword=kw,
                        ))

                    log.info(f"大麦 [{kw}]({i+1}/{len(keywords)}): DOM {len(dom_items)}个")
                except Exception as e:
                    log.warning(f"大麦 [{kw}] 异常: {e}")
                time.sleep(0.8)  # 请求间隔

            browser.close()
            browser = None
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


def _build_damai_entry(item_id: str, name: str, city: str, time_str: str, keyword: str) -> dict:
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


# ── 抖音：单浏览器跑全部关键词 ──

def search_all_douyin(keywords: list[str]) -> list[dict]:
    """一个移动端浏览器实例跑完所有抖音搜索"""
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
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
                ),
                viewport={"width": 390, "height": 844},
                locale="zh-CN",
            )
            page = context.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)

            for i, kw in enumerate(keywords):
                try:
                    search_url = f"https://www.douyin.com/search/{kw}%20演唱会%20门票?type=general"
                    page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(3000)

                    links_found = page.evaluate("""
                        () => {
                            const results = [];
                            const seen = new Set();
                            for (const a of document.querySelectorAll('a[href]')) {
                                const href = a.href;
                                if (!href) continue;
                                const text = (a.textContent || '').trim();
                                if (href.includes('haohuo.jinritemai.com') ||
                                    href.includes('v.douyin.com') ||
                                    (href.includes('douyin.com') && (
                                        text.includes('票') || text.includes('购') || text.includes('演')
                                    ))) {
                                    const key = href.substring(0, 80);
                                    if (seen.has(key)) continue;
                                    seen.add(key);
                                    results.push({url: href, text: text.substring(0, 200)});
                                }
                            }
                            return results;
                        }
                    """)

                    for link in links_found[:5]:
                        url = link.get("url", "")
                        text = link.get("text", "")
                        if not url:
                            continue
                        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
                        results.append({
                            "name": text if text and len(text) >= 3 else f"{kw} 抖音演出",
                            "city": extract_city(text + url),
                            "time": extract_time(text + url),
                            "platform": "douyin",
                            "item_id": f"dy_{url_hash}",
                            "url": url,
                            "url_mobile": url,
                            "buy_url": url,
                            "buy_keywords": ["立即购买", "立即抢购", "马上抢", "去购买", "提交订单"],
                            "sold_keywords": ["已售罄", "已抢光", "抢光了", "已结束", "暂时无货", "缺货"],
                            "mode": "page",
                            "keywords": f"{kw},抖音",
                        })

                    if links_found:
                        log.info(f"抖音 [{kw}]({i+1}/{len(keywords)}): {len(links_found)}个链接")
                except Exception as e:
                    log.warning(f"抖音 [{kw}] 异常: {e}")
                time.sleep(0.8)

            browser.close()
            browser = None
    except ImportError:
        log.debug("Playwright 未安装，跳过抖音搜索")
    except Exception as e:
        log.warning(f"抖音搜索崩溃: {e}")
        if browser:
            try:
                browser.close()
            except Exception:
                pass
    return results


# ── 缓存管理 ──

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
    log.info("搜索缓存更新开始（单浏览器模式）")
    existing = load_existing()
    log.info(f"现有缓存: {len(existing)} 条")

    all_new = []

    # ── 大麦（单浏览器） ──
    log.info(f"大麦搜索 — {len(DAMAI_KEYWORDS)} 个关键词")
    damai_results = search_all_damai(DAMAI_KEYWORDS)
    all_new.extend(damai_results)
    log.info(f"大麦完成: {len(damai_results)} 个结果 ({time.time()-t0:.0f}s)")

    # ── 抖音（单浏览器） ──
    log.info(f"抖音搜索 — {len(DOUYIN_KEYWORDS)} 个关键词")
    douyin_results = search_all_douyin(DOUYIN_KEYWORDS)
    all_new.extend(douyin_results)
    log.info(f"抖音完成: {len(douyin_results)} 个结果 ({time.time()-t0:.0f}s)")

    # ── 合并写入 ──
    merged = merge(existing, all_new)
    log.info(f"总计新增: {len(all_new)} 条, 合并后: {len(merged)} 条")

    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    count_file = BASE_DIR / "cache_update_count.txt"
    count_file.write_text(str(len(all_new)))
    log.info(f"缓存写入完成，总耗时 {time.time()-t0:.0f}s")
    if all_new:
        log.info(f"::notice::新增 {len(all_new)} 条搜索结果")


if __name__ == "__main__":
    main()
