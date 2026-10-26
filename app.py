# -*- coding: utf-8 -*-
"""
QR Content Manager — 二维码生成管理工具
Flask + SQLite + qrcode[pil]，服务端渲染，无构建步骤。
"""
import base64
import binascii
import io
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone

import qrcode
import requests
from flask import (Flask, abort, g, jsonify, redirect, render_template_string,
                   request, send_file, send_from_directory, url_for)
from markupsafe import escape
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
DB_PATH = os.path.join(BASE_DIR, "data.db")

BASE_URL = os.environ.get("BASE_URL", "http://175.178.16.78/qr").rstrip("/")
PORT = int(os.environ.get("PORT", "6000"))
# URL 前缀（如 nginx 反代 location /qr/ 且不剥离前缀时设为 /qr）
P = os.environ.get("URL_PREFIX", "").rstrip("/")

ALLOWED_EXT = {"jpg", "jpeg", "png", "gif", "webp"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB

# AI 美化（Seedream / 火山方舟 ARK）默认配置，可在 /api/settings 覆盖
DEFAULT_SEEDREAM_MODEL = "doubao-seedream-4-0-250828"
DEFAULT_SEEDREAM_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
BEAUTIFY_PROMPT_PREFIX = (
    "这是一张二维码图片。请在绝对保持二维码黑白模块图案、三个定位角完整清晰、"
    "可正常扫描的前提下，仅美化背景与留白区域："
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 总体请求上限（多图场景）

os.makedirs(UPLOAD_DIR, exist_ok=True)


# ---------------------------------------------------------------- DB helpers
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id         TEXT PRIMARY KEY,
            title      TEXT,
            content    TEXT,
            image_path TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    # 数据迁移：新增 blocks 列（有序内容块 JSON），已存在则忽略
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)")]
    if "blocks" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN blocks TEXT")
    # 数据迁移：新增 beautified_image 列（AI 美化图文件名）
    if "beautified_image" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN beautified_image TEXT")
    # AI 美化设置（seedream_api_key / seedream_model / seedream_base_url）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id():
    for _ in range(20):
        rid = secrets.token_hex(4)  # 8 位短随机 id
        if not get_db().execute("SELECT 1 FROM items WHERE id=?", (rid,)).fetchone():
            return rid
    raise RuntimeError("unable to allocate id")


# --------------------------------------------------------------- settings helpers
def get_setting(key):
    row = get_db().execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    return row["value"] if row else None


def set_setting(key, value):
    """value 为 None 时删除该键（回落到默认值 / 视为未配置）。"""
    db = get_db()
    if value is None:
        db.execute("DELETE FROM settings WHERE key=?", (key,))
    else:
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
            (key, value),
        )


def mask_api_key(key):
    """api key 脱敏：>8 位显示 前4...后4，否则仅提示已保存。"""
    if not key:
        return ""
    if len(key) > 8:
        return key[:4] + "..." + key[-4:]
    return "已保存"


def row_images(row):
    raw = row["image_path"] or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data]
    except (ValueError, TypeError):
        pass
    return [raw]  # 兼容旧的单路径字符串


# ----------------------------------------------------------------- 内容块 helpers
def stored_blocks(row):
    """读取 blocks 列并解析；无列/NULL/非法 JSON 时返回 []。"""
    try:
        raw = row["blocks"]
    except (KeyError, IndexError):
        raw = None
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for b in data:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            out.append({"type": "text", "text": str(b.get("text") or "")})
        elif b.get("type") == "image" and b.get("file"):
            out.append({"type": "image", "file": os.path.basename(str(b["file"]))})
    return out


def legacy_blocks(row):
    """legacy 数据（content + image_path）转换为内容块：文字在前，图片按序在后。"""
    out = []
    content = row["content"] or ""
    if content:
        out.append({"type": "text", "text": content})
    for name in row_images(row):
        out.append({"type": "image", "file": os.path.basename(name)})
    return out


def effective_blocks(row):
    """blocks 非空时以 blocks 为准；为空回退 legacy 转换结果。"""
    blocks = stored_blocks(row)
    return blocks if blocks else legacy_blocks(row)


def sync_legacy_columns(blocks):
    """由 blocks 生成 legacy 列内容（content / image_path），保持旧接口与列表统计可用。"""
    texts = [b["text"] for b in blocks if b["type"] == "text"]
    images = [b["file"] for b in blocks if b["type"] == "image"]
    return "\n\n".join(texts), json.dumps(images, ensure_ascii=False)


def parse_blocks_field(raw):
    """解析请求中的 blocks JSON。文字块含 text；图片块含 ref（已存在文件名或新上传字段名）。
    非法结构抛 ValueError。"""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("blocks 不是合法的 JSON")
    if not isinstance(data, list):
        raise ValueError("blocks 必须是数组")
    out = []
    for b in data:
        if not isinstance(b, dict):
            raise ValueError("内容块必须是对象")
        t = b.get("type")
        if t == "text":
            out.append({"type": "text", "text": str(b.get("text") or "")})
        elif t == "image":
            ref = str(b.get("ref") or "").strip()
            if not ref:
                raise ValueError("图片块缺少 ref 字段")
            out.append({"type": "image", "ref": ref})
        else:
            raise ValueError(f"未知的内容块类型：{t}")
    return out


def item_detail(row):
    images = row_images(row)
    blocks = []
    for b in effective_blocks(row):
        if b["type"] == "text":
            blocks.append({"type": "text", "text": b["text"]})
        else:
            blocks.append({
                "type": "image",
                "file": b["file"],
                "url": P + "/uploads/" + b["file"],
            })
    try:
        beautified = row["beautified_image"]
    except (KeyError, IndexError):
        beautified = None
    return {
        "id": row["id"],
        "title": row["title"] or "",
        "content": row["content"] or "",
        "images": images,
        "image_urls": [P + "/uploads/" + os.path.basename(p) for p in images],
        "image_count": len(images),
        "blocks": blocks,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "beautified_url": (P + "/uploads/" + beautified) if beautified else None,
        "view_url": f"{BASE_URL}/v/{row['id']}",
        "qr_url": f"{P}/api/items/{row['id']}/qrcode",
    }


