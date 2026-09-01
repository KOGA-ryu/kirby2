"""Closed SSH executor for the Linux x86_64 release qualification.

This module owns one exact Fedora provider and one exact WO40-H operation.  It is
not a generic SSH adapter: callers cannot select a host, credential, command,
network policy, remote path, timeout, or cleanup target.  The controller transfers
only immutable WO40-F inputs, recreates one session path for the two forms in
sequence, installs and executes beneath an unprivileged network namespace, and
publishes the canonical provider and attempt records only after remote cleanup.

The installed qualification worker remains the product oracle.  Checkout Python
controls the provider but never enters the installed worker's effective import
path.  SSH is required for provider proof, exact artifact transfer, execution, and
cleanup; it is pinned to one ED25519 host key and cannot use an agent, password,
forward, proxy, multiplexed connection, or trust-on-first-use fallback.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import pwd
import re
import secrets
import shlex
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, Mapping, Sequence

from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes

from .artifacts import (
    RELEASE_ARTIFACT_INDEX_FILENAME_V1,
    RELEASE_ARTIFACT_MAX_BYTES_V1,
    RELEASE_BUILD_RECORD_FILENAME_V1,
    RELEASE_RECORD_MAX_BYTES_V1,
    ReleaseArtifactBuildRecordV1,
)
from .build import (
    ReleaseCleanProviderInventoryV1,
    ReleaseCleanProviderV1,
    ReleaseCommandOutcomeV1,
    ReleaseCommandStatusV1,
    ReleaseProtocolBundleV1,
    verify_release_artifacts,
)
from .manifest import RELEASE_VERSION_V1, ReleaseArtifactIndexV1
from .qualification import (
    ReleaseBuildEvidenceBindingV1,
    _require_canonical_tracked_build_evidence,
    _require_macos_integer_core_baseline,
    _verify_release_qualification_records,
    verify_release_qualification,
)
from .qualification_executor import (
    QualificationExecutorRefusalCodeV1,
    _CommandResult,
    _FileSnapshot,
    _QualificationRefused,
    _absolute_input,
    _copy_projection_file,
    _executor_outcome,
    _fsync_directory,
    _identity,
    _open_artifact_store,
    _open_publication_directory,
    _publish_records,
    _record_target_path,
    _require_store_anchor,
    _run_command,
    _same_snapshot,
    _sha256,
    _stable_read,
    _write_private,
)
from .qualification_records import (
    RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1,
    RELEASE_QUALIFICATION_ATTESTATION_METHOD_V1,
    RELEASE_QUALIFICATION_INSTALLATION_SOURCE_V1,
    ReleaseCleanProviderAttestationV1,
    ReleaseQualificationArtifactBindingV1,
    ReleaseQualificationAttemptV1,
    ReleaseQualificationCommandObservationV1,
    ReleaseQualificationFactsV1,
    ReleaseQualificationNetworkScopeV1,
    ReleaseQualificationRootObservationV1,
    ReleaseQualificationSessionV1,
    ReleaseQualificationStepObservationV1,
    build_release_qualification_attempt_record,
    release_qualification_record_paths,
    verify_release_qualification_record,
)


LINUX_TARGET_ID_V1: Final[str] = "linux-x86_64"
LINUX_GATE_ID_V1: Final[str] = "WO40-H"
LINUX_PROVIDER_POLICY_ID_V1: Final[str] = (
    "KIRBY2_SSH_EPHEMERAL_HOST_PROVIDER_V1"
)
LINUX_PROVIDER_ADAPTER_ID_V1: Final[str] = "SSH_EPHEMERAL_HOST_V1"

SSH_EXECUTABLE_V1: Final[Path] = Path("/usr/bin/ssh")
SFTP_EXECUTABLE_V1: Final[Path] = Path("/usr/bin/sftp")
SSH_HOST_V1: Final[str] = "172.16.1.63"
SSH_USER_V1: Final[str] = "kogaRyu"
SSH_PORT_V1: Final[int] = 22
SSH_HOST_KEY_ALIAS_V1: Final[str] = "kirby2-fedora-provider"
SSH_HOST_KEY_ALGORITHM_V1: Final[str] = "ssh-ed25519"
SSH_HOST_KEY_PUBLIC_V1: Final[str] = (
    "AAAAC3NzaC1lZDI1NTE5AAAAILKn61YwmdypxUQPgCGqNrfoSDGvO8h1Djhw2hMwXjHP"
)
SSH_HOST_KEY_FINGERPRINT_V1: Final[str] = (
    "SHA256:3Ku8lT/Ujew/fkGMSYb0b1cQB+ZG4Mkeul/FwomA0Ps"
)
SSH_IDENTITY_FILE_V1: Final[Path] = Path.home().resolve() / ".ssh" / "id_ed25519"

_FORMS: Final[tuple[str, ...]] = ("desktop", "headless")
_ARTIFACT_SELECTORS: Final[Mapping[str, str]] = {
    "desktop": "linux-x86_64/desktop",
    "headless": "linux-x86_64/headless",
}
_BUNDLE_ARTIFACTS: Final[Mapping[str, str]] = {
    "desktop": "linux-x86_64-desktop-bundle",
    "headless": "linux-x86_64-wheelhouse",
}
_BUNDLE_ROOTS: Final[Mapping[str, str]] = {
    "desktop": f"kirby2-{RELEASE_VERSION_V1}-linux-x86_64",
    "headless": f"kirby2-{RELEASE_VERSION_V1}-linux-x86_64-wheelhouse",
}
_INSTALLED_LAUNCHERS: Final[Mapping[str, str]] = {
    "desktop": "kirby2-desktop",
    "headless": "kirby2-headless",
}
_REMOTE_PYTHON_CANDIDATES: Final[tuple[str, ...]] = (
    "/usr/bin/python3.14",
    "/usr/local/bin/python3.14",
)
_WORKER_SCHEMA_ID_V1: Final[str] = (
    "KIRBY2_RELEASE_QUALIFICATION_WORKER_RESULT_V1"
)
_WORKER_EXECUTION_POLICY_ID_V1: Final[str] = (
    "KIRBY2_WO40_GH_INSTALLED_EXECUTION_POLICY_V1"
)
_PROJECT_WHEEL_V1: Final[str] = f"kirby2-{RELEASE_VERSION_V1}-py3-none-any.whl"

_UNSHARE_PREFIX_V1: Final[tuple[str, ...]] = (
    "/usr/bin/unshare",
    "--user",
    "--map-root-user",
    "--net",
    "--pid",
    "--fork",
    "--kill-child=KILL",
    "--",
)
_NO_NEW_PRIVILEGES_PREFIX_V1: Final[tuple[str, ...]] = (
    "/usr/bin/setpriv",
    "--no-new-privs",
    "--",
)

_HOST_COMMAND_TIMEOUT_SECONDS: Final[int] = 600
_WORKER_TIMEOUT_SECONDS: Final[int] = 3_600
_COMMAND_OUTPUT_MAX_BYTES: Final[int] = 16 * 1024 * 1024
_WORKER_OUTPUT_MAX_BYTES: Final[int] = 32 * 1024 * 1024
_BUILD_EVIDENCE_MAX_BYTES: Final[int] = 16 * 1024 * 1024
_MINIMUM_REMOTE_FREE_BYTES: Final[int] = 20 * 1024 * 1024 * 1024
_MINIMUM_REMOTE_MEMORY_BYTES: Final[int] = 8 * 1024 * 1024 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_REMOTE_ROOT = re.compile(
    r"/var/tmp/kirby2-wo40h-[0-9a-f]{12}-[0-9a-f]{32}\Z"
)
_REMOTE_PROVIDER_LOCK_V1: Final[PurePosixPath] = PurePosixPath(
    "/var/tmp/kirby2-wo40h-provider.lock"
)
_REMOTE_HOME = re.compile(r"/home/[A-Za-z0-9._-]{1,128}\Z")
_SAFE_TRANSFER_PATH = re.compile(r"/[A-Za-z0-9_./-]{1,4095}\Z")
_SAFE_INPUT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")


_PROVIDER_PROBE_SCRIPT: Final[str] = r'''import hashlib,importlib.util,json,os,platform,pwd,re,shutil,sys
def text(path):
    try:
        return open(path,"r",encoding="utf-8",errors="strict").read().strip()
    except (OSError,UnicodeError):
        return "UNAVAILABLE"
values={}
for line in text("/etc/os-release").splitlines():
    if "=" in line:
        key,value=line.split("=",1)
        values[key]=value.strip().strip('"')
memory=0
for line in text("/proc/meminfo").splitlines():
    if line.startswith("MemTotal:"):
        memory=int(line.split()[1])*1024
        break
boot=text("/proc/sys/kernel/random/boot_id")
os_version=values.get("PRETTY_NAME","UNAVAILABLE")
machine_model=text("/sys/class/dmi/id/product_name")
if (re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",boot) is None
        or os_version=="UNAVAILABLE" or machine_model=="UNAVAILABLE"):
    raise SystemExit(70)
home=pwd.getpwuid(os.getuid()).pw_dir
payload={
 "available_disk_bytes":shutil.disk_usage("/var/tmp").free,
 "base_exec_prefix":os.path.realpath(sys.base_exec_prefix),
 "base_prefix":os.path.realpath(sys.base_prefix),
 "boot_id_sha256":hashlib.sha256(boot.encode("utf-8")).hexdigest(),
 "cpu_count":os.cpu_count() or 0,
 "gid":os.getgid(),
 "home":home,
 "kernel_release":platform.release(),
 "machine":platform.machine(),
 "machine_model":machine_model,
 "memory_bytes":memory,
 "os_version":os_version,
 "python_executable":sys.executable,
 "python_executable_realpath":os.path.realpath(sys.executable),
 "python_implementation":platform.python_implementation(),
 "python_version":platform.python_version(),
 "rmtree_symlink_safe":bool(getattr(shutil.rmtree,"avoids_symlink_attacks",False)),
 "source_checkout_present":importlib.util.find_spec("kirby2") is not None,
 "system":platform.system(),
 "uid":os.getuid(),
}
print(json.dumps(payload,sort_keys=True,separators=(",",":")))'''

_NETWORK_PROBE_SCRIPT: Final[str] = r'''import hashlib,json,os,socket
interfaces=sorted(name for _index,name in socket.if_nameindex())
route4=[]
try:
    lines=open("/proc/net/route","r",encoding="ascii").read().splitlines()[1:]
    route4=[line for line in lines if line.split() and line.split()[1]=="00000000"]
except OSError:
    route4=["UNREADABLE"]
route6=[]
try:
    for line in open("/proc/net/ipv6_route","r",encoding="ascii").read().splitlines():
        parts=line.split()
        if (len(parts)>=10 and parts[0]=="0"*32 and parts[1]=="00"
                and parts[-1]!="lo"):
            route6.append(line)
except OSError:
    route6=["UNREADABLE"]
sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
sock.settimeout(2.0)
try:
    test_net_result=sock.connect_ex(("192.0.2.1",9))
finally:
    sock.close()
uid_map=open("/proc/self/uid_map","rb").read()
status=open("/proc/self/status","r",encoding="ascii").read().splitlines()
no_new_privs=[line for line in status if line.startswith("NoNewPrivs:")]
if len(no_new_privs)!=1: raise SystemExit(70)
payload={
 "default_ipv4_route_count":len(route4),
 "default_ipv6_route_count":len(route6),
 "interfaces":interfaces,
 "no_new_privileges":no_new_privs[0].split()[1]=="1",
 "test_net_connect_result":test_net_result,
 "uid":os.getuid(),
 "uid_map_sha256":hashlib.sha256(uid_map).hexdigest(),
}
print(json.dumps(payload,sort_keys=True,separators=(",",":")))'''

_CREATE_ROOT_SCRIPT: Final[str] = r'''import base64,hashlib,json,os,stat,sys
root=sys.argv[1]
marker=base64.b64decode(sys.argv[2].encode("ascii"),validate=True)
if not root.startswith("/var/tmp/kirby2-wo40h-") or "/" in root[len("/var/tmp/"):]:
    raise SystemExit(70)
os.mkdir(root,0o700)
for leaf in ("home","inputs","tmp","unpacked"):
    os.mkdir(root+"/"+leaf,0o700)
flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)
fd=os.open(root+"/.kirby2-owner.json",flags,0o400)
try:
    view=memoryview(marker)
    while view:
        count=os.write(fd,view)
        if count<=0: raise OSError("short marker write")
        view=view[count:]
    os.fsync(fd)
finally:
    os.close(fd)
for path in (root+"/inputs",root+"/tmp",root+"/unpacked",root,"/var/tmp"):
    directory=os.open(path,os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0))
    try: os.fsync(directory)
    finally: os.close(directory)
metadata=os.lstat(root)
payload={"marker_sha256":hashlib.sha256(marker).hexdigest(),"mode":stat.S_IMODE(metadata.st_mode),"root":root,"uid":metadata.st_uid}
print(json.dumps(payload,sort_keys=True,separators=(",",":")))'''

_FINALIZE_INPUTS_SCRIPT: Final[str] = r'''import base64,hashlib,json,os,stat,sys
root=sys.argv[1]
expected=json.loads(base64.b64decode(sys.argv[2].encode("ascii"),validate=True))
if type(expected) is not list: raise SystemExit(70)
directory=os.open(root+"/inputs",os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0))
observed=[]
try:
    names=[]
    for row in expected:
        if type(row) is not dict or set(row)!={"name","sha256","size"}: raise SystemExit(70)
        name=row["name"]
        if type(name) is not str or not name or "/" in name or name in {".",".."}: raise SystemExit(70)
        names.append(name)
    if len(names)!=len(set(names)) or sorted(os.listdir(root+"/inputs"))!=sorted(name+".part" for name in names): raise SystemExit(70)
    for row in expected:
        name=row["name"]
        staging=name+".part"
        flags=os.O_RDONLY|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)
        fd=os.open(staging,flags,dir_fd=directory)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid!=os.getuid(): raise SystemExit(71)
            digest=hashlib.sha256(); size=0
            while True:
                chunk=os.read(fd,1024*1024)
                if not chunk: break
                size+=len(chunk); digest.update(chunk)
            after=os.fstat(fd)
            if (before.st_dev,before.st_ino,before.st_mode,before.st_nlink,before.st_uid,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_mode,after.st_nlink,after.st_uid,after.st_size,after.st_mtime_ns): raise SystemExit(72)
            value=digest.hexdigest()
            if size!=row["size"] or value!=row["sha256"]: raise SystemExit(73)
            os.fchmod(fd,0o444); os.fsync(fd)
        finally:
            os.close(fd)
        try: os.stat(name,dir_fd=directory,follow_symlinks=False)
        except FileNotFoundError: pass
        else: raise SystemExit(74)
        os.rename(staging,name,src_dir_fd=directory,dst_dir_fd=directory)
        observed.append({"name":name,"sha256":value,"size":size})
    if sorted(os.listdir(root+"/inputs"))!=sorted(names): raise SystemExit(75)
    os.fsync(directory)
finally:
    os.close(directory)
print(json.dumps({"inputs":observed},sort_keys=True,separators=(",",":")))'''

_ORIGIN_PROBE_SCRIPT: Final[str] = r'''import importlib.util,json,os,sys
spec=importlib.util.find_spec("kirby2")
real=os.path.realpath
payload={
 "base_exec_prefix":real(sys.base_exec_prefix),
 "base_prefix":real(sys.base_prefix),
 "executable":sys.executable,
 "executable_realpath":real(sys.executable),
 "origin":None if spec is None else real(spec.origin),
 "path":[real(item) if item else item for item in sys.path],
 "prefix":real(sys.prefix),
}
print(json.dumps(payload,sort_keys=True,separators=(",",":")))'''

_CLEANUP_SCRIPT: Final[str] = r'''import hashlib,json,os,re,shutil,stat,sys
root=sys.argv[1]; expected=sys.argv[2]
pattern=re.compile(r"/var/tmp/kirby2-wo40h-[0-9a-f]{12}-[0-9a-f]{32}\Z")
if pattern.fullmatch(root) is None or os.path.realpath(root)!=root: raise SystemExit(70)
name=os.path.basename(root)
options=os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)
parent=os.open("/var/tmp",options); directory=None
try:
    directory=os.open(name,options,dir_fd=parent)
    metadata=os.fstat(directory)
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=os.getuid()
            or stat.S_IMODE(metadata.st_mode)!=0o700): raise SystemExit(71)
    marker_name=".kirby2-owner.json"
    flags=os.O_RDONLY|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)
    marker_fd=os.open(marker_name,flags,dir_fd=directory)
    try:
        before=os.fstat(marker_fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1
                or before.st_uid!=os.getuid() or stat.S_IMODE(before.st_mode)!=0o400
                or before.st_size<=0 or before.st_size>65536): raise SystemExit(72)
        raw=b""
        while len(raw)<before.st_size:
            chunk=os.read(marker_fd,min(65536,before.st_size-len(raw)))
            if not chunk: break
            raw+=chunk
        after=os.fstat(marker_fd)
        identity=lambda item:(item.st_dev,item.st_ino,item.st_mode,item.st_nlink,item.st_uid,item.st_gid,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
        if len(raw)!=before.st_size or identity(before)!=identity(after): raise SystemExit(73)
    finally:
        os.close(marker_fd)
    if hashlib.sha256(raw).hexdigest()!=expected: raise SystemExit(74)
    if not getattr(shutil.rmtree,"avoids_symlink_attacks",False): raise SystemExit(75)
    for child in os.listdir(directory):
        child_metadata=os.stat(child,dir_fd=directory,follow_symlinks=False)
        if stat.S_ISDIR(child_metadata.st_mode) and not stat.S_ISLNK(child_metadata.st_mode):
            shutil.rmtree(child,dir_fd=directory)
        else:
            os.unlink(child,dir_fd=directory)
    os.fsync(directory)
    current=os.stat(name,dir_fd=parent,follow_symlinks=False)
    if ((current.st_dev,current.st_ino)!=(metadata.st_dev,metadata.st_ino)
            or not stat.S_ISDIR(current.st_mode) or os.listdir(directory)): raise SystemExit(76)
    os.rmdir(name,dir_fd=parent); os.fsync(parent)
    try: os.stat(name,dir_fd=parent,follow_symlinks=False)
    except FileNotFoundError: pass
    else: raise SystemExit(77)
finally:
    if directory is not None: os.close(directory)
    os.close(parent)
print(json.dumps({"deleted":True,"root":root},sort_keys=True,separators=(",",":")))'''

_PROCESS_ABSENCE_SCRIPT: Final[str] = r'''import json,os,re,sys
root=sys.argv[1]
pattern=re.compile(r"/var/tmp/kirby2-wo40h-[0-9a-f]{12}-[0-9a-f]{32}\Z")
if pattern.fullmatch(root) is None: raise SystemExit(70)
ignored=set(); current=os.getpid()
for _index in range(64):
    if current<=0 or current in ignored: break
    ignored.add(current)
    try: lines=open(f"/proc/{current}/status","r",encoding="ascii").read().splitlines()
    except FileNotFoundError: break
    parents=[line for line in lines if line.startswith("PPid:")]
    if len(parents)!=1: raise SystemExit(71)
    current=int(parents[0].split()[1])
prefix=b"/var/tmp/kirby2-wo40h-"; owner=os.getuid(); matches=[]; scanned=0
for name in sorted(os.listdir("/proc")):
    if not name.isdigit(): continue
    pid=int(name)
    if pid in ignored: continue
    base=f"/proc/{pid}"
    try:
        status=open(base+"/status","r",encoding="ascii").read().splitlines()
        uids=[line for line in status if line.startswith("Uid:")]
        if len(uids)!=1 or int(uids[0].split()[1])!=owner: continue
        scanned+=1
        with open(base+"/cmdline","rb") as stream:
            raw=stream.read(1048577)
        if len(raw)>1048576: raise SystemExit(72)
        lowered=raw.lower()
        transport_session=(b"internal-sftp" in lowered or b"sftp-server" in lowered
                or (b"sshd:" in lowered and b"@notty" in lowered))
        if prefix in raw or transport_session: matches.append(pid)
    except (FileNotFoundError,ProcessLookupError):
        continue
if matches:
    print(json.dumps({"matching_pids":matches,"root":root,"scanned_process_count":scanned},sort_keys=True,separators=(",",":")))
    raise SystemExit(77)
print(json.dumps({"matching_pids":[],"root":root,"scanned_process_count":scanned},sort_keys=True,separators=(",",":")))'''

_ACQUIRE_PROVIDER_LOCK_SCRIPT: Final[str] = r'''import base64,hashlib,json,os,stat,sys
lock="/var/tmp/kirby2-wo40h-provider.lock"
marker_name=".kirby2-owner.json"
marker=base64.b64decode(sys.argv[1].encode("ascii"),validate=True)
if not marker or len(marker)>65536: raise SystemExit(70)
try:
    os.mkdir(lock,0o700)
except FileExistsError:
    raise SystemExit(75)
directory=os.open(lock,os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0))
try:
    metadata=os.fstat(directory)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=os.getuid() or stat.S_IMODE(metadata.st_mode)!=0o700: raise SystemExit(71)
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)
    marker_fd=os.open(marker_name,flags,0o400,dir_fd=directory)
    try:
        view=memoryview(marker)
        while view:
            count=os.write(marker_fd,view)
            if count<=0: raise OSError("short lock-marker write")
            view=view[count:]
        os.fsync(marker_fd)
        marker_metadata=os.fstat(marker_fd)
        if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_uid!=os.getuid() or marker_metadata.st_nlink!=1 or stat.S_IMODE(marker_metadata.st_mode)!=0o400: raise SystemExit(72)
    finally:
        os.close(marker_fd)
    verify=os.open(lock,os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0))
    try:
        verify_metadata=os.fstat(verify)
        if ((verify_metadata.st_dev,verify_metadata.st_ino)!=(metadata.st_dev,metadata.st_ino)
                or os.listdir(verify)!=[marker_name]): raise SystemExit(73)
    finally:
        os.close(verify)
    os.fsync(directory)
finally:
    os.close(directory)
parent=os.open("/var/tmp",os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0))
try: os.fsync(parent)
finally: os.close(parent)
payload={"lock":lock,"marker_sha256":hashlib.sha256(marker).hexdigest(),"mode":448,"uid":os.getuid()}
print(json.dumps(payload,sort_keys=True,separators=(",",":")))'''

_RELEASE_PROVIDER_LOCK_SCRIPT: Final[str] = r'''import hashlib,json,os,stat,sys
lock="/var/tmp/kirby2-wo40h-provider.lock"; name=os.path.basename(lock)
marker_name=".kirby2-owner.json"; expected=sys.argv[1]
if len(expected)!=64 or any(ch not in "0123456789abcdef" for ch in expected): raise SystemExit(70)
options=os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)
parent=os.open("/var/tmp",options); directory=None
try:
    directory=os.open(name,options,dir_fd=parent)
    metadata=os.fstat(directory)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=os.getuid() or stat.S_IMODE(metadata.st_mode)!=0o700: raise SystemExit(71)
    if os.listdir(directory)!=[marker_name]: raise SystemExit(72)
    flags=os.O_RDONLY|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)
    marker_fd=os.open(marker_name,flags,dir_fd=directory)
    try:
        before=os.fstat(marker_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1 or stat.S_IMODE(before.st_mode)!=0o400 or before.st_size<=0 or before.st_size>65536: raise SystemExit(73)
        raw=b""
        while len(raw)<before.st_size:
            chunk=os.read(marker_fd,min(65536,before.st_size-len(raw)))
            if not chunk: break
            raw+=chunk
        after=os.fstat(marker_fd)
        identity=lambda item:(item.st_dev,item.st_ino,item.st_mode,item.st_nlink,item.st_uid,item.st_gid,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
        if len(raw)!=before.st_size or identity(before)!=identity(after) or hashlib.sha256(raw).hexdigest()!=expected: raise SystemExit(74)
    finally:
        os.close(marker_fd)
    os.unlink(marker_name,dir_fd=directory); os.fsync(directory)
    current=os.stat(name,dir_fd=parent,follow_symlinks=False)
    if ((current.st_dev,current.st_ino)!=(metadata.st_dev,metadata.st_ino)
            or not stat.S_ISDIR(current.st_mode) or os.listdir(directory)): raise SystemExit(75)
    os.rmdir(name,dir_fd=parent); os.fsync(parent)
    try: os.stat(name,dir_fd=parent,follow_symlinks=False)
    except FileNotFoundError: pass
    else: raise SystemExit(76)
finally:
    if directory is not None: os.close(directory)
    os.close(parent)
print(json.dumps({"lock":lock,"released":True},sort_keys=True,separators=(",",":")))'''


@dataclass(frozen=True, slots=True)
class _LocalSshProvider:
    known_hosts: Path
    temporary_root: Path
    temporary_root_identity: tuple[int, ...]
    executable_projection_sha256: str
    host_key_fingerprint: str


@dataclass(slots=True)
class _LinuxFormState:
    form: str
    projection_root: Path
    projection_root_identity: tuple[int, ...]
    input_rows: tuple[dict[str, object], ...]
    remote_root: PurePosixPath
    marker_sha256: str
    root_maybe_created: bool = False
    root_deleted: bool = False
    provider_proofs: list[dict[str, object]] = field(default_factory=list)
    transfer_observations: list[dict[str, object]] = field(default_factory=list)
    worker_result: dict[str, object] | None = None


@dataclass(slots=True)
class _RemoteProviderLock:
    marker_sha256: str
    maybe_acquired: bool = False
    acquired: bool = False
    released: bool = False
    observations: list[dict[str, object]] = field(default_factory=list)


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
    )


def _open_ssh_identity(path: Path) -> tuple[int, ...]:
    """Validate credential metadata without reading or hashing private-key bytes."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("SSH identity path must be absolute")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform lacks no-follow credential support")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > 1024 * 1024
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("SSH identity ownership, mode, links, or size is unsafe")
        return _identity(metadata)
    finally:
        os.close(descriptor)


