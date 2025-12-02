import json
import re
from datetime import datetime
import re
import requests
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


# =====================================
# 配置
# =====================================
app = Flask(__name__)
app.secret_key = "xxxx-xx-xxxx-xx-xx"  # !!!! 用于会话加密，实际部署时请更换为更复杂的密钥
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///tracker.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ====== 业务配置 ======
MAIL_USER = "xxxx@qq.com" # !!!! 替换为你的 QQ 邮箱
MAIL_PASS = "xxxx"        # !!!! 替换为你的 QQ 邮箱授权码
TRACKER_URL_TEMPLATE = (
    "https://tnlkuelk67.execute-api.us-east-1.amazonaws.com/tracker/{uuid}"
)
NOTIFY_URL = "http://14.103.144.180:7890/send/friend"
API_KEY = "xxxxxx" # !!!! 替换为你的 QQ 机器人 API Key（也可以不用管，这是我自己部署的QQ机器人）
# 管理员页面密码
ADMIN_PASSWORD = "xxxxx" # !!!! 替换为你的管理员密码
# 定时轮询间隔（秒）
CHECK_INTERVAL = 3600  # 每小时

STATUS_MAP = {
    3: "Under Review",
    4: "Required Reviewers Complete",
    8: "With Editor",
    9: "Completed - Accept",
    11: "Revision and Reconsider",
    23: "Under Review",
    28: "Editor Invited",
    29: "Review Complete",
}




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
        params = {"target": target_id, "key": API_KEY, "msg": message}
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
        yag = yagmail.SMTP(user=MAIL_USER, password=MAIL_PASS, host="smtp.qq.com", encoding='GBK')
        yag.send(to=to_addr, subject=subject, contents=body)
        print(f"[通知] 邮件已发送到 {to_addr}")
    except Exception as e:
        print(f"[通知] 邮件发送失败 ({to_addr}): {e}")


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


# =====================================
# 后台定时任务
# =====================================
scheduler = BackgroundScheduler(timezone="UTC")


def background_check_all_tasks():
    """定时任务：轮询所有任务"""
    with app.app_context():
        tasks = TrackerTask.query.all()
        if not tasks:
            print("[定时任务] 当前没有任何监控任务。")
            return

        print(f"[定时任务] 开始检查 {len(tasks)} 个任务...")
        for task in tasks:
            print(f"[定时任务] 检查 uuid={task.uuid} ...")
            tracker_data, has_changes, msg, err = process_tracker_for_task(
                task, do_notify=True
            )
            if err:
                print(f"[定时任务] 任务 {task.uuid} 失败: {err}")
            else:
                if has_changes:
                    print(f"[定时任务] 任务 {task.uuid} 有更新，已通知。")
                else:
                    print(f"[定时任务] 任务 {task.uuid} 无变化。")


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
    提交页面：
    - uuid
    - 通知方式：邮箱 / QQ
    - 联系方式：邮箱地址 or QQ 号
    """
    if request.method == "POST":
        uuid = request.form.get("uuid", "").strip()
        notify_type = request.form.get("notify_type", "").strip()
        contact = request.form.get("contact", "").strip()

        if not uuid or not notify_type or not contact:
            flash("请填写完整信息（uuid / 通知方式 / 联系方式）", "danger")
            return redirect(url_for("submit"))

        # 如果选邮箱，需要校验格式
        if notify_type == "email":
            if not is_valid_email(contact):
                flash("请输入正确的邮箱格式，例如 example@domain.com", "danger")
                return redirect(url_for("submit"))

        if notify_type not in ("email", "qq"):
            flash("通知方式非法，只能选择邮箱或 QQ", "danger")
            return redirect(url_for("submit"))

        task = TrackerTask.query.filter_by(uuid=uuid).first()
        if task:
            # 更新已有记录
            task.notify_type = notify_type
            task.contact = contact
            flash("已更新该 uuid 的通知配置", "success")
        else:
            # 新建
            task = TrackerTask(
                uuid=uuid,
                notify_type=notify_type,
                contact=contact,
            )
            db.session.add(task)
            flash("已创建监控任务", "success")

        db.session.commit()
        return redirect(url_for("query", uuid=uuid))

    return render_template("submit.html")


@app.route("/query", methods=["GET", "POST"])
def query():
    """
    查询页面：
    - 通过 uuid 查询当前状态
    - 会立即请求一次远程 TRACKER API，并做一次比对
    """
    uuid = request.args.get("uuid")

    if request.method == "POST":
        uuid = request.form.get("uuid", "").strip()
        if not uuid:
            flash("请填写 uuid", "danger")
            return redirect(url_for("query"))
        return redirect(url_for("query", uuid=uuid))

    task = None
    tracker_data = None
    changes_message = ""
    has_changes = False

    if uuid:
        task = TrackerTask.query.filter_by(uuid=uuid).first()
        if not task:
            flash("该 uuid 尚未在系统中登记，请先在提交页面创建。", "warning")
        else:
            tracker_data, has_changes, changes_message, err = process_tracker_for_task(
                task, do_notify=True
            )
            if err:
                flash(f"获取远程数据失败：{err}", "danger")

    return render_template(
        "query.html",
        uuid=uuid,
        task=task,
        tracker_data=tracker_data,
        has_changes=has_changes,
        changes_message=changes_message,
    )


@app.route("/delete/<uuid>", methods=["POST"])
def delete(uuid):
    """删除某个 uuid 对应的监控任务（前台/后台都可用）"""
    task = TrackerTask.query.filter_by(uuid=uuid).first()
    if not task:
        flash("要删除的任务不存在。", "warning")
        return redirect(url_for("query"))

    db.session.delete(task)
    db.session.commit()
    flash(f"已删除 uuid = {uuid} 的监控任务。", "success")
    return redirect(url_for("submit"))


# =====================================
# 管理员页面
# =====================================

@app.route("/admin", methods=["GET", "POST"])
def admin():
    """
    管理员页面：
    - 未登录：显示密码输入框
    - 已登录：显示所有任务、删除按钮、下一次定时检查时间
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
            flash("管理员密码错误", "danger")

    is_admin = session.get("is_admin", False)
    tasks = []
    next_run_time = None

    if is_admin:
        tasks = TrackerTask.query.order_by(TrackerTask.created_at.desc()).all()
        job = scheduler.get_job("tracker_check_all")
        if job and job.next_run_time:
            # APScheduler 返回的是 datetime（UTC），这里直接展示
            next_run_time = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S UTC")

    return render_template(
        "admin.html",
        is_admin=is_admin,
        tasks=tasks,
        next_run_time=next_run_time,
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
        app.run(host="0.0.0.0", port=5000, debug=True)
    finally:
        scheduler.shutdown()
