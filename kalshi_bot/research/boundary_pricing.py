"""Offline August-2026 audit of structural boundary pricing and its 60s clamp."""
from __future__ import annotations
import math, random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from kalshi_bot.research.opportunities import build_opportunities, number, reliability

START=datetime(2026,8,9,tzinfo=timezone.utc).timestamp(); END=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
FIELDS=("asset","ticker","window_id","window_id_ts","source_session","snapshot_ts","time_remaining","horizon_bin","yes_settled","actual_outcome","p_base","p_real","yes_price_raw","p_market","p_market_semantics","spot_now","price_to_beat","realized_vol","sigma_structural","z_threshold","raw_distance","relative_distance","log_distance","z_clamped","z_unclamped","p_unclamped","d_brier_unclamped","yes_bid","yes_ask","kalshi_spread","quote_age","spot_return_1s","spot_confidence","dislocation","per_venue_mids","n_venues")
def finite(v): return number(v)
def same(a,b):
 a,b=finite(a),finite(b)
 return (a is None and b is None) or (a is not None and b is not None and abs(a-b)<=1e-9)
def mean(x):
 x=[v for v in x if v is not None]; return sum(x)/len(x) if x else None
def ll(p,y):
 p=min(1-1e-12,max(1e-12,p)); return -(math.log(p) if y else math.log(1-p))
def cdf(z): return .5*(1+math.erf(z/math.sqrt(2)))
def bucket_tte(v):
 v=finite(v)
 if v is None:return None
 return "60-119s" if v<120 else ">=120s"
def structural(spot,strike,sigma,tau):
 if None in (spot,strike,sigma,tau) or spot<=0 or strike<=0 or sigma<=0 or tau<=0:return (None,None)
 t=tau/(365*24*3600); z=(math.log(spot/strike)-.5*sigma*sigma*t)/(sigma*math.sqrt(t)); return cdf(z),z
def raw_index(decisions):
 index=defaultdict(list)
 for d in decisions:
  window=finite(d.get("window_id_ts"))
  if d.get("asset") and window is not None:index[(d.get("asset"),int(window),str(d.get("ts")))].append(d)
 return index
def selected_raw(index,opportunity):
 return (index.get((opportunity.get("asset"),int(opportunity["window_id_ts"]),str(opportunity.get("snapshot_ts")))) or [{}])[-1]
def build_boundary_dataset(sessions):
 kept={}; conflicts=set(); duplicates=0; names=[]; tte=[]
 for s in sorted(sessions,key=lambda x:str(x["name"])):
  names.append(str(s["name"])); opps,_=build_opportunities(s.get("decisions",[]),s.get("windows",[]),s.get("trades",[]))
  index=raw_index(s.get("decisions",[]))
  for o in opps:
   w=finite(o.get("window_id_ts"))
   if o.get("snapshot_selection")!="preferred_ge_60s" or w is None or not START<=w<END:continue
   raw=selected_raw(index,o); row=enrich(o,raw,str(s["name"])); key=(row["asset"],row["window_id_ts"])
   if key in conflicts:continue
   old=kept.get(key)
   if old is None:kept[key]=row
   elif old["actual_outcome"]==row["actual_outcome"] and same(old["p_base"],row["p_base"] ) and same(old.get("p_real"),row.get("p_real")):duplicates+=1
   else:kept.pop(key,None);conflicts.add(key)
  tte.extend(tte_rows(s.get("decisions",[]),s.get("windows",[]),str(s["name"])))
 return sorted(kept.values(),key=lambda r:(r["window_id_ts"],r["asset"])),{"sessions_read":names,"duplicate_keys":duplicates,"conflicting_keys":len(conflicts),"tte":tte}
def enrich(o,raw,name):
 spot,strike,vol=finite(raw.get("spot_now") if raw else o.get("spot_now")),finite(raw.get("price_to_beat") if raw else o.get("price_to_beat")),finite(raw.get("realized_vol"))
 sigma=max(vol,.15) if vol is not None else None; t=finite(o.get("time_remaining")); pu,zu=structural(spot,strike,sigma,t); pc,zc=structural(spot,strike,sigma,max(t,60) if t is not None else None); y=int(o["yes_settled"]); pm=finite(o.get("p_market")); action=raw.get("action")
 r={f:o.get(f) for f in FIELDS}; r.update({"source_session":name,"window_id_ts":int(o["window_id_ts"]),"snapshot_ts":o.get("snapshot_ts"),"time_remaining":t,"horizon_bin":bucket_tte(t),"p_base":finite(o.get("p_base")),"yes_price_raw":finite(o.get("yes_price_raw")),"p_market":pm,"yes_settled":y,"actual_outcome":o["actual_outcome"],"spot_now":spot,"price_to_beat":strike,"realized_vol":vol,"sigma_structural":sigma,"z_threshold":raw.get("z_threshold"),"spot_confidence":raw.get("spot_confidence"),"z_clamped":zc,"z_unclamped":zu,"p_unclamped":pu,"raw_distance":spot-strike if spot and strike else None,"relative_distance":(spot-strike)/strike if spot and strike else None,"log_distance":math.log(spot/strike) if spot and strike else None,"p_market_semantics":"raw_WAIT" if action=="WAIT" else "raw_fill_quota" if str(action).startswith("BUY") and str(raw.get("strategy") or "")=="fill_quota" else "smoothed_buy_decision" if str(action).startswith("BUY") else "unknown","n_venues":len(raw.get("per_venue_mids")) if isinstance(raw.get("per_venue_mids"),dict) else None})
 r["d_brier_unclamped"]=(pu-y)**2-(r["p_base"]-y)**2 if pu is not None and r["p_base"] is not None else None; return r
