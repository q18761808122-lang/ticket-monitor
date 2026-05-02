#!/usr/bin/env python3
"""
演出票务回流票监控通知工具
只读检查页面/API → 状态变化 → Windows 桌面通知
不自动下单，不绕过任何安全机制。
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ── 路径 ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("ticket_monitor")

# ── HTTP 会话（模拟正常浏览器） ────────────────────────
session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
})


# ═══════════════════════════════════════════════════════
#  通知模块
# ═══════════════════════════════════════════════════════

def send_toast(title: str, message: str, url: str = "", open_browser: bool = False) -> bool:
    """Windows 托盘气泡通知，可选自动打开浏览器"""
    escaped_title = _escape_ps(title)
    escaped_msg = _escape_ps(message)
    escaped_url = _escape_ps(url)

    ps_script = f'''
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$icon = [System.Drawing.SystemIcons]::Information
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = $icon
$notify.Visible = $true
$notify.BalloonTipTitle = "{escaped_title}"
$notify.BalloonTipText = "{escaped_msg}"
$notify.BalloonTipIcon = "Info"
$notify.ShowBalloonTip(10000)
Start-Sleep -Seconds 12
$notify.Visible = $false
$notify.Dispose()
'''
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if url and open_browser:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f'Start-Process "{escaped_url}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return True
    except Exception as e:
        log.warning(f"Toast 通知失败: {e}")
        return False


def send_wechat(title: str, message: str, token: str) -> bool:
    """通过 PushPlus 发送微信通知，支持 HTML 链接"""
    if not token or token == "你的PushPlusToken":
        return False
    try:
        resp = requests.post(
            "http://www.pushplus.plus/send",
            json={"token": token, "title": title, "content": message, "template": "html"},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 200:
            return True
        log.warning(f"微信通知失败: {data}")
        return False
    except Exception as e:
        log.warning(f"微信通知请求失败: {e}")
        return False


def send_bark(title: str, message: str, bark_key: str, app_url: str = "") -> bool:
    """通过 Bark 发送 iOS 推送通知，支持点击跳转 App（http://github.com/Finb/Bark）"""
    if not bark_key or bark_key == "你的BarkKey":
        return False
    try:
        # Bark 对中文需要 URL 编码
        from urllib.parse import quote
        url = f"https://api.day.app/{bark_key}/{quote(title)}/{quote(message)}"
        if app_url:
            url += f"?url={quote(app_url, safe='')}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("code") == 200:
            return True
        log.warning(f"Bark 通知失败: {data}")
        return False
    except Exception as e:
        log.warning(f"Bark 通知请求失败: {e}")
        return False


def notify(title: str, message: str, wechat_token: str = "", desktop_url: str = "", open_browser: bool = False, bark_key: str = "", app_url: str = ""):
    """同时发送桌面气泡、微信和 iOS Bark 通知。"""
    send_toast(title, message, desktop_url, open_browser)
    if wechat_token:
        send_wechat(title, message, wechat_token)
    if bark_key:
        send_bark(title, _bark_msg(message), bark_key, app_url)


def _bark_msg(html_msg: str) -> str:
    """将 HTML 消息转为纯文本供 Bark 使用"""
    import re
    return re.sub(r'<[^>]+>', '', html_msg).replace('<br>', '\n').replace('&gt;', '>').replace('&lt;', '<')


def _escape_ps(s: str) -> str:
    return s.replace("'", "''").replace("\n", " ").replace("\r", "")


def _build_wx_msg(status_line: str, extra: str, buy_url: str) -> str:
    """构建微信 HTML 消息，包含可点击的购票链接"""
    parts = [status_line, extra]
    if buy_url:
        parts.append(
            f'<br><br><a href="{buy_url}">👉 点击立即购票</a>'
            f'<br><span style="color:#999;font-size:12px">'
            f'如提示渠道不支持，请点右上角 … 选择「在浏览器中打开」即可跳转 App</span>'
        )
    return "<br>".join(parts)


# ═══════════════════════════════════════════════════════
#  状态管理
# ═══════════════════════════════════════════════════════

def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════
#  页面抓取
# ═══════════════════════════════════════════════════════

def fetch_page(url: str, timeout: int = 15) -> Optional[str]:
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log.error(f"请求失败 [{url}]: {e}")
        return None


def fetch_api(url: str, headers: dict = None, timeout: int = 15) -> Optional[dict]:
    try:
        h = {**session.headers, **(headers or {})}
        resp = requests.get(url, headers=h, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"API 请求失败 [{url}]: {e}")
        return None


