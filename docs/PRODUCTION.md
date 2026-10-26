# 生产环境边界

- 项目：二维码生成管理工具
- 端口：8443
- 目录：/home/ubuntu/qrtool
- 进程：PM2：qrtool

## 推荐检查

`pip install -r requirements.txt；python3 -m py_compile app.py`

## 不上传、不覆盖

data.db、static/uploads/ 和 AI 设置。这些内容只应通过独立的生产备份机制保存，不属于 Git 源码。

本仓库公开可见。任何真实凭据都不应写入代码、Issue、Actions 日志或 Git 历史。
