#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
给第三方 KernelSU fork 的 dispatch.c 补 CMD_SUSFS_SET_PRIORITY_UID_FILTER case。

用法：
    python3 patch-priority-uid-filter.py <KSU源码目录>

行为：
    - 自动定位包含 ksu_handle_susfs_cmd() 的 .c 文件（通常是
      kernel/supercall/dispatch.c，fork 可能改名/挪目录）
    - 在 CMD_SUSFS_ENABLE_AVC_LOG_SPOOFING case 之前插入新 case
      （选这个锚点是因为它在你的 fork 里存在且不被 #ifdef 包裹，
      比 SUS_MAP 锚点稳定）
    - 幂等：已含 SET_PRIORITY_UID_FILTER 则跳过
    - 修改前备份为 <file>.bak
"""

import sys
import shutil
from pathlib import Path

# 锚点：你 fork 里已有的、位置固定且不在 #ifdef 里的 case
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


def find_dispatch(root: Path) -> Path | None:
    """找包含 ksu_handle_susfs_cmd 的源文件。"""
    candidates = list(root.rglob('*.c'))
    for c in candidates:
        # 跳过构建产物目录，加速
        if any(part in ('.git', 'out', 'build') for part in c.parts):
            continue
        try:
            text = c.read_text(errors='ignore')
        except OSError:
            continue
        if 'ksu_handle_susfs_cmd' in text:
            return c
    return None


def backup(path: Path) -> None:
    bak = path.with_name(path.name + '.bak')
    if not bak.exists():
        shutil.copy2(path, bak)
        print(f'    备份 -> {bak.name}')


def patch(path: Path) -> int:
    print(f'  目标文件: {path}')
    text = path.read_text()

    # 幂等检查
    if 'CMD_SUSFS_SET_PRIORITY_UID_FILTER' in text:
        print('    已包含 SET_PRIORITY_UID_FILTER，跳过')
        return 0

    # 锚点必须在，且不能在一个被注释掉的块里（粗查）
    if text.count(ANCHOR) != 1:
        print(f'    错误：锚点匹配到 {text.count(ANCHOR)} 处'
              f'（期望 1：CMD_SUSFS_ENABLE_AVC_LOG_SPOOFING case），')
        print('    说明 fork 的代码结构有差异，请手动确认后改 ANCHOR 常量。')
        return 1

    # 如果 AVC case 在某层 #ifdef 内而目标位置不对，提示但不拦截——
    # 绝大多数 fork 里它和 SET_PRIORITY_UID_FILTER 同属"无独立 Kconfig"的命令
    backup(path)
    text = text.replace(ANCHOR, INSERT + ANCHOR, 1)
    path.write_text(text)

    # 回读验证
    verify = path.read_text()
    assert verify.count('CMD_SUSFS_SET_PRIORITY_UID_FILTER') == 1
    print('    完成：case 已插入 ksu_handle_susfs_cmd()')
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(f'用法: {sys.argv[0]} <KSU源码目录>', file=sys.stderr)
        return 2

    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f'错误: {root} 不是目录', file=sys.stderr)
        return 2

    print('定位 dispatch 文件...')
    target = find_dispatch(root)
    if target is None:
        print('错误: 找不到包含 ksu_handle_susfs_cmd 的 .c 文件，'
              '该 fork 可能没有 susfs supercall 通道', file=sys.stderr)
        return 2

    print('应用补丁:')
    rc = patch(target)

    if rc == 0:
        print('\n完成。后续步骤:')
        print('  1. 确认内核侧 include/linux/susfs.h 含 susfs_set_priority_uid_filter 声明')
        print('  2. 确认内核侧 include/linux/susfs_def.h 含 0x60030')
        print('  3. defconfig: CONFIG_KSU_SUSFS=y')
        print('  4. grep fs/susfs.c 里的 ksu:s0，按 fork 实际 domain 修正')
    else:
        print('\n有错误，未修改。')
    return rc


if __name__ == '__main__':
    sys.exit(main())
