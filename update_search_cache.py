#!/usr/bin/env python3
"""
搜索缓存更新 v2.0 — 多平台并发聚合搜索
票星球 + 搜狗 + 大河 + 摩天轮 + 有票网 多源聚合
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "public" / "search_cache.json"
COUNT_FILE = BASE_DIR / "cache_update_count.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cache_updater")

SINGERS = [
    "薛之谦", "周杰伦", "林俊杰", "五月天", "陈奕迅", "邓紫棋",
    "张杰", "华晨宇", "蔡依林", "张学友", "刘德华", "周深",
    "许嵩", "汪苏泷", "李荣浩", "毛不易",
    "孙燕姿", "林宥嘉", "赵雷", "凤凰传奇", "刀郎",
    "陶喆", "梁静茹", "张惠妹", "李宗盛", "张信哲",
    "王菲", "刘若英", "李健", "陈粒", "伍佰",
    "任贤齐", "张靓颖", "谭咏麟", "那英",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

CITIES = re.compile(
    r"北京|上海|广州|深圳|成都|重庆|杭州|武汉|西安|南京|天津|苏州|长沙|郑州|"
    r"沈阳|青岛|大连|东莞|宁波|厦门|福州|合肥|无锡|佛山|昆明|贵阳|南宁|南昌|"
    r"哈尔滨|长春|石家庄|太原|济南|兰州|银川|西宁|拉萨|海口|呼和浩特|乌鲁木齐|"
    r"烟台|宜昌|洛阳|温州|泉州|惠州|珠海|金华|嘉兴|绍兴|中山"
)
DATE_RE = re.compile(r"(\d{4}[.\-/年]\d{1,2}[.\-/月]\d{1,2}[日]?)")
CONCERT_KW = re.compile(r"演唱|巡演|音乐节|音乐会|见面会|庆典|晚会|盛典")


def search_xingqiupiao(singer: str) -> list[dict]:
    """票星球 Nuxt SSR payload 解析"""
    results = []
    try:
        r = requests.get(f"http://www.xingqiupiao.com/search?keyword={singer}", headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return results

        scripts = re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.DOTALL)
        if len(scripts) < 4:
            return results

        try:
            payload = json.loads(scripts[3])
        except json.JSONDecodeError:
            return results

        all_strings = [x for x in payload if isinstance(x, str)]
        seen_names = set()

        for name in all_strings:
            if not CONCERT_KW.search(name) or len(name) < 4 or len(name) > 150:
                continue
            if singer not in name or name in seen_names:
                continue
            seen_names.add(name)

            city = _extract_city(name)
            date_str = _extract_date(name)
            eid = _find_event_id(payload, name)

            results.append(_make_item(
                name=name[:80], city=city, time=date_str,
                platform="generic", item_id=eid,
                url=f"http://www.xingqiupiao.com/event?id={eid}",
                keywords=singer,
                buy_kw=["立即购买", "选座购买", "去购买", "立即预订"],
                sold_kw=["已售罄", "售罄", "缺货登记", "下架", "已下架"],
            ))
    except Exception as e:
        log.warning(f"票星球 [{singer}]: {e}")
    return results


def search_sogou(singer: str) -> list[dict]:
    """搜狗搜索 — 提取大河/摩天轮/有票网/抖音等平台链接"""
    results = []
    try:
        r = requests.get(
            f"https://www.sogou.com/web?query={singer}+演唱会+购票",
            headers=HEADERS, timeout=15
        )
        text = r.content.decode("gbk", errors="ignore")

        url_pattern = r'https?://[^\s"\'<>]+'
        all_urls = re.findall(url_pattern, text)

        for url in all_urls:
            url = url.rstrip(".,;:!?）)")
            parsed = _parse_platform_url(url, singer, text)
            if parsed:
                results.append(parsed)
    except Exception as e:
        log.warning(f"搜狗 [{singer}]: {e}")
    return results


def search_dahe_direct(singer: str) -> list[dict]:
    """大河票务直接搜索"""
    results = []
    try:
        r = requests.get(
            f"https://www.dahepiao.com/search?keyword={singer}",
            headers=HEADERS, timeout=15
        )
        if r.status_code != 200:
            return results

        # 提取演出链接
        links = re.findall(r'href="(/yc/[^"]+)"', r.text)
        seen = set()
        for link in links:
            if link in seen:
                continue
            seen.add(link)

            full_url = f"https://www.dahepiao.com{link}"
            name = link.split("/")[-1].replace("xunyanchanghui", "巡回演唱会") if "/" in link else f"{singer}演出"
            city = _extract_city(name)
            date_str = _extract_date(name)
            item_id = f"dahe_{abs(hash(link)) % 100000}"

            results.append(_make_item(
                name=name[:80], city=city, time=date_str,
                platform="generic", item_id=item_id,
                url=full_url, keywords=singer,
                buy_kw=["立即购买", "选座购买", "立即预订", "有票"],
                sold_kw=["已售罄", "售罄", "缺货登记", "等待开售"],
            ))
    except Exception as e:
        log.warning(f"大河 [{singer}]: {e}")
    return results


def search_motianlun_direct(singer: str) -> list[dict]:
    """摩天轮直接搜索"""
    results = []
    try:
        r = requests.get(
            f"https://m.motianlun.cn/api/search?keyword={singer}",
            headers={**HEADERS, "Referer": "https://m.motianlun.cn/"},
            timeout=15
        )
        if r.status_code != 200:
            return results

        data = r.json()
        items = data.get("data", data) if isinstance(data, dict) else []
        if not isinstance(items, list):
            items = [items] if items else []

        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("showName", item.get("name", f"{singer}演出"))
            show_id = str(item.get("showId", item.get("id", "")))
            if not show_id:
                continue

            results.append(_make_item(
                name=name[:80],
                city=item.get("cityName", item.get("city", "")),
                time=str(item.get("showTime", item.get("time", ""))),
                platform="generic", item_id=show_id,
                url=f"https://m.motianlun.cn/pages/show-detail/show-detail?showId={show_id}",
                keywords=singer,
                buy_kw=["立即购买", "选座购买", "去购买", "有票"],
                sold_kw=["已售罄", "售罄", "缺货登记", "已下架"],
            ))
    except Exception as e:
        log.warning(f"摩天轮 [{singer}]: {e}")
    return results


# ── 工具函数 ──────────────────────────────────────────

def _extract_city(text: str) -> str:
    m = re.search(r"[【\[]([^】\]]+)[】\]]", text)
    if m:
        cm = CITIES.search(m.group(1))
        if cm:
            return cm.group(0)
    cm = CITIES.search(text)
    return cm.group(0) if cm else ""


def _extract_date(text: str) -> str:
    m = DATE_RE.search(text)
    return m.group(0) if m else ""


def _find_event_id(payload: list, name: str) -> str:
    """在 payload 中查找 name 附近的 event_id"""
    try:
        name_idx = payload.index(name)
        for offset in range(-15, 15):
            idx = name_idx + offset
            if 0 <= idx < len(payload):
                val = payload[idx]
                if isinstance(val, str) and val.isdigit() and 4 <= len(val) <= 8:
                    return val
                if isinstance(val, (int, float)) and 1000 < val < 99999999:
                    return str(int(val))
    except ValueError:
        pass
    # 回退
    for d in payload:
        if isinstance(d, dict) and "event_id" in d:
            return str(d["event_id"])
    return str(abs(hash(name)) % 900000 + 100000)


def _parse_platform_url(url: str, singer: str, page_text: str) -> dict | None:
    """根据 URL 识别平台并构建监控项"""
    platform = "generic"
    item_id = ""
    buy_kw = ["立即购买"]
    sold_kw = ["已售罄"]

    if "dahepiao.com" in url:
        m = re.search(r"/(?:yc|yanchu)/([^/\s?#]+)", url)
        item_id = m.group(1) if m else f"dahe_{abs(hash(url)) % 10000}"
        buy_kw = ["立即购买", "选座购买", "立即预订", "有票"]
        sold_kw = ["已售罄", "售罄", "缺货登记", "等待开售"]
    elif "motianlun.cn" in url:
        m = re.search(r"showId=(\w+)", url)
        item_id = m.group(1) if m else f"mtl_{abs(hash(url)) % 10000}"
        buy_kw = ["立即购买", "选座购买", "去购买", "有票"]
        sold_kw = ["已售罄", "售罄", "缺货登记", "已下架"]
    elif "ypiao.com" in url:
        m = re.search(r"t_(\d+)", url)
        item_id = m.group(1) if m else f"ypiao_{abs(hash(url)) % 10000}"
        buy_kw = ["立即购买", "去购买", "立即预订", "我要买票", "有票"]
        sold_kw = ["已售罄", "售罄", "缺货登记", "下架", "已下架"]
    elif "douyin.com" in url and "video" in url:
        platform = "douyin"
        import hashlib
        item_id = hashlib.md5(url.encode()).hexdigest()[:12]
        buy_kw = ["立即购买", "立即抢购", "马上抢"]
        sold_kw = ["已售罄", "已抢光", "抢光了"]
    else:
        return None

    # 提取上下文信息
    url_pos = page_text.find(url)
    ctx = re.sub(r"<[^>]+>", " ", page_text[max(0, url_pos - 300):url_pos + 100]) if url_pos > 0 else ""
    city = _extract_city(ctx)
    date_str = _extract_date(ctx)

    # 名称
    name_map = {"dahepiao": "大河票务", "motianlun": "摩天轮", "ypiao": "有票网"}
    name = f"{singer}演出"
    for k, v in name_map.items():
        if k in url:
            name = f"{singer}演出（{v}）"
            break

    return _make_item(
        name=name[:80], city=city, time=date_str,
        platform=platform, item_id=item_id,
        url=url, keywords=singer,
        buy_kw=buy_kw, sold_kw=sold_kw,
    )


def _make_item(name, city, time, platform, item_id, url, keywords, buy_kw, sold_kw) -> dict:
    return {
        "name": name, "city": city, "time": time,
        "platform": platform, "item_id": item_id,
        "url": url, "url_mobile": url, "buy_url": url,
        "buy_keywords": buy_kw, "sold_keywords": sold_kw,
        "mode": "page", "keywords": keywords,
    }


# ── 合并去重 ──────────────────────────────────────────

def load_existing() -> list[dict]:
    if CACHE_PATH.exists():
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def merge(existing: list[dict], new: list[dict]) -> list[dict]:
    seen = set()
    merged = []
    for item in existing + new:
        key = f"{item.get('platform','')}_{item.get('item_id','')}"
        if key not in seen:
            seen.add(key)
            merged.append(item)

    # 同歌手同城去重泛化名称
    groups = {}
    for item in merged:
        gk = f"{item.get('keywords','')}_{item.get('city','')}_{item.get('platform','')}"
        groups.setdefault(gk, []).append(item)

    cleaned = []
    for items in groups.values():
        specific = [i for i in items if not re.match(r"^[一-鿿]{1,4}演出", i.get("name", ""))]
        cleaned.extend(specific or items)

    cleaned.sort(key=lambda x: (x.get("keywords", ""), x.get("city", "")))
    return cleaned


# ── 主流程 ────────────────────────────────────────────

def main():
    t0 = time.time()
    log.info("搜索缓存更新 v2.0 — 多平台并发聚合")
    existing = load_existing()
    log.info(f"现有缓存: {len(existing)} 条")

    all_new = []

    # 并发搜索所有歌手
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}
        for singer in SINGERS:
            futures[executor.submit(search_xingqiupiao, singer)] = ("票星球", singer)
            futures[executor.submit(search_dahe_direct, singer)] = ("大河", singer)
            futures[executor.submit(search_motianlun_direct, singer)] = ("摩天轮", singer)

        for future in as_completed(futures):
            source, singer = futures[future]
            try:
                results = future.result(timeout=30)
                if results:
                    all_new.extend(results)
                    log.info(f"[{source}] {singer}: {len(results)} 个")
            except Exception as e:
                log.warning(f"[{source}] {singer} 超时/异常: {e}")

    # 搜狗作为补充（仅对前两轮无结果的歌手）
    found_singers = set(item.get("keywords", "") for item in all_new)
    missing = [s for s in SINGERS if s not in found_singers]
    if missing:
        log.info(f"搜狗补充搜索 {len(missing)} 位歌手...")
        for singer in missing:
            results = search_sogou(singer)
            if results:
                all_new.extend(results)
                log.info(f"[搜狗] {singer}: {len(results)} 个")
            time.sleep(0.3)

    merged = merge(existing, all_new)
    elapsed = time.time() - t0
    found = len(set(item.get("keywords", "") for item in merged))
    log.info(f"新增: {len(all_new)} 条, 合并后: {len(merged)} 条 / {found} 位歌手 / {elapsed:.0f}s")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    COUNT_FILE.write_text(str(len(all_new)))
    if all_new:
        log.info(f"::notice::新增 {len(all_new)} 条搜索结果")


if __name__ == "__main__":
    main()
