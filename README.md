# elsevier_tracker
爱思唯尔稿件的在线监控平台，支持订阅多用户稿件

![cover.png](https://raw.githubusercontent.com/1061700625/elsevier_tracker/refs/heads/main/assets/cover.png)


# 用法
## 我部署的平台
[http://xfxuezhang.cn:8081](http://xfxuezhang.cn:8081)


## 自行部署
1. 安装库
```bash
pip install flask flask_sqlalchemy requests apscheduler yagmail
```

2. `app.py`中改几个配置
```bash
app.secret_key = "xxxx-xx-xxxx-xx-xx"  # !!!! 用于会话加密，实际部署时请更换为更复杂的密钥

MAIL_USER = "xxxx@qq.com" # !!!! 替换为你的 QQ 邮箱
MAIL_PASS = "xxxx"        # !!!! 替换为你的 QQ 邮箱授权码

ADMIN_PASSWORD = "xxxxx" # !!!! 替换为你的管理员密码
```

3. 直接运行app.py
```bash
python app.py
```