def _safe_local_executable(path: Path, label: str) -> str:
    if not path.is_absolute():
        raise ValueError(f"{label} executable path is not absolute")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform lacks no-follow executable support")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > 64 * 1024 * 1024
            or before.st_mode & 0o022
            or not before.st_mode & 0o111
        ):
            raise ValueError(f"{label} executable identity is unsafe")
        digest = hashlib.sha256()
        observed = 0
        while observed < before.st_size:
            chunk = os.read(descriptor, min(1024 * 1024, before.st_size - observed))
            if not chunk:
                break
            observed += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if observed != before.st_size or _identity(before) != _identity(after):
            raise ValueError(f"{label} executable changed during verification")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _host_key_fingerprint(public_key: str) -> str:
    try:
        raw = base64.b64decode(public_key.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as error:
        raise ValueError("SSH host public key is not strict base64") from error
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return "SHA256:" + digest


def _prepare_local_ssh_provider() -> _LocalSshProvider:
    try:
        ssh_sha256 = _safe_local_executable(SSH_EXECUTABLE_V1, "SSH")
        sftp_sha256 = _safe_local_executable(SFTP_EXECUTABLE_V1, "SFTP")
        credential_identity = _open_ssh_identity(SSH_IDENTITY_FILE_V1)
    except (OSError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
            "fixed local SSH client or existing identity is unavailable",
        ) from error
    fingerprint = _host_key_fingerprint(SSH_HOST_KEY_PUBLIC_V1)
    if fingerprint != SSH_HOST_KEY_FINGERPRINT_V1:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "pinned Fedora ED25519 host-key bytes and fingerprint differ",
        )
    root = Path(tempfile.mkdtemp(prefix="kirby2-wo40h-ssh-")).resolve()
    os.chmod(root, 0o700)
    known_hosts = root / "known_hosts"
    raw = (
        f"{SSH_HOST_KEY_ALIAS_V1} {SSH_HOST_KEY_ALGORITHM_V1} "
        f"{SSH_HOST_KEY_PUBLIC_V1}\n"
    ).encode("ascii")
    try:
        _write_private(known_hosts, raw, mode=0o600)
        _fsync_directory(root)
        snapshot = _stable_read(
            known_hosts,
            maximum_bytes=16 * 1024,
            require_read_only=False,
        )
        if snapshot.raw != raw or stat.S_IMODE(snapshot.identity[2]) != 0o600:
            raise ValueError("dedicated known-hosts file differs after publication")
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    projection = {
        "credential_identity": list(credential_identity),
        "host": SSH_HOST_V1,
        "host_key_fingerprint": fingerprint,
        "port": SSH_PORT_V1,
        "sftp_executable_sha256": sftp_sha256,
        "ssh_executable_sha256": ssh_sha256,
        "user": SSH_USER_V1,
    }
    return _LocalSshProvider(
        known_hosts=known_hosts,
        temporary_root=root,
        temporary_root_identity=_directory_identity(os.lstat(root)),
        executable_projection_sha256=_sha256(canonical_json_bytes(projection)),
        host_key_fingerprint=fingerprint,
    )


