#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
在内核源码里加 uidfake 无差别隐藏辅助。

用法：
    python3 patch-uidfake.py <内核源码根目录>

改动：
    include/linux/uidfake_compat.h    新增（若不存在）
    kernel/sys.c                      getpriority / setpriority 的 PRIO_USER 分支
    block/ioprio.c 或 fs/ioprio.c     ioprio_get / ioprio_set 的 IOPRIO_WHO_USER 分支

原则：
    - 无标记即无操作：fakeuid 未加载时，所有进程 tag 为 0，判断恒为 false。
    - 只读 thread_info.flags，不写入，不影响系统进程。
    - 幂等：已修改过的文件会被检测到并跳过。
    - 修改前自动备份为 <file>.uidfake.bak。
"""

import sys
import shutil
from pathlib import Path


HEADER = r'''/* SPDX-License-Identifier: GPL-2.0 */
/*
 * uidfake_compat.h - 内核侧对 App 进程的无差别 uid 隐藏。
 *
 * 方案 B：不依赖 fakeuid 模块的 tag，直接看 uid 范围。
 *
 * 满足以下条件时返回 true，syscall 层返回 -ESRCH：
 *   - caller 是普通 App（10000..19999）或隔离进程（90000..98999）
 *   - target 是普通 App（10000..19999）
 *   - target 不是 caller 自己
 *   - caller 没有 CAP_SYS_NICE / CAP_SYS_ADMIN
 *
 * 该逻辑完全独立于 fakeuid 模块：
 *   - 不读 thread_info.flags
 *   - 不依赖任何 tag
 *   - 不需要 netlink、不需要用户态组件
 *   - 只需要这段头文件 + kernel/sys.c 和 ioprio.c 的四处调用
 *
 * 系统进程（uid < 10000）不受影响：caller_ok 返回 false。
 * 内核线程不调用这些 syscall，天然不受影响。
 */
#ifndef _LINUX_UIDFAKE_COMPAT_H
#define _LINUX_UIDFAKE_COMPAT_H

#include <linux/types.h>
#include <linux/sched.h>
#include <linux/cred.h>
#include <linux/uidgid.h>
#include <linux/capability.h>

/* caller 是否是 App 层面的进程（普通 App 或隔离进程） */
static __always_inline bool uidfake_caller_is_app(kuid_t caller)
{
	u32 app = __kuid_val(caller) % 100000u;

	return (app >= 10000u && app <= 19999u) ||
	       (app >= 90000u && app <= 98999u);
}

/* target 是否是普通 App uid */
static __always_inline bool uidfake_target_is_app(kuid_t uid)
{
	u32 app = __kuid_val(uid) % 100000u;

	return app >= 10000u && app <= 19999u;
}

/*
 * 是否对被查询的 uid 做无差别失效。
 *
 * 判断顺序按"便宜 + 常失败"排列：
 *   1. target_is_app   纯寄存器运算，最便宜，target 常是系统/隔离 uid
 *   2. caller_is_app   读 cred，中等开销
 *   3. uid_eq          读 cred，和 caller 判断共享 current_uid()
 *   4. capable         最贵，普通 App 没能力
 */
static __always_inline bool uidfake_should_hide(kuid_t uid)
{
	kuid_t caller = current_uid();

	/* 1. target 不是普通 App，直接放行 */
	if (!uidfake_target_is_app(uid))
		return false;

	/* 2. caller 不是 App/隔离进程，直接放行 */
	if (!uidfake_caller_is_app(caller))
		return false;

	/* 3. 查询自己，不隐藏 */
	if (uid_eq(uid, caller))
		return false;

	/* 4. 持有修改类能力的进程，走原生路径 */
	if (capable(CAP_SYS_NICE) || capable(CAP_SYS_ADMIN))
		return false;

	return true;
}

#endif /* _LINUX_UIDFAKE_COMPAT_H */
'''


SYS_INCLUDE_ANCHOR = '#include "uid16.h"\n'
SYS_INCLUDE_LINE   = '#include <linux/uidfake_compat.h>\n'

# kernel/sys.c 中 getpriority 和 setpriority 的 PRIO_USER 分支
# 同一段代码出现两次，一起替换。
SYS_OLD = '''\t\telse if (!uid_eq(uid, cred->uid)) {
\t\t\tuser = find_user(uid);
\t\t\tif (!user)
\t\t\t\tgoto out_unlock;\t/* No processes for this user */
\t\t}
'''

SYS_NEW = '''\t\telse if (!uid_eq(uid, cred->uid)) {
\t\t\tuser = find_user(uid);
\t\t\tif (!user)
\t\t\t\tgoto out_unlock;\t/* No processes for this user */
\t\t\tif (uidfake_should_hide(uid)) {
\t\t\t\tfree_uid(user);
\t\t\t\tgoto out_unlock;
\t\t\t}
\t\t}
'''


IOPRIO_INCLUDE_ANCHOR = '#include <linux/pid_namespace.h>\n'
IOPRIO_INCLUDE_LINE   = '#include <linux/uidfake_compat.h>\n'

# block/ioprio.c 中 ioprio_set 的 IOPRIO_WHO_USER 分支
IOPRIO_SET_OLD = '''\t\tcase IOPRIO_WHO_USER:
\t\t\tuid = make_kuid(current_user_ns(), who);
\t\t\tif (!uid_valid(uid))
\t\t\t\tbreak;
\t\t\tif (!who)
\t\t\t\tuser = current_user();
\t\t\telse
\t\t\t\tuser = find_user(uid);

\t\t\tif (!user)
\t\t\t\tbreak;

\t\t\tfor_each_process_thread(g, p) {
'''

IOPRIO_SET_NEW = '''\t\tcase IOPRIO_WHO_USER:
\t\t\tuid = make_kuid(current_user_ns(), who);
\t\t\tif (!uid_valid(uid))
\t\t\t\tbreak;
\t\t\tif (!who)
\t\t\t\tuser = current_user();
\t\t\telse
\t\t\t\tuser = find_user(uid);

\t\t\tif (!user)
\t\t\t\tbreak;

\t\t\tif (uidfake_should_hide(uid)) {
\t\t\t\tif (who)
\t\t\t\t\tfree_uid(user);
\t\t\t\tbreak;
\t\t\t}

\t\t\tfor_each_process_thread(g, p) {
'''

# block/ioprio.c 中 ioprio_get 的 IOPRIO_WHO_USER 分支
# 与 ioprio_set 的区别：没有 uid_valid 检查
IOPRIO_GET_OLD = '''\t\tcase IOPRIO_WHO_USER:
\t\t\tuid = make_kuid(current_user_ns(), who);
\t\t\tif (!who)
\t\t\t\tuser = current_user();
\t\t\telse
\t\t\t\tuser = find_user(uid);

\t\t\tif (!user)
\t\t\t\tbreak;

\t\t\tfor_each_process_thread(g, p) {
'''

IOPRIO_GET_NEW = '''\t\tcase IOPRIO_WHO_USER:
\t\t\tuid = make_kuid(current_user_ns(), who);
\t\t\tif (!who)
\t\t\t\tuser = current_user();
\t\t\telse
\t\t\t\tuser = find_user(uid);

\t\t\tif (!user)
\t\t\t\tbreak;

\t\t\tif (uidfake_should_hide(uid)) {
\t\t\t\tif (who)
\t\t\t\t\tfree_uid(user);
\t\t\t\tbreak;
\t\t\t}

\t\t\tfor_each_process_thread(g, p) {
'''


def backup(path: Path) -> None:
    bak = path.with_name(path.name + '.uidfake.bak')
    if not bak.exists():
        shutil.copy2(path, bak)
        print(f'    备份 -> {bak.name}')


def write_header(root: Path) -> int:
    path = root / 'include' / 'linux' / 'uidfake_compat.h'
    if path.exists():
        print(f'  {path.relative_to(root)} 已存在，跳过')
        return 0
    path.write_text(HEADER)
    print(f'  {path.relative_to(root)} 写入完成')
    return 0


def patch_sys_c(path: Path) -> int:
    print(f'  {path}')
    text = path.read_text()
    if 'uidfake_should_hide' in text:
        print('    已修改，跳过')
        return 0

    count = text.count(SYS_OLD)
    if count == 0:
        print('    错误：找不到 PRIO_USER 分支的 find_user 代码')
        return 1
    if count != 2:
        print(f'    错误：匹配到 {count} 处（期望 2：getpriority + setpriority）')
        return 1

    backup(path)
    text = text.replace(SYS_OLD, SYS_NEW)

    if 'uidfake_compat.h' not in text:
        if SYS_INCLUDE_ANCHOR in text:
            text = text.replace(SYS_INCLUDE_ANCHOR,
                                SYS_INCLUDE_ANCHOR + '\n' + SYS_INCLUDE_LINE,
                                1)
        else:
            # 退化：放在第一个 linux/ 头文件前面
            for line in text.splitlines(keepends=True):
                if line.startswith('#include <linux/'):
                    text = text.replace(line,
                                        SYS_INCLUDE_LINE + line, 1)
                    break

    path.write_text(text)
    print(f'    完成（{count} 处分支已加固）')
    return 0


def find_ioprio(root: Path) -> Path | None:
    for candidate in (root / 'block' / 'ioprio.c',
                      root / 'fs' / 'ioprio.c'):
        if candidate.is_file():
            return candidate
    return None


def patch_ioprio_c(path: Path) -> int:
    print(f'  {path}')
    text = path.read_text()
    if 'uidfake_should_hide' in text:
        print('    已修改，跳过')
        return 0

    n_set = text.count(IOPRIO_SET_OLD)
    n_get = text.count(IOPRIO_GET_OLD)
    if n_set != 1 or n_get != 1:
        print(f'    错误：ioprio_set={n_set}, ioprio_get={n_get}（期望各 1）')
        return 1

    backup(path)
    text = text.replace(IOPRIO_SET_OLD, IOPRIO_SET_NEW)
    text = text.replace(IOPRIO_GET_OLD, IOPRIO_GET_NEW)

    if 'uidfake_compat.h' not in text:
        if IOPRIO_INCLUDE_ANCHOR in text:
            text = text.replace(IOPRIO_INCLUDE_ANCHOR,
                                IOPRIO_INCLUDE_ANCHOR + IOPRIO_INCLUDE_LINE,
                                1)
        else:
            for line in text.splitlines(keepends=True):
                if line.startswith('#include <linux/'):
                    text = text.replace(line,
                                        IOPRIO_INCLUDE_LINE + line, 1)
                    break

    path.write_text(text)
    print('    完成（ioprio_get + ioprio_set 已加固）')
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(f'用法：{sys.argv[0]} <内核源码根目录>', file=sys.stderr)
        return 2

    root = Path(sys.argv[1]).resolve()
    if not (root / 'kernel' / 'sys.c').is_file():
        print(f'错误：{root} 不是内核源码根目录（缺 kernel/sys.c）',
              file=sys.stderr)
        return 2

    ioprio = find_ioprio(root)
    if ioprio is None:
        print('错误：找不到 block/ioprio.c 或 fs/ioprio.c', file=sys.stderr)
        return 2

    print('应用 uidfake 内核侧补丁：')
    rc = 0
    rc |= write_header(root)
    rc |= patch_sys_c(root / 'kernel' / 'sys.c')
    rc |= patch_ioprio_c(ioprio)

    if rc == 0:
        print('\n完成。重新编译内核：')
        print('  make ARCH=arm64 LLVM=1 -j$(nproc)')
    else:
        print('\n有错误，未全部完成；.uidfake.bak 保留了原始文件。')
    return rc


if __name__ == '__main__':
    sys.exit(main())