def find_keywords_in_html(html: str, keywords: list[str]) -> bool:
    """检查 HTML 中是否包含任一关键词"""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    for kw in keywords:
        if kw in text:
            return True
    return False


def find_keywords_in_json(data: dict, keywords: list[str], path: str = "") -> bool:
    """递归在 JSON 中搜索关键词（值转为字符串后匹配）"""
    if isinstance(data, dict):
        for k, v in data.items():
            if find_keywords_in_json(v, keywords, f"{path}.{k}"):
                return True
    elif isinstance(data, list):
        for i, item in enumerate(data):
            if find_keywords_in_json(item, keywords, f"{path}[{i}]"):
                return True
    elif isinstance(data, str):
        for kw in keywords:
            if kw in data:
                return True
    return False


# ═══════════════════════════════════════════════════════
#  平台检测器
# ═══════════════════════════════════════════════════════

STATUS_AVAILABLE = "available"
STATUS_SOLD_OUT = "sold_out"
STATUS_UNKNOWN = "unknown"


def check_damai(item_id: str) -> tuple[str, str]:
    """
    大麦桌面版：https://detail.damai.cn/item.htm?id={item_id}

    判断优先级（避免假阳性）：
      1. 「缺货登记」→ 100% 售罄，直接返回
      2. 「已售罄」「暂无可售」+ 无「立即购买」→ 售罄
      3. 「即将开售」「提交开售提醒」→ 尚未开售
      4. 「立即购买」存在但按钮为 disabled/gray → 不可购买
      5. buyFlag=true + 有购买词 + 无售罄词 → 有票
    """
    detail = f"大麦 item={item_id}"
    page_url = f"https://detail.damai.cn/item.htm?id={item_id}"
    html = fetch_page(page_url)
    if not html:
        return STATUS_UNKNOWN, f"{detail} → 页面请求失败"

    # ── 第一优先级：100% 售罄信号 ──
    # 「缺货登记」是大麦最明确的售罄标志，一旦出现绝无可能买到
    if "缺货登记" in html:
        return STATUS_SOLD_OUT, f"{detail} → 已售罄（缺货登记）"

    # ── 第二优先级：尚未开售 ──
    if find_keywords_in_html(html, ["即将开售", "预约抢购", "提交开售提醒"]):
        return STATUS_UNKNOWN, f"{detail} → 尚未开售"

    # ── 第三优先级：检查售罄词（仅次于缺货登记） ──
    has_sold = find_keywords_in_html(html, ["已售罄", "暂无可售", "已下架"])
    has_buy = find_keywords_in_html(html, ["立即购买", "选座购买", "立即预订"])

    # 有售罄词且没有有效的购买按钮 → 售罄
    if has_sold and not _has_active_buy_button(html):
        return STATUS_SOLD_OUT, f"{detail} → 已售罄"

    # ── 第四优先级：检测 buyFlag ──
    buy_flag_true = "window.buyFlag = true" in html or "window.buyFlag=true" in html or '"buyFlag":true' in html
    buy_flag_false = "window.buyFlag = false" in html or "window.buyFlag=false" in html or '"buyFlag":false' in html

    # buyFlag 明确为 false → 售罄
    if buy_flag_false and not buy_flag_true:
        return STATUS_SOLD_OUT, f"{detail} → 已售罄"

    # ── 第五优先级：有票需同时满足三个条件 ──
    # 1. buyFlag 为 true（或未明确 false）
    # 2. 存在活跃的购买按钮
    # 3. 不存在任何售罄词
    if buy_flag_true and _has_active_buy_button(html) and not has_sold:
        return STATUS_AVAILABLE, f"{detail} → 有票"

    # 仅有购买文字但无 buyFlag 确认 → 不确定（可能是缓存/部分渲染）
    if has_buy and not has_sold and not buy_flag_false:
        return STATUS_UNKNOWN, f"{detail} → 疑似有票（无 buyFlag 确认）"

    return STATUS_UNKNOWN, f"{detail} → 无法确定状态"


