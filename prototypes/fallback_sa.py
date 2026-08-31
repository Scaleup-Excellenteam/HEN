import time, resource, numpy as np
def rss(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1e9
T = open('artifacts/norm_blob.bin','rb').read()
a = np.frombuffer(T, dtype=np.uint8); n = len(a)
t_start = time.time()
rank = a.astype(np.int32); k = 1; rounds = 0
while True:
    t0 = time.time()
    key2 = np.zeros(n, dtype=np.int32); key2[:n-k] = rank[k:]
    order = np.lexsort((key2, rank))
    r1 = rank[order]; r2 = key2[order]
    neq = np.empty(n, dtype=bool); neq[0] = True
    neq[1:] = (r1[1:] != r1[:-1]) | (r2[1:] != r2[:-1])
    nr = int(neq.sum())
    rank = np.empty(n, dtype=np.int32)
    rank[order] = np.cumsum(neq, dtype=np.int64) - 1
    rounds += 1
    print(f"round {rounds} k={k}: {time.time()-t0:.1f}s distinct={nr}/{n} rss={rss():.2f}GB", flush=True)
    if nr == n: break
    k <<= 1
print(f"DONE rounds={rounds} total={time.time()-t_start:.1f}s peak_rss={rss():.2f}GB")
sa_ref = np.load('artifacts/sa.npy')
print("matches divsufsort:", np.array_equal(order.astype(np.int32), sa_ref))
