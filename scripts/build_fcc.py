import argparse
import csv
import io
import json
import os
import shutil
import sys
import time
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import h3

API = "https://broadbandmap.fcc.gov/api/public/map"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = ROOT / ".cache"
FINE, FINE_SHARD, COARSE, COARSE_SHARD = 8, 5, 6, 3
FIBER_TECH = "50"

FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10",
    "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19",
    "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35",
    "NY": "36", "NC": "37", "ND": "38", "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44",
    "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56", "PR": "72",
}
USPS = {v: k for k, v in FIPS.items()}


def log(msg):
    print(msg, flush=True)


def request(path, user, token):
    return urllib.request.Request(
        API + path,
        headers={
            "username": user,
            "hash_value": token,
            "User-Agent": "got-fiber-build/1.0",
            "Accept": "application/json, application/zip, */*",
        },
    )


def api_json(path, user, token):
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request(path, user, token), timeout=300) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == 4:
                raise
            log(f"  retry {path}: {e}")
            time.sleep(10 * (attempt + 1))


def api_download(path, user, token, dest):
    tmp = dest.with_suffix(".part")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request(path, user, token), timeout=900) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            tmp.replace(dest)
            return dest
        except Exception as e:
            if attempt == 4:
                raise
            log(f"  retry download: {e}")
            time.sleep(15 * (attempt + 1))


def as_of_dates(user, token):
    d = api_json("/listAsOfDates", user, token)
    dates = sorted({x["as_of_date"][:10] for x in d.get("data", []) if x.get("data_type") == "availability"}, reverse=True)
    if not dates:
        sys.exit("FCC API returned no availability dates. Check FCC_USERNAME and FCC_API_TOKEN.")
    return dates


def fiber_files(as_of, fips_wanted, user, token):
    d = api_json(f"/downloads/listAvailabilityData/{as_of}?category=State", user, token)
    rows = d.get("data", [])
    out = defaultdict(list)
    for f in rows:
        fips = str(f.get("state_fips") or "").zfill(2)
        if fips not in fips_wanted:
            continue
        if (f.get("category") or "State") != "State":
            continue
        kind = f"{f.get('technology_type') or ''} {f.get('subcategory') or ''}".lower()
        if "mobile" in kind:
            continue
        tech = str(f.get("technology_code") or "").strip()
        name = (f.get("file_name") or "").lower()
        if tech != FIBER_TECH and not (not tech and "fiber" in name):
            continue
        if (f.get("file_type") or "csv").lower() != "csv":
            continue
        out[fips].append(f)
    if not out and rows:
        sample = [f for f in rows if str(f.get("state_fips") or "").zfill(2) in fips_wanted and "mobile" not in str(f.get("technology_type") or "").lower()] or rows
        log(f"No fiber files matched for {as_of}. Sample entries from the FCC listing:")
        for f in sample[:8]:
            log("  " + json.dumps(f))
    return out


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def iter_csv(path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith(".csv"):
                    with z.open(name) as fh:
                        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline=""))
    else:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            yield from csv.DictReader(fh)


def ingest(path, cells, states_seen):
    n = 0
    for r in iter_csv(path):
        if str(r.get("technology", "")).strip() != FIBER_TECH:
            continue
        h = (r.get("h3_res8_id") or "").strip().lower()
        if not h or not h3.is_valid_cell(h):
            continue
        st = (r.get("state_usps") or "").strip().upper()
        if st:
            states_seen.add(st)
        e = cells.get(h)
        if e is None:
            e = cells[h] = [set(), {}]
        e[0].add(r.get("location_id") or f"row{n}")
        name = (r.get("brand_name") or "").strip() or f"Provider {r.get('provider_id', '?')}"
        down = to_int(r.get("max_advertised_download_speed"))
        up = to_int(r.get("max_advertised_upload_speed"))
        p = e[1].get(name)
        if p is None:
            e[1][name] = [down, up]
        else:
            p[0] = max(p[0], down)
            p[1] = max(p[1], up)
        n += 1
    return n


def fold(total, cells):
    for h, (locs, provs) in cells.items():
        t = total.get(h)
        if t is None:
            total[h] = [len(locs), provs]
            continue
        t[0] += len(locs)
        for name, (d, u) in provs.items():
            p = t[1].get(name)
            if p is None:
                t[1][name] = [d, u]
            else:
                p[0] = max(p[0], d)
                p[1] = max(p[1], u)


def reset_dir(p):
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True)
    (p / ".gitkeep").touch()


def dump(path, obj):
    path.write_text(json.dumps(obj, separators=(",", ":"), sort_keys=True), encoding="utf-8")