def _ssh_environment() -> dict[str, str]:
    account = pwd.getpwuid(os.getuid())
    home = Path(account.pw_dir).resolve(strict=True)
    if home != Path.home().resolve(strict=True):
        raise ValueError("SSH controller account home differs from the active account")
    return {
        "HOME": os.fspath(home),
        "LANG": "C",
        "LC_ALL": "C",
        "LOGNAME": account.pw_name,
        "PATH": "/usr/bin:/bin",
        "TMPDIR": tempfile.gettempdir(),
        "USER": account.pw_name,
    }


def _ssh_common_options(provider: _LocalSshProvider) -> tuple[str, ...]:
    return (
        "-F",
        "/dev/null",
        "-i",
        os.fspath(SSH_IDENTITY_FILE_V1),
        "-o",
        "BatchMode=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "UseKeychain=yes",
        "-o",
        "AddKeysToAgent=no",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "HostbasedAuthentication=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "CheckHostIP=no",
        "-o",
        "VerifyHostKeyDNS=no",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        f"UserKnownHostsFile={provider.known_hosts}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        f"HostKeyAlias={SSH_HOST_KEY_ALIAS_V1}",
        "-o",
        f"HostKeyAlgorithms={SSH_HOST_KEY_ALGORITHM_V1}",
        "-o",
        f"PubkeyAcceptedAlgorithms={SSH_HOST_KEY_ALGORITHM_V1}",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "RequestTTY=no",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "CanonicalizeHostname=no",
        "-o",
        "GSSAPIAuthentication=no",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        "-o",
        "LogLevel=ERROR",
    )


def _ssh_argv(
    provider: _LocalSshProvider,
    remote_argv: Sequence[str],
) -> tuple[str, ...]:
    if (
        not remote_argv
        or any(type(item) is not str or not item or "\x00" in item for item in remote_argv)
    ):
        raise TypeError("remote command requires nonempty NUL-free argv")
    command = shlex.join(tuple(remote_argv))
    return (
        os.fspath(SSH_EXECUTABLE_V1),
        *_ssh_common_options(provider),
        "-T",
        "-p",
        str(SSH_PORT_V1),
        f"{SSH_USER_V1}@{SSH_HOST_V1}",
        command,
    )


def _sftp_argv(
    provider: _LocalSshProvider,
    batch: Path,
) -> tuple[str, ...]:
    return (
        os.fspath(SFTP_EXECUTABLE_V1),
        *_ssh_common_options(provider),
        "-q",
        "-b",
        os.fspath(batch),
        "-P",
        str(SSH_PORT_V1),
        f"{SSH_USER_V1}@{SSH_HOST_V1}",
    )


def _ssh_exec(
    provider: _LocalSshProvider,
    remote_argv: Sequence[str],
    *,
    timeout_seconds: int = _HOST_COMMAND_TIMEOUT_SECONDS,
    maximum_output_bytes: int = _COMMAND_OUTPUT_MAX_BYTES,
) -> _CommandResult:
    return _run_command(
        _ssh_argv(provider, remote_argv),
        environment=_ssh_environment(),
        timeout_seconds=timeout_seconds,
        maximum_output_bytes=maximum_output_bytes,
    )


def _parse_canonical_object_line(raw: bytes, label: str) -> dict[str, object]:
    if (
        type(raw) is not bytes
        or not raw.endswith(b"\n")
        or raw.count(b"\n") != 1
        or not raw[:-1]
    ):
        raise ValueError(f"{label} is not one nonempty line")
    value = load_canonical_json_bytes(raw[:-1], label)
    if type(value) is not dict:
        raise ValueError(f"{label} is not an object")
    return dict(value)


def _parse_provider_probe(raw: bytes) -> dict[str, object]:
    row = _parse_canonical_object_line(raw, "Linux provider probe")
    fields = {
        "available_disk_bytes",
        "base_exec_prefix",
        "base_prefix",
        "boot_id_sha256",
        "cpu_count",
        "gid",
        "home",
        "kernel_release",
        "machine",
        "machine_model",
        "memory_bytes",
        "os_version",
        "python_executable",
        "python_executable_realpath",
        "python_implementation",
        "python_version",
        "rmtree_symlink_safe",
        "source_checkout_present",
        "system",
        "uid",
    }
    if set(row) != fields:
        raise ValueError("Linux provider probe fields differ")
    for name in ("available_disk_bytes", "cpu_count", "gid", "memory_bytes", "uid"):
        if type(row[name]) is not int or int(row[name]) < 0:
            raise ValueError(f"Linux provider {name} is invalid")
    for name in (
        "base_exec_prefix",
        "base_prefix",
        "boot_id_sha256",
        "home",
        "kernel_release",
        "machine",
        "machine_model",
        "os_version",
        "python_executable",
        "python_executable_realpath",
        "python_implementation",
        "python_version",
        "system",
    ):
        if type(row[name]) is not str or not row[name] or "\x00" in str(row[name]):
            raise ValueError(f"Linux provider {name} is invalid")
    if (
        row["system"] != "Linux"
        or row["machine"] != "x86_64"
        or row["python_implementation"] != "CPython"
        or not str(row["python_version"]).startswith("3.14.")
        or row["python_executable_realpath"] not in _REMOTE_PYTHON_CANDIDATES
        or row["uid"] == 0
        or row["cpu_count"] < 2
        or row["memory_bytes"] < _MINIMUM_REMOTE_MEMORY_BYTES
        or row["available_disk_bytes"] < _MINIMUM_REMOTE_FREE_BYTES
        or row["source_checkout_present"] is not False
        or row["rmtree_symlink_safe"] is not True
        or row["machine_model"] == "UNAVAILABLE"
        or row["os_version"] == "UNAVAILABLE"
        or any(
            not str(row[name]).startswith("/")
            or ".." in PurePosixPath(str(row[name])).parts
            for name in ("base_exec_prefix", "base_prefix")
        )
        or _REMOTE_HOME.fullmatch(str(row["home"])) is None
        or _SHA256.fullmatch(str(row["boot_id_sha256"])) is None
    ):
        raise ValueError("Linux provider capability or clean-state proof differs")
    return row


def _parse_network_probe(raw: bytes) -> dict[str, object]:
    row = _parse_canonical_object_line(raw, "Linux network-namespace probe")
    if set(row) != {
        "default_ipv4_route_count",
        "default_ipv6_route_count",
        "interfaces",
        "no_new_privileges",
        "test_net_connect_result",
        "uid",
        "uid_map_sha256",
    }:
        raise ValueError("Linux network-namespace probe fields differ")
    if (
        type(row["interfaces"]) is not list
        or row["interfaces"] != ["lo"]
        or row["no_new_privileges"] is not True
        or type(row["default_ipv4_route_count"]) is not int
        or type(row["default_ipv6_route_count"]) is not int
        or row["default_ipv4_route_count"] != 0
        or row["default_ipv6_route_count"] != 0
        or type(row["test_net_connect_result"]) is not int
        or row["test_net_connect_result"] == 0
        or row["uid"] != 0
        or type(row["uid_map_sha256"]) is not str
        or _SHA256.fullmatch(str(row["uid_map_sha256"])) is None
    ):
        raise ValueError("Linux network namespace retains an external route or interface")
    return row


def _remote_root(candidate_commit: str, nonce: str) -> PurePosixPath:
    if (
        type(candidate_commit) is not str
        or _COMMIT.fullmatch(candidate_commit) is None
        or type(nonce) is not str
        or _NONCE.fullmatch(nonce) is None
    ):
        raise ValueError("Linux qualification root identity is invalid")
    return _require_remote_root(
        PurePosixPath(
            f"/var/tmp/kirby2-wo40h-{candidate_commit[:12]}-{nonce}"
        )
    )


def _require_remote_root(path: PurePosixPath) -> PurePosixPath:
    if (
        not isinstance(path, PurePosixPath)
        or not path.is_absolute()
        or _REMOTE_ROOT.fullmatch(path.as_posix()) is None
        or ".." in path.parts
    ):
        raise ValueError("remote cleanup root escaped the WO40-H ownership grammar")
    return path


def _remote_paths(root: PurePosixPath, form: str) -> dict[str, PurePosixPath]:
    selected = _require_remote_root(root)
    if form not in _FORMS:
        raise ValueError("Linux qualification form is invalid")
    unpacked = selected / "unpacked"
    bundle_root = unpacked / _BUNDLE_ROOTS[form]
    return {
        "root": selected,
        "home": selected / "home",
        "inputs": selected / "inputs",
        "tmp": selected / "tmp",
        "unpacked": unpacked,
        "bundle_root": bundle_root,
        "wheelhouse": bundle_root / "wheelhouse",
        "venv": selected / "venv",
        "worker_attempt": selected / "worker-attempt",
    }


def _remote_environment_argv(
    *,
    home: str,
    temporary: PurePosixPath,
    lifetime_root: PurePosixPath | None = None,
) -> tuple[str, ...]:
    owned_home = bool(
        _REMOTE_ROOT.fullmatch(temporary.parent.as_posix())
        and temporary.name == "tmp"
        and home == (temporary.parent / "home").as_posix()
    )
    if (
        (_REMOTE_HOME.fullmatch(home) is None and not owned_home and home != "/var/tmp")
        or not temporary.is_absolute()
    ):
        raise ValueError("remote environment path is invalid")
    lifetime = (
        ()
        if lifetime_root is None
        else (
            "KIRBY2_QUALIFICATION_LIFETIME_ROOT="
            + _require_remote_root(lifetime_root).as_posix(),
        )
    )
    return (
        "/usr/bin/env",
        "-i",
        f"HOME={home}",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
        "PATH=/usr/bin:/bin",
        "PIP_DISABLE_PIP_VERSION_CHECK=1",
        "PIP_NO_CACHE_DIR=1",
        "PIP_NO_INDEX=1",
        "PIP_NO_INPUT=1",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONNOUSERSITE=1",
        f"TMPDIR={temporary}",
        "TZ=UTC",
        *lifetime,
    )


def _unshared_argv(
    *,
    home: str,
    temporary: PurePosixPath,
    command: Sequence[str],
    lifetime_seconds: int,
    lifetime_root: PurePosixPath | None = None,
) -> tuple[str, ...]:
    if (
        not command
        or any(type(item) is not str or not item for item in command)
        or type(lifetime_seconds) is not int
        or not 1 <= lifetime_seconds <= _WORKER_TIMEOUT_SECONDS
    ):
        raise TypeError("network-disabled command requires fixed argv")
    return (
        "/usr/bin/timeout",
        "--foreground",
        "--signal=TERM",
        "--kill-after=5s",
        f"{lifetime_seconds}s",
        *_UNSHARE_PREFIX_V1,
        *_NO_NEW_PRIVILEGES_PREFIX_V1,
        *_remote_environment_argv(
            home=home,
            temporary=temporary,
            lifetime_root=lifetime_root,
        ),
        *command,
    )


def _remote_cleanup_argv(
    root: PurePosixPath,
    marker_sha256: str,
    python_path: str,
) -> tuple[str, ...]:
    selected = _require_remote_root(root)
    if _SHA256.fullmatch(marker_sha256) is None:
        raise ValueError("remote cleanup marker digest is invalid")
    if python_path not in _REMOTE_PYTHON_CANDIDATES:
        raise ValueError("remote cleanup Python path is invalid")
    return (
        python_path,
        "-I",
        "-c",
        _CLEANUP_SCRIPT,
        selected.as_posix(),
        marker_sha256,
    )


