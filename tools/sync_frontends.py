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

# app/assets 根目录**文件**（不含 character/ stickers/ 子目录）必须是这两类的并集。
# 2026-09-13（门禁修复 D-3）：原来 ASSET_FILES 是硬编码 9 项，新加的图标/背景如果忘了登记，
# 网页端正常（直接读 app/assets）但 APK 里缺文件 → 手机服务器模式裂图，且门禁全绿。
# 现在盘上出现未登记文件即 FAIL，逼着显式分类。
ASSET_FILES = (
    "background.jpg", "StarRailFont.ttf",
    "icon_home.png", "icon_rest.png", "icon_trash.png", "icon_undo.png",
    "notice_speaker.png", "theme_moon.png", "theme_sun.png",
)
# 当前**无任何客户端引用**的历史遗留件（2026-09-13 全仓检索无引用：前端 html/css/js、
# firefly.spec、package/firefly.iss、android 资源均未引用）。不入 APK；待阶段 1/6 清理。
# 它们的价值是"必须被显式分类"——将来若真要用，登记进 ASSET_FILES 即可。
ASSET_NOT_SHIPPED = ("icon.png", "icon_tip.svg", "img_header.png", "thumb.svg")


def _check_asset_set() -> bool:
    """app/assets 根目录文件集合断言（目录约定 + 集合比对）。"""
    src_dir = ROOT / "app" / "assets"
    if not src_dir.is_dir():
        print(f"  X app/assets 目录不存在: {src_dir}")
        return False
    actual = {p.name for p in src_dir.iterdir() if p.is_file()}
    expected = set(ASSET_FILES) | set(ASSET_NOT_SHIPPED)
    unlisted = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unlisted:
        print(f"  X app/assets 下有未登记的资产（是否要随 APK 打包？）: {unlisted}")
    if missing:
        print(f"  X 已登记的资产在盘上不存在: {missing}")
    if unlisted or missing:
        print("  补救：要进 APK → 加进 ASSET_FILES；不进 APK → 加进 ASSET_NOT_SHIPPED")
        return False
    return True


def _md5(fp: Path) -> str:
    return hashlib.md5(fp.read_bytes()).hexdigest()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_static(dst: Path, exclude: tuple = ()) -> None:
    """把 app/static 全部文件拷到 dst（config.js 除外——安卓 assets 用服务器版 config.js）。
    含子目录（js/ 模块、vendor/ 依赖），0.9.0 起 app.js 拆分为 js/*.js。"""
    for fp in STATIC.iterdir():
        if fp.is_file() and fp.name not in exclude:
            _copy(fp, dst / fp.name)
    for sub in ("js", "vendor"):
        src_dir = STATIC / sub
        if not src_dir.is_dir():
            continue
        for fp in src_dir.rglob("*"):
            if fp.is_file():
                _copy(fp, dst / sub / fp.relative_to(src_dir))


def _remove_stale(dst: Path, names: tuple) -> None:
    """删除目标处已退役的旧文件（如 0.9.0 拆分前的 app.js）。"""
    for name in names:
        fp = dst / name
        if fp.exists():
            fp.unlink()
            print(f"  -> 删除旧文件 {dst.name}/{name}")


