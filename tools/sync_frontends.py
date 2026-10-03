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
import re
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
# 由 server/frontend 进安卓 assets 的文件（**单一来源**）：新增文件必须同时登记
# android/app/build.gradle.kts 的 Sync 清单，否则构建时会被 Sync 静默删除（错误总结 #9）。
ANDROID_FROM_SERVER = ("config.js", "login.html", "bg-sunset.css")
# **服务器专用**、明确不进安卓包的页面（必须显式登记，否则下面的分类断言会报未分类）
SERVER_ONLY = ("admin.html",)

# ── 角色卡平台：server/frontend 的**唯一**内容集（2026-10-01 架构纠正）─────────────
# 用户原话：「不能直接在服务器使用 app 功能，服务器仅作为后台…不是让你搬一个 app 到服务器里」。
# 历史上本脚本把 `app/static` **整份镜像**进 server/frontend（index.html + App bundle +
# 全部 App 面板 + App 样式）⇒ 服务器公开提供了整个 App。现在服务器侧**只准有平台**：
#   · 平台文件直接在 server/frontend 里维护（不是从 app/static 拷过来的）；
#   · 本脚本对 server 侧只做**白名单自检**（成员缺失即红），不再有任何 STATIC → SERVER_FRONT 的拷贝；
#   · `app/static` → 安卓 assets 的同步**保持不变**（App 本地模式不受影响，见下方 aa_* 段）。
SERVER_PLATFORM = ("platform.html", "platform.bundle.js", "platform.css", "platform.js")
SERVER_PLATFORM_DEP = ("login.html",)      # 登录页（登录成功后跳 platform.html）
# 旧镜像残留：新架构下服务器不该有这些（服务端闸门已挡 302/404），**部署阶段**由 Lead 移除。
# 这里只**告警**不判红 —— 否则在搬迁完成前 `--check` 会一直红，掩盖真正的漂移。
SERVER_LEGACY_APP = ("index.html", "style.css", "pc.css", "js", "vendor")


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


def _gradle_sync_includes() -> set:
    """解析 android/app/build.gradle.kts 里 Sync 任务对 server/frontend 的 include 清单。

    存在的意义：Gradle 的 Sync 会**删除**目标目录中不属于它的文件 ⇒ 只改 Python 侧清单
    而忘了登记 Gradle 清单，构建时文件会被静默删掉（错误总结 #9，2026-10-01 复发）。
    """
    fp = ROOT / "android" / "app" / "build.gradle.kts"
    if not fp.is_file():
        return set()
    text = fp.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'from\(\s*"\.\./\.\./server/frontend"\s*\)\s*\{([^}]*)\}', text)
    if not m:
        return set()
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _check_frontend_classified() -> bool:
    """server/frontend/ 下的**每个顶层文件**都必须被显式分类。

    存在意义（verifier 2026-10-01 用变异指出）：只断言「手工清单 ⊆ Gradle 清单」是**单向**的，
    往 server/frontend/ 扔一个谁都没登记的新文件时门禁照样全绿 —— 而那正是 #9 的复发形状
    （新文件未登记 ⇒ 构建时被 Gradle Sync 静默删除，或被永远遗忘不进包）。
    与既有 ASSET_FILES / ASSET_NOT_SHIPPED 的分类做法同款：未分类 = 红。
    """
    known = set(ANDROID_FROM_SERVER) | set(SERVER_ONLY)
    # 由 app/static 同步过来、三份一致的那几个（sf_targets 的镜像）——**只对安卓侧**成立；
    # server/frontend 侧不再镜像 App（见 SERVER_PLATFORM 段），下面把旧残留一并登记为
    # "已知但待移除"，免得分类断言在搬迁期误报。
    known |= {"index.html", "style.css", "pc.css"}
    known |= set(SERVER_PLATFORM) | set(SERVER_PLATFORM_DEP)
    known |= set(SERVER_LEGACY_APP)
    unclassified = sorted(fp.name for fp in SERVER_FRONT.iterdir()
                          if fp.is_file() and fp.name not in known)
    if unclassified:
        print(f"  X 漂移: server/frontend/ 下有未分类文件: {unclassified}")
        print("      （平台文件请登记进 SERVER_PLATFORM/SERVER_PLATFORM_DEP；"
              "进安卓包请登记进 ANDROID_FROM_SERVER）")
        return False
    return True