def _utc_second() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_projection(
    *,
    form: str,
    artifact_root: Path,
    build_evidence: Path,
    index: ReleaseArtifactIndexV1,
    build_record: ReleaseArtifactBuildRecordV1,
) -> tuple[Path, tuple[dict[str, object], ...], tuple[int, ...]]:
    """Build the exact, immutable SFTP input set for one product form."""

    if form not in _FORMS:
        raise ValueError("Linux qualification projection form is invalid")
    selected = index.select(_ARTIFACT_SELECTORS[form])
    projection = Path(
        tempfile.mkdtemp(prefix=f"kirby2-wo40h-{form}-input-")
    ).resolve()
    os.chmod(projection, 0o700)
    rows: list[dict[str, object]] = []
    try:
        for artifact in selected:
            if _SAFE_INPUT_NAME.fullmatch(artifact.artifact_id) is None:
                raise ValueError("selected artifact name is unsafe for SFTP")
            copied = _copy_projection_file(
                artifact_root / artifact.artifact_id,
                projection / artifact.artifact_id,
                maximum_bytes=RELEASE_ARTIFACT_MAX_BYTES_V1,
                require_read_only=True,
            )
            if (
                copied["size"] != artifact.size
                or copied["sha256"] != artifact.transport_sha256
            ):
                raise ValueError(
                    "selected Linux qualification artifact differs from its index"
                )
            rows.append({"artifact_id": artifact.artifact_id, **copied})

        for artifact_id, filename, source, maximum, digest in (
            (
                "release-artifact-index",
                RELEASE_ARTIFACT_INDEX_FILENAME_V1,
                artifact_root / RELEASE_ARTIFACT_INDEX_FILENAME_V1,
                RELEASE_RECORD_MAX_BYTES_V1,
                index.sha256,
            ),
            (
                "release-build-record",
                RELEASE_BUILD_RECORD_FILENAME_V1,
                artifact_root / RELEASE_BUILD_RECORD_FILENAME_V1,
                RELEASE_RECORD_MAX_BYTES_V1,
                build_record.sha256,
            ),
        ):
            copied = _copy_projection_file(
                source,
                projection / filename,
                maximum_bytes=maximum,
                require_read_only=True,
            )
            if copied["sha256"] != digest:
                raise ValueError(f"Linux qualification {filename} copy differs")
            rows.append({"artifact_id": artifact_id, **copied})

        evidence = _copy_projection_file(
            build_evidence,
            projection / "KIRBY2_RELEASE_BUILD_EVIDENCE.md",
            maximum_bytes=_BUILD_EVIDENCE_MAX_BYTES,
            require_read_only=False,
        )
        rows.append({"artifact_id": "release-build-evidence", **evidence})

        request = canonical_json_bytes(
            {
                "artifact_index_sha256": index.sha256,
                "candidate_commit": index.candidate_commit,
                "form": form,
                "inputs": [
                    {
                        "artifact_id": row["artifact_id"],
                        "name": row["name"],
                        "sha256": row["sha256"],
                        "size": row["size"],
                    }
                    for row in rows
                ],
                "logical_build_id": index.logical_build_id,
                "network_scope": "GUEST_NETWORK_DISABLED_VERIFIED",
                "policy_id": LINUX_PROVIDER_POLICY_ID_V1,
                "protocol_set_sha256": build_record.protocol_set_sha256,
                "schema_id": "KIRBY2_SSH_QUALIFICATION_INPUT_V1",
                "schema_version": 1,
                "target_id": LINUX_TARGET_ID_V1,
            }
        )
        _write_private(
            projection / "qualification-request.json",
            request,
            mode=0o444,
        )
        rows.append(
            {
                "artifact_id": "qualification-request",
                "name": "qualification-request.json",
                "sha256": _sha256(request),
                "size": len(request),
                "source_identity": [],
            }
        )
        names = tuple(row.get("name") for row in rows)
        if (
            len(names) != len(set(names))
            or any(
                type(name) is not str or _SAFE_INPUT_NAME.fullmatch(name) is None
                for name in names
            )
            or set(os.listdir(projection)) != set(names)
        ):
            raise ValueError("Linux qualification projection inventory differs")
        _fsync_directory(projection)
        return projection, tuple(rows), _directory_identity(os.lstat(projection))
    except Exception:
        shutil.rmtree(projection, ignore_errors=True)
        raise


def _remove_local_tree(
    path: Path,
    *,
    prefix: str,
    expected_identity: tuple[int, ...],
) -> str | None:
    """Remove only a controller-created directory beneath the local temp root."""

    try:
        temporary = Path(tempfile.gettempdir()).resolve(strict=True)
        if not path.is_absolute() or path.parent != temporary:
            raise ValueError("local cleanup target left the exact temporary root")
        metadata = os.lstat(path)
        if (
            not path.name.startswith(prefix)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or _directory_identity(metadata) != expected_identity
            or not getattr(shutil.rmtree, "avoids_symlink_attacks", False)
        ):
            raise ValueError("local cleanup target escaped its ownership grammar")
        shutil.rmtree(path)
        if os.path.lexists(path):
            raise ValueError("local cleanup target remained after deletion")
    except FileNotFoundError:
        return None
    except Exception as error:
        return f"local cleanup failed: {type(error).__name__}"
    return None


def _require_remote_success(
    result: _CommandResult,
    label: str,
    *,
    terminal: bool = False,
) -> _CommandResult:
    if result.returncode != 0:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            f"Linux provider {label} failed with exit status {result.returncode}",
            terminal=terminal,
        )
    return result


def _probe_remote_python(
    provider: _LocalSshProvider,
) -> tuple[str, dict[str, object], dict[str, object]]:
    observations: list[dict[str, object]] = []
    for candidate in _REMOTE_PYTHON_CANDIDATES:
        result = _ssh_exec(
            provider,
            (candidate, "-I", "-c", _PROVIDER_PROBE_SCRIPT),
        )
        observations.append(
            {"candidate": candidate, "command": result.observation()}
        )
        if result.returncode != 0:
            continue
        try:
            probe = _parse_provider_probe(result.stdout)
        except (TypeError, ValueError):
            continue
        if probe["python_executable_realpath"] == candidate:
            return candidate, probe, {"candidate_probes": observations}
    raise _QualificationRefused(
        QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
        "fixed Fedora provider lacks its closed CPython 3.14 runtime",
    )


def _prove_remote_provider(
    provider: _LocalSshProvider,
    *,
    python_path: str,
    phase: str,
    lifetime_root: PurePosixPath | None = None,
) -> dict[str, object]:
    if phase not in {
        "BEFORE_INSTALL",
        "AFTER_INSTALL",
        "AFTER_WORKER",
        "AFTER_CLEANUP",
    }:
        raise ValueError("Linux provider proof phase is invalid")
    terminal = phase != "BEFORE_INSTALL"
    probe_result = _require_remote_success(
        _ssh_exec(
            provider,
            (python_path, "-I", "-c", _PROVIDER_PROBE_SCRIPT),
        ),
        "identity proof",
        terminal=terminal,
    )
    try:
        identity = _parse_provider_probe(probe_result.stdout)
    except (TypeError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "Linux provider returned an invalid identity proof",
            terminal=terminal,
        ) from error
    if identity["python_executable_realpath"] != python_path:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "Linux provider Python identity changed",
            terminal=terminal,
        )

    sudo_version = _require_remote_success(
        _ssh_exec(
            provider,
            ("/usr/bin/sudo", "-V"),
            timeout_seconds=30,
            maximum_output_bytes=1024 * 1024,
        ),
        "sudo executable proof",
        terminal=terminal,
    )
    true_control = _require_remote_success(
        _ssh_exec(
            provider,
            ("/usr/bin/true",),
            timeout_seconds=30,
            maximum_output_bytes=1024 * 1024,
        ),
        "true control command",
        terminal=terminal,
    )
    sudo_listing = _ssh_exec(
        provider,
        (
            "/usr/bin/env",
            "-i",
            "LANG=C",
            "LC_ALL=C",
            "PATH=/usr/bin:/bin",
            "/usr/bin/sudo",
            "-n",
            "-l",
        ),
        timeout_seconds=30,
        maximum_output_bytes=1024 * 1024,
    )
    if sudo_listing.returncode == 0:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_NOT_CLEAN,
            "Linux provider exposes a noninteractive sudo capability listing",
            terminal=terminal,
        )
    if sudo_listing.returncode != 1:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "Linux provider sudo listing did not use the exact denial status 1",
            terminal=terminal,
        )

    sudo = _ssh_exec(
        provider,
        ("/usr/bin/sudo", "-n", "/usr/bin/true"),
        timeout_seconds=30,
        maximum_output_bytes=1024 * 1024,
    )
    if sudo.returncode == 0:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_NOT_CLEAN,
            "Linux provider unexpectedly grants passwordless root authority",
            terminal=terminal,
        )
    if sudo.returncode != 1:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "Linux provider sudo refusal did not use the exact exit status 1",
            terminal=terminal,
        )

    network_result = _require_remote_success(
        _ssh_exec(
            provider,
            _unshared_argv(
                home="/var/tmp",
                temporary=PurePosixPath("/var/tmp"),
                command=(python_path, "-I", "-c", _NETWORK_PROBE_SCRIPT),
                lifetime_seconds=30,
                lifetime_root=lifetime_root,
            ),
            timeout_seconds=60,
            maximum_output_bytes=1024 * 1024,
        ),
        "network-namespace proof",
        terminal=terminal,
    )
    try:
        network = _parse_network_probe(network_result.stdout)
    except (TypeError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_ISOLATION_UNAVAILABLE,
            "Linux provider network namespace retained external reachability",
            terminal=terminal,
        ) from error

    return {
        **identity,
        "command_observations": {
            "identity": probe_result.observation(),
            "network_namespace": network_result.observation(),
            "passwordless_sudo_refusal": sudo.observation(),
            "sudo_capability_listing_refusal": sudo_listing.observation(),
            "sudo_executable": sudo_version.observation(),
            "true_control": true_control.observation(),
        },
        "host_key_fingerprint": provider.host_key_fingerprint,
        "local_ssh_projection_sha256": provider.executable_projection_sha256,
        "network": network,
        "network_scope": "GUEST_NETWORK_DISABLED_VERIFIED",
        "phase": phase,
        "provider_policy_id": LINUX_PROVIDER_POLICY_ID_V1,
    }


def _marker_bytes(
    *,
    candidate_commit: str,
    form: str,
    root: PurePosixPath,
) -> bytes:
    if _COMMIT.fullmatch(candidate_commit) is None or form not in _FORMS:
        raise ValueError("remote ownership marker identity is invalid")
    selected = _require_remote_root(root)
    return canonical_json_bytes(
        {
            "candidate_commit": candidate_commit,
            "form": form,
            "policy_id": LINUX_PROVIDER_POLICY_ID_V1,
            "root": selected.as_posix(),
            "schema_id": "KIRBY2_SSH_EPHEMERAL_ROOT_OWNER_V1",
            "schema_version": 1,
        }
    )


def _provider_lock_marker_bytes(
    *,
    candidate_commit: str,
    nonce: str,
) -> bytes:
    if (
        _COMMIT.fullmatch(candidate_commit) is None
        or _NONCE.fullmatch(nonce) is None
    ):
        raise ValueError("remote provider-lock identity is invalid")
    return canonical_json_bytes(
        {
            "candidate_commit": candidate_commit,
            "host": SSH_HOST_V1,
            "lock": _REMOTE_PROVIDER_LOCK_V1.as_posix(),
            "nonce": nonce,
            "policy_id": LINUX_PROVIDER_POLICY_ID_V1,
            "port": SSH_PORT_V1,
            "schema_id": "KIRBY2_SSH_PROVIDER_LOCK_OWNER_V1",
            "schema_version": 1,
            "user": SSH_USER_V1,
        }
    )


def _acquire_remote_provider_lock(
    provider: _LocalSshProvider,
    lock: _RemoteProviderLock,
    *,
    python_path: str,
    marker: bytes,
) -> None:
    if (
        python_path not in _REMOTE_PYTHON_CANDIDATES
        or _sha256(marker) != lock.marker_sha256
        or lock.maybe_acquired
        or lock.acquired
        or lock.released
    ):
        raise ValueError("remote provider-lock acquisition state is invalid")
    lock.maybe_acquired = True
    result = _ssh_exec(
        provider,
        (
            python_path,
            "-I",
            "-c",
            _ACQUIRE_PROVIDER_LOCK_SCRIPT,
            base64.b64encode(marker).decode("ascii"),
        ),
        timeout_seconds=60,
        maximum_output_bytes=1024 * 1024,
    )
    if result.returncode == 75 and result.stdout == b"" and result.stderr == b"":
        # Atomic mkdir proved that another owner already holds the fixed lock.
        # Never inspect, age, replace, or recover that owner's marker.
        lock.maybe_acquired = False
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
            "fixed Fedora qualification provider is locked by another controller",
        )
    if result.returncode != 0:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_CLEANUP_FAILED,
            "remote provider-lock acquisition was ambiguous",
            terminal=True,
        )
    try:
        payload = _parse_canonical_object_line(
            result.stdout,
            "remote provider-lock acquisition",
        )
    except (TypeError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_CLEANUP_FAILED,
            "remote provider-lock acquisition receipt is invalid",
            terminal=True,
        ) from error
    if (
        set(payload) != {"lock", "marker_sha256", "mode", "uid"}
        or payload.get("lock") != _REMOTE_PROVIDER_LOCK_V1.as_posix()
        or payload.get("marker_sha256") != lock.marker_sha256
        or payload.get("mode") != 0o700
        or type(payload.get("uid")) is not int
        or payload["uid"] == 0
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_CLEANUP_FAILED,
            "remote provider-lock acquisition receipt differs",
            terminal=True,
        )
    lock.acquired = True
    lock.observations.append(
        {
            "command": result.observation(),
            "phase": "ACQUIRE_PROVIDER_LOCK",
            "receipt": payload,
        }
    )


