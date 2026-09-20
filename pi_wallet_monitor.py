import os, json, time, requests, pandas as pd
from datetime import datetime, timezone
from twilio.rest import Client

HORIZON_URL='https://api.mainnet.minepi.com'
STATE_FILE='wallet_state.json'
GROUPS={'WC':'pi_wallets_wc.csv','WCD':'pi_wallets_wcd.csv','RP':'pi_wallets_rp.csv','LP':'pi_wallets_lp.csv'}

def iso_ts(v):
    try:return int(datetime.fromisoformat(str(v).replace('Z','+00:00')).timestamp())
    except:return None

def find_times(p, created=None, neg=False):
    out=[]
    if not isinstance(p,dict): return out
    if 'not' in p: out += find_times(p['not'],created,not neg)
    for k in ('abs_before','absBefore','abs_before_epoch','absBeforeEpoch'):
        if k in p and neg:
            try:t=int(p[k])
            except:t=iso_ts(p[k])
            if t is not None: out.append(t)
    for k in ('rel_before','relBefore'):
        if k in p and neg and created is not None:
            try: out.append(created+int(p[k]))
            except: pass
    for k in ('and','or'):
        if isinstance(p.get(k),list):
            for child in p[k]: out += find_times(child,created,neg)
    return out

def get_json(url,**kwargs):
    r=requests.get(url,timeout=30,headers={'Accept':'application/json'},**kwargs)
    if r.status_code==404:return None
    r.raise_for_status(); return r.json()

def check_wallet(address):
    acct=get_json(f'{HORIZON_URL}/accounts/{address}')
    if acct is None:return None
    available=0.0
    for b in acct.get('balances',[]):
        if b.get('asset_type')=='native': available=float(b.get('balance',0)); break
    data=get_json(f'{HORIZON_URL}/claimable_balances',params={'claimant':address,'limit':200}) or {}
    now=int(datetime.now(timezone.utc).timestamp()); locks=[]
    for cb in data.get('_embedded',{}).get('records',[]):
        if cb.get('asset','native')!='native':continue
        try:amount=float(cb.get('amount',0))
        except:continue
        created=None
        for f in ('last_modified_time','created_at','created_time'):
            if cb.get(f):
                created=iso_ts(cb[f])
                if created is not None:break
        for claimant in cb.get('claimants',[]):
            if claimant.get('destination')!=address:continue
            future=[t for t in find_times(claimant.get('predicate',{}),created) if t>now]
            if future: locks.append((cb.get('id',''),amount,min(future)))
    unique={x[0] or f'{x[1]}-{x[2]}':x for x in locks}; locks=list(unique.values())
    locks.sort(key=lambda x:x[2])
    return {'available':available,'locked':sum(x[1] for x in locks),'next_unlock':locks[0][2] if locks else None}

def load_state():
    try:
        with open(STATE_FILE,encoding='utf-8') as f:s=json.load(f)
    except:s={}
    s.setdefault('wallets',{}); s.setdefault('unlock_alerts',{}); return s

def save_state(s):
    with open(STATE_FILE,'w',encoding='utf-8') as f:json.dump(s,f,indent=2,sort_keys=True)

def send_whatsapp(body):
    c=Client(os.environ['TWILIO_ACCOUNT_SID'],os.environ['TWILIO_AUTH_TOKEN'])
    m=c.messages.create(body=body,from_='whatsapp:'+os.environ['TWILIO_WHATSAPP_FROM'],to='whatsapp:'+os.environ['TWILIO_WHATSAPP_TO'])
    print('Sent',m.sid)

def wallets(path):
    df=pd.read_csv(path); df.columns=[str(c).strip().lower() for c in df.columns]
    if 'address' not in df.columns: raise ValueError(path+' needs address column')
    if 'name' not in df.columns: df['name']=[f'Wallet {i+1}' for i in range(len(df))]
    df['address']=df['address'].fillna('').astype(str).str.strip().str.upper()
    df['name']=df['name'].fillna('').astype(str).str.strip()
    return df[df['address'].str.startswith('G') & (df['address'].str.len()==56)].drop_duplicates('address')

def main():
    state=load_state(); now=datetime.now(timezone.utc); alerts=[]
    for group,path in GROUPS.items():
        if not os.path.exists(path): print('Missing',path); continue
        for _,row in wallets(path).iterrows():
            name,address=row['name'],row['address']
            try:r=check_wallet(address)
            except Exception as e: print('ERROR',group,name,e); continue
            if r is None: continue
            cur=round(r['available'],7); old=state['wallets'].get(address)
            if old is not None:
                prev=float(old.get('available',0))
                if cur>prev+1e-7:
                    alerts.append(f'🔔 Pi balance increased\nGroup: {group}\nWallet: {name}\nAddress: {address[:8]}...{address[-6:]}\nPrevious: {prev:.7f} Pi\nCurrent: {cur:.7f} Pi\nIncrease: +{cur-prev:.7f} Pi')
            u=r['next_unlock']
            if u:
                left=u-int(now.timestamp())
                key=f'{address}:{u}'
                if 24*3600 <= left < 48*3600 and key not in state['unlock_alerts']:
                    dt=datetime.fromtimestamp(u,tz=timezone.utc)
                    alerts.append(f'⏰ Pi unlock tomorrow\nGroup: {group}\nWallet: {name}\nAddress: {address[:8]}...{address[-6:]}\nLocked: {r["locked"]:.7f} Pi\nUnlock: {dt:%Y-%m-%d %H:%M:%S UTC}')
                    state['unlock_alerts'][key]=now.isoformat()
            state['wallets'][address]={'available':cur,'group':group,'name':name,'last_checked':now.isoformat(),'next_unlock':u}
            time.sleep(.1)
    if alerts:
        chunk=''
        for a in alerts:
            candidate=a if not chunk else chunk+'\n\n----------------\n\n'+a
            if len(candidate)>1400:
                send_whatsapp(chunk); chunk=a
            else: chunk=candidate
        if chunk: send_whatsapp(chunk)
    else: print('No alerts')
    save_state(state)

if __name__=='__main__': main()
