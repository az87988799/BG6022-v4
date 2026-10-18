/* Installed before any untrusted Python is compiled. No Python API dependency. */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/audit.h>
#include <linux/capability.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sched.h>
#include <stddef.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#define DENY(n) BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, __NR_##n, 0, 1), \
                BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ERRNO | EPERM)
#define THREAD_FLAGS (CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD| \
                      CLONE_SYSVSEM|CLONE_SETTLS|CLONE_PARENT_SETTID| \
                      CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID|CLONE_DETACHED)

int install_task_filter(void) {
    struct __user_cap_header_struct head = {_LINUX_CAPABILITY_VERSION_3, 0};
    struct __user_cap_data_struct caps[2] = {{0}, {0}};
    if (syscall(SYS_capset, &head, caps) != 0) return -1;
    if (prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0) return -2;
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) return -3;
    struct sock_filter code[] = {
        BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, AUDIT_ARCH_X86_64, 1, 0),
        BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data, nr)),
        /* Reject the x32 syscall ABI as well as non-x86-64 architectures. */
        BPF_JUMP(BPF_JMP|BPF_JGE|BPF_K, 0x40000000, 0, 1),
        BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_KILL_PROCESS),
        DENY(execve), DENY(execveat), DENY(fork), DENY(vfork),
        DENY(socket), DENY(socketpair), DENY(connect), DENY(bind), DENY(listen),
        DENY(accept), DENY(accept4), DENY(ptrace),
        DENY(process_vm_readv), DENY(process_vm_writev),
        DENY(unshare), DENY(setns), DENY(mount), DENY(umount2),
        DENY(pivot_root), DENY(chroot), DENY(bpf), DENY(perf_event_open),
        DENY(open_by_handle_at), DENY(pidfd_getfd), DENY(capset),
        DENY(io_uring_setup), DENY(io_uring_enter), DENY(io_uring_register),
        DENY(kill), DENY(tkill),
        /* glibc may retry clone when clone3 reports ENOSYS. */
        BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, __NR_clone3, 0, 1),
        BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ERRNO | ENOSYS),
        BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, __NR_clone, 0, 11),
        BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_STMT(BPF_ALU|BPF_AND|BPF_K, (unsigned int)~THREAD_FLAGS),
        BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, 0, 1, 0),
        BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_STMT(BPF_ALU|BPF_AND|BPF_K, CLONE_THREAD|CLONE_VM|CLONE_SIGHAND),
        BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, CLONE_THREAD|CLONE_VM|CLONE_SIGHAND, 1, 0),
        BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data, args[0]) + 4),
        BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, 0, 1, 0),
        BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = {(unsigned short)(sizeof(code)/sizeof(code[0])), code};
    if (syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER,
                SECCOMP_FILTER_FLAG_TSYNC, &program) != 0) return -4;
    return 0;
}