def _release_remote_provider_lock(
    provider: _LocalSshProvider,
    lock: _RemoteProviderLock,
    *,
    python_path: str,
) -> str | None:
    if not lock.maybe_acquired or lock.released:
        return None
    try:
        if python_path not in _REMOTE_PYTHON_CANDIDATES:
            raise ValueError("provider-lock Python path differs")
        result = _ssh_exec(
            provider,
            (
                python_path,
                "-I",
                "-c",
                _RELEASE_PROVIDER_LOCK_SCRIPT,
                lock.marker_sha256,
            ),
            timeout_seconds=60,
            maximum_output_bytes=1024 * 1024,
        )
        if result.returncode != 0:
            raise ValueError(f"provider-lock release exited {result.returncode}")
        payload = _parse_canonical_object_line(
            result.stdout,
            "remote provider-lock release",
        )
        if payload != {
            "lock": _REMOTE_PROVIDER_LOCK_V1.as_posix(),
            "released": True,
        }:
            raise ValueError("provider-lock release receipt differs")
        lock.released = True
        lock.observations.append(
            {
                "command": result.observation(),
                "phase": "RELEASE_PROVIDER_LOCK",
                "receipt": payload,
            }
        )
    except Exception as error:
        return f"remote provider-lock cleanup failed: {type(error).__name__}"
    return None


def _create_remote_root(
    provider: _LocalSshProvider,
    state: _LinuxFormState,
    *,
    python_path: str,
    marker: bytes,
) -> None:
    if _sha256(marker) != state.marker_sha256:
        raise ValueError("remote root marker and state digest differ")
    state.root_maybe_created = True
    result = _require_remote_success(
        _ssh_exec(
            provider,
            (
                python_path,
                "-I",
                "-c",
                _CREATE_ROOT_SCRIPT,
                state.remote_root.as_posix(),
                base64.b64encode(marker).decode("ascii"),
            ),
        ),
        "owned-root creation",
        terminal=True,
    )
    try:
        payload = _parse_canonical_object_line(result.stdout, "remote root creation")
    except (TypeError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            "Linux provider root-creation receipt is invalid",
            terminal=True,
        ) from error
    if (
        set(payload) != {"marker_sha256", "mode", "root", "uid"}
        or payload["root"] != state.remote_root.as_posix()
        or payload["marker_sha256"] != state.marker_sha256
        or payload["mode"] != 0o700
        or type(payload["uid"]) is not int
        or payload["uid"] == 0
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            "Linux provider root-creation receipt differs",
            terminal=True,
        )
    state.transfer_observations.append(
        {"command": result.observation(), "phase": "CREATE_ROOT", "receipt": payload}
    )


def _sftp_quote(path: str) -> str:
    if (
        type(path) is not str
        or _SAFE_TRANSFER_PATH.fullmatch(path) is None
        or ".." in PurePosixPath(path).parts
    ):
        raise ValueError("SFTP path is outside the closed transfer grammar")
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sftp_put(
    provider: _LocalSshProvider,
    state: _LinuxFormState,
) -> None:
    lines: list[str] = []
    expected: set[str] = set()
    for row in state.input_rows:
        name = row.get("name")
        digest = row.get("sha256")
        size = row.get("size")
        if (
            type(name) is not str
            or _SAFE_INPUT_NAME.fullmatch(name) is None
            or type(digest) is not str
            or _SHA256.fullmatch(digest) is None
            or type(size) is not int
            or size <= 0
            or name in expected
        ):
            raise ValueError("Linux SFTP input row is malformed")
        expected.add(name)
        local = (state.projection_root / name).resolve(strict=True)
        local.relative_to(state.projection_root)
        remote = state.remote_root / "inputs" / f"{name}.part"
        lines.append(
            f"put {_sftp_quote(os.fspath(local))} {_sftp_quote(remote.as_posix())}"
        )
    if expected != set(os.listdir(state.projection_root)):
        raise ValueError("Linux SFTP projection changed before transfer")

    batch = provider.temporary_root / f"{state.form}.sftp"
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    try:
        _write_private(batch, raw, mode=0o600)
        result = _run_command(
            _sftp_argv(provider, batch),
            environment=_ssh_environment(),
            timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS,
            maximum_output_bytes=_COMMAND_OUTPUT_MAX_BYTES,
        )
        _require_remote_success(result, "exact SFTP transfer", terminal=True)
        state.transfer_observations.append(
            {
                "command": result.observation(),
                "input_count": len(lines),
                "phase": "SFTP_TRANSFER",
            }
        )
    finally:
        try:
            batch.unlink(missing_ok=True)
            _fsync_directory(provider.temporary_root)
        except OSError:
            pass


def _finalize_remote_inputs(
    provider: _LocalSshProvider,
    state: _LinuxFormState,
    *,
    python_path: str,
) -> None:
    expected = [
        {"name": row["name"], "sha256": row["sha256"], "size": row["size"]}
        for row in state.input_rows
    ]
    encoded = base64.b64encode(canonical_json_bytes(expected)).decode("ascii")
    result = _require_remote_success(
        _ssh_exec(
            provider,
            (
                python_path,
                "-I",
                "-c",
                _FINALIZE_INPUTS_SCRIPT,
                state.remote_root.as_posix(),
                encoded,
            ),
        ),
        "input hash/freeze",
        terminal=True,
    )
    try:
        payload = _parse_canonical_object_line(result.stdout, "remote input receipt")
    except (TypeError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            "Linux provider input receipt is invalid",
            terminal=True,
        ) from error
    if set(payload) != {"inputs"} or payload["inputs"] != expected:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            "Linux provider input receipt differs from the release store",
            terminal=True,
        )
    state.transfer_observations.append(
        {"command": result.observation(), "phase": "FREEZE_INPUTS", "receipt": payload}
    )


def _prove_remote_process_absence(
    provider: _LocalSshProvider,
    root: PurePosixPath,
    *,
    python_path: str,
) -> tuple[_CommandResult, dict[str, object]]:
    selected = _require_remote_root(root)
    absence = _ssh_exec(
        provider,
        (
            python_path,
            "-I",
            "-c",
            _PROCESS_ABSENCE_SCRIPT,
            selected.as_posix(),
        ),
        timeout_seconds=60,
        maximum_output_bytes=1024 * 1024,
    )
    if absence.returncode != 0:
        raise ValueError(f"process-absence proof exited {absence.returncode}")
    payload = _parse_canonical_object_line(
        absence.stdout,
        "remote process-absence proof",
    )
    if (
        set(payload) != {"matching_pids", "root", "scanned_process_count"}
        or payload.get("matching_pids") != []
        or payload.get("root") != selected.as_posix()
        or type(payload.get("scanned_process_count")) is not int
        or int(payload["scanned_process_count"]) < 0
    ):
        raise ValueError("remote process-absence proof differs")
    return absence, payload


def _cleanup_remote_root(
    provider: _LocalSshProvider,
    state: _LinuxFormState,
    *,
    python_path: str,
) -> str | None:
    if not state.root_maybe_created or state.root_deleted:
        return None
    try:
        absence, absence_payload = _prove_remote_process_absence(
            provider,
            state.remote_root,
            python_path=python_path,
        )
        state.transfer_observations.append(
            {
                "command": absence.observation(),
                "phase": "PROVE_PROCESS_ABSENCE",
                "receipt": absence_payload,
            }
        )
        result = _ssh_exec(
            provider,
            _remote_cleanup_argv(
                state.remote_root,
                state.marker_sha256,
                python_path,
            ),
        )
        if result.returncode != 0:
            missing = _ssh_exec(
                provider,
                (
                    "/usr/bin/test",
                    "!",
                    "-e",
                    state.remote_root.as_posix(),
                ),
                timeout_seconds=30,
                maximum_output_bytes=1024 * 1024,
            )
            if missing.returncode != 0:
                raise ValueError(f"cleanup exited {result.returncode}")
            state.root_deleted = True
            state.transfer_observations.append(
                {
                    "command": missing.observation(),
                    "phase": "ROOT_ALREADY_ABSENT",
                    "receipt": {
                        "deleted": True,
                        "root": state.remote_root.as_posix(),
                    },
                }
            )
            return None
        payload = _parse_canonical_object_line(result.stdout, "remote cleanup receipt")
        if payload != {"deleted": True, "root": state.remote_root.as_posix()}:
            raise ValueError("cleanup receipt differs")
        state.root_deleted = True
        state.transfer_observations.append(
            {"command": result.observation(), "phase": "DELETE_ROOT", "receipt": payload}
        )
    except Exception as error:
        return f"{state.form} remote cleanup failed: {type(error).__name__}"
    return None


def _isolated_remote_exec(
    provider: _LocalSshProvider,
    *,
    home: str,
    temporary: PurePosixPath,
    command: Sequence[str],
    timeout_seconds: int = _HOST_COMMAND_TIMEOUT_SECONDS,
    maximum_output_bytes: int = _COMMAND_OUTPUT_MAX_BYTES,
) -> _CommandResult:
    return _ssh_exec(
        provider,
        _unshared_argv(
            home=home,
            temporary=temporary,
            command=command,
            lifetime_seconds=max(1, timeout_seconds - 30),
            lifetime_root=_require_remote_root(temporary.parent),
        ),
        timeout_seconds=timeout_seconds,
        maximum_output_bytes=maximum_output_bytes,
    )


def _install_form(
    provider: _LocalSshProvider,
    state: _LinuxFormState,
    *,
    python_path: str,
    provider_proof: Mapping[str, object],
    bundle: ReleaseProtocolBundleV1,
) -> tuple[str, str, tuple[dict[str, object], ...]]:
    paths = _remote_paths(state.remote_root, state.form)
    provider_home = provider_proof.get("home")
    if type(provider_home) is not str or _REMOTE_HOME.fullmatch(provider_home) is None:
        raise RuntimeError("Linux provider proof omitted its fixed account home")
    home = paths["home"].as_posix()
    commands: list[dict[str, object]] = []

    def execute(
        label: str,
        command: Sequence[str],
        *,
        timeout_seconds: int = _HOST_COMMAND_TIMEOUT_SECONDS,
    ) -> _CommandResult:
        result = _require_remote_success(
            _isolated_remote_exec(
                provider,
                home=home,
                temporary=paths["tmp"],
                command=command,
                timeout_seconds=timeout_seconds,
            ),
            label,
            terminal=True,
        )
        commands.append({"label": label, **result.observation()})
        return result

    execute(
        "verify isolated runtime root is absent",
        ("/usr/bin/test", "!", "-e", paths["venv"].as_posix()),
    )
    bundle_artifact = _BUNDLE_ARTIFACTS[state.form]
    execute(
        "extract verified release bundle",
        (
            "/usr/bin/tar",
            "--extract",
            "--gzip",
            "--no-same-owner",
            "--no-same-permissions",
            f"--file={(paths['inputs'] / bundle_artifact).as_posix()}",
            f"--directory={paths['unpacked'].as_posix()}",
        ),
    )
    execute(
        "verify extracted bundle root",
        ("/usr/bin/test", "-d", paths["bundle_root"].as_posix()),
    )
    execute(
        "create isolated installed runtime",
        (
            python_path,
            "-I",
            "-m",
            "venv",
            paths["venv"].as_posix(),
        ),
    )
    installed_python = (paths["venv"] / "bin" / "python").as_posix()
    dependency = bundle.requirements_lock.for_target(LINUX_TARGET_ID_V1)
    if (
        len(dependency) != 1
        or dependency[0].name != "duckdb"
        or dependency[0].version != "1.5.5"
    ):
        raise RuntimeError("Linux qualification dependency lock differs")
    install_result = execute(
        "install release artifacts without an index",
        (
            installed_python,
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-input",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--find-links",
            paths["wheelhouse"].as_posix(),
            f"kirby2=={RELEASE_VERSION_V1}",
            f"{dependency[0].name}=={dependency[0].version}",
        ),
    )
    install_stream = (install_result.stdout + install_result.stderr).lower()
    if any(
        token in install_stream
        for token in (b"downloading", b"http://", b"https://")
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            "offline Linux installation emitted network-fetch output",
            terminal=True,
        )

    launcher = (
        paths["venv"] / "bin" / _INSTALLED_LAUNCHERS[state.form]
    ).as_posix()
    execute(
        "verify installed launcher",
        ("/usr/bin/test", "-x", launcher),
    )
    execute(
        "verify worker attempt root is absent",
        ("/usr/bin/test", "!", "-e", paths["worker_attempt"].as_posix()),
    )
    origin_result = execute(
        "verify installed package origin",
        (installed_python, "-I", "-c", _ORIGIN_PROBE_SCRIPT),
    )
    try:
        origin = _parse_canonical_object_line(
            origin_result.stdout, "Linux installed package origin"
        )
    except (TypeError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_NOT_CLEAN,
            "Linux installed package origin receipt is invalid",
            terminal=True,
        ) from error
    if set(origin) != {
        "base_exec_prefix",
        "base_prefix",
        "executable",
        "executable_realpath",
        "origin",
        "path",
        "prefix",
    }:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_NOT_CLEAN,
            "Linux installed package origin fields differ",
            terminal=True,
        )
    expected_prefix = paths["venv"].as_posix()
    base_roots = (
        provider_proof.get("base_prefix"),
        provider_proof.get("base_exec_prefix"),
    )

    def beneath(value: str, root: str) -> bool:
        return value == root or value.startswith(root + "/")

    effective_path = origin["path"]
    if (
        origin["prefix"] != expected_prefix
        or origin["executable"] != installed_python
        or origin["executable_realpath"] != python_path
        or origin["base_prefix"] != base_roots[0]
        or origin["base_exec_prefix"] != base_roots[1]
        or type(origin["origin"]) is not str
        or not beneath(str(origin["origin"]), expected_prefix)
        or any(type(root) is not str or not root.startswith("/") for root in base_roots)
        or type(effective_path) is not list
        or not effective_path
        or any(
            type(item) is not str
            or not item
            or not any(
                beneath(item, root)
                for root in (expected_prefix, *base_roots)
                if type(root) is str
            )
            for item in effective_path
        )
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_NOT_CLEAN,
            "Linux installed runtime exposes a source checkout or foreign import root",
            terminal=True,
        )
    return installed_python, launcher, tuple(commands)