def _check_server_platform() -> bool:
    """server/frontend 必须包含平台白名单的**全部**成员（缺失即红）。

    这是"服务器上只有平台"的正向判据：platform.html 缺了就是平台页 404，
    platform.bundle.js 缺了就是白屏（服务端只会对白名单内缺失文件回 404，不会回退 App）。"""
    need = list(SERVER_PLATFORM) + list(SERVER_PLATFORM_DEP) + list(ANDROID_FROM_SERVER)
    missing = [n for n in need if not (SERVER_FRONT / n).is_file()]
    if missing:
        print(f"  X 漂移: server/frontend 缺少平台/依赖文件: {missing}")
        print("      （平台 bundle 由 `python tools/build_frontend_bundle.py --platform` 生成；"
              "服务端白名单只认 platform.* / login.html / admin.html / config.js / bg-sunset.css）")
        return False
    return True


def _check_server_legacy() -> bool:
    """server/frontend 里**不得**再有 App 旧镜像（2026-10-01 部署后由"提醒"改判红）。

    架构约束（CLAUDE.md / 05 §七）：**服务器只做后台**，不提供 App 前端。
    这些文件曾把整个 App 搬上服务器；部署阶段已搬走（静态根之外），
    此后本地部署源若再出现它们，就是真漂移 ⇒ 必须红，而不是打印一条提醒。
    """
    left = [n for n in SERVER_LEGACY_APP if (SERVER_FRONT / n).exists()]
    if left:
        print(f"  X 漂移: server/frontend 仍有 App 旧镜像残留 {left}")
        print("      服务器只做后台（无 App 壳/样式/面板）；这些文件不该出现在部署源里。")
        return False
    return True


def _check_no_bom() -> bool:
    """代码/构建文件不得带 UTF-8 BOM（错误总结 #17/#18：PowerShell 写文件会带 BOM）。"""
    bad = []
    for fp in sorted((ROOT / "android").rglob("*.kts")) + sorted((ROOT / "tools").rglob("*.py")):
        if "build" in fp.parts:      # 构建产物目录跳过
            continue
        try:
            if fp.read_bytes().startswith(b"\xef\xbb\xbf"):
                bad.append(fp.relative_to(ROOT).as_posix())
        except OSError:
            pass
    if bad:
        print(f"  X 漂移: 以下文件带 UTF-8 BOM（会让首行标识符失效）: {bad[:5]}")
        return False
    return True


