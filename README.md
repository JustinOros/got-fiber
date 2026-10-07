# Got Fiber

A static web app for GitHub Pages. Enter a US home address and it shows:

- Whether fiber internet is reported at that address, and by which providers at what speeds
- Every area within 50 miles where fiber is reported, shaded on a map
- Broadband contracts, awards, and open bids near the address (BEAD, RDOF, state grants)

## How it works

The page is plain HTML and JavaScript. It geocodes the address with OpenStreetMap Nominatim, then reads small JSON tiles from `data/`.

A GitHub Actions workflow downloads FCC National Broadband Map data (fixed broadband, technology code 50, fiber to the premises), aggregates it into H3 hexagons, and commits the tiles back to the repo. Nothing secret ever reaches the browser.

| Path | What it holds |
| --- | --- |
| `data/fiber/r8/` | Address level detail: H3 resolution 8 cells (about 0.28 sq mi) with providers and max speeds, sharded by resolution 5 parent |
| `data/fiber/r6/` | Map level detail: H3 resolution 6 cells (about 14 sq mi) with location counts, sharded by resolution 3 parent |
| `data/contracts/XX.json` | Hand maintained contracts and bids per state |
| `data/manifest.json` | Which states are loaded and the FCC data date |

## Setup

1. Create a GitHub repo and push these files to `main`.
2. Get an FCC API token: sign in at https://broadbandmap.fcc.gov, open the account menu, choose Manage API Access, and generate a token.
3. In the repo, go to Settings, Secrets and variables, Actions:
   - Secret `FCC_USERNAME`: the email you use on the FCC site
   - Secret `FCC_API_TOKEN`: the token from step 2
   - Variable `STATES` (optional): `AZ` by default, a list like `AZ,NM,NV,UT,CA`, or `ALL`
4. Run Actions, Update fiber data, Run workflow. It repeats on the 5th of each month.
5. Turn on Settings, Pages, Deploy from a branch, `main`, `/ (root)`.

### Building without the API

Download the state files by hand from https://broadbandmap.fcc.gov/data-download (By State, Fixed Broadband, Fiber to the Premises) into a folder, then:

```
pip install -r scripts/requirements.txt
python scripts/build_fcc.py --local path/to/folder --as-of "June 30, 2025"
```

## BEAD project areas

`scripts/build_bead.py` downloads the BEAD Final Proposal data compiled by [BroadbandExpanded](https://broadbandexpanded.com/funding/beadfinalproposaldata) (every state's funded projects and the FCC location IDs in each), then maps those locations to H3 cells using FCC availability files. The page shades those cells purple and lists the projects within 50 miles. It runs as part of the Update fiber data workflow and writes to `data/bead/`.

Please credit BroadbandExpanded if you reuse this data.

## Starlink

`scripts/build_starlink.py` reads the FCC low Earth orbit satellite availability files (technology code 61), keeps the rows reported by Starlink, and writes the max advertised speeds per H3 resolution 7 cell to `data/starlink/`. The page shows one line in the result saying whether Starlink reports service at the address.

## Adding contracts and bids

Edit or add `data/contracts/XX.json` (XX is the state code). Pushing a change refreshes the manifest automatically.

```json
{
  "state": "AZ",
  "programs": [
    { "name": "Arizona BEAD", "office": "Arizona Commerce Authority", "url": "https://...", "summary": "One line status." }
  ],
  "items": [
    {
      "id": "unique-id",
      "program": "BEAD",
      "awardee": "Provider name, or leave out for an open bid",
      "status": "open | proposed | awarded | construction | complete | defaulted",
      "technology": "Fiber",
      "amount": 1000000,
      "locations": 500,
      "deadline": "2026-11-30",
      "scope": "statewide | county | area",
      "counties": ["Pima"],
      "geometry": { "type": "Point", "coordinates": [-110.97, 32.22] },
      "notes": "Anything useful",
      "source": "https://...",
      "updated": "2026-10"
    }
  ]
}
```

An item shows for an address when its `geometry` (Point, Polygon, or MultiPolygon, in `[lng, lat]` order) is within 50 miles, when the address county is in `counties`, or when `scope` is `statewide`. Open bids are listed first and drawn in yellow.

Good sources for project areas: the FCC Broadband Funding Map (https://broadbandmap.fcc.gov/funding-map) for RDOF, ReConnect, and other federal awards, and each state broadband office for BEAD awards and open rounds.

## Coverage and limits

- US only: all 50 states, DC, and Puerto Rico. Canada publishes only coarse speed data with no technology breakdown, and Mexico has no usable public data.
- FCC data is self reported by providers and lags about six months. Always confirm with the provider.
- Address level results are per H3 cell, not per house, because the FCC location fabric with exact coordinates is licensed and not public.
- Nominatim allows about one search per second, which is fine for personal use.

## Credits

Fiber data from the FCC National Broadband Map. Map tiles by CARTO on OpenStreetMap data. Hexagons by Uber H3.