def _parse_worker_result(
    form: str,
    launcher: str,
    attempt_root: PurePosixPath,
    command: _CommandResult,
) -> dict[str, object]:
    if command.returncode not in {0, 2}:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            f"installed Linux qualification worker exited {command.returncode}",
            terminal=True,
        )
    try:
        payload = _parse_canonical_object_line(
            command.stdout, "installed Linux qualification worker result"
        )
    except (TypeError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "Linux qualification worker result is not canonical JSON",
            terminal=True,
        ) from error
    schema_id = payload.get("schema_id")
    schema_version = payload.get("schema_version")
    status = payload.get("status")
    result_sha256 = payload.get("result_sha256")
    if (
        schema_id != _WORKER_SCHEMA_ID_V1
        or schema_version != 1
        or payload.get("form") != form
        or status not in {"PASS", "FAIL", "REFUSED"}
        or type(result_sha256) is not str
        or _SHA256.fullmatch(result_sha256) is None
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "Linux qualification worker identity fields differ",
            terminal=True,
        )
    body = dict(payload)
    del body["result_sha256"]
    if _sha256(canonical_json_bytes(body)) != result_sha256:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "Linux qualification worker self-digest differs",
            terminal=True,
        )
    expected_returncode = 0 if status == "PASS" else 2
    if command.returncode != expected_returncode:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "Linux qualification worker status and exit code disagree",
            terminal=True,
        )
    if status != "PASS":
        code = payload.get("failure_code")
        detail = payload.get("detail")
        if type(code) is not str or type(detail) is not str:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                "failed Linux worker result omitted its closed failure fields",
                terminal=True,
            )
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            f"installed Linux qualification worker {status.lower()}: {code}: {detail}",
            terminal=True,
        )
    expected_fields = {
        "attempt_root",
        "command_observations",
        "execution_policy_id",
        "facts",
        "form",
        "launcher",
        "offline",
        "platform",
        "result_sha256",
        "roots",
        "schema_id",
        "schema_version",
        "status",
        "step_results",
    }
    platform_row = payload.get("platform")
    if (
        set(payload) != expected_fields
        or payload.get("execution_policy_id") != _WORKER_EXECUTION_POLICY_ID_V1
        or payload.get("offline") is not True
        or payload.get("attempt_root") != attempt_root.as_posix()
        or payload.get("launcher") != launcher
        or type(platform_row) is not dict
        or set(platform_row)
        != {"machine", "python_implementation", "python_version", "system"}
        or platform_row.get("system") != "Linux"
        or platform_row.get("machine") != "x86_64"
        or platform_row.get("python_implementation") != "CPython"
        or type(platform_row.get("python_version")) is not str
        or not str(platform_row["python_version"]).startswith("3.14.")
        or any(
            type(payload.get(name)) is not list
            for name in ("command_observations", "roots", "step_results")
        )
        or type(payload.get("facts")) is not dict
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "passing Linux worker envelope differs from the closed contract",
            terminal=True,
        )
    return dict(payload)


def _run_installed_worker(
    provider: _LocalSshProvider,
    state: _LinuxFormState,
    *,
    home: str,
    installed_python: str,
    launcher: str,
) -> tuple[dict[str, object], _CommandResult]:
    paths = _remote_paths(state.remote_root, state.form)
    result = _isolated_remote_exec(
        provider,
        home=home,
        temporary=paths["tmp"],
        command=(
            installed_python,
            "-I",
            "-m",
            "kirby2.release.qualification_worker",
            "--form",
            state.form,
            "--launcher",
            launcher,
            "--attempt-root",
            paths["worker_attempt"].as_posix(),
        ),
        timeout_seconds=_WORKER_TIMEOUT_SECONDS,
        maximum_output_bytes=_WORKER_OUTPUT_MAX_BYTES,
    )
    parsed = _parse_worker_result(
        state.form,
        launcher,
        paths["worker_attempt"],
        result,
    )
    state.worker_result = parsed
    return parsed, result


def _run_form(
    provider: _LocalSshProvider,
    state: _LinuxFormState,
    *,
    python_path: str,
    bundle: ReleaseProtocolBundleV1,
) -> None:
    before = _prove_remote_provider(
        provider,
        python_path=python_path,
        phase="BEFORE_INSTALL",
        lifetime_root=state.remote_root,
    )
    state.provider_proofs.append(before)
    installed_python, launcher, installation = _install_form(
        provider,
        state,
        python_path=python_path,
        provider_proof=before,
        bundle=bundle,
    )
    state.provider_proofs.append(
        {
            "form": state.form,
            "installation_commands": list(installation),
            "phase": "INSTALLATION",
        }
    )
    after_install = _prove_remote_provider(
        provider,
        python_path=python_path,
        phase="AFTER_INSTALL",
        lifetime_root=state.remote_root,
    )
    state.provider_proofs.append(after_install)
    worker, worker_command = _run_installed_worker(
        provider,
        state,
        home=_remote_paths(state.remote_root, state.form)["home"].as_posix(),
        installed_python=installed_python,
        launcher=launcher,
    )
    state.provider_proofs.append(
        {
            "form": state.form,
            "phase": "WORKER",
            "worker_command": worker_command.observation(),
            "worker_result_sha256": worker["result_sha256"],
            "worker_status": worker["status"],
        }
    )
    after_worker = _prove_remote_provider(
        provider,
        python_path=python_path,
        phase="AFTER_WORKER",
        lifetime_root=state.remote_root,
    )
    state.provider_proofs.append(after_worker)


def _worker_collection(
    payload: Mapping[str, object],
    name: str,
) -> list[object]:
    value = payload.get(name)
    if type(value) is not list:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            f"Linux qualification worker omitted {name}",
            terminal=True,
        )
    return value


def _qualification_commands(
    states: Sequence[_LinuxFormState],
) -> tuple[ReleaseQualificationCommandObservationV1, ...]:
    commands: list[ReleaseQualificationCommandObservationV1] = []
    sequence = 1
    seen: set[str] = set()
    for state in states:
        if state.worker_result is None:
            raise RuntimeError("Linux qualification worker result is unavailable")
        for value in _worker_collection(state.worker_result, "command_observations"):
            if type(value) is not dict:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "Linux worker command observation is not an object",
                    terminal=True,
                )
            row = dict(value)
            command_id = row.get("command_id")
            if type(command_id) is not str or command_id in seen:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "Linux worker command IDs are invalid or duplicated",
                    terminal=True,
                )
            row["sequence"] = sequence
            try:
                command = ReleaseQualificationCommandObservationV1.from_dict(row)
            except (TypeError, ValueError) as error:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "Linux worker command observation failed typed parsing",
                    terminal=True,
                ) from error
            if command.artifact_selector != _ARTIFACT_SELECTORS[state.form]:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "Linux worker command selector differs from its form",
                    terminal=True,
                )
            commands.append(command)
            seen.add(command_id)
            sequence += 1
    return tuple(commands)


def _qualification_steps(
    states: Sequence[_LinuxFormState],
) -> tuple[ReleaseQualificationStepObservationV1, ...]:
    steps: list[ReleaseQualificationStepObservationV1] = []
    for state in states:
        if state.worker_result is None:
            raise RuntimeError("Linux qualification worker result is unavailable")
        for value in _worker_collection(state.worker_result, "step_results"):
            try:
                step = ReleaseQualificationStepObservationV1.from_dict(value)
            except (TypeError, ValueError) as error:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "Linux worker step observation failed typed parsing",
                    terminal=True,
                ) from error
            if step.artifact_selector != _ARTIFACT_SELECTORS[state.form]:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "Linux worker step selector differs from its form",
                    terminal=True,
                )
            steps.append(step)
    return tuple(steps)


def _qualification_roots(
    states: Sequence[_LinuxFormState],
) -> tuple[ReleaseQualificationRootObservationV1, ...]:
    observed: list[tuple[ReleaseQualificationRootObservationV1, ...]] = []
    for state in states:
        if state.worker_result is None:
            raise RuntimeError("Linux qualification worker result is unavailable")
        try:
            roots = tuple(
                ReleaseQualificationRootObservationV1.from_dict(value)
                for value in _worker_collection(state.worker_result, "roots")
            )
        except (TypeError, ValueError) as error:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                "Linux worker root observation failed typed parsing",
                terminal=True,
            ) from error
        observed.append(roots)
    if not observed or any(
        tuple(item.as_dict() for item in roots)
        != tuple(item.as_dict() for item in observed[0])
        for roots in observed[1:]
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "Linux desktop and headless clean-root observations differ",
            terminal=True,
        )
    return observed[0]


def _worker_facts(state: _LinuxFormState) -> dict[str, object]:
    if state.worker_result is None or type(state.worker_result.get("facts")) is not dict:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "Linux qualification worker omitted its fact projection",
            terminal=True,
        )
    facts = dict(state.worker_result["facts"])
    if set(facts) != {
        "clean_environment",
        "cross_platform_integer_core_sha256",
        "replay_sha256",
        "run_sha256",
    }:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "Linux qualification worker fact fields differ",
            terminal=True,
        )
    for field_name in (
        "cross_platform_integer_core_sha256",
        "replay_sha256",
        "run_sha256",
    ):
        value = facts[field_name]
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                "Linux qualification worker fact digest is invalid",
                terminal=True,
            )
    if facts["clean_environment"] is not True:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_NOT_CLEAN,
            "Linux qualification worker did not observe a clean environment",
            terminal=True,
        )
    return facts


def _artifact_bindings(
    index: ReleaseArtifactIndexV1,
    states: Sequence[_LinuxFormState],
) -> tuple[ReleaseQualificationArtifactBindingV1, ...]:
    required = RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1[LINUX_TARGET_ID_V1]
    copied: dict[str, tuple[int, str]] = {}
    for state in states:
        if not any(
            item.get("phase") == "FREEZE_INPUTS"
            for item in state.transfer_observations
        ):
            raise RuntimeError("Linux provider input freeze receipt is unavailable")
        for row in state.input_rows:
            artifact_id = row.get("artifact_id")
            if artifact_id not in required:
                continue
            size = row.get("size")
            digest = row.get("sha256")
            if (
                type(artifact_id) is not str
                or type(size) is not int
                or type(digest) is not str
            ):
                raise RuntimeError("Linux artifact transfer binding is malformed")
            prior = copied.get(artifact_id)
            if prior is not None and prior != (size, digest):
                raise RuntimeError("Linux artifact transfer bindings conflict")
            copied[artifact_id] = (size, digest)
    if set(copied) != set(required):
        raise RuntimeError("Linux artifact transfer binding inventory differs")
    indexed = {item.artifact_id: item for item in index.artifacts}
    return tuple(
        ReleaseQualificationArtifactBindingV1(
            artifact_id=artifact_id,
            size=copied[artifact_id][0],
            release_store_sha256=indexed[artifact_id].transport_sha256,
            provider_copy_sha256=copied[artifact_id][1],
        )
        for artifact_id in required
    )


def _before_install_proofs(
    states: Sequence[_LinuxFormState],
) -> tuple[dict[str, object], ...]:
    selected: list[dict[str, object]] = []
    for state in states:
        proof = next(
            (
                item
                for item in state.provider_proofs
                if item.get("phase") == "BEFORE_INSTALL"
            ),
            None,
        )
        if proof is None:
            raise RuntimeError("Linux before-install provider proof is unavailable")
        selected.append(proof)
    return tuple(selected)