def write_shards(total):
    r8, r6 = DATA / "fiber" / "r8", DATA / "fiber" / "r6"
    reset_dir(r8)
    reset_dir(r6)
    fine_shards = defaultdict(dict)
    coarse = {}
    for h, (n, provs) in total.items():
        fine_shards[h3.cell_to_parent(h, FINE_SHARD)][h] = (n, provs)
        c = h3.cell_to_parent(h, COARSE)
        e = coarse.setdefault(c, [0, set()])
        e[0] += n
        e[1].update(provs)
    for sid, cells in fine_shards.items():
        names = sorted({p for _, provs in cells.values() for p in provs})
        idx = {p: i for i, p in enumerate(names)}
        dump(r8 / f"{sid}.json", {
            "p": names,
            "c": {h: [n, [[idx[p], v[0], v[1]] for p, v in sorted(provs.items())]] for h, (n, provs) in cells.items()},
        })
    coarse_shards = defaultdict(dict)
    for c, v in coarse.items():
        coarse_shards[h3.cell_to_parent(c, COARSE_SHARD)][c] = v
    for sid, cells in coarse_shards.items():
        names = sorted({p for _, provs in cells.values() for p in provs})
        idx = {p: i for i, p in enumerate(names)}
        dump(r6 / f"{sid}.json", {
            "p": names,
            "c": {c: [n, sorted(idx[p] for p in provs)] for c, (n, provs) in cells.items()},
        })
    log(f"Wrote {len(fine_shards)} fine shards and {len(coarse_shards)} coarse shards for {len(total)} cells")


def write_manifest(as_of=None, states=None):
    path = DATA / "manifest.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    contract_files = sorted((DATA / "contracts").glob("*.json"))
    contract_states = [p.stem for p in contract_files]
    geo_states = [p.stem for p in contract_files if any(i.get("geometry") for i in json.loads(p.read_text(encoding="utf-8")).get("items", []))]
    m = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asOf": as_of if as_of is not None else old.get("asOf"),
        "states": sorted(states) if states is not None else old.get("states", []),
        "contractStates": contract_states,
        "contractGeoStates": geo_states,
        "fineRes": FINE,
        "fineShardRes": FINE_SHARD,
        "coarseRes": COARSE,
        "coarseShardRes": COARSE_SHARD,
        "source": "FCC National Broadband Map, fixed broadband availability, technology code 50 (fiber to the premises)",
    }
    path.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    log(f"Manifest: states={m['states']} asOf={m['asOf']} contracts={contract_states}")


def parse_states(s):
    s = (s or "").strip().upper()
    if not s or s == "ALL":
        return sorted(FIPS)
    out = [x.strip() for x in s.replace(" ", ",").split(",") if x.strip()]
    bad = [x for x in out if x not in FIPS]
    if bad:
        sys.exit(f"Unknown state codes: {', '.join(bad)}")
    return sorted(set(out))


def run_api(states):
    user = os.environ.get("FCC_USERNAME", "").strip()
    token = os.environ.get("FCC_API_TOKEN", "").strip()
    if not user or not token:
        sys.exit("Set FCC_USERNAME and FCC_API_TOKEN.")
    wanted = {FIPS[s] for s in states}
    files, as_of = {}, None
    for as_of in as_of_dates(user, token)[:4]:
        log(f"Checking FCC availability data: {as_of}")
        files = fiber_files(as_of, wanted, user, token)
        if files:
            break
    if not files:
        sys.exit("No fiber files found in the latest FCC data releases.")
    log(f"Using FCC availability data: {as_of}")
    missing = sorted(USPS[f] for f in wanted if f not in files)
    if missing:
        log(f"No fiber file listed for: {', '.join(missing)}")
    CACHE.mkdir(exist_ok=True)
    total, done = {}, set()
    for fips in sorted(files):
        st = USPS[fips]
        cells, seen = {}, set()
        for f in files[fips]:
            fid = f.get("file_id")
            dest = CACHE / f"{as_of}_{fid}.zip"
            if not dest.exists():
                log(f"{st}: downloading {f.get('file_name', fid)}")
                api_download(f"/downloads/downloadFile/availability/{fid}", user, token, dest)
            n = ingest(dest, cells, seen)
            log(f"{st}: {n} fiber rows, {len(cells)} cells")
            dest.unlink()
        fold(total, cells)
        done.add(st)
    return as_of, total, done


def run_local(folder, as_of):
    total, done = {}, set()
    paths = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in (".zip", ".csv"))
    if not paths:
        sys.exit(f"No .zip or .csv files in {folder}")
    for p in paths:
        cells, seen = {}, set()
        n = ingest(p, cells, seen)
        log(f"{p.name}: {n} fiber rows, {len(cells)} cells, states {sorted(seen)}")
        fold(total, cells)
        done |= seen
    return as_of, total, done


def main():
    ap = argparse.ArgumentParser(description="Build fiber availability tiles from FCC National Broadband Map data")
    ap.add_argument("--states", default=os.environ.get("STATES", "AZ"), help="Comma separated USPS codes, or ALL")
    ap.add_argument("--local", help="Folder of FCC state fixed broadband CSV or ZIP files downloaded by hand")
    ap.add_argument("--as-of", default="local data", help="Date label to show when using --local")
    ap.add_argument("--manifest-only", action="store_true", help="Only refresh data/manifest.json")
    a = ap.parse_args()
    if a.manifest_only:
        write_manifest()
        return
    if a.local:
        as_of, total, done = run_local(a.local, a.as_of)
    else:
        as_of, total, done = run_api(parse_states(a.states))
    if not total:
        sys.exit("No fiber rows found. Nothing written.")
    write_shards(total)
    write_manifest(as_of, done)


if __name__ == "__main__":
    main()
