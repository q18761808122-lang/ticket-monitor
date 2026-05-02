#!/usr/bin/env python3
"""
搜索缓存更新 — 搜索引擎聚合版
用 Bing 搜索「歌手 演唱会 2026 购票」，从搜索结果中提取所有平台链接
不直接爬票务平台，绕过反爬，一次搜索覆盖大麦/猫眼/票星球/大河/摩天轮/秀动/抖音等
"""
import json
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "public" / "search_cache.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cache_updater")

# ── 所有要搜索的歌手 ──
SINGERS = [
    "薛之谦", "周杰伦", "林俊杰", "五月天", "陈奕迅", "邓紫棋",
    "张杰", "华晨宇", "蔡依林", "张学友", "刘德华", "周深",
    "许嵩", "汪苏泷", "徐良", "李荣浩", "毛不易",
    "孙燕姿", "林宥嘉", "赵雷", "凤凰传奇", "刀郎",
    "陶喆", "梁静茹", "张惠妹", "李宗盛", "张信哲",
    "王菲", "刘若英", "李健", "陈粒", "伍佰",
    "任贤齐", "张靓颖", "谭咏麟", "那英",
]

# ── 各平台 URL 识别规则 ──
PLATFORM_RULES = [
    {
        "host": "detail.damai.cn",
        "id_re": r"id=(\d+)",
        "platform": "damai",
        "buy_keywords": ["立即购买", "立即预订", "选座购买"],
        "sold_keywords": ["缺货登记", "已售罄", "暂无可售"],
    },
    {
        "host": "show.maoyan.com",
        "id_re": r"detail/(\d+)",
        "platform": "maoyan",
        "buy_keywords": ["立即购票", "选座购票", "立即预订"],
        "sold_keywords": ["已售罄", "暂时无货"],
    },
    {
        "host": "xingqiupiao.com",
        "id_re": r"[?&]id=(\d+)",
        "platform": "generic",
        "buy_keywords": ["立即购买", "选座购买", "去购买", "立即预订"],
        "sold_keywords": ["已售罄", "售罄", "缺货登记", "下架", "已下架"],
    },
    {
        "host": "dahepiao.com",
        "id_re": r"yc/([^/]+)",
        "platform": "generic",
        "buy_keywords": ["立即购买", "选座购买", "立即预订", "有票"],
        "sold_keywords": ["已售罄", "售罄", "缺货登记", "等待开售"],
    },
    {
        "host": "motianlun.cn",
        "id_re": r"showId=(\w+)",
        "platform": "generic",
        "buy_keywords": ["立即购买", "选座购买", "去购买", "有票"],
        "sold_keywords": ["已售罄", "售罄", "缺货登记", "已下架"],
    },
    {
        "host": "ypiao.com",
        "id_re": r"t_(\d+)",
        "platform": "generic",
        "buy_keywords": ["立即购买", "去购买", "立即预订", "我要买票", "有票"],
        "sold_keywords": ["已售罄", "售罄", "缺货登记", "下架", "已下架", "等待开售"],
    },
    {
        "host": "showstart.com",
        "id_re": r"event/(\d+)",
        "platform": "showstart",
        "buy_keywords": ["立即购票", "立即购买"],
        "sold_keywords": ["已售罄", "售罄", "已结束"],
    },
    # 抖音系列 — 无固定 ID 格式，用 URL hash 做 key
    {
        "host": "douyin.com",
        "id_re": "",
        "platform": "douyin",
        "buy_keywords": ["立即购买", "立即抢购", "马上抢", "去购买", "提交订单"],
        "sold_keywords": ["已售罄", "已抢光", "抢光了", "已结束", "暂时无货", "缺货"],
    },
    {
        "host": "haohuo.jinritemai.com",
        "id_re": "",
        "platform": "douyin",
        "buy_keywords": ["立即购买", "立即抢购", "马上抢"],
        "sold_keywords": ["已售罄", "已抢光", "抢光了"],
    },
    {
        "host": "v.douyin.com",
        "id_re": "",
        "platform": "douyin",
        "buy_keywords": ["立即购买", "立即抢购", "马上抢"],
        "sold_keywords": ["已售罄", "已抢光", "抢光了"],
    },
]

