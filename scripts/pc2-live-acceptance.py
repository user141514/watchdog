"""Task-owned browser read-back; no prompts or production registrations."""
import argparse, json, sys, urllib.request, websocket
from chat_watchdog.relay_cdp import RelayCdpProtocol
URL = "https://chatgpt.com/g/g-p-6a983ccfa9148191b42da3db5412f946-subagents/project"
parser=argparse.ArgumentParser()
parser.add_argument("mode",choices=["open","inspect","strength"])
parser.add_argument("--level",default="Medium")
parser.add_argument("--url",default=URL)
parser.add_argument("--tab-id",type=int)
args=parser.parse_args()
URL=args.url
def request(path,payload):
    req=urllib.request.Request("http://127.0.0.1:7337"+path,data=json.dumps(payload).encode(),headers={"content-type":"application/json"},method="POST")
    with urllib.request.urlopen(req,timeout=30) as response: return json.load(response)
if args.mode=="strength":
    print(json.dumps(request("/mcp",{"jsonrpc":"2.0","id":"quiet-gate-"+args.level,"method":"tools/call","params":{"name":"webgpt_shift_test","arguments":{"target":args.level,**({"target_tab_id":args.tab_id} if args.tab_id else {"target_url":URL})}}}),ensure_ascii=True))
    sys.exit()
if args.mode=="open":
    if not URL.startswith("https://chatgpt.com/g/g-p-") or not URL.rstrip("/").endswith("/project"):
        parser.error("open requires an explicit ChatGPT Project URL; task conversations belong in subagents")
    reply=request("/mcp",{"jsonrpc":"2.0","id":"quiet-project-create","method":"tools/call","params":{"name":"conversation_create","arguments":{"project_url":URL}}})
    result=reply.get("result",{})
    if reply.get("error") or result.get("isError"):
        raise RuntimeError(json.dumps(reply,ensure_ascii=False))
    created=json.loads(result["content"][0]["text"])
    print(json.dumps(created,ensure_ascii=False))
    sys.exit()
with urllib.request.urlopen("http://127.0.0.1:9224/json/version",timeout=5) as r: version=json.load(r)
socket=websocket.create_connection(version["webSocketDebuggerUrl"],timeout=5,suppress_origin=True)
p=RelayCdpProtocol(socket,request_timeout=10)
try:
    if args.mode=="inspect":
        with urllib.request.urlopen("http://127.0.0.1:9224/json/list",timeout=5) as r: targets=json.load(r)
        matches=[t for t in targets if (t["id"]=="PAGE"+str(args.tab_id) if args.tab_id else t.get("url","")==URL)]
        if len(matches)!=1: raise RuntimeError("dedicated test target missing or ambiguous")
        sid=p.attach_target(matches[0]["id"])
        try:
            print(json.dumps(p.evaluate(sid,r"""(() => {
              const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
              const buttons=[...document.querySelectorAll('button')].filter(visible).map(e=>({text:e.innerText,label:e.getAttribute('aria-label'),pressed:e.getAttribute('aria-pressed'),state:e.getAttribute('data-state')}));
              const sliders=[...document.querySelectorAll('[role=slider],input[type=range]')].filter(visible).map(e=>({label:e.getAttribute('aria-label'),value:e.getAttribute('aria-valuenow')||e.value,min:e.getAttribute('aria-valuemin')||e.min,max:e.getAttribute('aria-valuemax')||e.max}));
              return {url:location.href,title:document.title,buttons,sliders,body:document.body.innerText.slice(-600)};
            })()"""),ensure_ascii=False))
        finally:
            p.command("Target.detachFromTarget",{"sessionId":sid})
finally:
    socket.close()
