import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import h3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_fcc import (CACHE, COARSE, COARSE_SHARD, DATA, FINE_SHARD, FIPS, USPS, api_download, api_json,
                       as_of_dates, dump, log, parse_states, reset_dir)

PAGE = "https://broadbandexpanded.com/funding/beadfinalproposaldata"
BASE = "https://broadbandexpanded.com/files/data/bead/"
TECH_ORDER = ["60", "61", "70", "71", "72", "10", "40", "0", "50"]
TECH_LABEL = {
    "50": "Fiber", "61": "LEO satellite", "60": "Satellite", "70": "Unlicensed fixed wireless",
    "71": "Licensed fixed wireless", "72": "Licensed fixed wireless", "40": "Cable", "10": "DSL", "0": "Other",
}
DRAFT = {"MO", "NC", "RI", "AS", "GU", "MP", "PR", "VI"}
SUBMITTED = {"GA", "IL", "PA"}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def pick(header, *alts):
    keys = {norm(h): h for h in header}
    for a in alts:
        if norm(a) in keys:
            return keys[norm(a)]
    for a in alts:
        for k, h in keys.items():
            if norm(a) in k:
                return h
    return None


def clean_id(v):
    v = str(v or "").strip()
    return v[:-2] if v.endswith(".0") else v


def money(v):
    try:
        return round(float(re.sub(r"[^0-9.\-]", "", str(v))))
    except ValueError:
        return None


