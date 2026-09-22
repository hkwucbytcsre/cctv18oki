#!/usr/bin/env python3
"""
动态修复 ABK_control_module 中的 abk_control_ksu_patch.py，
使其兼容 ReSukiSU 新版多管理器架构（commit f98b7a6 之后）。
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


def fix_file(py_path: Path) -> None:
    """修改 abk_control_ksu_patch.py 文件。"""
    content = py_path.read_text(encoding="utf-8")
    original = content

    # 1. 替换 patch_single_manager_identity 函数
    pattern = re.compile(
        r"def patch_single_manager_identity\(ksu_dir: Path\) -> None:.*?(?=\ndef |\Z)",
        re.S,
    )
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
    content, count1 = pattern.subn(lambda m: new_func, content, count=1)

    # 2. 替换 is_resukisu_tracker 函数
    pattern = re.compile(
        r"def is_resukisu_tracker\(ksu_dir: Path\) -> bool:.*?(?=\ndef |\Z)",
        re.S,
    )
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
    content, count2 = pattern.subn(lambda m: new_is_resu, content, count=1)

    # 3. 替换 patch_tracker 函数
    pattern = re.compile(
        r"def patch_tracker\(ksu_dir: Path\) -> None:.*?(?=\ndef |\Z)",
        re.S,
    )
    new_patch_tracker = '''def patch_tracker(ksu_dir: Path) -> None:
    path = ksu_dir / "manager/throne_tracker.c"
    if not path.exists():
        raise SystemExit(f"{path} is missing")
    if is_resukisu_tracker(ksu_dir):
        text = path.read_text(errors="ignore")
        if "void abk_try_register_manager(void)" in text:
            return
        block = """
#ifdef ABK_MANAGER_OFFICIAL_CERT
void abk_try_register_manager(void)
{
    struct file *fp;
    if (is_manager())
        return;
    fp = filp_open(SYSTEM_PACKAGES_LIST_PATH, O_RDONLY, 0);
    if (IS_ERR(fp))
        return;
    filp_close(fp, 0);
}
#endif
"""
        anchor = "\\nvoid __init ksu_throne_tracker_init()"
        if anchor not in text:
            anchor = "\\nvoid __init ksu_throne_tracker_init(void)"
        if anchor in text:
            text = text.replace(anchor, block + anchor, 1)
        else:
            text += block
        path.write_text(text)
        print(f"ABK Control: patched new multi-manager tracker in {path}")
    else:
        patch_single_manager_tracker(ksu_dir, path)
        patch_single_manager_allowlist(ksu_dir)
'''
    content, count3 = pattern.subn(lambda m: new_patch_tracker, content, count=1)

    if content != original:
        py_path.write_text(content, encoding="utf-8")
        print(f"Fixed: {py_path}")
    else:
        print(f"No change needed: {py_path}")

    # -------- 打印实际写入后的内容，方便核对 --------
    print()
    print("#### 修复情况汇总 ####")
    print(f"  patch_single_manager_identity 替换次数: {count1}")
    print(f"  is_resukisu_tracker 替换次数: {count2}")
    print(f"  patch_tracker 替换次数: {count3}")

    final_text = py_path.read_text(encoding="utf-8")

    def extract(name: str) -> str:
        pat = re.compile(
            rf"^def {name}\(.*?(?=^def |\Z)",
            re.S | re.M,
        )
        m = pat.search(final_text)
        return m.group(0) if m else f"<未找到 {name}>"

    _print_block("patch_single_manager_identity (写入后)", extract("patch_single_manager_identity"))
    _print_block("is_resukisu_tracker (写入后)", extract("is_resukisu_tracker"))
    _print_block("patch_tracker (写入后)", extract("patch_tracker"))


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