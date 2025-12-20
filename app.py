import json
import re
from datetime import datetime, timezone, timedelta
import re
import requests
import time
import logging
from bs4 import BeautifulSoup
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
)
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
import yagmail
from urllib.parse import urlparse, parse_qs
import uuid as uuidlib
CST = timezone(timedelta(hours=8))

# pip install flask flask_sqlalchemy requests apscheduler yagmail


# =====================================
# 配置
# =====================================
app = Flask(__name__)
app.secret_key = "xx-xx-xx-xx-xx"  # !!!!这里要改
# SQLite 数据库，用于持久化任务信息
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///tracker.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ====== 业务配置 ======
MAIL_USER = "xxx@qq.com"  # !!!!这里要改
MAIL_PASS = "xxx"         # !!!!这里要改

TRACKER_URL_TEMPLATE = (
    "https://tnlkuelk67.execute-api.us-east-1.amazonaws.com/tracker/{uuid}"
)
NOTIFY_URL = "http://14.103.144.178:70/send/friend"  # 这里可以不管
API_KEY = "xx"  # 这里可以不管
# 管理员页面密码
ADMIN_PASSWORD = "xx"  # !!!!这里要改
# 定时轮询间隔（秒）
CHECK_INTERVAL = 3600  # 每小时

STATUS_MAP = {
    1: "Revision Needs Approval",
    3: "Under Review",
    4: "Required Reviewers Complete",
    7: "Revise",
    8: "With Editor",
    9: "Completed - Accept",
    11: "Revision and Reconsider",
    23: "Under Review",
    28: "Editor Invited",
    29: "Decision in Process",
    39: "Review Complete",
}



# ====== Article tracking（authors.elsevier.com）配置 ======
ARTICLE_TRACKING_URL_TEMPLATE = "https://authors.elsevier.com/tracking/article/details.do?aid={aid}&jid={jid}&surname={surname}"
ARTICLE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8",
}
ARTICLE_MAX_RETRIES = 3
ARTICLE_REQUEST_TIMEOUT = 30


