# Ferry landings and Citi Bike: live data collector

Every 5 minutes, GitHub runs `collect.py` for free on its own servers. It records:

- **NYC Ferry**: predicted arrival time for every boat about to reach any of the 25 landings
- **Citi Bike**: bikes, e-bikes and open docks at the 79 stations within 500 m of a landing
- **MTA buses** (optional): predicted arrival times at the bus stops within 500 m of a landing

Each run polls every 30 seconds for 9 minutes, then saves what changed into `data/YYYY-MM-DD/`.
Your computer doesn't need to be on.

## Setup (about 15 minutes, all in the browser)

1. **Make a GitHub account** at github.com if you don't have one.
2. **Create a repository.** Click **+** (top right) → **New repository**.
   - Name: `ferry-citibike-collector`
   - Choose **Public**. Public repos get unlimited free Actions minutes. This collector uses about
     8,600 minutes a month, and private repos only get 2,000 free. The data is all public feeds anyway.
   - Leave "Add a README" unchecked. Click **Create repository**.
3. **Upload the files.** On the new repo page, click **uploading an existing file**.
   Drag in everything from the unzipped folder **except** the `.github` folder
   (`collect.py`, `analyze.py`, `requirements.txt`, `README.md` and the `reference` folder). The `data` folder gets created on the first run.
   Click **Commit changes**.
   - Mac Finder hides folders starting with a dot, so the `.github` folder is added separately in the next step.
4. **Add the schedule file.** Click **Add file → Create new file**. In the name box type exactly
   `.github/workflows/collect.yml` (the slashes create the folders). Open `collect.yml` from the
   download in TextEdit, copy everything, paste it into the big box, and click **Commit changes**.
5. **Start it.** Click the **Actions** tab. If asked, click the green button to enable workflows.
   Click **Collect ferry and Citi Bike data** on the left → **Run workflow** → **Run workflow**.
6. **Check the first run** after about 10 minutes. Click into the run → **collect** → expand
   **Poll feeds for 9 minutes**. Each line should look like
   `14:02:31 {'ferry': 12, 'bike': 40, 'bus': 0}`. Lines with `error:` mean a feed didn't answer.
   One or two now and then is normal, but if every line errors, send me the text.
   A `data` folder with today's date should then appear in the repo.

After that it runs on its own every 5 minutes. GitHub sometimes starts scheduled runs late,
especially at the top of the hour. Each run waits for the previous one, so gaps should be short.

### Optional: bus data

Request a free key from the link on https://bt.mta.info/developers (it usually arrives within half an hour), then in the repo go to
**Settings → Secrets and variables → Actions → New repository secret**.
Name: `MTA_BUSTIME_KEY`. Value: your key.

### Stopping

**Actions** tab → **Collect ferry and Citi Bike data** → **...** (top right) → **Disable workflow**.

## Getting and analyzing the data

1. On the repo page: **Code → Download ZIP**.
2. The easiest way to run the analysis without installing anything is **Google Colab**
   (colab.research.google.com): New notebook → folder icon on the left → upload the ZIP, then run:
   ```
   !unzip -q ferry-citibike-collector-main.zip
   %cd ferry-citibike-collector-main
   !python analyze.py
   ```
   To add Citi Bike trip files (https://s3.amazonaws.com/tripdata/index.html, posted about a week
   after month end), upload them too and run `!python analyze.py --trips 2026*citibike-tripdata*.csv`.
3. Results go to `out/`:

| file | what it tells you |
|---|---|
| `ferry_arrivals.csv` | every landing observed: actual time, scheduled time, minutes late |
| `dock_change.csv` | for each landing and each nearby station: bikes just before, bikes 15 min after, the usual 15 min change at that station and hour, and `extra_taken` (bikes taken beyond usual) |
| `landing_summary.csv` | per landing: landings observed, average extra bikes taken, stations and docks within 500 m |
| `trip_response.csv` | with trip files: trips that actually started 0 to 15 min after each landing vs the usual |
| `bus_connection.csv` | with bus data: minutes from walking off the ferry to the next actual bus, by route |

## How to read it

- **Landing time** = the last arrival prediction NYC Ferry published before the boat got in.
  NYC Ferry doesn't publish boat GPS, so this is the best live signal available.
- **Dock counts are net.** A drop of 3 bikes could be 4 taken and 1 returned, and rebalancing vans
  move bikes too. That's why `extra_taken` compares against the usual change at the same station and
  hour, measured in windows with no landing nearby.
- **Trip files settle it.** They list every trip with its exact start time, so `trip_response.csv` is
  the strongest evidence. Live dock counts show the pattern sooner.
- **Ferry landings closer together than 15 minutes overlap**, so their effects can't be separated
  at that landing.

## Files

```
collect.py                           the collector GitHub runs
analyze.py                           builds the tables in out/
.github/workflows/collect.yml        the every-5-minutes schedule
reference/ferry_landings.csv         25 landings with coordinates
reference/bike_stations_near_ferry.csv   Citi Bike stations within 500 m of each landing
reference/bus_stops_near_ferry.csv   bus stops within 500 m of each landing
reference/ferry_gtfs/                NYC Ferry schedule, for scheduled vs actual times
data/                                collected data (filled in by GitHub)
```
