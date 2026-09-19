#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Firefly 聊天 App — HTTP 骨架（分发 + 响应工具 + 启动）

拆分结构：
- modules/app_config.py  路径引导 + 配置状态（全局唯一 user_data 公式）
- modules/multipart.py   multipart/form-data 解析
- routes.py              全部 API 路由（POST_ROUTES / GET_ROUTES 分发表）
- server.py（本文件）    FireflyHandler + 启动块
"""

import json, sys, os, logging, warnings
# 屏蔽 requests 依赖版本不匹配的警告（不影响功能）
warnings.filterwarnings("ignore", message=".*urllib3.*", module="requests")
warnings.filterwarnings("ignore", message=".*chardet.*", module="requests")
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote

# PyInstaller console=False（windowed）下 sys.stdout/stderr 为 None：
# print/logging 会直接崩。此时把输出重定向到 user_data/logs/firefly.log（与 exe 同级）。
# 开发者直接 python server.py 不受影响（终端正常输出）。
if getattr(sys, "frozen", False) and (sys.stdout is None or sys.stderr is None):
    try:
        _log_root = Path(os.environ.get("FIREFLY_DATA_DIR", Path(sys.executable).parent))
        _log_dir = _log_root / "logs"
        _log_dir.mkdir(parents=True, exist_ok=True)
        _log_fp = open(_log_dir / "firefly.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = _log_fp
        sys.stderr = _log_fp
    except OSError:
        pass

# 配置日志输出到终端
logging.basicConfig(level=logging.INFO, format='%(name)s: %(message)s', stream=sys.stderr)

# 将项目根目录加入 sys.path，使 memory/ knowledge/ 可导入
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from modules import app_config as cfg
import routes
from shared_http import (ResponseMixin, setup_stdio_utf8, preload_knowledge,
                         shutdown_server, _SERVER_REF, local_origin_ok)


# ── 热更新覆盖层（见 docs/热更新规范.md）────────────
class LocalServer(ThreadingHTTPServer):
    """本机服务：放大 accept backlog。

    `request_queue_size` 默认 5（`ss -lnt` 里 LISTEN 的 Send-Q 就是它）→ 突发并发
    （多标签页 + relay/主动消息/记忆条/热更 几个轮询同时到）会被内核直接丢弃。
    2026-09-19 压测实测：32 并发时每档恰好 1 次拒连。
    """

    request_queue_size = 128


def _static_file(rel: str) -> Path:
    """静态文件解析：**覆盖层优先，回落只读的 STATIC_DIR**。

    热更新只改这里就能生效：`{hotupdate}/web/<rel>` 命中就用它。
    覆盖层是"只增/只改"的（补丁不允许删文件），所以任何未覆盖的文件自然走底座 ⇒
    即使覆盖层在替换的那一瞬间缺失，用户看到的也只是**底座版本**而不是白屏。
    """
    rel = rel.lstrip("/")
    # 审查：覆盖层路径不许越界（do_GET 已查过一次，这里再查一次——本函数也可能被别处调用）
    if ".." in rel.split("/") or rel.startswith("/"):
        return cfg.STATIC_DIR / "__invalid__"
    try:
        from hotupdate import web_dir
        p = web_dir() / rel
        if p.is_file():
            return p
    except Exception:
        pass          # 热更模块坏了也绝不能影响静态服务
    return cfg.STATIC_DIR / rel


# ── HTTP 服务器 ──────────────────────────────────
class FireflyHandler(ResponseMixin, SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(cfg.STATIC_DIR), **kwargs)

    def do_POST(self):
        # 本地版跨站写防护（审计 A2，2026-09-15）：CORS 只拦"读得到响应"，拦不住
        # 免预检的跨站请求（HTML 表单/multipart POST 属 CORS 简单请求，直接发出）——
        # 写端点在服务端再验一次浏览器来源。非浏览器客户端（多开探测/安装器/curl）
        # 无来源标记不受影响；服务器版由 local_origin_ok 直接放行（Bearer 鉴权，
        # 另一套信任模型）。
        if not local_origin_ok(self):
            self.send_error(403)
            return
        fn = routes.POST_ROUTES.get(urlparse(self.path).path)
        if fn:
            fn(self)
        else:
            self.send_error(404)

    def do_GET(self):
        path = urlparse(self.path).path
        # 路径穿越防护：_serve_file 是手写的，/assets/../config.json 能读走 API Key
        if ".." in unquote(path):
            self.send_error(404)
            return
        # 多开限制：新实例探测旧实例存活性/触发优雅关闭
        if path == "/health":
            self._json({"ok": True, "alive": True})
            return
        if path == "/shutdown":
            # 优雅关闭：新实例启动时调用，保存文件后退出。
            # A1（审计 2026-09-15）：来源校验落地——shared_http.shutdown_server 的文档
            # 铁律原先只写了没执行：任意网页 <img src="http://127.0.0.1:8765/shutdown">
            # 即可静默杀死进程（未落盘的记忆/手账/对话全丢）。本服务只绑 127.0.0.1，
            # 连接必来自本机，但攻击页面跑在用户浏览器里同样"来自本机"——只认浏览器
            # 来源标记。方法保持 GET 不动：新实例探测旧实例（≤0.8.1）走 GET，
            # 改 POST 会把跨版本交接打成兜底强杀（丢优雅保存）。
            if not local_origin_ok(self):
                self.send_error(403)
                return
            self._json({"ok": True, "shutting_down": True})
            import threading as _t
            _t.Timer(0.3, lambda: shutdown_server()).start()
            return
        fn = routes.GET_ROUTES.get(path)
        if fn:
            fn(self)
        # 静态文件路由（unquote 解码中文路径，否则表情包等中文文件名 404）
        elif path.startswith("/assets/"):
            self._serve_file(cfg.resolve_asset("assets/" + unquote(path[8:])))
        elif path.startswith("/static/"):
            self._serve_file(_static_file(unquote(path[8:])))
        elif path == "/" or path == "/index.html":
            self._serve_file(_static_file("index.html"))
        else:
            # 根目录静态文件（轮播图等）：先按编码路径，再按原始中文路径尝试
            # （SimpleHTTPRequestHandler 对未编码中文返回 404，这里统一用 unquote 处理）
            name = unquote(path)
            if name and not name.startswith("/"):
                name = "/" + name
            fp = _static_file(name)
            if fp.exists() and fp.is_file():
                self._serve_file(fp)
            else:
                super().do_GET()

    # ── 响应工具（供 routes 调用）──────────────────
    # _MIME/_serve_file/_json/log_message 来自 shared_http.ResponseMixin


def main():
    setup_stdio_utf8()
    # 旧布局迁移（一次性，幂等）显式执行：原在 app_config import 期自动跑，会导致
    # 服务器版每次启动都对共享 user_data 根做移动式迁移（2026-09-10 修复）。
    # 无待迁移内容时不产生任何副作用（不写备份、不建目录）。
    try:
        _mig = cfg.run_legacy_migration()
        if _mig.get("aborted"):
            print(f"  [WARN] 旧布局迁移已中止（迁移前备份失败）：{_mig.get('error')}", flush=True)
        elif _mig.get("pending"):
            print(f"  [OK] 旧布局迁移：moved={_mig['moved']} skipped={_mig['skipped']} "
                  f"failed={_mig['failed']}", flush=True)
    except Exception as e:
        print(f"  [WARN] 旧布局迁移异常（继续启动）：{e}", flush=True)
    # 首启引导（建目录 + 拷贝默认文件）必须在迁移**之后**：迁移未跑就拷贝默认文件会
    # 占位，导致旧数据迁移被跳过而丢失（该顺序铁律原先靠 import 期串行保证）。
    try:
        _init = cfg.run_startup_init()
        if _init.get("files_copied") or _init.get("stale_removed"):
            print(f"  [OK] 首启引导：拷贝默认文件 {_init['files_copied']} 个，"
                  f"清理未修改副本 {_init['stale_removed']} 个", flush=True)
    except Exception as e:
        print(f"  [WARN] 首启引导异常（继续启动）：{e}", flush=True)
    # B7（审计 2026-09-15）：换入残留回收——上次恢复/导入若死在换目录中途，目标目录
    # 不存在，读取链会静默回退 bundled（对话/记忆/手账"消失"）。扫描 .restore_old 等
    # 残留：X 不存在则回滚找回，其余留置告警（详见 recover_swap_leftovers 文档）。
    try:
        from infra.sync.restore import recover_swap_leftovers
        for _note in recover_swap_leftovers():
            print(f"  [WARN] 启动回收：{_note}", flush=True)
    except Exception as e:
        print(f"  [WARN] 换入残留回收异常（继续启动）：{e}", flush=True)
    # memory 历史文件迁移（阶段 2.6）：原在 memory_manager import 期自动跑，
    # 使"import 即改盘"。现在只在启动链里显式执行一次（幂等）。
    try:
        from modules.memory_manager import migrate_legacy_memory
        migrate_legacy_memory()
    except Exception as e:
        print(f"  [WARN] memory 迁移异常（继续启动）：{e}", flush=True)
    # 表情包注册表播种/迁移（阶段 2.7）：同样原在模块 import 期跑，改为启动链显式执行
    try:
        from domain.stickers.picker import migrate_sticker_seed
        migrate_sticker_seed()
    except Exception as e:
        print(f"  [WARN] 表情包注册表迁移异常（继续启动）：{e}", flush=True)
    preload_knowledge()

    # 端口占用检查——防止旧进程残留导致请求路由到旧代码。
    # 新实例检测到旧实例：先探测 /shutdown 优雅关闭（保存文件），
    # 旧实例无响应（卡死）才兜底强杀。避免 taskkill /F 丢数据。
    import socket as _sock
    import time as _tm   # C1（审计 2026-09-15）：/health 探测失败（端口被非 firefly
    # 进程占用/旧实例卡死）也走强杀分支，_tm 必须在此已定义——原先只在 200 分支内
    # import，强杀分支的 _tm.sleep(1) 直接 NameError 穿透 main()，服务起不来
    _probe = _sock.socket()
    _probe.settimeout(1)
    try:
        _probe.connect(("127.0.0.1", cfg.PORT))
        # 端口被占：旧实例存在
        import urllib.request as _ur
        try:
            with _ur.urlopen(f"http://127.0.0.1:{cfg.PORT}/health", timeout=2) as r:
                if r.status == 200:
                    # 旧实例活着 → 优雅关闭
                    print("  检测到旧实例，正在优雅关闭...", flush=True)
                    try:
                        with _ur.urlopen(f"http://127.0.0.1:{cfg.PORT}/shutdown", timeout=5):
                            pass
                    except Exception:
                        pass
                    # 等端口释放（最多 5s）
                    for _ in range(25):
                        _tm.sleep(0.2)
                        _probe2 = _sock.socket()
                        _probe2.settimeout(0.5)
                        try:
                            _probe2.connect(("127.0.0.1", cfg.PORT))
                        except (ConnectionRefusedError, OSError):
                            _probe2.close()
                            break
                        _probe2.close()
        except Exception:
            pass
        # 端口仍未释放 → 兜底强杀（找 PID）
        try:
            _probe3 = _sock.socket()
            _probe3.settimeout(0.5)
            _probe3.connect(("127.0.0.1", cfg.PORT))
            print("  [WARN] 旧实例未响应优雅关闭，尝试强杀", flush=True)
            _probe3.close()
            _out = os.popen(f'netstat -ano | findstr ":{cfg.PORT}" | findstr LISTENING').read()
            for _line in _out.splitlines():
                _parts = _line.split()
                if len(_parts) >= 5:
                    try:
                        os.system(f"taskkill /F /PID {_parts[-1]} >nul 2>&1")
                    except Exception:
                        pass
                    break
            _tm.sleep(1)
        except (ConnectionRefusedError, OSError):
            pass  # 端口已释放
        finally:
            _probe.close()
    except (ConnectionRefusedError, OSError):
        pass  # 端口空闲
    finally:
        _probe.close()

    # 只绑本机：局域网暴露会让任何人用你的 API Key 聊天/改设定
    # `request_queue_size`：socketserver 默认只有 5，突发并发（多标签页 + 多个轮询同时到）
    # 会被内核直接丢弃。实测 32 并发时每档恰好 1 次拒连（2026-09-19 压测）。
    server = LocalServer(("127.0.0.1", cfg.PORT), FireflyHandler)
    _SERVER_REF["server"] = server
    print("\n  Firefly 聊天 App 启动中...")
    _key = cfg.config.get("api_key", "")
    if _key:
        print(f"  Key OK: {_key[:12]}...", flush=True)
    else:
        print(f"  [WARN] 未检测到 API Key ({cfg.CONFIG_FILE})，请在浏览器中配置", flush=True)
    print(f"  打开浏览器访问: http://localhost:{cfg.PORT}\n")
    # 热更新启动钩子（见 docs/热更新规范.md）：
    #   base 变更清理 → 安全模式判定 → 起后台检查线程。**必须容错**：
    #   热更是后台设施，它出任何问题都不许拦住聊天主链的启动。
    try:
        from hotupdate import on_startup as _hu_startup
        _hu = _hu_startup()
        if _hu.get("note"):
            print(f"  [热更新] {_hu['note']}", flush=True)
    except Exception as _e:
        print(f"  [WARN] 热更新启动钩子异常（已忽略）: {_e}", flush=True)
    # 自动打开浏览器（安卓内嵌模式不打开；环境变量 FIREFLY_NO_BROWSER=1 关闭）
    try:
        if os.environ.get("FIREFLY_NO_BROWSER") != "1" and not os.environ.get("FIREFLY_ANDROID"):
            import webbrowser, threading as _t
            _t.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{cfg.PORT}")).start()
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  关闭服务器")
    finally:
        server.shutdown()
        server.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