def _compose_records(
    *,
    bundle: ReleaseProtocolBundleV1,
    build_evidence: _FileSnapshot,
    provider_inventory: _FileSnapshot,
    provider_capability: ReleaseCleanProviderV1,
    index: ReleaseArtifactIndexV1,
    build_record: ReleaseArtifactBuildRecordV1,
    provider: _LocalSshProvider,
    provider_lock: _RemoteProviderLock,
    states: Sequence[_LinuxFormState],
    final_provider_proof: Mapping[str, object],
    started_at_utc: str,
    finished_at_utc: str,
    duration_ns: int,
) -> tuple[ReleaseCleanProviderAttestationV1, ReleaseQualificationAttemptV1]:
    if (
        tuple(state.form for state in states) != _FORMS
        or any(not state.root_deleted for state in states)
        or type(provider_lock) is not _RemoteProviderLock
        or not provider_lock.acquired
        or not provider_lock.released
        or tuple(
            item.get("phase") for item in provider_lock.observations
        )
        != ("ACQUIRE_PROVIDER_LOCK", "RELEASE_PROVIDER_LOCK")
    ):
        raise RuntimeError(
            "Linux qualification requires one released provider lock and two deleted roots"
        )
    proofs = _before_install_proofs(states)
    identity_fields = (
        "base_exec_prefix",
        "base_prefix",
        "boot_id_sha256",
        "cpu_count",
        "gid",
        "home",
        "kernel_release",
        "machine",
        "machine_model",
        "memory_bytes",
        "os_version",
        "python_executable",
        "python_executable_realpath",
        "python_implementation",
        "python_version",
        "rmtree_symlink_safe",
        "source_checkout_present",
        "system",
        "uid",
    )
    all_identity_proofs = [
        item
        for state in states
        for item in state.provider_proofs
        if item.get("phase") in {"BEFORE_INSTALL", "AFTER_INSTALL", "AFTER_WORKER"}
    ] + [dict(final_provider_proof)]
    for field_name in identity_fields:
        if any(
            item.get(field_name) != all_identity_proofs[0].get(field_name)
            for item in all_identity_proofs[1:]
        ):
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
                f"Linux provider {field_name} changed during qualification",
                terminal=True,
            )
    if any(
        item.get("network_scope") != "GUEST_NETWORK_DISABLED_VERIFIED"
        for item in all_identity_proofs
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_ISOLATION_UNAVAILABLE,
            "Linux provider isolation proof changed during qualification",
            terminal=True,
        )

    capability_sha256 = provider_capability.fingerprint
    attestation = ReleaseCleanProviderAttestationV1(
        provider_id=(
            f"kirby2-clean-provider-{LINUX_TARGET_ID_V1}-"
            f"{capability_sha256[:16]}"
        ),
        target_id=LINUX_TARGET_ID_V1,
        provider_inventory_sha256=provider_inventory.sha256,
        provider_capability_sha256=capability_sha256,
        provider_adapter_id=LINUX_PROVIDER_ADAPTER_ID_V1,
        attestation_method=RELEASE_QUALIFICATION_ATTESTATION_METHOD_V1,
        system=str(proofs[0]["system"]),
        os_version=str(proofs[0]["os_version"]),
        kernel_release=str(proofs[0]["kernel_release"]),
        machine=str(proofs[0]["machine"]),
        machine_model=str(proofs[0]["machine_model"]),
        python_implementation=str(proofs[0]["python_implementation"]),
        python_version=str(proofs[0]["python_version"]),
        cpu_count=int(proofs[0]["cpu_count"]),
        memory_bytes=int(proofs[0]["memory_bytes"]),
        available_disk_bytes=min(
            int(item["available_disk_bytes"]) for item in all_identity_proofs
        ),
        offline_install=True,
        network_scope=(
            ReleaseQualificationNetworkScopeV1.GUEST_NETWORK_DISABLED_VERIFIED
        ),
        observed_at_utc=started_at_utc,
    )

    commands = _qualification_commands(states)
    steps = _qualification_steps(states)
    roots = _qualification_roots(states)
    desktop_facts = _worker_facts(states[0])
    headless_facts = _worker_facts(states[1])
    if (
        desktop_facts["cross_platform_integer_core_sha256"]
        != headless_facts["cross_platform_integer_core_sha256"]
        or desktop_facts["replay_sha256"] != headless_facts["replay_sha256"]
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "Linux desktop and headless baseline or replay identities differ",
            terminal=True,
        )
    facts = ReleaseQualificationFactsV1(
        clean_environment=True,
        cross_platform_integer_core_sha256=str(
            desktop_facts["cross_platform_integer_core_sha256"]
        ),
        desktop_run_sha256=str(desktop_facts["run_sha256"]),
        headless_run_sha256=str(headless_facts["run_sha256"]),
        platform_id=LINUX_TARGET_ID_V1,
        replay_sha256=str(desktop_facts["replay_sha256"]),
    )

    instance_projection = {
        "forms": [
            {
                "form": state.form,
                "provider_proofs": state.provider_proofs,
                "remote_root": state.remote_root.as_posix(),
                "transfer_observations": state.transfer_observations,
            }
            for state in states
        ],
        "host": SSH_HOST_V1,
        "host_key_fingerprint": provider.host_key_fingerprint,
        "local_ssh_projection_sha256": provider.executable_projection_sha256,
        "port": SSH_PORT_V1,
        "provider_lock": {
            "marker_sha256": provider_lock.marker_sha256,
            "observations": provider_lock.observations,
        },
        "provider_policy_id": LINUX_PROVIDER_POLICY_ID_V1,
        "user": SSH_USER_V1,
    }
    instance_sha256 = _sha256(canonical_json_bytes(instance_projection))
    session_projection = {
        "candidate_commit": index.candidate_commit,
        "instance_sha256": instance_sha256,
        "provider_sha256": attestation.sha256,
        "worker_result_sha256s": [
            state.worker_result["result_sha256"]  # type: ignore[index]
            for state in states
        ],
    }
    session_sha256 = _sha256(canonical_json_bytes(session_projection))
    session = ReleaseQualificationSessionV1(
        session_id=f"wo40h-{session_sha256[:24]}",
        provider_id=attestation.provider_id,
        provider_attestation_sha256=attestation.sha256,
        provider_instance_id=f"ssh-ephemeral-instance-{instance_sha256[:24]}",
        target_id=LINUX_TARGET_ID_V1,
        attempt_number=1,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        duration_ns=duration_ns,
        network_scope=(
            ReleaseQualificationNetworkScopeV1.GUEST_NETWORK_DISABLED_VERIFIED
        ),
        installation_source=RELEASE_QUALIFICATION_INSTALLATION_SOURCE_V1,
        source_checkout_present=False,
        artifact_bindings=_artifact_bindings(index, states),
        roots=roots,
    )
    attempt = build_release_qualification_attempt_record(
        gate_id=LINUX_GATE_ID_V1,
        target_id=LINUX_TARGET_ID_V1,
        candidate_commit=index.candidate_commit,
        protocol_set_sha256=bundle.protocol_set_sha256,
        source_manifest_sha256=build_record.source_manifest_sha256,
        artifact_index_sha256=index.sha256,
        build_evidence_sha256=build_evidence.sha256,
        session=session,
        commands=commands,
        steps=steps,
        facts=facts,
    )
    pure = verify_release_qualification_record(
        attestation,
        attempt,
        bundle.qualification_protocol,
    )
    if pure.status != "PASS" or attempt.status != "PASS":
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "Linux qualification did not compose a passing typed attempt",
            terminal=True,
        )
    return attestation, attempt


def _require_no_prior_records(root: Path) -> tuple[Path, Path]:
    provider_relative, attempt_relative = release_qualification_record_paths(
        LINUX_TARGET_ID_V1
    )
    provider_path = _record_target_path(root, provider_relative)
    attempt_path = _record_target_path(root, attempt_relative)
    for path in (provider_path, attempt_path):
        if path.exists() or path.is_symlink():
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PRIOR_ATTEMPT_EXISTS,
                "immutable Linux provider or attempt evidence already exists",
            )
    return provider_path, attempt_path


def _require_remote_root_absent(
    provider: _LocalSshProvider,
    root: PurePosixPath,
) -> dict[str, object]:
    selected = _require_remote_root(root)
    result = _require_remote_success(
        _ssh_exec(
            provider,
            ("/usr/bin/test", "!", "-e", selected.as_posix()),
            timeout_seconds=30,
            maximum_output_bytes=1024 * 1024,
        ),
        "final owned-root absence proof",
        terminal=True,
    )
    return result.observation()


