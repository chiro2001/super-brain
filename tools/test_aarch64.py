"""在已配置 aarch64 主机的测试目录启动独立 Codex，不改现有安装与 daemon。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shlex
from pathlib import Path

from super_brain.models import Host
from super_brain.rpc import CodexClient

BOOTSTRAP = r'''
import atexit,base64,hashlib,io,json,os,platform,shutil,subprocess,sys,tarfile,tempfile,time,urllib.request
from pathlib import Path
assert platform.machine() in ['aarch64','arm64'], '目标不是 aarch64'
run=Path(tempfile.mkdtemp(prefix='super-brain-aarch64-',dir='/tmp'))
owned=[]
def abort():
    for process in reversed(owned):
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
    shutil.rmtree(run,ignore_errors=True)
atexit.register(abort)
(run/'work').mkdir()
home=run/'codex-home';home.mkdir()
cache=Path.home()/'.cache/super-brain/codex-0.166.0-arm64'
cache.mkdir(parents=True,exist_ok=True)
binary=cache/'codex'
if not binary.is_file():
    with urllib.request.urlopen('https://registry.npmjs.org/@openai%2fcodex/0.166.0-linux-arm64',timeout=20) as response:
        metadata=json.load(response)
    with urllib.request.urlopen(metadata['dist']['tarball'],timeout=120) as response:
        package=response.read()
    expected=metadata['dist']['integrity'].split('sha512-',1)[1].split()[0]
    assert base64.b64encode(hashlib.sha512(package).digest()).decode()==expected,'包完整性校验失败'
    with tarfile.open(fileobj=io.BytesIO(package),mode='r:gz') as archive:
        member=next(m for m in archive.getmembers() if m.name.endswith('/aarch64-unknown-linux-musl/bin/codex'))
        binary.write_bytes(archive.extractfile(member).read())
    binary.chmod(0o755)
version=subprocess.check_output([str(binary),'--version'],text=True).strip()
assert version=='codex-cli 0.166.0',version
mock_code=r"""
import json,sys,time,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
root=Path(sys.argv[1])
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  with (root/'requests.jsonl').open('a') as log:log.write(json.dumps({'model':body.get('model'),'path':self.path})+'\n')
  item={'id':'msg_'+uuid.uuid4().hex,'type':'message','role':'assistant','status':'completed','phase':'final_answer','content':[{'type':'output_text','text':'aarch64 验证完成，保留事实 ARM-42。','annotations':[]}]}
  response={'id':'resp_'+uuid.uuid4().hex,'object':'response','status':'completed','model':body.get('model'),'output':[item],'usage':{'input_tokens':50,'output_tokens':20,'total_tokens':70}}
  events=[{'type':'response.created','response':dict(response,status='in_progress',output=[])},{'type':'response.output_item.added','output_index':0,'item':dict(item,status='in_progress',content=[])},{'type':'response.output_text.delta','item_id':item['id'],'output_index':0,'content_index':0,'delta':item['content'][0]['text']},{'type':'response.output_item.done','output_index':0,'item':item},{'type':'response.completed','response':response}]
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
  try:
   for seq,event in enumerate(events):
    event['sequence_number']=seq
    self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode());self.wfile.flush()
  except (BrokenPipeError,ConnectionResetError):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
