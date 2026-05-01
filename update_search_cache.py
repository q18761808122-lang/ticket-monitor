#!/usr/bin/env python3
"""
搜索缓存更新脚本
由 GitHub Actions 定期运行，搜索热门演出 → 更新 public/search_cache.json
"""
import json
import logging
import sys
import time
import urllib.parse
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "public" / "search_cache.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cache_updater")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://search.damai.cn/",
})

# 热门搜索关键词
HOT_KEYWORDS = [
    "薛之谦", "周杰伦", "林俊杰", "五月天", "陈奕迅", "邓紫棋",
    "张杰", "华晨宇", "蔡依林", "王菲", "张学友", "刘德华",
    "李宗盛", "张惠妹", "周深", "张信哲", "陶喆", "梁静茹",
]


def search_damai(keyword: str) -> list[dict]:
    """通过大麦搜索 AJAX API 搜索演出"""
    results = []
    try:
        # 先访问首页获取 cookie
        session.get("https://www.damai.cn/", timeout=15)

        params = {
            "keyword": keyword,
            "ctl": "1",
            "page": "1",
            "ts": str(int(time.time() * 1000)),
        }
        url = "https://search.damai.cn/searchajax.html?" + urllib.parse.urlencode(params)
        resp = session.get(url, timeout=15)

        if resp.status_code != 200 or len(resp.text) < 100:
            log.warning(f"搜索 [{keyword}] 异常: status={resp.status_code}, len={len(resp.text)}")
            return results

        if "x5secdata" in resp.text or "punish" in resp.text:
            log.warning(f"搜索 [{keyword}] 被反爬拦截")
            return results

        data = resp.json()
        items = data.get("pageData", {}).get("resultData", [])

        for item in items:
            item_id = str(item.get("id", ""))
            if not item_id:
                continue
            results.append({
                "name": item.get("name", f"{keyword} 演出"),
                "city": item.get("cityName", item.get("venueCity", "")),
                "time": item.get("showTime", ""),
                "platform": "damai",
                "item_id": item_id,
                "url": f"https://detail.damai.cn/item.htm?id={item_id}",
                "url_mobile": f"damai://V1/ProjectPage?id={item_id}",
                "buy_url": f"https://detail.damai.cn/item.htm?id={item_id}",
                "buy_keywords": ["立即购买", "立即预订", "选座购买"],
                "sold_keywords": ["缺货登记", "已售罄", "暂无可售"],
                "mode": "page",
                "keywords": keyword,
            })
        log.info(f"搜索 [{keyword}] 找到 {len(results)} 个结果")
    except Exception as e:
        log.error(f"搜索 [{keyword}] 失败: {e}")
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
    log.info("搜索缓存更新开始")
    existing = load_existing()
    log.info(f"现有: {len(existing)} 条")

    all_new = []
    for kw in HOT_KEYWORDS:
        results = search_damai(kw)
        all_new.extend(results)

    merged = merge(existing, all_new)
    log.info(f"新增: {len(all_new)} 条, 总计: {len(merged)} 条")

    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    log.info("缓存写入完成")
    if all_new:
        with open(BASE_DIR / "cache_update_count.txt", "w") as f:
            f.write(str(len(all_new)))
        log.info(f"::notice::新增 {len(all_new)} 条搜索结果")


if __name__ == "__main__":
    main()
