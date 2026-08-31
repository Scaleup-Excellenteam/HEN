import time, random, numpy as np
import engine_proto as E
random.seed(42)

def bench(queries, label, show_worst=0):
    rows=[]
    for q in queries:
        E.stats.update(lookups=0, range_elems=0)
        t0=time.perf_counter(); r = E.get_top5(q); t1=time.perf_counter()
        rows.append(((t1-t0)*1e3, E.stats['lookups'], E.stats['range_elems'], len(r), q))
    ts=sorted(rows); n=len(ts)
    print(f"{label}: n={n} p50={ts[n//2][0]:.2f} p95={ts[int(n*.95)][0]:.2f} "
          f"p99={ts[min(n-1,int(n*.99))][0]:.2f} max={ts[-1][0]:.2f} ms", flush=True)
    for row in ts[-show_worst:] if show_worst else []:
        print(f"   worst: {row[4]!r:20} {row[0]:.2f}ms lookups={row[1]} elems={row[2]} res={row[3]}")
    return rows

ALPHA = b'abcdefghijklmnopqrstuvwxyz0123456789'

# all canonical length-1 / length-2 (first hit, no caching exists)
bench([bytes([c]) for c in ALPHA], "all length-1", show_worst=2)
bench([bytes([a,b]) for a in ALPHA for b in ALPHA], "all length-2", show_worst=3)

# typical workload: random real substrings, half with an injected typo
lens_ = np.diff(E.starts)-1
def real_sub(m):
    idx = np.where(lens_>=m)[0]
    j = int(idx[random.randrange(len(idx))]); s0=int(E.starts[j]); L=int(lens_[j])
    off = random.randrange(L-m+1)
    return E.T[s0+off:s0+off+m]
typical=[]
for _ in range(400):
    m = random.choice([3,4,5,6,8,10,12,15,20,30])
    q = bytearray(real_sub(m))
    if random.random() < 0.5 and m > 2:
        i = random.randrange(m)
        op = random.choice(['sub','ins','del'])
        c = ALPHA[random.randrange(36)]
        if op=='sub': q[i:i+1] = bytes([c])
        elif op=='ins': q[i:i] = bytes([c])
        else: del q[i:i+1]
    typical.append(bytes(q))
bench(typical, "typical (real substrings, 50% one injected typo)", show_worst=3)

# adversarial A: rare-prefix + common word => fuzzy runs, common repaired patterns
advA = [b'xthe', b'qthe', b'zthe', b'jtion', b'xand ', b'qq the', b'zzation']
r=bench(advA, "adversarial A (<5 exact, common repaired patterns)", show_worst=3)

# adversarial B: Frankenstein — both halves individually common, whole absent
cand = [b'ationation', b'the the qq', b'tion ation', b'and andand', b'ing  ing', b'the qq the']
frank=[]
for q in cand:
    m=len(q); s1=(m+1)//2
    e = E.find(q); h1=E.find(q[:s1]); h2=E.find(q[s1:])
    if e[1]<=e[0] and h1[1]>h1[0] and h2[1]>h2[0]: frank.append(q)
print(f"frankenstein queries (halves present, whole absent): {frank}")
bench(frank, "adversarial B (Frankenstein)", show_worst=3)

# adversarial C: long no-match garbage (prefilter should kill instantly)
garbage=[bytes(random.choices(ALPHA,k=m)) for m in (10,20,50,100,200) for _ in range(20)]
bench(garbage, "adversarial C (long random garbage)")

# adversarial D: repeated characters
bench([b'aaaa', b'aaaaaaaa', b'thethethe', b'ababababab'], "adversarial D (repeats)", show_worst=2)

# prefilter on/off equivalence + savings
diff=0; t_on=t_off=0.0
sample = typical[:100] + advA + frank + garbage[:20]
for q in sample:
    t0=time.perf_counter(); a=E.get_top5(q,prefilter=True); t1=time.perf_counter()
    b=E.get_top5(q,prefilter=False); t2=time.perf_counter()
    t_on+=t1-t0; t_off+=t2-t1
    if a!=b: diff+=1; print("MISMATCH", q)
print(f"prefilter on/off: {diff} mismatches / {len(sample)}; total {t_on*1e3:.0f}ms vs {t_off*1e3:.0f}ms")
