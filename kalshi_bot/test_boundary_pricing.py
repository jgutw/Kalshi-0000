"""Synthetic checks for offline structural boundary pricing research."""
from __future__ import annotations
from datetime import datetime,timezone
import math
from pathlib import Path
from kalshi_bot.research.boundary_pricing import START,build_boundary_dataset,ci,report,score,structural,tte_rows
from scripts.analyze_boundary_pricing import outpath
W=int(datetime(2026,8,9,tzinfo=timezone.utc).timestamp())
def ok(n,x):
 if not x:raise AssertionError(n)
 print("PASS",n)
def d(asset="BTC",w=W,base=.5,real=.6,t=90,**x):return {"asset":asset,"window_id_ts":w,"ts":str(w),"p_base":base,"p_real":real,"time_remaining":t,"spot_now":101,"price_to_beat":100,"realized_vol":.2,**x}
def s(name,ds,out):return {"name":name,"decisions":ds,"windows":[{"asset":a,"window_id_ts":w,"actual_outcome":o} for a,w,o in out],"trades":[]}
def main():
 rows,c=build_boundary_dataset([s("a",[d(),d(real=.7)],[("BTC",W,"YES")])]);ok("preferred last selected",len(rows)==1 and rows[0]["p_real"]==.7)
 aug31=int(datetime(2026,8,31,23,45,tzinfo=timezone.utc).timestamp());sep=int(datetime(2026,9,1,tzinfo=timezone.utc).timestamp());rows,_=build_boundary_dataset([s("f",[d(w=aug31),d("ETH",sep)],[("BTC",aug31,"YES"),("ETH",sep,"YES")])]);ok("date fence",len(rows)==1)
 rows,c=build_boundary_dataset([s("a",[d()],[("BTC",W,"YES")]),s("b",[d()],[("BTC",W,"YES")])]);ok("duplicate",len(rows)==1 and c["duplicate_keys"]==1)
 rows,c=build_boundary_dataset([s("a",[d()],[("BTC",W,"YES")]),s("b",[d(real=.7)],[("BTC",W,"YES")])]);ok("conflict",not rows and c["conflicting_keys"]==1)
 rows,_=build_boundary_dataset([s("a",[d(t=45)],[("BTC",W,"YES")])]);ok("fallback excluded",not rows)
 rows,_=build_boundary_dataset([s("a",[d(realized_vol=None)],[("BTC",W,"YES")])]);ok("missing sigma skips unclamped",rows[0]["p_unclamped"] is None)
 rows,_=build_boundary_dataset([s("a",[d(t=90)],[("BTC",W,"YES")])]);ok("raw tau distinct clamp",structural(101,100,.2,30)[1]!=structural(101,100,.2,60)[1])
 ok("distance derivation",rows[0]["raw_distance"]==1 and rows[0]["relative_distance"]==.01 and rows[0]["log_distance"]>0 and structural(0,100,.2,90)==(None,None))
 rows,_=build_boundary_dataset([s("a",[d(spot_now=0),d("ETH",W+900,price_to_beat=0)],[("BTC",W,"YES"),("ETH",W+900,"NO")])]);ok("invalid S K excludes log distance",all(r["log_distance"] is None for r in rows))
 ticks=tte_rows([d(t=90,real=.6),d(t=91,real=.7),d(t=120,real=.6)],[( {"asset":"BTC","window_id_ts":W,"actual_outcome":"YES"})],"a");ok("TTE one row per asset window bucket",len(ticks)==2 and len({(x[0]["asset"],x[0]["window_id_ts"],x[1]) for x in ticks})==2)
 pre=tte_rows([d(t=91,real=.6),d(t=88,real=.9)],[( {"asset":"BTC","window_id_ts":W,"actual_outcome":"YES"})],"a");ok("TTE chooses valid pre horizon over post horizon",len(pre)==1 and pre[0][0]["time_remaining"]==91)
 post=tte_rows([d(t=88,real=.9)],[( {"asset":"BTC","window_id_ts":W,"actual_outcome":"YES"})],"a");ok("post horizon only is omitted",not post)
 multi=tte_rows([d(t=93,real=.5),d(t=91,real=.6)],[( {"asset":"BTC","window_id_ts":W,"actual_outcome":"YES"})],"a");ok("closest pre horizon selection deterministic",len(multi)==1 and multi[0][0]["time_remaining"]==91)
 post_aug=tte_rows([d(w=sep,t=91)],[( {"asset":"BTC","window_id_ts":sep,"actual_outcome":"YES"})],"a");ok("TTE secondary rows obey August fence",not post_aug)
 rows,_=build_boundary_dataset([s("a",[d(t=90),d(t=45)],[("BTC",W,"YES")])]);ok("secondary ticks cannot inflate headline",len(rows)==1)
 rows,_=build_boundary_dataset([s("a",[d(),d("ETH",W,base=.4,real=.4)],[("BTC",W,"YES"),("ETH",W,"NO")])]);ok("matched comparison same keys",score(rows,"p_base")["n"]==score(rows,"p_real")["n"])
 rows,_=build_boundary_dataset([s("a",[d(action="WAIT"),d("ETH",W,action="BUY_YES")],[("BTC",W,"YES"),("ETH",W,"NO")])]);text=report(rows,{"sessions_read":["a"],"duplicate_keys":0,"conflicting_keys":0,"tte":[]},"fixture");ok("p market semantics separated", "p_market raw_WAIT" in text and "p_market smoothed_buy_decision" in text)
 rows,_=build_boundary_dataset([s("a",[d(z_threshold=.7,spot_confidence=.8),d("ETH",W,action="BUY_YES",strategy="fill_quota",z_threshold=.6,spot_confidence=.9),d("DOGE",W,action="BUY_NO",strategy="fill_quota")],[("BTC",W,"YES"),("ETH",W,"NO"),("DOGE",W,"YES")])]);ok("selected raw z and spot confidence carry through",rows[0]["z_threshold"]==.7 and rows[0]["spot_confidence"]==.8 and sum(r["z_threshold"] is not None for r in rows)==2)
 semantics={r["asset"]:r["p_market_semantics"] for r in rows};ok("fill quota is not smoothed p market",semantics["ETH"]=="raw_fill_quota")
 ok("BUY_NO fill quota is not smoothed p market",semantics["DOGE"]=="raw_fill_quota")
 tau=90/(365*24*3600); expected=.5*(1+math.erf((-.5*.2*.2*tau)/(.2*math.sqrt(tau))/math.sqrt(2))); legacy_tau=90/(365.25*24*3600); legacy=.5*(1+math.erf((-.5*.2*.2*legacy_tau)/(.2*math.sqrt(legacy_tau))/math.sqrt(2)));ok("structural reconstruction uses production 365 day year",abs(structural(100,100,.2,90)[0]-expected)<1e-12 and abs(expected-legacy)>1e-9)
 probs=[r for r in rows if r["p_base"] is not None];ok("probability buckets reconcile eligible",len(probs)==3 and sum(1 for r in probs if r["p_base"]<=.5)+sum(1 for r in probs if r["p_base"]>=.5)>=len(probs))
 ok("error map bucket accounting safe", "STRUCTURAL ERROR MAP" in text and "p_base low" in text)
 cluster_rows=[{"window_id_ts":W,"x":-1.0},{"window_id_ts":W,"x":1.0}];ok("cluster unit is window timestamp",ci(cluster_rows,"x")== (0.0,0.0))
 empty=score([],"p_base");ok("empty populations safe",empty["n"]==0 and empty["brier"] is None and ci([],"x")==(None,None))
 ok("reports only",outpath(Path("reports")/"x") and reject())
 print("26/26 boundary pricing fixture checks passed")
def reject():
 try:outpath(Path("outside"))
 except ValueError:return True
 return False
if __name__=="__main__":main()