CITIES_RE = re.compile(
    r"北京|上海|广州|深圳|成都|重庆|杭州|武汉|西安|南京|天津|苏州|长沙|郑州|"
    r"沈阳|青岛|大连|东莞|宁波|厦门|福州|合肥|无锡|佛山|昆明|贵阳|南宁|南昌|"
    r"哈尔滨|长春|石家庄|太原|济南|兰州|银川|西宁|拉萨|海口|呼和浩特|乌鲁木齐"
)

TIME_RE = re.compile(r"(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})")

YEAR_RE = re.compile(r"202[56]")


def _match_platform(url: str):
    """识别 URL 所属平台并提取 ID / buy_url"""
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.hostname or ""
    for rule in PLATFORM_RULES:
        if rule["host"] in host:
            item_id = ""
            buy_url = url  # 默认用原始 URL

            if rule["id_re"]:
                m = re.search(rule["id_re"], url)
                if m:
                    item_id = m.group(1)
                    # 统一用原始 URL 作为 buy_url，保证能直接打开
                else:
                    continue  # 没匹配到 ID，跳过这个平台
            else:
                # 无固定 ID 格式（如抖音），用 URL hash
                import hashlib
                item_id = hashlib.md5(url.encode()).hexdigest()[:12]

            return {
                "platform": rule["platform"],
                "item_id": item_id,
                "url": url,
                "buy_url": buy_url,
                "buy_url_mobile": buy_url,
                "buy_keywords": rule["buy_keywords"],
                "sold_keywords": rule["sold_keywords"],
            }
    return None


