#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
给第三方 KernelSU fork 补 priority uid filter 支持（fork 侧全部内容）。

用法：
    python3 patch-priority-uid-filter.py <KSU源码目录>

改动（两处，均已上游 10_enable_susfs_for_ksu.patch 的相关部分为准）：
    1. dispatch.c 的 ksu_handle_susfs_cmd() 里注册
       case CMD_SUSFS_SET_PRIORITY_UID_FILTER → susfs_set_priority_uid_filter()
       （对应补丁里 ksu_handle_sys_reboot 中的同名 case；你的 fork 用集中
       分发函数，所以插到这里。没有这处，0x60030 命令进来匹配不到 case，
       用户态看到"命令不支持"）
    2. pkg_observer.c 的 ksu_defer_track_throne() 里调用
       susfs_parse_priority_app_strings()
       （应用卸载重装换了 uid 后自动重解析名单；不改不影响基础功能，
       只影响自动刷新）

行为：
    - 自动定位文件（fork 可能改名/挪目录，按内容特征搜索）
    - 幂等：已修改过的文件会被检测到并跳过
    - 修改前自动备份为 <file>.bak
    - 找不到 pkg_observer（fork 砍掉了包观察者）时安全跳过，不算错误
"""

import sys
import shutil
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# 补丁 1：dispatch case
# 锚点选 CMD_SUSFS_ENABLE_AVC_LOG_SPOOFING：它在各 fork 里普遍存在、
# 位置固定在 ksu_handle_susfs_cmd() 尾部、不被 #ifdef 包裹。
# ─────────────────────────────────────────────────────────────
ANCHOR = '''    case CMD_SUSFS_ENABLE_AVC_LOG_SPOOFING: {
        susfs_set_avc_log_spoofing(arg);
        return 0;
    }
'''

INSERT = '''    case CMD_SUSFS_SET_PRIORITY_UID_FILTER: {
        susfs_set_priority_uid_filter(arg);
        return 0;
    }
'''


# ─────────────────────────────────────────────────────────────
# 补丁 2：pkg_observer 回调
# ─────────────────────────────────────────────────────────────
OBS_FUNC_SIG = 'static void ksu_defer_track_throne(void)'
OBS_FUNC_DECL = 'struct callback_head *cb;'
OBS_CALL = '''    // Renew the getpriority uid bitmap filter
    susfs_parse_priority_app_strings();
'''
OBS_INCLUDE_ANCHOR = '#include "klog.h" // IWYU pragma: keep'
OBS_INCLUDE_LINE = '#include <linux/susfs.h>'


def backup(path: Path) -> None:
    bak = path.with_name(path.name + '.bak')
    if not bak.exists():
        shutil.copy2(path, bak)
        print(f'    备份 -> {bak.name}')


def find_file(root: Path, needle: str, suffix: str = '.c') -> Path | None:
    """按内容特征在源码树里定位文件，跳过构建产物目录。"""
    for p in sorted(root.rglob(f'*{suffix}')):
        if any(part in ('.git', 'out', 'build') for part in p.parts):
            continue
        try:
            if needle in p.read_text(errors='ignore'):
                return p
        except OSError:
            continue
    return None


def patch_dispatch(root: Path) -> int:
    print('[1/2] dispatch case (CMD_SUSFS_SET_PRIORITY_UID_FILTER)')
    path = find_file(root, 'ksu_handle_susfs_cmd')
    if path is None:
        print('    错误: 找不到包含 ksu_handle_susfs_cmd 的 .c 文件，')
        print('    该 fork 可能没有 susfs supercall 通道，无法自动补丁。')
        return 1

    print(f'  目标文件: {path}')
    text = path.read_text()

    if 'CMD_SUSFS_SET_PRIORITY_UID_FILTER' in text:
        print('    已包含，跳过')
        return 0

    if text.count(ANCHOR) != 1:
        print(f'    错误: 锚点匹配 {text.count(ANCHOR)} 处（期望 1），')
        print('    fork 的代码结构有差异，请手动确认后修改脚本的 ANCHOR 常量。')
        return 1

    backup(path)
    text = text.replace(ANCHOR, INSERT + ANCHOR, 1)
    path.write_text(text)

    verify = path.read_text()
    if verify.count('CMD_SUSFS_SET_PRIORITY_UID_FILTER') != 1:
        print('    错误: 写入验证失败')
        return 1
    print('    完成')
    return 0


def patch_pkg_observer(root: Path) -> int:
    print('[2/2] pkg_observer 回调 (susfs_parse_priority_app_strings)')
    path = find_file(root, 'ksu_defer_track_throne')
    if path is None:
        print('    未找到 ksu_defer_track_throne，fork 可能无包观察者')
        print('    安全跳过（仅影响包变化后的名单自动刷新，非必需）')
        return 0

    print(f'  目标文件: {path}')
    text = path.read_text()

    if 'susfs_parse_priority_app_strings' in text:
        print('    已包含，跳过')
        return 0

    if OBS_FUNC_SIG not in text:
        print('    错误: 函数签名不匹配，fork 结构有差异，请手动处理')
        return 1

    backup(path)

    # 在函数体内第一个声明之后插入调用（函数体开头，与上游位置一致）
    sig_idx = text.index(OBS_FUNC_SIG)
    decl_idx = text.index(OBS_FUNC_DECL, sig_idx)
    insert_at = decl_idx + len(OBS_FUNC_DECL)
    text = text[:insert_at] + '\n\n' + OBS_CALL + text[insert_at:]

    # 头部 include（若无）
    if OBS_INCLUDE_LINE not in text:
        if OBS_INCLUDE_ANCHOR in text:
            text = text.replace(OBS_INCLUDE_ANCHOR,
                                OBS_INCLUDE_ANCHOR + '\n' + OBS_INCLUDE_LINE, 1)
        else:
            # 退化：塞到第一个 kernel 头文件前
            for line in text.splitlines(keepends=True):
                if line.startswith('#include <linux/'):
                    text = text.replace(line, OBS_INCLUDE_LINE + '\n' + line, 1)
                    break

    path.write_text(text)

    verify = path.read_text()
    if 'susfs_parse_priority_app_strings' not in verify:
        print('    错误: 写入验证失败')
        return 1
    print('    完成')
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(f'用法: {sys.argv[0]} <KSU源码目录>', file=sys.stderr)
        return 2

    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f'错误: {root} 不是目录', file=sys.stderr)
        return 2

    print('应用 priority uid filter fork 侧补丁：\n')
    rc = 0
    rc |= patch_dispatch(root)
    print()
    rc |= patch_pkg_observer(root)

    print()
    if rc == 0:
        print('完成。fork 侧补丁已全部应用（或原本已存在）。')
        print('后续检查（内核侧，本脚本不负责）:')
        print('  1. include/linux/susfs.h      含 susfs_set_priority_uid_filter /')
        print('                                susfs_parse_priority_app_strings 声明')
        print('  2. include/linux/susfs_def.h  含 CMD_SUSFS_SET_PRIORITY_UID_FILTER 0x60030')
        print('  3. fs/susfs.c                 含上述两个函数的实现')
        print('  4. defconfig                  CONFIG_KSU_SUSFS=y')
        print('  5. fs/susfs.c 里的 u:r:ksu:s0 改为 fork 实际 domain（MKSU 系是 su:s0）')
    else:
        print('有错误。所有 .bak 文件保留了原始内容。')
    return rc


if __name__ == '__main__':
    sys.exit(main())
