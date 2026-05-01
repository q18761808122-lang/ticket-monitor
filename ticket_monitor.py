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
    核心信号：window.buyFlag（服务端直接写入的 JS 变量）
    """
    detail = f"大麦 item={item_id}"
    page_url = f"https://detail.damai.cn/item.htm?id={item_id}"
    html = fetch_page(page_url)
    if not html:
        return STATUS_UNKNOWN, f"{detail} → 页面请求失败"

    # ⭐ 最可靠信号：window.buyFlag（服务端直接写入，不依赖 JS 渲染）
    if "window.buyFlag = true" in html or "window.buyFlag=true" in html:
        return STATUS_AVAILABLE, f"{detail} → 有票/可购买"
    if "window.buyFlag = false" in html or "window.buyFlag=false" in html:
        pass  # buyFlag 为 false，继续用其他指标确认

    # 辅助信号：文本关键词
    has_buy_text = find_keywords_in_html(html, ["立即购买", "立即预订", "选座购买"])
    has_sold_text = find_keywords_in_html(html, ["缺货登记", "已售罄", "暂无可售"])
    has_upcoming_text = find_keywords_in_html(html, ["即将开售", "预约抢购", "提交开售提醒"])

    if has_buy_text:
        return STATUS_AVAILABLE, f"{detail} → 有票/可购买"
    if has_sold_text:
        return STATUS_SOLD_OUT, f"{detail} → 已售罄"
    if has_upcoming_text:
        return STATUS_UNKNOWN, f"{detail} → 尚未开售"

    # buyFlag 明确为 false 且无其他信号 → 售罄
    if "window.buyFlag" in html:
        return STATUS_SOLD_OUT, f"{detail} → 已售罄 (buyFlag=false)"

    return STATUS_UNKNOWN, f"{detail} → 无法确定状态"


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

    wechat_token = global_cfg.get("wechat_token", "")
    bark_key = global_cfg.get("bark_key", "")
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

    wechat_token = global_cfg.get("wechat_token", "")
    bark_key = global_cfg.get("bark_key", "")
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
