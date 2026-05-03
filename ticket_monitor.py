#!/usr/bin/env python3
"""
演出票务回流票监控通知工具 v2.0
并发检测 + Playwright 渲染 + 多通道通知 + 智能去重
"""

import json
import logging
import os
import re
import sys
import time
import hashlib
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# ── 终极窗口压制：环境变量级封堵，确保 Chromium/Playwright 无法创建任何可见窗口 ──
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "0")
os.environ["DISPLAY"] = ":99"
os.environ["BROWSER"] = "none"
os.environ["CHROMIUM_FLAGS"] = "--headless=new --no-sandbox --disable-gpu --disable-software-rasterizer"
os.environ["NO_CONSOLE"] = "1"

# ── 路径 ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "monitor.log"
CACHE_PATH = BASE_DIR / "public" / "search_cache.json"

# ── 日志（自动轮转，单文件最大 5MB，保留 3 个备份）────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
    ],
)
# 彻底禁用 stdout/stderr 输出，防止弹出控制台窗口
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')
log = logging.getLogger("ticket_monitor")

# ── HTTP 会话池 ────────────────────────────────────────
def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    })
    return s

# ── 状态常量 ───────────────────────────────────────────
STATUS_AVAILABLE = "available"
STATUS_SOLD_OUT = "sold_out"
STATUS_UNKNOWN = "unknown"

# ═══════════════════════════════════════════════════════
#  通知模块
# ═══════════════════════════════════════════════════════

def send_pushplus(title: str, message: str, token: str) -> bool:
    if not token or token == "你的PushPlusToken":
        return False
    try:
        resp = requests.post(
            "http://www.pushplus.plus/send",
            json={"token": token, "title": title, "content": message, "template": "html"},
            timeout=10,
        )
        if resp.json().get("code") == 200:
            return True
        log.warning(f"PushPlus 失败: {resp.json()}")
        return False
    except Exception as e:
        log.warning(f"PushPlus 请求失败: {e}")
        return False

def send_bark(title: str, message: str, bark_key: str, app_url: str = "") -> bool:
    if not bark_key or bark_key == "你的BarkKey":
        return False
    try:
        url = f"https://api.day.app/{bark_key}/{quote(title)}/{quote(message)}"
        if app_url:
            url += f"?url={quote(app_url, safe='')}"
        resp = requests.get(url, timeout=10)
        if resp.json().get("code") == 200:
            return True
        log.warning(f"Bark 失败: {resp.json()}")
        return False
    except Exception as e:
        log.warning(f"Bark 请求失败: {e}")
        return False

def send_dingtalk(title: str, message: str, webhook: str) -> bool:
    """钉钉机器人 webhook 通知"""
    if not webhook or webhook == "你的钉钉Webhook":
        return False
    try:
        # 钉钉限制 title 用 Markdown 格式
        text_msg = re.sub(r'<[^>]+>', '', message)
        resp = requests.post(webhook, json={
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"## {title}\n\n{text_msg}\n\n> 票务监控 {datetime.now().strftime('%H:%M:%S')}"}
        }, timeout=10)
        if resp.json().get("errcode") == 0:
            return True
        log.warning(f"钉钉通知失败: {resp.json()}")
        return False
    except Exception as e:
        log.warning(f"钉钉请求失败: {e}")
        return False

def send_feishu(title: str, message: str, webhook: str) -> bool:
    """飞书机器人 webhook 通知"""
    if not webhook or webhook == "你的飞书Webhook":
        return False
    try:
        text_msg = re.sub(r'<[^>]+>', '', message)
        resp = requests.post(webhook, json={
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
                "elements": [{"tag": "markdown", "content": text_msg}]
            }
        }, timeout=10)
        if resp.json().get("code") == 0:
            return True
        log.warning(f"飞书通知失败: {resp.json()}")
        return False
    except Exception as e:
        log.warning(f"飞书请求失败: {e}")
        return False

def notify_all(title: str, html_msg: str, cfg: dict):
    """多通道通知广播（仅手机通道，不弹桌面窗口）"""
    text_msg = re.sub(r'<[^>]+>', '', html_msg)
    buy_url = cfg.get("buy_url", "")
    app_url = cfg.get("buy_url_mobile", buy_url)

    # 不再弹 Windows Toast — 所有通知走手机通道
    if pt := cfg.get("pushplus_token"):
        send_pushplus(title, html_msg, pt)
    if bk := cfg.get("bark_key"):
        send_bark(title, text_msg, bk, app_url)
    if dw := cfg.get("dingtalk_webhook"):
        send_dingtalk(title, html_msg, dw)
    if fw := cfg.get("feishu_webhook"):
        send_feishu(title, html_msg, fw)