def search_bing(keyword: str) -> list[dict]:
    """
    用 Bing 搜索「歌手 演唱会 2026 购票」
    从搜索结果中提取所有已知票务平台的链接
    """
    results = []
    browser = None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
            )
            page = ctx.new_page()

            query = f"{keyword} 演唱会 2026 购票"
            bing_url = f"https://www.bing.com/search?q={query}&setlang=zh-cn"

            try:
                page.goto(bing_url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)
            except Exception:
                pass

            # 从搜索结果提取所有链接
            links = page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();
                    // Bing 搜索结果在 #b_results 里
                    const container = document.querySelector('#b_results');
                    const allLinks = (container || document).querySelectorAll('a[href]');
                    allLinks.forEach(a => {
                        const href = a.href;
                        if (!href || seen.has(href)) return;
                        seen.add(href);
                        const parentText = (a.closest('li, .b_algo, div')?.textContent || a.textContent || '').substring(0, 500);
                        results.push({
                            url: href,
                            name: a.textContent.trim().substring(0, 200),
                            text: parentText,
                        });
                    });
                    return results;
                }
            """)

            for link in links:
                url = link.get("url", "")
                text = link.get("text", "") or link.get("name", "")
                full_text = link.get("text", "")

                if not url:
                    continue

                match = _match_platform(url)
                if not match:
                    continue

                # 检查是否包含年份（过滤过期内容）
                if not YEAR_RE.search(text + full_text + url):
                    continue

                city = ""
                time_str = ""
                m_city = CITIES_RE.search(text + full_text)
                if m_city:
                    city = m_city.group(0)
                m_time = TIME_RE.search(text + full_text)
                if m_time:
                    time_str = m_time.group(1)

                name = text if len(text) >= 3 else f"{keyword} 演出"
                # 清理名称中混入的 URL
                name = re.sub(r'https?://\S+', '', name).strip()
                if len(name) < 3:
                    name = f"{keyword} 演出"

                results.append({
                    "name": name,
                    "city": city,
                    "time": time_str,
                    "platform": match["platform"],
                    "item_id": match["item_id"],
                    "url": match["url"],
                    "url_mobile": match["url"],
                    "buy_url": match["buy_url"],
                    "buy_keywords": match["buy_keywords"],
                    "sold_keywords": match["sold_keywords"],
                    "mode": "page",
                    "keywords": f"{keyword},{match['platform']}",
                })

            browser.close()
            browser = None
    except ImportError:
        log.error("Playwright 未安装")
    except Exception as e:
        log.error(f"Bing 搜索 [{keyword}] 失败: {e}")
        if browser:
            try:
                browser.close()
            except Exception:
                pass
    return results


def search_all(keywords: list[str]) -> list[dict]:
    """单浏览器顺序搜索所有歌手（Bing 不限速）"""
    results = []
    browser = None
    try:
        from playwright.sync_api import sync_playwright

        t0 = time.time()
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu", "--single-process",
                ],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
            )
            page = ctx.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)

            log.info(f"浏览器就绪 ({time.time()-t0:.0f}s)，开始搜索 {len(keywords)} 位歌手")

            hit_count = 0
            for i, kw in enumerate(keywords):
                try:
                    query = f"{kw} 演唱会 2026 购票"
                    bing_url = f"https://www.bing.com/search?q={query}&setlang=zh-cn"
                    page.goto(bing_url, wait_until="domcontentloaded", timeout=12000)
                    page.wait_for_timeout(1500)

                    links = page.evaluate("""
                        () => {
                            const results = [];
                            const seen = new Set();
                            const container = document.querySelector('#b_results');
                            (container || document).querySelectorAll('a[href]').forEach(a => {
                                const href = a.href;
                                if (!href || seen.has(href) || href.includes('bing.com')) return;
                                seen.add(href);
                                const el = a.closest('li, .b_algo, div');
                                results.push({
                                    url: href,
                                    name: a.textContent.trim().substring(0, 200),
                                    text: (el?.textContent || '').substring(0, 500),
                                });
                            });
                            return results;
                        }
                    """)

                    kw_results = 0
                    for link in links:
                        url = link.get("url", "")
                        text = link.get("text", "")
                        full_text = link.get("text", "")
                        if not url:
                            continue

                        match = _match_platform(url)
                        if not match:
                            continue
                        if not YEAR_RE.search(text + full_text + url):
                            continue

                        city = ""
                        time_str = ""
                        m_city = CITIES_RE.search(text + full_text)
                        if m_city:
                            city = m_city.group(0)
                        m_time = TIME_RE.search(text + full_text)
                        if m_time:
                            time_str = m_time.group(1)

                        name = text if len(text) >= 3 else f"{kw} 演出"
                        name = re.sub(r'https?://\S+', '', name).strip()
                        if len(name) < 3:
                            name = f"{kw} 演出"

                        results.append({
                            "name": name,
                            "city": city,
                            "time": time_str,
                            "platform": match["platform"],
                            "item_id": match["item_id"],
                            "url": match["url"],
                            "url_mobile": match["url"],
                            "buy_url": match["buy_url"],
                            "buy_keywords": match["buy_keywords"],
                            "sold_keywords": match["sold_keywords"],
                            "mode": "page",
                            "keywords": kw,
                        })
                        kw_results += 1

                    if kw_results:
                        hit_count += 1
                        log.info(f"[{i+1}/{len(keywords)}] {kw}: {kw_results} 个链接")
                except Exception as e:
                    log.warning(f"[{kw}] 搜索异常: {e}")
                time.sleep(0.3)

            browser.close()
            browser = None

        found_singers = len(set(r["keywords"] for r in results))
        log.info(f"搜索完成: {len(results)} 个演出 / {found_singers} 位歌手 / 总耗时 {time.time()-t0:.0f}s")
    except ImportError:
        log.error("Playwright 未安装")
    except Exception as e:
        log.error(f"搜索崩溃: {e}")
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
    log.info("搜索缓存更新 — Bing 聚合模式")
    existing = load_existing()
    log.info(f"现有缓存: {len(existing)} 条")

    all_new = search_all(SINGERS)

    merged = merge(existing, all_new)
    elapsed = time.time() - t0
    log.info(f"新增: {len(all_new)} 条, 合并后: {len(merged)} 条, {elapsed:.0f}s")

    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    count_file = BASE_DIR / "cache_update_count.txt"
    count_file.write_text(str(len(all_new)))
    if all_new:
        log.info(f"::notice::新增 {len(all_new)} 条搜索结果")


if __name__ == "__main__":
    main()
