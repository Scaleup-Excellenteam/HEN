import numpy as np
T = open('artifacts/norm_blob.bin','rb').read()
sa = np.load('artifacts/sa.npy'); starts = np.load('artifacts/starts.npy')
blk = np.load('artifacts/blocks.npy')
NSA = len(sa); LMAX = 385; K = 5; B = 4096
ALPHA = b'abcdefghijklmnopqrstuvwxyz0123456789 '
P_SUB = {1:5, 2:4, 3:3, 4:2}; P_INDEL = {1:10, 2:8, 3:6, 4:4}
SENT = np.iinfo(np.int32).max

stats = {'lookups':0, 'range_elems':0}

def lower_bound(pat):
    lo, hi = 0, NSA
    while lo < hi:
        mid = (lo+hi) >> 1
        s = sa[mid]
        if T[s:s+len(pat)] < pat: lo = mid+1
        else: hi = mid
    return lo
def find(p):
    stats['lookups'] += 1
    return lower_bound(p), lower_bound(p + b'\xff')

def topk_range(lo, hi, exclude, need):
    """smallest `need` distinct record ids in SA range, excluding `exclude` (exact)."""
    if hi <= lo: return []
    b_lo, b_hi = -(-lo // B), hi // B
    parts = []
    if b_lo < b_hi:
        parts.append(blk[b_lo:b_hi].ravel())
        if lo < b_lo*B: parts.append(np.searchsorted(starts, sa[lo:b_lo*B], side='right') - 1)
        if b_hi*B < hi: parts.append(np.searchsorted(starts, sa[b_hi*B:hi], side='right') - 1)
        stats['range_elems'] += (b_hi-b_lo)*K + (b_lo*B - lo) + (hi - b_hi*B)
    else:
        parts.append(np.searchsorted(starts, sa[lo:hi], side='right') - 1)
        stats['range_elems'] += hi - lo
    u = np.unique(np.concatenate(parts))
    out = []
    for rid in u:
        rid = int(rid)
        if rid == SENT: break
        if rid not in exclude:
            out.append(rid)
            if len(out) == need: break
    return out

def repairs_tagged(q):
    """yield (pattern, score, half) — half: 1=edit in first half, 2=second, 0=boundary-ins."""
    m = len(q); s1 = (m + 1) // 2
    for i in range(1, m+1):
        pen = P_SUB.get(min(i,5), 1)
        for c in ALPHA:
            if c != q[i-1:i][0]:
                yield q[:i-1] + bytes([c]) + q[i:], 2*(m-1) - pen, (1 if i <= s1 else 2)
    if m >= 2:
        for i in range(1, m+1):
            pen = P_INDEL.get(min(i,5), 2)
            yield q[:i-1] + q[i:], 2*(m-1) - pen, (1 if i <= s1 else 2)
    for i in range(1, m+2):
        pen = P_INDEL.get(min(i,5), 2)
        half = 1 if i <= s1 else (0 if i == s1+1 else 2)
        for c in ALPHA:
            yield q[:i-1] + bytes([c]) + q[i-1:], 2*m - pen, half

def get_top5(q, prefilter=True):
    m = len(q)
    if m == 0 or m - 1 > LMAX: return []
    found = []
    fset = set()
    if m <= LMAX:
        lo, hi = find(q)
        for rid in topk_range(lo, hi, fset, K):
            found.append((rid, 2*m)); fset.add(rid)
    if len(found) >= K: return found
    # prefilter
    s1 = (m + 1) // 2
    q1, q2 = q[:s1], q[s1:]
    if prefilter and m >= 2:
        p1 = find(q1)[1] > find(q1)[0]
        p2 = (len(q2) == 0) or (find(q2)[1] > find(q2)[0])
        if not p1 and not p2: return found
    else:
        p1 = p2 = True
    best = {}
    for pat, sc, half in repairs_tagged(q):
        if len(pat) == 0 or len(pat) > LMAX: continue
        if half == 1 and not p2: continue      # edit in first half needs q2 present
        if half == 2 and not p1: continue      # edit in second half needs q1 present
        if half == 0 and not (p1 and p2): continue
        if pat not in best or sc > best[pat]: best[pat] = sc
    tiers = {}
    for pat, sc in best.items(): tiers.setdefault(sc, []).append(pat)
    for sc in sorted(tiers, reverse=True):
        cands = set()
        for pat in tiers[sc]:
            lo, hi = find(pat)
            for rid in topk_range(lo, hi, fset, K - len(found)):
                cands.add(rid)
        for rid in sorted(cands)[:K - len(found)]:
            found.append((rid, sc)); fset.add(rid)
        if len(found) >= K: break
    return found