# ═══════════════════════════════════════════════════════
#  状态管理
# ═══════════════════════════════════════════════════════

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            log.warning("state.json 损坏，重建")
    return {}

def save_state(state: dict):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ═══════════════════════════════════════════════════════
#  HTTP 工具
# ═══════════════════════════════════════════════════════

def fetch_page(url: str, timeout: int = 15, retries: int = 3) -> Optional[str]:
    """HTTP GET 页面，带指数退避重试"""
    for attempt in range(retries):
        try:
            resp = _new_session().get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                log.debug(f"[重试 {attempt+1}/{retries}] {url} — {wait}s 后重试")
                time.sleep(wait)
            else:
                log.error(f"请求失败 [{url}]: {e}")
    return None

def fetch_api(url: str, headers: dict = None, timeout: int = 15) -> Optional[dict]:
    try:
        s = _new_session()
        if headers:
            s.headers.update(headers)
        resp = s.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"API 请求失败 [{url}]: {e}")
        return None

def find_in_html(html: str, keywords: list[str]) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    return any(kw in text for kw in keywords)

def find_in_json(data, keywords: list[str]) -> bool:
    if isinstance(data, dict):
        for v in data.values():
            if find_in_json(v, keywords):
                return True
    elif isinstance(data, list):
        for item in data:
            if find_in_json(item, keywords):
                return True
    elif isinstance(data, str):
        return any(kw in data for kw in keywords)
    return False

# ═══════════════════════════════════════════════════════
#  平台检测器
# ═══════════════════════════════════════════════════════

def check_damai(item_id: str) -> tuple[str, str]:
    """
    大麦检测：优先 Playwright 渲染，失败回退到 HTTP 纯文本。
    Playwright 可准确判断按钮是否可点击，解决 JS 渲染导致的"无法确定"。
    """
    detail = f"大麦 item={item_id}"
    page_url = f"https://detail.damai.cn/item.htm?id={item_id}"

    # ── 方案 A：Playwright 渲染 ──
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--headless=new",
                "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--mute-audio",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-default-apps",
                "--hide-scrollbars",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-notifications",
                "--disable-popup-blocking",
                "--window-size=1024,768",
            ])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )
            page = context.new_page()
            page.goto(page_url, wait_until="domcontentloaded", timeout=15000)
            try:
                page.wait_for_selector("body", timeout=3000)
                page.wait_for_timeout(1500)
            except Exception:
                pass

            html = page.content()
            browser.close()

            if not html or len(html) < 500:
                raise Exception("Playwright 返回内容过短")
    except Exception as e:
        log.debug(f"Playwright 渲染失败: {e}，回退到 HTTP 模式")
        # ── 方案 B：HTTP 回退 ──
        html = fetch_page(page_url)
        if not html:
            return STATUS_UNKNOWN, f"{detail} → 页面请求失败"

    # ── 第一优先级：硬性售罄信号 ──
    if "缺货登记" in html:
        return STATUS_SOLD_OUT, f"{detail} → 已售罄（缺货登记）"

    # ── 第二优先级：尚未开售 ──
    if find_in_html(html, ["即将开售", "预约抢购", "提交开售提醒", "开售提醒"]):
        return STATUS_UNKNOWN, f"{detail} → 尚未开售 / 等待开售"

    # ── 第三优先级：buyFlag ──
    buy_flag_true = any(x in html for x in ["window.buyFlag = true", "window.buyFlag=true", '"buyFlag":true'])
    buy_flag_false = any(x in html for x in ["window.buyFlag = false", "window.buyFlag=false", '"buyFlag":false'])

    if buy_flag_false and not buy_flag_true:
        return STATUS_SOLD_OUT, f"{detail} → 已售罄"

    # ── 第四优先级：按钮状态分析 ──
    has_sold = find_in_html(html, ["已售罄", "暂无可售", "已下架"])
    has_buy = find_in_html(html, ["立即购买", "选座购买", "立即预订"])

    # 有活跃购买按钮 + buyFlag true + 无售罄词 → 有票
    if buy_flag_true and _has_active_button(html) and not has_sold:
        return STATUS_AVAILABLE, f"{detail} → 有票！"

    # 有售罄词 + 无活跃按钮 → 售罄
    if has_sold and not _has_active_button(html):
        return STATUS_SOLD_OUT, f"{detail} → 已售罄"

    # 有购买词但无 buyFlag 确认
    if has_buy and not has_sold and not buy_flag_false:
        return STATUS_UNKNOWN, f"{detail} → 疑似有票（待确认）"

    return STATUS_UNKNOWN, f"{detail} → 暂无法判断"