# ------------------------------------------------------------ upload helpers
def allowed_file(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in ALLOWED_EXT


def save_image_file(f):
    """校验并保存单个上传图片，返回 uuid 文件名；失败抛 ValueError（调用方负责清理已保存文件）。"""
    if not allowed_file(f.filename):
        raise ValueError(f"不支持的图片格式：{f.filename}（允许 jpg/jpeg/png/gif/webp）")
    ext = f.filename.rsplit(".", 1)[-1].lower()
    data = f.read()
    if len(data) > MAX_IMAGE_SIZE:
        raise ValueError(f"图片超过 10MB 限制：{f.filename}")
    if not data:
        raise ValueError(f"图片内容为空：{f.filename}")
    name = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(UPLOAD_DIR, name), "wb") as out:
        out.write(data)
    return name


def save_images(files):
    """校验并保存上传图片，返回保存后的文件名列表；失败抛 ValueError。"""
    saved = []
    for f in files:
        if not f or not f.filename:
            continue
        try:
            saved.append(save_image_file(f))
        except ValueError:
            cleanup_files(saved)
            raise
    return saved


def cleanup_files(names):
    for name in names:
        safe = os.path.basename(name)
        try:
            os.remove(os.path.join(UPLOAD_DIR, safe))
        except OSError:
            pass


def resolve_blocks(parsed, keepable):
    """把解析出的 blocks 落为可存储结构：图片块 ref 若是 keepable 中的已有文件名则保留，
    否则从 request.files 取同名字段保存为新文件。返回 (blocks, saved_names)，失败抛 ValueError。"""
    blocks = []
    saved = []
    try:
        for b in parsed:
            if b["type"] == "text":
                blocks.append({"type": "text", "text": b["text"]})
                continue
            ref = b["ref"]
            base = os.path.basename(ref)
            if (ref == base) and base in keepable:
                blocks.append({"type": "image", "file": base})
                continue
            f = request.files.get(ref)
            if not f or not f.filename:
                raise ValueError(f"图片块引用的文件未上传：{ref}")
            name = save_image_file(f)
            saved.append(name)
            blocks.append({"type": "image", "file": name})
    except ValueError:
        cleanup_files(saved)
        raise
    return blocks, saved


def legacy_request_blocks():
    """旧格式请求（content + images[]）转内容块：文字块在前、图片块按序在后。"""
    content = request.form.get("content") or ""
    images = save_images(request.files.getlist("images"))
    blocks = []
    if content:
        blocks.append({"type": "text", "text": content})
    for name in images:
        blocks.append({"type": "image", "file": name})
    return blocks


# ------------------------------------------------------------------ API 路由
@app.post(P + "/api/items")
def api_create():
    title = (request.form.get("title") or "").strip()
    raw_blocks = request.form.get("blocks")
    try:
        if raw_blocks is not None:
            blocks, _ = resolve_blocks(parse_blocks_field(raw_blocks), keepable=set())
        else:  # 兼容旧格式：content + images[]
            blocks = legacy_request_blocks()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    content, image_path = sync_legacy_columns(blocks)
    rid = new_id()
    ts = now_iso()
    db = get_db()
    db.execute(
        "INSERT INTO items (id, title, content, image_path, blocks, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (rid, title, content, image_path,
         json.dumps(blocks, ensure_ascii=False), ts, ts),
    )
    db.commit()
    return jsonify({
        "id": rid,
        "view_url": f"{BASE_URL}/v/{rid}",
        "qr_url": f"{P}/api/items/{rid}/qrcode",
    }), 201


@app.get(P + "/api/items")
def api_list():
    rows = get_db().execute(
        "SELECT * FROM items ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return jsonify([{
        "id": r["id"],
        "title": r["title"] or "",
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "image_count": len(row_images(r)),
    } for r in rows])


@app.get(P + "/api/items/<item_id>")
def api_get(item_id):
    row = get_db().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(item_detail(row))


