#!/usr/bin/env python3
"""
搜索缓存更新 — 票星球 HTTP + 搜狗聚合版
直接搜索票星球（无需浏览器），搜狗作为其他平台补充
"""
import json
import logging
import re
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "public" / "search_cache.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cache_updater")

SINGERS = [
    "薛之谦", "周杰伦", "林俊杰", "五月天", "陈奕迅", "邓紫棋",
    "张杰", "华晨宇", "蔡依林", "张学友", "刘德华", "周深",
    "许嵩", "汪苏泷", "徐良", "李荣浩", "毛不易",
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

CITIES_PATTERN = re.compile(
    r"北京|上海|广州|深圳|成都|重庆|杭州|武汉|西安|南京|天津|苏州|长沙|郑州|"
    r"沈阳|青岛|大连|东莞|宁波|厦门|福州|合肥|无锡|佛山|昆明|贵阳|南宁|南昌|"
    r"哈尔滨|长春|石家庄|太原|济南|兰州|银川|西宁|拉萨|海口|呼和浩特|乌鲁木齐|"
    r"烟台|宜昌|洛阳|温州|泉州|惠州|珠海|金华|嘉兴|绍兴|中山"
)
DATE_PATTERN = re.compile(r"(\d{4}[.\-/年]\d{1,2}[.\-/月]\d{1,2}[日]?)")
CONCERT_KW = re.compile(r"演唱|巡演|音乐节|音乐会|见面会|庆典|晚会|盛典")


def search_xingqiupiao(singer: str) -> list[dict]:
    """搜索票星球（HTTP），从 Nuxt SSR payload 中提取演出信息"""
    results = []
    try:
        r = requests.get(
            f"http://www.xingqiupiao.com/search?keyword={singer}",
            headers=HEADERS, timeout=15
        )
        if r.status_code != 200:
            return results

        # 提取 Nuxt SSR payload
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.DOTALL)
        if len(scripts) < 4:
            return results

        try:
            payload = json.loads(scripts[3])
        except json.JSONDecodeError:
            return results

        # 提取所有演出名称字符串
        all_strings = [x for x in payload if isinstance(x, str)]
        seen_names = set()

        for name in all_strings:
            if not CONCERT_KW.search(name) or len(name) < 4 or len(name) > 150:
                continue
            if singer not in name:
                continue
            if name in seen_names:
                continue
            seen_names.add(name)

            # 提取城市
            city = ""
            city_m = re.search(r"[【\[]([^】\]]+)[】\]]", name)
            if city_m:
                inner = city_m.group(1)
                city_m2 = CITIES_PATTERN.search(inner)
                if city_m2:
                    city = city_m2.group(0)
            if not city:
                city_m = CITIES_PATTERN.search(name)
                if city_m:
                    city = city_m.group(0)

            # 提取日期
            date_str = ""
            date_m = DATE_PATTERN.search(name)
            if date_m:
                date_str = date_m.group(0)

            # 在 payload 中查找附近的 event_id
            eid = ""
            try:
                name_idx = payload.index(name)
                for offset in range(-15, 15):
                    check_idx = name_idx + offset
                    if 0 <= check_idx < len(payload):
                        val = payload[check_idx]
                        if isinstance(val, str) and val.isdigit() and 4 <= len(val) <= 8:
                            eid = val
                            break
                        if isinstance(val, (int, float)) and 1000 < val < 99999999:
                            eid = str(int(val))
                            break
            except ValueError:
                pass

            if not eid:
                for d in payload:
                    if isinstance(d, dict) and "event_id" in d:
                        eid = str(d["event_id"])
                        break

            if not eid:
                eid = str(abs(hash(name)) % 900000 + 100000)

            results.append({
                "name": name[:80],
                "city": city,
                "time": date_str,
                "platform": "generic",
                "item_id": eid,
                "url": f"http://www.xingqiupiao.com/event?id={eid}",
                "url_mobile": f"http://www.xingqiupiao.com/event?id={eid}",
                "buy_url": f"http://www.xingqiupiao.com/event?id={eid}",
                "buy_keywords": ["立即购买", "选座购买", "去购买", "立即预订"],
                "sold_keywords": ["已售罄", "售罄", "缺货登记", "下架", "已下架"],
                "mode": "page",
                "keywords": singer,
            })

    except Exception as e:
        log.warning(f"票星球 [{singer}]: {e}")

    return results