def _has_active_button(html: str) -> bool:
    """检查是否有可点击的购买按钮"""
    soup = BeautifulSoup(html, "html.parser")
    buy_texts = ["立即购买", "选座购买", "立即预订"]

    for element in soup.find_all(string=lambda t: t and any(kw in str(t) for kw in buy_texts)):
        parent = element.parent
        for _ in range(3):
            if parent is None:
                break
            classes = " ".join(parent.get("class", [])).lower()
            style = str(parent.get("style", "")).lower()
            if any(bad in classes for bad in ["disabled", "gray", "cant-buy", "unable", "not-available"]):
                break
            if parent.get("disabled") is not None:
                break
            if "display:none" in style or "visibility:hidden" in style:
                break
            parent = parent.parent
        else:
            return True
    return False

def check_maoyan(show_id: str) -> tuple[str, str]:
    detail = f"猫眼 show={show_id}"
    page_url = f"https://show.maoyan.com/qq/detail/{show_id}"
    html = fetch_page(page_url)
    if not html:
        return STATUS_UNKNOWN, f"{detail} → 页面请求失败"

    has_buy = find_in_html(html, ["立即购票", "选座购票", "立即预订"])
    has_sold = find_in_html(html, ["已售罄", "暂时无货"])

    if has_sold and not has_buy:
        return STATUS_SOLD_OUT, f"{detail} → 已售罄"
    if has_buy:
        return STATUS_AVAILABLE, f"{detail} → 有票/可购买"
    return STATUS_UNKNOWN, f"{detail} → 无法确定"

def check_showstart(event_id: str) -> tuple[str, str]:
    detail = f"秀动 event={event_id}"
    page_url = f"https://www.showstart.com/event/{event_id}"
    html = fetch_page(page_url)
    if not html:
        return STATUS_UNKNOWN, f"{detail} → 页面请求失败"

    has_buy = find_in_html(html, ["立即购票", "立即购买"])
    has_sold = find_in_html(html, ["已售罄", "售罄", "已结束"])

    if has_buy and not has_sold:
        return STATUS_AVAILABLE, f"{detail} → 有票/可购买"
    if has_sold and not has_buy:
        return STATUS_SOLD_OUT, f"{detail} → 已售罄"
    return STATUS_UNKNOWN, f"{detail} → 无法确定"

def check_douyin(cfg: dict) -> tuple[str, str]:
    url = cfg.get("buy_url") or cfg.get("url", "")
    label = cfg.get("label", "抖音")
    detail = f"抖音 {label}"

    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.47",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.douyin.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return STATUS_UNKNOWN, f"{detail} → HTTP {resp.status_code}"
        html = resp.text
    except requests.RequestException as e:
        return STATUS_UNKNOWN, f"{detail} → 请求失败: {e}"

    buy_kw = cfg.get("buy_keywords", ["立即购买", "立即抢购", "马上抢", "去购买", "立即预订", "提交订单"])
    sold_kw = cfg.get("sold_keywords", ["已售罄", "已抢光", "抢光了", "已结束", "暂时无货", "缺货", "已下架"])

    has_buy = any(kw in html for kw in buy_kw) or find_in_html(html, buy_kw)
    has_sold = any(kw in html for kw in sold_kw) or find_in_html(html, sold_kw)

    if has_buy and not has_sold:
        return STATUS_AVAILABLE, f"{detail} → 有票/可购买"
    if has_sold and not has_buy:
        return STATUS_SOLD_OUT, f"{detail} → 已售罄"
    if len(html) < 500:
        return STATUS_UNKNOWN, f"{detail} → SPA壳/反爬页"
    return STATUS_UNKNOWN, f"{detail} → 无法确定"

