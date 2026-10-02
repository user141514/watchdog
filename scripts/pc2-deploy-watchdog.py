"""Export one clean Git commit as an immutable runtime, then activate without changing membership."""
from __future__ import annotations
import argparse, io, json, os, shutil, subprocess, time, urllib.request, zipfile
from pathlib import Path

def health():
    try:
        with urllib.request.urlopen("http://127.0.0.1:9235/health",timeout=2) as response:
            return json.load(response)
    except Exception:
        return None

def prepare(root):
    subprocess.run(["git","diff","--quiet"],cwd=root,check=True)
    subprocess.run(["git","diff","--cached","--quiet"],cwd=root,check=True)
    untracked=subprocess.check_output(["git","ls-files","--others","--exclude-standard"],cwd=root,text=True).strip()
    if untracked: raise RuntimeError("untracked files must be reviewed and preserved before exporting a release")
    commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip()
    runtime=Path(os.environ["LOCALAPPDATA"])/"chat-watchdog"
    target=runtime/"releases"/commit
    payload=subprocess.check_output(["git","archive","--format=zip",commit],cwd=root)
    if not target.exists():
        staging=runtime/"releases"/(commit+".staging")
        if staging.exists(): raise RuntimeError("stale staging directory requires inspection")
        staging.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for name in archive.namelist():
                relative=Path(name)
                if relative.is_absolute() or ".." in relative.parts: raise RuntimeError("unsafe Git export path")
            archive.extractall(staging)
        staging.rename(target)
    if not (target/"chat_watchdog"/"registry.py").is_file():
        raise RuntimeError("invalid exported runtime")
    # A directory named after a SHA is not evidence that its bytes still match.
    # Existing releases may contain normal Python caches, but every tracked
    # source file must equal this exact Git export; never hot-repair a release.
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.infolist():
            if member.is_dir(): continue
            path=target/member.filename
            if not path.is_file() or path.read_bytes()!=archive.read(member):
                raise RuntimeError("release content does not match Git commit: "+member.filename)
    return commit,target

def activate(root):
    if health() is not None: raise RuntimeError("stop the exact old Watchdog instance before activation")
    runtime=Path(os.environ["LOCALAPPDATA"])/"chat-watchdog"
    commit,target=prepare(root)
    python=runtime/"venv"/"Scripts"/"python.exe"
    launcher=runtime/"start-current-watchdog.cmd"
    store=runtime/"registry-v2.sqlite3"
    if launcher.exists():
        backup=runtime/("start-current-watchdog.before-"+commit+".cmd")
        if not backup.exists(): shutil.copy2(launcher,backup)
    launcher.write_text(
        '@echo off\ncd /d "'+str(target)+'"\n"'+str(python)+
        '" -m chat_watchdog --simple --registry-port 9235 --registry-store "'+
        str(store)+'" --simple-interval-seconds 15\n',encoding="utf-8")
    with (runtime/"quiet-watchdog.stdout.log").open("ab") as stdout, (runtime/"quiet-watchdog.stderr.log").open("ab") as stderr:
        subprocess.Popen(["cmd.exe","/d","/s","/c",str(launcher)],cwd=runtime,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP)
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        current=health()
        if current:
            actual=Path(current.get("module_path","")).resolve()
            if not actual.is_relative_to(target.resolve()):
                raise RuntimeError("listener belongs to a different runtime")
            if Path(current.get("store_path","")).resolve()!=store.resolve():
                raise RuntimeError("listener belongs to a different registry")
            if current.get("ready") is True:
                return {"commit":commit,"runtime":str(target),"health":current}
        time.sleep(0.25)
    raise RuntimeError("new runtime did not become ready; retain rollback launcher and inspect stderr")

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("mode",choices=["prepare","activate"])
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    if args.mode=="prepare":
        commit,target=prepare(root)
        result={"commit":commit,"runtime":str(target)}
    else: result=activate(root)
    print(json.dumps(result,ensure_ascii=True))