def search_sogou(singer: str) -> list[dict]:
    """搜索搜狗，提取大河/摩天轮/有票网/抖音等平台的链接"""
    results = []
    try:
        r = requests.get(
            f"https://www.sogou.com/web?query={singer}+演唱会+2026+购票",
            headers=HEADERS, timeout=15
        )
        text = r.content.decode("gbk", errors="ignore")

        # 提取非票星球的票务链接
        url_pattern = r'https?://[^\s"\'<>]+'
        all_urls = re.findall(url_pattern, text)

        for url in all_urls:
            url = url.rstrip(".,;:!?）)")

            # 识别平台
            platform = None
            item_id = ""
            buy_keywords = ["立即购买"]
            sold_keywords = ["已售罄"]

            if "dahepiao.com" in url:
                platform = "generic"
                m = re.search(r"/(?:yc|yanchu)/([^/\s]+)", url)
                item_id = m.group(1) if m else f"dahe_{abs(hash(url))%10000}"
                buy_keywords = ["立即购买", "选座购买", "立即预订", "有票"]
                sold_keywords = ["已售罄", "售罄", "缺货登记", "等待开售"]
            elif "motianlun.cn" in url:
                platform = "generic"
                m = re.search(r"showId=(\w+)", url)
                item_id = m.group(1) if m else f"mtl_{abs(hash(url))%10000}"
                buy_keywords = ["立即购买", "选座购买", "去购买", "有票"]
                sold_keywords = ["已售罄", "售罄", "缺货登记", "已下架"]
            elif "ypiao.com" in url:
                platform = "generic"
                m = re.search(r"t_(\d+)", url)
                item_id = m.group(1) if m else f"ypiao_{abs(hash(url))%10000}"
                buy_keywords = ["立即购买", "去购买", "立即预订", "我要买票", "有票"]
                sold_keywords = ["已售罄", "售罄", "缺货登记", "下架", "已下架"]
            elif "douyin.com" in url and "video" in url:
                platform = "douyin"
                import hashlib
                item_id = hashlib.md5(url.encode()).hexdigest()[:12]
                buy_keywords = ["立即购买", "立即抢购", "马上抢"]
                sold_keywords = ["已售罄", "已抢光", "抢光了"]
            else:
                continue

            # 提取名称/城市/日期
            # 在 URL 附近搜索上下文
            url_pos = text.find(url)
            ctx = text[max(0, url_pos-300):url_pos+100] if url_pos > 0 else ""
            clean_ctx = re.sub(r"<[^>]+>", " ", ctx)

            city = ""
            city_m = CITIES_PATTERN.search(clean_ctx)
            if city_m:
                city = city_m.group(0)

            date_str = ""
            date_m = DATE_PATTERN.search(clean_ctx)
            if date_m:
                date_str = date_m.group(0)

            # 名称：优先从 URL 路径提取
            name = ""
            if "dahepiao" in url:
                path_m = re.search(r"/(?:yc|yanchu)/([^/]+)", url)
                if path_m:
                    name = path_m.group(1).replace("xunyanchanghui", "巡回演唱会")
                else:
                    name = f"{singer}演出（大河票务）"
            elif "motianlun" in url:
                name = f"{singer}演出（摩天轮）"
            elif "ypiao" in url:
                name = f"{singer}演出（有票网）"
            else:
                name = f"{singer}演出"

            results.append({
                "name": name[:80],
                "city": city,
                "time": date_str,
                "platform": platform,
                "item_id": item_id,
                "url": url,
                "url_mobile": url,
                "buy_url": url,
                "buy_keywords": buy_keywords,
                "sold_keywords": sold_keywords,
                "mode": "page",
                "keywords": singer,
            })

    except Exception as e:
        log.warning(f"搜狗 [{singer}]: {e}")

    return results


def load_existing() -> list[dict]:
    if CACHE_PATH.exists():
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def merge(existing: list[dict], new: list[dict]) -> list[dict]:
    """合并去重：相同平台+item_id 只保留一条；同歌手同城优先保留名称更具体的"""
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

    # 去重泛化名称：同歌手同城，优先保留名称更具体的
    groups = {}
    for item in merged:
        group_key = f"{item.get('keywords','')}_{item.get('city','')}_{item.get('platform','')}"
        if group_key not in groups:
            groups[group_key] = []
        groups[group_key].append(item)

    cleaned = []
    for items in groups.values():
        generic_items = []
        specific_items = []
        for item in items:
            name = item.get("name", "")
            if re.match(r"^[一-鿿]{1,4}演出", name):
                generic_items.append(item)
            else:
                specific_items.append(item)
        if specific_items:
            cleaned.extend(specific_items)
        else:
            cleaned.extend(generic_items)

    cleaned.sort(key=lambda x: (x.get("keywords", ""), x.get("city", "")))
    return cleaned


def main():
    t0 = time.time()
    log.info("搜索缓存更新 — 票星球HTTP + 搜狗聚合模式")
    existing = load_existing()
    log.info(f"现有缓存: {len(existing)} 条")

    all_new = []
    for i, singer in enumerate(SINGERS):
        # 票星球搜索
        xq_results = search_xingqiupiao(singer)
        if xq_results:
            all_new.extend(xq_results)
            log.info(f"[{i+1}/{len(SINGERS)}] {singer}: 票星球 {len(xq_results)} 个")

        # 搜狗搜索（仅在票星球无结果时补充）
        if not xq_results:
            sg_results = search_sogou(singer)
            if sg_results:
                all_new.extend(sg_results)
                log.info(f"[{i+1}/{len(SINGERS)}] {singer}: 搜狗 {len(sg_results)} 个")

        time.sleep(0.3)

    merged = merge(existing, all_new)
    elapsed = time.time() - t0
    found_singers = len(set(item.get("keywords", "") for item in merged))
    log.info(f"新增: {len(all_new)} 条, 合并后: {len(merged)} 条 / {found_singers} 位歌手 / {elapsed:.0f}s")

    # 确保目录存在
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    count_file = BASE_DIR / "cache_update_count.txt"
    count_file.write_text(str(len(all_new)))
    if all_new:
        log.info(f"::notice::新增 {len(all_new)} 条搜索结果")


if __name__ == "__main__":
    main()