def _check_dir(name: str, a: Path, b: Path) -> bool:
    """校验两个目录树内容一致（文件集合 + md5）。"""
    ok = True
    a_files = {fp.relative_to(a) for fp in a.rglob("*") if fp.is_file()} if a.is_dir() else set()
    b_files = {fp.relative_to(b) for fp in b.rglob("*") if fp.is_file()} if b.is_dir() else set()
    for rel in sorted(a_files | b_files):
        ok &= _check_pair(f"{name}/{rel.as_posix()}", a / rel, b / rel)
    return ok


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

    # 0) 运行时 bundle：js/*.js（模块源码）→ js/bundle.js（classic，三端统一入口）。
    #    不跑这步会把旧 bundle 同步出去（2026-08-18 file:// 拦截 ESM 事故的配套防线）。
    import subprocess
    bcmd = [sys.executable, str(ROOT / "tools" / "build_frontend_bundle.py")]
    rb = subprocess.run(bcmd + (["--check"] if check_only else []), capture_output=True,
                        encoding="utf-8", errors="replace")
    if rb.returncode != 0:
        print("X bundle 生成/校验失败：\n" + (rb.stdout + rb.stderr)[:800])
        return 1
    if check_only and "漂移" in rb.stdout:
        print(rb.stdout.strip())
        return 1

    # 0b) 资产清单断言：app/assets 根目录文件必须已被显式分类（进 APK / 不进 APK）
    if not _check_asset_set():
        return 1

    # 1) server/frontend：三份共享前端文件（config.js/login.html/admin.html 为 server 独有，不动）
    #    pc.css：PC 三栏外壳样式（2026-09-19 起独立成文件，见 docs/设计/PC端前端重构.md）
    sf_targets = ("index.html", "style.css", "pc.css")
    # 2) 安卓 assets：app/static 全部 + assets 子集 + server 版 config.js/login.html
    # 注：模式封面/角色头像已入预设包（assets/character/{包}/assets/），经 syncBackend 随 app/ 进 APK
    aa_static = ("index.html", "style.css", "pc.css",
                 "开拓者_穹.png", "开拓者_星.png")

    if check_only:
        ok = True
        for name in sf_targets:
            ok &= _check_pair(name, STATIC / name, SERVER_FRONT / name)
        ok &= _check_dir("server/js", STATIC / "js", SERVER_FRONT / "js")
        ok &= _check_dir("server/vendor", STATIC / "vendor", SERVER_FRONT / "vendor")
        if (SERVER_FRONT / "app.js").exists():
            print("  X 漂移: server/frontend 旧 app.js 未清理")
            ok = False
        if (SERVER_FRONT / "vendor").exists() and not any((SERVER_FRONT / "vendor").iterdir()):
            print("  X 漂移: server/frontend/vendor 空目录未清理（petite-vue 已退役）")
            ok = False
        if (SERVER_FRONT / "js" / "pc_nav.js").exists():
            print("  X 漂移: server/frontend 旧 pc_nav.js 未清理（已由 pc_shell.js 取代）")
            ok = False
        if (ANDROID_ASSETS / "js" / "pc_nav.js").exists():
            print("  X 漂移: android assets 旧 pc_nav.js 未清理（已由 pc_shell.js 取代）")
            ok = False
        if (ANDROID_ASSETS / "app.js").exists():
            print("  X 漂移: android assets 旧 app.js 未清理")
            ok = False
        for name in aa_static:
            ok &= _check_pair(name, STATIC / name, ANDROID_ASSETS / name)
        ok &= _check_dir("android/js", STATIC / "js", ANDROID_ASSETS / "js")
        ok &= _check_dir("android/vendor", STATIC / "vendor", ANDROID_ASSETS / "vendor")
        ok &= _check_pair("config.js", SERVER_FRONT / "config.js", ANDROID_ASSETS / "config.js")
        ok &= _check_pair("login.html", SERVER_FRONT / "login.html", ANDROID_ASSETS / "login.html")
        for name in ASSET_FILES:
            ok &= _check_pair(name, ROOT / "app" / "assets" / name, ANDROID_ASSETS / "assets" / name)
        print("结果:", "一致" if ok else "存在漂移（运行 python tools/sync_frontends.py 同步）")
        return 0 if ok else 1

    for name in sf_targets:
        _copy(STATIC / name, SERVER_FRONT / name)
        print(f"  -> server/frontend/{name}")
    for sub in ("js", "vendor"):
        src_dir = STATIC / sub
        if src_dir.is_dir():
            for fp in src_dir.rglob("*"):
                if fp.is_file():
                    _copy(fp, SERVER_FRONT / sub / fp.relative_to(src_dir))
            print(f"  -> server/frontend/{sub}/（{sum(1 for _ in src_dir.rglob('*') if _.is_file())} 文件）")
    _remove_stale(SERVER_FRONT, ("app.js", "js/pc_nav.js"))

    _copy_static(ANDROID_ASSETS, exclude=("config.js",))
    _remove_stale(ANDROID_ASSETS, ("app.js", "js/pc_nav.js"))
    print("  -> android assets（app/static 全量 + js/ + vendor/，config.js 除外）")
    _copy_assets(ANDROID_ASSETS / "assets")
    print("  -> android assets/assets（背景/字体/图标 9 件）")
    _copy(SERVER_FRONT / "config.js", ANDROID_ASSETS / "config.js")
    _copy(SERVER_FRONT / "login.html", ANDROID_ASSETS / "login.html")
    print("  -> android assets/config.js + login.html（服务器地址单点 + 登录页）")
    print("同步完成 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(sync("--check" in sys.argv))
