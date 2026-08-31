import time, numpy as np, random, sys
T = open('artifacts/norm_blob.bin','rb').read()
sa = np.load('artifacts/sa.npy')
starts = np.load('artifacts/starts.npy')
NSA = len(sa)
random.seed(42)
lens = np.diff(starts) - 1

def lower_bound(pat):
    lo, hi = 0, NSA
    while lo < hi:
        mid = (lo + hi) >> 1
        s = sa[mid]
        if T[s:s+len(pat)] < pat: lo = mid + 1
        else: hi = mid
    return lo

def find(pat):
    return lower_bound(pat), lower_bound(pat + b'\xff')

def sample_real(m, k=200):
    idx = np.where(lens >= m)[0]
    if len(idx) == 0: return []
    out = []
    for _ in range(k):
        j = int(idx[random.randrange(len(idx))])
        s0 = int(starts[j]); L = int(lens[j])
        off = random.randrange(L - m + 1)
        out.append(T[s0+off : s0+off+m])
    return out

ALPHA = b'abcdefghijklmnopqrstuvwxyz0123456789 '
print(f"{'m':>4} {'hit us':>8} {'miss us':>8} {'avg hit range':>14}", flush=True)
for m in (1,2,3,5,10,20,50,100,200,385):
    hits = sample_real(m)
    if not hits: print(f"{m:>4} no lines long enough"); continue
    misses = [bytes(random.choices(ALPHA,k=m)) for _ in range(len(hits))]
    t0=time.perf_counter(); ranges=[find(p) for p in hits]; t1=time.perf_counter()
    for p in misses: find(p)
    t2=time.perf_counter()
    n=len(hits)
    print(f"{m:>4} {(t1-t0)/n*1e6:8.1f} {(t2-t1)/n*1e6:8.1f} {sum(hi-lo for lo,hi in ranges)/n:14.0f}", flush=True)

def repairs(q):
    pats = {}; m = len(q)
    for i in range(m):
        for c in ALPHA:
            if c != q[i:i+1][0]: pats[q[:i] + bytes([c]) + q[i+1:]] = 1
    if m >= 2:
        for i in range(m): pats[q[:i] + q[i+1:]] = 1
    for i in range(m+1):
        for c in ALPHA: pats[q[:i] + bytes([c]) + q[i:]] = 1
    return list(pats)

for m in (4, 10, 50):
    qs = sample_real(m, 20)
    t0=time.perf_counter(); np_ = 0
    for q in qs:
        ps = repairs(q); np_ += len(ps)
        for p in ps: find(p)
    t1=time.perf_counter()
    print(f"repair batch m={m}: ~{np_//len(qs)} patterns, {(t1-t0)/len(qs)*1000:.1f} ms/query (lookups only)", flush=True)