def tte_rows(decisions,windows,name,tolerance=3):
 outcomes={(w.get("asset"),int(finite(w.get("window_id_ts")) or -1)):str(w.get("actual_outcome","")).upper() for w in windows if START<=int(finite(w.get("window_id_ts")) or -1)<END}; kept={}
 for d in decisions:
  k=(d.get("asset"),int(finite(d.get("window_id_ts")) or -1)); t=finite(d.get("time_remaining"))
  if k not in outcomes or outcomes[k] not in {"YES","NO"} or t is None or finite(d.get("p_real")) is None:continue
  for target in (120,90,60,30,15):
   if not target <= t <= target+tolerance:continue
   prior=kept.get((k,target))
   if prior is None or t < finite(prior[0].get("time_remaining")):
    kept[(k,target)]=(d,target,name)
 return list(kept.values())
def score(rows,field):
 v=[(finite(r.get(field)),int(r["yes_settled"])) for r in rows if finite(r.get(field)) is not None]; return {"n":len(v),"brier":mean((p-y)**2 for p,y in v),"logloss":mean(ll(p,y) for p,y in v),"mean_p":mean(p for p,y in v),"yes":mean(y for p,y in v),"cal":mean(p for p,y in v)-mean(y for p,y in v) if v else None}
def ci(rows,metric):
 c=defaultdict(list)
 for r in rows:
  v=finite(r.get(metric));
  if v is not None:c[r["window_id_ts"]].append(v)
 g=list(c.values());
 if not g:return (None,None)
 rng=random.Random(0); a=[]
 for _ in range(300):
  x=[v for _ in g for v in rng.choice(g)];a.append(sum(x)/len(x))
 a.sort();return a[7],a[291]
