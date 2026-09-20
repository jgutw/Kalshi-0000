#!/usr/bin/env python3
"""Run offline August structural boundary pricing research."""
from __future__ import annotations
import argparse,csv,json,sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from kalshi_bot.research.boundary_pricing import FIELDS,build_boundary_dataset,report
def load(path):
 if not path.exists():return []
 out=[]
 for line in path.read_text(encoding="utf-8").splitlines():
  try:
   x=json.loads(line)
   if isinstance(x,dict):out.append(x)
  except json.JSONDecodeError:pass
 return out
def outpath(path):
 root=(ROOT/"reports").resolve();candidate=path.resolve()
 if candidate!=root and root not in candidate.parents:raise ValueError("outputs must be under reports/")
 return candidate
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument("sessions",nargs="*",type=Path);p.add_argument("--out-dir",type=Path,default=ROOT/"reports");a=p.parse_args();paths=a.sessions or sorted(x for x in (ROOT/"sessions").iterdir() if x.is_dir())
 sessions=[{"name":x.name,"decisions":load(x/"kalshi_decisions.jsonl"),"windows":load(x/"kalshi_windows.jsonl"),"trades":load(x/"kalshi_trades.jsonl")} for x in paths];rows,cov=build_boundary_dataset(sessions);text=report(rows,cov,", ".join(str(x) for x in paths));dest=outpath(a.out_dir);dest.mkdir(parents=True,exist_ok=True);stem="boundary_pricing_"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
 (dest/f"{stem}.txt").write_text(text,encoding="utf-8")
 with (dest/f"{stem}.jsonl").open("w",encoding="utf-8") as f:
  for r in rows:f.write(json.dumps(r,default=str,sort_keys=True)+"\n")
 with (dest/f"{stem}.csv").open("w",newline="",encoding="utf-8") as f:
  w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore");w.writeheader();w.writerows(rows)
 print(text,end="");print("Outputs written:",dest/f"{stem}.txt")
if __name__=="__main__":main()