def fetch(url, dest=None):
    req = urllib.request.Request(url, headers={"User-Agent": "got-fiber-build/1.0"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                if dest is None:
                    return r.read()
                with open(dest, "wb") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                return dest
        except Exception as e:
            if attempt == 4:
                raise
            log(f"  retry {url}: {e}")
            time.sleep(10 * (attempt + 1))


def latest_zip():
    html = fetch(PAGE).decode("utf-8", "replace")
    stamps = sorted(set(re.findall(r"BEAD_FP_Data_BroadbandExpanded_(\d{8})\.zip", html)))
    if not stamps:
        sys.exit(f"Could not find the BEAD data zip on {PAGE}")
    d = stamps[-1]
    return f"{BASE}BEAD_FP_Data_BroadbandExpanded_{d}.zip", f"{d[:4]}-{d[4:6]}-{d[6:]}"


def table(z, prefix):
    for n in z.namelist():
        base = n.rsplit("/", 1)[-1].upper()
        if base.startswith(prefix) and base.endswith(".CSV"):
            r = csv.DictReader(io.TextIOWrapper(z.open(n), encoding="utf-8-sig", newline=""))
            log(f"{prefix} columns: {r.fieldnames}")
            return r
    sys.exit(f"No {prefix} table in the BEAD zip")


def load_bead(path, wanted):
    z = zipfile.ZipFile(path)
    subs = {}
    r = table(z, "SUBGRANTEE")
    c_st, c_uei, c_name = pick(r.fieldnames, "state"), pick(r.fieldnames, "uei"), pick(r.fieldnames, "uei_name", "name")
    for row in r:
        uei = (row.get(c_uei) or "").strip()
        name = (row.get(c_name) or "").strip()
        st = (row.get(c_st) or "").strip().upper()
        if uei and name:
            subs[(st, uei)] = name
            subs.setdefault(("", uei), name)

    projects = {}
    r = table(z, "PROJECT")
    f = r.fieldnames
    c_st, c_pid, c_name, c_uei = pick(f, "state"), pick(f, "project_id"), pick(f, "project_name"), pick(f, "uei")
    c_amt = pick(f, "projected_bead_funding", "bead_funding", "bead_amount", "funding")
    for row in r:
        st = (row.get(c_st) or "").strip().upper()
        if st not in wanted:
            continue
        uei = (row.get(c_uei) or "").strip()
        projects[(st, clean_id(row.get(c_pid)))] = {
            "name": (row.get(c_name) or "").strip(),
            "awardee": subs.get((st, uei)) or subs.get(("", uei)) or uei,
            "amount": money(row.get(c_amt)) if c_amt else None,
            "tech": Counter(),
            "locs": 0,
        }

    locs = defaultdict(dict)
    r = table(z, "LOCATION")
    f = r.fieldnames
    c_st, c_loc, c_pid = pick(f, "state"), pick(f, "location_id"), pick(f, "project_id")
    c_tech = pick(f, "technology_code", "technology", "tech")
    for row in r:
        st = (row.get(c_st) or "").strip().upper()
        if st not in wanted:
            continue
        lid, pid = clean_id(row.get(c_loc)), clean_id(row.get(c_pid))
        if not lid or not pid:
            continue
        locs[st][lid] = pid
        p = projects.get((st, pid))
        if p is None:
            p = projects[(st, pid)] = {"name": "", "awardee": "", "amount": None, "tech": Counter(), "locs": 0}
        p["locs"] += 1
        p["tech"][clean_id(row.get(c_tech)) if c_tech else ""] += 1
    return projects, locs


def tech_label(counter):
    total = sum(counter.values()) or 1
    out = []
    for code, n in counter.most_common():
        label = TECH_LABEL.get(code)
        if label and n / total >= 0.1 and label not in out:
            out.append(label)
    return "/".join(out)


def fast_rows(path):
    opener = []
    if path.suffix.lower() == ".zip":
        z = zipfile.ZipFile(path)
        for n in z.namelist():
            if n.lower().endswith(".csv"):
                opener.append(lambda n=n: io.TextIOWrapper(z.open(n), encoding="utf-8-sig", newline=""))
    else:
        opener.append(lambda: open(path, encoding="utf-8-sig", newline=""))
    for o in opener:
        with o() as fh:
            r = csv.reader(fh)
            head = next(r, None)
            if not head:
                continue
            try:
                i_loc, i_h3 = head.index("location_id"), head.index("h3_res8_id")
            except ValueError:
                continue
            for row in r:
                if len(row) > max(i_loc, i_h3):
                    yield row[i_loc], row[i_h3]


def resolve(path, pending, found):
    for lid, cell in fast_rows(path):
        lid = clean_id(lid)
        if lid in pending:
            cell = cell.strip().lower()
            if h3.is_valid_cell(cell):
                found[lid] = cell
                pending.discard(lid)
                if not pending:
                    return


def availability_files(as_of, fips_wanted, user, token):
    d = api_json(f"/downloads/listAvailabilityData/{as_of}?category=State", user, token)
    out = defaultdict(lambda: defaultdict(list))
    for f in d.get("data", []):
        fips = str(f.get("state_fips") or "").zfill(2)
        if fips not in fips_wanted or (f.get("category") or "State") != "State":
            continue
        kind = f"{f.get('technology_type') or ''} {f.get('subcategory') or ''}".lower()
        if "mobile" in kind or (f.get("file_type") or "csv").lower() != "csv":
            continue
        tech = str(f.get("technology_code") or "").strip()
        if tech in TECH_ORDER:
            out[fips][tech].append(f)
    return out


def map_locations_api(locs, user, token):
    fips_wanted = {FIPS[s] for s in locs if s in FIPS}
    files, as_of = {}, None
    for as_of in as_of_dates(user, token)[:4]:
        files = availability_files(as_of, fips_wanted, user, token)
        if files:
            break
    log(f"Mapping BEAD locations with FCC data {as_of}")
    CACHE.mkdir(exist_ok=True)
    found = {}
    for st in sorted(locs):
        pending = set(locs[st])
        total = len(pending)
        by_tech = files.get(FIPS.get(st, ""), {})
        for tech in TECH_ORDER:
            if not pending:
                break
            for f in by_tech.get(tech, []):
                if not pending:
                    break
                fid = f.get("file_id")
                dest = CACHE / f"{as_of}_{fid}.zip"
                log(f"{st}: tech {tech}, {len(pending)} locations left, downloading {f.get('file_name', fid)}")
                api_download(f"/downloads/downloadFile/availability/{fid}", user, token, dest)
                resolve(dest, pending, found)
                dest.unlink()
        log(f"{st}: mapped {total - len(pending)} of {total} BEAD locations")
    return found


def map_locations_local(locs, folder):
    pending = set().union(*locs.values()) if locs else set()
    found = {}
    for p in sorted(Path(folder).iterdir()):
        if p.suffix.lower() in (".zip", ".csv") and pending:
            resolve(p, pending, found)
    log(f"Mapped {len(found)} BEAD locations from {folder}, {len(pending)} unmapped")
    return found


def write(projects, locs, found, as_of, source):
    root = DATA / "bead"
    for sub in ("r8", "r6", "projects"):
        reset_dir(root / sub)
    fine = defaultdict(Counter)
    mapped = Counter()
    for st, ids in locs.items():
        for lid, pid in ids.items():
            cell = found.get(lid)
            if cell:
                fine[cell][f"{st}:{pid}"] += 1
                mapped[(st, pid)] += 1
    fine_shards = defaultdict(dict)
    coarse = defaultdict(Counter)
    for cell, c in fine.items():
        fine_shards[h3.cell_to_parent(cell, FINE_SHARD)][cell] = sorted(c.items())
        coarse[h3.cell_to_parent(cell, COARSE)].update(c)
    for sid, cells in fine_shards.items():
        dump(root / "r8" / f"{sid}.json", {"c": {k: [list(x) for x in v] for k, v in cells.items()}})
    coarse_shards = defaultdict(dict)
    for cell, c in coarse.items():
        coarse_shards[h3.cell_to_parent(cell, COARSE_SHARD)][cell] = [list(x) for x in sorted(c.items())]
    for sid, cells in coarse_shards.items():
        dump(root / "r6" / f"{sid}.json", {"c": cells})
    states = sorted({st for st, _ in projects})
    for st in states:
        status = "proposed" if st in DRAFT or st in SUBMITTED else "awarded"
        p = {pid: [v["name"], v["awardee"], v["amount"], tech_label(v["tech"]), v["locs"], mapped[(s, pid)]]
             for (s, pid), v in projects.items() if s == st}
        dump(root / "projects" / f"{st}.json", {"status": status, "p": p})
    path = DATA / "manifest.json"
    m = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    m.update({
        "beadStates": states,
        "beadAsOf": as_of,
        "beadSource": source,
        "beadGenerated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    path.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    log(f"BEAD: {len(projects)} projects, {len(fine)} cells, {len(fine_shards)} fine and {len(coarse_shards)} coarse shards, states {states}")


def main():
    ap = argparse.ArgumentParser(description="Map BEAD Final Proposal projects to H3 cells")
    ap.add_argument("--states", default=os.environ.get("STATES", "AZ"))
    ap.add_argument("--bead-zip", help="Local copy of the BroadbandExpanded BEAD CSV zip")
    ap.add_argument("--as-of", help="Date label for a local BEAD zip")
    ap.add_argument("--fcc-local", help="Folder of FCC availability CSV or ZIP files instead of the API")
    a = ap.parse_args()
    wanted = set(parse_states(a.states))
    if a.bead_zip:
        zpath, as_of = Path(a.bead_zip), a.as_of or "local data"
    else:
        url, as_of = latest_zip()
        CACHE.mkdir(exist_ok=True)
        zpath = CACHE / url.rsplit("/", 1)[-1]
        if not zpath.exists():
            log(f"Downloading {url}")
            fetch(url, zpath)
    projects, locs = load_bead(zpath, wanted)
    log(f"Loaded {len(projects)} projects and {sum(len(v) for v in locs.values())} locations")
    if not projects:
        sys.exit("No BEAD projects found for the requested states.")
    if a.fcc_local:
        found = map_locations_local(locs, a.fcc_local)
    else:
        user = os.environ.get("FCC_USERNAME", "").strip()
        token = os.environ.get("FCC_API_TOKEN", "").strip()
        if not user or not token:
            sys.exit("Set FCC_USERNAME and FCC_API_TOKEN.")
        found = map_locations_api(locs, user, token)
    write(projects, locs, found, as_of, PAGE)


if __name__ == "__main__":
    main()
