"""限定到已配置 SSH 账号的 Codex 进程与已知运行目录，不扫描文件系统。"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import shlex

REMOTE_PROBE = r'''
import getpass,json,os,platform,stat,subprocess
from pathlib import Path

homes={str(Path.home()/'.codex')}
endpoints={}
processes=[]
result=subprocess.run(['ps','-u',str(os.getuid()),'-o','pid=,comm='],capture_output=True,text=True,timeout=5)
for line in result.stdout.splitlines():
    parts=line.split(None,1)
    if len(parts)!=2 or not parts[1].startswith('codex'):
        continue
    pid=int(parts[0])
    try:
        argv=Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\0')
        argv=[x.decode(errors='replace') for x in argv if x]
        env=Path('/proc/'+str(pid)+'/environ').read_bytes().split(b'\0')
        home=next((x.split(b'=',1)[1].decode() for x in env if x.startswith(b'CODEX_HOME=')),str(Path.home()/'.codex'))
        homes.add(home)
        processes.append({'pid':pid,'codex_home':home})
        for index,arg in enumerate(argv):
            value=argv[index+1] if arg=='--listen' and index+1<len(argv) else arg.removeprefix('--listen=') if arg.startswith('--listen=') else ''
            if value.startswith('unix://') and value!='unix://':
                path=value[len('unix://'):]
                if Path(path).is_socket():
                    endpoints[path]={'socket':path,'codex_home':home,'pid':pid}
    except (OSError,ValueError):
        pass
for home in homes:
    directory=Path(home)
    candidates=[directory/'app-server-control/app-server-control.sock',directory/'app-server.sock']
    for sub in [directory,directory/'app-server-control',directory/'daemon',directory/'sockets']:
        if sub.is_dir():
            candidates.extend(list(sub.glob('*.sock'))[:32])
    for path in candidates:
        try:
            if path.is_socket():
                endpoints.setdefault(str(path),{'socket':str(path),'codex_home':home,'pid':None})
        except OSError:
            pass
print(json.dumps({'architecture':platform.machine(),'username':getpass.getuser(),'process_count':len(processes),'endpoints':list(endpoints.values())[:128]},ensure_ascii=True))
'''


async def _remote_json(host, program: str, argument: dict | None = None) -> dict:
    args = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes", "-p", str(host.port)]
    if host.known_hosts_file:
        args += ["-o", f"UserKnownHostsFile={host.known_hosts_file}"]
    if host.identity_file:
        args += ["-i", host.identity_file]
    if host.jump:
        args += ["-J", host.jump]
    target = f"{host.username}@{host.address}" if host.username else host.address
    marker = "SB-PROBE-" + secrets.token_hex(16) + ":"
    # 完成标记在整个探测程序执行结束且 stdout 已解析为 JSON 后才产生。
    # 不依赖 Windows OpenSSH 的标准管道 EOF，也不提前接受中途输出。
    wrapper = (
        "import contextlib,io,json\n"
        "buffer=io.StringIO()\n"
        "try:\n"
        "    with contextlib.redirect_stdout(buffer):\n"
        f"        exec({program!r})\n"
        "    response={'ok':True,'result':json.loads(buffer.getvalue())}\n"
        "except Exception as error:\n"
        "    response={'ok':False,'error':type(error).__name__+': '+str(error)}\n"
        f"print({marker!r}+json.dumps(response,ensure_ascii=True),flush=True)\n"
    )
    encoded = base64.b64encode(wrapper.encode()).decode()
    command = ["python3", "-c", f"import base64;exec(base64.b64decode('{encoded}'))"]
    if argument is not None:
        command.append(json.dumps(argument, ensure_ascii=False))
    args += [target, shlex.join(command)]
    process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=128 * 1024)
    result = None
    try:
        async with asyncio.timeout(20):
            size = 0
            while line := await process.stdout.readline():
                size += len(line)
                if size > 128 * 1024:
                    raise ValueError("主机发现响应超过限制")
                if line.startswith(marker.encode()):
                    result = json.loads(line[len(marker):])
                    break
    finally:
        # 每次探测拥有独立 SSH 子进程；标记已证明远端只读程序执行完成。
        if process.returncode is None:
            process.kill()
            await process.wait()
    if result is None:
        stderr = await process.stderr.read(4096)
        raise ConnectionError(stderr.decode(errors="replace")[-1500:])
    if not isinstance(result, dict) or not result.get("ok"):
        raise ConnectionError((result or {}).get("error", "主机探测未完成"))
    result = result.get("result")
    if not isinstance(result, dict):
        raise ValueError("主机发现响应必须为 JSON 对象")
    return result


async def probe_host(host) -> dict:
    return await _remote_json(host, REMOTE_PROBE)


async def check_remote_scope(host, cwd: str, roots: list[str]) -> dict:
    program = """
import json,sys
from pathlib import Path
data=json.loads(sys.argv[1])
cwd=Path(data['cwd']).resolve(strict=True)
roots=[Path(p).resolve(strict=True) for p in data['roots']]
allowed=any(cwd.is_relative_to(root) for root in roots)
print(json.dumps({'allowed':allowed,'cwd':str(cwd),'roots':[str(p) for p in roots]}))
"""
    return await _remote_json(host, program, {"cwd": cwd, "roots": roots})
