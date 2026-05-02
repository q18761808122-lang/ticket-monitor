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
    # 一线歌手 / 乐队
    "薛之谦", "周杰伦", "林俊杰", "五月天", "陈奕迅", "邓紫棋",
    "张杰", "华晨宇", "蔡依林", "王菲", "张学友", "刘德华",
    "李宗盛", "张惠妹", "周深", "张信哲", "陶喆", "梁静茹",
    # 热门 / 巡演常客
    "许嵩", "汪苏泷", "徐良", "李荣浩", "毛不易", "刘若英",
    "孙燕姿", "萧敬腾", "林宥嘉", "杨千嬅", "容祖儿", "陈粒",
    "赵雷", "李健", "朴树", "许巍", "老狼", "伍佰",
    "凤凰传奇", "大张伟", "二手玫瑰", "痛仰", "新裤子",
    "周传雄", "光良", "品冠", "任贤齐", "张靓颖", "谭维维",
    "田馥甄", "苏打绿", "告五人", "八三夭", "茄子蛋",
    # 偶像 / 流量
    "王源", "王俊凯", "易烊千玺", "时代少年团", "鹿晗",
    "张艺兴", "王一博", "肖战", "蔡徐坤",
    # 日韩 / 欧美来华
    "泰勒斯威夫特", "周兴哲", "米津玄师",
    # 经典
    "谭咏麟", "林子祥", "叶倩文", "罗大佑", "费玉清",
    "那英", "韩红", "孙楠", "刀郎",
]

CITIES_RE = re.compile(
    r"北京|上海|广州|深圳|成都|重庆|杭州|武汉|西安|南京|天津|苏州|长沙|郑州|"
    r"沈阳|青岛|大连|东莞|宁波|厦门|福州|合肥|无锡|佛山|昆明|贵阳|南宁|南昌|"
    r"哈尔滨|长春|石家庄|太原|济南|兰州|银川|西宁|拉萨|海口|呼和浩特|乌鲁木齐"
)

TIME_RE = re.compile(r"(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})")


DOUYIN_KEYWORDS = [
    "薛之谦", "周杰伦", "林俊杰", "五月天", "陈奕迅", "邓紫棋",
    "张杰", "华晨宇", "蔡依林", "王菲", "张学友", "刘德华",
    "许嵩", "汪苏泷", "徐良", "李荣浩", "毛不易",
    "孙燕姿", "林宥嘉", "赵雷", "凤凰传奇", "刀郎",
    "周深", "张信哲", "陶喆", "梁静茹",
]


def search_douyin_playwright(keyword: str) -> list[dict]:
    """用 Playwright 搜索抖音演出门票 — 移动端 UA，从搜索/商城页面提取购票链接"""
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

            # 搜索：歌手名 + 演唱会/门票
            search_url = f"https://www.douyin.com/search/{keyword}%20演唱会%20门票?type=general"
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(6000)
            except Exception:
                pass

            html = page.content()

            # 提取商品/购票链接
            links_found = page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();

                    // douyin.com 商品页
                    for (const a of document.querySelectorAll('a[href]')) {
                        const href = a.href;
                        if (!href) continue;
                        const text = (a.textContent || '').trim();

                        // 匹配抖音小店/商品链接
                        if (href.includes('haohuo.jinritemai.com') ||
                            href.includes('v.douyin.com') ||
                            href.includes('www.douyin.com/goods') ||
                            href.includes('www.douyin.com/user/') && text.includes('票') ||
                            href.includes('douyin.com/video/') && text.includes('票')) {

                            const key = href.substring(0, 80);
                            if (seen.has(key)) continue;
                            seen.add(key);
                            results.push({url: href, text: text.substring(0, 200)});
                        }
                    }
                    return results;
                }
            """)

            for link in links_found[:5]:  # 最多取 5 个
                url = link.get("url", "")
                text = link.get("text", "")
                if not url:
                    continue

                # 生成唯一 ID
                import hashlib
                url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
                item_id = f"dy_{url_hash}"

                # 从文本或 URL 中提取演出名
                name = text if text and len(text) >= 3 else f"{keyword} 抖音演出"
                results.append({
                    "name": name,
                    "city": extract_city(text + url),
                    "time": extract_time(text + url),
                    "platform": "douyin",
                    "item_id": item_id,
                    "url": url,
                    "url_mobile": url,
                    "buy_url": url,
                    "buy_keywords": ["立即购买", "立即抢购", "马上抢", "去购买", "提交订单"],
                    "sold_keywords": ["已售罄", "已抢光", "抢光了", "已结束", "暂时无货", "缺货"],
                    "mode": "page",
                    "keywords": f"{keyword},抖音",
                })

            browser.close()

            # 同时尝试抖音商城搜索
            mall_results = _search_douyin_mall(keyword)
            results.extend(mall_results)

            if results:
                log.info(f"抖音搜索 [{keyword}]: {len(results)} 个结果")
    except ImportError:
        log.debug("Playwright 未安装，跳过抖音搜索")
    except Exception as e:
        log.warning(f"抖音搜索 [{keyword}] 失败: {e}")
        if browser:
            try:
                browser.close()
            except Exception:
                pass
    return results


def _search_douyin_mall(keyword: str) -> list[dict]:
    """搜索抖音商城 haohuo.jinritemai.com — 补充数据源"""
    results = []
    try:
        import requests

        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

        # 抖音商城搜索
        search_url = f"https://haohuo.jinritemai.com/views/search?keyword={keyword}%20演唱会%20门票"
        resp = session.get(search_url, timeout=15, allow_redirects=True)

        if resp.status_code == 200 and len(resp.text) > 500:
            import hashlib
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            links = soup.select('a[href*="product"], a[href*="detail"], a[href*="item"]')
            seen = set()
            for link in links[:5]:
                href = link.get("href", "")
                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://haohuo.jinritemai.com" + href
                if href in seen:
                    continue
                seen.add(href)
                url_hash = hashlib.md5(href.encode()).hexdigest()[:12]
                results.append({
                    "name": f"{keyword} 抖音商城演出",
                    "city": "",
                    "time": "",
                    "platform": "douyin",
                    "item_id": f"dymall_{url_hash}",
                    "url": href,
                    "url_mobile": href,
                    "buy_url": href,
                    "buy_keywords": ["立即购买", "立即抢购", "马上抢"],
                    "sold_keywords": ["已售罄", "已抢光", "抢光了"],
                    "mode": "page",
                    "keywords": f"{keyword},抖音商城",
                })
    except Exception:
        pass
    return results


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

    # ── 大麦搜索 ──
    damai_count = 0
    for kw in HOT_KEYWORDS:
        results = search_damai_playwright(kw)
        if results:
            damai_count += 1
            all_new.extend(results)
        time.sleep(2)

    log.info(f"大麦: {damai_count}/{len(HOT_KEYWORDS)} 个关键词成功")

    # ── 抖音搜索（热门歌手子集） ──
    douyin_count = 0
    for kw in DOUYIN_KEYWORDS:
        results = search_douyin_playwright(kw)
        if results:
            douyin_count += 1
            all_new.extend(results)
        time.sleep(2)

    log.info(f"抖音: {douyin_count}/{len(DOUYIN_KEYWORDS)} 个关键词成功")

    log.info(f"总计新增: {len(all_new)} 条")

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