def check_generic(cfg: dict) -> tuple[str, str]:
    url = cfg["url"]
    mode = cfg.get("mode", "page")
    buy_kw = cfg.get("buy_keywords", [])
    sold_kw = cfg.get("sold_keywords", [])

    if mode == "api":
        data = fetch_api(url, headers=cfg.get("headers"))
        if data is None:
            return STATUS_UNKNOWN, f"{url} → API 请求失败"
        has_buy = find_in_json(data, buy_kw)
        has_sold = find_in_json(data, sold_kw)
    else:
        html = fetch_page(url)
        if html is None:
            return STATUS_UNKNOWN, f"{url} → 页面请求失败"
        has_buy = any(kw in html for kw in buy_kw) or find_in_html(html, buy_kw)
        has_sold = any(kw in html for kw in sold_kw) or find_in_html(html, sold_kw)

    if has_buy and not has_sold:
        return STATUS_AVAILABLE, f"{url} → 有票/可购买"
    if has_sold and not has_buy:
        return STATUS_SOLD_OUT, f"{url} → 已售罄"
    if has_buy and has_sold:
        return STATUS_AVAILABLE, f"{url} → 可能有票（关键词交叉命中）"
    return STATUS_UNKNOWN, f"{url} → 无法确定"

# ═══════════════════════════════════════════════════════
#  核心检测调度
# ═══════════════════════════════════════════════════════

def check_single(cfg: dict) -> dict:
    """检测单个监控项，返回结果字典（线程安全）"""
    monitor_id = cfg["id"]
    platform = cfg.get("platform", "generic")
    label = cfg.get("label", monitor_id)
    start = time.time()

    try:
        if platform == "damai":
            status, message = check_damai(cfg.get("item_id", ""))
        elif platform == "maoyan":
            status, message = check_maoyan(cfg.get("show_id", ""))
        elif platform == "showstart":
            status, message = check_showstart(cfg.get("event_id", ""))
        elif platform == "douyin":
            status, message = check_douyin(cfg)
        else:
            status, message = check_generic(cfg)
    except Exception as e:
        log.error(f"[{label}] 检测异常: {e}")
        status, message = STATUS_UNKNOWN, f"异常: {e}"

    elapsed = time.time() - start
    return {
        "cfg": cfg, "status": status, "message": message,
        "label": label, "elapsed": elapsed,
    }

def process_result(result: dict, state: dict, global_cfg: dict):
    """处理单个检测结果，更新状态并通知"""
    cfg = result["cfg"]
    monitor_id = cfg["id"]
    status = result["status"]
    message = result["message"]
    label = result["label"]

    log.info(f"[{label}] {message} ({result['elapsed']:.1f}s)")

    prev = state.get(monitor_id, {})
    prev_status = prev.get("status", "")
    now = datetime.now()
    remind_interval = int(global_cfg.get("remind_interval_seconds", 300))
    buy_url_mobile = cfg.get("buy_url_mobile", cfg.get("buy_url", ""))

    # 通知配置注入到 cfg
    for key in ("pushplus_token", "bark_key", "dingtalk_webhook", "feishu_webhook"):
        if key not in cfg:
            cfg[key] = global_cfg.get(key, "")

    def _build_msg(status_line: str, extra: str) -> str:
        parts = [status_line, extra]
        if buy_url := cfg.get("buy_url", ""):
            parts.append(
                f'<br><br><a href="{buy_url}">👉 点击立即购票</a>'
                f'<br><span style="color:#999;font-size:12px">'
                f'如提示渠道不支持，请点右上角 … 选择「在浏览器中打开」</span>'
            )
        return "<br>".join(parts)

    # 首次记录
    if not prev:
        state[monitor_id] = {
            "status": status, "message": message,
            "last_change": now.isoformat(), "last_remind": "", "remind_count": 0,
        }
        if status == STATUS_AVAILABLE:
            log.info(f"🎫 [{label}] 当前有票！")
            time_str = now.strftime("%H:%M:%S")
            notify_all(f"🎫 有票！— {label}", _build_msg(message, f"时间: {time_str}"), cfg)
            state[monitor_id]["last_remind"] = now.isoformat()
        return

    # 状态变化
    if status != prev_status:
        state[monitor_id] = {
            "status": status, "message": message,
            "last_change": now.isoformat(), "last_remind": prev.get("last_remind", ""), "remind_count": 0,
        }
        if status == STATUS_AVAILABLE:
            log.info(f"🎫 [{label}] 状态变化：{prev_status} → 有票！")
            time_str = now.strftime("%H:%M:%S")
            notify_all(f"🎫 有票了！— {label}", _build_msg(message, f"状态变化通知<br>时间: {time_str}"), cfg)
            state[monitor_id]["last_remind"] = now.isoformat()
        elif prev_status:
            log.info(f"[{label}] 状态变化：{prev_status} → {status}")
        return

    # 持续有票 → 周期性提醒
    if status == STATUS_AVAILABLE:
        last_remind_str = prev.get("last_remind", "")
        if last_remind_str:
            last_remind = datetime.fromisoformat(last_remind_str)
            if (now - last_remind).total_seconds() >= remind_interval:
                cnt = prev.get("remind_count", 0) + 1
                time_str = now.strftime("%H:%M:%S")
                elapsed_s = int((now - last_remind).total_seconds())
                log.info(f"🔔 [{label}] 持续有票，距上次提醒 {elapsed_s}s，第{cnt}次提醒")
                notify_all(f"🔔 仍有票！— {label}", _build_msg(message, f"第 {cnt} 次提醒<br>时间: {time_str}"), cfg)
                state[monitor_id]["last_remind"] = now.isoformat()
                state[monitor_id]["remind_count"] = cnt

