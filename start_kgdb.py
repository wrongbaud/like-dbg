#!/usr/bin/env python3

import requests
import configparser
import shutil
import tarfile
from typing import List
import re
import tqdm
from loguru import logger
from tqdm import tqdm
import os
from contextlib import contextmanager
import urllib.request
from pathlib import Path
import subprocess as sp
import docker
from fabric import Connection

config = Path.cwd() / 'config.ini'

# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
# | MISC QOL functions                                                                                  |
# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
def cfg_setter(obj, sections: List[str]) -> None:
    cfg = configparser.ConfigParser()
    cfg.read(config)
    for sect in sections:
        for key in cfg[sect]:
            tmp = cfg[sect][key]
            val = tmp if tmp not in ['yes', 'no'] else cfg[sect].getboolean(key)
            setattr(obj, key, val)

def tmux(cmd: str) -> None:
    sp.run(f"tmux {cmd}", shell=True)

def tmux_shell(cmd: str) -> None:
    tmux(f"send-keys '{cmd}' 'C-m'")

# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
# | TQDM WGET WITH PROGRESS BAR                                                                         |
# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
class DLProgressBarTQDM(tqdm):
    def update_to(self, b=1, bsize=1, tsize=None) -> None:
        if tsize is not None:
            self.total = tsize
        return self.update(b * bsize - self.n)  # also sets self.n = b * bsize


# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
# | KERNEL DOWNLOADER                                                                                   |
# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
class KernelDownloader:
    def __init__(self) -> None:
        cfg_setter(self, ['kernel_dl'])
        self.commit = self.commit if self.commit and not self.tag else self.resolve_latest()
        self.choice = self.tag if self.tag else self.commit
        logger.info(f"Using kernel with (tag/commit) {self.choice}")
        self.dl_uri = f"{self.snap_uri}{self.choice}.tar.gz"
        self.archive = Path(f"linux-{self.choice}.tar.gz")
        logger.debug(f"Kernel snap: {self.dl_uri}")

    def resolve_latest(self) -> str:
        commit_re = rb"commit\/\?id=[0-9a-z]*"
        r = requests.get(self.commit_uri)
        search_res = re.search(commit_re, r.content)
        if search_res.group():
            commit = search_res.group().split(b"=")[1].decode()
            logger.debug(f"Found latest commit: {commit}")
            return commit
        else:
            logger.error("Resolving latest commit")
            exit(-1)

    def is_present(self) -> bool:
        if Path(self.archive).exists():
            logger.info("Kernel archive already exists locally. Skipping download...")
            return True
        else:
            return False

    def download_kernel_archive(self) -> None:
        logger.info(f"Downloading {self.dl_uri}... This may take a while!")
        with DLProgressBarTQDM(unit="B", unit_scale=True, miniters=1, desc=self.archive.name) as t:
            urllib.request.urlretrieve(self.dl_uri, filename=self.archive, reporthook=t.update_to)
            t.total = t.n

    def run(self):
        if not self.is_present():
            self.download_kernel_archive()
        return self.archive


# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
# | KERNEL UNPACKER                                                                                     |
# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
class KernelUnpacker:
    def __init__(self, p: Path) -> None:
        cfg_setter(self, ['kernel_general'])
        self.kernel_root = Path(self.kernel_root)
        self.archive = p
        self.ex_name = Path(self.archive.name.split(".")[0])
        self.dst_content = None

    def _is_dest_empty(self) -> bool:
        if self.kernel_root.exists():
            self.content = [x.name for x in self.kernel_root.iterdir()]
            if self.content:
                return False
            else:
                return True
        else:
            return True

    def _is_vmlinux(self) -> int:
        if "vmlinux" in self.content:
            return 1
        else:
            return 0

    def _reuse_existing_vmlinux(self) -> int:
        choice = "y"
        logger.info('Found "vmlinux" locally. Re-use it? [Y/n]')
        tmp = input().lower()
        if tmp != "":
            choice = tmp
        if choice in ["y", "yes"]:
            logger.info("Reusing existing kernel...")
            return 1
        else:
            self._purge(self.kernel_root)
            self._unpack_targz()
            return 0

    def _unpack_targz(self) -> None:
        if not tarfile.is_tarfile(self.archive):
            logger.error("Did not get a valid kernel tar archive for unpacking. Exiting...")
            exit(-1)

        logger.info("Unpacking kernel archive...")
        with tarfile.open(self.archive, mode="r") as t:
            members = t.getmembers()
            for member in tqdm(iterable=members, total=len(members)):
                t.extract(member)
        self.ex_name.rename(self.kernel_root)

    @staticmethod
    def _purge(p: Path) -> None:
        logger.info("Purging unclean kernel build environment...")
        shutil.rmtree(p, ignore_errors=True)

    def run(self) -> int:
        if not self._is_dest_empty():
            if self._is_vmlinux():
                if self._reuse_existing_vmlinux():
                    return 1
        self._purge(self.kernel_root)
        self._unpack_targz()
        return 0


# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
# | KERNEL BUILDER                                                                                     |
# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
class KernelBuilder:
    def __init__(self) -> None:
        cfg_setter(self, ['kernel_general', 'kernel_builder', 'general'])

    @staticmethod
    @contextmanager
    def build_context(location: Path):
        cur_cwd = Path.cwd()
        try:
            os.chdir(location)
            yield
        finally:
            os.chdir(cur_cwd)

    def apply_patches(self):
        # TODO add option to apply patch files
        pass

    @staticmethod
    def _run_generic(cmd: str) -> int:
        ret = sp.run(cmd, shell=True)
        if ret.returncode != 0:
            logger.error(f"{cmd} in {Path.cwd()}")
            exit(ret.returncode)
        return ret.returncode

    def _build_mrproper(self):
        self._run_generic("make mrproper")

    def _build_arch(self):
        # TODO check how we need to sanitize the [general] config arch field to reflect the make options
        # All i know is it works if arch is x86_64
        self._run_generic(f"make {self.arch}_defconfig")

    def _build_kvm_guest(self):
        self._run_generic("make kvm_guest.config")

    def _configure_kernel(self):
        if self.mode == 'syzkaller':
            self._configure_syzkaller()
        else:
            self._configure_generic()

    def _configure_generic(self):
        self._run_generic(
            "./scripts/config \
            -e DEBUG_KERNEL \
            -e DEBUG_INFO \
            -e DEBUG_INFO_DWARF4 \
            -e FRAME_POINTER \
            -e GDB_SCRIPTS \
            -e KALLSYMS \
            -d DEBUG_INFO_DWARF5 \
            -d DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT \
            -d DEBUG_INFO_REDUCED \
            -d DEBUG_INFO_COMPRESSED \
            -d DEBUG_INFO_SPLIT \
            -d RANDOMIZE_BASE"
        )

    def _configure_syzkaller(self):
        self._run_generic(
            "./scripts/config \
            -e DEBUG_FS -e DEBUG_INFO \
            -e KALLSYMS -e KALLSYMS_ALL \
            -e NAMESPACES -e UTS_NS -e IPC_NS -e PID_NS -e NET_NS -e USER_NS \
            -e CGROUP_PIDS -e MEMCG -e CONFIGFS_FS -e SECURITYFS \
            -e KASAN -e KASAN_INLINE -e WARNING \
            -e FAULT_INJECTION -e FAULT_INJECTION_DEBUG_FS \
            -e FAILSLAB -e FAIL_PAGE_ALLOC \
            -e FAIL_MAKE_REQUEST -e FAIL_IO_TIMEOUT -e FAIL_FUTEX \
            -e LOCKDEP -e PROVE_LOCKING \
            -e DEBUG_ATOMIC_SLEEP \
            -e PROVE_RCU -e DEBUG_VM \
            -e REFCOUNT_FULL -e FORTIFY_SOURCE \
            -e HARDENED_USERCOPY -e LOCKUP_DETECTOR \
            -e SOFTLOCKUP_DETECTOR -e HARDLOCKUP_DETECTOR \
            -e BOOTPARAM_HARDLOCKUP_PANIC \
            -e DETECT_HUNG_TASK -e WQ_WATCHDOG \
            --set-val DEFAULT_HUNG_TASK_TIMEOUT 140 \
            --set-val RCU_CPU_STALL_TIMEOUT 100 \
            -e UBSAN \
            -d RANDOMIZE_BASE"
        )

    def _make(self):
        self._run_generic("make -j$(nproc) all")
        self._run_generic("make -j$(nproc) modules")

    def run(self):
        logger.info("Building kernel. This may take a while...")
        with self.build_context(self.kernel_root):
            self._build_mrproper()
            self._build_arch()
            self._build_kvm_guest()
            self._configure_kernel()
            self._make()
        logger.info("Successfully build the kernel")


# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
# | DOCKER RUNNER                                                                                       |
# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
class DockerRunner:
    def __init__(self) -> None:
        self.dockerfile_ctx = Path.cwd()
        self.dockerfile_path = ""
        self.client = docker.from_env()
        self.image = None
        self.tag = None

    def build_image_hl(self):
        image = self.client.images.build(
            path=str(self.dockerfile_ctx),
            dockerfile=self.dockerfile_path,
            tag=self.tag
        )[0]
        return image

    def build_image(self):
        for log_entry in self.cli.build(
            path=str(self.dockerfile_ctx),
            dockerfile=self.dockerfile_path,
            tag=self.tag,
            decode=True):
            v = next(iter(log_entry.values()))
            if isinstance(v ,str):
                v.strip()
                if v:
                    logger.debug(v)

    def get_image(self):
        try:
            image = self.client.images.get(self.tag)
            return image
        except docker.errors.ImageNotFound:
            return None

    def run(self):
        if not self.image:
            logger.info(f"Building fresh image for {type(self).__name__}")
            self.build_image()
            self.image = self.get_image()
        self.run_container()

    def run_container(self):
        pass


# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
# | ROOTFS BUILDER                                                                                      |
# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
class RootFSBuilder(DockerRunner):
    def __init__(self):
        super().__init__()
        cfg_setter(self, ['rootfs_general', 'rootfs_builder', 'general'])
        self.cli = docker.APIClient(base_url=self.docker_sock)
        self.ssh_conn = None
        self.guarantee_ssh()
        self.rootfs_path = "/" + self.rootfs_dir + self.rootfs_base + self.arch + self.rootfs_ftype
        self.dockerfile_path = self.dockerfile_ctx / self.dockerfile

    def guarantee_ssh(self):
        if Path(self.ssh_dir).exists() and os.listdir(self.ssh_dir):
            pass
            # logger.debug(f"Trying to reuse local ssh keys from {self.ssh_dir}...")
        else:
            # logger.debug("Generating new ssh key pair...")
            Path(self.ssh_dir).mkdir()
            sp.run(f'ssh-keygen -f {self.ssh_dir / "like.id_rsa"} -t rsa -N ""', shell=True)

    def _init_ssh(self):
        # TODO remove hardcoded values in favor of a config
        # TODO do we even want this
        self.ssh_conn = Connection("dbg@localhost:2222", connect_kwargs={"key_filename": ".ssh/like.id_rsa"})

    def run_container(self):
        command = f"/home/dbg/rootfs.sh -n {self.rootfs_path}"
        container = self.client.containers.run(
            self.image,
            volumes=[f"{Path.cwd() / 'io'}:/io"], 
            remove=True,
            detach=True,
            privileged=True,
            ports={"22/tcp": self.ssh_fwd_port},
            command=command
        )
        gen = container.logs(stream=True, follow=True)
        [logger.debug(log.strip().decode()) for log in gen]

    def check_existing(self):
        if Path(self.rootfs_path).exists():
            choice = "y"
            logger.info(f"Found {self.rootfs_path}. Re-use it? [Y/n]")
            tmp = input().lower()
            if tmp != "":
                choice = tmp
            if choice in ["y", "yes"]:
                logger.info("Reusing existing rootfs...")
                return self.rootfs_path
            else:
                self.image = self.get_image()
        else:
            self.image = self.get_image()

    def run(self):
        self.check_existing()
        super().run()

# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
# | DEBUGGER                                                                                            |
# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
class Debugger(DockerRunner):
    def __init__(self, arch: str, p: Path):
        super().__init__()
        cfg_setter(self, ['general'])
        self.cli = docker.APIClient(base_url=self.docker_sock)
        self.mnt = Path("/io")
        self.arch = arch
        self.project_root = p
        self.tag = "debugger"

    def build_container(self):
        logger.info("Building debug container...")

        self.image = self.client.images.build(
            path=str(self.dockerfile_ctx), dockerfile=self.dockerfile_ctx / ".dockerfile_dbg", tag=self.tag
        )[0]
        self.tag = self.image.tags[0]
        logger.debug(f"Image: {self.image}")

    def run_container(self):
        entrypoint = f"/home/dbg/debugger.sh -a {self.arch} -p {self.mnt}"
        runner = f'docker run -it --security-opt seccomp=unconfined --cap-add=SYS_PTRACE -v {Path.cwd() / self.project_root}:/io --net="host" {self.tag} {entrypoint}'
        tmux_shell(runner)

    def run(self):
        self.get_image()
        super().run() # TODO continue here


# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
# | DEBUGGEE                                                                                            |
# +-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-+
class Debuggee:
    def __init__(self):
        cfg_setter(self, ['debuggee', 'general', 'kernel_general', 'rootfs_general'])
        self.img = self.rootfs_dir + self.rootfs_base + self.arch + self.rootfs_ftype


    def run(self):
        cmd = f"qemu-system-{self.arch} -m {self.memory} -smp {self.smp} -kernel {self.kernel_root}/arch/{self.arch}/boot/bzImage"
        cmd += " -append \"console=ttyS0 root=/dev/sda earlyprintk=serial net.ifnames=0" 
        if self.kaslr:
            cmd += "\""
        else:
            cmd += " nokaslr\""
        cmd += f" -drive file={self.img},format=raw -net user,host=10.0.2.10,hostfwd=tcp:127.0.0.1:10021-:22 -net nic,model=e1000 -nographic -pidfile vm.pid"
        if self.kvm:
            cmd += " -enable-kvm"
        if self.dbg:
            cmd += " -S -s"
        tmux_shell(cmd)



def main():
    karchive = KernelDownloader().run()
    if not KernelUnpacker(karchive).run():
        KernelBuilder().run()
    RootFSBuilder().run()

    # By now we should be in a tmux window
    # Run pwndbg container
    #tmux_shell("tmux source-file .tmux.conf")
    #tmux("selectp -t 0")
    #tmux('rename-window "LIKE-DBG"')
    #tmux("splitw -h -p 50")
    #tmux("selectp -t 1")
    #tmux("splitw -v -p 50")

    # Then attach debugger
    #tmux("selectp -t 0")
    #Debugger("i386:x86-64:intel", Path("kernel_root")).run()

    # Start kernel first
    #tmux("selectp -t 1")
    #Debuggee().run()

if __name__ == "__main__":
    main()