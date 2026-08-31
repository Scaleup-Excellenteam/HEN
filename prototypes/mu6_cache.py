import time, hashlib, os, numpy as np
ROOT='/Users/orhadad/Desktop/לימודים/שנה ד/Google Project/ArchiveFiles'
# corpus fingerprint: stat-based (paths+sizes+mtimes, length-delimited)
t0=time.time()
h=hashlib.sha256()
for dp,dn,fn in os.walk(ROOT):
    dn.sort()
    for f in sorted(fn):
        if f.endswith('.txt'):
            p=os.path.join(dp,f); st=os.stat(p); rel=os.path.relpath(p,ROOT).encode()
            h.update(len(rel).to_bytes(4,'little')+rel+int(st.st_size).to_bytes(8,'little')+int(st.st_mtime_ns).to_bytes(12,'little'))
t1=time.time(); print(f"stat fingerprint: {t1-t0:.2f}s")
# corpus content hash
h=hashlib.sha256()
for dp,dn,fn in os.walk(ROOT):
    dn.sort()
    for f in sorted(fn):
        if f.endswith('.txt'):
            p=os.path.join(dp,f); rel=os.path.relpath(p,ROOT).encode()
            h.update(len(rel).to_bytes(4,'little')+rel)
            with open(p,'rb') as fh:
                d = fh.read(); h.update(len(d).to_bytes(8,'little')+d)
t2=time.time(); print(f"content hash (122MB): {t2-t1:.2f}s")
# artifact checksums
tot=0
for f in ('sa.npy','norm_blob.bin','orig_blob.bin','starts.npy'):
    with open(f'artifacts/{f}','rb') as fh: d=fh.read(); tot+=len(d); hashlib.sha256(d)
t3=time.time(); print(f"artifact sha256 ({tot/1e6:.0f}MB): {t3-t2:.2f}s")
# mmap open vs full load
t0=time.time(); sa=np.load('artifacts/sa.npy', mmap_mode='r'); t1=time.time()
print(f"np.load mmap open: {(t1-t0)*1e3:.1f}ms")
_ = int(sa[0]) + int(sa[len(sa)//2]); t2=time.time()
t0=time.time(); sa2=np.load('artifacts/sa.npy'); t1=time.time()
print(f"np.load full read (395MB): {t1-t0:.2f}s")
