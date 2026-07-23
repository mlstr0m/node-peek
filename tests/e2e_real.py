"""E2E: real worker, realistic custom-node material + plain material,
two sequential jobs in ONE worker session, plus a bogus stub type.
Run: python3 e2e_real.py BLENDER_BIN WORKER_PY DIR"""
import json
import os
import subprocess
import sys
import time

blender, worker_py, d = sys.argv[1], sys.argv[2], sys.argv[3]
job = os.path.join(d, "job2")
cache = os.path.join(d, "cache2")
os.makedirs(job, exist_ok=True)
os.makedirs(cache, exist_ok=True)
try:
    os.remove(os.path.join(job, "response.json"))
except OSError:
    pass

with open(os.path.join(d, "customs.json")) as fh:
    customs = json.load(fh)

proc = subprocess.Popen(
    [blender, "--background", "--factory-startup", "--python", worker_py,
     "--", "--job", job, "--cache", cache,
     "--log", os.path.join(job, "worker.log")],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, text=True, encoding="utf-8")


def run_job(seq, material, custom_types, normalize_data_previews=False):
    req = {"seq": seq, "job": job, "lib": os.path.join(d, "real.blend"),
           "material": material, "res": 96, "engine": "CYCLES",
           "force": False, "priority": [], "path": [],
           "custom_types": custom_types,
           "normalize_data_previews": normalize_data_previews}
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            with open(os.path.join(job, "response.json")) as fh:
                resp = json.load(fh)
            if resp.get("seq") == seq and resp.get("done"):
                return resp
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.3)
    return None


# job 1: custom material, including a bogus type to exercise stub failure
r1 = run_job(1, "RealSim", customs + ["Bogus Type ###"])
# job 2: plain material (regression path, same worker session)
r2 = run_job(2, "PlainMat", [])
# jobs 3/4: normalize a deliberately HDR data map. The shader preview should
# reuse its exact cache entry; only the flat data preview gets a new image.
r3 = run_job(3, "NormalizeMat", [])
r4 = run_job(4, "NormalizeMat", [], normalize_data_previews=True)

proc.stdin.write(json.dumps({"stop": True}) + "\n")
proc.stdin.flush()
proc.stdin.close()
proc.wait(timeout=15)

ok = True
if r1 is None or r2 is None or r3 is None or r4 is None:
    print("E2E FAIL: missing response", bool(r1), bool(r2), bool(r3), bool(r4))
    sys.exit(1)

p1 = r1["previews"]
p2 = r2["previews"]
p3 = r3["previews"]
p4 = r4["previews"]
print("JOB1 previews:", sorted(p1))
print("JOB2 previews:", sorted(p2))

def check(cond, label):
    global ok
    print(("PASS " if cond else "FAIL ") + label)
    ok = ok and cond

check("CheckerA" in p1 and "CheckerB" in p1, "both custom instances previewed")
check(p1.get("CheckerA") != p1.get("CheckerB"),
      "per-instance values -> distinct thumbnails (4.0 vs 24.0)")
check("Grad" in p1, "shared-tree custom node previewed")
check("Wrap" in p1, "group containing a custom node previewed")
check("InvertA" in p1, "builtin downstream of custom previewed")
check("Note" not in p1, "pure-python node honestly skipped")
check("AfterNote" in p1, "builtin downstream of unrenderable still previewed")
check("PlainChecker" in p2 and "Principled BSDF" in p2,
      "plain material regression ok")
check("Amplify" in p3 and "Amplify" in p4, "HDR data preview rendered")
check(p3.get("Principled BSDF") == p4.get("Principled BSDF"),
      "shader preview cache is unaffected by normalization")
try:
    with open(os.path.join(cache, p3["Amplify"]), "rb") as fh:
        raw_png = fh.read()
    with open(os.path.join(cache, p4["Amplify"]), "rb") as fh:
        normalized_png = fh.read()
    check(raw_png != normalized_png,
          "normalized HDR data preview differs from clipped preview")
except (KeyError, OSError):
    check(False, "normalized HDR data preview differs from clipped preview")

# expose file paths for visual inspection
for k in ("CheckerA", "CheckerB", "Grad", "Wrap", "InvertA", "Principled BSDF"):
    if k in p1:
        print("PNG", k, "=", os.path.join(cache, p1[k]))

print("E2E_REAL_" + ("OK" if ok else "FAIL"))
sys.exit(0 if ok else 1)