def _check_gradle_sync_list() -> bool:
    """断言「手工清单 ⊆ Gradle Sync 清单」，并反向确认登记的文件真实存在。"""
    ok = True
    got = _gradle_sync_includes()
    if not got:
        print("  X 漂移: 解析不出 android/app/build.gradle.kts 的 Sync 清单（工具或清单结构变了）")
        return False
    missing = sorted(set(ANDROID_FROM_SERVER) - got)
    if missing:
        print(f"  X 漂移: 未登记进 Gradle Sync 清单: {missing}")
        print("      （Gradle Sync 会删除目标目录里不属于它的文件 ⇒ 构建时这些文件会静默消失）")
        ok = False
    for n in sorted(got):
        if not (SERVER_FRONT / n).is_file():
            print(f"  X 漂移: Gradle Sync 清单登记了 server/frontend 里不存在的文件: {n}")
            ok = False
    return ok


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

    # 0b) ★ 平台 bundle：与 App bundle **分开生成**（平台只含 plaza 域 + 三件基础模块 +
    #     敏感头加密 + 平台入口；App 壳一律不进平台）。放在这里是为了让"改平台源码后忘了
    #     重出产物"和"改 App 源码后忘了重出"一样被门禁抓住。
    pcmd = [sys.executable, str(ROOT / "tools" / "build_frontend_bundle.py"), "--platform"]
    rp = subprocess.run(pcmd + (["--check"] if check_only else []), capture_output=True,
                        encoding="utf-8", errors="replace")
    if rp.returncode != 0:
        print("X 平台 bundle 生成/校验失败：\n" + (rp.stdout + rp.stderr)[:800])
        return 1
    if check_only and "漂移" in rp.stdout:
        print(rp.stdout.strip())
        return 1

    # 0c) 资产清单断言：app/assets 根目录文件必须已被显式分类（进 APK / 不进 APK）
    if not _check_asset_set():
        return 1

    # 1) **server/frontend：只做平台白名单自检，不再镜像 app/static**
    #    （2026-10-01 架构纠正——历史上正是这里的镜像把整个 App 搬上了服务器）
    # 2) 安卓 assets：app/static 全量 + assets 子集 + server 版 config.js/login.html
    # 注：模式封面/角色头像已入预设包（assets/character/{包}/assets/），经 syncBackend 随 app/ 进 APK
    aa_static = ("index.html", "style.css", "pc.css",
                 "开拓者_穹.png", "开拓者_星.png")

    if check_only:
        ok = True
        ok &= _check_server_platform()
        ok &= _check_server_legacy()
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
        for name in ANDROID_FROM_SERVER:
            ok &= _check_pair(name, SERVER_FRONT / name, ANDROID_ASSETS / name)
        # ★ task-21：手工清单必须 ⊆ Gradle Sync 清单（否则构建时被 Sync 静默删除）
        ok &= _check_gradle_sync_list()
        # ★ task-21 复查补强：分类断言（防"新文件谁都没登记却全绿"的假阴性）+ BOM 断言
        ok &= _check_frontend_classified()
        ok &= _check_no_bom()
        for name in ASSET_FILES:
            ok &= _check_pair(name, ROOT / "app" / "assets" / name, ANDROID_ASSETS / "assets" / name)
        print("结果:", "一致" if ok else "存在漂移（运行 python tools/sync_frontends.py 同步）")
        return 0 if ok else 1

    # 写模式：server 侧**没有任何拷贝**（平台文件是手写在该目录里的），只自检两项：
    # ① 平台白名单成员必须都在；② 不得再有 App 旧镜像（2026-10-01 部署后由"提醒"改判红）。
    # ⚠ 2026-10-01：`_warn_server_legacy` 改名 `_check_server_legacy`（并改成判红）时漏了这个调用点，
    #   写模式会 NameError —— 这里按新语义接上（写模式同样拦，不只是 --check）。
    if not _check_server_platform():
        return 1
    if not _check_server_legacy():
        return 1
    print("  -> server/frontend：平台白名单已就位（不再镜像 app/static）")

    _copy_static(ANDROID_ASSETS, exclude=("config.js",))
    _remove_stale(ANDROID_ASSETS, ("app.js", "js/pc_nav.js"))
    print("  -> android assets（app/static 全量 + js/ + vendor/，config.js 除外）")
    _copy_assets(ANDROID_ASSETS / "assets")
    print("  -> android assets/assets（背景/字体/图标 9 件）")
    for name in ANDROID_FROM_SERVER:
        _copy(SERVER_FRONT / name, ANDROID_ASSETS / name)
    print("  -> android assets/" + " + ".join(ANDROID_FROM_SERVER) + "（服务器地址单点 + 登录页 + 晚霞背景）")
    if not _check_gradle_sync_list():
        print("  X 同步后校验 Gradle Sync 清单失败（上面有原因）")
        return 1
    print("同步完成 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(sync("--check" in sys.argv))
