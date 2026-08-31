import itertools, random
P_SUB={1:5,2:4,3:3,4:2}; P_INDEL={1:10,2:8,3:6,4:4}
def psub(i): return P_SUB.get(min(i,5),1)
def pindel(i): return P_INDEL.get(min(i,5),2)

def score_repairs(q, alpha):
    """repair-enumeration scorer: dedup by pattern, keep max score."""
    m=len(q); best={}
    def upd(p,s):
        if p not in best or s>best[p]: best[p]=s
    for i in range(1,m+1):
        for c in alpha:
            if c!=q[i-1]: upd(q[:i-1]+c+q[i:], 2*(m-1)-psub(i))
    if m>=2:
        for i in range(1,m+1): upd(q[:i-1]+q[i:], 2*(m-1)-pindel(i))
    for i in range(1,m+2):
        for c in alpha: upd(q[:i-1]+c+q[i-1:], 2*m-pindel(i))
    return best

def best_score_enum(q, s, alpha):
    """engine-side: exact else best repair whose pattern is substring of s."""
    if q in s: return 2*len(q)
    b=None
    for p,sc in score_repairs(q,alpha).items():
        if p and p in s and (b is None or sc>b): b=sc
    return b

def best_score_window(q, s):
    """independent reference: window alignment, max over all valid positions."""
    m=len(q); b=None
    def upd(x):
        nonlocal b
        if b is None or x>b: b=x
    if q in s: return 2*m
    for st in range(len(s)+1):
        # substitution: window length m
        w=s[st:st+m]
        if len(w)==m:
            mism=[i for i in range(m) if q[i]!=w[i]]
            if len(mism)==1: upd(2*(m-1)-psub(mism[0]+1))
        # query has EXTRA char: window length m-1, delete q[i]
        w=s[st:st+m-1]
        if m>=2 and len(w)==m-1:
            for i in range(1,m+1):
                if q[:i-1]+q[i:]==w: upd(2*(m-1)-pindel(i))
        # query MISSING char: window length m+1, insert into q at pos i
        w=s[st:st+m+1]
        if len(w)==m+1:
            for i in range(1,m+2):
                if q[:i-1]+w[i-1]+q[i-1:]==w: upd(2*m-pindel(i))
    return b

alpha='ab'
sents=[''.join(p) for L in range(1,7) for p in itertools.product('ab',repeat=L)]
qs=[''.join(p) for L in range(1,5) for p in itertools.product('abc',repeat=L)]
bad=0; total=0
for s in sents:
    for q in qs:
        total+=1
        a=best_score_enum(q,s,'abc'); b=best_score_window(q,s)
        if a!=b:
            bad+=1
            if bad<5: print("MISMATCH", repr(q), repr(s), a, b)
print(f"scoring: repair-enum vs window-reference, {total} (q,s) pairs, {bad} mismatches")

# duplicate-pattern max-score invariant check on repeated chars
q='aab'
sr=score_repairs(q,'ab')
assert sr['ab']==2*2-pindel(2), f"expected del@2 penalty (max score), got {sr['ab']}"
print(f"dedup-max: 'aab'->'ab' kept score {sr['ab']} (= del@pos2, penalty 8) not {2*2-pindel(1)}")

# 5-per-part lemma: per-part top-5 distinct suffices under exclusion, k+|F|<=5
random.seed(1); bad=0
for _ in range(20000):
    nparts=random.randrange(1,8)
    parts=[set(random.choices(range(40),k=random.randrange(1,15))) for _ in range(nparts)]
    union=set().union(*parts)
    f=random.randrange(0,5); F=set(random.sample(range(40),f))
    need=5-f
    exact=sorted(union-F)[:need]
    approx=set()
    for P in parts: approx.update(sorted(P)[:5])
    got=sorted(approx-F)[:need]
    if got!=exact: bad+=1
print(f"5-per-part lemma: 20000 random systems, {bad} failures")
