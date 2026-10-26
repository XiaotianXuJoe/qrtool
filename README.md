# QR Content Manager — 二维码生成管理工具

自托管的二维码内容管理工具：上传图片 + 填写文字 → 生成二维码；扫码打开移动友好的内容展示页；后台可管理 / 编辑 / 删除所有条目。**编辑内容后二维码不变**（id 与链接保持恒定），已分发的二维码可长期复用。

## 技术栈

- Python 3 + Flask（服务端渲染，内嵌模板，无构建步骤）
- SQLite（标准库 `sqlite3`，单文件 `data.db`，运行时自动创建）
- `qrcode[pil]` 生成二维码 PNG
- 前端原生 `fetch` 调用 API

## 快速开始

```bash
pip install -r requirements.txt
python app.py
```

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BASE_URL` | `http://175.178.16.78/qr` | 二维码内容与查看页链接的外网地址（含前缀） |
| `PORT` | `6000` | 监听端口（监听 `0.0.0.0`） |
| `URL_PREFIX` | 空 | URL 前缀，如 `/qr`（尾部斜杠自动去除）。用于 nginx `location /qr/ { proxy_pass http://127.0.0.1:6000; }` 不剥离前缀的反代场景，所有路由与页面内链接都会带上该前缀 |

## 页面

- `GET /` — 创建页：表单提交后展示二维码、可复制查看链接、下载 PNG
- `GET /admin` — 管理页：列表、查看/下载二维码、编辑（改文字、追加/删除图片）、删除
- `GET /v/<id>` — 扫码查看页：移动端优先，标题、多行文字（保留换行）、图片画廊

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/items` | multipart：`title`, `content`, `images[]` → 201 `{id, view_url, qr_url}` |
| GET | `/api/items` | 列表 `[{id,title,created_at,updated_at,image_count}]` |
| GET | `/api/items/<id>` | 完整条目（含图片 URL 列表） |
| PUT | `/api/items/<id>` | multipart：`title`, `content`, 新增 `images[]`, `delete_images[]`（文件名列表）→ 200 |
| DELETE | `/api/items/<id>` | 204，同时删除磁盘图片 |
| GET | `/api/items/<id>/qrcode` | 二维码 PNG，内容为 `BASE_URL/v/<id>` |
| GET | `/uploads/<filename>` | 静态图片 |

## 约束

- 图片：单张 ≤ 10MB，允许 jpg/jpeg/png/gif/webp，uuid 重命名防冲突
- 所有用户输出 HTML 转义防 XSS；文字以 `white-space: pre-wrap` 保留换行
- `data.db` 与 `static/uploads/` 为运行时数据，不入库（见 `.gitignore`）