# ═══════════════════════════════════════════════════════
#  监控项管理
# ═══════════════════════════════════════════════════════

def load_global_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"monitors": [], "check_interval_seconds": 30, "remind_interval_seconds": 300}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def load_cache_monitors() -> list[dict]:
    if not CACHE_PATH.exists():
        return []
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        cache = json.load(f)
    monitors = []
    for item in cache:
        monitors.append({
            "id": f"auto_{item.get('platform','generic')}_{item.get('item_id','')}",
            "enabled": True,
            "platform": item.get("platform", "generic"),
            "mode": item.get("mode", "page"),
            "label": str(item.get("name", ""))[:60],
            "url": item.get("url", ""),
            "item_id": item.get("item_id", ""),
            "buy_url": item.get("buy_url", item.get("url", "")),
            "buy_url_mobile": item.get("url_mobile", item.get("buy_url", item.get("url", ""))),
            "buy_keywords": item.get("buy_keywords", ["立即购买", "选座购买", "立即预订"]),
            "sold_keywords": item.get("sold_keywords", ["已售罄", "售罄", "缺货登记"]),
            "comment": f'{item.get("city","")} · {item.get("time","")} · 自动发现',
        })
    return monitors

def merge_monitors(config_monitors: list[dict], cache_monitors: list[dict]) -> list[dict]:
    """
    智能合并：同 item_id + platform 去重，手动配置优先。
    对于手动已配置的 damai item_id，跳过缓存的同名自动项。
    """
    seen = set()
    result = []

    # 手动配置项先加入（优先级高），记录去重 key
    manual_keys = set()
    for m in config_monitors:
        if not m.get("enabled", True):
            continue
        key = f"{m.get('platform','')}_{m.get('item_id','')}_{m.get('url','')}"
        if key not in seen:
            seen.add(key)
            result.append(m)
            manual_keys.add(key)

    # 缓存项：跳过已手动覆盖的
    for m in cache_monitors:
        if not m.get("enabled", True):
            continue
        key = f"{m.get('platform','')}_{m.get('item_id','')}_{m.get('url','')}"
        if key in seen:
            continue
        # 额外的平台级去重：手动 damai item_id 已覆盖同名缓存
        platform = m.get("platform", "")
        item_id = m.get("item_id", "")
        if platform == "damai" and any(k.startswith(f"damai_{item_id}_") for k in manual_keys):
            continue
        seen.add(key)
        result.append(m)

    return result

def reload_cache_if_stale(cache_monitors: list[dict], last_reload: float) -> tuple[list[dict], float, bool]:
    """每 5 分钟自动重载搜索缓存"""
    if time.time() - last_reload < 300:
        return cache_monitors, last_reload, False
    new_cache = load_cache_monitors()
    if len(new_cache) > len(cache_monitors):
        log.info(f"缓存更新：{len(cache_monitors)} → {len(new_cache)} 条")
        return new_cache, time.time(), True
    return cache_monitors, time.time(), False