def execute_linux_release_qualification(
    bundle: ReleaseProtocolBundleV1,
    *,
    build_evidence: Path,
    artifact_root: Path,
) -> ReleaseCommandOutcomeV1:
    """Execute and publish the one closed Linux x86_64 WO40-H attempt."""

    if type(bundle) is not ReleaseProtocolBundleV1:
        raise TypeError("Linux qualification requires the exact protocol bundle")
    states: list[_LinuxFormState] = []
    projections: list[tuple[Path, tuple[int, ...]]] = []
    provider: _LocalSshProvider | None = None
    provider_lock: _RemoteProviderLock | None = None
    provider_process_quarantine = False
    provider_local_retired = False
    python_path: str | None = None
    store_descriptor: int | None = None
    opened_identity: tuple[int, ...] | None = None
    candidate_commit: str | None = None
    result: ReleaseCommandOutcomeV1 | None = None
    failure: Exception | None = None
    cleanup_failures: list[str] = []
    try:
        root = _absolute_input(artifact_root, "release artifact root")
        evidence_path = _absolute_input(build_evidence, "WO40-F build evidence")
        try:
            _require_canonical_tracked_build_evidence(
                bundle.repository_root.resolve(strict=True),
                evidence_path,
            )
        except (OSError, RuntimeError, ValueError) as error:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.INPUT_INVALID,
                "Linux qualification requires committed canonical WO40-F evidence",
            ) from error

        store_descriptor, opened_identity = _open_artifact_store(root)
        try:
            fcntl.flock(store_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                "release artifact store is locked by another operation",
            ) from error
        _require_no_prior_records(root)

        evidence_snapshot = _stable_read(
            evidence_path,
            maximum_bytes=_BUILD_EVIDENCE_MAX_BYTES,
            require_read_only=False,
        )
        binding = ReleaseBuildEvidenceBindingV1.from_markdown_bytes(
            evidence_snapshot.raw
        )
        index_snapshot = _stable_read(
            root / RELEASE_ARTIFACT_INDEX_FILENAME_V1,
            maximum_bytes=RELEASE_RECORD_MAX_BYTES_V1,
            require_read_only=True,
        )
        build_record_snapshot = _stable_read(
            root / RELEASE_BUILD_RECORD_FILENAME_V1,
            maximum_bytes=RELEASE_RECORD_MAX_BYTES_V1,
            require_read_only=True,
        )
        index = ReleaseArtifactIndexV1.from_bytes(index_snapshot.raw)
        build_record = ReleaseArtifactBuildRecordV1.from_bytes(
            build_record_snapshot.raw
        )
        candidate_commit = index.candidate_commit
        build_check_rows = tuple(
            (item.check_id, item.evidence_sha256, item.status)
            for item in build_record.checks
        )
        if (
            binding.build_evidence_sha256 != evidence_snapshot.sha256
            or binding.candidate_commit != candidate_commit
            or binding.protocol_set_sha256 != bundle.protocol_set_sha256
            or binding.source_manifest_sha256 != build_record.source_manifest_sha256
            or binding.artifact_index_sha256 != index.sha256
            or binding.artifact_index_record_sha256 != index_snapshot.sha256
            or binding.artifact_index_record_size != len(index_snapshot.raw)
            or binding.build_record_sha256 != build_record_snapshot.sha256
            or binding.build_record_size != len(build_record_snapshot.raw)
            or binding.check_rows != build_check_rows
            or build_record.candidate_commit != candidate_commit
            or build_record.protocol_set_sha256 != bundle.protocol_set_sha256
            or build_record.artifact_index_sha256 != index.sha256
        ):
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.ARTIFACT_VERIFICATION_FAILED,
                "WO40-F evidence, artifacts, build record, or protocol identity differs",
            )
        artifact_verification = verify_release_artifacts(
            bundle,
            root,
            candidate_commit=candidate_commit,
        )
        if artifact_verification.status is not ReleaseCommandStatusV1.PASS:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.ARTIFACT_VERIFICATION_FAILED,
                "deep immutable release-artifact verification did not pass",
            )

        inventory_snapshot = _stable_read(
            root / "clean-providers.toml",
            maximum_bytes=4 * 1024 * 1024,
            require_read_only=False,
        )
        inventory = ReleaseCleanProviderInventoryV1.from_bytes(
            inventory_snapshot.raw
        )
        capability = inventory.by_target().get(LINUX_TARGET_ID_V1)
        platform_target = next(
            (
                item
                for item in bundle.platform_protocol.targets
                if item.target_id == LINUX_TARGET_ID_V1
            ),
            None,
        )
        if (
            capability is None
            or platform_target is None
            or capability.readiness(platform_target)[0] != "PASS"
            or capability.access_method != "REMOTE_SSH"
            or capability.clean_root_mechanism != "EPHEMERAL_HOST"
            or capability.evidence_return != "SSH_ARTIFACT_RETURN"
        ):
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
                "preregistered Linux SSH provider capability is not ready",
            )

        try:
            macos_verification = verify_release_qualification(
                bundle,
                target_id="macos-arm64",
                build_evidence=evidence_path,
                artifact_root=root,
            )
        except (OSError, TypeError, ValueError) as error:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                "passing immutable WO40-G baseline is unavailable",
            ) from error
        if (
            macos_verification.status != "PASS"
            or macos_verification.candidate_commit != candidate_commit
            or macos_verification.artifact_index_sha256 != index.sha256
            or macos_verification.build_evidence_sha256 != evidence_snapshot.sha256
        ):
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                "WO40-G baseline differs from the Linux candidate",
            )
        mac_provider_relative, mac_attempt_relative = (
            release_qualification_record_paths("macos-arm64")
        )
        mac_provider_path = _record_target_path(root, mac_provider_relative)
        mac_attempt_path = _record_target_path(root, mac_attempt_relative)
        mac_provider_snapshot = _stable_read(
            mac_provider_path,
            maximum_bytes=4 * 1024 * 1024,
            require_read_only=True,
        )
        mac_attempt_snapshot = _stable_read(
            mac_attempt_path,
            maximum_bytes=64 * 1024 * 1024,
            require_read_only=True,
        )

        common_root = _remote_root(candidate_commit, secrets.token_hex(16))
        for form in _FORMS:
            projection, input_rows, projection_identity = _build_projection(
                form=form,
                artifact_root=root,
                build_evidence=evidence_path,
                index=index,
                build_record=build_record,
            )
            projections.append((projection, projection_identity))
            marker = _marker_bytes(
                candidate_commit=candidate_commit,
                form=form,
                root=common_root,
            )
            states.append(
                _LinuxFormState(
                    form=form,
                    projection_root=projection,
                    projection_root_identity=projection_identity,
                    input_rows=input_rows,
                    remote_root=common_root,
                    marker_sha256=_sha256(marker),
                )
            )

        started_at_utc = _utc_second()
        started_ns = time.monotonic_ns()
        provider = _prepare_local_ssh_provider()
        python_path, discovery, discovery_observation = _probe_remote_python(provider)
        provider_lock_marker = _provider_lock_marker_bytes(
            candidate_commit=candidate_commit,
            nonce=secrets.token_hex(16),
        )
        provider_lock = _RemoteProviderLock(
            marker_sha256=_sha256(provider_lock_marker)
        )
        _acquire_remote_provider_lock(
            provider,
            provider_lock,
            python_path=python_path,
            marker=provider_lock_marker,
        )
        states[0].provider_proofs.append(
            {
                **discovery,
                **discovery_observation,
                "phase": "PYTHON_DISCOVERY",
            }
        )
        for state in states:
            marker = _marker_bytes(
                candidate_commit=candidate_commit,
                form=state.form,
                root=state.remote_root,
            )
            form_failure: Exception | None = None
            try:
                _create_remote_root(
                    provider,
                    state,
                    python_path=python_path,
                    marker=marker,
                )
                _sftp_put(provider, state)
                _finalize_remote_inputs(
                    provider,
                    state,
                    python_path=python_path,
                )
                _run_form(
                    provider,
                    state,
                    python_path=python_path,
                    bundle=bundle,
                )
            except Exception as error:
                form_failure = error
            form_cleanup = _cleanup_remote_root(
                provider,
                state,
                python_path=python_path,
            )
            if form_cleanup is not None:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.PROVIDER_CLEANUP_FAILED,
                    form_cleanup,
                    terminal=True,
                ) from form_failure
            if form_failure is not None:
                raise form_failure

        provider_process_quarantine = True
        root_absence_observation = _require_remote_root_absent(
            provider,
            common_root,
        )
        final_provider_proof = _prove_remote_provider(
            provider,
            python_path=python_path,
            phase="AFTER_CLEANUP",
            lifetime_root=common_root,
        )
        final_provider_proof["root_absence_command"] = root_absence_observation
        final_absence, final_absence_payload = _prove_remote_process_absence(
            provider,
            common_root,
            python_path=python_path,
        )
        provider_process_quarantine = False
        final_provider_proof["process_absence_command"] = final_absence.observation()
        final_provider_proof["process_absence_receipt"] = final_absence_payload
        if any(
            state.root_maybe_created and not state.root_deleted
            for state in states
        ):
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PROVIDER_CLEANUP_FAILED,
                "provider lock cannot be released before every owned root is absent",
                terminal=True,
            )
        provider_lock_failure = _release_remote_provider_lock(
            provider,
            provider_lock,
            python_path=python_path,
        )
        if provider_lock_failure is not None:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PROVIDER_CLEANUP_FAILED,
                provider_lock_failure,
                terminal=True,
            )

        final_verification = verify_release_artifacts(
            bundle,
            root,
            candidate_commit=candidate_commit,
        )
        if final_verification.status is not ReleaseCommandStatusV1.PASS:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.ARTIFACT_VERIFICATION_FAILED,
                "release artifacts changed during Linux qualification",
                terminal=True,
            )
        if (
            not _same_snapshot(
                evidence_path,
                evidence_snapshot,
                _BUILD_EVIDENCE_MAX_BYTES,
            )
            or not _same_snapshot(
                root / RELEASE_ARTIFACT_INDEX_FILENAME_V1,
                index_snapshot,
                RELEASE_RECORD_MAX_BYTES_V1,
            )
            or not _same_snapshot(
                root / RELEASE_BUILD_RECORD_FILENAME_V1,
                build_record_snapshot,
                RELEASE_RECORD_MAX_BYTES_V1,
            )
            or not _same_snapshot(
                root / "clean-providers.toml",
                inventory_snapshot,
                4 * 1024 * 1024,
            )
            or not _same_snapshot(
                mac_provider_path,
                mac_provider_snapshot,
                4 * 1024 * 1024,
            )
            or not _same_snapshot(
                mac_attempt_path,
                mac_attempt_snapshot,
                64 * 1024 * 1024,
            )
        ):
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.INPUT_INVALID,
                "immutable Linux qualification inputs changed during provider execution",
                terminal=True,
            )

        finished_at_utc = _utc_second()
        duration_ns = time.monotonic_ns() - started_ns
        provider_record, attempt = _compose_records(
            bundle=bundle,
            build_evidence=evidence_snapshot,
            provider_inventory=inventory_snapshot,
            provider_capability=capability,
            index=index,
            build_record=build_record,
            provider=provider,
            provider_lock=provider_lock,
            states=states,
            final_provider_proof=final_provider_proof,
            started_at_utc=started_at_utc,
            finished_at_utc=finished_at_utc,
            duration_ns=duration_ns,
        )

        for projection, projection_identity in projections:
            local_failure = _remove_local_tree(
                projection,
                prefix="kirby2-wo40h-",
                expected_identity=projection_identity,
            )
            if local_failure is not None:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.PROVIDER_CLEANUP_FAILED,
                    local_failure,
                    terminal=True,
                )
        projections.clear()
        local_failure = _remove_local_tree(
            provider.temporary_root,
            prefix="kirby2-wo40h-ssh-",
            expected_identity=provider.temporary_root_identity,
        )
        if local_failure is not None:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PROVIDER_CLEANUP_FAILED,
                local_failure,
                terminal=True,
            )
        provider_local_retired = True

        _require_store_anchor(root, store_descriptor, opened_identity)
        _require_no_prior_records(root)
        gate_descriptor = _open_publication_directory(
            store_descriptor,
            ("gate-evidence",),
        )
        baseline = None
        try:
            # This provider-free comparison is deliberately inside the entrypoint
            # and before either Linux record can become immutable.
            baseline = _require_macos_integer_core_baseline(
                bundle=bundle,
                root_fd=store_descriptor,
                gate_fd=gate_descriptor,
                inventory=inventory,
                inventory_raw=inventory_snapshot.raw,
                index=index,
                binding=binding,
                linux_attempt=attempt,
            )
            try:
                _require_canonical_tracked_build_evidence(
                    bundle.repository_root.resolve(strict=True),
                    evidence_path,
                )
            except (OSError, RuntimeError, ValueError) as error:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.INPUT_INVALID,
                    "tracked WO40-F evidence or Git index changed before publication",
                    terminal=True,
                ) from error
            post_baseline_verification = verify_release_artifacts(
                bundle,
                root,
                candidate_commit=candidate_commit,
            )
            if post_baseline_verification.status is not ReleaseCommandStatusV1.PASS:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.ARTIFACT_VERIFICATION_FAILED,
                    "release artifacts changed after macOS-baseline binding",
                    terminal=True,
                )
            if (
                baseline.provider_raw != mac_provider_snapshot.raw
                or baseline.attempt_raw != mac_attempt_snapshot.raw
                or not _same_snapshot(
                    evidence_path,
                    evidence_snapshot,
                    _BUILD_EVIDENCE_MAX_BYTES,
                )
                or not _same_snapshot(
                    root / RELEASE_ARTIFACT_INDEX_FILENAME_V1,
                    index_snapshot,
                    RELEASE_RECORD_MAX_BYTES_V1,
                )
                or not _same_snapshot(
                    root / RELEASE_BUILD_RECORD_FILENAME_V1,
                    build_record_snapshot,
                    RELEASE_RECORD_MAX_BYTES_V1,
                )
                or not _same_snapshot(
                    root / "clean-providers.toml",
                    inventory_snapshot,
                    4 * 1024 * 1024,
                )
                or not _same_snapshot(
                    mac_provider_path,
                    mac_provider_snapshot,
                    4 * 1024 * 1024,
                )
                or not _same_snapshot(
                    mac_attempt_path,
                    mac_attempt_snapshot,
                    64 * 1024 * 1024,
                )
            ):
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.INPUT_INVALID,
                    "immutable qualification inputs changed after baseline binding",
                    terminal=True,
                )
            _require_store_anchor(root, store_descriptor, opened_identity)
            _require_no_prior_records(root)
            if (
                type(provider_lock) is not _RemoteProviderLock
                or not provider_lock.released
            ):
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.PROVIDER_CLEANUP_FAILED,
                    "provider lock was not released before immutable publication",
                    terminal=True,
                )
            candidate_deep = _verify_release_qualification_records(
                bundle,
                target_id=LINUX_TARGET_ID_V1,
                build_evidence=evidence_path,
                artifact_root=root,
                candidate_provider_raw=provider_record.canonical_bytes(),
                candidate_attempt_raw=attempt.canonical_bytes(),
            )
            if (
                candidate_deep.status != attempt.status
                or candidate_deep.provider_attestation_sha256
                != provider_record.sha256
                or candidate_deep.qualification_attempt_sha256 != attempt.sha256
                or candidate_deep.check_count != len(attempt.checks)
            ):
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "candidate Linux qualification evidence failed deep verification",
                    terminal=True,
                )
        finally:
            if baseline is not None:
                os.close(baseline.attempt_directory_fd)
            os.close(gate_descriptor)
        provider_relative, attempt_relative = release_qualification_record_paths(
            LINUX_TARGET_ID_V1
        )
        prepared_result = _executor_outcome(
            bundle,
            status=ReleaseCommandStatusV1.PASS,
            detail=(
                "Linux desktop and headless qualification completed sequentially "
                "in one recreated network-disabled SSH root; immutable WO40-H "
                "evidence was published after cleanup and macOS-baseline binding."
            ),
            payload={
                "artifact_index_sha256": index.sha256,
                "attempt_path": attempt_relative,
                "candidate_commit": candidate_commit,
                "check_count": len(attempt.checks),
                "provider_attestation_path": provider_relative,
                "provider_attestation_sha256": provider_record.sha256,
                "qualification_attempt_sha256": attempt.sha256,
                "session_id": attempt.session.session_id,
                "target_id": LINUX_TARGET_ID_V1,
                "warning_count": len(attempt.warnings),
            },
        )
        _publish_records(
            root_descriptor=store_descriptor,
            target_id=LINUX_TARGET_ID_V1,
            provider=provider_record,
            attempt=attempt,
        )
        result = prepared_result
    except Exception as error:
        failure = error
    finally:
        if provider is not None and python_path is not None:
            for state in states:
                remote_failure = _cleanup_remote_root(
                    provider,
                    state,
                    python_path=python_path,
                )
                if remote_failure is not None:
                    cleanup_failures.append(remote_failure)
            if provider_lock is not None:
                quarantine = provider_process_quarantine or any(
                    state.root_maybe_created and not state.root_deleted
                    for state in states
                )
                if quarantine and provider_lock.maybe_acquired and not provider_lock.released:
                    cleanup_failures.append(
                        "remote provider lock retained because owned-root or process "
                        "cleanup remains ambiguous"
                    )
                else:
                    lock_failure = _release_remote_provider_lock(
                        provider,
                        provider_lock,
                        python_path=python_path,
                    )
                    if lock_failure is not None:
                        cleanup_failures.append(lock_failure)
        for projection, projection_identity in projections:
            local_failure = _remove_local_tree(
                projection,
                prefix="kirby2-wo40h-",
                expected_identity=projection_identity,
            )
            if local_failure is not None:
                cleanup_failures.append(local_failure)
        if provider is not None and not provider_local_retired:
            local_failure = _remove_local_tree(
                provider.temporary_root,
                prefix="kirby2-wo40h-ssh-",
                expected_identity=provider.temporary_root_identity,
            )
            if local_failure is not None:
                cleanup_failures.append(local_failure)
        if store_descriptor is not None:
            activation_complete = result is not None and failure is None
            try:
                try:
                    fcntl.flock(store_descriptor, fcntl.LOCK_UN)
                except OSError as error:
                    if not activation_complete:
                        cleanup_failures.append(
                            f"release-store unlock failed: {type(error).__name__}"
                        )
            finally:
                try:
                    os.close(store_descriptor)
                except OSError as error:
                    if not activation_complete:
                        cleanup_failures.append(
                            f"release-store close failed: {type(error).__name__}"
                        )

    payload: dict[str, object] = {
        "candidate_commit": candidate_commit,
        "target_id": LINUX_TARGET_ID_V1,
    }
    if cleanup_failures:
        payload["cleanup_failures"] = list(dict.fromkeys(cleanup_failures))
        if isinstance(failure, _QualificationRefused):
            payload["primary_refusal_code"] = failure.code.value
        return _executor_outcome(
            bundle,
            status=ReleaseCommandStatusV1.FAIL,
            detail="Linux qualification provider cleanup did not complete exactly",
            refusal_code=QualificationExecutorRefusalCodeV1.PROVIDER_CLEANUP_FAILED,
            payload=payload,
        )
    if failure is not None:
        if isinstance(failure, _QualificationRefused):
            return _executor_outcome(
                bundle,
                status=(
                    ReleaseCommandStatusV1.FAIL
                    if failure.terminal
                    else ReleaseCommandStatusV1.REFUSED
                ),
                detail=failure.detail,
                refusal_code=failure.code,
                payload=payload,
            )
        return _executor_outcome(
            bundle,
            status=(
                ReleaseCommandStatusV1.FAIL
                if any(state.root_maybe_created for state in states)
                or (
                    provider_lock is not None
                    and provider_lock.maybe_acquired
                )
                else ReleaseCommandStatusV1.REFUSED
            ),
            detail=f"closed Linux qualification failed: {type(failure).__name__}",
            refusal_code=(
                QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED
            ),
            payload=payload,
        )
    if result is None:  # pragma: no cover - total control-flow guard
        raise RuntimeError("Linux qualification executor omitted its terminal outcome")
    return result


__all__ = [
    "LINUX_GATE_ID_V1",
    "LINUX_PROVIDER_POLICY_ID_V1",
    "LINUX_TARGET_ID_V1",
    "execute_linux_release_qualification",
]
