"""Save and verify code/config rollback points; never restore live membership implicitly."""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, sqlite3
from datetime import datetime, timezone
from pathlib import Path

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def snapshot(destination):
    if destination.exists():
        raise RuntimeError("snapshot destination must be new")
    local=Path(os.environ["LOCALAPPDATA"])
    obs=local/"GPTObservatory"
    dog=local/"chat-watchdog"
    side=local/"Conversation Sidecar"
    destination.mkdir(parents=True)
    manifest={"createdAt":datetime.now(timezone.utc).isoformat(),"host":os.environ.get("COMPUTERNAME"),"codeOnlyRollback":True,"files":{},"sources":{}}
    selections={
      "observatory-server":obs/"app"/"current"/"server",
      "observatory-dist":obs/"app"/"current"/"dist",
      "observatory-package.json":obs/"app"/"current"/"package.json",
      "observatory-package-lock.json":obs/"app"/"current"/"package-lock.json",
      "observatory-install.json":obs/"install.json",
      "watchdog-start.cmd":dog/"start-current-watchdog.cmd",
      "sidecar-runtime.json":side/"runtime.json",
    }
    for name,source in selections.items():
        if not source.exists(): raise RuntimeError(f"missing baseline: {source}")
        target=destination/name
        if source.is_dir(): shutil.copytree(source,target)
        else: shutil.copy2(source,target)
        manifest["sources"][name]=str(source)
    registry=dog/"registry-v2.sqlite3"
    with sqlite3.connect(registry.as_uri()+"?mode=ro",uri=True,timeout=10) as src:
        with sqlite3.connect(destination/"watchdog-registry.sqlite3") as dst:
            src.backup(dst)
    manifest["registryBackupPolicy"]="Disaster recovery only; do not restore for a code rollback, as desired membership may have changed."
    manifest["watchdogCodeBaseline"]="rollback/watchdog-pre-quiet-20261002 (7052c11)"
    manifest["observatoryCodeBaseline"]="rollback/observatory-pre-quiet-20261002 (6455cf8)"
    manifest["sidecarCodeBaseline"]="rollback/sidecar-pre-quiet-20261002 (d50c48e6)"
    manifest["sidecarImmutableRelease"]=json.loads((side/"runtime.json").read_text(encoding="utf-8"))["current_release"]
    for path in destination.rglob("*"):
        if path.is_file(): manifest["files"][path.relative_to(destination).as_posix()]=digest(path)
    (destination/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    return verify(destination)

def verify(destination):
    manifest=json.loads((destination/"manifest.json").read_text(encoding="utf-8"))
    mismatches=[name for name,wanted in manifest["files"].items() if not (destination/name).is_file() or digest(destination/name)!=wanted]
    with sqlite3.connect((destination/"watchdog-registry.sqlite3").as_uri()+"?mode=ro",uri=True) as db:
        integrity=db.execute("PRAGMA integrity_check").fetchone()[0]
    if mismatches or integrity!="ok": raise RuntimeError(f"invalid snapshot: {mismatches}; sqlite={integrity}")
    return {"snapshot":str(destination),"verifiedFiles":len(manifest["files"]),"sqliteIntegrity":integrity,"codeOnlyRollback":True}

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("mode",choices=["snapshot","verify"])
    parser.add_argument("directory",type=Path)
    args=parser.parse_args()
    print(json.dumps((snapshot if args.mode=="snapshot" else verify)(args.directory),ensure_ascii=True))
