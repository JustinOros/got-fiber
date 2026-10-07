import argparse
import csv
import io
import json
import os
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import h3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_fcc import CACHE, DATA, FIPS, USPS, api_download, as_of_dates, dump, log, parse_states, reset_dir
from build_bead import availability_files

RES = 7
SHARD = 4
TECH = "61"


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def is_starlink(brand, provider):
    s = f"{brand} {provider}".lower()
    return "starlink" in s or "space exploration" in s or "spacex" in s


def rows(path):
    openers = []
    if path.suffix.lower() == ".zip":
        z = zipfile.ZipFile(path)
        for n in z.namelist():
            if n.lower().endswith(".csv"):
                openers.append(lambda n=n: io.TextIOWrapper(z.open(n), encoding="utf-8-sig", newline=""))
    else:
        openers.append(lambda: open(path, encoding="utf-8-sig", newline=""))
    for o in openers:
        with o() as fh:
            r = csv.reader(fh)
            head = next(r, None)
            if not head:
                continue
            idx = {k: head.index(k) if k in head else None for k in
                   ("brand_name", "provider_id", "technology", "h3_res8_id",
                    "max_advertised_download_speed", "max_advertised_upload_speed")}
            if idx["h3_res8_id"] is None:
                continue
            top = max(i for i in idx.values() if i is not None)
            g = lambda row, k: row[idx[k]] if idx[k] is not None else ""
            for row in r:
                if len(row) <= top:
                    continue
                if idx["technology"] is not None and g(row, "technology").strip() != TECH:
                    continue
                yield g(row, "brand_name"), g(row, "provider_id"), g(row, "h3_res8_id"), g(row, "max_advertised_download_speed"), g(row, "max_advertised_upload_speed")


def ingest(path, cells):
    n = 0
    for brand, provider, cell, down, up in rows(path):
        if not is_starlink(brand, provider):
            continue
        cell = cell.strip().lower()
        if not h3.is_valid_cell(cell):
            continue
        c7 = h3.cell_to_parent(cell, RES)
        e = cells.get(c7)
        d, u = to_int(down), to_int(up)
        if e is None:
            cells[c7] = [d, u]
        else:
            e[0] = max(e[0], d)
            e[1] = max(e[1], u)
        n += 1
    return n


def write(cells, states, as_of):
    root = DATA / "starlink"
    reset_dir(root)
    shards = defaultdict(dict)
    for c, v in cells.items():
        shards[h3.cell_to_parent(c, SHARD)][c] = v
    for sid, cs in shards.items():
        tiers = sorted({tuple(v) for v in cs.values()})
        idx = {t: i for i, t in enumerate(tiers)}
        dump(root / f"{sid}.json", {"t": [list(t) for t in tiers], "c": {c: idx[tuple(v)] for c, v in cs.items()}})
    path = DATA / "manifest.json"
    m = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    m.update({
        "starlinkStates": sorted(states),
        "starlinkAsOf": as_of,
        "starlinkRes": RES,
        "starlinkShardRes": SHARD,
        "starlinkGenerated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    path.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    log(f"Starlink: {len(cells)} cells in {len(shards)} shards, states {sorted(states)}")


def main():
    ap = argparse.ArgumentParser(description="Build Starlink availability tiles from FCC data")
    ap.add_argument("--states", default=os.environ.get("STATES", "AZ"))
    ap.add_argument("--local", help="Folder of FCC satellite availability CSV or ZIP files")
    ap.add_argument("--as-of", default="local data")
    a = ap.parse_args()
    states = parse_states(a.states)
    cells, done = {}, set()
    if a.local:
        for p in sorted(Path(a.local).iterdir()):
            if p.suffix.lower() in (".zip", ".csv"):
                log(f"{p.name}: {ingest(p, cells)} Starlink rows")
        done = set(states)
        as_of = a.as_of
    else:
        user = os.environ.get("FCC_USERNAME", "").strip()
        token = os.environ.get("FCC_API_TOKEN", "").strip()
        if not user or not token:
            sys.exit("Set FCC_USERNAME and FCC_API_TOKEN.")
        wanted = {FIPS[s] for s in states}
        files, as_of = {}, None
        for as_of in as_of_dates(user, token)[:4]:
            files = availability_files(as_of, wanted, user, token)
            if any(TECH in v for v in files.values()):
                break
        CACHE.mkdir(exist_ok=True)
        for fips in sorted(files):
            st = USPS[fips]
            for f in files[fips].get(TECH, []):
                fid = f.get("file_id")
                dest = CACHE / f"{as_of}_{fid}.zip"
                if not dest.exists():
                    log(f"{st}: downloading {f.get('file_name', fid)}")
                    api_download(f"/downloads/downloadFile/availability/{fid}", user, token, dest)
                log(f"{st}: {ingest(dest, cells)} Starlink rows, {len(cells)} cells so far")
                dest.unlink()
                done.add(st)
    if not cells:
        sys.exit("No Starlink rows found. Nothing written.")
    write(cells, done, as_of)


if __name__ == "__main__":
    main()