# ═══════════════════════════════════════════════════════
#  主循环
# ═══════════════════════════════════════════════════════

def run_loop(once: bool = False):
    log.info("=" * 50)
    log.info("Ticket Monitor v2.0 启动")
    log.info("=" * 50)

    global_cfg = load_global_config()
    cache_monitors = load_cache_monitors()
    config_monitors = global_cfg.get("monitors", [])
    monitors = merge_monitors(config_monitors, cache_monitors)

    if not monitors:
        log.error("没有可用的监控项。请先配置 config.json 或运行 update_search_cache.py")
        return

    # 提取全局通知配置
    notifier_cfg = {
        "pushplus_token": os.environ.get("PUSHPLUS_TOKEN") or global_cfg.get("pushplus_token", ""),
        "bark_key": os.environ.get("BARK_KEY") or global_cfg.get("bark_key", ""),
        "dingtalk_webhook": os.environ.get("DINGTALK_WEBHOOK") or global_cfg.get("dingtalk_webhook", ""),
        "feishu_webhook": os.environ.get("FEISHU_WEBHOOK") or global_cfg.get("feishu_webhook", ""),
    }
    # 合并到 global_cfg 方便 process_result 使用
    global_cfg.update(notifier_cfg)

    check_interval = int(global_cfg.get("check_interval_seconds", 30))
    max_workers = min(len(monitors), 8)  # 最多 8 并发

    # 通知状态
    if notifier_cfg["pushplus_token"] and notifier_cfg["pushplus_token"] != "你的PushPlusToken":
        log.info(f"PushPlus 微信通知已启用，每 {global_cfg.get('remind_interval_seconds', 300)}s 提醒一次")
    else:
        log.info("PushPlus 微信通知未配置，仅使用桌面弹窗")
    if notifier_cfg["bark_key"] and notifier_cfg["bark_key"] != "你的BarkKey":
        log.info("Bark iOS 通知已启用")
    if notifier_cfg["dingtalk_webhook"] and notifier_cfg["dingtalk_webhook"] != "你的钉钉Webhook":
        log.info("钉钉通知已启用")
    if notifier_cfg["feishu_webhook"] and notifier_cfg["feishu_webhook"] != "你的飞书Webhook":
        log.info("飞书通知已启用")

    state = load_state()
    check_count = 0
    last_cache_reload = time.time()

    log.info(f"已加载 {len(monitors)} 个监控项（手动 {len(config_monitors)} + 缓存 {len(cache_monitors)}），并发度 {max_workers}")

    try:
        while True:
            check_count += 1

            # 缓存热重载
            cache_monitors, last_cache_reload, changed = reload_cache_if_stale(cache_monitors, last_cache_reload)
            if changed:
                monitors = merge_monitors(config_monitors, cache_monitors)
                log.info(f"监控项更新：{len(monitors)} 个")

            round_start = time.time()
            log.info(f"--- 第 {check_count} 轮（{len(monitors)}项/{max_workers}并发）---")

            # 并发检测
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(check_single, m): m for m in monitors}
                for future in as_completed(futures):
                    try:
                        result = future.result(timeout=30)
                        process_result(result, state, global_cfg)
                    except Exception as e:
                        cfg = futures[future]
                        log.error(f"[{cfg.get('label', cfg.get('id','?'))}] 检测超时或异常: {e}")

            save_state(state)
            elapsed = time.time() - round_start
            log.info(f"本轮耗时 {elapsed:.1f}s，等待 {check_interval}s 后下一轮...")

            if once:
                break
            time.sleep(max(0, check_interval))
    except KeyboardInterrupt:
        log.info("用户终止，保存状态退出。")
        save_state(state)

# ═══════════════════════════════════════════════════════
#  CLI 管理
# ═══════════════════════════════════════════════════════

