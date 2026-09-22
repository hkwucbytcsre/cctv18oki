#!/usr/bin/env python3
"""
动态修复 ABK_control_module 中的 abk_control_ksu_patch.py，
使其兼容 ReSukiSU 新版多管理器架构（commit f98b7a6 之后）。

新版下 ABK 只做两件事：
  1. 让 apk_sign.c 信任 ABK 管理器的包名和签名（签名信任）
  2. 在 manager_identity.h 中预留 abk_register_manager /
     abk_unregister_all_managers（供 ABK 内核模块使用）

不再注入 abk_try_register_manager 空壳：
  - KernelSU 新版 do_track_throne 无条件清空 + 重扫
  - track_throne 仍在，KernelSU 自己会在启动 / packages.list
    rename / pkg_observer 时触发
  - ABK 管理器只要签名被信任，就会被自动注册进官方列表
"""
import re
import sys
from pathlib import Path


def _print_block(title: str, body: str) -> None:
    print("=" * 78)
    print(f"[{title}]")
    print("-" * 78)
    print(body.rstrip("\n"))
    print("=" * 78)


def _sub_function(content: str, func_name: str, new_body: str) -> tuple[str, int]:
    """用正则把 abk_control_ksu_patch.py 中某个 def 整体替换掉。"""
    pattern = re.compile(
        rf"def {func_name}\(.*?(?=\ndef |\Z)",
        re.S,
    )
    return pattern.subn(lambda m: new_body, content, count=1)


