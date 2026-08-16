# -*- coding: utf-8 -*-
"""统一前端三处同步（0.8.0 起：app/static 为唯一源）

方向：
- app/static/{index.html,app.js,style.css,图片...} → server/frontend/（服务器网页调试副本，
  config.js / login.html / admin.html 为 server/ 独有，不覆盖）
- app/static + app/assets 子集 + server/frontend/{config.js,login.html} → android 打包 assets
  （服务器模式 file:// 加载用；本地模式页面由内置引擎 HTTP 提供，assets 仅服务器模式用）

用法：python tools/sync_frontends.py [--check]（--check 只校验 md5 不写文件，漂移即退出 1）
"""
import hashlib
import shutil
import sys
from pathlib import Path

# Windows GBK 控制台打印 ✓ 会 UnicodeEncodeError → 强制 UTF-8（与 check_version 同款兜底）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
SERVER_FRONT = ROOT / "server" / "frontend"
ANDROID_ASSETS = ROOT / "android" / "app" / "src" / "main" / "assets"

# app/assets 中随 APK 打包的子集（服务器模式 file:// 引用的背景/字体/图标）
ASSET_FILES = (
    "background.jpg", "StarRailFont.ttf",
    "icon_home.png", "icon_rest.png", "icon_trash.png", "icon_undo.png",
    "notice_speaker.png", "theme_moon.png", "theme_sun.png",
)


def _md5(fp: Path) -> str:
    return hashlib.md5(fp.read_bytes()).hexdigest()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_static(dst: Path, exclude: tuple = ()) -> None:
    """把 app/static 全部文件拷到 dst（config.js 除外——安卓 assets 用服务器版 config.js）。"""
    for fp in STATIC.iterdir():
        if fp.is_file() and fp.name not in exclude:
            _copy(fp, dst / fp.name)


def _copy_assets(dst_dir: Path) -> None:
    """app/assets 根目录 9 个 UI 文件 → dst/assets/（排除 character/ stickers/ 目录）。"""
    src_dir = ROOT / "app" / "assets"
    for name in ASSET_FILES:
        fp = src_dir / name
        if fp.exists():
            _copy(fp, dst_dir / name)


def _check_pair(name: str, a: Path, b: Path) -> bool:
    ok = a.exists() and b.exists() and _md5(a) == _md5(b)
    if not ok:
        print(f"  X 漂移: {name}  {a}  vs  {b}")
    return ok


def sync(check_only: bool) -> int:
    print("=== 前端同步" + ("（--check 校验模式）" if check_only else "") + " ===")

    # 1) server/frontend：三份共享前端文件（config.js/login.html/admin.html 为 server 独有，不动）
    sf_targets = ("index.html", "app.js", "style.css")
    # 2) 安卓 assets：app/static 全部 + assets 子集 + server 版 config.js/login.html
    aa_static = ("index.html", "app.js", "style.css",
                 "剧情模式.png", "春日手信.png",
                 "开拓者_穹.png", "开拓者_星.png", "流萤_头像.png")

    if check_only:
        ok = True
        for name in sf_targets:
            ok &= _check_pair(name, STATIC / name, SERVER_FRONT / name)
        for name in aa_static:
            ok &= _check_pair(name, STATIC / name, ANDROID_ASSETS / name)
        ok &= _check_pair("config.js", SERVER_FRONT / "config.js", ANDROID_ASSETS / "config.js")
        ok &= _check_pair("login.html", SERVER_FRONT / "login.html", ANDROID_ASSETS / "login.html")
        for name in ASSET_FILES:
            ok &= _check_pair(name, ROOT / "app" / "assets" / name, ANDROID_ASSETS / "assets" / name)
        print("结果:", "一致" if ok else "存在漂移（运行 python tools/sync_frontends.py 同步）")
        return 0 if ok else 1

    for name in sf_targets:
        _copy(STATIC / name, SERVER_FRONT / name)
        print(f"  -> server/frontend/{name}")

    _copy_static(ANDROID_ASSETS, exclude=("config.js",))
    print("  -> android assets（app/static 全量，config.js 除外）")
    _copy_assets(ANDROID_ASSETS / "assets")
    print("  -> android assets/assets（背景/字体/图标 9 件）")
    _copy(SERVER_FRONT / "config.js", ANDROID_ASSETS / "config.js")
    _copy(SERVER_FRONT / "login.html", ANDROID_ASSETS / "login.html")
    print("  -> android assets/config.js + login.html（服务器地址单点 + 登录页）")
    print("同步完成 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(sync("--check" in sys.argv))
