import random, numpy as np
import engine_proto as E
random.seed(99)

def brute_top5(q):
    """exhaustive: every pattern's FULL range, all records, max score, exact ordering."""
    m=len(q); scores={}
    if m==0 or m-1>E.LMAX: return []
    if m<=E.LMAX:
        lo,hi=E.find(q)
        if hi>lo:
            ids=np.unique(np.searchsorted(E.starts, E.sa[lo:hi], side='right')-1)
            for r in ids: scores[int(r)]=2*m
    best={}
    for pat,sc,_ in E.repairs_tagged(q):
        if len(pat)==0 or len(pat)>E.LMAX: continue
        if pat not in best or sc>best[pat]: best[pat]=sc
    for pat,sc in best.items():
        lo,hi=E.find(pat)
        if hi>lo:
            ids=np.unique(np.searchsorted(E.starts, E.sa[lo:hi], side='right')-1)
            for r in ids:
                r=int(r)
                if scores.get(r, -999)<sc: scores[r]=max(scores.get(r,-999), sc)
    ranked=sorted(scores.items(), key=lambda kv:(-kv[1], kv[0]))
    return ranked[:5]

lens_=np.diff(E.starts)-1
ALPHA=b'abcdefghijklmnopqrstuvwxyz0123456789'
def real_sub(m):
    idx=np.where(lens_>=m)[0]
    j=int(idx[random.randrange(len(idx))]); s0=int(E.starts[j]); L=int(lens_[j])
    off=random.randrange(L-m+1)
    return E.T[s0+off:s0+off+m]
bad=0; n=0
tests=[]
for _ in range(40):
    m=random.choice([1,2,3,4,5,6,8,12])
    q=bytearray(real_sub(m))
    if random.random()<0.6 and m>1:
        i=random.randrange(m); op=random.choice('sid')
        c=ALPHA[random.randrange(36)]
        if op=='s': q[i:i+1]=bytes([c])
        elif op=='i': q[i:i]=bytes([c])
        else: del q[i:i+1]
    tests.append(bytes(q))
tests += [b'xthe', b'jtion', b'ationation', b'a', b'zq', b'q0']
for q in tests:
    if not q: continue
    n+=1
    a=E.get_top5(q); b=brute_top5(q)
    if a!=b:
        bad+=1; print("MISMATCH", q, "\n engine:", a, "\n brute :", b)
print(f"engine vs exhaustive brute force on real corpus: {bad} mismatches / {n} queries")
