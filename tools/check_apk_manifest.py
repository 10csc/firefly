#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""APK 二进制清单（AXML）属性检查 —— 发版后验收用

用途：构建完 APK 后**不装到手机上**就能确认关键安全属性是否生效。
场景：2026-09-18 审计发现"用户下载到的 APK 是 8-23 的构建，allowBackup 仍是 true"，
      而当时没有任何手段快速核验包内属性——每次都靠人工装真机看，漏检了 3 周。

用法：
    python tools/check_apk_manifest.py android/firefly.apk allowBackup usesCleartextTraffic
    # 输出：每个属性的实际值

期望值（发版验收基线）：
    allowBackup            = false   （user_data 含 API Key，不允许被云备份带出设备）
    usesCleartextTraffic   = true    （服务器暂无 HTTPS，客户端必须允许明文，见审计报告 2.1）

实现：标准库解析 AXML（解析 string pool + start element 的 attribute 数组）。
      只读、无副作用；不依赖 aapt / apktool。
"""
import struct
import sys
import zipfile

RES_STRING_POOL_TYPE = 0x0001
RES_XML_START_ELEMENT_TYPE = 0x0102
TYPE_INT_BOOLEAN = 0x12
TYPE_INT_DEC = 0x10
TYPE_STRING = 0x03

# ResXMLTree_node 固定 16 字节；attrExt 里 attributeStart 相对 attrExt 起点（node+16）
_NODE_HEADER = 16
_ATTR_START_OFF = 24        # node + 24：uint16 attributeStart
_ATTR_COUNT_OFF = 28        # node + 28：uint16 attributeCount
_ATTR_SIZE = 20             # 每条 ResXMLTree_attribute 20 字节
_VALUE_TYPE_OFF = 15        # attr + 15：uint8 dataType
_VALUE_DATA_OFF = 16        # attr + 16：uint32 data


def _parse_string_pool(buf, off):
    """返回 (strings, next_offset)。"""
    typ, _hdr, size = struct.unpack_from("<HHI", buf, off)
    string_count = struct.unpack_from("<I", buf, off + 8)[0]
    flags = struct.unpack_from("<I", buf, off + 16)[0]
    strings_start = struct.unpack_from("<I", buf, off + 20)[0]
    utf8 = bool(flags & (1 << 8))
    offsets = struct.unpack_from("<%dI" % string_count, buf, off + 28)
    base = off + strings_start
    out = []
    for o in offsets:
        p = base + o
        if utf8:
            n = buf[p]
            p += 1
            if n & 0x80:
                n = ((n & 0x7F) << 8) | buf[p]
                p += 1
            m = buf[p]
            p += 1
            if m & 0x80:
                m = ((m & 0x7F) << 8) | buf[p]
                p += 1
            out.append(buf[p:p + m].decode("utf-8", "replace"))
        else:
            n = struct.unpack_from("<H", buf, p)[0]
            p += 2
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", buf, p)[0]
                p += 2
            out.append(buf[p:p + n * 2].decode("utf-16-le", "replace"))
    return out, off + size


def read_attrs(axml: bytes) -> dict:
    """返回 {属性名: 值字符串}（同名取最后一次出现）。"""
    strings = None
    pos = 8                       # 跳过文件头
    out = {}
    while pos < len(axml) - 8:
        typ, _hdr, size = struct.unpack_from("<HHI", axml, pos)
        if size <= 0:
            break
        if typ == RES_STRING_POOL_TYPE and strings is None:
            strings, _ = _parse_string_pool(axml, pos)
        elif typ == RES_XML_START_ELEMENT_TYPE:
            attr_start = struct.unpack_from("<H", axml, pos + _ATTR_START_OFF)[0]
            attr_count = struct.unpack_from("<H", axml, pos + _ATTR_COUNT_OFF)[0]
            abase = pos + _NODE_HEADER + attr_start
            for i in range(attr_count):
                ap = abase + i * _ATTR_SIZE
                if ap + _ATTR_SIZE > len(axml):
                    break
                name_idx = struct.unpack_from("<i", axml, ap + 4)[0]
                vtype = axml[ap + _VALUE_TYPE_OFF]
                vdata = struct.unpack_from("<I", axml, ap + _VALUE_DATA_OFF)[0]
                if strings is None or not (0 <= name_idx < len(strings)):
                    continue
                name = strings[name_idx]
                if vtype == TYPE_INT_BOOLEAN:
                    val = "true" if vdata else "false"
                elif vtype == TYPE_INT_DEC:
                    val = str(vdata)
                elif vtype == TYPE_STRING and 0 <= vdata < len(strings):
                    val = strings[vdata]
                else:
                    val = hex(vdata)
                out[name] = val
        pos += size
    return out


# 发版验收基线：属性名 -> (期望值, 说明)
EXPECTED = {
    "allowBackup": ("false", "user_data 含 API Key，禁止被云备份/adb backup 带出设备"),
    "usesCleartextTraffic": ("true", "服务器暂无 HTTPS，客户端需允许明文（见审计报告 2.1）"),
}


def main(argv: list) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 1
    apk = argv[1]
    wants = argv[2:]
    try:
        with zipfile.ZipFile(apk) as z:
            axml = z.read("AndroidManifest.xml")
    except (OSError, KeyError) as e:
        print(f"[错误] 读不到 {apk} 的 AndroidManifest.xml：{e}")
        return 2

    attrs = read_attrs(axml)
    print(f"清单属性（{apk}）：")
    rc = 0
    for w in wants:
        got = attrs.get(w)
        exp = EXPECTED.get(w)
        if got is None:
            print(f"  {w} = <清单里没有这个属性>")
            if exp:
                print(f"      期望 {exp[0]}（{exp[1]}）→ 不一致")
                rc = 1
        else:
            flag = ""
            if exp:
                flag = " ✔" if got == exp[0] else f" ✗（期望 {exp[0]}，因为{exp[1]}）"
                if got != exp[0]:
                    rc = 1
            print(f"  {w} = {got}{flag}")
    # 顺带提醒包内是否含已下线的旧前端结构（发版漏跑 sync_frontends 的典型症状）
    with zipfile.ZipFile(apk) as z:
        names = z.namelist()
    has_sync = any(n.endswith("assets/js/sync.js") for n in names)
    has_old = any(n.endswith("assets/js/panels.js") for n in names)
    print(f"  包内前端：sync.js={'有' if has_sync else '❌ 没有（旧构建！）'}"
          f"  panels.js(旧结构)={'有' if has_old else '无'}")
    if not has_sync:
        print("      → 构建前没跑 tools/sync_frontends.py，或包没重建")
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
