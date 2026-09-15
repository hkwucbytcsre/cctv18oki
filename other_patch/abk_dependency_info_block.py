#!/usr/bin/env python3
import os
import re
import sys
from pathlib import Path


MARKER = "ABK_MANAGER_DEPENDENCY_INFO_BLOCK_FOR_ABK_PKG"


def build_helper(abk_package: str) -> str:
    return f"""/* {MARKER}:
 * ReSukiSU 严格签名块检查会拒绝 ABK Manager APK 里的
 * DEPENDENCY_INFO_BLOCK (0x504b4453)。这里只为 ABK 包名
 * 提供对该块的识别能力，其他未知块仍然严格拒绝，
 * CAN-2026-2035133 防护不受影响。
 *
 * 放行条件：
 *   1. 块 ID 必须是 0x504b4453 (DEPENDENCY_INFO_BLOCK)；
 *   2. 路径位于 Android 11+ 标准安装目录 /data/app/~~...==/ 下；
 *   3. 路径以 /base.apk 结尾；
 *   4. get_pkg_from_apk_path() 能成功提取包名；
 *   5. 提取出的包名精确等于 {abk_package}。
 *
 * 不复用 is_manager_apk() 中 KSU_MANAGER_PACKAGE 的判定结果，
 * 独立做精确包名匹配。
 *
 * 注意：Android 11+ 的随机后缀使用 base64url 字母表
 * [A-Za-z0-9_-]，其中可能包含 '-'，因此这里不检查
 * 倒数第二个目录内 '-' 的数量，避免误拒绝合法路径。
 */
#define ABK_DEPENDENCY_INFO_BLOCK_ID 0x504b4453u

int get_pkg_from_apk_path(char *pkg, const char *path);

static __always_inline bool abk_path_is_standard_apk(const char *path)
{{
    size_t path_len;

    if (!path)
        return false;

    /* Android 11+ 标准安装路径以 /data/app/~~ 开头 */
    if (strncmp(path, "/data/app/~~", sizeof("/data/app/~~") - 1) != 0)
        return false;

    /* 必须以 /base.apk 结尾 */
    path_len = strlen(path);
    if (path_len < sizeof("/base.apk") - 1)
        return false;
    if (strcmp(path + path_len - (sizeof("/base.apk") - 1), "/base.apk") != 0)
        return false;

    return true;
}}

static __always_inline bool abk_is_manager_path(const char *path)
{{
    char pkg[KSU_MAX_PACKAGE_NAME];

    if (!path)
        return false;
    if (!abk_path_is_standard_apk(path))
        return false;
    if (get_pkg_from_apk_path(pkg, path) < 0)
        return false;
    return strncmp(pkg, ABK_MANAGER_PACKAGE, sizeof(ABK_MANAGER_PACKAGE)) == 0;
}}

static __always_inline bool abk_is_abk_dependency_block(u32 id, const char *path)
{{
    if (id != ABK_DEPENDENCY_INFO_BLOCK_ID)
        return false;
    return abk_is_manager_path(path);
}}
"""


STRICT_RE = re.compile(
    r"(?P<indent>[ \t]*)\}\s*else\s+if\s*\(\s*id\s*!=\s*0x42726577u\s*\)\s*\{[^\n]*\n"
    r"(?P<body>(?:.*?\n)*?)"
    r"(?P=indent)[ \t]*goto\s+invalid;\s*\n"
    r"(?P=indent)\}",
    re.M,
)


def find_ksu_dirs(root: Path):
    dirs = []
    seen = set()
    preferred = [
        root / "common/drivers/kernelsu",
        root / "drivers/kernelsu",
        root / "KernelSU/kernel",
        root / "kernel",
    ]
    for candidate in preferred:
        if (candidate / "Kbuild").exists() and (candidate / "manager/apk_sign.c").exists():
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                dirs.append(candidate)

    for dispatch in root.rglob("supercall/dispatch.c"):
        candidate = dispatch.parent.parent
        if (candidate / "Kbuild").exists() and (candidate / "manager/apk_sign.c").exists():
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                dirs.append(candidate)
    return dirs


def patch_file(path: Path, helper: str) -> bool:
    text = path.read_text(errors="ignore")
    if MARKER in text:
        print(f"ABK DEPENDENCY_INFO_BLOCK patch already present: {path}")
        return False

    if not STRICT_RE.search(text):
        print(f"ABK DEPENDENCY_INFO_BLOCK: strict block pattern not found, skip {path}")
        return False

    anchor = re.search(
        r"(?m)^static\s+(?:__always_inline\s+)?bool\s+check_v2_signature\s*\(",
        text,
    )
    if not anchor:
        print(f"ABK DEPENDENCY_INFO_BLOCK: check_v2_signature anchor not found, skip {path}")
        return False

    text = text[:anchor.start()] + helper + "\n" + text[anchor.start():]

    def repl(m):
        indent = m.group("indent")
        body = m.group("body")
        return (
            f"{indent}}} else if (id == ABK_DEPENDENCY_INFO_BLOCK_ID) {{ // ABK DEPENDENCY_INFO_BLOCK\n"
            f"{indent}    if (!abk_is_manager_path(path))\n"
            f"{indent}        goto invalid;\n"
            f"{indent}}} else if (id != 0x42726577u) {{ // APK verity padding\n"
            f"{body}"
            f"{indent}    goto invalid;\n"
            f"{indent}}}"
        )

    text, count = STRICT_RE.subn(repl, text, count=1)
    if not count:
        print(f"ABK DEPENDENCY_INFO_BLOCK: replacement failed, skip {path}")
        return False

    path.write_text(text)
    print(f"ABK DEPENDENCY_INFO_BLOCK patched: {path}")
    return True


def main() -> int:
    kernel_root = os.environ.get("KERNEL_ROOT")
    if not kernel_root:
        print("ABK DEPENDENCY_INFO_BLOCK: KERNEL_ROOT is required", file=sys.stderr)
        return 1

    root = Path(kernel_root).resolve()
    if not root.exists():
        print(f"ABK DEPENDENCY_INFO_BLOCK: KERNEL_ROOT does not exist: {root}", file=sys.stderr)
        return 1

    abk_package = os.environ.get("ABK_MANAGER_PACKAGE", "com.abk.kernel")
    helper = build_helper(abk_package)

    ksu_dirs = find_ksu_dirs(root)
    if not ksu_dirs:
        print("ABK DEPENDENCY_INFO_BLOCK: no KernelSU source dir found, skip")
        return 0

    changed = False
    for ksu_dir in ksu_dirs:
        apk_sign = ksu_dir / "manager/apk_sign.c"
        if apk_sign.exists():
            changed = patch_file(apk_sign, helper) or changed

    print("ABK DEPENDENCY_INFO_BLOCK: done" + ("" if changed else " (no changes)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
