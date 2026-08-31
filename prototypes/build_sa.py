import time, resource, numpy as np
from pydivsufsort import divsufsort
def rss_gb(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1e9
T = open('artifacts/norm_blob.bin','rb').read()
print(f"blob loaded N={len(T)/1e6:.1f}MB rss={rss_gb():.2f}GB")
t0=time.time()
sa = divsufsort(T)
t1=time.time()
print(f"divsufsort: {t1-t0:.1f}s dtype={sa.dtype} peak_rss={rss_gb():.2f}GB")
sa = sa.astype(np.int32) if sa.dtype != np.int32 else sa
np.save('artifacts/sa.npy', sa)
print(f"saved. total={time.time()-t0:.1f}s peak_rss={rss_gb():.2f}GB")