def _has_active_buy_button(html: str) -> bool:
    """
    检查是否存在真正可点击的购买按钮，而非灰色/disabled 状态。
    大麦的购买按钮在不可点击时通常带有 disabled、gray、cant-buy 等标记。
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    buy_texts = ["立即购买", "选座购买", "立即预订"]

    # 查找包含购买文字的元素
    for element in soup.find_all(string=lambda t: t and any(kw in t for kw in buy_texts)):
        parent = element.parent
        # 向上查找 3 层，检查是否有 disabled/gray 标记
        for _ in range(3):
            if parent is None:
                break
            # 检查 class 和属性
            classes = " ".join(parent.get("class", []))
            attrs = " ".join(parent.attrs.keys()) if hasattr(parent, "attrs") else ""

            # 失效信号
            if any(x in classes.lower() for x in ["disabled", "gray", "cant-buy", "unable", "not-available"]):
                break
            if parent.get("disabled") is not None:
                break
            if "disable" in str(parent.get("style", "")).lower():
                break

            parent = parent.parent
        else:
            # 未发现失效标记 → 按钮可能有效
            return True

    return False


def check_maoyan(show_id: str) -> tuple[str, str]:
    """
    猫眼：页面关键词检测
    页面：https://show.maoyan.com/qq/detail/{show_id}
    """
    detail = f"猫眼 show={show_id}"
    page_url = f"https://show.maoyan.com/qq/detail/{show_id}"
    html = fetch_page(page_url)
    if html:
        has_buy = find_keywords_in_html(html, ["立即购票", "选座购票", "立即预订"])
        has_sold = find_keywords_in_html(html, ["已售罄", "暂时无货"])
        # 猫眼有时会在 JSON 数据中嵌入状态
        if "已售罄" in html and "立即购票" not in html:
            return STATUS_SOLD_OUT, f"{detail} → 已售罄"
        if has_buy:
            return STATUS_AVAILABLE, f"{detail} → 有票/可购买"

    return STATUS_UNKNOWN, f"{detail} → 无法确定状态"


def check_showstart(event_id: str) -> tuple[str, str]:
    """
    秀动：页面关键词检测
    页面：https://www.showstart.com/event/{event_id}
    """
    detail = f"秀动 event={event_id}"
    page_url = f"https://www.showstart.com/event/{event_id}"
    html = fetch_page(page_url)
    if html:
        has_buy = find_keywords_in_html(html, ["立即购票", "立即购买"])
        has_sold = find_keywords_in_html(html, ["已售罄", "售罄", "已结束"])

        if has_buy and not has_sold:
            return STATUS_AVAILABLE, f"{detail} → 有票/可购买"
        if has_sold and not has_buy:
            return STATUS_SOLD_OUT, f"{detail} → 已售罄"

    return STATUS_UNKNOWN, f"{detail} → 无法确定状态"


def check_douyin(cfg: dict) -> tuple[str, str]:
    """
    抖音票务检测：使用移动端 User-Agent 抓取页面，匹配购票/售罄关键词。
    抖音是重 JS 渲染的 SPA，简单 HTTP 可能拿不到完整内容。
    如持续返回「无法确定」，建议找到抖音小程序的 API 接口后改用 api 模式。
    """
    url = cfg.get("buy_url") or cfg.get("url", "")
    item_id = cfg.get("item_id", "")
    label = cfg.get("label", "抖音")

    # 抖音期望的移动端请求头
    douyin_headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
            "MicroMessenger/8.0.47"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.douyin.com/",
    }
    detail = f"抖音 {label}"

    try:
        resp = requests.get(url, headers=douyin_headers, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return STATUS_UNKNOWN, f"{detail} → HTTP {resp.status_code}"
        html = resp.text
    except requests.RequestException as e:
        log.error(f"[{detail}] 请求失败: {e}")
        return STATUS_UNKNOWN, f"{detail} → 页面请求失败"

    buy_keywords = cfg.get("buy_keywords", ["立即购买", "立即抢购", "马上抢", "去购买", "立即预订", "提交订单"])
    sold_keywords = cfg.get("sold_keywords", ["已售罄", "已抢光", "抢光了", "已结束", "暂时无货", "缺货", "已下架"])

    # 检查原始 HTML
    has_buy = any(kw in html for kw in buy_keywords)
    has_sold = any(kw in html for kw in sold_keywords)

    # 也检查 BeautifulSoup 解析后的纯文本
    if not has_buy and not has_sold:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator=" ", strip=True)
        has_buy = any(kw in text for kw in buy_keywords)
        has_sold = any(kw in text for kw in sold_keywords)

    if has_buy and not has_sold:
        return STATUS_AVAILABLE, f"{detail} → 有票/可购买"
    if has_sold and not has_buy:
        return STATUS_SOLD_OUT, f"{detail} → 已售罄"
    if has_buy and has_sold:
        return STATUS_AVAILABLE, f"{detail} → 可能有票（关键词同时命中）"

    # 如果页面内容极少（<500 字符），说明是 SPA 壳或反爬页
    if len(html) < 500:
        return STATUS_UNKNOWN, f"{detail} → 页面为 SPA 壳/反爬页，建议改用 API 模式"

    return STATUS_UNKNOWN, f"{detail} → 无法确定状态"


def check_generic(cfg: dict) -> tuple[str, str]:
    """
    通用检测：用户提供 URL + 关键词列表
    支持 page 和 api 两种模式。page 模式同时检查原始 HTML 和解析文本。
    """
    url = cfg["url"]
    mode = cfg.get("mode", "page")
    buy_keywords = cfg.get("buy_keywords", [])
    sold_keywords = cfg.get("sold_keywords", [])
    extra_headers = cfg.get("headers", {})

    if mode == "api":
        data = fetch_api(url, headers=extra_headers if extra_headers else None)
        if data is None:
            return STATUS_UNKNOWN, f"{url} → API 请求失败"
        has_buy = find_keywords_in_json(data, buy_keywords)
        has_sold = find_keywords_in_json(data, sold_keywords)
    else:
        html = fetch_page(url)
        if html is None:
            return STATUS_UNKNOWN, f"{url} → 页面请求失败"
        # 同时检查原始 HTML 和 BeautifulSoup 解析文本
        has_buy = has_sold = False
        for kw in buy_keywords:
            if kw in html or find_keywords_in_html(html, [kw]):
                has_buy = True
                break
        for kw in sold_keywords:
            if kw in html or find_keywords_in_html(html, [kw]):
                has_sold = True
                break

    if has_buy and not has_sold:
        return STATUS_AVAILABLE, f"{url} → 有票/可购买"
    if has_sold and not has_buy:
        return STATUS_SOLD_OUT, f"{url} → 已售罄"
    if has_buy and has_sold:
        return STATUS_AVAILABLE, f"{url} → 可能有票（关键词同时命中）"
    return STATUS_UNKNOWN, f"{url} → 无法确定状态"


# ═══════════════════════════════════════════════════════
#  核心监控循环
# ═══════════════════════════════════════════════════════

def check_one(cfg: dict, state: dict, wechat_token: str = "", remind_interval: int = 300, bark_key: str = "") -> dict:
    """检查单个监控项，返回更新后的 state"""
    monitor_id = cfg["id"]
    platform = cfg.get("platform", "generic")
    label = cfg.get("label", monitor_id)
    buy_url = cfg.get("buy_url", "")
    buy_url_mobile = cfg.get("buy_url_mobile", buy_url)

    # 执行检测
    try:
        if platform == "damai":
            status, message = check_damai(cfg["item_id"])
        elif platform == "maoyan":
            status, message = check_maoyan(cfg["show_id"])
        elif platform == "showstart":
            status, message = check_showstart(cfg["event_id"])
        elif platform == "douyin":
            status, message = check_douyin(cfg)
        else:
            status, message = check_generic(cfg)
    except Exception as e:
        log.error(f"[{label}] 检测异常: {e}")
        return state

    log.info(f"[{label}] {message}")

    prev = state.get(monitor_id, {})
    prev_status = prev.get("status", "")
    now = datetime.now()

    # 初始化首次状态
    if not prev:
        state[monitor_id] = {
            "status": status,
            "message": message,
            "last_change": now.isoformat(),
            "last_remind": "",
            "remind_count": 0,
        }
        # 首次运行如果有票也通知
        if status == STATUS_AVAILABLE:
            log.info(f"🎫 [{label}] 当前有票！")
            time_str = now.strftime("%H:%M:%S")
            wx_msg = _build_wx_msg(message, f"时间: {time_str}", buy_url_mobile)
            notify(f"🎫 有票！— {label}", wx_msg, wechat_token, buy_url, open_browser=True, bark_key=bark_key, app_url=buy_url_mobile)
            state[monitor_id]["last_remind"] = now.isoformat()
        return state

    # 状态变化检测
    if status != prev_status:
        state[monitor_id] = {
            "status": status,
            "message": message,
            "last_change": now.isoformat(),
            "last_remind": prev.get("last_remind", ""),
            "remind_count": 0,
        }

        if status == STATUS_AVAILABLE:
            log.info(f"🎫 [{label}] 状态变化：{prev_status} → 有票！")
            time_str = now.strftime("%H:%M:%S")
            wx_msg = _build_wx_msg(message, f"状态变化通知<br>时间: {time_str}", buy_url_mobile)
            notify(f"🎫 有票了！— {label}", wx_msg, wechat_token, buy_url, open_browser=True, bark_key=bark_key, app_url=buy_url_mobile)
            state[monitor_id]["last_remind"] = now.isoformat()
        elif prev_status:
            log.info(f"[{label}] 状态变化：{prev_status} → {status}")
        return state

    # 持续有票 → 周期性提醒
    if status == STATUS_AVAILABLE:
        last_remind_str = prev.get("last_remind", "")
        if last_remind_str:
            last_remind = datetime.fromisoformat(last_remind_str)
            elapsed = (now - last_remind).total_seconds()
            if elapsed >= remind_interval:
                remind_count = prev.get("remind_count", 0) + 1
                time_str = now.strftime("%H:%M:%S")
                wx_msg = _build_wx_msg(message, f"第 {remind_count} 次提醒<br>时间: {time_str}", buy_url_mobile)
                log.info(f"🔔 [{label}] 持续有票，距上次提醒 {int(elapsed)}秒，第{remind_count}次提醒")
                notify(f"🔔 仍有票！— {label}", wx_msg, wechat_token, buy_url, bark_key=bark_key, app_url=buy_url_mobile)
                state[monitor_id]["last_remind"] = now.isoformat()
                state[monitor_id]["remind_count"] = remind_count

    return state


def load_global_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def run():
    log.info("=" * 50)
    log.info("Ticket Monitor 启动")
    log.info("=" * 50)

    global_cfg = load_global_config()
    monitors = global_cfg.get("monitors", [])
    if not monitors:
        log.error("config.json 中没有配置监控项，请在 monitors 数组中添加。")
        return

    wechat_token = os.environ.get("PUSHPLUS_TOKEN") or global_cfg.get("wechat_token", "")
    bark_key = os.environ.get("BARK_KEY") or global_cfg.get("bark_key", "")
    remind_interval = int(global_cfg.get("remind_interval_seconds", 300))
    check_interval = int(global_cfg.get("check_interval_seconds", 30))

    if wechat_token and wechat_token != "你的PushPlusToken":
        log.info(f"微信通知已启用 (PushPlus)，持续有票时每 {remind_interval}秒 提醒一次")
    else:
        log.info("微信通知未配置，仅使用桌面弹窗。")
        log.info("获取 PushPlus token: http://www.pushplus.plus → 登录 → 一键生成Token")
    if bark_key and bark_key != "你的BarkKey":
        log.info("Bark iOS 通知已启用")
    else:
        log.info("Bark iOS 通知未配置。获取方式：App Store 搜索 'Bark' 安装 → 复制 Key")

    state = load_state()
    check_count = 0
    enabled_count = sum(1 for m in monitors if m.get("enabled", True))
    log.info(f"已加载 {enabled_count} 个启用的监控项（共 {len(monitors)} 个配置）")

    try:
        while True:
            check_count += 1
            log.info(f"--- 第 {check_count} 轮检查 ---")
            for monitor_cfg in monitors:
                if not monitor_cfg.get("enabled", True):
                    continue
                state = check_one(monitor_cfg, state, wechat_token, remind_interval, bark_key)
                time.sleep(2)

            save_state(state)
            log.info(f"等待 {check_interval} 秒后下一轮...")
            time.sleep(check_interval)
    except KeyboardInterrupt:
        log.info("用户终止，保存状态退出。")
        save_state(state)


def run_once():
    """单次检查模式 —— 供 GitHub Actions / cron 使用"""
    global_cfg = load_global_config()
    monitors = global_cfg.get("monitors", [])
    if not monitors:
        log.error("config.json 中没有配置监控项。")
        return

    wechat_token = os.environ.get("PUSHPLUS_TOKEN") or global_cfg.get("wechat_token", "")
    bark_key = os.environ.get("BARK_KEY") or global_cfg.get("bark_key", "")
    remind_interval = int(global_cfg.get("remind_interval_seconds", 300))

    state = load_state()
    enabled = [m for m in monitors if m.get("enabled", True)]
    log.info(f"单次检查：{len(enabled)} 个监控项")

    for monitor_cfg in enabled:
        state = check_one(monitor_cfg, state, wechat_token, remind_interval, bark_key)
        time.sleep(2)

    save_state(state)
    log.info("单次检查完成。")


# ═══════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        run()