def fix_file(py_path: Path) -> None:
    content = py_path.read_text(encoding="utf-8")
    original = content
    counts: dict[str, int] = {}

    # ============================================================
    # 1. patch_single_manager_identity
    #    新版：检测到多管理器接口后，只注入 ABK 辅助函数
    # ============================================================
    new_func = '''def patch_single_manager_identity(ksu_dir: Path) -> None:
    """新版多管理器：注入 ABK 专用辅助函数。"""
    path = ksu_dir / "manager/manager_identity.h"
    if not path.exists():
        raise SystemExit(f"{path} is missing")
    text = path.read_text(errors="ignore")
    if "ABK_MANAGER_MULTI_MANAGER_BRIDGE" in text:
        return

    if "ksu_register_manager(u32 uid, u8 signature_index)" in text and "ksu_unregister_all_manager" in text:
        insert = """
#ifdef ABK_MANAGER_OFFICIAL_CERT
#define ABK_MANAGER_MULTI_MANAGER_BRIDGE 1
static inline void abk_register_manager(u32 appid)
{
    ksu_register_manager(appid, KSU_SIGNATURE_INDEX_DYNAMIC_MANAGER);
}
static inline void abk_unregister_all_managers(void)
{
    ksu_unregister_all_manager();
}
#endif
"""
        marker = "\\n#endif\\n"
        if marker not in text:
            raise SystemExit(f"{path} missing final header guard")
        text = text.rsplit(marker, 1)[0] + insert + marker
        path.write_text(text)
        print(f"ABK Control: injected ABK multi-manager helper in {path}")
        return

    raise SystemExit(f"{path} unsupported manager_identity.h layout")
'''
    content, counts["patch_single_manager_identity"] = _sub_function(
        content, "patch_single_manager_identity", new_func
    )

    # ============================================================
    # 2. is_resukisu_tracker
    #    新版：只通过 manager_identity.h 判断
    # ============================================================
    new_is_resu = '''def is_resukisu_tracker(ksu_dir: Path) -> bool:
    """检测新版 ReSukiSU 多管理器。"""
    path = ksu_dir / "manager/throne_tracker.c"
    header_path = ksu_dir / "manager/throne_tracker.h"
    if not path.exists() or not header_path.exists():
        return False
    identity = (ksu_dir / "manager/manager_identity.h").read_text(errors="ignore")
    return (
        "ksu_register_manager(u32 uid, u8 signature_index)" in identity
        and "ksu_unregister_all_manager" in identity
    )
'''
    content, counts["is_resukisu_tracker"] = _sub_function(
        content, "is_resukisu_tracker", new_is_resu
    )

    # ============================================================
    # 3. patch_throne_header
    #    新版：不需要 abk_try_register_manager，直接跳过
    # ============================================================
    new_patch_throne_header = '''def patch_throne_header(ksu_dir: Path) -> None:
    if is_resukisu_tracker(ksu_dir):
        print("ABK Control: new multi-manager detected, skip throne_tracker.h patch")
        return
    path = ksu_dir / "manager/throne_tracker.h"
    if not path.exists():
        raise SystemExit(f"{path} is missing")
    text = path.read_text(errors="ignore")
    if "abk_try_register_manager" in text:
        return
    insert = """
#ifdef ABK_MANAGER_OFFICIAL_CERT
#ifdef CONFIG_KSU_DISABLE_MANAGER
static inline void abk_try_register_manager(void)
{
}
#else
void abk_try_register_manager(void);
#endif
#endif
"""
    marker = "\\n#endif\\n"
    if marker not in text:
        raise SystemExit(f"{path} missing final header guard")
    path.write_text(text.rsplit(marker, 1)[0] + insert + marker)
    print(f"ABK Control: declared manager registration hook in {path}")
'''
    content, counts["patch_throne_header"] = _sub_function(
        content, "patch_throne_header", new_patch_throne_header
    )

    # ============================================================
    # 4. patch_tracker
    #    新版：不再注入 abk_try_register_manager 空壳，直接跳过
    # ============================================================
    new_patch_tracker = '''def patch_tracker(ksu_dir: Path) -> None:
    path = ksu_dir / "manager/throne_tracker.c"
    if not path.exists():
        raise SystemExit(f"{path} is missing")
    if is_resukisu_tracker(ksu_dir):
        print("ABK Control: new multi-manager detected, skip throne_tracker.c patch")
        return
    patch_single_manager_tracker(ksu_dir, path)
    patch_single_manager_allowlist(ksu_dir)
'''
    content, counts["patch_tracker"] = _sub_function(
        content, "patch_tracker", new_patch_tracker
    )

    # ============================================================
    # 5. patch_dispatch_registration
    #    新版：不需要在 GET_INFO 里调用 abk_try_register_manager
    # ============================================================
    new_patch_dispatch = '''def patch_dispatch_registration(ksu_dir: Path) -> None:
    path = ksu_dir / "supercall/dispatch.c"
    if not path.exists():
        raise SystemExit(f"{path} is missing")
    if is_resukisu_tracker(ksu_dir):
        print("ABK Control: new multi-manager detected, skip dispatch GET_INFO hook")
        return
    text = path.read_text(errors="ignore")
    if '"manager/throne_tracker.h"' not in text:
        anchor = '#include "manager/manager_identity.h"\\n'
        if anchor not in text:
            raise SystemExit(f"{path} missing manager_identity include anchor")
        text = text.replace(anchor, anchor + '#include "manager/throne_tracker.h"\\n', 1)
    block = """#ifdef ABK_MANAGER_OFFICIAL_CERT
    if (!is_manager()) {
        abk_try_register_manager();
    }
#endif
"""
    if "abk_try_register_manager();" not in text:
        anchor = """    if (is_manager()) {
        cmd.flags |= KSU_GET_INFO_FLAG_MANAGER;
    }
"""
        if anchor not in text:
            raise SystemExit(f"{path} missing GET_INFO manager flag anchor")
        text = text.replace(anchor, block + anchor, 1)
    if write_if_changed(path, text):
        print(f"ABK Control: connected manager registration to GET_INFO in {path}")
'''
    content, counts["patch_dispatch_registration"] = _sub_function(
        content, "patch_dispatch_registration", new_patch_dispatch
    )

    # ============================================================
    # 6. validate_ksu_dir
    #    新版：不再要求 abk_try_register_manager 相关符号
    # ============================================================
    new_validate = '''def validate_ksu_dir(ksu_dir: Path, require_control_bridge: bool) -> None:
    required = {
        ksu_dir / "Kbuild": [
            "ABK_MANAGER_PACKAGE",
            "ABK_MANAGER_CERT_SHA256",
            "ABK_MANAGER_CERT_MAX_LENGTH",
            "ABK_MANAGER_OFFICIAL_CERT",
        ],
        ksu_dir / "manager/apk_sign.c": [
            "ABK_MANAGER_CERT_SHA256",
            "ABK_MANAGER_CERT_MAX_LENGTH",
        ],
    }

    if require_control_bridge:
        required.setdefault(ksu_dir / "supercall/dispatch.c", []).append(
            "ABK_CONTROL_IOCTL_GET_STATUS"
        )

    if is_resukisu_tracker(ksu_dir):
        required[ksu_dir / "manager/manager_identity.h"] = [
            "ABK_MANAGER_MULTI_MANAGER_BRIDGE",
            "abk_register_manager",
            "abk_unregister_all_managers",
        ]
    else:
        required[ksu_dir / "manager/throne_tracker.h"] = ["abk_try_register_manager"]
        required[ksu_dir / "manager/throne_tracker.c"] = [
            "abk_try_register_manager",
            "ABK_MANAGER_MULTI_MANAGER_BRIDGE",
            "abk_prune_missing_managers",
            "ksu_register_manager",
        ]
        required.setdefault(ksu_dir / "supercall/dispatch.c", []).extend(
            ["abk_try_register_manager", "KSU_GET_INFO_FLAG_MANAGER"]
        )
        required[ksu_dir / "manager/manager_identity.h"] = [
            "ABK_MANAGER_MULTI_MANAGER_BRIDGE",
            "ksu_register_manager",
            "ksu_has_manager",
        ]
        if (ksu_dir / "policy/allowlist.c").exists():
            required[ksu_dir / "policy/allowlist.c"] = ["is_uid_manager(uid)"]

    for path, needles in required.items():
        text = path.read_text(errors="ignore")
        missing = [needle for needle in needles if needle not in text]
        if missing:
            raise SystemExit(
                f"{path} missing ABK Control injection: {', '.join(missing)}"
            )

    if not is_resukisu_tracker(ksu_dir):
        tracker = (ksu_dir / "manager/throne_tracker.c").read_text(errors="ignore")
        forbidden = [
            "ABK Control: prefer ABK manager",
            "bool should_stop = get_pkg_from_apk_path",
            "ksu_invalidate_manager_uid();\\n    track_throne(false);",
        ]
        present = [marker for marker in forbidden if marker in tracker]
        if present:
            raise SystemExit(
                f"{ksu_dir / 'manager/throne_tracker.c'} contains legacy manager override: {', '.join(present)}"
            )
'''
    content, counts["validate_ksu_dir"] = _sub_function(
        content, "validate_ksu_dir", new_validate
    )

    # ============================================================
    # 写回
    # ============================================================
    if content != original:
        py_path.write_text(content, encoding="utf-8")
        print(f"Fixed: {py_path}")
    else:
        print(f"No change needed: {py_path}")

    # -------- 汇总 --------
    print()
    print("#### 修复情况汇总 ####")
    for name, cnt in counts.items():
        print(f"  {name} 替换次数: {cnt}")

    # -------- 打印实际写入后的关键函数 --------
    final_text = py_path.read_text(encoding="utf-8")

    def extract(name: str) -> str:
        pat = re.compile(rf"^def {name}\(.*?(?=^def |\Z)", re.S | re.M)
        m = pat.search(final_text)
        return m.group(0) if m else f"<未找到 {name}>"

    for name in (
        "patch_single_manager_identity",
        "is_resukisu_tracker",
        "patch_throne_header",
        "patch_tracker",
        "patch_dispatch_registration",
    ):
        _print_block(f"{name} (写入后)", extract(name))


def main():
    if len(sys.argv) < 2:
        print("Usage: fix_abk_ksu_patch.py <path-to-abk_control_ksu_patch.py>")
        sys.exit(1)
    py_path = Path(sys.argv[1])
    if not py_path.exists():
        print(f"File not found: {py_path}")
        sys.exit(1)
    fix_file(py_path)


if __name__ == "__main__":
    main()