# -*- coding: utf-8 -*-
"""multipart/form-data 解析 — 手动实现，不引外部库

server 拆分产物：纯函数，零业务依赖。
"""


def parse_multipart(handler):
    """从 POST 请求解析 multipart/form-data。

    Returns:
        (fields, files) — fields: {name: str 值}，files: {name: {"filename": str, "data": bytes}}
    """
    content_type = handler.headers.get("Content-Type", "")
    if not content_type.startswith("multipart/form-data"):
        return {}, {}
    # 提取 boundary
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip('"')
            break
    if not boundary:
        return {}, {}

    length = int(handler.headers.get("Content-Length", 0))
    # 上传大小上限 10MB：防大文件撑爆内存/磁盘（本地单用户）
    if length <= 0 or length > 10 * 1024 * 1024:
        return {}, {}
    body = handler.rfile.read(length)
    boundary_bytes = ("--" + boundary).encode("utf-8")

    fields = {}
    files = {}
    # 按 boundary 分块
    chunks = body.split(boundary_bytes)
    for chunk in chunks:
        # 去掉首尾的 \r\n
        if chunk in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        # 找 headers 和 content 的分隔（空行 \r\n\r\n）
        sep_idx = chunk.find(b"\r\n\r\n")
        if sep_idx < 0:
            continue
        header_bytes = chunk[:sep_idx].decode("utf-8", errors="replace")
        content = chunk[sep_idx + 4:]

        # 解析 Content-Disposition
        name = None
        filename = None
        for line in header_bytes.split("\r\n"):
            if "Content-Disposition" in line:
                for seg in line.split(";"):
                    seg = seg.strip()
                    if seg.startswith("name="):
                        name = seg[len("name="):].strip('"')
                    elif seg.startswith("filename="):
                        filename = seg[len("filename="):].strip('"')
        if name is None:
            continue

        if filename is not None:
            files[name] = {"filename": filename, "data": content}
        else:
            fields[name] = content.decode("utf-8", errors="replace").strip()

    return fields, files