def cmd_list():
    """列出所有监控项"""
    global_cfg = load_global_config()
    cache = load_cache_monitors()
    manual = global_cfg.get("monitors", [])
    all_items = merge_monitors(manual, cache)

    print(f"\n{'='*80}")
    print(f"  票务监控项列表（共 {len(all_items)} 项）")
    print(f"{'='*80}")
    print(f"{'ID':<45} {'平台':<10} {'状态':<10} {'标签'}")
    print(f"{'-'*45} {'-'*10} {'-'*10} {'-'*20}")

    manual_ids = {m["id"] for m in manual}
    state = load_state()
    for m in all_items:
        mid = m["id"][:44]
        plat = m.get("platform", "?")[:9]
        st = state.get(m["id"], {}).get("status", "new")[:9]
        label = m.get("label", "")[:30]
        src = "手动" if m["id"] in manual_ids else "缓存"
        print(f"{mid:<45} {plat:<10} {st:<10} {label} [{src}]")

    print(f"{'='*80}\n")

def cmd_add(args):
    """添加监控项"""
    cfg = load_global_config()
    if "monitors" not in cfg:
        cfg["monitors"] = []

    new_item = {
        "id": args.id,
        "enabled": True,
        "platform": args.platform or "generic",
        "label": args.label or args.id,
        "url": args.url or "",
        "item_id": args.item_id or "",
        "buy_url": args.buy_url or args.url or "",
        "buy_url_mobile": args.buy_url_mobile or args.buy_url or args.url or "",
        "buy_keywords": args.buy_keywords.split(",") if args.buy_keywords else ["立即购买", "选座购买", "立即预订"],
        "sold_keywords": args.sold_keywords.split(",") if args.sold_keywords else ["已售罄", "售罄", "缺货登记"],
        "comment": args.comment or "",
    }
    if args.mode:
        new_item["mode"] = args.mode

    # 检查重复
    for m in cfg["monitors"]:
        if m["id"] == args.id:
            print(f"错误: ID '{args.id}' 已存在")
            return

    cfg["monitors"].append(new_item)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"已添加监控项: {args.id}")

def cmd_remove(args):
    """删除监控项"""
    cfg = load_global_config()
    before = len(cfg.get("monitors", []))
    cfg["monitors"] = [m for m in cfg.get("monitors", []) if m["id"] != args.id]
    after = len(cfg["monitors"])

    if before == after:
        print(f"未找到监控项: {args.id}")
        return

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"已删除监控项: {args.id}")

def cmd_enable(args):
    """启用/禁用监控项"""
    cfg = load_global_config()
    for m in cfg.get("monitors", []):
        if m["id"] == args.id:
            m["enabled"] = args.enable
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            print(f"已{'启用' if args.enable else '禁用'}: {args.id}")
            return
    print(f"未找到监控项: {args.id}")

# ═══════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="演出票务回流票监控 v2.0")
    sub = parser.add_subparsers(dest="command")

    # --once / 默认循环
    parser.add_argument("--once", action="store_true", help="单次检查（CI/CD 模式）")

    # list
    sub.add_parser("list", help="列出所有监控项")

    # add
    p_add = sub.add_parser("add", help="添加监控项")
    p_add.add_argument("--id", required=True, help="唯一 ID")
    p_add.add_argument("--platform", choices=["damai","maoyan","showstart","douyin","generic"], help="平台")
    p_add.add_argument("--label", help="显示标签")
    p_add.add_argument("--url", help="页面/API URL")
    p_add.add_argument("--item-id", help="平台 item ID")
    p_add.add_argument("--buy-url", help="购票链接")
    p_add.add_argument("--buy-url-mobile", help="移动端购票链接")
    p_add.add_argument("--buy-keywords", help="有票关键词（逗号分隔）")
    p_add.add_argument("--sold-keywords", help="售罄关键词（逗号分隔）")
    p_add.add_argument("--mode", choices=["page","api"], help="检测模式")
    p_add.add_argument("--comment", help="备注")

    # remove
    p_rm = sub.add_parser("remove", help="删除监控项")
    p_rm.add_argument("--id", required=True, help="要删除的 ID")

    # enable / disable
    p_en = sub.add_parser("enable", help="启用监控项")
    p_en.add_argument("--id", required=True, help="监控项 ID")
    p_en.add_argument("--enable", type=bool, default=True, help="启用/禁用")

    args = parser.parse_args()

    if args.command == "list":
        cmd_list()
    elif args.command == "add":
        cmd_add(args)
    elif args.command == "remove":
        cmd_remove(args)
    elif args.command == "enable":
        cmd_enable(args)
    elif args.once:
        run_loop(once=True)
    else:
        run_loop(once=False)

if __name__ == "__main__":
    main()
