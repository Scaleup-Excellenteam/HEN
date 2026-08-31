import time, numpy as np
T = open('artifacts/norm_blob.bin','rb').read()
sa = np.load('artifacts/sa.npy'); starts = np.load('artifacts/starts.npy')
NSA = len(sa); LMAX = 385; K = 5
KRAW = K * LMAX  # 1925: smallest KRAW raw entries guarantee K distinct records

def lower_bound(pat):
    lo, hi = 0, NSA
    while lo < hi:
        mid = (lo+hi) >> 1
        s = sa[mid]
        if T[s:s+len(pat)] < pat: lo = mid+1
        else: hi = mid
    return lo
def find(p): return lower_bound(p), lower_bound(p + b'\xff')

def topk_fullscan(lo, hi):
    pos = sa[lo:hi]                                        # int32 copy, 4H
    ids = np.searchsorted(starts, pos, side='right') - 1   # int64, 8H
    kr = min(KRAW + 1, len(ids))
    part = np.partition(ids, kr - 1)[:kr]
    part.sort()
    uniq = part[np.concatenate(([True], part[1:] != part[:-1]))]
    return uniq[:K], len(ids)

# --- one-time SA-aligned record ids (for rec_sa variant AND block build)
t0 = time.perf_counter()
rec_sa = (np.searchsorted(starts, sa, side='right') - 1).astype(np.int32)
t1 = time.perf_counter()
print(f"rec_sa build (searchsorted over all {NSA/1e6:.0f}M SA entries): {t1-t0:.2f}s, size={rec_sa.nbytes/1e6:.0f}MB", flush=True)

def topk_recsa(lo, hi):
    ids = rec_sa[lo:hi]
    kr = min(KRAW + 1, len(ids))
    part = np.partition(ids, kr - 1)[:kr]; part.sort()
    uniq = part[np.concatenate(([True], part[1:] != part[:-1]))]
    return uniq[:K], hi - lo

# --- block summaries: top-5 distinct record ids per SA block
B = 4096
t0 = time.perf_counter()
nb = (NSA + B - 1) // B
blk = np.full((nb, K), np.iinfo(np.int32).max, dtype=np.int32)
for b in range(nb):
    seg = rec_sa[b*B:(b+1)*B]
    u = np.unique(seg)          # sorted distinct
    blk[b, :min(K, len(u))] = u[:K]
t1 = time.perf_counter()
print(f"block summary build: {t1-t0:.1f}s, blocks={nb}, size={blk.nbytes/1e6:.2f}MB", flush=True)

def topk_blocks(lo, hi):
    b_lo, b_hi = -(-lo // B), hi // B   # full blocks [b_lo, b_hi)
    parts = []
    if b_lo < b_hi:
        parts.append(blk[b_lo:b_hi].ravel())
        if lo < b_lo * B: parts.append(rec_sa[lo:b_lo*B])
        if b_hi * B < hi: parts.append(rec_sa[b_hi*B:hi])
    else:
        parts.append(rec_sa[lo:hi])
    allc = np.concatenate(parts)
    u = np.unique(allc)
    return u[:K], hi - lo

pats = [b' ', b'e', b't', b'th', b'the', b'the ', b'tion', b'ation', b'q', b'0']
print(f"{'pattern':>8} {'hits':>10} {'fullscan ms':>12} {'rec_sa ms':>10} {'blocks ms':>10} same")
for p in pats:
    lo, hi = find(p)
    if hi <= lo: print(f"{p!r:>8} absent"); continue
    t0=time.perf_counter(); r1,_ = topk_fullscan(lo,hi); t1=time.perf_counter()
    r2,_ = topk_recsa(lo,hi); t2=time.perf_counter()
    r3,_ = topk_blocks(lo,hi); t3=time.perf_counter()
    ok = np.array_equal(r1,r2) and np.array_equal(r1,r3)
    print(f"{str(p):>8} {hi-lo:>10} {(t1-t0)*1e3:12.2f} {(t2-t1)*1e3:10.2f} {(t3-t2)*1e3:10.2f} {ok}", flush=True)

# randomized cross-validation of the three methods incl. exact brute force
import random; random.seed(7)
def brute(lo, hi):
    ids = np.unique(np.searchsorted(starts, sa[lo:hi], side='right') - 1)
    return ids[:K]
bad = 0
for _ in range(300):
    m = random.choice([1,2,3,4,6,10])
    p0 = random.randrange(len(T)-m)
    p = T[p0:p0+m]
    if b'\n' in p: continue
    lo, hi = find(p)
    if hi <= lo: continue
    r0 = brute(lo,hi); r1,_ = topk_fullscan(lo,hi); r2,_ = topk_recsa(lo,hi); r3,_ = topk_blocks(lo,hi)
    if not (np.array_equal(r0,r1) and np.array_equal(r0,r2) and np.array_equal(r0,r3)): bad += 1
print(f"randomized cross-check vs brute force: {bad} mismatches / 300")
np.save('artifacts/rec_sa.npy', rec_sa); np.save('artifacts/blocks.npy', blk)
