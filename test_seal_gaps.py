#!/usr/bin/env python3
"""Adversarial self-test: reproduce the exploits from the review and assert they are BLOCKED.
Runs each case on a throwaway copy of the (already migrated) library dir."""
import os, sys, shutil, subprocess, tempfile
import katana_lock as K

HERE=os.path.dirname(os.path.abspath(__file__))
# Default to the library this repository ships. This used to default to "." — the repo
# root, which holds no LOCK.tsv — so a bare run died on a FileNotFoundError traceback that
# reads like library corruption rather than a usage mistake. verify.py passes the path
# explicitly (run(SUITE, str(LIB))) and is unaffected either way.
BASE=sys.argv[1] if len(sys.argv)>1 else os.path.join(HERE,"parts-library","ref_parts")
BASE=os.path.abspath(BASE)
VER=os.path.join(HERE,"verify_library_v2.py")
if not os.path.isfile(os.path.join(BASE,"LOCK.tsv")):
    sys.exit(f"No LOCK.tsv in {BASE}\n"
             f"usage: {os.path.basename(__file__)} [LIBRARY_DIR]"
             f"   (default: parts-library/ref_parts)")

def run_verify(d):
    r=subprocess.run([sys.executable, VER, os.path.join(d,"LOCK.tsv")],
                     capture_output=True, text=True)
    return r.returncode, r.stdout.strip()

def fresh():
    d=tempfile.mkdtemp(prefix="kltest_")
    for item in os.listdir(BASE):
        s=os.path.join(BASE,item); t=os.path.join(d,item)
        if os.path.isdir(s): shutil.copytree(s,t)
        else: shutil.copy2(s,t)
    return d

results=[]
def check(name, cond, detail=""):
    results.append((name,cond,detail))
    print(f"{'PASS' if cond else 'FAIL'} — {name}" + (f"  [{detail}]" if detail and not cond else ""))

# baseline: fresh copy verifies green
d=fresh(); rc,out=run_verify(d); check("baseline library verifies SEALED", rc==0, out)

# GAP 4b: edit a trust field (source accession) in a LOCK row, leave file untouched
d=fresh()
lp=os.path.join(d,"LOCK.tsv"); L=open(lp).read()
L2=L.replace("NC_000913.3:363231-366305","NC_000913.3:999999-999999")
open(lp,"w",newline="\n").write(L2)
rc,out=run_verify(d); check("Gap4b: edited accession in row is caught", rc!=0 and "row_sha256 MISMATCH" in out, out)

# GAP 5a: orphan file (dropped into store, no LOCK row)
d=fresh()
shutil.copy2(os.path.join(d,"sfGFP__v1__08a1e654bd76.gb"), os.path.join(d,"sneaky__v1__deadbeefcafe.gb"))
rc,out=run_verify(d); check("Gap5: orphan file with no row is caught", rc!=0 and "ORPHAN" in out, out)

# GAP 5b: fail-closed on missing file (row present, file gone)
d=fresh(); os.remove(os.path.join(d,"B0015__v1__696c73e5a7a8.gb"))
rc,out=run_verify(d); check("Gap5: missing sealed file is caught (fail-closed)", rc!=0 and "MISSING" in out, out)

# BONUS: tamper file bytes (sequence edit)
d=fresh()
fp=os.path.join(d,"B0032__v1__7480912e8220.gb"); b=open(fp).read()
open(fp,"w",newline="\n").write(b.replace("ORIGIN","ORIGIN      \n        1 aaaaa",1) if "ORIGIN" in b else b+"\nextra")
rc,out=run_verify(d); check("Bonus: tampered file bytes are caught", rc!=0, out)

# GAP 2: resolver bans id-only, honours version + pin
_,rows=K.read_lock(os.path.join(BASE,"LOCK.tsv"))
def expect_raise(fn):
    try: fn(); return False
    except K.ResolveError: return True
check("Gap2: id-only resolution raises", expect_raise(lambda: K.resolve(rows,"sfGFP")))
ok=False
try: r=K.resolve(rows,"sfGFP",1); ok=(r["seq_sha256"][:12]=="08a1e654bd76")
except Exception: ok=False
check("Gap2: id+version resolves to the pinned row", ok)
check("Gap2: wrong seq-sha pin raises", expect_raise(lambda: K.resolve(rows,"sfGFP",1,"ffffffffffff")))

n=sum(1 for _,c,_ in results if c); print(f"\n{n}/{len(results)} checks passed")
sys.exit(0 if n==len(results) else 1)