(root/'model-port').write_text(str(server.server_port))
server.serve_forever()
"""
log=(run/'bootstrap.log').open('ab')
mock=subprocess.Popen([sys.executable,'-c',mock_code,str(run)],stdout=log,stderr=log,start_new_session=True)
owned.append(mock)
deadline=time.monotonic()+10
while not (run/'model-port').is_file():
    assert time.monotonic()<deadline,'模拟服务启动超时'
    time.sleep(.05)
port=(run/'model-port').read_text()
(home/'config.toml').write_text('model_provider="mock"\nmodel="A2-dsv41f"\ncheck_for_update_on_startup=false\n[model_providers.mock]\nname="隔离验收"\nbase_url="http://127.0.0.1:'+port+'/v1"\nwire_api="responses"\nrequires_openai_auth=false\n[analytics]\nenabled=false\n')
sock=run/'control.sock'
daemon=subprocess.Popen([str(binary),'app-server','--listen','unix://'+str(sock)],env=dict(os.environ,CODEX_HOME=str(home)),stdout=log,stderr=log,start_new_session=True)
owned.append(daemon)
deadline=time.monotonic()+15
while not sock.exists():
    assert daemon.poll() is None,(run/'bootstrap.log').read_text(errors='replace')[-1500:]
    assert time.monotonic()<deadline,'Codex 启动超时'
    time.sleep(.05)
state={'run':str(run),'socket':str(sock),'binary':str(binary),'architecture':platform.machine(),'version':version,'daemon_pid':daemon.pid,'mock_pid':mock.pid}
(run/'owned-processes.json').write_text(json.dumps(state))
atexit.unregister(abort)
print(json.dumps(state))
'''

CLEANUP = r'''
import json,os,shutil,signal,sys,time
from pathlib import Path
state=json.loads(sys.argv[1]);run=Path(state['run'])
assert str(run).startswith('/tmp/super-brain-aarch64-')
requests=[json.loads(line) for line in (run/'requests.jsonl').read_text().splitlines()] if (run/'requests.jsonl').exists() else []
for field in ['daemon_pid','mock_pid']:
    pid=state[field]
    try:
        args=Path('/proc/'+str(pid)+'/cmdline').read_bytes().decode(errors='replace')
        if str(run) in args:
            os.kill(pid,signal.SIGTERM)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                status=Path('/proc/'+str(pid)+'/stat')
                if not status.exists() or status.read_text().split(') ',1)[1].startswith('Z'):break
                time.sleep(.05)
            else:os.kill(pid,signal.SIGKILL);time.sleep(.2)
    except (FileNotFoundError,ProcessLookupError):pass
for attempt in range(5):
    try:shutil.rmtree(run);break
    except FileNotFoundError:break
    except OSError:
        if attempt==4:raise
        time.sleep(.2)
print(json.dumps({'requests':requests,'cleaned_run':str(run)}))
'''


async def remote(alias: str, code: str, argument: dict | None = None, timeout: float = 180) -> dict:
    wrapper = "import base64;exec(base64.b64decode(" + repr(base64.b64encode(code.encode()).decode()) + "))"
    command = ["python3", "-c", wrapper]
    if argument is not None:
        command.append(json.dumps(argument))
    process = await asyncio.create_subprocess_exec("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=yes", alias, shlex.join(command), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace")[-3500:])
    return json.loads(stdout)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="a3-21")
    args = parser.parse_args()
    state = None
    result = {"passed": False, "host_alias": args.host, "real_model_calls": 0,
              "isolation": "专用 Codex 配置、模拟模型、临时目录；保留现有安装和运行会话"}
    client = None
    try:
        state = await remote(args.host, BOOTSTRAP)
        result.update(architecture=state["architecture"], cli_version=state["version"])
        print("aarch64 测试端已启动，版本 " + state["version"], flush=True)
        client = await CodexClient.ssh(Host(name="aarch64 测试端", address=args.host, transport="stdio",
            codex_command=[state["binary"]], control_socket=state["socket"]))
        thread = (await client.call("thread/start", {"cwd": state["run"] + "/work", "model":"A2-dsv41f", "modelProvider":"mock"}))["thread"]
        thread_id = thread["id"]
        await client.call("thread/goal/set", {"threadId":thread_id,"objective":"验证 aarch64 的 goal 与压缩接续","origin":"user","status":"paused"})
        await client.call("turn/start", {"threadId":thread_id,"input":[{"type":"text","text":"返回 aarch64 测试进度，不调用工具。"}]})
        await client.wait_event("turn/completed", thread_id=thread_id)
        goal = (await client.call("thread/goal/get", {"threadId":thread_id}))["goal"]
        assert goal["objective"] == "验证 aarch64 的 goal 与压缩接续"
        await client.call("thread/settings/update", {"threadId":thread_id,"model":"ZL-DeepSeek-v4.1"})
        await client.call("thread/compact/start", {"threadId":thread_id})
        await client.wait_for(lambda e:e["method"]=="item/completed" and e.get("params",{}).get("item",{}).get("type")=="contextCompaction")
        await client.wait_event("turn/completed", thread_id=thread_id)
        await client.call("thread/settings/update", {"threadId":thread_id,"model":"A3-dsv41f"})
        await client.call("turn/start", {"threadId":thread_id,"input":[{"type":"text","text":"继续并保留 ARM-42 测试事实。"}]})
        await client.wait_event("turn/completed", thread_id=thread_id)
        result.update(passed=True, controlled_thread=thread_id, goal_observed=True, compaction_continuation=True)
    finally:
        if client:
            await client.close()
        if state:
            cleanup = await remote(args.host, CLEANUP, state, timeout=20)
            result["model_request_sequence"] = [item["model"] for item in cleanup["requests"]]
            result["test_processes_cleaned"] = True
        report = Path(".runtime/reports/aarch64.json")
        await asyncio.to_thread(report.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(report.write_text, json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__ == "__main__":
    asyncio.run(main())