@app.put(P + "/api/items/<item_id>")
def api_update(item_id):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    title = (request.form.get("title") or "").strip()
    old_files = {os.path.basename(x) for x in row_images(row)}
    old_files |= {b["file"] for b in stored_blocks(row) if b["type"] == "image"}

    raw_blocks = request.form.get("blocks")
    try:
        if raw_blocks is not None:
            # 新格式：ref 可为已有文件名（保留）或新上传文件字段名
            blocks, _ = resolve_blocks(parse_blocks_field(raw_blocks), keepable=old_files)
        else:
            # 兼容旧格式：content + images[] + delete_images[]
            content = request.form.get("content") or ""
            delete_set = {os.path.basename(d) for d in request.form.getlist("delete_images")}
            kept = [n for n in row_images(row) if os.path.basename(n) not in delete_set]
            kept.extend(save_images(request.files.getlist("images")))
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            for name in kept:
                blocks.append({"type": "image", "file": os.path.basename(name)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # 未被新 blocks 引用的旧图片从磁盘删除
    referenced = {b["file"] for b in blocks if b["type"] == "image"}
    cleanup_files(sorted(old_files - referenced))

    content, image_path = sync_legacy_columns(blocks)
    db.execute(
        "UPDATE items SET title=?, content=?, image_path=?, blocks=?, updated_at=?"
        " WHERE id=?",
        (title, content, image_path, json.dumps(blocks, ensure_ascii=False),
         now_iso(), item_id),
    )
    db.commit()
    return jsonify({"id": item_id, "ok": True,
                    "image_count": len(referenced)}), 200


@app.delete(P + "/api/items/<item_id>")
def api_delete(item_id):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    files = {os.path.basename(x) for x in row_images(row)}
    files |= {b["file"] for b in stored_blocks(row) if b["type"] == "image"}
    try:
        if row["beautified_image"]:
            files.add(os.path.basename(row["beautified_image"]))
    except (KeyError, IndexError):
        pass
    cleanup_files(sorted(files))
    db.execute("DELETE FROM items WHERE id=?", (item_id,))
    db.commit()
    return "", 204


@app.get(P + "/api/items/<item_id>/qrcode")
def api_qrcode(item_id):
    row = get_db().execute(
        "SELECT id FROM items WHERE id=?", (item_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    img = qrcode.make(f"{BASE_URL}/v/{item_id}", box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png",
                     download_name=f"qrcode-{item_id}.png")


# ------------------------------------------------------------ AI 美化（Seedream）
@app.get(P + "/api/settings")
def api_get_settings():
    key = get_setting("seedream_api_key") or ""
    return jsonify({
        "seedream_api_key": mask_api_key(key),
        "seedream_model": get_setting("seedream_model") or DEFAULT_SEEDREAM_MODEL,
        "seedream_base_url": get_setting("seedream_base_url") or DEFAULT_SEEDREAM_BASE_URL,
        "has_key": bool(key),
    })


@app.put(P + "/api/settings")
def api_put_settings():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    db = get_db()
    for key in ("seedream_api_key", "seedream_model", "seedream_base_url"):
        if key not in data:
            continue  # 未传的键不修改
        value = str(data.get(key) or "").strip()
        # 空串 = 清除该键（api_key 清除；model/base_url 回落默认）
        set_setting(key, value if value else None)
    db.commit()
    return jsonify({"ok": True}), 200


def check_scannable(img_bytes, expected_url):
    """cv2 解码美化图并比对内容：与原始二维码 URL 相同返回 True，
    解码失败/内容不符返回 False；检测本身异常返回 None（不阻塞主流程）。"""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(img_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return False
        decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
        return bool(decoded) and decoded.strip() == expected_url
    except Exception:
        return None


@app.post(P + "/api/items/<item_id>/beautify")
def api_beautify(item_id):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    api_key = get_setting("seedream_api_key")
    if not api_key:
        return jsonify({"error": "请先在 AI 设置中填写 Seedream API Key"}), 400
    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt 不能为空"}), 400

    # 内存中生成该条目二维码 PNG，base64 后作为 ARK 的参考图
    img = qrcode.make(f"{BASE_URL}/v/{item_id}", box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    model = get_setting("seedream_model") or DEFAULT_SEEDREAM_MODEL
    base_url = (get_setting("seedream_base_url") or DEFAULT_SEEDREAM_BASE_URL).rstrip("/")
    # 兼容用户直接粘贴完整接口地址（…/v1/images/generations）的情况，避免路径重复拼接
    if not base_url.endswith("/images/generations"):
        base_url = base_url + "/images/generations"
    try:
        resp = requests.post(
            base_url,
            headers={"Authorization": "Bearer " + api_key,
                     "Content-Type": "application/json"},
            json={
                "model": model,
                "prompt": BEAUTIFY_PROMPT_PREFIX + prompt,
                "image": "data:image/png;base64," + qr_b64,
                "size": "1024x1024",
                "response_format": "b64_json",
            },
            timeout=120,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"调用 Seedream 接口失败：{e}"}), 502

    try:
        rj = resp.json()
    except ValueError:
        rj = None
    if resp.status_code >= 400:
        # 错误透传：状态码 + 上游响应体前 300 字符，便于排查中转网关问题
        body_snippet = (resp.text or "").strip()[:300]
        msg = None
        if isinstance(rj, dict):
            err = rj.get("error")
            if isinstance(err, dict):
                msg = err.get("message")
            msg = msg or rj.get("message")
        parts = [f"Seedream 接口返回 HTTP {resp.status_code}"]
        if msg:
            parts.append(str(msg))
        if body_snippet and body_snippet != msg:
            parts.append(f"响应体：{body_snippet}")
        return jsonify({"error": "；".join(parts)}), 502

    # 成功：优先 data[0].b64_json，否则尝试 data[0].url 下载
    b64_img = url_img = None
    if isinstance(rj, dict):
        try:
            d0 = rj["data"][0]
            b64_img = d0.get("b64_json")
            url_img = d0.get("url")
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
    img_bytes = None
    if b64_img:
        try:
            img_bytes = base64.b64decode(b64_img)
        except (ValueError, binascii.Error):
            return jsonify({"error": "Seedream 返回的图片数据无法解析"}), 502
    elif url_img:
        try:
            dl = requests.get(url_img, timeout=120)
        except requests.RequestException as e:
            return jsonify({"error": f"下载美化图失败：{e}"}), 502
        if dl.status_code >= 400:
            return jsonify({"error": f"下载美化图失败：HTTP {dl.status_code}"}), 502
        img_bytes = dl.content
    if not img_bytes:
        return jsonify({"error": "Seedream 返回结果中没有图片数据"}), 502

    fname = f"beautified_{item_id}.png"
    with open(os.path.join(UPLOAD_DIR, fname), "wb") as out:
        out.write(img_bytes)  # 覆盖旧美化图
    db.execute("UPDATE items SET beautified_image=? WHERE id=?", (fname, item_id))
    db.commit()
    scannable = check_scannable(img_bytes, f"{BASE_URL}/v/{item_id}")
    return jsonify({
        "beautified_url": f"{P}/uploads/{fname}",
        "scannable": scannable,
    }), 200


@app.get(P + "/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ------------------------------------------------------------------ 页面模板
BASE_CSS = """
:root{
  --bg:#faf6f0; --card:#fffdf9; --ink:#4a4038; --muted:#9a8d7f;
  --accent:#c08a5e; --accent-d:#a9744c; --line:#ece2d6; --danger:#b96a5a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
  "Hiragino Sans GB","Microsoft YaHei",sans-serif;line-height:1.7;}
.wrap{max-width:760px;margin:0 auto;padding:48px 20px 80px;}
h1{font-size:1.6rem;font-weight:600;letter-spacing:.02em;margin:0 0 6px;}
.sub{color:var(--muted);font-size:.9rem;margin-bottom:32px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:28px;margin-bottom:24px;box-shadow:0 1px 3px rgba(120,95,70,.06);}
label{display:block;font-size:.85rem;color:var(--muted);margin:18px 0 6px;}
input[type=text],textarea{width:100%;border:1px solid var(--line);border-radius:9px;
  padding:10px 12px;font-size:.95rem;background:#fff;color:var(--ink);font-family:inherit;}
textarea{min-height:120px;resize:vertical;white-space:pre-wrap;}
input:focus,textarea:focus{outline:2px solid #e8d4bf;border-color:var(--accent);}
.btn{display:inline-block;background:var(--accent);color:#fff;border:none;
  border-radius:9px;padding:11px 26px;font-size:.95rem;cursor:pointer;
  text-decoration:none;transition:background .15s;}
.btn:hover{background:var(--accent-d);}
.btn.ghost{background:transparent;color:var(--accent);border:1px solid var(--accent);}
.btn.ghost:hover{background:#f7ede2;}
.btn.danger{background:var(--danger);}
.btn.sm{padding:6px 14px;font-size:.82rem;}
.note{font-size:.8rem;color:var(--muted);}
.err{color:var(--danger);font-size:.85rem;margin-top:10px;white-space:pre-wrap;}
.dropzone{border:2px dashed var(--line);border-radius:12px;padding:24px 16px;
  text-align:center;cursor:pointer;background:#fff;
  transition:border-color .15s,background .15s;}
.dropzone:hover{border-color:#d8c3a8;background:#fdfaf5;}
.dropzone.dragover{border-color:var(--accent);background:#f7ede2;}
.dz-main{font-size:.92rem;color:var(--ink);}
.dz-sub{font-size:.8rem;color:var(--muted);margin-top:4px;}
.preview{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px;}
.preview:empty{display:none;}
.pv-item{position:relative;width:88px;height:88px;border-radius:9px;overflow:hidden;
  border:1px solid var(--line);}
.pv-item img{width:100%;height:100%;object-fit:cover;display:block;}
.pv-item button{position:absolute;top:3px;right:3px;background:rgba(185,106,90,.92);
  color:#fff;border:none;border-radius:6px;font-size:.72rem;padding:2px 7px;cursor:pointer;}
.blk{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);
  border-radius:12px;padding:12px;margin-bottom:12px;background:#fff;}
.blk-ctl{display:flex;flex-direction:column;gap:6px;flex-shrink:0;}
.blk-btn{width:30px;height:30px;border:1px solid var(--line);border-radius:7px;
  background:#fdfaf5;color:var(--muted);cursor:pointer;font-size:.85rem;padding:0;
  line-height:1;}
.blk-btn:hover{border-color:var(--accent);color:var(--accent);}
.blk-btn.del:hover{border-color:var(--danger);color:var(--danger);}
.blk-btn:disabled{opacity:.35;cursor:default;}
.blk-text{flex:1;min-height:84px;}
.blk-img{flex:1;min-width:0;}
.blk-img img{max-width:100%;max-height:200px;border-radius:9px;
  border:1px solid var(--line);display:block;}
.blk-img .note{margin-top:4px;}
.blk-empty{color:var(--muted);font-size:.85rem;text-align:center;padding:14px 0;
  border:1px dashed var(--line);border-radius:12px;margin-bottom:12px;}
.blk-actions{display:flex;gap:10px;align-items:stretch;flex-wrap:wrap;margin-top:4px;}
.blk-actions .dropzone{flex:1;min-width:220px;padding:18px 14px;}
.btf{margin-top:26px;border-top:1px dashed var(--line);padding-top:20px;text-align:left;}
.btf h3{font-size:1rem;font-weight:600;margin:0 0 4px;}
.btf .btf-result{text-align:center;margin-top:16px;}
.btf .btf-result:empty{display:none;}
.btf .btf-result img{width:220px;height:220px;border:1px solid var(--line);
  border-radius:10px;background:#fff;padding:8px;display:block;margin:0 auto 12px;}
.btf .btn:disabled{opacity:.6;cursor:default;}
.btf-badge{display:inline-block;border-radius:8px;padding:5px 12px;font-size:.82rem;
  margin:0 auto 12px;border:1px solid;line-height:1.5;}
.btf-badge.ok{background:#eef3ec;border-color:#c3d2c4;color:#5f7f66;}
.btf-badge.fail{background:#f7ecea;border-color:#e0c3bc;color:#a15c4e;}
.btf-badge.na{background:#f3f0ec;border-color:#ddd5cb;color:#8d8177;}
"""

EDITOR_JS = """
// ---- 有序内容块编辑器（创建页 / 管理页编辑弹层共用）--------------------
// 块模型：
//   文字块     {type:'text', text:string}
//   新图片块   {type:'image', ref:'img_N', fileObj:File, url:objectURL, name:string}
//   已有图片块 {type:'image', file:'<已保存文件名>', url:'<完整URL>'}
// 提交时 appendFormData 把 blocks(JSON) 与每个新图片的文件字段（字段名 = ref）写入 FormData。
function createBlockEditor(cfg){
  let blocks = [];
  let imgSeq = 0;

  function el(tag, cls, text){
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function render(){
    cfg.list.innerHTML = '';
    if (!blocks.length){
      cfg.list.appendChild(el('div', 'blk-empty', '暂无内容块，用下方按钮添加文字或图片。'));
      return;
    }
    blocks.forEach(function(b, i){
      const row = el('div', 'blk');
      const ctl = el('div', 'blk-ctl');
      const up = el('button', 'blk-btn', '\\u2191');
      up.type = 'button'; up.title = '上移'; up.disabled = (i === 0);
      const down = el('button', 'blk-btn', '\\u2193');
      down.type = 'button'; down.title = '下移'; down.disabled = (i === blocks.length - 1);
      const del = el('button', 'blk-btn del', '\\u2715');
      del.type = 'button'; del.title = '删除';
      up.addEventListener('click', function(){ move(i, -1); });
      down.addEventListener('click', function(){ move(i, 1); });
      del.addEventListener('click', function(){ remove(i); });
      ctl.appendChild(up); ctl.appendChild(down); ctl.appendChild(del);
      row.appendChild(ctl);
      if (b.type === 'text'){
        const ta = el('textarea', 'blk-text');
        ta.value = b.text || '';
        ta.placeholder = '文字内容，支持多行…';
        ta.rows = 3;
        ta.addEventListener('input', function(){ b.text = ta.value; });
        row.appendChild(ta);
      } else {
        const wrap = el('div', 'blk-img');
        const img = el('img');
        img.src = b.url;
        img.alt = b.name || b.file || '图片块';
        wrap.appendChild(img);
        if (b.file) wrap.appendChild(el('div', 'note', '已保存图片：' + b.file));
        row.appendChild(wrap);
      }
      cfg.list.appendChild(row);
    });
  }

  function move(i, d){
    const j = i + d;
    if (j < 0 || j >= blocks.length) return;
    const t = blocks[i]; blocks[i] = blocks[j]; blocks[j] = t;
    render();
  }
  function remove(i){ blocks.splice(i, 1); render(); }
  function addText(t){ blocks.push({type: 'text', text: t || ''}); render(); }

  // 点击选择 / 拖拽 / 粘贴的图片统一从这里进入，追加为图片块到列表末尾
  function addFiles(fileList){
    let rejected = 0;
    Array.from(fileList || []).forEach(function(f){
      if (f.type && f.type.indexOf('image/') === 0){
        blocks.push({
          type: 'image',
          ref: 'img_' + (imgSeq++),
          fileObj: f,
          url: URL.createObjectURL(f),
          name: f.name || ''
        });
      } else rejected++;
    });
    if (rejected) cfg.onError('已忽略 ' + rejected + ' 个非图片文件，仅支持图片上传。');
    render();
  }

  // 从接口详情加载（blocks 为空时调用方传 legacy 转换结果亦可，结构一致）
  function load(apiBlocks){
    blocks = [];
    (apiBlocks || []).forEach(function(b){
      if (b.type === 'text') blocks.push({type: 'text', text: b.text || ''});
      else if (b.type === 'image') blocks.push({type: 'image', file: b.file, url: b.url});
    });
    render();
  }

  function appendFormData(fd){
    const out = [];
    blocks.forEach(function(b){
      if (b.type === 'text'){
        if ((b.text || '').trim()) out.push({type: 'text', text: b.text});
      } else if (b.file){
        out.push({type: 'image', ref: b.file});   // 引用已保存文件
      } else {
        out.push({type: 'image', ref: b.ref});    // 新上传文件字段
        fd.append(b.ref, b.fileObj, b.name || 'image.png');
      }
    });
    fd.append('blocks', JSON.stringify(out));
  }

  function clear(){ blocks = []; imgSeq = 0; render(); }

  // 图片拖放区：点击选择 / 拖拽进入（粘贴由页面级 paste 监听调 addFiles）
  cfg.zone.addEventListener('click', function(){ cfg.input.click(); });
  cfg.input.addEventListener('change', function(){
    if (cfg.input.files.length) addFiles(Array.from(cfg.input.files));
    cfg.input.value = '';
  });
  ['dragenter','dragover'].forEach(function(ev){
    cfg.zone.addEventListener(ev, function(e){
      e.preventDefault();
      cfg.zone.classList.add('dragover');
    });
  });
  ['dragleave','drop'].forEach(function(ev){
    cfg.zone.addEventListener(ev, function(e){
      e.preventDefault();
      cfg.zone.classList.remove('dragover');
    });
  });
  cfg.zone.addEventListener('drop', function(e){
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length)
      addFiles(e.dataTransfer.files);
  });
  if (cfg.addTextBtn)
    cfg.addTextBtn.addEventListener('click', function(){ addText(''); });

  render();
  return {addFiles: addFiles, addText: addText, load: load,
          clear: clear, appendFormData: appendFormData};
}
// 从 paste 事件中提取图片文件；非图片的文件项计入 rejected 由调用方提示
function imagesFromPaste(e){
  const files = [];
  let rejected = 0;
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return {files: files, rejected: rejected};
  for (const it of items){
    if (it.kind !== 'file') continue;
    if (it.type && it.type.indexOf('image/') === 0){
      const f = it.getAsFile();
      if (f) files.push(f);
    } else rejected++;
  }
  return {files: files, rejected: rejected};
}
"""

BEAUTIFY_JS = """
// ---- ✨ AI 美化区块（创建结果区 / 管理页二维码弹层共用）------------------
// cfg: {root: 容器元素, id: 条目 id, beautifiedUrl: 已有美化图 URL 或 null}
function createBeautifyWidget(cfg){
  const root = cfg.root;
  root.className = 'btf';
  root.innerHTML =
    '<h3>✨ AI 美化</h3>' +
    '<label style="margin-top:8px;">美化提示词</label>' +
    '<input type="text" class="btf-prompt">' +
    '<div style="margin-top:12px;">' +
      '<button type="button" class="btn sm btf-gen">生成美化二维码</button>' +
    '</div>' +
    '<div class="err btf-err"></div>' +
    '<div class="btf-result"></div>' +
    '<p class="note" style="margin-top:14px;">AI 美化可能降低识别率，请务必扫码测试后再使用；原始二维码始终可用。</p>';
  const promptInput = root.querySelector('.btf-prompt');
  promptInput.value = '二维码融入中国风山水画背景，色彩淡雅，意境悠远';
  const genBtn = root.querySelector('.btf-gen');
  const errBox = root.querySelector('.btf-err');
  const resultBox = root.querySelector('.btf-result');

  function showResult(url, scannable){
    resultBox.innerHTML = '';
    if (scannable === true || scannable === false || scannable === null){
      const badge = document.createElement('div');
      if (scannable === true){
        badge.className = 'btf-badge ok';
        badge.textContent = '✓ 扫码验证通过';
      } else if (scannable === false){
        badge.className = 'btf-badge fail';
        badge.textContent = '✗ 未能识别，建议调整提示词重新生成（可尝试减少美化强度、强调保留二维码清晰度）';
      } else {
        badge.className = 'btf-badge na';
        badge.textContent = '检测不可用，请手动扫码测试';
      }
      resultBox.appendChild(badge);
    }
    const img = document.createElement('img');
    // 缓存破坏参数，确保覆盖生成后立刻看到新图
    img.src = url + (url.indexOf('?') >= 0 ? '&' : '?') + 't=' + Date.now();
    img.alt = 'AI 美化二维码';
    resultBox.appendChild(img);
    const dl = document.createElement('a');
    dl.className = 'btn ghost sm';
    dl.textContent = '下载美化图';
    dl.href = url;
    dl.setAttribute('download', '');
    resultBox.appendChild(dl);
  }
  if (cfg.beautifiedUrl) showResult(cfg.beautifiedUrl);

  genBtn.addEventListener('click', function(){
    errBox.textContent = '';
    const prompt = promptInput.value.trim();
    if (!prompt){ errBox.textContent = '请先填写美化提示词'; return; }
    genBtn.disabled = true;
    genBtn.textContent = '生成中，约需 1 分钟...';
    fetch('{{ p }}/api/items/' + cfg.id + '/beautify', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt: prompt})
    }).then(function(res){
      return res.json().catch(function(){ return {}; }).then(function(data){
        if (!res.ok){
          errBox.textContent = data.error || ('生成失败：HTTP ' + res.status);
          return;
        }
        showResult(data.beautified_url, data.scannable);
      });
    }).catch(function(e){
      errBox.textContent = '网络异常或超时，生成失败，请稍后重试。';
    }).finally(function(){
      genBtn.disabled = false;
      genBtn.textContent = '生成美化二维码';
    });
  });
}
"""

CREATE_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>创建二维码 · QR Content Manager</title>
<style>""" + BASE_CSS + """
.result{text-align:center;display:none;}
.result img{width:220px;height:220px;border:1px solid var(--line);border-radius:10px;
  background:#fff;padding:8px;}
.linkrow{display:flex;gap:8px;margin:14px 0;}
.linkrow input{flex:1;border:1px solid var(--line);border-radius:9px;
  padding:9px 12px;font-size:.85rem;background:#fff;color:var(--ink);}
.topbar{display:flex;justify-content:space-between;align-items:baseline;}
</style></head><body>
<div class="wrap">
  <div class="topbar">
    <h1>创建二维码</h1>
    <a class="note" href="{{ p }}/admin">管理后台 →</a>
  </div>
  <p class="sub">上传图片、填写文字，生成可长期使用的二维码。之后随时可在后台编辑内容，二维码不变。</p>

  <div class="card">
    <form id="createForm" enctype="multipart/form-data">
      <label for="title">标题</label>
      <input type="text" id="title" name="title" placeholder="例如：产品介绍 / 活动说明">
      <label>内容块（文字与图片可自由排序，扫码页按此顺序展示）</label>
      <div id="blocks"></div>
      <div class="blk-actions">
        <button class="btn ghost sm" type="button" id="addText">+ 添加文字块</button>
        <div class="dropzone" id="dz">
          <div class="dz-main">+ 添加图片块：点击选择 / 拖拽图片到此处</div>
          <div class="dz-sub">或复制图片后按 Ctrl+V 粘贴（jpg/png/gif/webp，单张 ≤10MB）</div>
        </div>
      </div>
      <input type="file" id="images" multiple accept="image/*" hidden>
      <div style="margin-top:26px">
        <button class="btn" type="submit">生成二维码</button>
      </div>
      <div class="err" id="err"></div>
    </form>
  </div>

  <div class="card result" id="result">
    <h2 style="font-size:1.1rem;margin:0 0 14px;">已生成 ✓</h2>
    <img id="qrImg" alt="二维码">
    <div class="linkrow">
      <input id="viewUrl" readonly>
      <button class="btn ghost sm" id="copyBtn" type="button">复制链接</button>
    </div>
    <a class="btn sm" id="dlBtn" download>下载二维码 PNG</a>
    <a class="btn ghost sm" id="viewBtn" target="_blank" style="margin-left:8px">打开查看页</a>
    <div id="beautifyBox"></div>
    <p class="note" style="margin-top:18px">提示：保存此二维码即可长期使用；今后在后台编辑内容，二维码保持不变。</p>
  </div>
</div>
<script>""" + EDITOR_JS + BEAUTIFY_JS + """
const editor = createBlockEditor({
  list: document.getElementById('blocks'),
  addTextBtn: document.getElementById('addText'),
  zone: document.getElementById('dz'),
  input: document.getElementById('images'),
  onError: function(msg){ document.getElementById('err').textContent = msg; }
});
// 创建页整页监听粘贴，把剪贴板中的图片追加为图片块
document.addEventListener('paste', function(e){
  const r = imagesFromPaste(e);
  if (!r.files.length && !r.rejected) return;
  e.preventDefault();
  if (r.files.length) editor.addFiles(r.files);
  if (r.rejected)
    document.getElementById('err').textContent = '已忽略 ' + r.rejected + ' 个非图片内容，仅支持图片。';
});
const form = document.getElementById('createForm');
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  document.getElementById('err').textContent = '';
  const fd = new FormData();
  fd.append('title', document.getElementById('title').value);
  editor.appendFormData(fd);
  const res = await fetch('{{ p }}/api/items', {method:'POST', body:fd});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { document.getElementById('err').textContent = data.error || ('请求失败：' + res.status); return; }
  editor.clear();
  const box = document.getElementById('result');
  box.style.display = 'block';
  document.getElementById('qrImg').src = data.qr_url;
  document.getElementById('viewUrl').value = data.view_url;
  document.getElementById('dlBtn').href = data.qr_url;
  document.getElementById('viewBtn').href = data.view_url;
  // 新条目无美化图，直接挂载 AI 美化区块
  createBeautifyWidget({
    root: document.getElementById('beautifyBox'),
    id: data.id,
    beautifiedUrl: null
  });
  box.scrollIntoView({behavior:'smooth'});
});
document.getElementById('copyBtn').addEventListener('click', async () => {
  const v = document.getElementById('viewUrl').value;
  try { await navigator.clipboard.writeText(v); } catch(e) {
    const i = document.getElementById('viewUrl'); i.select(); document.execCommand('copy');
  }
  document.getElementById('copyBtn').textContent = '已复制';
});
</script>
</body></html>
"""

ADMIN_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>管理后台 · QR Content Manager</title>
<style>""" + BASE_CSS + """
table{width:100%;border-collapse:collapse;font-size:.9rem;}
th,td{padding:12px 10px;text-align:left;border-bottom:1px solid var(--line);
  vertical-align:middle;}
th{color:var(--muted);font-weight:500;font-size:.8rem;}
.ops{display:flex;gap:6px;flex-wrap:wrap;}
.empty{text-align:center;color:var(--muted);padding:60px 0;}
.modal{position:fixed;inset:0;background:rgba(74,64,56,.45);display:none;
  align-items:center;justify-content:center;padding:20px;z-index:10;}
.modal.open{display:flex;}
.modal .box{background:var(--card);border-radius:14px;padding:28px;width:100%;
  max-width:560px;max-height:88vh;overflow:auto;}
.qrbox{text-align:center;}
.qrbox img{width:220px;height:220px;border:1px solid var(--line);border-radius:10px;
  background:#fff;padding:8px;}
@media(max-width:640px){ .hide-sm{display:none;} table{font-size:.82rem;} }
</style></head><body>
<div class="wrap">
  <div class="topbar" style="display:flex;justify-content:space-between;align-items:center;">
    <h1>管理后台</h1>
    <div style="display:flex;align-items:center;gap:14px;">
      <button class="btn ghost sm" id="aiSettingsBtn" type="button">⚙ AI 设置</button>
      <a class="note" href="{{ p }}/">← 创建新二维码</a>
    </div>
  </div>
  <p class="sub">编辑条目内容不会改变二维码（id 与链接保持不变），已印刷/分发的二维码可继续长期使用。</p>
  <div class="card" style="padding:8px 18px;">
    <div id="list"></div>
  </div>
</div>

<div class="modal" id="qrModal">
  <div class="box qrbox">
    <h2 style="font-size:1.05rem;margin:0 0 14px;" id="qrTitle"></h2>
    <img id="qrModalImg" alt="二维码">
    <div style="margin-top:16px;">
      <a class="btn sm" id="qrDl" download>下载 PNG</a>
      <a class="btn ghost sm" id="qrOpen" target="_blank" style="margin-left:8px">打开查看页</a>
      <button class="btn ghost sm" style="margin-left:8px" onclick="closeModal('qrModal')">关闭</button>
    </div>
    <div id="qrBeautify"></div>
  </div>
</div>

<div class="modal" id="setModal">
  <div class="box">
    <h2 style="font-size:1.05rem;margin:0 0 4px;">⚙ AI 设置（Seedream / 火山方舟）</h2>
    <p class="note">配置后即可在二维码弹层中使用「✨ AI 美化」生成艺术二维码。</p>
    <label>API Key</label>
    <input type="text" id="sKey" placeholder="请到火山方舟控制台（ARK）创建 API Key 后粘贴于此" autocomplete="off">
    <div class="note" id="sKeyStatus" style="margin-top:6px;"></div>
    <div style="margin-top:8px;">
      <button class="btn ghost sm" id="sKeyClear" type="button">清除已保存的 Key</button>
    </div>
    <label>模型</label>
    <input type="text" id="sModel" placeholder="doubao-seedream-4-0-250828">
    <label>Base URL</label>
    <input type="text" id="sBase" placeholder="https://ark.cn-beijing.volces.com/api/v3">
    <div class="note" style="margin-top:6px;">填写 API 基础地址（如 https://ark.cn-beijing.volces.com/api/v3）或完整接口地址（…/v1/images/generations），程序会自动识别。</div>
    <div class="err" id="sErr"></div>
    <div class="note" id="sOk" style="margin-top:8px;"></div>
    <div style="margin-top:22px;display:flex;gap:10px;">
      <button class="btn" id="sSave">保存</button>
      <button class="btn ghost" onclick="closeModal('setModal')">取消</button>
    </div>
  </div>
</div>

<div class="modal" id="editModal">
  <div class="box">
    <h2 style="font-size:1.05rem;margin:0 0 4px;">编辑条目</h2>
    <p class="note">保存后二维码与链接不变。内容块顺序即扫码页展示顺序。</p>
    <label>标题</label>
    <input type="text" id="eTitle">
    <label>内容块（文字与图片可排序 / 删除 / 追加）</label>
    <div id="eBlocks"></div>
    <div class="blk-actions">
      <button class="btn ghost sm" type="button" id="eAddText">+ 添加文字块</button>
      <div class="dropzone" id="eDz">
        <div class="dz-main">+ 添加图片块：点击选择 / 拖拽图片到此处</div>
        <div class="dz-sub">或复制图片后按 Ctrl+V 粘贴</div>
      </div>
    </div>
    <input type="file" id="eFiles" multiple accept="image/*" hidden>
    <div class="err" id="eErr"></div>
    <div id="eBeautify"></div>
    <p class="note" style="margin-top:6px;">美化针对当前二维码链接，与内容编辑互不影响。</p>
    <div style="margin-top:22px;display:flex;gap:10px;">
      <button class="btn" id="eSave">保存</button>
      <button class="btn ghost" onclick="closeModal('editModal')">取消</button>
    </div>
  </div>
</div>

<script>""" + EDITOR_JS + BEAUTIFY_JS + """
let editingId = null;

const eEditor = createBlockEditor({
  list: document.getElementById('eBlocks'),
  addTextBtn: document.getElementById('eAddText'),
  zone: document.getElementById('eDz'),
  input: document.getElementById('eFiles'),
  onError: function(msg){ document.getElementById('eErr').textContent = msg; }
});
// 仅在编辑弹层打开时响应粘贴，图片进入弹层的追加列表，避免与其他弹层冲突
document.addEventListener('paste', function(e){
  if (!document.getElementById('editModal').classList.contains('open')) return;
  const r = imagesFromPaste(e);
  if (!r.files.length && !r.rejected) return;
  e.preventDefault();
  if (r.files.length) eEditor.addFiles(r.files);
  if (r.rejected)
    document.getElementById('eErr').textContent = '已忽略 ' + r.rejected + ' 个非图片内容，仅支持图片。';
});

function closeModal(id){ document.getElementById(id).classList.remove('open'); }
function esc(s){ const d=document.createElement('div'); d.textContent=s==null?'':s; return d.innerHTML; }

async function load(){
  const res = await fetch('{{ p }}/api/items');
  const items = await res.json();
  const list = document.getElementById('list');
  if (!items.length){ list.innerHTML = '<div class="empty">暂无条目，去 <a href="{{ p }}/">创建</a> 一个吧</div>'; return; }
  let html = '<table><thead><tr><th>标题</th><th class="hide-sm">图片</th><th class="hide-sm">创建时间</th><th>操作</th></tr></thead><tbody>';
  for (const it of items){
    html += '<tr><td>' + esc(it.title || '(无标题)') + '<div class="note">{{ p }}/v/' + esc(it.id) + '</div></td>'
      + '<td class="hide-sm">' + it.image_count + ' 张</td>'
      + '<td class="hide-sm">' + esc((it.created_at||'').replace('T',' ').slice(0,16)) + '</td>'
      + '<td><div class="ops">'
      + '<button class="btn ghost sm" onclick="showQr(\\''+it.id+'\\', \\''+esc(it.title||'')+'\\')">二维码</button>'
      + '<button class="btn ghost sm" onclick="openEdit(\\''+it.id+'\\')">编辑</button>'
      + '<button class="btn danger sm" onclick="delItem(\\''+it.id+'\\')">删除</button>'
      + '</div></td></tr>';
  }
  list.innerHTML = html + '</tbody></table>';
}

async function showQr(id, title){
  const d = await (await fetch('{{ p }}/api/items/' + id)).json();
  document.getElementById('qrTitle').textContent = title || d.title || id;
  document.getElementById('qrModalImg').src = d.qr_url;
  document.getElementById('qrDl').href = d.qr_url;
  document.getElementById('qrOpen').href = d.view_url;
  // AI 美化区块：已有美化图时直接展示
  createBeautifyWidget({
    root: document.getElementById('qrBeautify'),
    id: id,
    beautifiedUrl: d.beautified_url || null
  });
  document.getElementById('qrModal').classList.add('open');
}

// ---- ⚙ AI 设置弹层 ------------------------------------------------------
async function openSettings(){
  document.getElementById('sErr').textContent = '';
  document.getElementById('sOk').textContent = '';
  const d = await (await fetch('{{ p }}/api/settings')).json();
  document.getElementById('sModel').value = d.seedream_model || '';
  document.getElementById('sBase').value = d.seedream_base_url || '';
  document.getElementById('sKey').value = '';
  document.getElementById('sKeyStatus').textContent = d.has_key
    ? ('当前状态：已保存（' + d.seedream_api_key + '）。输入新值即覆盖；留空保存则不修改。')
    : '当前状态：未设置。';
  document.getElementById('setModal').classList.add('open');
}
document.getElementById('aiSettingsBtn').addEventListener('click', openSettings);

document.getElementById('sSave').addEventListener('click', async () => {
  const body = {
    seedream_model: document.getElementById('sModel').value.trim(),
    seedream_base_url: document.getElementById('sBase').value.trim()
  };
  const k = document.getElementById('sKey').value.trim();
  if (k) body.seedream_api_key = k;  // 留空 = 不修改已保存的 Key
  const res = await fetch('{{ p }}/api/settings', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok){
    document.getElementById('sErr').textContent = d.error || ('保存失败：' + res.status);
    return;
  }
  await openSettings();
  document.getElementById('sOk').textContent = '已保存 ✓';
});

document.getElementById('sKeyClear').addEventListener('click', async () => {
  await fetch('{{ p }}/api/settings', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({seedream_api_key: ''})  // 空串 = 清除
  });
  await openSettings();
  document.getElementById('sOk').textContent = 'API Key 已清除 ✓';
});

async function openEdit(id){
  const d = await (await fetch('{{ p }}/api/items/' + id)).json();
  editingId = id;
  document.getElementById('eTitle').value = d.title;
  // blocks 由接口返回（legacy 数据服务端已转换），直接加载进块编辑器
  eEditor.load(d.blocks);
  document.getElementById('eErr').textContent = '';
  // AI 美化区块：针对条目本身的二维码（id 不变），已有美化图时直接展示
  createBeautifyWidget({
    root: document.getElementById('eBeautify'),
    id: id,
    beautifiedUrl: d.beautified_url || null
  });
  document.getElementById('editModal').classList.add('open');
}

document.getElementById('eSave').addEventListener('click', async () => {
  const fd = new FormData();
  fd.append('title', document.getElementById('eTitle').value);
  eEditor.appendFormData(fd);
  const res = await fetch('{{ p }}/api/items/' + editingId, {method:'PUT', body:fd});
  const data = await res.json().catch(() => ({}));
  if (!res.ok){ document.getElementById('eErr').textContent = data.error || ('保存失败：' + res.status); return; }
  eEditor.clear();
  closeModal('editModal'); load();
});

async function delItem(id){
  if (!confirm('确认删除该条目？图片与数据将一并删除，二维码将失效。')) return;
  const res = await fetch('{{ p }}/api/items/' + id, {method:'DELETE'});
  if (res.status === 204) load();
}

load();
</script>
</body></html>
"""

VIEW_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }}</title>
<style>
:root{--bg:#faf6f0;--card:#fffdf9;--ink:#4a4038;--muted:#9a8d7f;--line:#ece2d6;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
  "Hiragino Sans GB","Microsoft YaHei",sans-serif;line-height:1.8;}
.wrap{max-width:640px;margin:0 auto;padding:40px 18px 72px;}
h1{font-size:1.5rem;font-weight:600;margin:0 0 22px;line-height:1.4;}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:26px 22px;box-shadow:0 1px 3px rgba(120,95,70,.06);}
.content{white-space:pre-wrap;word-break:break-word;font-size:1rem;}
.blk-text{margin:14px 0;}
.blk-text:first-child{margin-top:0;}
.blk-text:last-child{margin-bottom:0;}
.blk-image{margin:14px 0;}
.blk-image img{width:100%;border-radius:12px;display:block;border:1px solid var(--line);}
.footer{margin-top:34px;text-align:center;color:var(--muted);font-size:.78rem;}
</style></head><body>
<div class="wrap">
  <h1>{{ title }}</h1>
  <div class="card">
    {% for b in blocks %}
      {% if b.type == 'text' %}
    <div class="content blk-text">{{ b.text }}</div>
      {% else %}
    <div class="blk-image"><img src="{{ b.url }}" alt="" loading="lazy"></div>
      {% endif %}
    {% endfor %}
  </div>
  <div class="footer">由 QR Content Manager 生成</div>
</div>
</body></html>
"""

NOTFOUND_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>内容不存在</title>
<style>
body{margin:0;background:#faf6f0;color:#4a4038;display:flex;min-height:100vh;
  align-items:center;justify-content:center;text-align:center;
  font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}
.box{padding:40px 24px;}
h1{font-size:1.3rem;font-weight:600;margin-bottom:10px;}
p{color:#9a8d7f;font-size:.9rem;}
</style></head><body>
<div class="box">
  <h1>内容不存在或已删除</h1>
  <p>该二维码对应的内容可能已被管理员移除。</p>
</div>
</body></html>
"""


# ------------------------------------------------------------------ 页面路由
@app.get(P + "/")
def page_create():
    return render_template_string(CREATE_HTML, p=P)


@app.get(P + "/admin")
def page_admin():
    return render_template_string(ADMIN_HTML, p=P)


@app.get(P + "/v/<item_id>")
def page_view(item_id):
    if not re.fullmatch(r"[0-9a-f]{8}", item_id or ""):
        return render_template_string(NOTFOUND_HTML), 404
    row = get_db().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return render_template_string(NOTFOUND_HTML), 404
    d = item_detail(row)
    # 按块顺序渲染；空文字块跳过。Jinja 自动转义所有输出，防 XSS；
    # 换行由 CSS white-space:pre-wrap 保留
    blocks = [b for b in d["blocks"] if b["type"] == "image" or b["text"].strip()]
    return render_template_string(
        VIEW_HTML,
        p=P,
        title=d["title"] or "内容详情",
        blocks=blocks,
    )


@app.get(P + "/admin/edit/<item_id>")
def page_edit_redirect(item_id):
    # 编辑在管理页弹层完成，此路由仅作兼容跳转
    return redirect(url_for("page_admin"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
