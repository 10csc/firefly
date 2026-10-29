# -*- coding: utf-8 -*-
"""版本一致性 / 发布语义门禁（J3 + M2 第一件，2026-10-27 修订）。

## 三档 profile（默认 dev，向后兼容旧调用）

  dev      开发期：本地版本源必须一致；update.json/version.json 若存在则校验投影与自洽；
           已发布版本可以落后于开发版本；不核对大产物哈希（除非 --artifacts）。
  release  发版前：本地版本源齐全且一致；候选版本必须有不可变台账且本地产物与台账一致；
           已发布版本必须有台账；候选 > 已发布时 versionCode 必须递增；候选 == 已发布时
           视为重复发版，产物必须与已发布台账逐字节一致（否则 FAIL——差异只能升版本）。
  deploy   发布后/部署前：在 release 基础上要求 server/update.json 的 tag 已切到候选版本、
           资产 sha256/size 与台账一致、version.json 投影一致；本地产物仍须与台账一致。

## 与旧版的关系

  - 旧版只比较 7 处版本字面量；新版改为"5 处本地源 + update.json 投影 + 台账"。
  - `server/frontend/js/update.js` 不再参与校验：按 P6-12 它**应永久缺失**（sync_frontends 有反向检查）。
  - 缺文件在 dev 下仍是 SKIP；release/deploy 下除显式豁免外一律 FAIL（P6-14 的教训：空过=假绿）。
  - 唯一新增的强校验是 **release_manifests/ 台账 + 产物哈希**（P6-1/P6-2/同版本不同字节）。

用法：
    python tools/check_version.py                     # dev（默认，等价旧行为 + 服务端投影校验）
    python tools/check_version.py --profile release   # 发版前
    python tools/check_version.py --profile deploy    # 发布后/部署前
    python tools/check_version.py --json              # 机器可读（供 M2 协调器/任务卡消费）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TOOLS))
import version_truth as vt  # noqa: E402

ROOT = _TOOLS.parent

# versionCode 历史兜底：>= 0.8.0 时代；发版时只要求严格递增（下一次发版可改为 901）。
PREV_VERSION_CODE = 805

# (名称, 文件, 正则, 是否 release/deploy 必检)
SOURCE_SPECS = [
    ("core.config.APP_VERSION", "app/core/config.py", r'APP_VERSION\s*=\s*"([^"]+)"', True),
    ("update.js CURRENT_VERSION", "app/static/js/update.js", r'CURRENT_VERSION\s*=\s*"([^"]+)"', True),
    ("android versionName", "android/app/build.gradle.kts", r'versionName\s*=\s*"([^"]+)"', True),
    ("android versionCode", "android/app/build.gradle.kts", r'versionCode\s*=\s*(\d+)', True),
    ("iss AppVersion", "package/firefly.iss", r'AppVersion=(\d+\.\d+\.\d+)', True),
]


class Result:
    """一次检查的结论。保持纯数据，便于 --json 和测试直接断言。"""

    def __init__(self, profile: str, verbose: bool = True):
        self.profile = profile
        self.verbose = verbose
        self.checks: list[dict] = []
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.skips: list[str] = []

    def _say(self, mark: str, msg: str) -> None:
        if self.verbose:
            print(f"  {mark} {msg}")

    def section(self, title: str) -> None:
        if self.verbose:
            print(f"\n=== {title} ===")

    def ok(self, section: str, msg: str) -> None:
        self.checks.append({"level": "ok", "section": section, "msg": msg})
        self._say("✓", msg)

    def fail(self, section: str, msg: str) -> None:
        self.checks.append({"level": "fail", "section": section, "msg": msg})
        self.failures.append(msg)
        self._say("X", msg)

    def warn(self, section: str, msg: str) -> None:
        self.checks.append({"level": "warn", "section": section, "msg": msg})
        self.warnings.append(msg)
        self._say("!", msg)

    def skip(self, section: str, msg: str) -> None:
        self.checks.append({"level": "skip", "section": section, "msg": msg})
        self.skips.append(msg)
        self._say("-", msg)

    @property
    def ok_all(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "ok": self.ok_all,
            "checked": sum(1 for c in self.checks if c["level"] == "ok"),
            "failed": len(self.failures),
            "warned": len(self.warnings),
            "skipped": len(self.skips),
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "skips": list(self.skips),
            "checks": list(self.checks),
        }


def _read_source(root: Path, rel: str, pattern: str):
    fp = root / rel
    if not fp.is_file():
        return None, "missing"
    try:
        text = fp.read_text(encoding="utf-8")
    except Exception as e:
        return None, f"读取失败: {type(e).__name__}: {e}"
    m = re.search(pattern, text)
    if not m:
        return None, "pattern 未命中"
    return m.group(1), ""


def _add_issues(r: Result, section: str, fails, warns) -> None:
    for m in fails:
        r.fail(section, m)
    for m in warns:
        r.warn(section, m)


def _assets_match_ledger(sanitized: dict, ledger: dict) -> tuple[list[str], list[str]]:
    """update.json 的资产身份必须与已发布台账一致。"""
    fails, warns = [], []
    arts = vt.ledger_artifacts(ledger)
    for kind, name in vt.ASSET_NAMES.items():
        a = (sanitized.get("assets") or {}).get(kind) or {}
        e = arts.get(name) or {}
        if not a:
            fails.append(f"update.json 缺 {kind} 资产")
            continue
        if str(a.get("sha256") or "").lower() != str(e.get("sha256") or "").lower():
            fails.append(f"update.json {kind}.sha256 与已发布台账不一致")
        try:
            if int(a.get("size") or 0) != int(e.get("size") or 0):
                fails.append(f"update.json {kind}.size 与已发布台账不一致")
        except (TypeError, ValueError):
            fails.append(f"update.json {kind}.size 非法")
    return fails, warns


def check(root: Path, profile: str = "dev", artifacts: bool = False,
          manifest: str | None = None, apk: str | None = None,
          exe: str | None = None, verbose: bool = True) -> Result:
    if profile not in vt.PROFILES:
        raise ValueError(f"未知 profile: {profile}")
    strict = profile in ("release", "deploy")
    r = Result(profile, verbose=verbose)
    paths = {}
    if apk:
        paths["apk"] = apk
    if exe:
        paths["exe"] = exe

    # ── 1. 本地版本源 ─────────────────────────────────────────
    r.section("1. 本地版本源（candidate）")
    parsed: dict[str, str] = {}
    version_code = None
    for name, rel, pattern, required in SOURCE_SPECS:
        val, err = _read_source(root, rel, pattern)
        if err:
            msg = f"{name}: {rel} {err}"
            if strict and required:
                r.fail("local", msg)
            else:
                r.skip("local", msg + "（dev 跳过；release/deploy 必检）")
            continue
        if name == "android versionCode":
            version_code = val
            r.ok("local", f"{name}: {val}")
        else:
            parsed[name] = val
            r.ok("local", f"{name}: {val}")

    if not parsed:
        r.fail("local", "没有读到任何本地版本号（检出异常）")
    else:
        vals = set(parsed.values())
        if len(vals) != 1:
            r.fail("local", f"本地版本号不一致: {dict(sorted(parsed.items()))}")
        else:
            v = next(iter(vals))
            r.ok("local", f"本地版本一致: {v}")
            if not re.fullmatch(r"\d+\.\d+\.\d+", v):
                r.fail("local", f"版本格式非法（必须 x.y.z 纯数字）: {v!r}")

    candidate = next(iter(set(parsed.values()))) if len(set(parsed.values())) == 1 else ""

    if version_code is not None:
        try:
            vc = int(version_code)
            if vc <= PREV_VERSION_CODE:
                r.fail("local", f"versionCode={vc} 未大于 PREV_VERSION_CODE={PREV_VERSION_CODE}")
            else:
                r.ok("local", f"versionCode={vc} > PREV_VERSION_CODE={PREV_VERSION_CODE}")
        except ValueError:
            r.fail("local", f"versionCode 解析失败: {version_code!r}")

    # ── 2. 已发布真相（server/update.json + version.json 投影）────
    r.section("2. 已发布真相（published）")
    pub = vt.load_published_manifest(root)
    pub_tag = ""
    if pub["err"] == "missing":
        if strict:
            r.fail("published", "server/update.json 缺失（release/deploy 必须有已发布清单）")
        else:
            r.skip("published", "server/update.json 缺失（干净 clone / 仅客户端检出，dev 跳过）")
    elif pub["err"]:
        r.fail("published", f"server/update.json 不可用: {pub['err']}")
    else:
        raw, san, mod = pub["raw"], pub["sanitized"], pub["module"]
        fails, warns = vt.validate_update_manifest(raw, san, mod, profile)
        _add_issues(r, "published", fails, warns)
        pub_tag = (san or {}).get("tag") or ""
        if pub_tag:
            r.ok("published", f"update.json tag（唯一真相源）: {pub_tag}")
        if not pub_tag:
            r.fail("published", "update.json tag 为空/非法")

        vj, verr = vt.load_version_json(root)
        projected = vt.project_version_json(san or {}, mod)
        if verr == "missing":
            if strict:
                r.fail("published", "server/version.json 缺失（兼容投影必须存在）")
            else:
                r.skip("published", "server/version.json 缺失（dev 跳过）")
        elif verr:
            r.fail("published", f"server/version.json 不可用: {verr}")
        else:
            diffs = vt.projection_diff(projected, vj)
            if diffs:
                r.fail("published", "server/version.json 不是 update.json 的投影: " + "; ".join(diffs))
            else:
                r.ok("published", "server/version.json 与 update.json 投影一致")
            if isinstance(vj, dict):
                extra = set(vj) - {"tag", "exe", "apk"}
                if extra:
                    r.warn("published", f"server/version.json 有投影外字段: {sorted(extra)}")

        if pub_tag and candidate:
            if vt.compare(pub_tag, candidate) > 0:
                r.fail("published", f"已发布版本 {pub_tag} 高于本地版本 {candidate}（代码落后于线上）")

    # ── 3. 不可变台账（release_manifests/）──────────────────────
    r.section("3. 不可变产物台账（release_manifests/）")
    cand_ledger = None
    pub_ledger = None
    if manifest:
        mp = Path(manifest)
        if not mp.is_absolute():
            mp = root / mp
        data, err = vt.load_json(mp)
        cand_ledger = {"path": mp, "data": data, "legacy": False, "err": err}
    elif candidate:
        cand_ledger = vt.find_ledger(root, candidate, allow_legacy=not strict)
    else:
        cand_ledger = {"path": None, "data": None, "legacy": False, "err": "missing"}

    if pub_tag:
        pub_ledger = vt.find_ledger(root, pub_tag, allow_legacy=not strict)

    def _report_ledger(led: dict, label: str, ver: str) -> dict | None:
        if led["err"] == "missing":
            if strict:
                legacy = vt.legacy_ledger_path(root, ver) if ver else None
                if legacy is not None and legacy.is_file():
                    r.fail("ledger", f"{label}台账只在旧位置 _harden/（必须迁移到 release_manifests/ 才算权威）")
                else:
                    r.fail("ledger", f"{label}缺不可变台账（release_manifests/release_manifest_x.y.z.json）")
            else:
                r.skip("ledger", f"{label}缺台账（dev 不阻塞；发版前必须补）")
            return None
        if led["err"]:
            r.fail("ledger", f"{label}台账不可读: {led['err']}")
            return None
        if led["legacy"]:
            if strict:
                r.fail("ledger", f"{label}台账仍在 _harden/（必须迁移到 release_manifests/ 才算权威）")
            else:
                r.warn("ledger", f"{label}台账在旧位置 _harden/（建议迁移到 release_manifests/）")
        fails = vt.validate_ledger(led["data"])
        _add_issues(r, "ledger", fails, [])
        if fails:
            return None
        r.ok("ledger", f"{label}台账: {led['path'].relative_to(root) if str(led['path']).startswith(str(root)) else led['path']}")
        return led["data"]

    cand_data = _report_ledger(cand_ledger, "候选版本", candidate) if cand_ledger else None
    pub_data = _report_ledger(pub_ledger, "已发布版本", pub_tag) if pub_ledger else None

    if cand_data and candidate:
        lv = vt.normalize_tag(cand_data.get("version"))
        if lv != candidate:
            r.fail("ledger", f"候选台账 version={lv} 与本地 {candidate} 不一致")
        if version_code is not None:
            try:
                if int(cand_data.get("version_code")) != int(version_code):
                    r.fail("ledger", f"候选台账 version_code={cand_data.get('version_code')} 与本地 {version_code} 不一致")
            except (TypeError, ValueError):
                r.fail("ledger", "候选台账 version_code 非法")

    if pub_data and pub_tag:
        lv = vt.normalize_tag(pub_data.get("version"))
        if lv != pub_tag:
            r.fail("ledger", f"已发布台账 version={lv} 与 update.json {pub_tag} 不一致")
        if not strict:
            pass
        # 已发布 update.json 的资产身份必须与台账一致（release/deploy 一律校验）
        if isinstance(pub.get("sanitized"), dict):
            fails, _ = _assets_match_ledger(pub["sanitized"], pub_data)
            _add_issues(r, "ledger", fails, [])

    # ── 4. 产物身份 / profile 语义 ──────────────────────────────
    do_artifacts = strict or artifacts
    if do_artifacts and cand_data:
        fails, warns = vt.check_artifacts(root, cand_data, paths)
        if strict:
            _add_issues(r, "artifacts", fails, warns)
            if not fails:
                r.ok("artifacts", "本地产物与候选台账逐字节一致")
        else:
            # dev：本地重建差异允许存在，但不能当发布物；只告警，由 release profile 拦截。
            for m in fails:
                r.warn("artifacts", m + "（dev 允许重建差异；发布前必须与台账一致或升版本）")
            for m in warns:
                r.warn("artifacts", m)
            if not fails:
                r.ok("artifacts", "本地产物与台账一致")
    elif not do_artifacts:
        r.skip("artifacts", "dev 默认不核对大产物哈希（--artifacts 可开）")

    if strict and candidate and pub_tag:
        if profile == "release":
            if vt.compare(candidate, pub_tag) < 0:
                r.fail("profile", f"候选 {candidate} 低于已发布 {pub_tag}，禁止回退发布")
            elif vt.compare(candidate, pub_tag) == 0:
                r.ok("profile", "候选 == 已发布：按幂等重发校验（产物必须与已发布台账一致）")
            else:
                r.ok("profile", f"候选 {candidate} > 已发布 {pub_tag}（正常发版路径）")
                # versionCode 必须已发布台账更大
                if pub_data and version_code is not None:
                    try:
                        if int(version_code) <= int(pub_data.get("version_code")):
                            r.fail("profile", f"versionCode={version_code} 未大于已发布 {pub_tag} 的 {pub_data.get('version_code')}")
                    except (TypeError, ValueError):
                        r.fail("profile", "无法比较 versionCode 与已发布台账")
        elif profile == "deploy":
            if candidate != pub_tag:
                r.fail("profile", f"deploy 要求已发布 tag == 本地版本：update.json={pub_tag!r} 本地={candidate!r}")
            else:
                r.ok("profile", f"update.json 已切到 {candidate}")

    # ── 5. 汇总 ────────────────────────────────────────────────
    r.section("结果")
    if r.ok_all:
        r.ok("summary", f"PASS（profile={profile}，检查 {len(r.checks)} 项，跳过 {len(r.skips)} 项）")
    else:
        r.fail("summary", f"FAIL（profile={profile}，失败 {len(r.failures)} 项，警告 {len(r.warnings)} 项）")
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="版本一致性 / 发布语义门禁（dev/release/deploy）")
    ap.add_argument("--profile", choices=vt.PROFILES, default="dev",
                    help="dev=开发；release=发版前；deploy=发布后/部署前")
    ap.add_argument("--root", default=str(ROOT), help="仓库根（测试用）")
    ap.add_argument("--artifacts", action="store_true", help="dev 下也核对本地产物哈希")
    ap.add_argument("--manifest", default=None, help="候选台账路径（默认 release_manifests/）")
    ap.add_argument("--apk", default=None, help="APK 路径覆盖")
    ap.add_argument("--exe", default=None, help="EXE 路径覆盖")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（供机器消费）")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    if not root.is_dir():
        print(f"X root 不存在: {root}", file=sys.stderr)
        return 2
    r = check(root, profile=a.profile, artifacts=a.artifacts, manifest=a.manifest,
              apk=a.apk, exe=a.exe, verbose=not a.json)
    if a.json:
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
    return 0 if r.ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