def fmt(v):return "n/a" if v is None else f"{v:.4f}"
def report(rows,cov,source):
 base=score(rows,"p_base"); raw=[r for r in rows if r["p_base"] is not None and r["yes_price_raw"] is not None]; unclamped=[r for r in rows if r["d_brier_unclamped"] is not None]; dates=[datetime.fromtimestamp(r["window_id_ts"],tz=timezone.utc).date().isoformat() for r in rows]
 lines=["BOUNDARY PRICING — AUGUST 9–31 2026 DEVELOPMENT SAMPLE",f"Source: {source}","Forecast quality ≠ theoretical edge ≠ executable edge ≠ captured P&L. Descriptive research only; no production parameter recommendation or hypothetical P&L.","",f"INTEGRITY sessions={len(cov['sessions_read'])} preferred={len(rows)} unique_keys={len(rows)} duplicates={cov['duplicate_keys']} conflicts={cov['conflicting_keys']} dates={min(dates) if dates else 'n/a'}..{max(dates) if dates else 'n/a'} YES={fmt(mean(r['yes_settled'] for r in rows))}","assets: "+", ".join(f"{k}={v}" for k,v in sorted(Counter(r['asset'] for r in rows).items())),f"PRODUCTION p_base N={base['n']} Brier={fmt(base['brier'])} logloss={fmt(base['logloss'])} mean_p={fmt(base['mean_p'])} observed_YES={fmt(base['yes'])} calibration-in-large={fmt(base['cal'])}"]
 rawb=score(raw,"p_base"); rawq=score(raw,"yes_price_raw"); lines+= [f"RAW yes_price_raw matched N={len(raw)} p_base Brier={fmt(rawb['brier'])} logloss={fmt(rawb['logloss'])} raw Brier={fmt(rawq['brier'])} logloss={fmt(rawq['logloss'])} deltaBrier={fmt((rawq['brier']-rawb['brier']) if rawq['brier'] is not None else None)} deltaLogLoss={fmt((rawq['logloss']-rawb['logloss']) if rawq['logloss'] is not None else None)}; this is a forecast comparison only, not evidence that p_base economically beats Kalshi. Raw is not universally executable."]
 for sem in ("raw_WAIT","raw_fill_quota","smoothed_buy_decision","unknown"): lines.append(f"p_market {sem}: {score([r for r in rows if r['p_market_semantics']==sem],'p_market')}")
 us=score(unclamped,"p_unclamped"); ps=score(unclamped,"p_base"); lines+= [f"UNCLAMPED matched N={len(unclamped)} production Brier={fmt(ps['brier'])} unclamped Brier={fmt(us['brier'])} dBrier={fmt(mean(r['d_brier_unclamped'] for r in unclamped))} CI={ci(unclamped,'d_brier_unclamped')}. Sigma coverage={sum(r['sigma_structural'] is not None for r in rows)}/{len(rows)}; no 0.3 imputation; August cannot evaluate clamp removal when N=0."]
 lines+=["FEATURE COVERAGE — selected raw-tick copied fields unless marked derived; unavailable means absent from the selected raw tick and not reconstructed"]
 for f in ("spot_now","price_to_beat","time_remaining","p_base","z_threshold","realized_vol","yes_price_raw","p_market","yes_bid","yes_ask","kalshi_spread","quote_age","spot_return_1s","spot_confidence","dislocation","per_venue_mids"):
  state="derived/reconstructed" if f in {"p_base","yes_price_raw","p_market"} else "raw copied" if f in {"z_threshold","spot_confidence"} else "raw selected-tick"
  lines.append(f"{f} ({state}): {sum(r.get(f) is not None for r in rows)}/{len(rows)}")
 lines.append(f"sigma_structural (derived from realized_vol; unavailable when realized_vol is absent): {sum(r.get('sigma_structural') is not None for r in rows)}/{len(rows)}")
 lines+=["STRUCTURAL ERROR MAP — low/high partitions are value tertiles, fixed before outcome review"]
 for f in ("p_base","log_distance","time_remaining","z_clamped","realized_vol"):
  vals=sorted(finite(r.get(f)) for r in rows if finite(r.get(f)) is not None)
  if not vals:continue
  for label,grp in (("low",[r for r in rows if finite(r.get(f)) is not None and finite(r[f])<=vals[(len(vals)-1)//3]]),("high",[r for r in rows if finite(r.get(f)) is not None and finite(r[f])>=vals[2*(len(vals)-1)//3]])):
   s=score(grp,"p_base");lines.append(f"{f} {label}: N={s['n']} mean_p={fmt(mean(finite(r.get('p_base')) for r in grp))} YES={fmt(mean(r['yes_settled'] for r in grp))} cal={fmt(s['cal'])} Brier={fmt(s['brier'])} logloss={fmt(s['logloss'])}")
 lines += ["TTE COVERAGE secondary/dependent; tolerance=3s; pre-horizon only target <= TTE <= target+tolerance; omitted when no valid pre-horizon tick exists"]
 for t in (120,90,60,30,15):
  x=[d for d,target,n in cov['tte'] if target==t];parents={(d.get('asset'),d.get('window_id_ts')) for d in x};den=len(rows);coverage=(100*len(parents)/den) if den else None;omitted=max(0,den-len(parents));post=sum(finite(d.get('time_remaining')) < t for d in x);lines.append(f"TTE {t}: snapshots={len(x)} parent_windows={len(parents)} coverage={fmt(coverage)}% omitted_no_valid_pre={omitted} post_horizon={post} p_base={sum(finite(d.get('p_base')) is not None for d in x)} S/K={sum(finite(d.get('spot_now')) is not None and finite(d.get('price_to_beat')) is not None for d in x)} sigma={sum(finite(d.get('realized_vol')) is not None for d in x)} quotes={sum(d.get('yes_bid') is not None or d.get('yes_ask') is not None for d in x)}")
 lines += ["RESEARCH_STORE_V2_REQUIREMENTS","MUST HAVE: raw/unclamped time, tau used, realized_vol, structural sigma, spot_now, price_to_beat, standardized TTE, yes bid/ask/spread, quote age, p_market semantics, venue count, dislocation.","NICE TO HAVE: z_threshold and spot_confidence (historically available on 840 selected raw ticks but useful for a stable prospective schema), short/long vol, vol acceleration, boundary momentum/crossing metrics, time above boundary, market factor, Kalshi depth.","P7 reusable variables: realized volatility, venue dispersion/count, spread/liquidity, quote staleness, TTE, time of day, market-wide movement. Missing August coverage limits regime inference.","LIMITATIONS: repeated TTE rows are dependent; outcome is spot_vs_price_to_beat, not official settlement; missing sigma prevents historical clamp reconstruction."]
 return "\n".join(lines)+"\n"
