import os, sys, time, resource, json
import numpy as np

ROOT = '/Users/orhadad/Desktop/לימודים/שנה ד/Google Project/ArchiveFiles'
OUT = 'artifacts'
os.makedirs(OUT, exist_ok=True)

def rss_gb(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9

# --- corrected normalizer: A-Z->a-z, \t\r\v\f -> space, keep [a-z0-9 ], delete rest
KEEP = set(b'abcdefghijklmnopqrstuvwxyz0123456789 ')
table = bytearray(range(256))
for c in range(65, 91): table[c] = c + 32          # A-Z -> a-z
for c in (9, 11, 12, 13): table[c] = 32            # tab, VT, FF, CR -> space
TABLE = bytes(table)
DELETE = bytes(c for c in range(256) if TABLE[c] not in KEEP)

def normalize(line: bytes) -> bytes:
    return b' '.join(line.translate(TABLE, DELETE).split())

t0 = time.time()
records = []   # (orig_bytes, path_id, line_no, norm_bytes)
paths = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames.sort()
    for fn in sorted(filenames):
        if not fn.endswith('.txt'): continue
        full = os.path.join(dirpath, fn)
        rel = os.path.relpath(full, ROOT)
        pid = len(paths); paths.append(rel)
        with open(full, 'rb') as f: data = f.read()
        for i, line in enumerate(data.split(b'\n'), start=1):
            n = normalize(line)
            if n: records.append((line.rstrip(b'\r'), pid, i, n))
t1 = time.time()
print(f"scan+normalize: {t1-t0:.1f}s rss={rss_gb():.2f}GB records={len(records)}")

# alphabet invariant check on the fly
seen = set()
for r in records: seen.update(r[3])
assert seen <= KEEP, f"alphabet violation: {sorted(seen - KEEP)}"
print(f"alphabet check OK: {len(seen)} distinct output chars, all in [a-z0-9 ]")
t2 = time.time(); print(f"alphabet check: {t2-t1:.1f}s")

# --- sort by tie key: (original sentence bytes, path, line)  [revised D7]
records.sort(key=lambda r: (r[0], paths[r[1]], r[2]))
t3 = time.time()
print(f"sort by (orig,path,line): {t3-t2:.1f}s rss={rss_gb():.2f}GB")

S = len(records)
norm_blob = b'\n'.join(r[3] for r in records) + b'\n'
orig_blob = b'\n'.join(r[0] for r in records) + b'\n'
starts = np.zeros(S + 1, dtype=np.int32)
lens = np.fromiter((len(r[3]) + 1 for r in records), dtype=np.int32, count=S)
np.cumsum(lens, out=starts[1:])
orig_starts = np.zeros(S + 1, dtype=np.int64)
olens = np.fromiter((len(r[0]) + 1 for r in records), dtype=np.int64, count=S)
np.cumsum(olens, out=orig_starts[1:])
file_id = np.fromiter((r[1] for r in records), dtype=np.uint16, count=S)
line_no = np.fromiter((r[2] for r in records), dtype=np.int32, count=S)
maxlen = int(lens.max()) - 1
t4 = time.time()
print(f"blobs+arrays: {t4-t3:.1f}s rss={rss_gb():.2f}GB N={len(norm_blob)/1e6:.1f}MB Lmax={maxlen}")

with open(f'{OUT}/norm_blob.bin','wb') as f: f.write(norm_blob)
with open(f'{OUT}/orig_blob.bin','wb') as f: f.write(orig_blob)
np.save(f'{OUT}/starts.npy', starts); np.save(f'{OUT}/orig_starts.npy', orig_starts)
np.save(f'{OUT}/file_id.npy', file_id); np.save(f'{OUT}/line_no.npy', line_no)
json.dump({'paths': paths, 'S': S, 'Lmax': maxlen}, open(f'{OUT}/meta.json','w'))
t5 = time.time()
print(f"write artifacts: {t5-t4:.1f}s total={t5-t0:.1f}s peak_rss={rss_gb():.2f}GB")