# =====================================
# 数据库模型
# =====================================
class TrackerTask(db.Model):
    __tablename__ = "tracker_task"

    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(100), unique=True, nullable=False)

    # 通知方式：email / qq
    notify_type = db.Column(db.String(20), nullable=False)
    # 联系方式内容：邮箱地址或 QQ 号
    contact = db.Column(db.String(100), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_checked_at = db.Column(db.DateTime)

    # 上一次状态 & 事件统计，用于比对
    last_status = db.Column(db.Integer)
    last_event_counts = db.Column(db.Text)  # JSON 字符串

    # 最近一次错误信息
    last_error = db.Column(db.Text)


class ArticleTask(db.Model):
    __tablename__ = "article_task"

    id = db.Column(db.Integer, primary_key=True)

    # 唯一键：便于查询/删除
    article_key = db.Column(db.String(200), unique=True, nullable=False)

    aid = db.Column(db.String(50), nullable=False)
    jid = db.Column(db.String(50), nullable=False)
    surname = db.Column(db.String(100), nullable=False)
    url = db.Column(db.Text, nullable=False)

    notify_type = db.Column(db.String(20), nullable=False)
    contact = db.Column(db.String(100), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_checked_at = db.Column(db.DateTime)

    # 上一次快照：JSON 字符串（parse_snapshot 输出）
    last_snapshot = db.Column(db.Text)

    last_error = db.Column(db.Text)




with app.app_context():
    db.create_all()


# =====================================
# 工具函数
# =====================================
def is_valid_email(email: str) -> bool:
    """简单且有效的邮箱格式校验"""
    pattern = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
    return re.match(pattern, email) is not None


def unix_to_str(unixtime):
    """将Unix时间戳转为可读字符串（UTC）"""
    try:
        return datetime.utcfromtimestamp(unixtime).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "未知时间"

# =====================================
# Article 监控：抓取/解析/比对
# =====================================
def _norm_text(s: str) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def parse_article_params(article_url: str = "", aid: str = "", jid: str = "", surname: str = ""):
    """
    支持两种输入：
    1) 直接给 authors.elsevier.com 的 article URL（含 aid/jid/surname）
    2) 分别给 aid/jid/surname
    返回 (aid, jid, surname) 或 (None, None, None)
    """
    article_url = (article_url or "").strip()
    aid = (aid or "").strip()
    jid = (jid or "").strip()
    surname = (surname or "").strip()

    # 优先从 URL 解析
    if article_url:
        try:
            u = urlparse(article_url)
            qs = parse_qs(u.query)
            aid = (qs.get("aid") or [aid])[0]
            jid = (qs.get("jid") or [jid])[0]
            surname = (qs.get("surname") or [surname])[0]
        except Exception:
            pass

    aid = (aid or "").strip()
    jid = (jid or "").strip()
    surname = (surname or "").strip()
    if not (aid and jid and surname):
        return None, None, None
    return aid, jid, surname


def build_article_url(aid: str, jid: str, surname: str) -> str:
    return ARTICLE_TRACKING_URL_TEMPLATE.format(aid=aid, jid=jid, surname=surname)


def build_article_key(aid: str, jid: str, surname: str) -> str:
    # 用 URL-safe 的 key，便于路由传参
    safe_surname = re.sub(r"[^A-Za-z0-9._-]+", "_", surname.strip())
    safe_aid = re.sub(r"[^A-Za-z0-9._-]+", "_", aid.strip())
    safe_jid = re.sub(r"[^A-Za-z0-9._-]+", "_", jid.strip())
    return f"aid{safe_aid}_jid{safe_jid}_surname{safe_surname}"


def fetch_article_html(url: str, retries: int = ARTICLE_MAX_RETRIES, timeout: int = ARTICLE_REQUEST_TIMEOUT) -> str:
    last_exc = None
    for i in range(retries):
        try:
            with requests.Session() as s:
                s.headers.update(ARTICLE_HEADERS)
                resp = s.get(url, timeout=timeout, allow_redirects=True)
                resp.raise_for_status()
                return resp.text
        except Exception as e:
            last_exc = e
            logging.warning("Article fetch attempt %s failed: %s", i + 1, e)
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Failed to fetch page after {retries} attempts: {last_exc}")


def parse_snapshot(html: str):
    """
    返回包含三项信息的快照：
      - lastUpdatedDate: str
      - statusComment: str
      - productionEvents: List[Dict[str, str]] (键：date, event)
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) lastUpdatedDate
    last_updated = ""
    el = soup.select_one("#lastUpdatedDate")
    if el:
        last_updated = _norm_text(el.get_text())
    else:
        candidates = soup.find_all(string=re.compile(r"Last update", re.I))
        if candidates:
            node = candidates[0].parent
            last_updated = _norm_text(node.get_text())

    # 2) Status comment
    status_comment = ""
    label = soup.find(string=re.compile(r"Status comment", re.I))
    if label:
        container = label.parent
        dd = container.find_next(["dd", "p", "span", "div"])
        status_comment = _norm_text((dd.get_text() if dd else container.get_text()).replace(str(label), ""))
        if not status_comment:
            status_comment = _norm_text(container.find_next(string=True) or "")
    else:
        possible = soup.find_all(string=re.compile(r"(Status comment|status:|status\s+comment)", re.I))
        if possible:
            status_comment = _norm_text(possible[0])

    # 3) Production events
    production_events = []
    head = soup.find(string=re.compile(r"Production events", re.I))
    if head:
        sec = head.parent
        table = sec.find_next("table")
        if table:
            rows = table.find_all("tr")
            for r in rows:
                cols = [_norm_text(c.get_text()) for c in r.find_all(["td", "th"])]
                if len(cols) >= 2:
                    date, event = cols[0], cols[1]
                    if re.match(r"(?i)date", date) and re.match(r"(?i)event", event):
                        continue
                    if date or event:
                        production_events.append({"date": date, "event": event})
        else:
            ul = sec.find_next("ul")
            if ul:
                for li in ul.find_all("li"):
                    txt = _norm_text(li.get_text())
                    m = re.match(r"^(\d{1,4}[-/]\d{1,2}[-/]\d{1,2}).*?[—-]\s*(.+)$", txt)
                    if m:
                        production_events.append({"date": m.group(1), "event": m.group(2)})
                    else:
                        production_events.append({"date": "", "event": txt})

    return {
        "lastUpdatedDate": last_updated,
        "statusComment": status_comment,
        "productionEvents": production_events,
    }


def diff_snapshots(old, new):
    if not old:
        return False, "Baseline initialized."  # 启动即基线，不通知
    changes = []

    if old.get("lastUpdatedDate") != new.get("lastUpdatedDate"):
        changes.append(f"• lastUpdatedDate: '{old.get('lastUpdatedDate')}' → '{new.get('lastUpdatedDate')}'")

    if _norm_text(old.get("statusComment", "")) != _norm_text(new.get("statusComment", "")):
        changes.append(
            "• Status comment changed:\n"
            f"    OLD: {old.get('statusComment')}\n"
            f"    NEW: {new.get('statusComment')}"
        )

    old_events = old.get("productionEvents", []) or []
    new_events = new.get("productionEvents", []) or []
    if old_events != new_events:
        old_set = {(e.get("date", ""), e.get("event", "")) for e in old_events}
        new_set = {(e.get("date", ""), e.get("event", "")) for e in new_events}
        added = new_set - old_set
        removed = old_set - new_set
        if added:
            changes.append("• Production events — ADDED:\n  " + "\n  ".join([f"{d} — {ev}" for d, ev in added]))
        if removed:
            changes.append("• Production events — REMOVED:\n  " + "\n  ".join([f"{d} — {ev}" for d, ev in removed]))
        if not added and not removed:
            changes.append("• Production events changed order/content.")

    if not changes:
        return False, "No change detected."
    return True, "\n".join(changes)


def format_snapshot_for_message(snap, url: str) -> str:
    lines = [
        f"URL: {url}",
        f"LastUpdatedDate: {snap.get('lastUpdatedDate', '')}",
        f"Status comment: {snap.get('statusComment', '')}",
        "Production events:",
    ]
    events = snap.get("productionEvents") or []
    if not events:
        lines.append("  (none)")
    else:
        for e in events:
            lines.append(f"  - {e.get('date', '')} — {e.get('event', '')}")
    return "\n".join(lines)


def validate_article_form_data(article_url: str, aid: str, jid: str, surname: str, notify_type: str, contact: str):
    if not notify_type or not contact:
        return False, "请填写完整信息（通知方式 / 联系方式）"
    if notify_type not in ("email", "qq"):
        return False, "通知方式必须是 email 或 qq"
    if notify_type == "email" and not is_valid_email(contact):
        return False, "邮箱格式不正确"

    a, j, s = parse_article_params(article_url, aid, jid, surname)
    if not (a and j and s):
        return False, "Article 参数不完整：请提供 article URL，或分别填写 aid / jid / surname"
    return True, ""


def process_article_for_task(task: ArticleTask, do_notify=True):
    """
    对某个 article 任务执行一次抓取 + 解析 + 比对 +（可选）通知 + 更新数据库。
    返回 (snapshot, has_changes, changes_message, error_message)
    """
    try:
        html = fetch_article_html(task.url)
        snap = parse_snapshot(html)
    except Exception as e:
        err = str(e)
        task.last_error = err
        task.last_checked_at = datetime.utcnow()
        db.session.commit()
        return None, False, "", err

    old = None
    if task.last_snapshot:
        try:
            old = json.loads(task.last_snapshot)
        except Exception:
            old = None

    has_changes, changes_message = diff_snapshots(old, snap)

    # 更新数据库
    task.last_snapshot = json.dumps(snap, ensure_ascii=False)
    task.last_checked_at = datetime.utcnow()
    task.last_error = None
    db.session.commit()

    if has_changes and do_notify:
        subject = f"Article 状态更新通知 - {task.article_key}"
        message = (
            f"📢 Article 状态更新通知\n"
            f"----------------------------------------\n"
            f"🔎 Article: aid={task.aid}, jid={task.jid}, surname={task.surname}\n"
            f"🕐 检测时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            f"----------------------------------------\n"
            f"{changes_message}\n"
            f"----------------------------------------\n"
            f"{format_snapshot_for_message(snap, task.url)}\n"
            f"----------------------------------------\n"
            f"查询页面：{request.host_url.rstrip('/')}{url_for('query', article_key=task.article_key)}"
        )
        # 复用 submission 的发送实现，但 subject 需自定义
        if task.notify_type == "qq":
            do_send_notification_qq(task.contact, message)
        else:
            send_email(task.contact, subject, message)

    return snap, has_changes, changes_message, None


def send_test_notification_article(aid: str, jid: str, surname: str, url: str, notify_type: str, contact: str, snap):
    """
    发送 Article 测试通知
    """
    try:
        body = (
            f"📢 测试通知 - Elsevier Article Tracker\n"
            f"----------------------------------------\n"
            f"✅ 通知测试成功！\n"
            f"🔎 Article: aid={aid}, jid={jid}, surname={surname}\n"
            f"📱 通知方式: {'邮箱' if notify_type == 'email' else 'QQ'}\n"
            f"📞 联系方式: {contact}\n"
            f"🕐 测试时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            f"----------------------------------------\n"
            f"{format_snapshot_for_message(snap or {}, url)}\n"
        )
        if notify_type == "qq":
            do_send_notification_qq(contact, body)
            return True, "✅ 测试通知已发送，请检查QQ是否收到消息"
        elif notify_type == "email":
            subject = "测试通知 - Elsevier Article Tracker"
            send_email(contact, subject, body)
            return True, "✅ 测试通知已发送，请检查邮箱是否收到消息"
        return False, "❌ 未知通知方式"
    except Exception as e:
        return False, f"❌ 发送测试通知失败: {str(e)}"


def send_delete_notification_article(task: ArticleTask, delete_by: str, delete_reason: str):
    """
    发送 Article 任务删除通知
    """
    try:
        who = "管理员" if delete_by == "admin" else "用户"
        message = (
            f"⚠️ Article 监控任务已被删除 - Elsevier Article Tracker\n"
            f"----------------------------------------\n"
            f"❌ 您的 Article 监控任务已被{who}删除。\n"
            f"🔎 Article: aid={task.aid}, jid={task.jid}, surname={task.surname}\n"
            f"🗑️ 删除时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        )
        if delete_reason:
            message += f"📝 删除理由: {delete_reason}\n"
        message += "----------------------------------------\n"

        if task.notify_type == "qq":
            do_send_notification_qq(task.contact, message)
        elif task.notify_type == "email":
            subject = f"Article 监控任务已删除 - {task.article_key}"
            send_email(task.contact, subject, message)
        else:
            print(f"[删除通知] 未知通知方式: {task.notify_type}")
    except Exception as e:
        print(f"[删除通知] 发送 Article 删除通知失败 ({task.article_key}): {e}")


def fetch_tracker_data(uuid):
    """请求远程 tracker 数据"""
    url = TRACKER_URL_TEMPLATE.format(uuid=uuid)
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[错误] 获取数据失败 ({uuid}): {e}")
        return None


def do_send_notification_qq(target_id, message):
    """发送 QQ 通知"""
    try:
        now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST")
        params = {"target": target_id, "key": API_KEY, "msg": f"[{now_str}]\n{message}"}
        response = requests.get(NOTIFY_URL, params=params, timeout=10)
        if response.status_code == 200:
            print(f"[通知] QQ 通知已发送成功 -> {target_id}")
        else:
            print(f"[通知] QQ 发送失败({target_id})，状态码: {response.status_code}")
    except Exception as e:
        print(f"[通知] QQ 请求发送失败 ({target_id}): {e}")


def send_email(to_addr: str, subject: str, body: str):
    """使用 yagmail 发送邮件"""
    try:
        now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST")
        yag = yagmail.SMTP(user=MAIL_USER, password=MAIL_PASS, host="smtp.qq.com", encoding='utf-8')
        yag.send(to=to_addr, subject=subject, contents=f"[{now_str}]\n{body}")
        print(f"[通知] 邮件已发送到 {to_addr}")
    except Exception as e:
        print(f"[通知] 邮件发送失败 ({to_addr}): {e}")


def safe_int(value, default=0):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digits = re.findall(r"\d+", value)
        return int(digits[0]) if digits else default
    return default


def count_review_events(summary):
    """统计最新 Revision 的特定事件数量（基于 ReviewSummary）"""
    return {
        "REVIEWER_INVITED": safe_int(summary.get("ReviewInvitationsSent", 0)),
        "REVIEWER_ACCEPTED": safe_int(summary.get("ReviewInvitationsAccepted", 0)),
        "REVIEWER_COMPLETED": safe_int(summary.get("ReviewsCompleted", 0)),
    }


def check_for_updates(prev_status, prev_counts, current_status, current_counts):
    """
    对比旧状态/事件与当前状态/事件，返回 (是否有变化, 变化描述字符串)
    prev_counts / current_counts 都是 dict
    """
    changes = []

    # 状态变化
    if prev_status is not None and current_status != prev_status:
        old_status_str = STATUS_MAP.get(prev_status, f"未知状态({prev_status})")
        new_status_str = STATUS_MAP.get(current_status, f"未知状态({current_status})")
        changes.append(f"状态变化: {old_status_str} → {new_status_str}")

    # 事件数量变化
    prev_counts = prev_counts or {}
    for key, cur_val in current_counts.items():
        old_val = prev_counts.get(key, 0)
        if cur_val != old_val:
            changes.append(f"{key} 数量变化: {old_val} → {cur_val}")

    if changes:
        status_desc = STATUS_MAP.get(current_status, f"未知状态({current_status})")
        msg = (
            "检测到更新：\n"
            + "\n".join(changes)
            + f"\n\n- 当前状态: {current_status} ({status_desc})\n- 当前事件: {current_counts}"
        )
        return True, msg
    else:
        return False, ""


def process_tracker_for_task(task, do_notify=True):
    """
    对某个任务执行一次查询 + 比对 +（可选）通知 + 更新数据库。
    返回 (tracker_data, has_changes, changes_message, error_message)
    """
    tracker_data = fetch_tracker_data(task.uuid)
    if not tracker_data:
        error_msg = "获取远程数据失败"
        task.last_error = error_msg
        task.last_checked_at = datetime.utcnow()
        db.session.commit()
        return None, False, "", error_msg

    status = tracker_data.get("Status")
    status_desc = STATUS_MAP.get(status, status)
    last_updated_str = unix_to_str(tracker_data.get("LastUpdated"))
    summary = tracker_data.get("ReviewSummary", {}) or {}
    event_counts = count_review_events(summary)

    # 旧值
    prev_status = task.last_status
    prev_counts = (
        json.loads(task.last_event_counts) if task.last_event_counts else {}
    )

    has_changes, changes_message = check_for_updates(
        prev_status, prev_counts, status, event_counts
    )
    # 如果是第一次初始化，不通知
    is_first_run = (prev_status is None)
    # 如果不是第一次，才允许发送通知
    if not is_first_run and has_changes and do_notify:
        send_notification(task, changes_message)

    # 更新数据库记录
    task.last_status = status
    task.last_event_counts = json.dumps(event_counts, ensure_ascii=False)
    task.last_checked_at = datetime.utcnow()
    task.last_error = None  # 本次成功
    db.session.commit()

    # 在 tracker_data 中塞一些可读字段，方便模板展示
    tracker_data["_status_desc"] = status_desc
    tracker_data["_last_updated_str"] = last_updated_str
    tracker_data["_event_counts"] = event_counts

    return tracker_data, has_changes, changes_message, None


def validate_form_data(uuid, notify_type, contact):
    """
    验证任务表单数据
    返回: (is_valid, error_message)
    """
    if not uuid or not notify_type or not contact:
        return False, "请填写完整信息（uuid / 通知方式 / 联系方式）"
    
    # 如果选邮箱，需要校验格式
    if notify_type == "email":
        if not is_valid_email(contact):
            return False, "请输入正确的邮箱格式，例如 example@domain.com"
    
    if notify_type not in ("email", "qq"):
        return False, "通知方式非法，只能选择邮箱或 QQ"
    
    return True, ""


def send_notification(task, message):
    """
    根据通知方式发送通知：
    - QQ：调用原来的 NOTIFY_URL
    - 邮箱：这里简单 print，你可以替换为真实发邮件逻辑
    """
    if task.notify_type == "qq":
        do_send_notification_qq(task.contact, message)
    elif task.notify_type == "email":
        subject = f"稿件状态更新通知 - {task.uuid}"
        send_email(task.contact, subject, message)
    else:
        print(f"[通知] 未知通知方式: {task.notify_type}")


def send_test_notification(task, status_report=""):
    """
    发送测试通知消息
    返回: (success, message)
    """
    try:
        base_message = (
            f"📢 测试通知 - Elsevier Submission Tracker\n"
            f"----------------------------------------\n"
            f"✅ 通知测试成功！\n"
            f"🔑 UUID: {task.uuid}\n"
            f"📱 通知方式: {'邮箱' if task.notify_type == 'email' else 'QQ'}\n"
            f"📞 联系方式: {task.contact}\n"
            f"🕐 测试时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            f"----------------------------------------\n"
        )
        if status_report:
            full_message = base_message + "📊 稿件状态: " + status_report
        else:
            full_message = base_message + "系统已配置成功，将会在稿件状态变化时自动通知您。"
        full_message += (
            f"\n----------------------------------------\n"
            f"您可以在查询页面查看详细状态: \n"
            f"{request.host_url.rstrip('/')}{url_for('query', uuid=task.uuid)}"
        )

        if task.notify_type == "qq":
            do_send_notification_qq(task.contact, full_message)
            return True, "✅ 测试通知已发送，请检查QQ是否收到消息"
        elif task.notify_type == "email":
            subject = f"测试通知 - Elsevier Submission Tracker - {task.uuid}"
            send_email(task.contact, subject, full_message)
            return True, "✅ 测试通知已发送，请检查邮箱是否收到消息"
        else:
            return False, "❌ 未知通知方式"
    except Exception as e:
        return False, f"❌ 发送测试通知失败: {str(e)}"


def send_delete_notification(uuid, notify_type, contact, delete_by, delete_reason):
    """
    发送删除通知给用户
    """
    try:
        if delete_by == 'admin':
            message = (
                f"⚠️ 监控任务已被管理员删除 - Elsevier Submission Tracker\n"
                f"----------------------------------------\n"
                f"❌ 您的稿件监控任务已被管理员删除。\n"
                f"🔑 UUID: {uuid}\n"
                f"🗑️ 删除时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"📝 删除理由: {delete_reason}\n"
                f"----------------------------------------\n"
                f"如果您对此有疑问，请联系系统管理员。\n"
                f"您可以在提交页面重新提交该稿件的监控任务。\n"
            )
        else:  # 用户自行删除
            message = (
                f"✅ 监控任务已取消 - Elsevier Submission Tracker\n"
                f"----------------------------------------\n"
                f"您已成功取消对稿件的监控。\n"
                f"🔑 UUID: {uuid}\n"
                f"🗑️ 删除时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"----------------------------------------\n"
                f"如果您需要重新监控此稿件，请在提交页面重新提交。\n"
            )
        
        if notify_type == "qq":
            do_send_notification_qq(contact, message)
            print(f"[删除通知] QQ 删除通知已发送 -> {contact}")
        elif notify_type == "email":
            subject = f"监控任务已删除 - Elsevier Submission Tracker - {uuid}"
            send_email(contact, subject, message)
            print(f"[删除通知] 邮箱删除通知已发送 -> {contact}")
        else:
            print(f"[删除通知] 未知通知方式: {notify_type}")
    except Exception as e:
        print(f"[删除通知] 发送删除通知失败 ({uuid}): {e}")


def extract_uuid(s: str):
    s = (s or "").strip()
    # 如果是 URL：从 ?uuid= 里取
    try:
        u = urlparse(s)
        if u.scheme in ("http", "https") and u.netloc:
            qs = parse_qs(u.query)
            if qs.get("uuid"): s = qs["uuid"][0].strip()
    except Exception:
        pass
    # 兜底：如果用户粘贴了包含 uuid=xxx 的文本
    m = re.search(r"uuid=([0-9a-fA-F-]{36})", s)
    if m: s = m.group(1)
    # 最终校验并规范化（小写标准格式）
    try:
        return str(uuidlib.UUID(s))
    except Exception:
        return None



# =====================================
# 后台定时任务
# =====================================
scheduler = BackgroundScheduler(timezone="UTC")


def background_check_all_tasks():
    """定时任务：轮询所有任务（submission + article）"""
    with app.app_context():
        sub_tasks = TrackerTask.query.all()
        art_tasks = ArticleTask.query.all()

        if not sub_tasks and not art_tasks:
            print("[定时任务] 当前没有任何监控任务。")
            return

        if sub_tasks:
            print(f"[定时任务] 开始检查 {len(sub_tasks)} 个 submission 任务...")
            for task in sub_tasks:
                print(f"[定时任务] 检查 uuid={task.uuid} ...")
                tracker_data, has_changes, msg, err = process_tracker_for_task(task, do_notify=True)
                if err:
                    print(f"[定时任务] 任务 {task.uuid} 失败: {err}")
                else:
                    if has_changes:
                        print(f"[定时任务] 任务 {task.uuid} 有更新，已通知。")
                    else:
                        print(f"[定时任务] 任务 {task.uuid} 无变化。")

        if art_tasks:
            print(f"[定时任务] 开始检查 {len(art_tasks)} 个 article 任务...")
            for task in art_tasks:
                print(f"[定时任务] 检查 article_key={task.article_key} ...")
                snap, has_changes, msg, err = process_article_for_task(task, do_notify=True)
                if err:
                    print(f"[定时任务] Article 任务 {task.article_key} 失败: {err}")
                else:
                    if has_changes:
                        print(f"[定时任务] Article 任务 {task.article_key} 有更新，已通知。")
                    else:
                        print(f"[定时任务] Article 任务 {task.article_key} 无变化。")


# 启动定时任务
scheduler.add_job(
    func=background_check_all_tasks,
    trigger="interval",
    seconds=CHECK_INTERVAL,
    id="tracker_check_all",
    replace_existing=True,
)


# =====================================
# 路由
# =====================================

@app.route("/")
def index():
    return redirect(url_for("submit"))


@app.route("/submit", methods=["GET", "POST"])
def submit():
    """
    提交页面：两类任务
    1) submission：uuid
    2) article：aid/jid/surname 或作者追踪 URL
    """
    if request.method == "POST":
        task_type = (request.form.get("task_type") or "submission").strip()

        notify_type = (request.form.get("notify_type", "") or "").strip()
        contact = (request.form.get("contact", "") or "").strip()

        if task_type == "article":
            article_url = request.form.get("article_url", "").strip()
            aid = request.form.get("aid", "").strip()
            jid = request.form.get("jid", "").strip()
            surname = request.form.get("surname", "").strip()

            is_valid, error_msg = validate_article_form_data(article_url, aid, jid, surname, notify_type, contact)
            if not is_valid:
                flash(error_msg, "danger")
                return redirect(url_for("submit"))

            aid, jid, surname = parse_article_params(article_url, aid, jid, surname)
            url = build_article_url(aid, jid, surname)
            article_key = build_article_key(aid, jid, surname)

            task = ArticleTask.query.filter_by(article_key=article_key).first()
            if task:
                task.notify_type = notify_type
                task.contact = contact
                task.aid = aid
                task.jid = jid
                task.surname = surname
                task.url = url
                flash("已更新 Article 监控任务", "success")
            else:
                task = ArticleTask(
                    article_key=article_key,
                    aid=aid,
                    jid=jid,
                    surname=surname,
                    url=url,
                    notify_type=notify_type,
                    contact=contact,
                )
                db.session.add(task)
                flash("已创建 Article 监控任务", "success")

            db.session.commit()
            return redirect(url_for("query", article_key=article_key))

        # 默认 submission
        uuid_raw = request.form.get("uuid", "").strip()
        uuid = extract_uuid(uuid_raw)
        if not uuid:
            flash("UUID 格式不正确，或链接中未包含 uuid 参数。", "danger")
            return redirect(url_for("submit"))

        is_valid, error_msg = validate_form_data(uuid, notify_type, contact)
        if not is_valid:
            flash(error_msg, "danger")
            return redirect(url_for("submit"))

        task = TrackerTask.query.filter_by(uuid=uuid).first()
        if task:
            task.notify_type = notify_type
            task.contact = contact
            flash("已更新监控任务", "success")
        else:
            task = TrackerTask(uuid=uuid, notify_type=notify_type, contact=contact)
            db.session.add(task)
            flash("已创建监控任务", "success")

        db.session.commit()
        return redirect(url_for("query", uuid=uuid))

    return render_template("submit.html")


@app.route("/test_notify", methods=["POST"])
def test_notify():
    """测试通知功能：立即发送一条测试消息给用户（submission/article 都支持）"""
    task_type = (request.form.get("task_type") or "submission").strip()
    notify_type = (request.form.get("notify_type", "") or "").strip()
    contact = (request.form.get("contact", "") or "").strip()

    if task_type == "article":
        article_url = request.form.get("article_url", "").strip()
        aid = request.form.get("aid", "").strip()
        jid = request.form.get("jid", "").strip()
        surname = request.form.get("surname", "").strip()

        is_valid, error_msg = validate_article_form_data(article_url, aid, jid, surname, notify_type, contact)
        if not is_valid:
            flash(error_msg, "danger")
            return redirect(url_for("submit"))

        aid, jid, surname = parse_article_params(article_url, aid, jid, surname)
        url = build_article_url(aid, jid, surname)

        # 立即抓一次快照并发送
        try:
            html = fetch_article_html(url)
            snap = parse_snapshot(html)
        except Exception:
            snap = {}

        ok, msg = send_test_notification_article(aid, jid, surname, url, notify_type, contact, snap)
        flash(msg, "success" if ok else "danger")
        return redirect(url_for("submit"))

    # submission
    uuid_raw = request.form.get("uuid", "").strip()
    uuid = extract_uuid(uuid_raw)
    if not uuid:
        flash("UUID 格式不正确，或链接中未包含 uuid 参数。", "danger")
        return redirect(url_for("submit"))

    is_valid, error_msg = validate_form_data(uuid, notify_type, contact)
    if not is_valid:
        flash(error_msg, "danger")
        return redirect(url_for("submit"))

    task = TrackerTask(uuid=uuid, notify_type=notify_type, contact=contact)
    tracker_data = fetch_tracker_data(task.uuid)
    if tracker_data:
        status = tracker_data.get("Status")
        status_report = STATUS_MAP.get(status, status)
    else:
        status_report = "获取远程数据失败"

    success, message = send_test_notification(task, status_report)
    flash(message, "success" if success else "danger")
    return redirect(url_for("submit"))


@app.route("/query", methods=["GET", "POST"])
def query():
    """
    查询页面：
    - submission：uuid
    - article：article_key（或 url / aid/jid/surname）
    """
    uuid = request.args.get("uuid")
    article_key = request.args.get("article_key")
    active_tab = "article" if article_key else "submission"

    if request.method == "POST":
        task_type = (request.form.get("task_type") or "submission").strip()
        if task_type == "article":
            article_url = request.form.get("article_url", "").strip()
            aid = request.form.get("aid", "").strip()
            jid = request.form.get("jid", "").strip()
            surname = request.form.get("surname", "").strip()
            a, j, s = parse_article_params(article_url, aid, jid, surname)
            if not (a and j and s):
                flash("请填写 Article URL，或分别填写 aid / jid / surname", "danger")
                return redirect(url_for("query"))
            key = build_article_key(a, j, s)
            return redirect(url_for("query", article_key=key))
        else:
            uuid_raw = request.form.get("uuid", "").strip()
            u = extract_uuid(uuid_raw)
            if not u:
                flash("UUID 格式不正确，或链接中未包含 uuid 参数。", "danger")
                return redirect(url_for("query"))
            return redirect(url_for("query", uuid=u))

    # ====== submission 查询 ======
    if uuid:
        task = TrackerTask.query.filter_by(uuid=uuid).first()
        tracker_data = None
        has_changes = False
        changes_message = ""

        if not task:
            flash("该 uuid 尚未在系统中登记，请先在提交页面创建。", "warning")
        else:
            tracker_data, has_changes, changes_message, err = process_tracker_for_task(task, do_notify=True)
            if err:
                flash(f"获取远程数据失败：{err}", "danger")

        return render_template(
            "query.html",
            active_tab="submission",
            uuid=uuid,
            task=task,
            tracker_data=tracker_data,
            has_changes=has_changes,
            changes_message=changes_message,
        )

    # ====== article 查询 ======
    if article_key:
        task = ArticleTask.query.filter_by(article_key=article_key).first()
        snapshot = None
        has_changes = False
        changes_message = ""

        if not task:
            flash("该 article 尚未在系统中登记，请先在提交页面创建。", "warning")
        else:
            snapshot, has_changes, changes_message, err = process_article_for_task(task, do_notify=True)
            if err:
                flash(f"获取远程数据失败：{err}", "danger")

        return render_template(
            "query.html",
            active_tab="article",
            article_key=article_key,
            article_task=task,
            snapshot=snapshot,
            has_changes=has_changes,
            changes_message=changes_message,
        )

    # 无参数：默认页面
    return render_template("query.html", active_tab=active_tab)


@app.route("/delete/<uuid>", methods=["POST"])
def delete(uuid):
    """删除某个 uuid 对应的监控任务（前台/后台都可用）"""
    task = TrackerTask.query.filter_by(uuid=uuid).first()
    if not task:
        flash("要删除的任务不存在。", "warning")
        return redirect(url_for("query"))
    # 删除任务
    db.session.delete(task)
    db.session.commit()
    # 获取删除理由和删除来源
    delete_reason = request.form.get("delete_reason", "").strip()
    delete_by = request.form.get("delete_by", "user")
    send_delete_notification(uuid, task.notify_type, task.contact, delete_by, delete_reason)
    flash(f"已删除 uuid = {uuid} 的监控任务。" + (f"已发送删除通知。" if delete_reason else ""), "success")

    if delete_by == 'admin': return redirect(url_for("admin"))
    else: return redirect(url_for("submit"))


@app.route("/delete_article/<article_key>", methods=["POST"])
def delete_article(article_key):
    """删除某个 article_key 对应的监控任务（前台/后台都可用）"""
    task = ArticleTask.query.filter_by(article_key=article_key).first()
    if not task:
        flash("要删除的任务不存在。", "warning")
        return redirect(url_for("query"))

    # 删除任务
    db.session.delete(task)
    db.session.commit()

    delete_reason = request.form.get("delete_reason", "").strip()
    delete_by = request.form.get("delete_by", "user")

    # 和 submission 保持一致：删除后给用户发通知（理由可空）
    send_delete_notification_article(task, delete_by, delete_reason)

    flash(f"已删除 article = {article_key} 的监控任务。已发送删除通知。", "success")

    if delete_by == "admin":
        return redirect(url_for("admin"))
    return redirect(url_for("submit"))

# =====================================
# 管理员页面
# =====================================

@app.route("/admin", methods=["GET", "POST"])
def admin():
    """
    管理员页面：
    - 未登录：显示密码输入框
    - 已登录：显示所有任务（submission + article）、删除按钮、下一次定时检查时间
    """
    is_admin = session.get("is_admin", False)

    # 未登录时处理登录提交
    if request.method == "POST" and not is_admin:
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["is_admin"] = True
            flash("管理员登录成功", "success")
            return redirect(url_for("admin"))
        else:
            flash("密码错误", "danger")
            return redirect(url_for("admin"))

    tasks = []
    article_tasks = []
    next_run_time = None

    if is_admin:
        tasks = TrackerTask.query.order_by(TrackerTask.created_at.desc()).all()
        article_tasks = ArticleTask.query.order_by(ArticleTask.created_at.desc()).all()
        job = scheduler.get_job("tracker_check_all")
        if job and job.next_run_time:
            next_run_time = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S UTC")

    return render_template(
        "admin.html",
        is_admin=is_admin,
        tasks=tasks,
        article_tasks=article_tasks,
        next_run_time=next_run_time,
        status_map=STATUS_MAP,
    )


@app.route("/admin/logout")
def admin_logout():
    """管理员退出登录"""
    session.pop("is_admin", None)
    flash("已退出管理员登录", "info")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    # 先启动调度器，再启动 Flask
    scheduler.start()
    try:
        app.run(host="0.0.0.0", port=8081, debug=True)
    finally:
        scheduler.shutdown()
